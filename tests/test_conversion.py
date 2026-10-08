import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from convert import (InvalidDomain, ROOT, UniqueLoader, build, convert_payload, domain_pattern,
                     shadowrocket_rules, shadowrocket_wildcards)


class ConversionTests(unittest.TestCase):
    def test_optional_wildcards_preserve_patterns_and_report_unrepresentable_regex(self):
        payload = ["+.example.*", "*.service.example", ".sub.example", "*.service.example",
                   "DOMAIN-REGEX,^arbitrary[0-9]+$", "+.literal-?.*.example", "+.plain.example",
                   "DOMAIN-SUFFIX,.typed.example,DIRECT"]
        lines, represented = shadowrocket_wildcards(payload)
        self.assertEqual(lines, ["DOMAIN-WILDCARD,*.example.*", "DOMAIN-WILDCARD,*.service.example",
                                 "DOMAIN-WILDCARD,*.sub.example", "DOMAIN-WILDCARD,*.typed.example",
                                 "DOMAIN-WILDCARD,example.*"])
        self.assertEqual(len(represented), 4)
        rule, *_ = convert_payload(payload, "Test.yaml", set())
        main, excluded = shadowrocket_rules(rule)
        self.assertEqual(main, ["DOMAIN-SUFFIX,plain.example"])
        self.assertEqual(sum((e["field"], e["value"]) not in represented for e in excluded), 2)

    def test_shadowrocket_export_has_no_policy_or_ip_and_preserves_literals(self):
        rule, *_ = convert_payload(["DOMAIN,EXACT.example,REJECT", "+.example.net", "DOMAIN-KEYWORD,video",
                                    "IP-CIDR,192.0.2.0/24,no-resolve", "+.awsdns-cn-??.com"], "Mixed.yaml", set())
        lines, excluded = shadowrocket_rules(rule)
        self.assertEqual(lines, ["DOMAIN,exact.example", "DOMAIN-KEYWORD,video",
                                 "DOMAIN-SUFFIX,awsdns-cn-??.com", "DOMAIN-SUFFIX,example.net"])
        self.assertEqual(excluded, [])

    def test_shadowrocket_unconfirmed_regex_and_subdomain_only_are_reported(self):
        rule, *_ = convert_payload(["*.example.com", ".example.org", "DOMAIN,ok.example"], "Test.yaml", set())
        lines, excluded = shadowrocket_rules(rule)
        self.assertEqual(lines, ["DOMAIN,ok.example"])
        self.assertEqual({item["field"] for item in excluded}, {"domain_regex", "domain_suffix"})
        self.assertEqual({item["value"] for item in excluded}, {r"^[^.]+\.example\.com$", ".example.org"})
        for value in ["bad,value", "bad\nDOMAIN,other.example", " bad", ""]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                shadowrocket_rules({"domain": [value]})

    def test_extract_mixed_domains_and_ignore_ips(self):
        payload = ["DOMAIN,EXAMPLE.com", "DOMAIN-SUFFIX,example.net",
                   "DOMAIN-KEYWORD,video", "IP-CIDR,192.0.2.0/24,no-resolve",
                   "IP-CIDR6,2001:db8::/32", "GEOIP,cn", "192.0.2.1/32",
                   "PROCESS-NAME,app", "+.example.org", "DOMAIN,example.com"]
        rule, stats, ignored, _, issues = convert_payload(payload, "Mixed.yaml", set())
        self.assertEqual(rule, {"domain": ["example.com"], "domain_keyword": ["video"],
                                "domain_suffix": ["example.net", "example.org"]})
        self.assertEqual(stats["ignored_non_domain_entries"], 5)
        self.assertEqual(stats["duplicate_entries"], 1)
        self.assertEqual(ignored["bare_ip_cidr"], 1)
        self.assertEqual(issues, [])

    def test_only_ip_payload_has_no_domain_rule(self):
        rule, stats, *_ = convert_payload(["IP-CIDR,192.0.2.0/24", "2001:db8::/32", "GEOIP,cn"], "IP.yaml", set())
        self.assertEqual(rule, {})
        self.assertEqual(stats["converted_entries"], 0)

    def test_upstream_policy_is_recorded_not_turned_into_a_predicate(self):
        rule, _, _, policies, _ = convert_payload(["DOMAIN,ads.example,REJECT-DROP",
                                                   "DOMAIN,cdn.example,DIRECT,no-resolve"], "Mixed.yaml", set())
        self.assertEqual(policies, {"DIRECT": 1, "REJECT-DROP": 1})
        self.assertEqual(set(rule), {"domain"})

    def test_exact_suffix_and_subdomain_only_remain_distinct(self):
        self.assertEqual(domain_pattern("example.com"), ("domain", "example.com"))
        self.assertEqual(domain_pattern("+.example.com"), ("domain_suffix", "example.com"))
        self.assertEqual(domain_pattern(".example.com"), ("domain_suffix", ".example.com"))

    def test_wildcard_has_label_boundaries(self):
        _, regex = domain_pattern("*.example.com")
        self.assertRegex("api.example.com", regex)
        for domain in ["example.com", "a.b.example.com", "evil-example.com", "api.example.com.evil", ".example.com"]:
            self.assertIsNone(re.search(regex, domain), domain)

    def test_complex_wildcard_includes_apex_and_all_subdomains(self):
        _, regex = domain_pattern("+.example.*.*")
        for domain in ["example.co.uk", "api.example.co.uk", "a.b.example.co.uk"]:
            self.assertRegex(domain, regex)
        for domain in ["example.com", "example.co.uk.evil", "evilexample.co.uk"]:
            self.assertIsNone(re.search(regex, domain), domain)

    def test_question_mark_is_literal(self):
        field, value = domain_pattern("+.awsdns-cn-??.com")
        self.assertEqual((field, value), ("domain_suffix", "awsdns-cn-??.com"))
        *_, issues = convert_payload(["+.awsdns-cn-??.com"], "Domain.yaml", set())
        self.assertEqual(issues[0]["status"], "preserved_literal")

    def test_new_invalid_pattern_blocks_build(self):
        for value in ["*.bad.", "+.+.example.com", "+.*partial.example", "+.regexp:^example$"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                convert_payload([value], "Domain.yaml", set())

    def test_known_invalid_pattern_is_excluded_with_evidence(self):
        rule, stats, *_, issues = convert_payload(["*.bad.", "+.ok.example"], "Domain.yaml", {("Domain.yaml", "*.bad.")})
        self.assertEqual(rule, {"domain_suffix": ["ok.example"]})
        self.assertEqual(stats["excluded_invalid_entries"], 1)
        self.assertEqual(issues[0]["value"], "*.bad.")

    def test_unhandled_logical_or_new_domain_type_fails(self):
        for value in ["DOMAIN-WILDCARD,*.example.com", "AND,((DOMAIN,x),(DST-PORT,443))", "DOMAIN,"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                convert_payload([value], "Domain.yaml", set())

    def test_yaml_comments_do_not_become_rules(self):
        data = yaml.load("payload:\n  # - '+.disabled.example'\n  - '+.enabled.example' # comment\n", Loader=UniqueLoader)
        rule, *_ = convert_payload(data["payload"], "Domain.yaml", set())
        self.assertEqual(rule, {"domain_suffix": ["enabled.example"]})

    def test_duplicate_yaml_keys_and_nonstring_entries_fail(self):
        with self.assertRaises(ValueError):
            yaml.load("payload: []\npayload: ['+.lost.example']", Loader=UniqueLoader)
        for value in [None, 123, {"DOMAIN": "example.com"}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                convert_payload([value], "Domain.yaml", set())


@unittest.skipUnless(os.environ.get("SING_BOX_BINARY"), "set SING_BOX_BINARY to run official compiler integration tests")
class CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = str(Path(os.environ["SING_BOX_BINARY"]).resolve())

    def compile_rule(self, directory, rule):
        source, binary = directory / "rules.json", directory / "rules.srs"
        source.write_text(json.dumps({"version": 2, "rules": [rule]}))
        subprocess.run([self.compiler, "rule-set", "compile", "--output", str(binary), str(source)], check=True, capture_output=True)
        self.assertEqual(binary.read_bytes()[:4], b"SRS\x02")
        return binary

    def matches(self, binary, domain):
        result = subprocess.run([self.compiler, "rule-set", "match", "--format", "binary", str(binary), domain], check=True, capture_output=True, text=True)
        return "match rules.[" in result.stdout + result.stderr

    def test_actual_binary_matching(self):
        cases = [
            ("example.com", ["example.com"], ["api.example.com", "evilexample.com"]),
            ("+.example.com", ["example.com", "a.b.example.com"], ["evilexample.com", "example.com.evil"]),
            (".example.com", ["a.example.com", "a.b.example.com"], ["example.com", "evilexample.com"]),
            ("*.example.com", ["a.example.com"], ["example.com", "a.b.example.com"]),
            ("+.example.*", ["example.com", "a.b.example.net"], ["example.co.uk", "evilexample.com"]),
            ("+.awsdns-cn-??.com", ["awsdns-cn-??.com"], ["awsdns-cn-12.com"]),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            for pattern, yes, no in cases:
                with self.subTest(pattern=pattern):
                    field, value = domain_pattern(pattern)
                    binary = self.compile_rule(Path(temporary), {field: [value]})
                    for domain in yes:
                        self.assertTrue(self.matches(binary, domain), (pattern, domain))
                    for domain in no:
                        self.assertFalse(self.matches(binary, domain), (pattern, domain))

    def source_fixture(self, path, files):
        path.mkdir()
        (path / "LICENSE").write_text("MIT License\nCopyright fixture\n")
        for name, content in files.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        for args in [("init", "-b", "main"), ("add", "."),
                     ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Fixture")]:
            subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    def test_build_is_complete_deterministic_and_ip_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            self.source_fixture(source, {
                "Group/Test.yaml": "payload:\n  - DOMAIN,exact.example\n  - DOMAIN-KEYWORD,video\n  - IP-CIDR,192.0.2.0/24\n",
                "Group/Test_Domain.yml": "payload:\n  - '+.example.*'\n",
                "Group/Only_IP.yaml": "payload:\n  - '2001:db8::/32'\n",
            })
            one, two = root / "one", root / "two"
            manifest = build(source, one, Path(self.compiler))
            build(source, two, Path(self.compiler))
            self.assertEqual(manifest["source_files"], 3)
            self.assertEqual(manifest["generated_rule_sets"], 2)
            self.assertEqual((one / "json/manifest.json").read_bytes(), (one / "srs/manifest.json").read_bytes())
            self.assertEqual((one / "json/manifest.json").read_bytes(), (one / "shadowrocket/manifest.json").read_bytes())
            self.assertEqual(manifest["shadowrocket"], {"generated_rule_sets": 1, "unique_entries": 2,
                                                       "excluded_from_primary_entries": 1,
                                                       "generated_wildcard_rule_sets": 1, "wildcard_unique_entries": 2,
                                                       "wildcard_covered_predicates": 1, "unrepresented_entries": 0})
            self.assertEqual([i["source"] for i in manifest["issues"] if i["status"] == "excluded_shadowrocket_primary"],
                             ["Group/Test_Domain.yml"])
            self.assertEqual((one / "shadowrocket/Group/Test.list").read_text().splitlines()[4:],
                             ["DOMAIN,exact.example", "DOMAIN-KEYWORD,video"])
            self.assertFalse((one / "shadowrocket/Group/Test_Domain.list").exists())
            self.assertFalse((one / "shadowrocket/Group/Only_IP.list").exists())
            self.assertNotIn("Test_Domain.list", (one / "shadowrocket/INDEX.md").read_text())
            self.assertIn("Test_Domain.wildcard.list", (one / "shadowrocket/INDEX.md").read_text())
            self.assertEqual((one / "shadowrocket/Group/Test_Domain.wildcard.list").read_text().splitlines()[4:],
                             ["DOMAIN-WILDCARD,*.example.*", "DOMAIN-WILDCARD,example.*"])
            self.assertFalse((one / "srs/Group/Only_IP.srs").exists())
            for path in one.rglob("*"):
                if path.is_file():
                    self.assertEqual(path.read_bytes(), (two / path.relative_to(one)).read_bytes())
                if path.suffix == ".json" and path.name != "manifest.json":
                    content = json.loads(path.read_text())
                    for rule in content["rules"]:
                        self.assertLessEqual(set(rule), {"domain", "domain_suffix", "domain_regex", "domain_keyword"})
            with self.assertRaises(ValueError):
                build(source, one, Path(self.compiler))

    def test_path_collision_and_invalid_regex_do_not_publish_partial_output(self):
        cases = [
            {"Test.yaml": "payload: ['+.ok.example']", "Test.yml": "payload: ['+.other.example']"},
            {"Test.yaml": "payload: ['DOMAIN-REGEX,[']"},
            {"Test.yaml": "payload: ['+.example.*']", "Test.wildcard.yaml": "payload: ['+.other.example']"},
        ]
        for files in cases:
            with self.subTest(files=files), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output = root / "source", root / "dist"
                self.source_fixture(source, files)
                with self.assertRaises((ValueError, RuntimeError)):
                    build(source, output, Path(self.compiler))
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

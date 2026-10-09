import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_upstream import ROOT
from convert import build
from google_cn import load_snapshot, subtract, suffix_matches


def snapshot(path, data):
    path.mkdir()
    body = json.dumps(data).encode()
    (path / "input.json").write_bytes(body)
    config = json.loads((ROOT / "toolchain.json").read_text())
    metadata = {key: config["google_cn"][key] for key in ("repository", "path", "ref")}
    metadata.update(commit="a" * 40, sha256=hashlib.sha256(body).hexdigest())
    (path / "metadata.json").write_text(json.dumps(metadata))
    return config


class GoogleCnTests(unittest.TestCase):
    def test_domain_subtraction_uses_label_boundaries_and_preserves_unrelated_names(self):
        base = {"domain": ["fonts.googleapis.com", "api.fonts.googleapis.com", "notfonts.googleapis.com",
                           "fonts.googleapis.com.evil", "google.cn"]}
        kept, logical, lines, removed, *_ = subtract(base, {"domain_suffix": ["fonts.googleapis.com"]})
        self.assertEqual(kept["domain"], ["notfonts.googleapis.com", "fonts.googleapis.com.evil", "google.cn"])
        self.assertEqual([r["value"] for r in removed], ["fonts.googleapis.com", "api.fonts.googleapis.com"])
        self.assertTrue(logical["rules"][1]["invert"])
        self.assertTrue(all(line.startswith("DOMAIN,") for line in lines))
        self.assertFalse(suffix_matches("example.com", ".example.com"))
        self.assertTrue(suffix_matches("a.example.com", ".example.com"))

    def test_partial_suffix_is_retained_with_a_negative_guard(self):
        base = {"domain_suffix": ["example.com", "entire.example.net", "unrelated.cn"]}
        blocked = {"domain": ["one.example.com"], "domain_suffix": ["ai.example.com", "example.net"]}
        kept, logical, lines, removed, _, guarded = subtract(base, blocked)
        self.assertEqual(kept["domain_suffix"], ["example.com", "unrelated.cn"])
        self.assertEqual(removed, [{"field": "domain_suffix", "value": "entire.example.net"}])
        self.assertEqual(guarded, [{"field": "domain_suffix", "value": "example.com"}])
        self.assertTrue(lines[0].startswith("AND,((DOMAIN-SUFFIX,example.com),(NOT,((OR,"))
        self.assertEqual(lines[1], "DOMAIN-SUFFIX,unrelated.cn")
        self.assertEqual(logical["rules"][1], {**blocked, "invert": True})

    def test_regex_is_preserved_for_sing_box_and_not_approximated_for_shadowrocket(self):
        base = {"domain_regex": [r"^r[0-9]+\.example\.com$"], "domain_suffix": ["keep.cn"]}
        kept, logical, lines, _, unsupported, _ = subtract(base, {"domain_suffix": ["ai.example.com"]})
        self.assertEqual(kept["domain_regex"], base["domain_regex"])
        self.assertEqual(lines, ["DOMAIN-SUFFIX,keep.cn"])
        self.assertEqual(unsupported[0]["field"], "domain_regex")
        with self.assertRaisesRegex(ValueError, "cannot be exported equivalently"):
            subtract(base, {"domain_regex": [r"^ai\."]})

    def test_snapshot_hash_and_new_conditional_syntax_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input"
            config = snapshot(path, {"version": 2, "rules": [{"domain": ["keep.cn"], "ip_cidr": ["192.0.2.0/24"]}]})
            base, _, ignored = load_snapshot(config, path)
            self.assertEqual(base, {"domain": ["keep.cn"]})
            self.assertEqual(ignored, {"ip_cidr": 1})
            (path / "input.json").write_text('{"version":2,"rules":[]}')
            with self.assertRaisesRegex(ValueError, "hash or commit"):
                load_snapshot(config, path)
        for body in ({"version": 2, "rules": [{"domain": ["keep.cn"], "invert": True}]},
                     {"version": 2, "rules": [{"type": "logical", "mode": "and", "rules": []}]}):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "input"
                config = snapshot(path, body)
                with self.assertRaisesRegex(ValueError, "Unsupported Google CN source field"):
                    load_snapshot(config, path)


@unittest.skipUnless(os.environ.get("SING_BOX_BINARY"), "set SING_BOX_BINARY for official compiler tests")
class GoogleCnCompilerTests(unittest.TestCase):
    def test_complete_derived_build_and_real_matching_of_partial_overlap(self):
        compiler = Path(os.environ["SING_BOX_BINARY"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "Gemini").mkdir(parents=True)
            (source / "LICENSE").write_text("MIT License")
            (source / "Gemini/Gemini.yaml").write_text("payload: ['DOMAIN-SUFFIX,ai.example.com', 'DOMAIN,fonts.example.net']")
            for command in (("init", "-b", "main"), ("add", "."),
                            ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Fixture")):
                subprocess.run(["git", *command], cwd=source, check=True, capture_output=True)
            input_dir = root / "input"
            snapshot(input_dir, {"version": 2, "rules": [{"domain": ["fonts.example.net", "keep.cn"],
                     "domain_suffix": ["example.com"], "domain_regex": [r"^r[0-9]+\.example\.net$"]}]})
            output = root / "output"
            manifest = build(source, output, compiler, input_dir)
            self.assertEqual(manifest["generated_rule_sets"], 2)
            record = manifest["derived_rule_sets"][0]
            self.assertEqual(record["derivation"]["removed_entries"], [{"field": "domain", "value": "fonts.example.net"}])
            self.assertEqual(record["shadowrocket_unrepresented_entries"], 1)
            self.assertTrue((output / "shadowrocket" / record["shadowrocket"]).exists())
            self.assertFalse(list((output / "shadowrocket/Google").glob("*.wildcard.list")))
            for branch in ("json", "srs", "shadowrocket"):
                self.assertTrue((output / branch / "LICENSE.meta-rules-dat").is_file())
                self.assertIn("Google/google%40cn_no_gemini", (output / branch / "INDEX.md").read_text())
            binary = output / "srs" / record["srs"]
            for domain, expected in (("keep.cn", True), ("www.example.com", True), ("r12.example.net", True),
                                      ("fonts.example.net", False), ("ai.example.com", False), ("a.ai.example.com", False),
                                      ("unrelated.example.net", False), ("example.com.evil", False)):
                result = subprocess.run([str(compiler), "rule-set", "match", "--format", "binary", str(binary), domain],
                                        check=True, capture_output=True, text=True)
                self.assertEqual("match rules.[" in result.stdout + result.stderr, expected, domain)

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_upstream import ROOT
from convert import build
from proxy_rules import FILES, fetch_snapshot, load_snapshot, snapshot_digest


def proxy_fixture(directory, text):
    directory.mkdir()
    config = json.loads((ROOT / "toolchain.json").read_text())
    bodies = {"input.yaml": text.encode(), "LICENSE.upstream.txt": (ROOT / "licenses/LICENSE.blackmatrix7").read_bytes(),
              "README.proxy.md.txt": b"Proxy source description", "README.upstream.md.txt": b"Original special statements"}
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in bodies.items()}
    for name, body in bodies.items():
        (directory / name).write_bytes(body)
    metadata = {key: config["proxy"][key] for key in ("repository", "ref", "path", "readme")}
    metadata.update(commit="a" * 40, sha256=hashes["input.yaml"], files_sha256=hashes,
                    snapshot_sha256=snapshot_digest(hashes))
    (directory / "metadata.json").write_text(json.dumps(metadata))
    return config, bodies


class ProxySnapshotTests(unittest.TestCase):
    def test_pinned_fetch_and_unrelated_commits_do_not_change_input_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, bodies = proxy_fixture(root / "fixture", "payload: ['DOMAIN,exact.example']")
            urls = []

            def response(url, timeout):
                urls.append(url)
                for name, key in FILES.items():
                    path = config["proxy"][key] if key in {"path", "readme"} else key
                    if url.endswith("/" + path):
                        return io.BytesIO(bodies[name])
                raise AssertionError(url)

            with patch("proxy_rules.git", side_effect=["a" * 40 + "\trefs/heads/master", "b" * 40 + "\trefs/heads/master"]), \
                    patch("proxy_rules.urlopen", side_effect=response):
                one = fetch_snapshot(config, root / "one")
                two = fetch_snapshot(config, root / "two")
            self.assertNotEqual(one["commit"], two["commit"])
            self.assertEqual(one["snapshot_sha256"], two["snapshot_sha256"])
            self.assertEqual(load_snapshot(config, root / "one")[0], "payload: ['DOMAIN,exact.example']")
            self.assertTrue(all("/" + "a" * 40 + "/" in url for url in urls[:4]))
            self.assertTrue(all("/" + "b" * 40 + "/" in url for url in urls[4:]))

    def test_source_docs_license_and_metadata_integrity_are_checked(self):
        for target in (*FILES, "metadata.json"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "snapshot"
                config, _ = proxy_fixture(directory, "payload: ['DOMAIN,exact.example']")
                load_snapshot(config, directory)
                if target == "metadata.json":
                    metadata = json.loads((directory / target).read_text())
                    metadata["path"] = "rule/Clash/Proxy/Proxy.yaml"
                    (directory / target).write_text(json.dumps(metadata))
                else:
                    (directory / target).write_bytes(b"altered")
                with self.assertRaises(ValueError):
                    load_snapshot(config, directory)


@unittest.skipUnless(os.environ.get("SING_BOX_BINARY"), "set SING_BOX_BINARY for official compiler integration tests")
class ProxyBuildTests(unittest.TestCase):
    def source_fixture(self, path):
        (path / "Gemini").mkdir(parents=True)
        (path / "LICENSE").write_text("MIT License")
        (path / "Base.yaml").write_text("payload: ['DOMAIN-SUFFIX,base.example']")
        (path / "Gemini/Gemini.yaml").write_text("payload: ['DOMAIN-SUFFIX,ai.example']")
        for command in (("init", "-b", "main"), ("add", "."),
                        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Fixture")):
            subprocess.run(["git", *command], cwd=path, check=True, capture_output=True)

    def test_three_formats_provenance_statistics_matching_and_previous_outputs(self):
        compiler = Path(os.environ["SING_BOX_BINARY"]).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            self.source_fixture(source)
            google = root / "google"
            google.mkdir()
            body = json.dumps({"version": 2, "rules": [{"domain": ["ai.example", "keep.cn"]}]}).encode()
            (google / "input.json").write_bytes(body)
            config = json.loads((ROOT / "toolchain.json").read_text())
            metadata = {key: config["google_cn"][key] for key in ("repository", "ref", "path")}
            metadata.update(commit="b" * 40, sha256=hashlib.sha256(body).hexdigest())
            (google / "metadata.json").write_text(json.dumps(metadata))
            proxy = root / "proxy"
            proxy_fixture(proxy, "payload: ['DOMAIN,EXACT.example', 'DOMAIN,exact.example', 'DOMAIN-SUFFIX,suffix.example', "
                          "'DOMAIN-KEYWORD,needle', 'IP-CIDR,192.0.2.0/24,no-resolve', 'IP-CIDR6,2001:db8::/32']")
            before, after = root / "before", root / "after"
            old = build(source, before, compiler, google)
            manifest = build(source, after, compiler, google, proxy)
            self.assertEqual(manifest["derived_rule_sets"], old["derived_rule_sets"])
            self.assertEqual(manifest["generated_rule_sets"], old["generated_rule_sets"] + 1)
            record = manifest["additional_rule_sets"][0]
            self.assertEqual(record["ignored_types"], {"IP-CIDR": 1, "IP-CIDR6": 1})
            self.assertEqual(record["duplicate_entries"], 1)
            self.assertEqual(record["unique_entries"], 3)
            self.assertEqual(record["domain_counts"], {"domain": 1, "domain_keyword": 1, "domain_suffix": 1})
            self.assertEqual(record["shadowrocket_unrepresented_entries"], 0)
            for branch, suffix in (("json", "json"), ("srs", "srs"), ("shadowrocket", "list")):
                self.assertTrue((after / branch / f"Proxy/Proxy_Classical.{suffix}").exists())
                self.assertIn("Proxy/Proxy_Classical", (after / branch / "INDEX.md").read_text())
                self.assertTrue((after / branch / "LICENSE.blackmatrix7").is_file())
                self.assertTrue((after / branch / "SOURCE.proxy/README.upstream.md.txt").is_file())
                for path in (before / branch).rglob("*"):
                    if path.is_file() and path.name not in {"manifest.json", "INDEX.md"}:
                        self.assertEqual(path.read_bytes(), (after / branch / path.relative_to(before / branch)).read_bytes())
                self.assertEqual((after / branch / "manifest.json").read_bytes(), (after / "json/manifest.json").read_bytes())
            lines = (after / "shadowrocket/Proxy/Proxy_Classical.list").read_text().splitlines()[4:]
            self.assertEqual(lines, ["DOMAIN,exact.example", "DOMAIN-KEYWORD,needle", "DOMAIN-SUFFIX,suffix.example"])
            binary = after / "srs/Proxy/Proxy_Classical.srs"
            for domain, expected in (("exact.example", True), ("sub.exact.example", False), ("suffix.example", True),
                                      ("a.suffix.example", True), ("evilsuffix.example", False),
                                      ("contains-needle.test", True), ("unrelated.invalid", False), ("192.0.2.1", False)):
                result = subprocess.run([str(compiler), "rule-set", "match", "--format", "binary", str(binary), domain],
                                        check=True, capture_output=True, text=True)
                self.assertEqual("match rules.[" in result.stdout + result.stderr, expected, domain)

    def test_unknown_syntax_duplicate_yaml_and_collisions_leave_no_partial_build(self):
        for text, collision in (("payload: ['DOMAIN-WILDCARD,*.unknown.example']", False),
                                ("payload: []\npayload: ['DOMAIN,exact.example']", False),
                                ("payload: ['DOMAIN,exact.example']", True)):
            with self.subTest(text=text, collision=collision), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                self.source_fixture(source)
                proxy = root / "proxy"
                proxy_fixture(proxy, text)
                if collision:
                    (source / "Proxy").mkdir()
                    (source / "Proxy/Proxy_Classical.yaml").write_text("payload: ['DOMAIN,collision.example']")
                    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
                    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Collision"],
                                   cwd=source, check=True, capture_output=True)
                with self.assertRaises(ValueError):
                    build(source, root / "output", Path(os.environ["SING_BOX_BINARY"]).resolve(), proxy_input=proxy)
                self.assertFalse((root / "output").exists())

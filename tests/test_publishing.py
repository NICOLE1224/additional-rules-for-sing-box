import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_upstream import BRANCHES, ROOT, published_changed
from verify_published import verify_published

EXTENSIONS = {"json": "json", "srs": "srs", "shadowrocket": "list"}


class PublishingTests(unittest.TestCase):
    def command(self, *args, cwd=None, check=True):
        return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote, self.checkout, self.dist = self.root / "remote.git", self.root / "publish", self.root / "dist"
        self.command("git", "init", "--bare", str(self.remote))
        self.command("git", "clone", str(self.remote), str(self.checkout))
        (self.checkout / "source.txt").write_text("source branch fixture")
        self.command("git", "add", ".", cwd=self.checkout)
        self.command("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Initial", cwd=self.checkout)
        self.command("git", "push", "origin", "HEAD:main", cwd=self.checkout)
        self.commit, self.recipe = "a" * 40, "b" * 64
        self.write_products()

    def write_products(self, commit=None, filename="Rule"):
        for branch in BRANCHES:
            path = self.dist / branch
            path.mkdir(parents=True, exist_ok=True)
            for file in path.iterdir():
                file.unlink()
            manifest = {"upstream_commit": commit or self.commit, "build_fingerprint": self.recipe}
            (path / "manifest.json").write_text(json.dumps(manifest))
            (path / "LICENSE.upstream").write_text("MIT License")
            (path / f"{filename}.{EXTENSIONS[branch]}").write_text("fixture product")

    def publish(self, check=True):
        return self.command("bash", str(ROOT / "scripts/publish.sh"), str(self.checkout), str(self.dist), check=check)

    def heads(self):
        return self.command("git", "ls-remote", "--heads", str(self.remote), "main", *BRANCHES).stdout

    def test_first_build_and_unchanged_publication(self):
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout))
        self.publish()
        report = verify_published(self.checkout, self.dist)
        self.assertEqual(set(report), set(BRANCHES))
        self.assertTrue(all(r["rule_sets"] == 1 for r in report.values()))
        heads = self.heads()
        self.assertFalse(published_changed(self.commit, self.recipe, self.checkout))
        self.publish()
        self.assertEqual(heads, self.heads())
        self.assertTrue(published_changed("c" * 40, self.recipe, self.checkout))
        self.assertTrue(published_changed(self.commit, "d" * 64, self.checkout))

    def test_google_cn_content_changes_trigger_rebuild_without_changing_primary_upstream(self):
        digest = "f" * 64
        for branch in BRANCHES:
            path = self.dist / branch / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["google_cn_source"] = {"sha256": digest, "commit": "1" * 40}
            path.write_text(json.dumps(manifest))
        self.publish()
        self.assertFalse(published_changed(self.commit, self.recipe, self.checkout, digest))
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout, "e" * 64))

    def test_deleted_files_are_removed_and_branches_advance_together(self):
        self.publish()
        self.write_products(commit="c" * 40, filename="Replacement")
        self.publish()
        for branch in BRANCHES:
            listing = self.command("git", "--git-dir", str(self.remote), "ls-tree", "--name-only", branch).stdout
            self.assertIn(f"Replacement.{EXTENSIONS[branch]}", listing)
            self.assertNotIn(f"Rule.{EXTENSIONS[branch]}", listing)
        self.assertFalse(published_changed("c" * 40, self.recipe, self.checkout))
        verify_published(self.checkout, self.dist)

    def test_verification_rejects_missing_changed_or_extra_branch_files(self):
        for alteration in ("missing", "changed", "extra"):
            with self.subTest(alteration=alteration):
                self.publish()
                target = self.checkout / "Rule.list"
                if alteration == "missing":
                    target.unlink()
                elif alteration == "changed":
                    target.write_text("different rules")
                else:
                    (self.checkout / "unexpected.list").write_text("extra rules")
                self.command("git", "add", "--all", cwd=self.checkout)
                self.command("git", "commit", "-m", "Alter generated branch fixture", cwd=self.checkout)
                self.command("git", "push", "origin", "shadowrocket", cwd=self.checkout)
                with self.assertRaisesRegex(ValueError, "Incomplete or different shadowrocket contents"):
                    verify_published(self.checkout, self.dist)

    def test_verification_rejects_missing_remote_branch_and_unpublished_changes(self):
        self.publish()
        self.command("git", "--git-dir", str(self.remote), "update-ref", "-d", "refs/heads/json")
        with self.assertRaisesRegex(ValueError, "Published json head"):
            verify_published(self.checkout, self.dist)
        self.publish()
        (self.checkout / "Rule.list").write_text("unpublished rules")
        self.command("git", "add", ".", cwd=self.checkout)
        self.command("git", "commit", "-m", "Unpublished fixture", cwd=self.checkout)
        with self.assertRaisesRegex(ValueError, "Published shadowrocket head"):
            verify_published(self.checkout, self.dist)

    def test_rejecting_shadowrocket_leaves_all_remote_heads_unchanged(self):
        self.publish()
        before = self.heads()
        hook = self.remote / "hooks/pre-receive"
        hook.write_text('#!/usr/bin/env bash\nwhile read -r old new ref; do\n  if [[ "$ref" == refs/heads/shadowrocket ]]; then exit 1; fi\ndone\n')
        hook.chmod(0o755)
        self.write_products(commit="e" * 40)
        result = self.publish(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.heads())

    def test_inconsistent_manifests_are_rejected_before_branch_changes(self):
        before = self.heads()
        (self.dist / "shadowrocket/manifest.json").write_text("{}")
        self.assertNotEqual(self.publish(check=False).returncode, 0)
        self.assertEqual(before, self.heads())

    def test_missing_branch_or_broken_manifest_triggers_rebuild(self):
        self.publish()
        for branch in BRANCHES:
            (self.dist / branch / "manifest.json").write_text("broken")
        self.publish()
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout))
        self.command("git", "--git-dir", str(self.remote), "update-ref", "-d", "refs/heads/json")
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout))

    def test_missing_shadowrocket_is_added_without_rewriting_existing_history(self):
        self.publish()
        previous = {branch: self.command("git", "--git-dir", str(self.remote), "rev-parse", branch).stdout.strip()
                    for branch in ("json", "srs")}
        self.command("git", "--git-dir", str(self.remote), "update-ref", "-d", "refs/heads/shadowrocket")
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout))
        self.write_products(commit="f" * 40)
        self.publish()
        for branch, parent in previous.items():
            self.assertEqual(self.command("git", "--git-dir", str(self.remote), "rev-parse", f"{branch}^").stdout.strip(), parent)
        self.assertFalse(published_changed("f" * 40, self.recipe, self.checkout))


if __name__ == "__main__":
    unittest.main()

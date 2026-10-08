import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_upstream import ROOT, published_changed


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
        for branch in ("json", "srs"):
            path = self.dist / branch
            path.mkdir(parents=True, exist_ok=True)
            for file in path.iterdir():
                file.unlink()
            manifest = {"upstream_commit": commit or self.commit, "build_fingerprint": self.recipe}
            (path / "manifest.json").write_text(json.dumps(manifest))
            (path / "LICENSE.upstream").write_text("MIT License")
            (path / f"{filename}.{branch}").write_text("fixture product")

    def publish(self, check=True):
        return self.command("bash", str(ROOT / "scripts/publish.sh"), str(self.checkout), str(self.dist), check=check)

    def heads(self):
        return self.command("git", "ls-remote", "--heads", str(self.remote), "main", "json", "srs").stdout

    def test_first_build_and_unchanged_publication(self):
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout))
        self.publish()
        heads = self.heads()
        self.assertFalse(published_changed(self.commit, self.recipe, self.checkout))
        self.publish()
        self.assertEqual(heads, self.heads())
        self.assertTrue(published_changed("c" * 40, self.recipe, self.checkout))
        self.assertTrue(published_changed(self.commit, "d" * 64, self.checkout))

    def test_deleted_files_are_removed_and_branches_advance_together(self):
        self.publish()
        self.write_products(commit="c" * 40, filename="Replacement")
        self.publish()
        for branch in ("json", "srs"):
            listing = self.command("git", "--git-dir", str(self.remote), "ls-tree", "--name-only", branch).stdout
            self.assertIn(f"Replacement.{branch}", listing)
            self.assertNotIn(f"Rule.{branch}", listing)
        self.assertFalse(published_changed("c" * 40, self.recipe, self.checkout))

    def test_rejecting_one_branch_leaves_both_remote_heads_unchanged(self):
        self.publish()
        before = self.heads()
        hook = self.remote / "hooks/pre-receive"
        hook.write_text('#!/usr/bin/env bash\nwhile read -r old new ref; do\n  if [[ "$ref" == refs/heads/srs ]]; then exit 1; fi\ndone\n')
        hook.chmod(0o755)
        self.write_products(commit="e" * 40)
        result = self.publish(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.heads())

    def test_inconsistent_manifests_are_rejected_before_branch_changes(self):
        before = self.heads()
        (self.dist / "srs/manifest.json").write_text("{}")
        self.assertNotEqual(self.publish(check=False).returncode, 0)
        self.assertEqual(before, self.heads())

    def test_missing_branch_or_broken_manifest_triggers_rebuild(self):
        self.publish()
        (self.dist / "json/manifest.json").write_text("broken")
        (self.dist / "srs/manifest.json").write_text("broken")
        self.publish()
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout))
        self.command("git", "--git-dir", str(self.remote), "update-ref", "-d", "refs/heads/json")
        self.assertTrue(published_changed(self.commit, self.recipe, self.checkout))


if __name__ == "__main__":
    unittest.main()

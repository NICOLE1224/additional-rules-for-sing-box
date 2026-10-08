#!/usr/bin/env python3
"""Verify every published branch matches the complete local build."""
import argparse
import json
import os
from pathlib import Path

from check_upstream import BRANCHES, git


def verify_published(checkout, output):
    checkout, output = checkout.resolve(), output.resolve()
    remote_heads = {ref.removeprefix("refs/heads/"): sha for sha, ref in
                    (line.split() for line in git("ls-remote", "--heads", "origin", *BRANCHES, cwd=checkout).splitlines())}
    report = {}
    for branch in BRANCHES:
        directory = output / branch
        if not (directory / "manifest.json").is_file() or not (directory / "LICENSE.upstream").is_file():
            raise ValueError(f"Incomplete build output for {branch}")
        local_head = git("rev-parse", f"refs/heads/{branch}", cwd=checkout)
        if remote_heads.get(branch) != local_head:
            raise ValueError(f"Published {branch} head does not match this build's commit")
        expected = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                mode = "100755" if path.stat().st_mode & 0o111 else "100644"
                expected[path.relative_to(directory).as_posix()] = (
                    mode, "blob", git("hash-object", "--no-filters", "--", str(path), cwd=checkout))
        actual = {}
        for entry in git("ls-tree", "-r", "-z", local_head, cwd=checkout).split("\0"):
            if entry:
                metadata, path = entry.split("\t", 1)
                actual[path] = tuple(metadata.split())
        if actual != expected:
            missing = sorted(expected.keys() - actual.keys())
            extra = sorted(actual.keys() - expected.keys())
            changed = sorted(path for path in actual.keys() & expected.keys() if actual[path] != expected[path])
            raise ValueError(f"Incomplete or different {branch} contents: missing={missing}, extra={extra}, changed={changed}")
        suffix = {"json": ".json", "srs": ".srs", "shadowrocket": ".list"}[branch]
        rule_count = sum(path.endswith(suffix) and path != "manifest.json" for path in expected)
        report[branch] = {"commit": local_head, "files": len(expected), "rule_sets": rule_count}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published-git", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_published(args.published_git, args.output)
    print(json.dumps(report, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("### Verified published rule-sets\n\n| Branch | Rule files | Commit |\n| --- | --- | --- |\n")
            for branch, result in report.items():
                handle.write(f"| {branch} | {result['rule_sets']} | `{result['commit']}` |\n")
            handle.write("\nAll tracked files match the build output, including hashes and file modes.\n\n")


if __name__ == "__main__":
    main()

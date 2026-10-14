#!/usr/bin/env python3
"""Compare upstream HEAD and the build inputs with all published branches."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BRANCHES = ("json", "srs", "shadowrocket")


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result.stdout.strip()


def fingerprint(root=ROOT):
    files = [root / name for name in ("toolchain.json", "requirements.txt", "known_invalid.json", "README.md")]
    files += sorted((root / "scripts").glob("*.py"))
    files += sorted((root / "scripts").glob("*.sh"))
    files += sorted((root / "tests").glob("*.py"))
    files += sorted((root / ".github" / "workflows").glob("*.yml"))
    files += sorted((root / "licenses").glob("*"))
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def published_changed(commit, build_fingerprint, checkout, google_cn_sha256=None, proxy_snapshot_sha256=None):
    remote_heads = git("ls-remote", "--heads", "origin", *BRANCHES, cwd=checkout)
    existing = {line.split()[1] for line in remote_heads.splitlines()}
    changed = False
    for branch in BRANCHES:
        if f"refs/heads/{branch}" not in existing:
            changed = True
            continue
        git("fetch", "--no-tags", "--depth=1", "origin", f"refs/heads/{branch}", cwd=checkout)
        try:
            manifest = json.loads(git("show", "FETCH_HEAD:manifest.json", cwd=checkout))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            changed = True
            continue
        if not isinstance(manifest, dict) or manifest.get("upstream_commit") != commit or manifest.get("build_fingerprint") != build_fingerprint:
            changed = True
        else:
            for key, field, expected in (("google_cn_source", "sha256", google_cn_sha256),
                                         ("proxy_source", "snapshot_sha256", proxy_snapshot_sha256)):
                if expected is not None:
                    metadata = manifest.get(key)
                    if not isinstance(metadata, dict) or metadata.get(field) != expected:
                        changed = True
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published-git", type=Path, default=Path("."))
    parser.add_argument("--google-cn-output", type=Path, default=Path(".tools/google-cn"))
    parser.add_argument("--proxy-output", type=Path, default=Path(".tools/proxy"))
    args = parser.parse_args()
    config = json.loads((ROOT / "toolchain.json").read_text())
    ref = f"refs/heads/{config['upstream_ref']}"
    head = git("ls-remote", "--exit-code", config["upstream_repository"], ref)
    commit = head.split()[0]
    from google_cn import fetch_snapshot
    google_cn = fetch_snapshot(config, args.google_cn_output)
    from proxy_rules import fetch_snapshot as fetch_proxy_snapshot
    proxy = fetch_proxy_snapshot(config, args.proxy_output)
    changed = published_changed(commit, fingerprint(), args.published_git, google_cn["sha256"], proxy["snapshot_sha256"])
    outputs = {"changed": str(changed).lower(), "upstream_sha": commit,
               "google_cn_sha": google_cn["commit"], "google_cn_sha256": google_cn["sha256"],
               "proxy_sha": proxy["commit"], "proxy_sha256": proxy["sha256"],
               "proxy_snapshot_sha256": proxy["snapshot_sha256"]}
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(outputs, indent=2))


if __name__ == "__main__":
    main()

"""Fetch and verify a pinned blackmatrix7 Proxy source with its attribution."""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote
from urllib.request import urlopen

from check_upstream import git

FILES = {"input.yaml": "path", "README.proxy.md.txt": "readme",
         "README.upstream.md.txt": "README.md", "LICENSE.upstream.txt": "LICENSE"}


def snapshot_digest(hashes):
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fetch_snapshot(config, directory):
    source = config["proxy"]
    commit = git("ls-remote", "--exit-code", source["repository"], f"refs/heads/{source['ref']}").split()[0]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid Proxy upstream commit")
    repository = source["repository"].removeprefix("https://github.com/")
    bodies = {}
    for name, key in FILES.items():
        path = source[key] if key in {"path", "readme"} else key
        with urlopen(f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(path)}", timeout=60) as response:
            bodies[name] = response.read()
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in bodies.items()}
    if hashes["LICENSE.upstream.txt"] != source["license_sha256"]:
        raise ValueError("Proxy upstream license changed; review before rebuilding")
    metadata = {key: source[key] for key in ("repository", "ref", "path", "readme")}
    metadata.update(commit=commit, sha256=hashes["input.yaml"], files_sha256=hashes,
                    snapshot_sha256=snapshot_digest(hashes))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        (directory / name).write_bytes(body)
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def load_snapshot(config, directory):
    source = config["proxy"]
    metadata = json.loads((directory / "metadata.json").read_text())
    if not isinstance(metadata, dict) or any(metadata.get(key) != source[key] for key in ("repository", "ref", "path", "readme")):
        raise ValueError("Unexpected Proxy source metadata")
    if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("commit", "")):
        raise ValueError("Invalid Proxy snapshot commit")
    hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in FILES}
    if hashes != metadata.get("files_sha256") or snapshot_digest(hashes) != metadata.get("snapshot_sha256"):
        raise ValueError("Proxy snapshot hash mismatch")
    if hashes["input.yaml"] != metadata.get("sha256") or hashes["LICENSE.upstream.txt"] != source["license_sha256"]:
        raise ValueError("Proxy input or license hash mismatch")
    return (directory / "input.yaml").read_text(encoding="utf-8-sig"), metadata

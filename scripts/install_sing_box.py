#!/usr/bin/env python3
"""Download the pinned official Linux/amd64 compiler and check its SHA-256."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".tools/sing-box"))
    args = parser.parse_args()
    config = json.loads((Path(__file__).resolve().parents[1] / "toolchain.json").read_text())
    version = config["sing_box_version"]
    name = f"sing-box-{version}-linux-amd64"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{version}/{name}.tar.gz"
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = response.read()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != config["sing_box_linux_amd64_sha256"]:
        raise SystemExit(f"Compiler SHA-256 mismatch: {digest}")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.getmember(f"{name}/sing-box")
        if not member.isfile():
            raise SystemExit("Compiler archive does not contain a regular binary")
        binary = tar.extractfile(member).read()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(binary)
    args.output.chmod(0o755)
    print(f"Installed sing-box {version}; verified SHA-256 {digest}")


if __name__ == "__main__":
    main()

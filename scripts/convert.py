#!/usr/bin/env python3
"""Extract domain predicates from Clash YAML and compile sing-box rule-sets."""
import argparse
from collections import Counter, defaultdict
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote

import yaml

from check_upstream import ROOT, fingerprint, git

FIELDS = {"DOMAIN": "domain", "DOMAIN-SUFFIX": "domain_suffix",
          "DOMAIN-KEYWORD": "domain_keyword", "DOMAIN-REGEX": "domain_regex"}
# Unknown types deliberately fail rather than silently losing new domain syntax.
NON_DOMAIN_TYPES = {
    "IP-CIDR", "IP-CIDR6", "IP-SUFFIX", "SRC-IP-CIDR", "GEOIP", "SRC-GEOIP",
    "ASN", "IP-ASN", "SRC-IP-ASN", "PROCESS-NAME", "PROCESS-PATH",
    "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX", "DST-PORT", "SRC-PORT",
    "IN-PORT", "IN-TYPE", "IN-USER", "IN-NAME", "NETWORK", "DSCP", "UID", "MATCH",
}


class InvalidDomain(ValueError):
    pass


class UniqueLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def domain_pattern(value):
    """Match Mihomo domain-provider semantics; never infer partial globs/regex."""
    value = value.lower()
    if not value or value.endswith(".") or any(c.isspace() for c in value):
        raise InvalidDomain("empty domain, whitespace or trailing dot")
    parts = value.split(".")
    if any(not p for p in parts[1:]) or (len(parts) == 1 and not parts[0]):
        raise InvalidDomain("empty domain label")
    if "regexp:" in value or any(c in value for c in "/\\:()|$"):
        raise InvalidDomain("regex-like text is not a Clash domain-provider pattern")
    for i, part in enumerate(parts):
        if "+" in part and (part != "+" or i != 0 or len(parts) < 2):
            raise InvalidDomain("misplaced + wildcard")
        if "*" in part and part != "*":
            raise InvalidDomain("partial-label * wildcard is unsupported by Mihomo")
    if "*" not in value:
        if value.startswith("+."):
            return "domain_suffix", value[2:]
        if value.startswith("."):
            return "domain_suffix", value
        return "domain", value
    prefix = "^"
    if parts[0] == "+":
        prefix += r"(?:[^.]+\.)*"
        parts = parts[1:]
    elif parts[0] == "":
        prefix += r"(?:[^.]+\.)+"
        parts = parts[1:]
    # '?' is literal in Mihomo's trie. '*' consumes exactly one whole label.
    regex = prefix + r"\.".join(r"[^.]+" if p == "*" else re.escape(p) for p in parts) + "$"
    return "domain_regex", regex


def convert_payload(payload, source, known_invalid):
    values = defaultdict(set)
    stats = Counter({key: 0 for key in ("input_entries", "converted_entries", "excluded_invalid_entries",
                                      "unique_entries", "duplicate_entries", "ignored_non_domain_entries")})
    ignored = Counter()
    policies = Counter()
    issues = []
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{source}: payload[{index}] must be a nonempty string")
        raw = raw.strip()
        stats["input_entries"] += 1
        if "," in raw:
            parts = [part.strip() for part in raw.split(",")]
            kind = parts[0].upper()
            if kind in NON_DOMAIN_TYPES:
                ignored[kind] += 1
                continue
            if kind not in FIELDS or len(parts) < 2 or not parts[1]:
                raise ValueError(f"{source}: unsupported rule: {raw!r}")
            field, value = FIELDS[kind], parts[1]
            if field != "domain_regex":
                value = value.lower()
            for policy in parts[2:]:
                if policy and policy.lower() != "no-resolve":
                    policies[policy] += 1
        else:
            try:
                ipaddress.ip_network(raw, strict=False)
            except ValueError:
                try:
                    field, value = domain_pattern(raw)
                except InvalidDomain as error:
                    if (source, raw) not in known_invalid:
                        raise ValueError(f"{source}: NEW invalid domain {raw!r}: {error}") from error
                    issues.append({"source": source, "payload_index": index, "value": raw,
                                   "reason": str(error), "status": "excluded_known_invalid"})
                    stats["excluded_invalid_entries"] += 1
                    continue
            else:
                ignored["bare_ip_cidr"] += 1
                continue
        if "?" in value and field != "domain_regex":
            issues.append({"source": source, "payload_index": index, "value": raw,
                           "reason": "? is literal, not a single-character wildcard in Mihomo",
                           "status": "preserved_literal"})
        values[field].add(value)
        stats["converted_entries"] += 1
    rule = {field: sorted(entries) for field, entries in sorted(values.items())}
    stats["unique_entries"] = sum(map(len, values.values()))
    stats["duplicate_entries"] = stats["converted_entries"] - stats["unique_entries"]
    stats["ignored_non_domain_entries"] = sum(ignored.values())
    return rule, dict(stats), dict(sorted(ignored.items())), dict(sorted(policies.items())), issues


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr or result.stdout}")
    return (result.stdout + result.stderr).strip()


def build(source_dir, output, compiler):
    source_dir, output = source_dir.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"Output already exists; use a new directory: {output}")
    if source_dir == output or source_dir in output.parents or output in source_dir.parents:
        raise ValueError("Input and output directories must not contain each other")
    config = json.loads((ROOT / "toolchain.json").read_text())
    compiler_version = run(str(compiler), "version").splitlines()[0]
    if compiler_version != f"sing-box version {config['sing_box_version']}":
        raise ValueError(f"Unexpected compiler: {compiler_version}")
    if git("status", "--porcelain", "--untracked-files=all", cwd=source_dir):
        raise ValueError("Upstream checkout must be clean so the recorded commit is accurate")
    upstream_commit = git("rev-parse", "HEAD", cwd=source_dir)
    known_invalid = {(item["source"], item["value"]) for item in json.loads((ROOT / "known_invalid.json").read_text())}
    files = sorted(p for p in source_dir.rglob("*") if p.suffix.lower() in {".yaml", ".yml"}
                   and not any(part.startswith(".") for part in p.relative_to(source_dir).parts))
    if not files:
        raise ValueError("Upstream contains no YAML files")
    # Build in a temporary sibling; failed conversion never exposes partial output.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".convert-", dir=output.parent) as temporary:
        staging = Path(temporary)
        sources, issues, outputs = [], [], set()
        for path in files:
            relative = path.relative_to(source_dir)
            data = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=UniqueLoader)
            if not isinstance(data, dict) or not isinstance(data.get("payload"), list):
                raise ValueError(f"{relative}: expected a YAML payload list")
            rule, stats, ignored, policies, file_issues = convert_payload(data["payload"], relative.as_posix(), known_invalid)
            issues.extend(file_issues)
            record = {"source": relative.as_posix(), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      **stats, "ignored_types": ignored, "omitted_policies": policies}
            if rule:
                json_relative, srs_relative = relative.with_suffix(".json"), relative.with_suffix(".srs")
                if json_relative in outputs:
                    raise ValueError(f"Output filename collision: {json_relative}")
                outputs.add(json_relative)
                json_path, srs_path = staging / "json" / json_relative, staging / "srs" / srs_relative
                write_json(json_path, {"version": config["rule_set_version"], "rules": [rule]})
                srs_path.parent.mkdir(parents=True, exist_ok=True)
                run(str(compiler), "rule-set", "compile", "--output", str(srs_path), str(json_path))
                if srs_path.read_bytes()[:4] != b"SRS\x02":
                    raise ValueError(f"Unexpected binary header/version: {srs_relative}")
                record.update(json=json_relative.as_posix(), srs=srs_relative.as_posix(),
                              srs_sha256=hashlib.sha256(srs_path.read_bytes()).hexdigest(),
                              json_sha256=hashlib.sha256(json_path.read_bytes()).hexdigest(), status="converted")
            else:
                record["status"] = "no_domain_rules"
            sources.append(record)
        if not outputs:
            raise ValueError("No domain rule-sets were produced")
        # Use the official parser to validate every binary, including Go regexp syntax.
        check_config = staging / "check.json"
        write_json(check_config, {"route": {"rule_set": [
            {"type": "local", "tag": str(i), "format": "binary", "path": str(staging / "srs" / Path(relative).with_suffix(".srs"))}
            for i, relative in enumerate(sorted(outputs))
        ]}})
        run(str(compiler), "check", "--config", str(check_config))
        check_config.unlink()
        manifest = {"upstream_repository": config["upstream_repository"], "upstream_commit": upstream_commit,
                    "build_fingerprint": fingerprint(), "sing_box_version": config["sing_box_version"],
                    "rule_set_version": config["rule_set_version"], "source_files": len(files),
                    "generated_rule_sets": len(outputs), "sources": sources, "issues": issues}
        for branch in ("json", "srs"):
            write_json(staging / branch / "manifest.json", manifest)
            shutil.copy2(source_dir / "LICENSE", staging / branch / "LICENSE.upstream")
            shutil.copy2(ROOT / "README.md", staging / branch / "README.md")
            index = [f"# {branch} 规则索引", "", f"上游提交：`{upstream_commit}`", "",
                     "该分支仅包含域名匹配条件。原始规则中的 DIRECT/REJECT 等策略需要自行配置。", "",
                     "| 上游文件 | 规则集 | 去重后条目数 |", "| --- | --- | --- |"]
            for record in sources:
                if record["status"] == "converted":
                    url = record[branch]
                    # Percent-encode non-ASCII archive paths for portable Markdown links.
                    index.append(f"| {record['source']} | [{url}]({quote(url)}) | {record['unique_entries']} |")
            (staging / branch / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
        staging.rename(output)
    print(f"Scanned {len(files)} YAML files; produced {len(outputs)} JSON/SRS pairs; "
          f"excluded {sum(r.get('excluded_invalid_entries', 0) for r in sources)} known invalid entries.")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--sing-box", type=Path, default=Path(".tools/sing-box"))
    args = parser.parse_args()
    build(args.source, args.output, args.sing_box.resolve())


if __name__ == "__main__":
    main()

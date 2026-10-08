#!/usr/bin/env python3
"""Extract domain predicates for sing-box and Shadowrocket rule-sets."""
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

from check_upstream import BRANCHES, ROOT, fingerprint, git

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


def shadowrocket_rules(rule):
    """Export supported two-column predicates without inventing regex support."""
    kinds = {"domain": "DOMAIN", "domain_suffix": "DOMAIN-SUFFIX", "domain_keyword": "DOMAIN-KEYWORD"}
    lines, excluded = [], []
    for field, values in sorted(rule.items()):
        for value in values:
            reason = None
            if field == "domain_regex":
                reason = "Equivalent DOMAIN-REGEX support in Shadowrocket is unconfirmed"
            elif field == "domain_suffix" and value.startswith("."):
                reason = "Equivalent subdomain-only suffix semantics in Shadowrocket are unconfirmed"
            elif field not in kinds:
                raise ValueError(f"Unsupported Shadowrocket export field: {field}")
            if reason:
                excluded.append({"field": field, "value": value, "reason": reason})
                continue
            if not value or any(c in value for c in ",\r\n") or value != value.strip():
                raise ValueError(f"Unsafe Shadowrocket rule value: {value!r}")
            lines.append(f"{kinds[field]},{value}")
    return lines, excluded


def shadowrocket_wildcards(payload):
    """Offer original Clash patterns as opt-in globs; their semantics can widen."""
    patterns, represented = set(), set()
    for raw in payload:
        raw = raw.strip()
        if "," in raw:
            parts = [part.strip() for part in raw.split(",")]
            if parts[0].upper() == "DOMAIN-SUFFIX" and len(parts) > 1 and parts[1].startswith("."):
                raw = parts[1]
            else:
                # Arbitrary classical DOMAIN-REGEX cannot safely be reversed to a glob.
                continue
        try:
            field, value = domain_pattern(raw)
        except InvalidDomain:
            # Invalid upstream entries were checked/allowlisted by convert_payload.
            continue
        if field != "domain_regex" and not (field == "domain_suffix" and value.startswith(".")):
            continue
        if "?" in raw:
            # Literal '?' in Mihomo would become a wildcard in this target format.
            continue
        raw = raw.lower()
        if raw.startswith("+."):
            patterns.update((raw[2:], "*." + raw[2:]))
        elif raw.startswith("."):
            patterns.add("*" + raw)
        else:
            patterns.add(raw)
        represented.add((field, value))
    return [f"DOMAIN-WILDCARD,{value}" for value in sorted(patterns)], represented


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
        sources, issues, outputs, shadowrocket_paths = [], [], set(), set()
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
                lines, excluded = shadowrocket_rules(rule)
                wildcard_lines, represented = shadowrocket_wildcards(data["payload"])
                record.update(shadowrocket_unique_entries=len(lines), shadowrocket_excluded_from_primary_entries=len(excluded),
                              shadowrocket_wildcard_unique_entries=len(wildcard_lines),
                              shadowrocket_wildcard_covered_predicates=len(represented),
                              shadowrocket_unrepresented_entries=sum((e["field"], e["value"]) not in represented for e in excluded),
                              shadowrocket_status="converted" if lines else "no_supported_domain_rules")
                issues.extend({"source": relative.as_posix(), **item, "status": "excluded_shadowrocket_primary",
                               "wildcard_supplement_available": (item["field"], item["value"]) in represented}
                              for item in excluded)
                if lines:
                    list_relative = relative.with_suffix(".list")
                    if list_relative in shadowrocket_paths:
                        raise ValueError(f"Shadowrocket filename collision: {list_relative}")
                    shadowrocket_paths.add(list_relative)
                    list_path = staging / "shadowrocket" / list_relative
                    list_path.parent.mkdir(parents=True, exist_ok=True)
                    header = ["# Shadowrocket RULE-SET: domain predicates only; policy is supplied by the caller.",
                              f"# Source: {relative.as_posix()}", f"# Upstream commit: {upstream_commit}",
                              f"# Unsupported predicates excluded: {len(excluded)}; see manifest.json."]
                    list_path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
                    record.update(shadowrocket=list_relative.as_posix(),
                                  shadowrocket_sha256=hashlib.sha256(list_path.read_bytes()).hexdigest())
                if wildcard_lines:
                    wildcard_relative = relative.with_suffix(".wildcard.list")
                    if wildcard_relative in shadowrocket_paths:
                        raise ValueError(f"Shadowrocket filename collision: {wildcard_relative}")
                    shadowrocket_paths.add(wildcard_relative)
                    wildcard_path = staging / "shadowrocket" / wildcard_relative
                    wildcard_path.parent.mkdir(parents=True, exist_ok=True)
                    header = ["# OPTIONAL Shadowrocket DOMAIN-WILDCARD supplement; subscribe separately.",
                              "# WARNING: glob matching may cover more domains than the original Clash patterns.",
                              f"# Source: {relative.as_posix()}", f"# Upstream commit: {upstream_commit}"]
                    wildcard_path.write_text("\n".join(header + wildcard_lines) + "\n", encoding="utf-8")
                    record.update(shadowrocket_wildcard=wildcard_relative.as_posix(),
                                  shadowrocket_wildcard_sha256=hashlib.sha256(wildcard_path.read_bytes()).hexdigest())
            else:
                record["status"] = "no_domain_rules"
                record.update(shadowrocket_status="no_domain_rules", shadowrocket_unique_entries=0,
                              shadowrocket_excluded_from_primary_entries=0, shadowrocket_wildcard_unique_entries=0,
                              shadowrocket_wildcard_covered_predicates=0, shadowrocket_unrepresented_entries=0)
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
                    "generated_rule_sets": len(outputs),
                    "shadowrocket": {"generated_rule_sets": sum("shadowrocket" in r for r in sources),
                                     "unique_entries": sum(r["shadowrocket_unique_entries"] for r in sources),
                                     "excluded_from_primary_entries": sum(r["shadowrocket_excluded_from_primary_entries"] for r in sources),
                                     "generated_wildcard_rule_sets": sum("shadowrocket_wildcard" in r for r in sources),
                                     "wildcard_unique_entries": sum(r["shadowrocket_wildcard_unique_entries"] for r in sources),
                                     "wildcard_covered_predicates": sum(r["shadowrocket_wildcard_covered_predicates"] for r in sources),
                                     "unrepresented_entries": sum(r["shadowrocket_unrepresented_entries"] for r in sources)},
                    "sources": sources, "issues": issues}
        for branch in BRANCHES:
            write_json(staging / branch / "manifest.json", manifest)
            shutil.copy2(source_dir / "LICENSE", staging / branch / "LICENSE.upstream")
            shutil.copy2(ROOT / "README.md", staging / branch / "README.md")
            index = [f"# {branch} 规则索引", "", f"上游提交：`{upstream_commit}`", "",
                     "该分支仅包含域名匹配条件。原始规则中的 DIRECT/REJECT 等策略需要自行配置。", "",
                     "| 上游文件 | 规则集 | 去重后条目数 |", "| --- | --- | --- |"]
            for record in sources:
                if branch in record:
                    url = record[branch]
                    count = record["shadowrocket_unique_entries"] if branch == "shadowrocket" else record["unique_entries"]
                    # Percent-encode non-ASCII archive paths for portable Markdown links.
                    index.append(f"| {record['source']} | [{url}]({quote(url)}) | {count} |")
            if branch == "shadowrocket":
                index[6:6] = [f"主文件排除了 {manifest['shadowrocket']['excluded_from_primary_entries']} 条未确认等价支持的条件；"
                              "可选通配符补充见下方，详情见 `manifest.json`。", ""]
                index.extend(["", "## 可选 DOMAIN-WILDCARD 补充", "",
                              "这些文件需单独订阅。通配符可能扩大原始 Clash 规则的匹配范围，请按需启用。", "",
                              "| 上游文件 | 补充规则集 | 去重后条目数 |", "| --- | --- | --- |"])
                for record in sources:
                    if "shadowrocket_wildcard" in record:
                        url = record["shadowrocket_wildcard"]
                        index.append(f"| {record['source']} | [{url}]({quote(url)}) | {record['shadowrocket_wildcard_unique_entries']} |")
            (staging / branch / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
        staging.rename(output)
    print(f"Scanned {len(files)} YAML files; produced {len(outputs)} JSON/SRS pairs; "
          f"{manifest['shadowrocket']['generated_rule_sets']} Shadowrocket lists; "
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

"""Pinned Google CN input and domain-only subtraction for the derived rule-set."""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote
from urllib.request import urlopen

from check_upstream import git

DOMAIN_FIELDS = {"domain", "domain_suffix", "domain_keyword", "domain_regex"}
KINDS = {"domain": "DOMAIN", "domain_suffix": "DOMAIN-SUFFIX", "domain_keyword": "DOMAIN-KEYWORD"}


def fetch_snapshot(config, directory):
    source = config["google_cn"]
    commit = git("ls-remote", "--exit-code", source["repository"], f"refs/heads/{source['ref']}").split()[0]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid Google CN upstream commit")
    repository = source["repository"].removeprefix("https://github.com/")
    url = f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(source['path'])}"
    with urlopen(url, timeout=60) as response:
        body = response.read()
    metadata = {key: source[key] for key in ("repository", "ref", "path")}
    metadata.update(commit=commit, sha256=hashlib.sha256(body).hexdigest())
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "input.json").write_bytes(body)
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate Google CN JSON key: {key}")
        result[key] = value
    return result


def load_snapshot(config, directory):
    body = (directory / "input.json").read_bytes()
    metadata = json.loads((directory / "metadata.json").read_text())
    source = config["google_cn"]
    if any(metadata.get(key) != source[key] for key in ("repository", "ref", "path")):
        raise ValueError("Unexpected Google CN source metadata")
    if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("commit", "")) or metadata.get("sha256") != hashlib.sha256(body).hexdigest():
        raise ValueError("Google CN snapshot hash or commit is invalid")
    data = json.loads(body, object_pairs_hook=unique_object)
    if set(data) != {"version", "rules"} or data["version"] != 2 or not isinstance(data["rules"], list) or not data["rules"]:
        raise ValueError("Expected Google CN source rule-set version 2 with nonempty rules")
    values = {}
    ignored = {}
    for rule in data["rules"]:
        if not isinstance(rule, dict) or not rule:
            raise ValueError("Expected a flat, nonempty Google CN domain rule")
        for field, entries in rule.items():
            if field not in DOMAIN_FIELDS | {"ip_cidr", "source_ip_cidr"}:
                raise ValueError(f"Unsupported Google CN source field: {field}")
            if not isinstance(entries, list) or not all(isinstance(v, str) and v and not any(c.isspace() for c in v) for v in entries):
                raise ValueError(f"Invalid Google CN values for {field}")
            if field not in DOMAIN_FIELDS:
                ignored[field] = ignored.get(field, 0) + len(entries)
                continue
            values.setdefault(field, set()).update(v if field == "domain_regex" else v.lower() for v in entries)
    rule = {field: sorted(values[field]) for field in sorted(values) if values[field]}
    if not rule:
        raise ValueError("Google CN input has no domain predicates")
    return rule, metadata, ignored


def suffix_matches(domain, suffix):
    return domain.endswith(suffix) if suffix.startswith(".") else domain == suffix or domain.endswith("." + suffix)


def matches(domain, rule):
    return (domain in rule.get("domain", []) or any(suffix_matches(domain, s) for s in rule.get("domain_suffix", []))
            or any(k in domain for k in rule.get("domain_keyword", [])))


def suffix_covered(candidate, excluded):
    a, b = candidate.lstrip("."), excluded.lstrip(".")
    return a.endswith("." + b) or (a == b and (not excluded.startswith(".") or candidate.startswith(".")))


def needs_shadowrocket_guard(field, value, excluded):
    if field == "domain":
        return False  # Matching exact domains have already been removed.
    if field == "domain_keyword":
        return bool(excluded)
    apex = value.lstrip(".")
    return (bool(excluded.get("domain_keyword"))
            or any(suffix_matches(d, value) for d in excluded.get("domain", []))
            or any(apex == s.lstrip(".") or apex.endswith("." + s.lstrip("."))
                   or s.lstrip(".").endswith("." + apex) for s in excluded.get("domain_suffix", [])))


def subtract(base, excluded):
    if any(field not in KINDS for field in excluded) or any(v.startswith(".") for v in excluded.get("domain_suffix", [])):
        raise ValueError("Gemini subtraction cannot be exported equivalently to Shadowrocket; review new syntax")
    if any(any(c in value for c in ",()\r\n") for values in excluded.values() for value in values):
        raise ValueError("Unsafe Gemini value for logical rule export")
    retained, removed = {}, []
    for field, values in base.items():
        for value in values:
            covered = field == "domain" and matches(value, excluded)
            if field == "domain_suffix":
                covered = (any(suffix_covered(value, s) for s in excluded.get("domain_suffix", []))
                           or any(k in value for k in excluded.get("domain_keyword", [])))
            elif field == "domain_keyword":
                covered = any(k in value for k in excluded.get("domain_keyword", []))
            if covered:
                removed.append({"field": field, "value": value})
            else:
                retained.setdefault(field, []).append(value)
    if not retained:
        raise ValueError("Google CN subtraction removed all predicates")
    # The negative condition also cuts holes in suffixes, keywords and regexes.
    logical = {"type": "logical", "mode": "and", "rules": [retained, {**excluded, "invert": True}]} if excluded else retained
    conditions = [f"({KINDS[field]},{value})" for field, values in sorted(excluded.items()) for value in values]
    condition = f"OR,({','.join(conditions)})" if len(conditions) > 1 else conditions[0][1:-1] if conditions else None
    negative = f"NOT,(({condition}))" if condition else None
    lines, unsupported, guarded = [], [], []
    for field, values in retained.items():
        for value in values:
            if field == "domain_regex" or (field == "domain_suffix" and value.startswith(".")):
                unsupported.append({"field": field, "value": value, "reason": "Equivalent Shadowrocket representation is unconfirmed"})
                continue
            if any(c in value for c in ",()\r\n"):
                raise ValueError("Unsafe Google CN value for logical rule export")
            line = f"{KINDS[field]},{value}"
            if negative and needs_shadowrocket_guard(field, value, excluded):
                line = f"AND,(({line}),({negative}))"
                guarded.append({"field": field, "value": value})
            lines.append(line)
    return retained, logical, lines, removed, unsupported, guarded

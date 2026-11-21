"""Build and validate static rule sets from an official DLC release."""

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from time import sleep
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
ASSET = "dlc.dat_plain.yml"
TARGETS = {
    "openclash": {"full": "DOMAIN", "domain": "DOMAIN-SUFFIX",
                  "keyword": "DOMAIN-KEYWORD", "regexp": "DOMAIN-REGEX"},
    "quanx": {"full": "host", "domain": "host-suffix", "keyword": "host-keyword"},
    "homeproxy": {"full": "domain", "domain": "domain_suffix",
                  "keyword": "domain_keyword", "regexp": "domain_regex"},
    "nextin": {"full": "DOMAIN", "domain": "DOMAIN-SUFFIX", "keyword": "DOMAIN-KEYWORD"},
}
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*")
ATTRIBUTE_TAIL = re.compile(r":((?:@!?[\w-]+,)*@!?[\w-]+)$")
DOMAIN = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
QUANX_TYPES = {"host", "host-suffix", "host-keyword", "host-wildcard", "user-agent",
               "ip-cidr", "ip6-cidr", "ip-asn", "geoip"}
GFW_SUFFIX = re.compile(r"\|\|((?:[a-z0-9-]+\.)+[a-z0-9-]+)\^?", re.I)
GFW_EXCEPTION = re.compile(r"\|\|(?:\*\.)?((?:[a-z0-9-]+\.)+[a-z0-9-]+)", re.I)
GFW_REGEX_SUFFIX = re.compile(r"((?:[a-z0-9-]+\\\.)+[a-z0-9-]+)\$/?$", re.I)


@dataclass(frozen=True)
class Rule:
    kind: str
    value: str
    attributes: frozenset = frozenset()


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def parse_rule(entry):
    if not isinstance(entry, str):
        raise ValueError(f"Invalid DLC entry: {entry!r}")
    kind, _, value = entry.partition(":")
    tail = ATTRIBUTE_TAIL.search(value)
    attributes = frozenset()
    if tail:
        attributes = frozenset(part[1:] for part in tail[1].split(","))
        value = value[:tail.start()]
    if kind in {"full", "domain"}:
        value = value.rstrip(".").encode("idna").decode("ascii").lower()
        if not DOMAIN.fullmatch(value):
            raise ValueError(f"Invalid domain: {entry}")
    elif kind not in {"keyword", "regexp"} or not value.strip():
        raise ValueError(f"Unsupported or invalid DLC entry: {entry}")
    if any(char in value for char in "\r\n") or (kind == "keyword" and "," in value):
        raise ValueError(f"Invalid delimiter in DLC entry: {entry}")
    return Rule(kind, value, attributes)


def fetch(url, max_bytes, token=None):
    headers = {"User-Agent": "Network-Rules"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=30) as response:
                payload = response.read(max_bytes + 1)
            if not payload or len(payload) > max_bytes:
                raise ValueError(f"Unexpected download size: {url}")
            return payload
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            sleep(2 ** attempt)


def load_release(repo, tag):
    base = f"https://github.com/{repo}/releases/download"
    if tag is None:
        endpoint = f"https://api.github.com/repos/{repo}/releases/latest"
        tag = json.loads(fetch(endpoint, 1024 * 1024, os.getenv("GITHUB_TOKEN")))["tag_name"]
    if not re.fullmatch(r"\d{14}", tag):
        raise ValueError(f"Unexpected DLC release tag: {tag}")
    url = f"{base}/{tag}/{ASSET}"
    checksum_fields = fetch(f"{url}.sha256sum", 1024).decode("ascii").split()
    if not checksum_fields or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum_fields[0]):
        raise ValueError("Invalid DLC checksum file")
    expected = checksum_fields[0]
    payload = fetch(url, 32 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    if expected.lower() != digest:
        raise ValueError("DLC asset SHA-256 mismatch")
    source = yaml.safe_load(payload)
    lists = source["lists"]
    by_name = {item["name"]: item for item in lists}
    if len(by_name) != len(lists) or not by_name:
        raise ValueError("DLC release contains duplicate categories or no categories")
    return tag, digest, by_name


def target_rules(category, source):
    names = category["dlc"] if isinstance(category["dlc"], list) else [category["dlc"]]
    raw = []
    for name in names:
        rules = source[name]["rules"]
        if not isinstance(rules, list) or not rules or source[name]["length"] != len(rules):
            raise ValueError(f"Invalid DLC rule count for {name}")
        raw.extend(rules)
    attrs = category.get("attributes", {})
    include = set(attrs.get("require", []))
    exclude = set(attrs.get("exclude", []))
    overrides = ROOT / "overrides" / f'{category["id"]}.yml'
    local = read_yaml(overrides) if overrides.is_file() else {}
    removed = {(rule.kind, rule.value) for rule in map(parse_rule, local.get("remove", []))}
    selected = [rule for rule in map(parse_rule, raw)
                if include <= rule.attributes and not exclude & rule.attributes]
    def match(rule):
        return (rule.kind, rule.value)
    merged = {match(rule): rule for rule in selected if match(rule) not in removed}
    for rule in map(parse_rule, local.get("add", [])):
        merged[match(rule)] = rule
    if names and not merged:
        raise ValueError(f"Selected category is empty: {category['id']}")
    return list(merged.values())


def gfwlist_rules(payload):
    lines = base64.b64decode(b"".join(payload.split()), validate=True).decode("utf-8-sig").splitlines()
    if not lines or not lines[0].startswith("[AutoProxy "):
        raise ValueError("Invalid GFWList payload")
    domains, exceptions, omitted = set(), set(), []
    for raw in lines[1:]:
        line = raw.strip()
        if not line or line.startswith("!"):
            continue
        if line.startswith("@@"):
            match = GFW_EXCEPTION.fullmatch(line[2:].removesuffix("^"))
            if match:
                exceptions.add(match[1].lower())
            else:
                suffix = GFW_REGEX_SUFFIX.search(line)
                if suffix:
                    exceptions.add(suffix[1].replace(r"\.", ".").lower())
                omitted.append(line)
        elif (match := GFW_SUFFIX.fullmatch(line)) and DOMAIN.fullmatch(match[1].lower()):
            domains.add(match[1].lower())
        else:
            omitted.append(line)
    rules = []
    for domain in sorted(domains):
        if any(domain == e or domain.endswith("." + e) for e in exceptions):
            omitted.append("||" + domain)
        elif any(e.endswith("." + domain) for e in exceptions):
            rules.append(Rule("full", domain))
            omitted.append("||" + domain)
        else:
            rules.append(Rule("domain", domain))
    if not rules:
        raise ValueError("GFWList has no exportable domain suffixes")
    return rules, sorted(set(omitted))


def quanx_legacy_rules(content, url, policy):
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        fields = [part.strip() for part in line.split(",")]
        if len(fields) < 3 or fields[0].lower() not in QUANX_TYPES or not fields[1]:
            raise ValueError(f"Malformed QuanX filter at {url}: {line}")
        yield (fields[0].lower(), fields[1], policy, *fields[3:])


def nextin_legacy_payload(rows):
    types = {"host": "DOMAIN", "host-suffix": "DOMAIN-SUFFIX",
             "host-keyword": "DOMAIN-KEYWORD", "ip-cidr": "IP-CIDR",
             "ip6-cidr": "IP-CIDR6", "ip-asn": "IP-ASN"}
    payload, omitted = [], []
    for kind, value, _, *options in rows:
        if kind in types and all(option == "no-resolve" for option in options):
            payload.append(",".join([types[kind], value, *options]))
        else:
            omitted.append(f"{kind}:{value}")
    return payload, omitted


def write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def write_yaml(path, data):
    write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def write_json(path, data):
    write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def policy_name(value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in ",\r\n"):
        raise ValueError(f"Invalid policy name: {value!r}")
    return value


def can_export(rule, target, types):
    # Classical Clash providers use comma-separated fields.
    return rule.kind in types and (target != "openclash" or "," not in rule.value)


def provider_config(url, name):
    return {"type": "http", "behavior": "classical", "format": "yaml",
            "interval": 86400, "path": f"./rule_provider/{name}.yaml", "url": url}


def nextin_routing_template(template, routing, results, base):
    groups = [
        {"name": name, "type": "url-test", "include-all-proxies": True, **filters,
         "url": "https://www.gstatic.com/generate_204", "interval": 300,
         "empty-fallback": "REJECT"}
        for name, filters in routing["auto_filters"].items()
    ]
    groups.extend({"name": name, "type": "select", "proxies": members}
                  for name, members in routing["groups"].items())
    providers, rules = {}, list(routing["before"])
    policy_names = {"DIRECT", "REJECT"} | {group["name"] for group in groups}
    for slug, policy in routing["categories"].items():
        if "nextin" not in results[slug]["targets"] or policy not in policy_names:
            raise ValueError(f"Invalid Nextin routing category: {slug}/{policy}")
        name = f"network-rules-nextin-{slug}"
        providers[name] = provider_config(f"{base}/nextin/{slug}.yaml", name)
        rules.append(f"RULE-SET,{name},{policy}")
    rules.extend(routing["after"])
    return {**template, "proxy-groups": groups, "rule-providers": providers, "rules": rules}


def build(output, repo_slug, tag):
    config = read_yaml(ROOT / "sources.yml")
    policy = read_yaml(ROOT / "policy.yml")
    icons = read_yaml(ROOT / "resources.yml")["quanx_icons"]
    legacy = config.get("quanx_legacy", {})
    legacy_base = (f"https://raw.githubusercontent.com/{legacy['repository']}/{legacy['commit']}"
                   if legacy else "")
    compat = config.get("quanx_compat", {})
    compat_base = (f"https://raw.githubusercontent.com/{compat['repository']}/{compat['commit']}"
                   if compat else "")
    release, checksum, source = load_release(config["dlc"], tag)
    if output.exists():
        raise ValueError(f"Choose a new output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    base = f"https://raw.githubusercontent.com/{repo_slug}/publish/v1"
    with TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp) / "output"
        version = stage / "v1"
        results = {}
        providers = {}
        clash_rules, quanx_lines, homeproxy_routes, index = [], [], [], []
        nextin_providers, nextin_remote_rules, nextin_inline_rules = {}, [], []
        icon_index = {}
        for category in policy["categories"]:
            slug = category["id"]
            if not SLUG.fullmatch(slug) or slug in results:
                raise ValueError(f"Invalid or duplicate category ID: {slug}")
            gfw = config["gfwlist"] if category.get("gfwlist") else None
            gfw_omitted = []
            if gfw:
                original = fetch(gfw["url"], 1024 * 1024)
                selected, gfw_omitted = gfwlist_rules(original)
                write(version / "sources" / "gfwlist.txt", original.decode("ascii"))
                write(version / "LICENSE-GFWList.txt", fetch(gfw["license"], 64 * 1024).decode("utf-8"))
            else:
                selected = target_rules(category, source)
            policies = {target: policy_name(value) for target, value in category["policies"].items()}
            if not policies or not policies.keys() <= TARGETS.keys():
                raise ValueError(f"Invalid target selection: {slug}")
            active = category.get("enabled", True)
            compat_path = category.get("quanx_compat")
            merge_compat = category.get("quanx_merge_compat", False)
            compat_rows = []
            if compat_path:
                compat_source = f"{compat_base}/{compat_path}"
                compat_content = fetch(compat_source, 1024 * 1024)
                compat_rows = list(quanx_legacy_rules(
                    compat_content.decode("utf-8-sig"), compat_source, policies.get("quanx", "DIRECT")))
                if not compat_rows:
                    raise ValueError(f"No rules in QuanX companion file: {compat_source}")
            legacy_path = category.get("quanx_legacy")
            legacy_rows = []
            if legacy_path:
                legacy_url = f"{legacy_base}/{legacy_path}"
                content = fetch(legacy_url, 1024 * 1024).decode("utf-8-sig")
                legacy_rows = list(quanx_legacy_rules(content, legacy_url, policies.get("quanx", "DIRECT")))
            exports = {}
            for target, types in TARGETS.items():
                if target not in policies:
                    continue
                supported = [rule for rule in selected if can_export(rule, target, types)]
                omitted = gfw_omitted + sorted(f"{rule.kind}:{rule.value}" for rule in selected
                                               if not can_export(rule, target, types))
                include_legacy = target == "quanx" and legacy_path
                include_compat = target == "quanx" and merge_compat and bool(compat_rows)
                import_quanx = target == "nextin" and category.get("nextin_import_quanx", False)
                if not supported and not include_legacy and not include_compat and not import_quanx:
                    raise ValueError(f"No convertible rules in {slug}/{target}")
                exports[target] = {"count": len(supported), "omitted": omitted}
                if target == "openclash":
                    write_yaml(version / target / f"{slug}.yaml",
                               {"payload": [f"{types[r.kind]},{r.value}" for r in supported]})
                elif target == "quanx":
                    rows = [(types[r.kind], r.value, policies[target]) for r in supported]
                    if include_legacy:
                        rows.extend(legacy_rows)
                        exports[target]["legacy_source"] = legacy_url
                    if include_compat:
                        rows.extend(compat_rows)
                        exports[target]["compat_source"] = compat_source
                    rows = dict.fromkeys(rows)
                    if not rows:
                        raise ValueError(f"No QuanX rules in {slug}")
                    exports[target]["count"] = len(rows)
                    lines = [", ".join(row) for row in rows]
                    if gfw:
                        lines = [f"# GFWList: {gfw['url']}", "# License: ../LICENSE-GFWList.txt",
                                 "# Source snapshot: ../sources/gfwlist.txt", *lines]
                    if include_legacy:
                        lines = [f"# Legacy source: {legacy_url}", "# See ../LICENSE-QuanX-fork.txt", *lines]
                    if include_compat:
                        lines = [f"# Compatibility source: {compat_source}",
                                 "# See ../LICENSE-QuanX-compat.txt", *lines]
                    write(version / target / f"{slug}.list", "\n".join(lines) + "\n")
                elif target == "homeproxy":
                    fields = {}
                    for rule in supported:
                        fields.setdefault(types[rule.kind], []).append(rule.value)
                    rules = [{key: values} for key, values in fields.items()]
                    write_json(version / target / f"{slug}.json",
                               {"version": policy["homeproxy_rule_set_version"], "rules": rules})
                else:
                    payload = [f"{types[r.kind]},{r.value}" for r in supported]
                    if import_quanx:
                        extra, unconverted = nextin_legacy_payload(legacy_rows + (compat_rows if merge_compat else []))
                        payload = list(dict.fromkeys(payload + extra))
                        if not payload:
                            raise ValueError(f"No Nextin rules in {slug}")
                        exports[target]["count"] = len(payload)
                        exports[target]["omitted"].extend(unconverted)
                        if legacy_path:
                            exports[target]["legacy_source"] = legacy_url
                        if merge_compat and compat_rows:
                            exports[target]["compat_source"] = compat_source
                    write_yaml(version / target / f"{slug}.yaml", {"payload": payload})
                    if active and category.get("nextin_default", True):
                        for line in payload:
                            rule_type, value, *options = line.split(",")
                            nextin_inline_rules.append(",".join([rule_type, value, policies[target], *options]))
            results[slug] = {"source": gfw["url"] if gfw else category["dlc"],
                             "selected": len(selected), "targets": exports}
            provider = f"network-rules-{slug}"
            clash_url = f"{base}/openclash/{slug}.yaml"
            quanx_url = f"{base}/quanx/{slug}.list"
            homeproxy_url = f"{base}/homeproxy/{slug}.json"
            if active and "openclash" in policies:
                providers[provider] = provider_config(clash_url, provider)
                clash_rules.append(f"RULE-SET,{provider},{policies['openclash']}")
            if active and "nextin" in policies and category.get("nextin_default", True):
                nextin_provider = f"network-rules-nextin-{slug}"
                nextin_providers[nextin_provider] = provider_config(f"{base}/nextin/{slug}.yaml",
                                                                   nextin_provider)
                nextin_remote_rules.append(f"RULE-SET,{nextin_provider},{policies['nextin']}")
            if "quanx" in policies:
                quanx_lines.append(f"{quanx_url}, tag={slug}, force-policy={policies['quanx']}, "
                                   f"update-interval=86400, enabled={str(active).lower()}")
            if compat_path and "quanx" in policies:
                compat_name = f"{slug}-compat.list"
                (version / "quanx" / compat_name).write_bytes(compat_content)
                compat_url = f"{base}/quanx/{compat_name}"
                if not merge_compat:
                    quanx_lines.append(f"{compat_url}, tag={slug}-compat, force-policy={policies['quanx']}, "
                                       f"update-interval=86400, enabled={str(active).lower()}")
                exports["quanx"]["companion"] = {"source": compat_source, "count": len(compat_rows)}
            if active and "homeproxy" in policies:
                homeproxy_routes.append({"tag": provider, "format": "source", "url": homeproxy_url,
                                         "update_interval": "1d", "outbound": policies["homeproxy"]})
            links = [
                f"[YAML]({clash_url})" if "openclash" in policies else "—",
                f"[规则列表]({quanx_url})" if "quanx" in policies else "—",
                f"[JSON]({homeproxy_url})" if "homeproxy" in policies else "—",
                f"[YAML]({base}/nextin/{slug}.yaml)" if "nextin" in policies else "—",
            ]
            if compat_path and "quanx" in policies and not merge_compat:
                links[1] += f" · [兼容补充]({compat_url})"
            index.append(f"| {slug} | " + " | ".join(links) + " |")
            if slug in icons["files"]:
                if not re.fullmatch(r"[a-f0-9]{40}", icons["commit"]):
                    raise ValueError("QuanX icon source must use a pinned commit")
                icon = quote(icons["files"][slug], safe="/")
                icon_index[slug] = {"policy": policies["quanx"], "img-url":
                                    f"https://raw.githubusercontent.com/{icons['repository']}/{icons['commit']}/{icon}"}
        if not results:
            raise ValueError("No categories configured")
        write(version / "openclash" / "overwrite.conf", "[YAML]\n" + yaml.safe_dump(
            {"rule-providers": providers, "+rules": clash_rules}, allow_unicode=True, sort_keys=False))
        write(version / "quanx" / "filter-remote.conf", "[filter_remote]\n" + "\n".join(quanx_lines) + "\n")
        write_json(version / "quanx" / "icons.json", icon_index)
        write_json(version / "homeproxy" / "setup.json", homeproxy_routes)
        fallback = policy_name(policy["nextin_fallback"])
        groups = dict.fromkeys([fallback] + [
            c["policies"]["nextin"] for c in policy["categories"]
            if "nextin" in c["policies"] and c.get("enabled", True) and c.get("nextin_default", True)
        ])
        nextin_template = {
            "x-nextin": {"version": 1, "mode": "subscription-template"},
            "proxies": [],
            "proxy-groups": [{"name": name, "type": "select", "include-all-proxies": True}
                             for name in groups if name not in {"DIRECT", "REJECT"}],
        }
        write_yaml(version / "nextin" / "template.yaml",
                   {**nextin_template, "rules": nextin_inline_rules + [f"MATCH,{fallback}"]})
        write_yaml(version / "nextin" / "template-remote.yaml", {
            **nextin_template, "rule-providers": nextin_providers,
            "rules": nextin_remote_rules + [f"MATCH,{fallback}"],
        })
        write_yaml(version / "nextin" / "quanx-routing.yaml",
                   nextin_routing_template(nextin_template, policy["nextin_routing"], results, base))
        nextin_url = f"{base}/nextin/template.yaml"
        nextin_remote_url = f"{base}/nextin/template-remote.yaml"
        deep_link = "nextin://install?" + urlencode({"module": "subscription-template",
                                                   "url": nextin_remote_url})
        write(version / "README.md", "# Network-Rules 订阅目录\n\n"
              "| 分类 | OpenClash | QuanX | HomeProxy | Nextin |\n| --- | --- | --- | --- | --- |\n"
              + "\n".join(index) + f"\n\n[Nextin 自动拉取模板]({nextin_remote_url})"
              f" · [Nextin 内嵌模板]({nextin_url})\n\n"
              f"[Nextin 个人分流模板]({base}/nextin/quanx-routing.yaml)\n\n"
              f"Nextin 自动拉取模板导入链接\n\n`{deep_link}`\n\n"
              "QuanX 图标见 [icons.json](quanx/icons.json)。将 `img-url` 加到对应 `[policy]` 策略组。\n\n"
              "各端省略的规则见 [manifest.json](manifest.json) 中的 `omitted`。\n")
        license_text = fetch(f"https://raw.githubusercontent.com/{config['dlc']}/{release}/LICENSE", 64 * 1024)
        write(version / "LICENSE-DLC.txt", license_text.decode("utf-8"))
        if legacy_base:
            license_text = fetch(f"{legacy_base}/LICENSE", 64 * 1024)
            write(version / "LICENSE-QuanX-fork.txt", license_text.decode("utf-8"))
        if compat_base:
            license_text = fetch(f"{compat_base}/LICENSE", 64 * 1024)
            write(version / "LICENSE-QuanX-compat.txt", license_text.decode("utf-8"))
        artifacts = {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(version.rglob("*")) if path.is_file()}
        write_json(version / "manifest.json", {"release": release, "source_sha256": checksum,
                                              "categories": results, "files": artifacts})
        stage.rename(output)
    print(f"DLC {release}: {len(results)} categories in {output}")


def manifest(directory):
    return json.loads((directory / "v1" / "manifest.json").read_text(encoding="utf-8"))


def check(directory):
    data = manifest(directory)
    expected = set(data["files"]) | {"v1/manifest.json"}
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    if expected != actual:
        raise ValueError("Output file list differs from manifest")
    for path, digest in data["files"].items():
        if hashlib.sha256((directory / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Output hash mismatch: {path}")
    for slug, category in data["categories"].items():
        for target, info in category["targets"].items():
            if info["omitted"]:
                print(f"{slug}/{target}: {len(info['omitted'])} omitted (see manifest.json)")
    print(f"Verified {len(data['files'])} artifacts")


def diff(previous, candidate, gate):
    old, new = manifest(previous), manifest(candidate)
    if gate and old["release"] > new["release"]:
        raise ValueError("Refusing to publish an older DLC release")
    for slug in sorted(old["categories"].keys() - new["categories"].keys()):
        print(f"{slug}: category removed")
    for slug, entry in new["categories"].items():
        prior = old["categories"].get(slug, {}).get("selected", 0)
        current = entry["selected"]
        print(f"{slug}: {prior} -> {current}")
        if prior and abs(current - prior) / prior >= 0.30:
            print(f"{slug}: rule count changed by at least 30%")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("--output", type=Path, default=Path("dist"))
    make.add_argument("--release")
    make.add_argument("--repository", default="chenruiyingry/Network-Rules")
    verify = commands.add_parser("check")
    verify.add_argument("directory", type=Path)
    compare = commands.add_parser("diff")
    compare.add_argument("previous", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--gate", action="store_true")
    args = cli.parse_args()
    if args.command == "build":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository):
            raise ValueError("Repository must be owner/name")
        build(args.output, args.repository, args.release)
    elif args.command == "check":
        check(args.directory)
    else:
        diff(args.previous, args.candidate, args.gate)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError) as error:
        sys.exit(f"Network-Rules: {error}")

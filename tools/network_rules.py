"""Build and validate static rule sets from pinned upstream data."""

import argparse
from dataclasses import dataclass
import hashlib
from http.client import IncompleteRead
from ipaddress import collapse_addresses, ip_network
import json
import os
from pathlib import Path
import re
from subprocess import CalledProcessError, TimeoutExpired, run
import sys
from tempfile import TemporaryDirectory
from time import sleep
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
ASSET = "dlc.dat_plain.yml"
TARGETS = {
    "openclash": {"full": "DOMAIN", "domain": "DOMAIN-SUFFIX",
                  "keyword": "DOMAIN-KEYWORD", "regexp": "DOMAIN-REGEX",
                  "ipv4": "IP-CIDR", "ipv6": "IP-CIDR6"},
    "quanx": {"full": "host", "domain": "host-suffix", "keyword": "host-keyword",
              "ipv4": "ip-cidr", "ipv6": "ip6-cidr"},
    "homeproxy": {"full": "domain", "domain": "domain_suffix",
                  "keyword": "domain_keyword", "regexp": "domain_regex",
                  "ipv4": "ip_cidr", "ipv6": "ip_cidr"},
    "nextin": {"full": "DOMAIN", "domain": "DOMAIN-SUFFIX", "keyword": "DOMAIN-KEYWORD",
               "ipv4": "IP-CIDR", "ipv6": "IP-CIDR6"},
}
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*")
ATTRIBUTE_TAIL = re.compile(r":((?:@!?[\w-]+,)*@!?[\w-]+)$")
DOMAIN = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
QUANX_TYPES = {"host", "host-suffix", "host-keyword", "host-wildcard", "user-agent",
               "ip-cidr", "ip6-cidr", "ip-asn", "geoip"}
ADBLOCK_DNS_SUFFIX = re.compile(r"\|\|([a-z0-9.-]+)\^")
DOWNLOAD_RETRY_DELAYS = (5, 15, 30)
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


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
    request = Request(url, headers=headers)
    delays = (*DOWNLOAD_RETRY_DELAYS, None)
    for attempt, delay in enumerate(delays, 1):
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read(max_bytes + 1)
            if not payload or len(payload) > max_bytes:
                raise ValueError(f"Unexpected download size: {url}")
            return payload
        except (OSError, IncompleteRead) as error:
            if delay is None or (isinstance(error, HTTPError) and error.code not in RETRYABLE_HTTP_STATUS):
                raise OSError(f"Download failed: {url} (attempt {attempt}/{len(delays)}): {error}") from error
            print(f"Download failed: {url} (attempt {attempt}/{len(delays)}): {error}; "
                  f"retrying in {delay}s", file=sys.stderr, flush=True)
            sleep(delay)


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


def download_meta_geosite(url):
    payload = yaml.safe_load(fetch(url, 8 * 1024 * 1024))["payload"]
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Empty or invalid MetaCubeX geosite: {url}")
    rules = {}
    for entry in payload:
        if not isinstance(entry, str):
            raise ValueError(f"Invalid MetaCubeX domain at {url}: {entry!r}")
        suffix = entry.startswith("+.")
        domain = entry.removeprefix("+.").lower()
        if not DOMAIN.fullmatch(domain):
            raise ValueError(f"Unsupported MetaCubeX domain matcher at {url}: {entry!r}")
        rules[Rule("domain" if suffix else "full", domain)] = None
    return list(rules)


def adblock_dns_exceptions(*payloads):
    allowed = set()
    for payload in payloads:
        for raw in payload.decode("utf-8-sig").splitlines():
            line = raw.strip().lower()
            if not line.startswith("@@"):
                continue
            match = ADBLOCK_DNS_SUFFIX.fullmatch(line[2:].removesuffix("$important"))
            if not match or not DOMAIN.fullmatch(match[1]) or "." not in match[1]:
                raise ValueError(f"Unsupported AdBlock DNS exception: {raw}")
            allowed.add(match[1])
    if not allowed:
        raise ValueError("AdBlock DNS source is missing allow rules")
    return allowed


def adblock_dns_lite_rules(payload, allowed):
    blocked = set()
    for raw in payload.decode("utf-8-sig").splitlines():
        line = raw.strip().lower()
        if not line or line.startswith(("!", "#", "@@")):
            continue
        match = ADBLOCK_DNS_SUFFIX.fullmatch(line)
        if not match or not DOMAIN.fullmatch(match[1]) or "." not in match[1]:
            raise ValueError(f"Unsupported AdBlock DNS Lite rule: {raw}")
        blocked.add(match[1])

    def suffixes(domain):
        labels = domain.split(".")
        return {".".join(labels[index:]) for index in range(len(labels) - 1)}

    # Exclude overlapping parent blocks so exceptions continue to the normal route.
    exempt_parents = {suffix for domain in allowed for suffix in suffixes(domain)}
    included = {domain for domain in blocked - exempt_parents if not suffixes(domain) & allowed}
    if not included:
        raise ValueError("AdBlock DNS Lite has no rules after applying exceptions")
    omitted = sorted(blocked - included)
    return [Rule("domain", domain) for domain in sorted(included)], omitted


def github_raw_base(source):
    repo, ref = source["repository"], source["ref"]
    endpoint = f"https://api.github.com/repos/{repo}/git/ref/heads/{ref}"
    commit = json.loads(fetch(endpoint, 64 * 1024, os.getenv("GITHUB_TOKEN")))["object"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"Invalid source commit: {repo}/{ref}")
    return f"https://raw.githubusercontent.com/{repo}/{commit}"


def download_adblock_dns(version, slug, source, mihomo, scratch):
    raw_base = github_raw_base(source)
    mrs_url = f"{raw_base}/adblockmihomo.mrs"
    full_url = f"{raw_base}/adblockdns.txt"
    full = fetch(full_url, 12 * 1024 * 1024)
    lite = fetch(f"{raw_base}/adblockdnslite.txt", 1024 * 1024)
    data = fetch(mrs_url, 8 * 1024 * 1024)
    count = re.search(rb"(?m)^! Blocked domains: ([1-9][0-9]*)\r?$", full)
    if not count or not data.startswith(b"\x28\xb5\x2f\xfd") or len(data) < 256:
        raise ValueError("Invalid AdBlock DNS full MRS release")
    allowed = adblock_dns_exceptions(full, lite)
    selected, omitted = adblock_dns_lite_rules(lite, allowed)
    filename = f"{slug}.mrs"
    exceptions = f"{slug}-exceptions.yaml"
    folder = version / "nextin"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)
    decoded = scratch / f"{slug}-decoded.txt"
    convert_mrs(mihomo, "domain", "mrs", folder / filename, decoded)
    with decoded.open(encoding="utf-8") as lines:
        if sum(1 for _ in lines) != int(count[1]):
            raise ValueError("AdBlock DNS MRS rule count differs from its source")
    exceptions_payload = [f"+.{domain}" for domain in sorted(allowed)]
    write_yaml(folder / exceptions, {"payload": exceptions_payload})
    exceptions_mrs = folder / f"{slug}-exceptions.mrs"
    compile_mrs(mihomo, scratch, "domain", exceptions_payload, exceptions_mrs)
    write(version / "LICENSE-AdBlockDNS.txt", fetch(source["license"], 64 * 1024).decode("utf-8"))
    metadata = {"filename": filename, "format": "mrs", "count": int(count[1]),
                "source": mrs_url, "allow_source": full_url, "exceptions": exceptions_mrs.name,
                "exception_count": len(allowed)}
    return selected, omitted, metadata


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


def download_cidrs(url):
    networks = set()
    for raw in fetch(url, 64 * 1024).decode("ascii").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "/" not in line:
            raise ValueError(f"Invalid CIDR at {url}: {line}")
        networks.add(ip_network(line))
    if not networks:
        raise ValueError(f"No CIDRs at {url}")
    return [Rule(f"ipv{network.version}", str(network))
            for network in sorted(networks, key=lambda n: (n.version, n.network_address, n.prefixlen))]


def provider_config(url, name, behavior="classical", file_format="yaml"):
    return {"type": "http", "behavior": behavior, "format": file_format,
            "interval": 86400, "path": f"./rule_provider/{name}.{file_format}", "url": url}


def convert_mrs(mihomo, behavior, input_format, source, output):
    try:
        run([mihomo, "convert-ruleset", behavior, input_format, str(source), str(output)],
            capture_output=True, text=True, check=True, timeout=60)
    except CalledProcessError as error:
        raise ValueError(f"Mihomo failed to convert {source.name}: {error.stderr or error.stdout}") from error
    except TimeoutExpired as error:
        raise ValueError(f"Mihomo timed out converting {source.name}") from error
    except FileNotFoundError as error:
        raise ValueError("Install Mihomo or set MIHOMO to its executable path") from error


def compile_mrs(mihomo, scratch, behavior, values, output, source_url=None):
    if source_url:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(fetch(source_url, 8 * 1024 * 1024))
    else:
        text_path = scratch / f"{output.stem}.txt"
        write(text_path, "\n".join(values) + "\n")
        convert_mrs(mihomo, behavior, "text", text_path, output)
    decoded_path = scratch / f"{output.stem}-decoded.txt"
    convert_mrs(mihomo, behavior, "mrs", output, decoded_path)
    decoded = decoded_path.read_text(encoding="utf-8").splitlines()
    if behavior == "domain":
        def domain_union(entries):
            suffixes = {entry[2:] for entry in entries if entry.startswith("+.")}
            exact = {entry for entry in entries if not entry.startswith("+.")}
            def covered(domain, start):
                labels = domain.split(".")
                return any(".".join(labels[index:]) in suffixes for index in range(start, len(labels)))
            return ({domain for domain in suffixes if not covered(domain, 1)},
                    {domain for domain in exact if not covered(domain, 0)})
        equivalent = domain_union(decoded) == domain_union(values)
    else:
        def address_union(entries):
            networks = [ip_network(entry, strict=False) for entry in entries]
            return {network for version in (4, 6) for network in collapse_addresses(
                network for network in networks if network.version == version)}
        equivalent = address_union(decoded) == address_union(values)
    if not equivalent:
        raise ValueError(f"Mihomo changed the match set for {output.name}")


def nextin_provider_files(folder, slug, payload, mihomo, scratch, meta_mrs=None):
    sections = {}
    for entry in payload:
        kind, value, *options = entry.split(",")
        if kind in {"DOMAIN", "DOMAIN-SUFFIX"} and not options and DOMAIN.fullmatch(value.lower()):
            section = "domain"
            value = f"+.{value.lower()}" if kind == "DOMAIN-SUFFIX" else value.lower()
        elif kind in {"IP-CIDR", "IP-CIDR6"} and options in ([], ["no-resolve"]):
            section = "ip-no-resolve" if options else "ip"
            value = str(ip_network(value, strict=False))
        else:
            section, value = "classical", entry
        sections.setdefault(section, []).append(value)

    files = []
    for section, values in sections.items():
        domain_source, domain_url = meta_mrs if section == "domain" and meta_mrs else (None, None)
        if section == "classical":
            filename, behavior, file_format = f"{slug}-classical.yaml", "classical", "yaml"
            write_yaml(folder / filename, {"payload": values})
        else:
            behavior = "domain" if section == "domain" else "ipcidr"
            filename = f"{slug}.mrs" if section == "domain" else f"{slug}-{section}.mrs"
            file_format = "mrs"
            compile_mrs(mihomo, scratch, behavior, values, folder / filename, domain_source)
        asset = {"filename": filename, "behavior": behavior, "format": file_format, "count": len(values)}
        if domain_url:
            asset["url"] = domain_url
        if section == "ip-no-resolve":
            asset["no_resolve"] = True
        files.append(asset)
    return files


def add_nextin_providers(base, target, policy, providers, rules):
    for asset in target["provider_files"]:
        filename = asset["filename"]
        name = f"network-rules-nextin-{Path(filename).stem}"
        providers[name] = provider_config(asset.get("url", f"{base}/nextin/{filename}"), name,
                                          asset["behavior"], asset["format"])
        option = ",no-resolve" if asset.get("no_resolve") else ""
        rules.append(f"RULE-SET,{name},{policy}{option}")


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
        target = results[slug]["targets"]["nextin"]
        if "mrs" in target:
            mrs = target["mrs"]
            allow = f"{name}-exceptions"
            providers[allow] = provider_config(f"{base}/nextin/{mrs['exceptions']}", allow, "domain", "mrs")
            providers[name] = provider_config(f"{base}/nextin/{mrs['filename']}", name, "domain", "mrs")
            rules.append(f"AND,((RULE-SET,{name}),(NOT,((RULE-SET,{allow})))),{policy}")
        else:
            add_nextin_providers(base, target, policy, providers, rules)
    rules.extend(routing["after"])
    return {**template, "proxy-groups": groups, "rule-providers": providers, "rules": rules}


def nextin_inline_routing_template(remote, folder, scratch, geoip_sources):
    providers = {}
    for name, config in remote["rule-providers"].items():
        slug = name.removeprefix("network-rules-nextin-")
        if config["format"] == "mrs":
            payload = (scratch / f"{slug}-decoded.txt").read_text(encoding="utf-8").splitlines()
        else:
            payload = read_yaml(folder / f"{slug}.yaml")["payload"]
        if not payload:
            raise ValueError(f"Empty Nextin inline rule set: {slug}")
        providers[name] = {"type": "inline", "behavior": config["behavior"], "payload": payload}

    rules = []
    for rule in remote["rules"]:
        if not rule.startswith("GEOIP,"):
            rules.append(rule)
            continue
        _, code, policy, *options = rule.split(",")
        name = f"network-rules-nextin-geoip-{code.lower()}"
        source = geoip_sources[code]
        entries = yaml.safe_load(fetch(source, 1024 * 1024))["payload"]
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Empty or invalid MetaCubeX geoip: {source}")
        payload = list(dict.fromkeys(str(ip_network(entry)) for entry in entries))
        providers[name] = {"type": "inline", "behavior": "ipcidr", "payload": payload}
        rules.append(",".join(["RULE-SET", name, policy, *options]))

    return {**remote, "rule-providers": providers, "rules": rules}


def nextin_hub_routing_template(inline, geosite):
    template = {key: value for key, value in inline.items() if key != "rule-providers"}
    providers = inline["rule-providers"]
    ads = "network-rules-nextin-adblock-dns"
    rules = []
    for rule in inline["rules"]:
        if rule.startswith("RULE-SET,"):
            _, name, policy, *options = rule.split(",")
            slug = name.removeprefix("network-rules-nextin-")
            if slug in geosite:
                rules.append(",".join(["GEOSITE", geosite[slug], policy, *options]))
                continue
            provider = providers[name]
            for value in provider["payload"]:
                if provider["behavior"] == "ipcidr":
                    matcher = f"{'IP-CIDR6' if ':' in value else 'IP-CIDR'},{value}"
                elif provider["behavior"] == "domain":
                    matcher = (f"DOMAIN-SUFFIX,{value[2:]}" if value.startswith("+.")
                               else f"DOMAIN,{value}")
                else:
                    kind, value, *extra = value.split(",")
                    rules.append(",".join([kind, value, policy, *extra, *options]))
                    continue
                rules.append(",".join([matcher, policy, *options]))
        elif f"(RULE-SET,{ads})" in rule:
            allowed = providers[f"{ads}-exceptions"]["payload"]
            allow_matchers = ",".join(f"(DOMAIN-SUFFIX,{entry[2:]})" for entry in allowed)
            rule = rule.replace(f"(RULE-SET,{ads})", f"(GEOSITE,{geosite['adblock-dns']})")
            rule = rule.replace(f"(RULE-SET,{ads}-exceptions)", f"(OR,({allow_matchers}))")
            rules.append(rule)
        else:
            rules.append(rule)
    return {**template, "rules": rules}


def build(output, repo_slug, tag, mihomo="mihomo"):
    config = read_yaml(ROOT / "sources.yml")
    policy = read_yaml(ROOT / "policy.yml")
    icons = read_yaml(ROOT / "resources.yml")["quanx_icons"]
    compat_bases = {name: github_raw_base(source) for name, source in config["quanx_compat"].items()}
    meta = config["meta_rules"]
    meta_base = github_raw_base(meta)
    meta_branch = f"https://raw.githubusercontent.com/{meta['repository']}/{meta['ref']}"
    release, checksum, source = load_release(config["dlc"], tag)
    if output.exists():
        raise ValueError(f"Choose a new output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    base = f"https://raw.githubusercontent.com/{repo_slug}/publish/v1"
    with TemporaryDirectory(dir=output.parent) as tmp:
        scratch = Path(tmp)
        stage = scratch / "output"
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
            geosite = category.get("meta_geosite")
            meta_mrs = None
            ads = config["adblock_dns"] if category.get("adblock_dns") else None
            source_omitted = []
            if geosite:
                geosite_path = f"geo/geosite/{geosite}"
                geosite_url = f"{meta_base}/{geosite_path}.yaml"
                selected = download_meta_geosite(geosite_url)
                meta_mrs = (f"{meta_base}/{geosite_path}.mrs", f"{meta_branch}/{geosite_path}.mrs")
            elif ads:
                selected, source_omitted, mrs = download_adblock_dns(version, slug, ads, mihomo, scratch)
            else:
                selected = target_rules(category, source)
            cidr_url = config[category["cidr_source"]] if "cidr_source" in category else None
            if cidr_url:
                cidrs = download_cidrs(cidr_url)
                selected.extend(cidrs)
            policies = {target: policy_name(value) for target, value in category["policies"].items()}
            if not policies or not policies.keys() <= TARGETS.keys():
                raise ValueError(f"Invalid target selection: {slug}")
            active = category.get("enabled", True)
            compat_path = category.get("quanx_compat")
            merge_compat = category.get("quanx_merge_compat", False)
            compat_rows = []
            if compat_path:
                source_name = category.get("quanx_source", "blackmatrix")
                compat_source = f"{compat_bases[source_name]}/{compat_path}"
                compat_license = f"LICENSE-QuanX-{source_name}.txt"
                compat_content = fetch(compat_source, 1024 * 1024)
                compat_rows = list(quanx_legacy_rules(
                    compat_content.decode("utf-8-sig"), compat_source, policies.get("quanx", "DIRECT")))
                if not compat_rows:
                    raise ValueError(f"No rules in QuanX companion file: {compat_source}")
            exports = {}
            for target, types in TARGETS.items():
                if target not in policies:
                    continue
                supported = [rule for rule in selected if can_export(rule, target, types)]
                omitted = source_omitted + sorted(f"{rule.kind}:{rule.value}" for rule in selected
                                               if not can_export(rule, target, types))
                include_compat = target == "quanx" and merge_compat and bool(compat_rows)
                import_quanx = target == "nextin" and category.get("nextin_import_quanx", False)
                if not supported and not include_compat and not import_quanx:
                    raise ValueError(f"No convertible rules in {slug}/{target}")
                exports[target] = {"count": len(supported), "omitted": omitted}
                if target == "openclash":
                    write_yaml(version / target / f"{slug}.yaml",
                               {"payload": [f"{types[r.kind]},{r.value}" for r in supported]})
                elif target == "quanx":
                    rows = [(types[r.kind], r.value, policies[target]) for r in supported]
                    if include_compat:
                        rows.extend(compat_rows)
                        exports[target]["compat_source"] = compat_source
                    rows = dict.fromkeys(rows)
                    if not rows:
                        raise ValueError(f"No QuanX rules in {slug}")
                    exports[target]["count"] = len(rows)
                    lines = [", ".join(row) for row in rows]
                    if geosite:
                        lines = [f"# Source: {geosite_url}", "# License: ../LICENSE-MetaCubeX.txt",
                                 *lines]
                    if include_compat:
                        lines = [f"# Compatibility source: {compat_source}",
                                 f"# See ../{compat_license}", *lines]
                    content = "\n".join(lines) + "\n"
                    write(version / target / f"{slug}.list", content)
                    if alias := category.get("quanx_alias"):
                        write(version / target / alias, content)
                elif target == "homeproxy":
                    fields = {}
                    for rule in supported:
                        fields.setdefault(types[rule.kind], []).append(rule.value)
                    rules = [{key: values} for key, values in fields.items()]
                    write_json(version / target / f"{slug}.json",
                               {"version": policy["homeproxy_rule_set_version"], "rules": rules})
                else:
                    payload = [f"{types[r.kind]},{r.value}" for r in supported]
                    if ads:
                        payload = [f"+.{rule.value}" for rule in supported]
                        exports[target].update(behavior="domain", mrs=mrs)
                    if import_quanx:
                        extra, unconverted = nextin_legacy_payload(compat_rows if merge_compat else [])
                        payload = list(dict.fromkeys(payload + extra))
                        if not payload:
                            raise ValueError(f"No Nextin rules in {slug}")
                        exports[target]["count"] = len(payload)
                        exports[target]["omitted"].extend(unconverted)
                        if merge_compat and compat_rows:
                            exports[target]["compat_source"] = compat_source
                    write_yaml(version / target / f"{slug}.yaml", {"payload": payload})
                    # Existing Nextin imports continue to fetch the former residual provider.
                    if alias := category.get("nextin_yaml_alias"):
                        write_yaml(version / target / alias, {"payload": payload})
                    if not ads:
                        exports[target]["provider_files"] = nextin_provider_files(
                            version / target, slug, payload, mihomo, scratch, meta_mrs)
                    if active and category.get("nextin_default", True):
                        for line in payload:
                            rule_type, value, *options = line.split(",")
                            nextin_inline_rules.append(",".join([rule_type, value, policies[target], *options]))
            source_url = geosite_url if geosite else mrs["source"] if ads else category["dlc"]
            if not source_url:
                source_url = compat_source if compat_path else f"overrides/{slug}.yml"
            results[slug] = {"source": source_url,
                             "selected": len(selected), "targets": exports}
            if cidr_url:
                results[slug].update(cidr_source=cidr_url, cidr_count=len(cidrs))
            provider = f"network-rules-{slug}"
            clash_url = f"{base}/openclash/{slug}.yaml"
            quanx_url = f"{base}/quanx/{slug}.list"
            homeproxy_url = f"{base}/homeproxy/{slug}.json"
            if active and "openclash" in policies:
                providers[provider] = provider_config(clash_url, provider)
                clash_rules.append(f"RULE-SET,{provider},{policies['openclash']}")
            if active and "nextin" in policies and category.get("nextin_default", True):
                add_nextin_providers(base, exports["nextin"], policies["nextin"],
                                     nextin_providers, nextin_remote_rules)
            if "quanx" in policies:
                quanx_lines.append(f"{quanx_url}, tag={slug}, force-policy={policies['quanx']}, "
                                   f"update-interval=86400, enabled={str(active).lower()}")
            if compat_path and "quanx" in policies and (not merge_compat or category.get("quanx_keep_companion")):
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
            if ads:
                links[3] = f"[MRS]({base}/nextin/{mrs['filename']}) · {links[3]}"
            elif "nextin" in exports:
                compact = [f"[MRS]({base}/nextin/{item['filename']})"
                           for item in exports["nextin"]["provider_files"] if item["format"] == "mrs"]
                links[3] = " · ".join([*compact, links[3]])
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
        routing = policy["nextin_routing"]
        remote_routing = nextin_routing_template(nextin_template, routing, results, base)
        write_yaml(version / "nextin" / "quanx-routing.yaml", remote_routing)
        geoip_sources = {code: f"{meta_base}/geo/geoip/{name}.yaml"
                         for code, name in routing["inline_geoip"].items()}
        inline_routing = nextin_inline_routing_template(remote_routing, version / "nextin", scratch, geoip_sources)
        inline_path = "nextin/quanx-routing-inline.yaml"
        write_yaml(version / inline_path, inline_routing)
        hub_path = "nextin/quanx-routing-apple-tv.yaml"
        write_yaml(version / hub_path, nextin_hub_routing_template(inline_routing, routing["hub_geosite"]))
        nextin_url = f"{base}/nextin/template.yaml"
        nextin_remote_url = f"{base}/nextin/template-remote.yaml"
        deep_link = "nextin://install?" + urlencode({"module": "subscription-template",
                                                   "url": nextin_remote_url})
        write(version / "README.md", "# Network-Rules 订阅目录\n\n"
              "| 分类 | OpenClash | QuanX | HomeProxy | Nextin |\n| --- | --- | --- | --- | --- |\n"
              + "\n".join(index) + f"\n\n[Nextin 自动拉取模板]({nextin_remote_url})"
              f" · [Nextin 内嵌模板]({nextin_url})\n\n"
              f"[Nextin 个人分流模板]({base}/nextin/quanx-routing.yaml)"
              f" · [Nextin 内嵌个人分流模板]({base}/{inline_path})"
              f" · [Nextin Apple TV 模板]({base}/{hub_path})\n\n"
              f"Nextin 自动拉取模板导入链接\n\n`{deep_link}`\n\n"
              "QuanX 图标见 [icons.json](quanx/icons.json)。将 `img-url` 加到对应 `[policy]` 策略组。\n\n"
              "各端省略的规则见 [manifest.json](manifest.json) 中的 `omitted`。\n")
        license_text = fetch(f"https://raw.githubusercontent.com/{config['dlc']}/{release}/LICENSE", 64 * 1024)
        write(version / "LICENSE-DLC.txt", license_text.decode("utf-8"))
        license_text = fetch(f"https://raw.githubusercontent.com/{meta['repository']}/master/LICENSE", 64 * 1024)
        write(version / "LICENSE-MetaCubeX.txt", license_text.decode("utf-8"))
        for name, upstream_base in compat_bases.items():
            license_text = fetch(f"{upstream_base}/LICENSE", 64 * 1024)
            write(version / f"LICENSE-QuanX-{name}.txt", license_text.decode("utf-8"))
        artifacts = {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(version.rglob("*")) if path.is_file()}
        write_json(version / "manifest.json", {"release": release, "source_sha256": checksum,
                                              "nextin_inline_geoip_sources": geoip_sources,
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
        build(args.output, args.repository, args.release, os.getenv("MIHOMO", "mihomo"))
    elif args.command == "check":
        check(args.directory)
    else:
        diff(args.previous, args.candidate, args.gate)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError) as error:
        sys.exit(f"Network-Rules: {error}")

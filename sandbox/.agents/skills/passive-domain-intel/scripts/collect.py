#!/usr/bin/env python3
"""Bounded passive intelligence collection for one exact domain."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


COLLECTOR_VERSION = "1.0.0"
SCHEMA = "xuanmu.passive-domain-intel.manifest"
DNS_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "SOA", "TXT")
DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SUMMARY_ENTITY_CAP = 25


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect bounded passive DNS, RDAP, and crt.sh data without printing raw responses."
    )
    parser.add_argument("domain", help="exact domain scope, with no scheme, path, port, or wildcard")
    parser.add_argument("-o", "--output", required=True, type=Path, help="caller-provided output directory")
    parser.add_argument("--fixtures", type=Path, help="offline fixture directory; disables network and dig")
    parser.add_argument("--timeout", type=float, default=10.0, help="timeout per command/request in seconds (default: 10)")
    parser.add_argument("--cap", type=int, default=500, help="maximum normalized results per source (default: 500)")
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=8_000_000,
        help="maximum bytes read from one HTTP response or fixture (default: 8000000)",
    )
    args = parser.parse_args()
    if not 0.1 <= args.timeout <= 60:
        parser.error("--timeout must be between 0.1 and 60 seconds")
    if not 1 <= args.cap <= 5000:
        parser.error("--cap must be between 1 and 5000")
    if not 1024 <= args.max_response_bytes <= 64_000_000:
        parser.error("--max-response-bytes must be between 1024 and 64000000")
    if args.fixtures is not None and not args.fixtures.is_dir():
        parser.error("--fixtures must be an existing directory")
    return args


def normalize_domain(value: str) -> str:
    value = value.strip().rstrip(".")
    if not value or any(char in value for char in "/:@*?#"):
        raise ValueError("domain must be one exact DNS name without scheme, path, port, or wildcard")
    try:
        domain = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("domain is not valid IDNA") from exc
    if len(domain) > 253 or "." not in domain or any(not DOMAIN_LABEL.fullmatch(label) for label in domain.split(".")):
        raise ValueError("domain must be a valid multi-label DNS name")
    return domain


def scoped_name(value: str, scope: str) -> str | None:
    value = value.strip().lower().rstrip(".")
    if value.startswith("*."):
        value = value[2:]
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if value != scope and not value.endswith("." + scope):
        return None
    if len(value) > 253 or any(not DOMAIN_LABEL.fullmatch(label) for label in value.split(".")):
        return None
    return value


def write_raw(raw_dir: Path, name: str, data: bytes) -> str:
    path = raw_dir / name
    path.write_bytes(data)
    return f"raw/{name}"


def read_fixture(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"fixture exceeds {max_bytes} byte limit")
    return data


def fetch(url: str, timeout: float, max_bytes: int) -> tuple[bytes, str | None]:
    request = urllib.request.Request(url, headers={"User-Agent": f"XuanMu-passive-domain-intel/{COLLECTOR_VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            return data, f"response exceeded {max_bytes} byte limit"
        return data, None
    except urllib.error.HTTPError as exc:
        data = exc.read(max_bytes + 1)
        suffix = f"; body exceeded {max_bytes} byte limit" if len(data) > max_bytes else ""
        return data, f"HTTP {exc.code}{suffix}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return b"", f"request failed: {exc}"


def parse_dns(data: bytes, scope: str, cap: int) -> list[dict[str, str]]:
    records: set[tuple[str, str, str]] = set()
    for line in data.decode("utf-8", "replace").splitlines():
        fields = line.split(None, 4)
        if len(fields) != 5 or fields[2].upper() != "IN":
            continue
        name = scoped_name(fields[0], scope)
        record_type = fields[3].upper()
        if name and record_type in DNS_TYPES:
            records.add((name, record_type, fields[4].strip()[:2048]))
    return [
        {"name": name, "type": record_type, "value": value}
        for name, record_type, value in sorted(records)[:cap]
    ]


def collect_dns(args: argparse.Namespace, scope: str, raw_dir: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw_files: list[str] = []
    errors: list[str] = []
    successful = 0
    records: list[dict[str, str]] = []
    for record_type in DNS_TYPES:
        stdout_name = f"dns_{record_type}.txt"
        stderr_name = f"dns_{record_type}.stderr.txt"
        if args.fixtures:
            fixture = args.fixtures / stdout_name
            if not fixture.is_file():
                errors.append(f"{record_type}: missing fixture {stdout_name}")
                continue
            try:
                stdout = read_fixture(fixture, args.max_response_bytes)
            except (OSError, ValueError) as exc:
                errors.append(f"{record_type}: {exc}")
                continue
            stderr_fixture = args.fixtures / stderr_name
            stderr = read_fixture(stderr_fixture, args.max_response_bytes) if stderr_fixture.is_file() else b""
            successful += 1
        else:
            command = [
                "dig",
                f"+time={max(1, math.ceil(args.timeout))}",
                "+tries=1",
                "+noall",
                "+answer",
                scope,
                record_type,
            ]
            try:
                process = subprocess.run(command, capture_output=True, timeout=args.timeout, check=False)
                stdout, stderr = process.stdout, process.stderr
                if len(stdout) > args.max_response_bytes or len(stderr) > args.max_response_bytes:
                    errors.append(f"{record_type}: command output exceeded {args.max_response_bytes} byte limit")
                elif process.returncode != 0:
                    errors.append(f"{record_type}: dig exited {process.returncode}")
                else:
                    successful += 1
            except FileNotFoundError:
                errors.append(f"{record_type}: dig is not installed")
                stdout, stderr = b"", b""
            except subprocess.TimeoutExpired as exc:
                errors.append(f"{record_type}: timed out after {args.timeout:g}s")
                stdout, stderr = exc.stdout or b"", exc.stderr or b""
        raw_files.append(write_raw(raw_dir, stdout_name, stdout[: args.max_response_bytes]))
        raw_files.append(write_raw(raw_dir, stderr_name, stderr[: args.max_response_bytes]))
        records.extend(parse_dns(stdout, scope, args.cap))
    deduplicated = [dict(zip(("name", "type", "value"), row)) for row in sorted({(r["name"], r["type"], r["value"]) for r in records})[: args.cap]]
    status = "ok" if not errors else ("partial" if successful else "failed")
    return source_result("dns", status, len(deduplicated), raw_files, errors), deduplicated


def json_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from json_strings(item)
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from json_strings(value[key])


def collect_json_source(
    name: str,
    fixture_name: str,
    url: str,
    args: argparse.Namespace,
    raw_dir: Path,
) -> tuple[dict[str, Any], Any | None]:
    errors: list[str] = []
    if args.fixtures:
        fixture = args.fixtures / fixture_name
        if not fixture.is_file():
            return source_result(name, "failed", 0, [], [f"missing fixture {fixture_name}"]), None
        try:
            data = read_fixture(fixture, args.max_response_bytes)
        except (OSError, ValueError) as exc:
            return source_result(name, "failed", 0, [], [str(exc)]), None
    else:
        data, error = fetch(url, args.timeout, args.max_response_bytes)
        if error:
            errors.append(error)
    raw_file = write_raw(raw_dir, fixture_name, data[: args.max_response_bytes])
    if not errors:
        try:
            parsed = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"invalid JSON: {exc}")
            parsed = None
    else:
        parsed = None
    status = "ok" if parsed is not None else "failed"
    return source_result(name, status, 1 if parsed is not None else 0, [raw_file], errors), parsed


def source_result(name: str, status: str, count: int, raw_files: list[str], errors: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "result_count": count,
        "raw_files": raw_files,
        "errors": errors[:100],
    }


def raw_metadata(output: Path, relative_path: str) -> dict[str, Any]:
    data = (output / relative_path).read_bytes()
    return {"path": relative_path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        scope = normalize_domain(args.domain)
    except ValueError as exc:
        print(f"collect.py: error: {exc}", file=sys.stderr)
        return 2

    output = args.output.expanduser().resolve()
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    dns_source, dns_records = collect_dns(args, scope, raw_dir)
    rdap_url = "https://rdap.org/domain/" + urllib.parse.quote(scope, safe="")
    rdap_source, rdap = collect_json_source("rdap", "rdap.json", rdap_url, args, raw_dir)
    crt_url = "https://crt.sh/?" + urllib.parse.urlencode({"q": f"%.{scope}", "output": "json"})
    crt_source, crt = collect_json_source("crtsh", "crtsh.json", crt_url, args, raw_dir)
    if crt is not None and not isinstance(crt, list):
        crt_source["status"] = "failed"
        crt_source["result_count"] = 0
        crt_source["errors"].append("expected a JSON array")
        crt = None

    subdomains = {scope}
    addresses: set[str] = set()
    for record in dns_records:
        candidate = scoped_name(record["value"].split()[-1], scope)
        if candidate:
            subdomains.add(candidate)
        try:
            addresses.add(str(ipaddress.ip_address(record["value"])))
        except ValueError:
            pass
    rdap_names: set[str] = set()
    rdap_addresses: set[str] = set()
    if rdap is not None:
        for text in json_strings(rdap):
            name = scoped_name(text, scope)
            if name:
                rdap_names.add(name)
            try:
                rdap_addresses.add(str(ipaddress.ip_address(text.strip())))
            except ValueError:
                pass
            if len(rdap_names) + len(rdap_addresses) >= args.cap:
                break
    subdomains.update(rdap_names)
    addresses.update(rdap_addresses)
    if rdap is not None:
        rdap_source["result_count"] = len(rdap_names) + len(rdap_addresses)
    crt_names: set[str] = set()
    if crt is not None:
        for row in crt[: args.cap]:
            if not isinstance(row, dict):
                continue
            for field in ("common_name", "name_value"):
                value = row.get(field)
                if isinstance(value, str):
                    for item in value.splitlines():
                        name = scoped_name(item, scope)
                        if name:
                            crt_names.add(name)
                            if len(crt_names) >= args.cap:
                                break
    subdomains.update(crt_names)
    crt_source["result_count"] = len(crt_names)

    sources = [dns_source, rdap_source, crt_source]
    for source in sources:
        source["raw_files"] = [raw_metadata(output, path) for path in source["raw_files"]]
    sorted_subdomains = [scope, *sorted(subdomains - {scope})][: args.cap]
    sorted_addresses = sorted(
        addresses,
        key=lambda value: (ipaddress.ip_address(value).version, int(ipaddress.ip_address(value))),
    )[: args.cap]
    manifest = {
        "schema": SCHEMA,
        "schema_version": 1,
        "collector": {"name": "passive-domain-intel", "version": COLLECTOR_VERSION},
        "mode": "fixtures" if args.fixtures else "online",
        "scope": {"input": args.domain, "domain": scope},
        "limits": {
            "per_source_results": args.cap,
            "timeout_seconds": args.timeout,
            "max_response_bytes": args.max_response_bytes,
        },
        "sources": sources,
        "entities": {
            "dns_records": dns_records,
            "ip_addresses": sorted_addresses,
            "subdomains": sorted_subdomains,
        },
    }
    summary = {
        "schema_version": 1,
        "scope": scope,
        "counts": {
            "dns_records": len(dns_records),
            "ip_addresses": len(sorted_addresses),
            "subdomains": len(sorted_subdomains),
        },
        "source_status": {source["name"]: source["status"] for source in sources},
        "failed_sources": [source["name"] for source in sources if source["status"] != "ok"],
        "highlights": {
            "ip_addresses": manifest["entities"]["ip_addresses"][:SUMMARY_ENTITY_CAP],
            "subdomains": manifest["entities"]["subdomains"][:SUMMARY_ENTITY_CAP],
        },
    }
    write_json(output / "manifest.json", manifest)
    write_json(output / "summary.json", summary)
    print(json.dumps({"manifest": str(output / "manifest.json"), "summary": str(output / "summary.json")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

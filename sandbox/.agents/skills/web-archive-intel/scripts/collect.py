#!/usr/bin/env python3
"""Bounded Wayback CDX and Common Crawl URL intelligence collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


COLLECTOR_VERSION = "1.0.0"
SCHEMA = "xuanmu.web-archive-intel.manifest"
UNRESERVED_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")
INTERESTING_PATH = re.compile(
    r"(?:^|[/_.-])(admin|api|auth|backup|config|debug|graphql|login|openapi|status|swagger)(?:[/_.?=-]|$)",
    re.IGNORECASE,
)
SUMMARY_ENTITY_CAP = 25


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect bounded Wayback CDX and Common Crawl metadata without printing raw responses."
    )
    parser.add_argument("url", help="exact HTTP(S) URL prefix scope; fragments and credentials are rejected")
    parser.add_argument("-o", "--output", required=True, type=Path, help="caller-provided output directory")
    parser.add_argument("--fixtures", type=Path, help="offline fixture directory; disables all network requests")
    parser.add_argument("--timeout", type=float, default=15.0, help="timeout per request in seconds (default: 15)")
    parser.add_argument("--cap", type=int, default=1000, help="maximum accepted records per source (default: 1000)")
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=16_000_000,
        help="maximum bytes read from one response or fixture (default: 16000000)",
    )
    parser.add_argument(
        "--common-crawl-index",
        help="specific Common Crawl CDX API URL or index name; otherwise use the latest advertised index",
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


def normalize_escape(match: re.Match[str]) -> str:
    value = int(match.group(1), 16)
    character = chr(value)
    return character if character.isascii() and (character.isalnum() or character in "-._~") else f"%{value:02X}"


def normalize_url(value: str) -> str:
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("URL must include an http or https scheme and hostname")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError("URL credentials and fragments are not allowed")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("URL hostname or port is invalid") from exc
    if not host or any(char.isspace() for char in host):
        raise ValueError("URL hostname is invalid")
    if ":" in host:
        host = f"[{host}]"
    default_port = (parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = UNRESERVED_ESCAPE.sub(normalize_escape, parts.path or "/")
    path = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=-._~")
    if not path.startswith("/"):
        path = "/" + path
    if has_dot_segments(path):
        raise ValueError("URL paths containing dot segments are not allowed")
    query = UNRESERVED_ESCAPE.sub(normalize_escape, parts.query)
    query = urllib.parse.quote(query, safe="%/?@:!$&'()*+,;=-._~")
    normalized = urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, query, ""))
    if len(normalized) > 500:
        raise ValueError("normalized URL exceeds 500 characters")
    return normalized


def has_dot_segments(path: str) -> bool:
    decoded = urllib.parse.unquote(path)
    return "\\" in decoded or any(segment in (".", "..") for segment in decoded.split("/"))


def scope_parts(url: str) -> tuple[str, int | None, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.hostname or "", parts.port, parts.path


def is_in_scope(url: str, scope: str) -> bool:
    url_parts = urllib.parse.urlsplit(url)
    scope_parts_value = urllib.parse.urlsplit(scope)
    scope_host, scope_port, scope_path = scope_parts(scope)
    if url_parts.hostname != scope_host or url_parts.port != scope_port:
        return False
    if not url_parts.path.startswith(scope_path):
        return False
    return not scope_parts_value.query or url_parts.query.startswith(scope_parts_value.query)


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
    request = urllib.request.Request(url, headers={"User-Agent": f"XuanMu-web-archive-intel/{COLLECTOR_VERSION}"})
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


def source_result(name: str, status: str, count: int, raw_files: list[str], errors: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "result_count": count,
        "raw_files": raw_files,
        "errors": errors[:100],
    }


def capture(url: str, timestamp: Any, status: Any, mime: Any, digest: Any, source: str) -> dict[str, Any]:
    parts = urllib.parse.urlsplit(url)
    return {
        "url": url,
        "host": parts.hostname or "",
        "path": parts.path,
        "query": parts.query,
        "timestamp": str(timestamp or "")[:32],
        "status": str(status or "")[:16],
        "mime": str(mime or "")[:255],
        "digest": str(digest or "")[:255],
        "source": source,
    }


def parse_wayback(data: bytes, scope: str, cap: int) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], [f"invalid JSON: {exc}"]
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        return [], ["expected a CDX JSON table with a header row"]
    header = [str(value) for value in payload[0]]
    required = {"timestamp", "original"}
    if not required.issubset(header):
        return [], ["CDX header is missing timestamp or original"]
    records: list[dict[str, Any]] = []
    for row in payload[1:]:
        if len(records) >= cap:
            break
        if not isinstance(row, list) or len(row) != len(header):
            errors.append("ignored malformed CDX row")
            continue
        item = dict(zip(header, row))
        try:
            url = normalize_url(str(item["original"]))
        except ValueError:
            continue
        if is_in_scope(url, scope):
            records.append(capture(url, item.get("timestamp"), item.get("statuscode"), item.get("mimetype"), item.get("digest"), "wayback"))
    return deduplicate(records, cap), errors


def parse_common_crawl(data: bytes, scope: str, cap: int) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if len(records) >= cap:
            break
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"ignored malformed NDJSON line {number}")
            continue
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            errors.append(f"ignored malformed NDJSON record at line {number}")
            continue
        try:
            url = normalize_url(item["url"])
        except ValueError:
            continue
        if is_in_scope(url, scope):
            records.append(capture(url, item.get("timestamp"), item.get("status"), item.get("mime"), item.get("digest"), "commoncrawl"))
    return deduplicate(records, cap), errors


def deduplicate(records: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    keyed = {
        (record["url"], record["timestamp"], record["status"], record["mime"], record["digest"], record["source"]): record
        for record in records
    }
    return [keyed[key] for key in sorted(keyed)[:cap]]


def common_crawl_api(option: str | None, indexes: Any) -> str:
    if option:
        if option.startswith("https://"):
            return option.rstrip("/")
        if re.fullmatch(r"CC-MAIN-[0-9]{4}-[0-9]{2}", option):
            return f"https://index.commoncrawl.org/{option}-index"
        raise ValueError("--common-crawl-index must be an HTTPS CDX API URL or CC-MAIN-YYYY-NN name")
    if not isinstance(indexes, list):
        raise ValueError("Common Crawl index list is not a JSON array")
    for item in indexes:
        if isinstance(item, dict) and isinstance(item.get("cdx-api"), str) and item["cdx-api"].startswith("https://"):
            return item["cdx-api"].rstrip("/")
    raise ValueError("Common Crawl index list has no HTTPS cdx-api")


def raw_metadata(output: Path, relative_path: str) -> dict[str, Any]:
    data = (output / relative_path).read_bytes()
    return {"path": relative_path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        scope = normalize_url(args.url)
    except ValueError as exc:
        print(f"collect.py: error: {exc}", file=sys.stderr)
        return 2

    output = args.output.expanduser().resolve()
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    query_target = urllib.parse.urlsplit(scope).netloc + urllib.parse.urlsplit(scope).path
    if urllib.parse.urlsplit(scope).query:
        query_target += "?" + urllib.parse.urlsplit(scope).query

    wayback_params = {
        "url": query_target,
        "matchType": "prefix",
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "collapse": "urlkey",
        "limit": str(args.cap),
    }
    wayback_url = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(wayback_params)
    wayback_errors: list[str] = []
    if args.fixtures:
        fixture = args.fixtures / "wayback.json"
        try:
            wayback_data = read_fixture(fixture, args.max_response_bytes)
        except (OSError, ValueError) as exc:
            wayback_data, wayback_errors = b"", [f"wayback.json: {exc}"]
    else:
        wayback_data, error = fetch(wayback_url, args.timeout, args.max_response_bytes)
        if error:
            wayback_errors.append(error)
    wayback_raw = write_raw(raw_dir, "wayback.json", wayback_data[: args.max_response_bytes])
    wayback_records: list[dict[str, Any]] = []
    if not wayback_errors:
        wayback_records, parse_errors = parse_wayback(wayback_data, scope, args.cap)
        wayback_errors.extend(parse_errors)
    wayback_status = "ok" if not wayback_errors else ("partial" if wayback_records else "failed")
    wayback_source = source_result("wayback", wayback_status, len(wayback_records), [wayback_raw], wayback_errors)

    cc_errors: list[str] = []
    cc_raw_files: list[str] = []
    if args.common_crawl_index:
        indexes_data, indexes = b"", None
    elif args.fixtures:
        indexes_fixture = args.fixtures / "commoncrawl-indexes.json"
        try:
            indexes_data = read_fixture(indexes_fixture, args.max_response_bytes)
            indexes = json.loads(indexes_data)
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            indexes_data, indexes = b"", None
            cc_errors.append(f"commoncrawl-indexes.json: {exc}")
    else:
        indexes_data, error = fetch("https://index.commoncrawl.org/collinfo.json", args.timeout, args.max_response_bytes)
        if error:
            cc_errors.append(error)
            indexes = None
        else:
            try:
                indexes = json.loads(indexes_data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                indexes = None
                cc_errors.append(f"invalid Common Crawl index JSON: {exc}")
    if not args.common_crawl_index:
        cc_raw_files.append(write_raw(raw_dir, "commoncrawl-indexes.json", indexes_data[: args.max_response_bytes]))
    try:
        cc_api = common_crawl_api(args.common_crawl_index, indexes)
    except ValueError as exc:
        cc_api = ""
        cc_errors.append(str(exc))

    cc_data = b""
    if cc_api:
        cc_params = {"url": query_target, "matchType": "prefix", "output": "json", "pageSize": str(args.cap)}
        cc_url = cc_api + "?" + urllib.parse.urlencode(cc_params)
        if args.fixtures:
            fixture = args.fixtures / "commoncrawl.ndjson"
            try:
                cc_data = read_fixture(fixture, args.max_response_bytes)
            except (OSError, ValueError) as exc:
                cc_errors.append(f"commoncrawl.ndjson: {exc}")
        else:
            cc_data, error = fetch(cc_url, args.timeout, args.max_response_bytes)
            if error:
                cc_errors.append(error)
    cc_raw_files.append(write_raw(raw_dir, "commoncrawl.ndjson", cc_data[: args.max_response_bytes]))
    cc_records: list[dict[str, Any]] = []
    if cc_data:
        cc_records, parse_errors = parse_common_crawl(cc_data, scope, args.cap)
        cc_errors.extend(parse_errors)
    cc_status = "ok" if not cc_errors else ("partial" if cc_records else "failed")
    cc_source = source_result("commoncrawl", cc_status, len(cc_records), cc_raw_files, cc_errors)

    sources = [wayback_source, cc_source]
    for source in sources:
        source["raw_files"] = [raw_metadata(output, path) for path in source["raw_files"]]
    all_records = deduplicate(wayback_records + cc_records, args.cap * 2)
    urls = sorted({record["url"] for record in all_records})
    hosts = sorted({record["host"] for record in all_records})
    paths = sorted({record["path"] for record in all_records})
    manifest = {
        "schema": SCHEMA,
        "schema_version": 1,
        "collector": {"name": "web-archive-intel", "version": COLLECTOR_VERSION},
        "mode": "fixtures" if args.fixtures else "online",
        "scope": {"input": args.url, "url_prefix": scope},
        "limits": {
            "per_source_results": args.cap,
            "timeout_seconds": args.timeout,
            "max_response_bytes": args.max_response_bytes,
        },
        "common_crawl_index": cc_api or None,
        "sources": sources,
        "entities": {"captures": all_records, "hosts": hosts, "paths": paths, "urls": urls},
    }
    summary = {
        "schema_version": 1,
        "scope": scope,
        "counts": {"captures": len(all_records), "hosts": len(hosts), "paths": len(paths), "urls": len(urls)},
        "source_status": {source["name"]: source["status"] for source in sources},
        "failed_sources": [source["name"] for source in sources if source["status"] != "ok"],
        "highlights": {
            "hosts": hosts[:SUMMARY_ENTITY_CAP],
            "interesting_urls": [url for url in urls if INTERESTING_PATH.search(url)][:SUMMARY_ENTITY_CAP],
        },
    }
    write_json(output / "manifest.json", manifest)
    write_json(output / "summary.json", summary)
    print(json.dumps({"manifest": str(output / "manifest.json"), "summary": str(output / "summary.json")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

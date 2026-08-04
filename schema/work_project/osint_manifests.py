import hashlib
import ipaddress
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schema.work_project.assets import WorkProjectAssetRequest, WorkProjectAssetType
from schema.work_project.graph import WorkProjectGraphEdgeType


class OsintManifestAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80)
    asset: WorkProjectAssetRequest


class OsintManifestRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_asset_key: str = Field(min_length=1, max_length=80)
    target_asset_key: str = Field(min_length=1, max_length=80)
    type: WorkProjectGraphEdgeType
    label: str = Field(default="", max_length=255)


class OsintImportPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_schema: str
    scope: WorkProjectAssetRequest
    assets: list[OsintManifestAsset] = Field(max_length=10_001)
    relationships: list[OsintManifestRelationship] = Field(max_length=20_000)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class OsintManifestImportResponse(BaseModel):
    created_assets: int = 0
    unchanged_assets: int = 0
    created_relationships: int = 0
    unchanged_relationships: int = 0
    warnings: list[str] = Field(default_factory=list)


class _DomainDnsRecord(BaseModel):
    name: str
    type: str
    value: str


class _DomainEntities(BaseModel):
    dns_records: list[_DomainDnsRecord] = Field(default_factory=list)
    ip_addresses: list[str] = Field(default_factory=list)
    subdomains: list[str] = Field(default_factory=list)


class _DomainScope(BaseModel):
    domain: str


class _DomainManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_name: Literal["xuanmu.passive-domain-intel.manifest"] = Field(alias="schema")
    schema_version: Literal[1]
    scope: _DomainScope
    entities: _DomainEntities


class _ArchiveEntities(BaseModel):
    urls: list[str] = Field(default_factory=list)


class _ArchiveScope(BaseModel):
    url_prefix: str


class _ArchiveManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_name: Literal["xuanmu.web-archive-intel.manifest"] = Field(alias="schema")
    schema_version: Literal[1]
    scope: _ArchiveScope
    entities: _ArchiveEntities


def parse_osint_collector_manifest(payload: Any) -> OsintImportPlan:
    if not isinstance(payload, dict):
        raise ValueError("OSINT manifest must be a JSON object")
    schema = payload.get("schema")
    if schema == "xuanmu.passive-domain-intel.manifest":
        return _domain_import_plan(_DomainManifest.model_validate(payload))
    if schema == "xuanmu.web-archive-intel.manifest":
        return _archive_import_plan(_ArchiveManifest.model_validate(payload))
    raise ValueError(f"unsupported OSINT manifest schema: {schema or 'missing'}")


def _key(kind: str, value: str) -> str:
    return f"{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _domain_import_plan(manifest: _DomainManifest) -> OsintImportPlan:
    scope = manifest.scope.domain.strip().lower().rstrip(".")
    domain_values = {
        value.strip().lower().rstrip(".")
        for value in manifest.entities.subdomains
        if _in_domain_scope(value, scope)
    }
    domain_values.add(scope)
    ip_values: set[str] = set()
    warnings: list[str] = []
    for value in manifest.entities.ip_addresses:
        try:
            ip_values.add(str(ipaddress.ip_address(value.strip())))
        except ValueError:
            _warn(warnings, f"ignored invalid IP address: {value}")

    assets = [
        OsintManifestAsset(
            key=_key("domain", value),
            asset=WorkProjectAssetRequest(type=WorkProjectAssetType.DOMAIN, host=value),
        )
        for value in sorted(domain_values)
    ]
    assets.extend(
        OsintManifestAsset(
            key=_key("network", value),
            asset=WorkProjectAssetRequest(type=WorkProjectAssetType.NETWORK, host=value),
        )
        for value in sorted(ip_values, key=_ip_sort_key)
    )

    relationships: dict[tuple[str, str, WorkProjectGraphEdgeType], OsintManifestRelationship] = {}
    for record in manifest.entities.dns_records:
        name = record.name.strip().lower().rstrip(".")
        record_type = record.type.strip().upper()
        if name not in domain_values or record_type not in ("A", "AAAA"):
            continue
        try:
            address = str(ipaddress.ip_address(record.value.strip()))
        except ValueError:
            _warn(warnings, f"ignored invalid {record_type} value for {name}")
            continue
        if address not in ip_values:
            continue
        relationship = OsintManifestRelationship(
            source_asset_key=_key("domain", name),
            target_asset_key=_key("network", address),
            type=WorkProjectGraphEdgeType.RESOLVES_TO,
            label=f"passive DNS {record_type}",
        )
        identity = (relationship.source_asset_key, relationship.target_asset_key, relationship.type)
        relationships[identity] = relationship

    return OsintImportPlan(
        source_schema=manifest.schema_name,
        scope=WorkProjectAssetRequest(type=WorkProjectAssetType.DOMAIN, host=scope),
        assets=assets,
        relationships=[relationships[key] for key in sorted(relationships, key=lambda item: (item[0], item[1], item[2].value))],
        warnings=warnings,
    )


def _archive_import_plan(manifest: _ArchiveManifest) -> OsintImportPlan:
    from urllib.parse import unquote, urlsplit

    scope = urlsplit(manifest.scope.url_prefix)
    scope_host = (scope.hostname or "").lower().rstrip(".")
    if (
        scope.scheme not in ("http", "https")
        or not scope_host
        or scope.fragment
        or scope.username is not None
        or scope.password is not None
        or _has_dot_segments(scope.path, unquote)
    ):
        raise ValueError("archive manifest scope is not a valid HTTP(S) URL")

    domain_key = _key("domain", scope_host)
    assets = [
        OsintManifestAsset(
            key=domain_key,
            asset=WorkProjectAssetRequest(type=WorkProjectAssetType.DOMAIN, host=scope_host),
        )
    ]
    relationships: list[OsintManifestRelationship] = []
    warnings: list[str] = []
    seen_urls: set[str] = set()
    for value in sorted(set(manifest.entities.urls)):
        parts = urlsplit(value)
        if (
            parts.scheme != scope.scheme
            or (parts.hostname or "").lower().rstrip(".") != scope_host
            or parts.port != scope.port
            or not parts.path.startswith(scope.path)
            or (scope.query and not parts.query.startswith(scope.query))
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
            or _has_dot_segments(parts.path, unquote)
        ):
            _warn(warnings, f"ignored out-of-scope archive URL: {value[:200]}")
            continue
        if len(value) > 500:
            _warn(warnings, f"ignored archive URL longer than 500 characters: {value[:160]}")
            continue
        if value in seen_urls:
            continue
        seen_urls.add(value)
        url_key = _key("url", value)
        assets.append(OsintManifestAsset(
            key=url_key,
            asset=WorkProjectAssetRequest(type=WorkProjectAssetType.URL, path=value),
        ))
        relationships.append(OsintManifestRelationship(
            source_asset_key=domain_key,
            target_asset_key=url_key,
            type=WorkProjectGraphEdgeType.HOSTS,
            label="public web archive",
        ))
    return OsintImportPlan(
        source_schema=manifest.schema_name,
        scope=WorkProjectAssetRequest(type=WorkProjectAssetType.URL, path=manifest.scope.url_prefix),
        assets=assets,
        relationships=relationships,
        warnings=warnings,
    )


def _in_domain_scope(value: str, scope: str) -> bool:
    normalized = value.strip().lower().rstrip(".")
    return normalized == scope or normalized.endswith(f".{scope}")


def _ip_sort_key(value: str) -> tuple[int, int]:
    address = ipaddress.ip_address(value)
    return address.version, int(address)


def _warn(warnings: list[str], message: str) -> None:
    if len(warnings) < 100:
        warnings.append(message)


def _has_dot_segments(path: str, unquote) -> bool:
    decoded = unquote(path)
    return "\\" in decoded or any(segment in (".", "..") for segment in decoded.split("/"))

import io
import ipaddress
import re
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from schema.work_project.assets import WorkProjectAssetExtraSchema, WorkProjectAssetRequest, WorkProjectAssetType
from schema.work_project.graph import WorkProjectGraphEdgeType
from schema.work_project.scan_report_imports import (
    ScanReportAssetCandidate,
    ScanReportImportCounts,
    ScanReportRelationshipCandidate,
)


MAX_HOSTS = 10_000
MAX_SERVICES = 100_000
_STANDARD_NMAP_DOCTYPE = re.compile(rb"<!doctype\s+nmaprun\s*>", re.IGNORECASE)


class NmapXmlError(ValueError):
    pass


class ParsedNmapReport(BaseModel):
    assets: list[ScanReportAssetCandidate]
    relationships: list[ScanReportRelationshipCandidate]
    warnings: list[str] = Field(default_factory=list)
    counts: ScanReportImportCounts


def parse_nmap_xml(source: Path | BinaryIO | bytes) -> ParsedNmapReport:
    stream, should_close = _open_source(source)
    try:
        _reject_forbidden_xml(stream)
        stream.seek(0)
        return _parse_stream(stream)
    finally:
        if should_close:
            stream.close()


def _open_source(source: Path | BinaryIO | bytes) -> tuple[BinaryIO, bool]:
    if isinstance(source, bytes):
        return io.BytesIO(source), True
    if isinstance(source, Path):
        return source.open("rb"), True
    return source, False


def _reject_forbidden_xml(stream: BinaryIO) -> None:
    # Nmap emits a harmless `<!DOCTYPE nmaprun>` marker. ElementTree does not
    # load an external DTD, so allow only that exact declaration and reject
    # internal subsets, custom DTDs, and entity declarations.
    sample = stream.read().replace(b"\x00", b"")
    lowered = sample.lower()
    if b"<!entity" in lowered:
        raise NmapXmlError("ENTITY declarations are not allowed")
    without_standard_doctype = _STANDARD_NMAP_DOCTYPE.sub(b"", sample)
    if b"<!doctype" in without_standard_doctype.lower():
        raise NmapXmlError("only the standard <!DOCTYPE nmaprun> declaration is allowed")


def _parse_stream(stream: BinaryIO) -> ParsedNmapReport:
    assets: dict[str, ScanReportAssetCandidate] = {}
    relationships: dict[str, ScanReportRelationshipCandidate] = {}
    warnings: list[str] = []
    host_count = 0
    service_count = 0
    root_seen = False

    try:
        for event, element in ElementTree.iterparse(stream, events=("start", "end")):
            tag = _local_name(element.tag)
            if event == "start" and not root_seen:
                root_seen = True
                if tag != "nmaprun":
                    raise NmapXmlError("XML root must be nmaprun")
            if event != "end" or tag != "host":
                continue

            host_count += 1
            if host_count > MAX_HOSTS:
                raise NmapXmlError(f"Nmap report exceeds {MAX_HOSTS} hosts")
            service_count += _map_host(element, assets, relationships, warnings)
            if service_count > MAX_SERVICES:
                raise NmapXmlError(f"Nmap report exceeds {MAX_SERVICES} services")
            element.clear()
    except ElementTree.ParseError as error:
        if "no element found" in str(error):
            raise NmapXmlError(
                "Nmap XML is incomplete; wait for the scan to finish before importing it"
            ) from error
        raise NmapXmlError(f"invalid XML: {error}") from error

    if not root_seen:
        raise NmapXmlError("XML document is empty")

    counts = ScanReportImportCounts(
        hosts=host_count,
        assets=len(assets),
        relationships=len(relationships),
        networks=sum(asset.type == WorkProjectAssetType.NETWORK for asset in assets.values()),
        domains=sum(asset.type == WorkProjectAssetType.DOMAIN for asset in assets.values()),
        services=sum(asset.type == WorkProjectAssetType.SERVICE for asset in assets.values()),
    )
    return ParsedNmapReport(
        assets=list(assets.values()),
        relationships=list(relationships.values()),
        warnings=warnings,
        counts=counts,
    )


def _map_host(
    host_element: ElementTree.Element,
    assets: dict[str, ScanReportAssetCandidate],
    relationships: dict[str, ScanReportRelationshipCandidate],
    warnings: list[str],
) -> int:
    status = _first_child(host_element, "status")
    if status is None or status.get("state", "").lower() != "up":
        return _count_ports(host_element)

    network_keys: list[tuple[str, str]] = []
    for address in _children(host_element, "address"):
        address_type = address.get("addrtype", "").lower()
        if address_type not in {"ipv4", "ipv6"}:
            continue
        raw_address = address.get("addr", "").strip()
        try:
            ip = ipaddress.ip_address(raw_address)
        except ValueError:
            warnings.append(f"Ignored invalid {address_type} address: {raw_address or '<empty>'}")
            continue
        if (address_type == "ipv4") != (ip.version == 4):
            warnings.append(f"Ignored address with mismatched type: {raw_address}")
            continue
        request = WorkProjectAssetRequest(
            type=WorkProjectAssetType.NETWORK,
            host=f"{ip}/{ip.max_prefixlen}",
        )
        candidate = _candidate(request)
        assets.setdefault(candidate.key, candidate)
        network_keys.append((candidate.key, str(ip)))

    if not network_keys:
        warnings.append("Ignored an up host without an IPv4 or IPv6 address")
        return _count_ports(host_element)

    domain_keys: list[str] = []
    hostnames = _first_child(host_element, "hostnames")
    if hostnames is not None:
        for hostname in _children(hostnames, "hostname"):
            name = hostname.get("name", "").strip().lower().rstrip(".")
            if not name or len(name) > 255:
                warnings.append("Ignored an empty or overlong hostname")
                continue
            request = WorkProjectAssetRequest(type=WorkProjectAssetType.DOMAIN, host=name)
            candidate = _candidate(request)
            assets.setdefault(candidate.key, candidate)
            domain_keys.append(candidate.key)

    for domain_key in domain_keys:
        for network_key, _ in network_keys:
            _add_relationship(
                relationships,
                domain_key,
                network_key,
                WorkProjectGraphEdgeType.RESOLVES_TO,
            )

    port_count = 0
    ports = _first_child(host_element, "ports")
    if ports is None:
        return 0
    for port in _children(ports, "port"):
        port_count += 1
        state = _first_child(port, "state")
        if state is None or state.get("state", "").lower() != "open":
            continue
        try:
            port_number = int(port.get("portid", ""))
        except ValueError:
            warnings.append("Ignored an open port with an invalid port number")
            continue
        if not 1 <= port_number <= 65535:
            warnings.append(f"Ignored out-of-range open port: {port_number}")
            continue
        protocol = port.get("protocol", "tcp").strip().lower() or "tcp"
        service = _first_child(port, "service")
        service_name, banner = _service_details(service)
        if len(banner) > 512:
            banner = banner[:512]
            warnings.append(f"Truncated service banner for port {port_number}/{protocol}")
        for network_key, ip in network_keys:
            request = WorkProjectAssetRequest(
                type=WorkProjectAssetType.SERVICE,
                host=ip,
                port=port_number,
                extra=WorkProjectAssetExtraSchema(
                    banner=banner,
                    protocol=protocol,
                    service_name=service_name,
                ),
            )
            candidate = _candidate(request)
            assets.setdefault(candidate.key, candidate)
            _add_relationship(
                relationships,
                network_key,
                candidate.key,
                WorkProjectGraphEdgeType.HOSTS,
            )
    return port_count


def _candidate(request: WorkProjectAssetRequest) -> ScanReportAssetCandidate:
    key = f"{request.type.value}:{request.identifier}"
    return ScanReportAssetCandidate(
        key=key,
        type=request.type,
        identifier=request.identifier,
        host=request.host,
        port=request.port,
        path=request.path,
        extra=request.extra,
    )


def _add_relationship(
    relationships: dict[str, ScanReportRelationshipCandidate],
    source_key: str,
    target_key: str,
    edge_type: WorkProjectGraphEdgeType,
) -> None:
    key = f"{source_key}->{edge_type.value}->{target_key}"
    relationships.setdefault(
        key,
        ScanReportRelationshipCandidate(
            key=key,
            source_asset_key=source_key,
            target_asset_key=target_key,
            type=edge_type,
        ),
    )


def _service_details(service: ElementTree.Element | None) -> tuple[str, str]:
    if service is None:
        return "", ""
    name = service.get("name", "").strip()
    details = [
        service.get("tunnel", "").strip(),
        name,
        service.get("product", "").strip(),
        service.get("version", "").strip(),
        service.get("extrainfo", "").strip(),
    ]
    return name, " ".join(part for part in details if part)


def _count_ports(host_element: ElementTree.Element) -> int:
    ports = _first_child(host_element, "ports")
    return len(_children(ports, "port")) if ports is not None else 0


def _first_child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]

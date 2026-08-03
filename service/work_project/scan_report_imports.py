import asyncio
import hashlib
import json
import math
import shutil
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlmodel import select
from starlette.concurrency import run_in_threadpool

from config import WORKSPACE
from database import get_async_session
from model.blackboard.nodes import BlackboardNode
from model.work_project.assets import WorkProjectAsset
from model.work_project.graph import WorkProjectGraphEdge
from model.work_project.scan_report_imports import ScanReportImport
from schema.work_project.assets import WorkProjectAssetExtraSchema, WorkProjectAssetOrigin, WorkProjectAssetRequest
from schema.work_project.scan_report_imports import (
    CommitScanReportImportRequest,
    CommitScanReportImportResponse,
    ScanReportAssetCandidate,
    ScanReportImportFormat,
    ScanReportImportInfo,
    ScanReportImportPreviewResponse,
    ScanReportImportStatus,
)
from service.work_project.nmap_parser import NmapXmlError, ParsedNmapReport, parse_nmap_xml


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
UPLOAD_RATE_LIMIT_PER_PROJECT = 5
UPLOAD_RATE_LIMIT_PER_USER = 20
UPLOAD_RATE_LIMIT_WINDOW_SECONDS = 60
_ACCEPTED_XML_MEDIA_TYPES = {"application/xml", "text/xml"}
_REPORTS_DIRECTORY = "scan-reports"
_project_upload_attempts: dict[tuple[int, int], deque[float]] = {}
_user_upload_attempts: dict[int, deque[float]] = {}
_upload_rate_limit_lock = asyncio.Lock()


class ScanReportImportError(Exception):
    pass


class ScanReportImportBadRequest(ScanReportImportError):
    pass


class ScanReportImportNotFound(ScanReportImportError):
    pass


class ScanReportImportConflict(ScanReportImportError):
    pass


class ScanReportImportRateLimited(ScanReportImportError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"scan report upload rate limit exceeded; retry in {retry_after_seconds}s")


async def preview_scan_report_import(
    project_id: int,
    uploader_user_id: int,
    upload: UploadFile,
) -> ScanReportImportPreviewResponse:
    media_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in _ACCEPTED_XML_MEDIA_TYPES:
        await upload.close()
        raise ScanReportImportBadRequest("file media type must be application/xml or text/xml")

    import_id = str(uuid4())
    directory, original_path, normalized_path = _import_paths(project_id, import_id)
    directory.mkdir(parents=True, exist_ok=False)
    filename = _safe_filename(upload.filename)
    workspace = WORKSPACE.resolve()
    relative_original = original_path.relative_to(workspace).as_posix()
    relative_normalized = normalized_path.relative_to(workspace).as_posix()

    try:
        size_bytes, sha256 = await _stream_upload(upload, original_path)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        await upload.close()

    now = datetime.now()
    try:
        report = await run_in_threadpool(parse_nmap_xml, original_path)
        await run_in_threadpool(_write_normalized_report, normalized_path, report)
    except (NmapXmlError, OSError, ValueError) as error:
        await run_in_threadpool(shutil.rmtree, directory, True)
        raise ScanReportImportBadRequest(str(error)) from error

    async with get_async_session() as session:
        existing_assets = (await session.exec(
            select(WorkProjectAsset).where(WorkProjectAsset.project_id == project_id)
        )).all()
        existing_identities = {(asset.type, asset.identifier) for asset in existing_assets}
        duplicate_keys = sorted(
            candidate.key
            for candidate in report.assets
            if (candidate.type, candidate.identifier) in existing_identities
        )
        summary = {
            "counts": report.counts.model_dump(mode="json"),
            "warning_count": len(report.warnings),
            "duplicate_count": len(duplicate_keys),
        }
        import_row = ScanReportImport(
            import_id=import_id,
            project_id=project_id,
            uploader_user_id=uploader_user_id,
            filename=filename,
            media_type=media_type,
            sha256=sha256,
            format=ScanReportImportFormat.NMAP_XML,
            status=ScanReportImportStatus.PREVIEWED,
            size_bytes=size_bytes,
            original_file=relative_original,
            normalized_file=relative_normalized,
            summary=summary,
            created_at=now,
            updated_at=now,
        )
        session.add(import_row)
        await session.commit()
        await session.refresh(import_row)

    return ScanReportImportPreviewResponse(
        import_info=ScanReportImportInfo.model_validate(import_row),
        assets=report.assets,
        relationships=report.relationships,
        warnings=report.warnings,
        duplicate_keys=duplicate_keys,
        counts=report.counts,
    )


async def commit_scan_report_import(
    project_id: int,
    import_id: str,
    request: CommitScanReportImportRequest,
) -> CommitScanReportImportResponse:
    try:
        UUID(import_id)
    except ValueError as error:
        raise ScanReportImportNotFound("scan report import not found") from error

    async with get_async_session() as session:
        async with session.begin():
            import_row = (await session.exec(
                select(ScanReportImport)
                .where(
                    ScanReportImport.import_id == import_id,
                    ScanReportImport.project_id == project_id,
                )
                .with_for_update()
            )).first()
            if import_row is None:
                raise ScanReportImportNotFound("scan report import not found")
            if import_row.status == ScanReportImportStatus.COMMITTED:
                stored_result = import_row.summary.get("commit_result")
                if isinstance(stored_result, dict):
                    return CommitScanReportImportResponse.model_validate(stored_result)
                raise ScanReportImportConflict("scan report import is already committed")
            if import_row.status != ScanReportImportStatus.PREVIEWED:
                raise ScanReportImportConflict("scan report import is not ready to commit")

            normalized_path = _validated_normalized_path(import_row)
            try:
                report = ParsedNmapReport.model_validate_json(normalized_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ScanReportImportConflict("normalized scan report is unavailable or invalid") from error

            candidates = {candidate.key: candidate for candidate in report.assets}
            requested_keys = set(request.selected_asset_keys or candidates)
            unknown_keys = sorted(requested_keys - candidates.keys())
            if unknown_keys:
                raise ScanReportImportBadRequest(f"unknown selected asset key: {unknown_keys[0]}")
            selected_keys = set(candidates) if not request.selected_asset_keys else requested_keys

            result = await _commit_report_in_tx(
                session,
                import_row,
                report,
                selected_keys,
                request,
            )
            now = datetime.now()
            import_row.status = ScanReportImportStatus.COMMITTED
            import_row.committed_at = now
            import_row.updated_at = now
            import_row.summary = {**import_row.summary, "commit_result": result.model_dump(mode="json")}
            session.add(import_row)
        return result


async def _commit_report_in_tx(
    session,
    import_row: ScanReportImport,
    report: ParsedNmapReport,
    selected_keys: set[str],
    request: CommitScanReportImportRequest,
) -> CommitScanReportImportResponse:
    now = datetime.now()
    existing_rows = (await session.exec(
        select(WorkProjectAsset).where(WorkProjectAsset.project_id == import_row.project_id)
    )).all()
    existing = {(asset.type, asset.identifier): asset for asset in existing_rows}
    assets_by_key: dict[str, WorkProjectAsset] = {}
    created_assets: list[str] = []
    updated_assets: list[str] = []
    skipped_assets: list[str] = []

    for candidate in report.assets:
        if candidate.key not in selected_keys:
            skipped_assets.append(candidate.key)
            continue
        asset_request = _asset_request(candidate)
        asset = existing.get((asset_request.type, asset_request.identifier))
        if asset is None:
            asset = WorkProjectAsset(
                project_id=import_row.project_id,
                type=asset_request.type,
                origin=WorkProjectAssetOrigin.DISCOVERED,
                identifier=asset_request.identifier,
                host=asset_request.host,
                port=asset_request.port,
                path=asset_request.path,
                extra=asset_request.extra,
                created_at=now,
                updated_at=now,
            )
            session.add(asset)
            existing[(asset.type, asset.identifier)] = asset
            created_assets.append(candidate.key)
        else:
            merged_extra = _merge_extra(asset.extra, asset_request.extra)
            if merged_extra != asset.extra:
                asset.extra = merged_extra
                asset.updated_at = now
                session.add(asset)
                updated_assets.append(candidate.key)
            else:
                skipped_assets.append(candidate.key)
        assets_by_key[candidate.key] = asset

    await session.flush()

    eligible_relationships = [
        relationship
        for relationship in report.relationships
        if relationship.source_asset_key in selected_keys and relationship.target_asset_key in selected_keys
    ]
    eligible_relationship_keys = {relationship.key for relationship in eligible_relationships}
    created_relationships: list[str] = []
    skipped_relationships = [
        relationship.key
        for relationship in report.relationships
        if relationship.key not in eligible_relationship_keys
    ]
    if request.create_relationships:
        existing_edges = (await session.exec(
            select(WorkProjectGraphEdge).where(WorkProjectGraphEdge.project_id == import_row.project_id)
        )).all()
        edge_identities = {
            (edge.source_asset_id, edge.target_asset_id, edge.type) for edge in existing_edges
        }
        for relationship in eligible_relationships:
            source = assets_by_key[relationship.source_asset_key]
            target = assets_by_key[relationship.target_asset_key]
            identity = (source.id, target.id, relationship.type)
            if identity in edge_identities:
                skipped_relationships.append(relationship.key)
                continue
            session.add(WorkProjectGraphEdge(
                project_id=import_row.project_id,
                source_asset_id=source.id,
                target_asset_id=target.id,
                type=relationship.type,
                created_at=now,
                updated_at=now,
            ))
            edge_identities.add(identity)
            created_relationships.append(relationship.key)
    else:
        skipped_relationships.extend(relationship.key for relationship in eligible_relationships)

    blackboard_node_id: int | None = None
    if request.create_blackboard_fact:
        service_count = sum(
            candidate.type.value == "service" and candidate.key in selected_keys
            for candidate in report.assets
        )
        node = BlackboardNode(
            project_id=import_row.project_id,
            node_type="fact",
            status="confirmed",
            description=(
                f"Imported Nmap scan with {len(selected_keys)} assets and "
                f"{service_count} open services."
            ),
            extra=json.dumps({
                "import_id": import_row.import_id,
                "sha256": import_row.sha256,
                "counts": {
                    "selected_assets": len(selected_keys),
                    "services": service_count,
                    "created_assets": len(created_assets),
                    "updated_assets": len(updated_assets),
                    "created_relationships": len(created_relationships),
                },
                "original_file": import_row.original_file,
            }),
            created_at=now,
            updated_at=now,
        )
        session.add(node)
        await session.flush()
        blackboard_node_id = node.id

    return CommitScanReportImportResponse(
        import_id=import_row.import_id,
        created_assets=created_assets,
        updated_assets=updated_assets,
        skipped_assets=skipped_assets,
        created_relationships=created_relationships,
        skipped_relationships=skipped_relationships,
        blackboard_node_id=blackboard_node_id,
    )


def _asset_request(candidate: ScanReportAssetCandidate) -> WorkProjectAssetRequest:
    return WorkProjectAssetRequest(
        type=candidate.type,
        host=candidate.host,
        port=candidate.port,
        path=candidate.path,
        extra=candidate.extra,
    )


def _merge_extra(
    current: WorkProjectAssetExtraSchema,
    incoming: WorkProjectAssetExtraSchema,
) -> WorkProjectAssetExtraSchema:
    banner = current.banner
    if incoming.banner and incoming.banner not in banner:
        banner = f"{banner} | {incoming.banner}" if banner else incoming.banner
    return WorkProjectAssetExtraSchema(
        banner=banner[:512],
        protocol=incoming.protocol or current.protocol,
        service_name=incoming.service_name or current.service_name,
    )


async def _stream_upload(upload: UploadFile, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise ScanReportImportBadRequest("scan report exceeds the 50 MB upload limit")
            digest.update(chunk)
            output.write(chunk)
    if size == 0:
        raise ScanReportImportBadRequest("scan report is empty")
    return size, digest.hexdigest()


def _write_normalized_report(path: Path, report: ParsedNmapReport) -> None:
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


async def _enforce_upload_rate_limit(
    project_id: int,
    uploader_user_id: int,
    *,
    now: float | None = None,
) -> None:
    timestamp = time.monotonic() if now is None else now
    project_key = (uploader_user_id, project_id)
    async with _upload_rate_limit_lock:
        project_attempts = _project_upload_attempts.setdefault(project_key, deque())
        user_attempts = _user_upload_attempts.setdefault(uploader_user_id, deque())
        _discard_expired_attempts(project_attempts, timestamp)
        _discard_expired_attempts(user_attempts, timestamp)

        retry_after = 0
        if len(project_attempts) >= UPLOAD_RATE_LIMIT_PER_PROJECT:
            retry_after = max(retry_after, _retry_after(project_attempts, timestamp))
        if len(user_attempts) >= UPLOAD_RATE_LIMIT_PER_USER:
            retry_after = max(retry_after, _retry_after(user_attempts, timestamp))
        if retry_after:
            raise ScanReportImportRateLimited(retry_after)

        project_attempts.append(timestamp)
        user_attempts.append(timestamp)


def _discard_expired_attempts(attempts: deque[float], now: float) -> None:
    cutoff = now - UPLOAD_RATE_LIMIT_WINDOW_SECONDS
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()


def _retry_after(attempts: deque[float], now: float) -> int:
    return max(1, math.ceil(attempts[0] + UPLOAD_RATE_LIMIT_WINDOW_SECONDS - now))


def _import_paths(project_id: int, import_id: str) -> tuple[Path, Path, Path]:
    canonical_import_id = str(UUID(import_id))
    if project_id <= 0:
        raise ScanReportImportBadRequest("invalid work project id")
    root = WORKSPACE.resolve() / _REPORTS_DIRECTORY
    directory = (root / str(project_id) / canonical_import_id).resolve()
    if root not in directory.parents:
        raise ScanReportImportBadRequest("invalid scan report storage path")
    return directory, directory / "original.xml", directory / "normalized.json"


def _validated_normalized_path(import_row: ScanReportImport) -> Path:
    _, _, expected = _import_paths(import_row.project_id, import_row.import_id)
    relative = expected.relative_to(WORKSPACE.resolve()).as_posix()
    if import_row.normalized_file != relative:
        raise ScanReportImportConflict("invalid normalized scan report path")
    return expected


def _safe_filename(filename: str | None) -> str:
    name = (filename or "scan.xml").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (name or "scan.xml")[:255]

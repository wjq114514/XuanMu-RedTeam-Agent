from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from database import get_async_session
from model.work_project.assets import WorkProjectAsset
from model.work_project.graph import WorkProjectGraphEdge
from model.work_project.projects import WorkProject
from schema.work_project.assets import WorkProjectAssetOrigin, WorkProjectAssetType
from schema.work_project.osint_manifests import OsintImportPlan, OsintManifestImportResponse
from service.work_project.assets import apply_asset_request


async def import_osint_plan(
    project_id: int,
    plan: OsintImportPlan,
    *,
    created_by_agent_code: str,
    created_from_session_id: str,
) -> tuple[OsintManifestImportResponse | None, str]:
    try:
        async with get_async_session() as session:
            async with session.begin():
                project = (await session.exec(
                    select(WorkProject)
                    .where(WorkProject.id == project_id)
                    .with_for_update()
                )).first()
                if project is None:
                    return None, "work project not found"

                existing_assets = (await session.exec(
                    select(WorkProjectAsset).where(WorkProjectAsset.project_id == project_id)
                )).all()
                scope_assets = [
                    asset for asset in existing_assets
                    if asset.origin == WorkProjectAssetOrigin.SCOPE
                ]
                if not _plan_matches_project_scope(plan, scope_assets):
                    return None, "OSINT manifest scope is outside authoritative project scope"
                assets_by_identity = {(asset.type, asset.identifier): asset for asset in existing_assets}
                assets_by_key: dict[str, WorkProjectAsset] = {}
                result = OsintManifestImportResponse(warnings=plan.warnings)
                now = datetime.now()

                for candidate in plan.assets:
                    request = candidate.asset
                    asset = assets_by_identity.get(request.identity)
                    if asset is None:
                        asset = WorkProjectAsset(
                            project_id=project_id,
                            origin=WorkProjectAssetOrigin.DISCOVERED,
                            created_by_agent_code=created_by_agent_code.strip(),
                            created_from_session_id=created_from_session_id.strip(),
                            created_at=now,
                            updated_at=now,
                        )
                        apply_asset_request(asset, request, now)
                        session.add(asset)
                        assets_by_identity[request.identity] = asset
                        result.created_assets += 1
                    else:
                        result.unchanged_assets += 1
                    assets_by_key[candidate.key] = asset

                await session.flush()

                existing_edges = (await session.exec(
                    select(WorkProjectGraphEdge).where(WorkProjectGraphEdge.project_id == project_id)
                )).all()
                edge_identities = {
                    (edge.source_asset_id, edge.target_asset_id, edge.type) for edge in existing_edges
                }
                for relationship in plan.relationships:
                    source = assets_by_key.get(relationship.source_asset_key)
                    target = assets_by_key.get(relationship.target_asset_key)
                    if source is None or target is None or source.id is None or target.id is None:
                        raise ValueError("OSINT relationship references an unknown asset key")
                    identity = (source.id, target.id, relationship.type)
                    if identity in edge_identities:
                        result.unchanged_relationships += 1
                        continue
                    session.add(WorkProjectGraphEdge(
                        project_id=project_id,
                        source_asset_id=source.id,
                        target_asset_id=target.id,
                        type=relationship.type,
                        label=relationship.label,
                        created_by_agent_code=created_by_agent_code.strip(),
                        created_from_session_id=created_from_session_id.strip(),
                        created_at=now,
                        updated_at=now,
                    ))
                    edge_identities.add(identity)
                    result.created_relationships += 1
            return result, ""
    except IntegrityError:
        return None, "OSINT manifest conflicts with concurrently updated project records"
    except ValueError as exc:
        return None, str(exc)


def _plan_matches_project_scope(plan: OsintImportPlan, scope_assets: list[WorkProjectAsset]) -> bool:
    if plan.scope.type == WorkProjectAssetType.DOMAIN:
        domain = plan.scope.host
        return any(
            asset.type == WorkProjectAssetType.DOMAIN
            and (domain == asset.host or domain.endswith(f".{asset.host}"))
            for asset in scope_assets
        )

    if plan.scope.type == WorkProjectAssetType.URL:
        from urllib.parse import urlsplit

        requested = urlsplit(plan.scope.path)
        requested_host = (requested.hostname or "").lower().rstrip(".")
        for asset in scope_assets:
            if asset.type == WorkProjectAssetType.URL and _url_is_within_prefix(plan.scope.path, asset.path):
                return True
            if asset.type == WorkProjectAssetType.DOMAIN:
                scoped_host = asset.host.lower().rstrip(".")
                if requested_host == scoped_host or requested_host.endswith(f".{scoped_host}"):
                    return True
        return False
    return False


def _url_is_within_prefix(value: str, prefix: str) -> bool:
    from urllib.parse import unquote, urlsplit

    candidate = urlsplit(value)
    scope = urlsplit(prefix)
    return (
        not _has_dot_segments(candidate.path, unquote)
        and not _has_dot_segments(scope.path, unquote)
        and candidate.scheme == scope.scheme
        and candidate.hostname == scope.hostname
        and candidate.port == scope.port
        and candidate.path.startswith(scope.path)
        and (not scope.query or candidate.query.startswith(scope.query))
    )


def _has_dot_segments(path: str, unquote) -> bool:
    decoded = unquote(path)
    return "\\" in decoded or any(segment in (".", "..") for segment in decoded.split("/"))

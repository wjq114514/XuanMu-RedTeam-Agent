from fastapi import APIRouter, Depends, File, UploadFile

from handler.work_project.scan_report_imports import (
    commit_scan_report_import_handler,
    preview_scan_report_import_handler,
)
from middleware.auth import AuthUser, require_user
from router.common.responses import (
    BAD_REQUEST_RESPONSE,
    COMMON_ERROR_RESPONSES,
    CONFLICT_RESPONSE,
    RATE_LIMIT_RESPONSE,
    not_found_response,
)
from schema.common.responses import CommonResponse
from schema.work_project.scan_report_imports import (
    CommitScanReportImportRequest,
    CommitScanReportImportResponse,
    ScanReportImportPreviewResponse,
)


router = APIRouter(
    prefix="/work-projects/{project_id}/scan-report-imports",
    tags=["work-project-scan-report-imports"],
    dependencies=[Depends(require_user)],
)


async def preview_scan_report_import_route(
    project_id: int,
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_user),
) -> CommonResponse[ScanReportImportPreviewResponse]:
    return await preview_scan_report_import_handler(project_id, file, user)


async def commit_scan_report_import_route(
    project_id: int,
    import_id: str,
    request: CommitScanReportImportRequest,
    user: AuthUser = Depends(require_user),
) -> CommonResponse[CommitScanReportImportResponse]:
    return await commit_scan_report_import_handler(project_id, import_id, request, user)


router.add_api_route(
    "/preview",
    preview_scan_report_import_route,
    methods=["POST"],
    response_model=CommonResponse[ScanReportImportPreviewResponse],
    responses={
        **COMMON_ERROR_RESPONSES,
        **BAD_REQUEST_RESPONSE,
        **RATE_LIMIT_RESPONSE,
        **not_found_response("Work project"),
    },
)

router.add_api_route(
    "/{import_id}/commit",
    commit_scan_report_import_route,
    methods=["POST"],
    response_model=CommonResponse[CommitScanReportImportResponse],
    responses={
        **COMMON_ERROR_RESPONSES,
        **BAD_REQUEST_RESPONSE,
        **not_found_response("Work project or scan report import"),
        **CONFLICT_RESPONSE,
    },
)

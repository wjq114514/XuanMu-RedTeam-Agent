from http import HTTPStatus

from fastapi import UploadFile

from middleware.auth import AuthUser
from schema.common.responses import CommonResponse
from schema.work_project.scan_report_imports import CommitScanReportImportRequest
from service.work_project.projects import can_access_work_project
from service.work_project.scan_report_imports import (
    ScanReportImportBadRequest,
    ScanReportImportConflict,
    ScanReportImportNotFound,
    commit_scan_report_import,
    preview_scan_report_import,
)


async def preview_scan_report_import_handler(
    project_id: int,
    file: UploadFile,
    user: AuthUser,
) -> CommonResponse:
    if not await can_access_work_project(project_id, user.id, user.role):
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="work project not found")
    try:
        preview = await preview_scan_report_import(project_id, user.id, file)
    except ScanReportImportBadRequest as error:
        return CommonResponse(code=HTTPStatus.BAD_REQUEST.value, message=str(error))
    return CommonResponse(data=preview)


async def commit_scan_report_import_handler(
    project_id: int,
    import_id: str,
    request: CommitScanReportImportRequest,
    user: AuthUser,
) -> CommonResponse:
    if not await can_access_work_project(project_id, user.id, user.role):
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message="work project not found")
    try:
        result = await commit_scan_report_import(project_id, import_id, request)
    except ScanReportImportBadRequest as error:
        return CommonResponse(code=HTTPStatus.BAD_REQUEST.value, message=str(error))
    except ScanReportImportNotFound as error:
        return CommonResponse(code=HTTPStatus.NOT_FOUND.value, message=str(error))
    except ScanReportImportConflict as error:
        return CommonResponse(code=HTTPStatus.CONFLICT.value, message=str(error))
    return CommonResponse(data=result)

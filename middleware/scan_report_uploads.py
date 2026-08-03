import re
from http import HTTPStatus

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from middleware.auth import AuthUser
from schema.common.responses import CommonResponse
from service.work_project.scan_report_imports import (
    ScanReportImportRateLimited,
    _enforce_upload_rate_limit,
)


_PREVIEW_PATH = re.compile(r"^/api/work-projects/([1-9][0-9]*)/scan-report-imports/preview$")


class ScanReportUploadRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method != "POST":
            return await call_next(request)
        match = _PREVIEW_PATH.fullmatch(request.url.path)
        if match is None:
            return await call_next(request)
        user = getattr(request.state, "system_user", None)
        if not isinstance(user, AuthUser):
            return await call_next(request)

        try:
            await _enforce_upload_rate_limit(int(match.group(1)), user.id)
        except ScanReportImportRateLimited as error:
            status = HTTPStatus.TOO_MANY_REQUESTS.value
            return JSONResponse(
                status_code=status,
                content=CommonResponse(code=status, message=str(error)).model_dump(),
            )
        return await call_next(request)

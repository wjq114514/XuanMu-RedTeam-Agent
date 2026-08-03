import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile
from starlette.datastructures import Headers

from service.work_project.scan_report_imports import (
    UPLOAD_RATE_LIMIT_PER_PROJECT,
    UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
    ScanReportImportRateLimited,
    ScanReportImportBadRequest,
    _enforce_upload_rate_limit,
    _project_upload_attempts,
    _user_upload_attempts,
    preview_scan_report_import,
)


class ScanReportUploadRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _project_upload_attempts.clear()
        _user_upload_attempts.clear()

    async def test_limits_attempts_per_user_and_project(self) -> None:
        for offset in range(UPLOAD_RATE_LIMIT_PER_PROJECT):
            await _enforce_upload_rate_limit(7, 11, now=float(offset))

        with self.assertRaisesRegex(ScanReportImportRateLimited, "retry in"):
            await _enforce_upload_rate_limit(7, 11, now=10.0)

    async def test_window_expiry_allows_another_attempt(self) -> None:
        for offset in range(UPLOAD_RATE_LIMIT_PER_PROJECT):
            await _enforce_upload_rate_limit(7, 11, now=float(offset))

        await _enforce_upload_rate_limit(
            7,
            11,
            now=float(UPLOAD_RATE_LIMIT_WINDOW_SECONDS + 1),
        )

    async def test_invalid_report_is_removed(self) -> None:
        upload = UploadFile(
            file=io.BytesIO(b"<nmaprun><host>"),
            filename="broken.xml",
            headers=Headers({"content-type": "application/xml"}),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "service.work_project.scan_report_imports.WORKSPACE",
                Path(temporary_directory),
            ):
                with self.assertRaisesRegex(ScanReportImportBadRequest, "incomplete"):
                    await preview_scan_report_import(7, 11, upload)

            retained_files = [path for path in Path(temporary_directory).rglob("*") if path.is_file()]
            self.assertEqual(retained_files, [])


if __name__ == "__main__":
    unittest.main()

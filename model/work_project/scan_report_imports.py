from datetime import datetime

from sqlalchemy import CheckConstraint, Column, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from schema.work_project.scan_report_imports import ScanReportImportFormat, ScanReportImportStatus


class ScanReportImport(SQLModel, table=True):
    __tablename__ = "scan_report_imports"
    __table_args__ = (
        CheckConstraint("format = 'nmap_xml'", name="ck_scan_report_import_format"),
        CheckConstraint(
            "status IN ('previewed', 'committed', 'failed')",
            name="ck_scan_report_import_status",
        ),
    )

    import_id: str = Field(sa_column=Column(String(36), primary_key=True))
    project_id: int = Field(foreign_key="work_projects.id", index=True, ondelete="CASCADE")
    uploader_user_id: int = Field(foreign_key="system_users.id", index=True)
    filename: str = Field(sa_column=Column(String(255), nullable=False))
    media_type: str = Field(sa_column=Column(String(255), nullable=False))
    sha256: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    format: ScanReportImportFormat = Field(
        default=ScanReportImportFormat.NMAP_XML,
        sa_column=Column(String(32), nullable=False),
    )
    status: ScanReportImportStatus = Field(sa_column=Column(String(32), nullable=False, index=True))
    size_bytes: int
    original_file: str = Field(sa_column=Column(String(1000), nullable=False))
    normalized_file: str = Field(sa_column=Column(String(1000), nullable=False))
    summary: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    error: str = Field(default="", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    committed_at: datetime | None = None

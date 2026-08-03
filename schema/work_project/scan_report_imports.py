from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from schema.work_project.assets import WorkProjectAssetExtraSchema, WorkProjectAssetOrigin, WorkProjectAssetType
from schema.work_project.graph import WorkProjectGraphEdgeType


class ScanReportImportFormat(StrEnum):
    NMAP_XML = "nmap_xml"


class ScanReportImportStatus(StrEnum):
    PREVIEWED = "previewed"
    COMMITTED = "committed"
    FAILED = "failed"


class ScanReportAssetCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    type: WorkProjectAssetType
    identifier: str
    host: str
    port: int | None = None
    path: str = ""
    origin: WorkProjectAssetOrigin = WorkProjectAssetOrigin.DISCOVERED
    extra: WorkProjectAssetExtraSchema = Field(default_factory=WorkProjectAssetExtraSchema)


class ScanReportRelationshipCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    source_asset_key: str
    target_asset_key: str
    type: WorkProjectGraphEdgeType


class ScanReportImportInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    import_id: str
    project_id: int
    uploader_user_id: int
    filename: str
    media_type: str
    sha256: str
    format: ScanReportImportFormat
    status: ScanReportImportStatus
    size_bytes: int
    summary: dict = Field(default_factory=dict)
    error: str = ""
    created_at: datetime
    updated_at: datetime
    committed_at: datetime | None = None


class ScanReportImportCounts(BaseModel):
    hosts: int = 0
    assets: int = 0
    relationships: int = 0
    networks: int = 0
    domains: int = 0
    services: int = 0


class ScanReportImportPreviewResponse(BaseModel):
    import_info: ScanReportImportInfo
    assets: list[ScanReportAssetCandidate]
    relationships: list[ScanReportRelationshipCandidate]
    warnings: list[str]
    duplicate_keys: list[str]
    counts: ScanReportImportCounts


class CommitScanReportImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_asset_keys: list[str] | None = Field(default=None, max_length=110000)
    create_relationships: bool = True
    create_blackboard_fact: bool = True


class CommitScanReportImportResponse(BaseModel):
    import_id: str
    created_assets: list[str]
    updated_assets: list[str]
    skipped_assets: list[str]
    created_relationships: list[str]
    skipped_relationships: list[str]
    blackboard_node_id: int | None = None

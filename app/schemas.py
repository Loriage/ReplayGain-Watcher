"""Small public response models for the monitoring API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class APIError(BaseModel):
    code: str
    message: str


class Page(BaseModel):
    items: list[Any]
    page: int
    page_size: int
    total: int


class StatusResponse(BaseModel):
    service: str
    application_version: str
    rsgain_available: bool
    rsgain_version: str
    database_available: bool
    ready: bool
    libraries: int
    queue_depth: int
    running_job: dict[str, Any] | None = None


class LibraryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    path: str
    enabled: bool
    available: bool
    album_count: int
    processed_count: int
    waiting_count: int
    failed_count: int
    last_reconciliation_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None


class AlbumResponse(BaseModel):
    id: int
    library_id: int
    library_name: str
    relative_path: str
    state: str
    file_count: int
    total_size: int
    discovered_at: datetime
    last_seen_at: datetime
    stable_since: datetime | None
    processed_at: datetime | None
    source_fingerprint: str | None
    processed_source_fingerprint: str | None
    config_fingerprint: str | None
    processed_config_fingerprint: str | None
    last_job_id: int | None
    last_error: str | None


class JobResponse(BaseModel):
    id: int
    library_id: int
    library_name: str
    album_id: int
    album_path: str
    kind: str
    status: str
    reason: str
    priority: int
    queued_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    command_version: str | None
    source_fingerprint: str | None
    config_fingerprint: str | None
    stdout_tail: str
    stderr_tail: str
    error_message: str | None
    verification_result: str | None


class JobLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    timestamp: datetime
    stream: str
    level: str
    message: str

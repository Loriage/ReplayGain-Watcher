"""Versioned read-only API plus opt-in administrative actions."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from datetime import date, datetime
from datetime import time as datetime_time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import func, select

from app.models import Album, Job, JobLog, Library, utcnow
from app.schemas import AlbumResponse, JobLogResponse, JobResponse, LibraryResponse, Page

router = APIRouter(prefix="/api/v1")


def _container(request: Request):
    return request.app.state.container


def _page(page: int, page_size: int) -> tuple[int, int]:
    return (page - 1) * page_size, page_size


def _safe_error(value: str | None, library: Library, container) -> str | None:
    if value is None or not container.settings.redact_host_paths:
        return value
    return value.replace(library.path, f"/libraries/{library.name}")


def _album_payload(album: Album, library: Library, container) -> AlbumResponse:
    return AlbumResponse(
        id=album.id,
        library_id=album.library_id,
        library_name=library.name,
        relative_path=album.relative_path,
        state=album.state,
        file_count=album.file_count,
        total_size=album.total_size,
        discovered_at=album.discovered_at,
        last_seen_at=album.last_seen_at,
        stable_since=album.stable_since,
        processed_at=album.processed_at,
        source_fingerprint=album.source_fingerprint,
        processed_source_fingerprint=album.processed_source_fingerprint,
        config_fingerprint=album.config_fingerprint,
        processed_config_fingerprint=album.processed_config_fingerprint,
        last_job_id=album.last_job_id,
        last_error=_safe_error(album.last_error, library, container),
    )


def _job_payload(job: Job, library: Library, album: Album, container) -> JobResponse:
    return JobResponse(
        id=job.id,
        library_id=job.library_id,
        library_name=library.name,
        album_id=job.album_id,
        album_path=album.relative_path,
        kind=job.kind,
        status=job.status,
        reason=job.reason,
        priority=job.priority,
        queued_at=job.queued_at,
        started_at=job.started_at,
        heartbeat_at=job.heartbeat_at,
        finished_at=job.finished_at,
        exit_code=job.exit_code,
        command_version=job.command_version,
        source_fingerprint=job.source_fingerprint,
        config_fingerprint=job.config_fingerprint,
        stdout_tail=_safe_error(job.stdout_tail, library, container) or "",
        stderr_tail=_safe_error(job.stderr_tail, library, container) or "",
        error_message=_safe_error(job.error_message, library, container),
        verification_result=job.verification_result,
    )


@router.get("/libraries", response_model=Page)
async def list_libraries(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page:
    container = _container(request)
    async with container.session_factory() as session:
        total = await session.scalar(select(func.count(Library.id))) or 0
        libraries = (
            await session.scalars(
                select(Library)
                .order_by(Library.name)
                .offset(_page(page, page_size)[0])
                .limit(page_size)
            )
        ).all()
        items: list[LibraryResponse] = []
        for library in libraries:
            counts = dict(
                (state, count)
                for state, count in (
                    await session.execute(
                        select(Album.state, func.count(Album.id))
                        .where(Album.library_id == library.id)
                        .group_by(Album.state)
                    )
                ).all()
            )
            items.append(
                LibraryResponse(
                    id=library.id,
                    name=library.name,
                    path=library.path
                    if not container.settings.redact_host_paths
                    else f"/libraries/{library.name}",
                    enabled=library.enabled,
                    available=not bool(library.last_error),
                    album_count=sum(counts.values()),
                    processed_count=counts.get("processed", 0),
                    waiting_count=sum(
                        counts.get(state, 0)
                        for state in (
                            "discovered",
                            "waiting_for_stability",
                            "queued",
                            "processing",
                            "changed",
                        )
                    ),
                    failed_count=counts.get("failed", 0),
                    last_reconciliation_at=library.last_reconciliation_at,
                    last_success_at=library.last_success_at,
                    last_error=_safe_error(library.last_error, library, container),
                )
            )
        return Page(items=items, page=page, page_size=page_size, total=int(total))


@router.get("/libraries/{library_id}", response_model=LibraryResponse)
async def get_library(library_id: int, request: Request) -> LibraryResponse:
    container = _container(request)
    async with container.session_factory() as session:
        library = await session.get(Library, library_id)
        if library is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "library not found"})
        counts = dict(
            (state, count)
            for state, count in (
                await session.execute(
                    select(Album.state, func.count(Album.id))
                    .where(Album.library_id == library.id)
                    .group_by(Album.state)
                )
            ).all()
        )
        return LibraryResponse(
            id=library.id,
            name=library.name,
            path=library.path
            if not container.settings.redact_host_paths
            else f"/libraries/{library.name}",
            enabled=library.enabled,
            available=not bool(library.last_error),
            album_count=sum(counts.values()),
            processed_count=counts.get("processed", 0),
            waiting_count=sum(
                counts.get(state, 0)
                for state in (
                    "discovered",
                    "waiting_for_stability",
                    "queued",
                    "processing",
                    "changed",
                )
            ),
            failed_count=counts.get("failed", 0),
            last_reconciliation_at=library.last_reconciliation_at,
            last_success_at=library.last_success_at,
            last_error=_safe_error(library.last_error, library, container),
        )


@router.get("/albums", response_model=Page)
async def list_albums(
    request: Request,
    library_id: int | None = Query(default=None, ge=1),
    state: str | None = Query(default=None, min_length=1, max_length=32),
    path: str | None = Query(default=None, max_length=4096),
    processed_on: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page:
    container = _container(request)
    allowed_states = {
        "discovered",
        "waiting_for_stability",
        "queued",
        "processing",
        "processed",
        "changed",
        "failed",
        "ignored",
        "missing",
    }
    if state and state not in allowed_states:
        raise HTTPException(
            422, detail={"code": "invalid_filter", "message": "unknown album state"}
        )
    async with container.session_factory() as session:
        query = select(Album, Library).join(Library, Library.id == Album.library_id)
        count_query = select(func.count(Album.id))
        filters = []
        if library_id is not None:
            filters.append(Album.library_id == library_id)
        if state:
            filters.append(Album.state == state)
        if path:
            filters.append(Album.relative_path.ilike(f"%{path}%"))
        if processed_on:
            start = datetime.combine(processed_on, datetime_time.min)
            end = datetime.combine(processed_on, datetime_time.max)
            filters.extend([Album.processed_at >= start, Album.processed_at <= end])
        query = (
            query.where(*filters)
            .order_by(Album.last_seen_at.desc())
            .offset(_page(page, page_size)[0])
            .limit(page_size)
        )
        total = await session.scalar(count_query.where(*filters)) or 0
        rows = (await session.execute(query)).all()
        return Page(
            items=[_album_payload(album, library, container) for album, library in rows],
            page=page,
            page_size=page_size,
            total=int(total),
        )


@router.get("/albums/{album_id}", response_model=AlbumResponse)
async def get_album(album_id: int, request: Request) -> AlbumResponse:
    container = _container(request)
    async with container.session_factory() as session:
        row = (
            await session.execute(
                select(Album, Library)
                .join(Library, Library.id == Album.library_id)
                .where(Album.id == album_id)
            )
        ).first()
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "album not found"})
        return _album_payload(row[0], row[1], container)


@router.get("/jobs", response_model=Page)
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None, max_length=32),
    library_id: int | None = Query(default=None, ge=1),
    reason: str | None = Query(default=None, max_length=128),
    album_path: str | None = Query(default=None, max_length=4096),
    on: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page:
    container = _container(request)
    allowed_statuses = {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}
    if status and status not in allowed_statuses:
        raise HTTPException(422, detail={"code": "invalid_filter", "message": "unknown job status"})
    async with container.session_factory() as session:
        filters = []
        if status:
            filters.append(Job.status == status)
        if library_id is not None:
            filters.append(Job.library_id == library_id)
        if reason:
            filters.append(Job.reason == reason)
        if album_path:
            filters.append(Album.relative_path.ilike(f"%{album_path}%"))
        if on:
            start = datetime.combine(on, datetime_time.min)
            end = datetime.combine(on, datetime_time.max)
            filters.append(Job.queued_at.between(start, end))
        count_query = (
            select(func.count(Job.id)).join(Album, Album.id == Job.album_id).where(*filters)
        )
        total = await session.scalar(count_query) or 0
        rows = (
            await session.execute(
                select(Job, Library, Album)
                .join(Library, Library.id == Job.library_id)
                .join(Album, Album.id == Job.album_id)
                .where(*filters)
                .order_by(Job.queued_at.desc())
                .offset(_page(page, page_size)[0])
                .limit(page_size)
            )
        ).all()
        return Page(
            items=[_job_payload(job, library, album, container) for job, library, album in rows],
            page=page,
            page_size=page_size,
            total=int(total),
        )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, request: Request) -> JobResponse:
    container = _container(request)
    async with container.session_factory() as session:
        row = (
            await session.execute(
                select(Job, Library, Album)
                .join(Library, Library.id == Job.library_id)
                .join(Album, Album.id == Job.album_id)
                .where(Job.id == job_id)
            )
        ).first()
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "job not found"})
        return _job_payload(row[0], row[1], row[2], container)


@router.get("/jobs/{job_id}/logs", response_model=Page)
async def get_job_logs(
    job_id: int,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 100,
) -> Page:
    container = _container(request)
    async with container.session_factory() as session:
        exists = await session.scalar(select(Job.id).where(Job.id == job_id))
        if exists is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "job not found"})
        total = (
            await session.scalar(select(func.count(JobLog.id)).where(JobLog.job_id == job_id)) or 0
        )
        logs = (
            await session.scalars(
                select(JobLog)
                .where(JobLog.job_id == job_id)
                .order_by(JobLog.timestamp.asc(), JobLog.id.asc())
                .offset(_page(page, page_size)[0])
                .limit(page_size)
            )
        ).all()
        return Page(
            items=[JobLogResponse.model_validate(item) for item in logs],
            page=page,
            page_size=page_size,
            total=int(total),
        )


def _csrf_check(request: Request) -> None:
    cookie = request.cookies.get("rgw_csrf")
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(403, detail={"code": "csrf_failed", "message": "CSRF token required"})


def _admin_guard(request: Request) -> None:
    container = _container(request)
    if not container.settings.ui_actions_enabled:
        raise HTTPException(
            404, detail={"code": "disabled", "message": "administrative actions are disabled"}
        )
    _csrf_check(request)
    limiter = getattr(request.app.state, "action_limiter", None)
    if limiter is not None and not limiter.allow(
        request.client.host if request.client else "unknown"
    ):
        raise HTTPException(
            429, detail={"code": "rate_limited", "message": "too many administrative actions"}
        )


@router.post("/reconciliation", status_code=202)
async def run_reconciliation(request: Request) -> dict[str, str]:
    _admin_guard(request)
    container = _container(request)
    if container.scheduler is None:
        raise HTTPException(
            503, detail={"code": "not_ready", "message": "scheduler is unavailable"}
        )
    await container.scheduler.trigger()
    return {"status": "queued"}


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: int, request: Request) -> dict[str, str]:
    _admin_guard(request)
    if not await _container(request).runner.retry_job(job_id):
        raise HTTPException(
            409, detail={"code": "cannot_retry", "message": "job cannot be retried"}
        )
    return {"status": "queued"}


@router.post("/albums/{album_id}/requeue", status_code=202)
async def requeue_album(album_id: int, request: Request) -> dict[str, str]:
    _admin_guard(request)
    container = _container(request)
    async with container.session_factory() as session:
        album = await session.get(Album, album_id)
        if album is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "album not found"})
        active = await session.scalar(
            select(Job.id).where(Job.album_id == album.id, Job.status.in_(["queued", "running"]))
        )
        if active is not None:
            raise HTTPException(
                409, detail={"code": "already_active", "message": "album already has an active job"}
            )
        job = Job(
            library_id=album.library_id,
            album_id=album.id,
            kind="reanalyze",
            status="queued",
            reason="manual_requeue",
            queued_at=utcnow(),
            source_fingerprint=album.source_fingerprint,
            config_fingerprint=album.config_fingerprint,
        )
        session.add(job)
        album.state = "queued"
        await session.flush()
        album.last_job_id = job.id
        await session.commit()
    return {"status": "queued"}


@router.delete("/jobs/{job_id}", status_code=204)
async def cancel_job(job_id: int, request: Request) -> Response:
    _admin_guard(request)
    if not await _container(request).runner.cancel_job(job_id):
        raise HTTPException(
            409, detail={"code": "cannot_cancel", "message": "only queued jobs can be cancelled"}
        )
    return Response(status_code=204)


class ActionRateLimiter:
    def __init__(self, limit: int = 30, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        events = self._events[key]
        while events and events[0] < now - self.window_seconds:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True

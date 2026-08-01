"""Liveness, readiness, and Prometheus endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST

from app.schemas import StatusResponse

router = APIRouter()


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(request: Request) -> Response:
    container = request.app.state.container
    payload = {
        "status": "ok" if container.ready else "not_ready",
        "database_available": container.database_available,
        "rsgain_available": container.rsgain.available,
        "rsgain_version": container.rsgain.version,
        "error": container.initialization_error,
    }
    return JSONResponse(payload, status_code=200 if container.ready else 503)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    container = request.app.state.container
    return Response(content=container.metrics.render(), media_type=CONTENT_TYPE_LATEST)


@router.get("/api/v1/status", response_model=StatusResponse)
async def status(request: Request) -> StatusResponse:
    container = request.app.state.container
    session = container.session_factory()
    try:
        from sqlalchemy import func, select

        from app.models import Job, Library

        library_count = await session.scalar(select(func.count(Library.id))) or 0
        queue_depth = (
            await session.scalar(select(func.count(Job.id)).where(Job.status == "queued")) or 0
        )
        running = await session.scalar(
            select(Job).where(Job.status == "running").order_by(Job.started_at.asc())
        )
        running_payload = None
        if running is not None:
            running_payload = {
                "id": running.id,
                "album_id": running.album_id,
                "status": running.status,
            }
        return StatusResponse(
            service=container.settings.app_name,
            application_version=container.settings.app_version,
            rsgain_available=container.rsgain.available,
            rsgain_version=container.rsgain.version,
            database_available=container.database_available,
            ready=container.ready,
            libraries=int(library_count),
            queue_depth=int(queue_depth),
            running_job=running_payload,
        )
    finally:
        await session.close()

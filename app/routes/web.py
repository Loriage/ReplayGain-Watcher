"""Server-rendered monitoring pages."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.models import Album, Job, Library

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _safe_error(value, library, container):
    if value is None or not container.settings.redact_host_paths:
        return value
    return value.replace(library.path, f"/libraries/{library.name}")


templates.env.filters["safe_error"] = _safe_error

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _clean_output(value: str | None) -> str:
    return _ANSI_ESCAPE.sub("", value or "").replace("\r", "")


templates.env.filters["clean_output"] = _clean_output

_STATE_LABELS = {
    "waiting_for_stability": "Waiting for import",
    "queued": "Queued",
    "processing": "Processing",
    "processed": "Processed",
    "changed": "Changed",
    "failed": "Failed",
    "missing": "Missing",
    "discovered": "Discovered",
    "ignored": "Ignored",
    "succeeded": "Succeeded",
    "cancelled": "Cancelled",
    "interrupted": "Interrupted",
}


def _label(value: str | None) -> str:
    if not value:
        return "—"
    return _STATE_LABELS.get(value, value.replace("_", " ").capitalize())


templates.env.filters["label"] = _label


async def _dashboard_data(container):
    async with container.session_factory() as session:
        library_count = await session.scalar(select(func.count(Library.id))) or 0
        album_counts = dict(
            (state, count)
            for state, count in (
                await session.execute(
                    select(Album.state, func.count(Album.id)).group_by(Album.state)
                )
            ).all()
        )
        queue_depth = (
            await session.scalar(select(func.count(Job.id)).where(Job.status == "queued")) or 0
        )
        running = (
            await session.scalars(
                select(Job).where(Job.status == "running").order_by(Job.started_at.asc()).limit(1)
            )
        ).first()
        recent_jobs = (
            await session.execute(
                select(Job, Library, Album)
                .join(Library, Library.id == Job.library_id)
                .join(Album, Album.id == Job.album_id)
                .order_by(Job.queued_at.desc())
                .limit(8)
            )
        ).all()
        return {
            "library_count": int(library_count),
            "album_counts": album_counts,
            "queue_depth": int(queue_depth),
            "running": running,
            "recent_jobs": recent_jobs,
        }


@router.get("/")
async def dashboard(request: Request):
    container = request.app.state.container
    data = await _dashboard_data(container)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"request": request, "container": container, **data, "title": "Dashboard"},
    )


@router.get("/partials/summary")
async def summary_partial(request: Request):
    container = request.app.state.container
    data = await _dashboard_data(container)
    return templates.TemplateResponse(
        request=request,
        name="partials/summary.html",
        context={"request": request, "container": container, **data},
    )


@router.get("/libraries")
async def libraries_page(request: Request):
    container = request.app.state.container
    async with container.session_factory() as session:
        libraries = (await session.scalars(select(Library).order_by(Library.name))).all()
        rows = []
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
            rows.append((library, counts))
    return templates.TemplateResponse(
        request=request,
        name="libraries.html",
        context={"request": request, "container": container, "rows": rows, "title": "Libraries"},
    )


@router.get("/albums")
async def albums_page(
    request: Request,
    library_id: int | None = Query(default=None, ge=1),
    state: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
):
    container = request.app.state.container
    page_size = 50
    filters = []
    if library_id:
        filters.append(Album.library_id == library_id)
    if state:
        filters.append(Album.state == state)
    async with container.session_factory() as session:
        libraries = (await session.scalars(select(Library).order_by(Library.name))).all()
        total = await session.scalar(select(func.count(Album.id)).where(*filters)) or 0
        rows = (
            await session.execute(
                select(Album, Library)
                .join(Library, Library.id == Album.library_id)
                .where(*filters)
                .order_by(Album.last_seen_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    return templates.TemplateResponse(
        request=request,
        name="albums.html",
        context={
            "request": request,
            "container": container,
            "rows": rows,
            "libraries": libraries,
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "selected_library": library_id,
            "selected_state": state,
            "title": "Folders",
        },
    )


@router.get("/jobs")
async def jobs_page(
    request: Request,
    status: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
):
    container = request.app.state.container
    page_size = 50
    filters = [Job.status == status] if status else []
    async with container.session_factory() as session:
        total = await session.scalar(select(func.count(Job.id)).where(*filters)) or 0
        rows = (
            await session.execute(
                select(Job, Library, Album)
                .join(Library, Library.id == Job.library_id)
                .join(Album, Album.id == Job.album_id)
                .where(*filters)
                .order_by(Job.queued_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "request": request,
            "container": container,
            "rows": rows,
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "selected_status": status,
            "title": "Jobs",
        },
    )


@router.get("/jobs/{job_id}")
async def job_detail(job_id: int, request: Request):
    container = request.app.state.container
    async with container.session_factory() as session:
        row = (
            await session.execute(
                select(Job, Library, Album)
                .join(Library, Library.id == Job.library_id)
                .join(Album, Album.id == Job.album_id)
                .where(Job.id == job_id)
            )
        ).first()
    return templates.TemplateResponse(
        request=request,
        name="job_detail.html",
        context={
            "request": request,
            "container": container,
            "row": row,
            "title": f"Job {job_id}",
        },
    )

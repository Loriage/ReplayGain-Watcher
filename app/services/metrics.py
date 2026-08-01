"""Prometheus metrics with bounded label cardinality."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Album, Job, Library


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.up = Gauge(
            "replaygain_watcher_up", "Whether the application is serving", registry=self.registry
        )
        self.library_available = Gauge(
            "replaygain_watcher_library_available",
            "Whether a configured library is available",
            ["library"],
            registry=self.registry,
        )
        self.albums_total = Gauge(
            "replaygain_watcher_albums_total",
            "Indexed albums by library and state",
            ["library", "state"],
            registry=self.registry,
        )
        self.jobs_total = Gauge(
            "replaygain_watcher_jobs_total",
            "Jobs by library and status",
            ["library", "status"],
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "replaygain_watcher_queue_depth", "Number of queued jobs", registry=self.registry
        )
        self.last_success = Gauge(
            "replaygain_watcher_last_success_timestamp",
            "Unix timestamp of the last successful job",
            ["library"],
            registry=self.registry,
        )
        self.last_reconciliation = Gauge(
            "replaygain_watcher_last_reconciliation_timestamp",
            "Unix timestamp of the last reconciliation",
            ["library"],
            registry=self.registry,
        )
        self.job_duration = Histogram(
            "replaygain_watcher_job_duration_seconds",
            "Observed job durations",
            registry=self.registry,
        )
        self.reconciliation_duration = Histogram(
            "replaygain_watcher_reconciliation_duration_seconds",
            "Observed reconciliation durations",
            registry=self.registry,
        )
        self.rsgain_failures = Counter(
            "replaygain_watcher_rsgain_failures_total",
            "rsgain execution or verification failures",
            registry=self.registry,
        )
        self.up.set(1)

    async def refresh(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            libraries = (await session.scalars(select(Library))).all()
            for library in libraries:
                self.library_available.labels(library=library.name).set(
                    0 if library.last_error else 1
                )
                last_success = library.last_success_at.timestamp() if library.last_success_at else 0
                last_scan = (
                    library.last_reconciliation_at.timestamp()
                    if library.last_reconciliation_at
                    else 0
                )
                self.last_success.labels(library=library.name).set(last_success)
                self.last_reconciliation.labels(library=library.name).set(last_scan)
            for library_id, state, count in (
                await session.execute(
                    select(Album.library_id, Album.state, func.count(Album.id)).group_by(
                        Album.library_id, Album.state
                    )
                )
            ).all():
                library = next((item for item in libraries if item.id == library_id), None)
                if library:
                    self.albums_total.labels(library=library.name, state=state).set(count)
            for library_id, status, count in (
                await session.execute(
                    select(Job.library_id, Job.status, func.count(Job.id)).group_by(
                        Job.library_id, Job.status
                    )
                )
            ).all():
                library = next((item for item in libraries if item.id == library_id), None)
                if library:
                    self.jobs_total.labels(library=library.name, status=status).set(count)
            queued = await session.scalar(select(func.count(Job.id)).where(Job.status == "queued"))
            self.queue_depth.set(queued or 0)

    def render(self) -> bytes:
        return generate_latest(self.registry)

"""Application dependency container."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.metrics import Metrics
from app.services.reconciliation import Reconciler
from app.services.runner import JobRunner, ToolStatus
from app.services.scheduler import Scheduler


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    rsgain: ToolStatus
    reconciler: Reconciler
    runner: JobRunner
    metrics: Metrics
    scheduler: Scheduler | None
    database_available: bool
    initialization_error: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.database_available and self.rsgain.available and self.initialization_error is None
        )

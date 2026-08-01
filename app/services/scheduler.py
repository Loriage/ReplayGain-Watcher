"""Lifecycle-managed reconciliation scheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import utcnow
from app.services.metrics import Metrics
from app.services.reconciliation import Reconciler
from app.services.runner import JobRunner

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        reconciler: Reconciler,
        runner: JobRunner,
        metrics: Metrics,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.reconciler = reconciler
        self.runner = runner
        self.metrics = metrics
        self._task: asyncio.Task[None] | None = None
        self._trigger = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        await self.runner.recover_jobs()
        await self.reconciler.reconcile_all()
        await self.metrics.refresh(self.session_factory)
        await self.runner.start()
        self._task = asyncio.create_task(self._loop(), name="replaygain-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        self._trigger.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.runner.stop()

    async def trigger(self) -> None:
        self._trigger.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(
                    self._trigger.wait(), timeout=self.settings.reconciliation_interval_seconds
                )
            except TimeoutError:
                pass
            self._trigger.clear()
            try:
                results = await self.reconciler.reconcile_all()
                for result in results:
                    self.metrics.reconciliation_duration.observe(result.duration_seconds)
                await self.metrics.refresh(self.session_factory)
                await self._purge_old_logs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("periodic reconciliation failed")

    async def _purge_old_logs(self) -> None:
        from sqlalchemy import delete

        from app.models import JobLog

        cutoff = utcnow() - timedelta(days=self.settings.log_retention_days)
        async with self.session_factory() as session:
            await session.execute(delete(JobLog).where(JobLog.timestamp < cutoff))
            await session.commit()

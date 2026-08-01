from __future__ import annotations

from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import create_database_engine, create_schema_for_tests


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'watcher.db'}",
        config_file=None,
        settle_seconds=0,
        reconciliation_interval_seconds=10,
        worker_concurrency=1,
        ui_actions_enabled=True,
    )
    engine = create_database_engine(settings)
    await create_schema_for_tests(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield settings, engine, session_factory
    finally:
        await engine.dispose()

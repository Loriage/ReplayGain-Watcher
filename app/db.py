"""Async SQLAlchemy setup and SQLite hardening."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from alembic.config import Config
from app.config import Settings
from app.models import Base


def _sync_database_url(database_url: str) -> str:
    return database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def create_database_engine(settings: Settings) -> AsyncEngine:
    connect_args = {"check_same_thread": False}
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        connect_args=connect_args if settings.database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )

    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite") or ":memory:" in database_url:
        return
    path_part = database_url.rsplit("///", 1)[-1]
    if path_part.startswith("/"):
        Path(path_part).parent.mkdir(parents=True, exist_ok=True)


def _run_alembic_sync(database_url: str) -> None:
    _ensure_sqlite_parent(database_url)
    config = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", _sync_database_url(database_url))
    command.upgrade(config, "head")


async def run_migrations(settings: Settings) -> None:
    """Run the checked-in Alembic migrations before serving traffic."""

    if ":memory:" in settings.database_url:
        return
    await asyncio.to_thread(_run_alembic_sync, settings.database_url)


async def create_schema_for_tests(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_database(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

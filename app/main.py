"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.config import Settings, get_settings
from app.db import check_database, create_database_engine, run_migrations
from app.routes import api, health, web
from app.routes.api import ActionRateLimiter
from app.services.metrics import Metrics
from app.services.reconciliation import Reconciler
from app.services.runner import JobRunner, detect_rsgain
from app.services.scheduler import Scheduler
from app.state import AppContainer


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            },
            ensure_ascii=False,
        )


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if "rgw_csrf" not in request.cookies:
            response.set_cookie(
                "rgw_csrf",
                secrets.token_urlsafe(32),
                httponly=False,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    engine = create_database_engine(settings)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    rsgain_status = await detect_rsgain(settings.rsgain_binary)
    metrics = Metrics()
    reconciler = Reconciler(session_factory, settings, rsgain_status.version)
    runner = JobRunner(session_factory, settings, rsgain_status.version, metrics=metrics)
    scheduler: Scheduler | None = None
    initialization_error: str | None = None
    database_available = False
    try:
        await run_migrations(settings)
        database_available = await check_database(engine)
        await reconciler.sync_configured_libraries()
    except Exception as exc:
        initialization_error = str(exc)
        logging.getLogger(__name__).exception("application initialization failed")

    if database_available and rsgain_status.available and initialization_error is None:
        scheduler = Scheduler(session_factory, settings, reconciler, runner, metrics)
        try:
            await scheduler.start()
        except Exception as exc:
            initialization_error = str(exc)
            logging.getLogger(__name__).exception("scheduler initialization failed")
            scheduler = None
    app.state.container = AppContainer(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        rsgain=rsgain_status,
        reconciler=reconciler,
        runner=runner,
        metrics=metrics,
        scheduler=scheduler,
        database_available=database_available,
        initialization_error=initialization_error,
    )
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.container = None
    app.state.action_limiter = ActionRateLimiter()
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(health.router)
    app.include_router(api.router)
    app.include_router(web.router)
    return app


app = create_app()

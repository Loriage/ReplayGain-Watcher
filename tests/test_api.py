from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.models import Library


@pytest.mark.asyncio
async def test_health_live_and_unavailable_readiness(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        config_file=None,
        rsgain_binary="definitely-not-installed-rsgain",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            assert live.status_code == 200
            assert ready.status_code == 503
            assert ready.json()["rsgain_available"] is False


@pytest.mark.asyncio
async def test_ready_app_serves_dashboard_and_read_only_api(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ready.db'}",
        config_file=None,
        rsgain_binary="/bin/echo",
        reconciliation_interval_seconds=10,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            dashboard = await client.get("/")
            status = await client.get("/api/v1/status")
            assert dashboard.status_code == 200
            assert "ReplayGain Watcher" in dashboard.text
            assert "Scan all libraries" in dashboard.text
            assert "Recent lines" not in dashboard.text
            assert status.status_code == 200
            assert status.json()["ready"] is True


@pytest.mark.asyncio
async def test_library_scan_action_is_csrf_protected_and_queued(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}",
        config_file=None,
        rsgain_binary="/bin/echo",
        ui_actions_enabled=True,
    )
    library_path = tmp_path / "music"
    library_path.mkdir()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with app.state.container.session_factory() as session:
            library = Library(
                name="music",
                path=str(library_path),
                enabled=True,
                scan_interval_seconds=10,
                settle_seconds=0,
                include_extensions=[".flac"],
                exclude_patterns=[],
            )
            session.add(library)
            await session.commit()
            library_id = library.id

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/libraries")
            token = client.cookies.get("rgw_csrf")
            rejected = await client.post(f"/api/v1/libraries/{library_id}/scan")
            accepted = await client.post(
                f"/api/v1/libraries/{library_id}/scan",
                headers={"x-csrf-token": token},
            )

            assert rejected.status_code == 403
            assert accepted.status_code == 202
            assert accepted.json() == {"status": "queued", "library_id": library_id}


@pytest.mark.asyncio
async def test_empty_monitoring_pages_and_lists_are_valid(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}",
        config_file=None,
        rsgain_binary="/bin/echo",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            paths = [
                "/libraries",
                "/albums",
                "/albums?library_id=&state=",
                "/jobs",
                "/api/v1/libraries",
                "/api/v1/albums",
                "/api/v1/albums?library_id=",
                "/api/v1/jobs",
            ]
            responses = [await client.get(path) for path in paths]
            assert all(response.status_code == 200 for response in responses)
            assert "Folders" in responses[1].text
            assert responses[2].status_code == 200
            assert responses[-1].json()["items"] == []

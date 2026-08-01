from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Album, Job, JobLog, Library, utcnow
from app.services.runner import JobRunner, detect_rsgain
from app.services.verification import VerificationResult


class PassingVerifier:
    def verify(self, files, album_gain_enabled):
        return VerificationResult(ok=True, checked_files=len(files))


@pytest.mark.asyncio
async def test_detect_rsgain_returns_a_readable_version(tmp_path: Path):
    script = tmp_path / "fake-rsgain-version"
    script.write_text("#!/bin/sh\nprintf '\\033[1;32mrsgain\\033[0m 3.6 - using:\\n'\n")
    script.chmod(0o755)

    status = await detect_rsgain(str(script))

    assert status.available is True
    assert status.version == "rsgain 3.6"


async def _fixture_job(session_factory, root: Path, binary: Path) -> tuple[int, object]:
    async with session_factory() as session:
        library = Library(
            name="runner-library",
            path=str(root),
            enabled=True,
            scan_interval_seconds=10,
            settle_seconds=0,
            include_extensions=[".flac"],
            exclude_patterns=[],
        )
        album_path = root / "Album"
        album_path.mkdir(parents=True, exist_ok=True)
        (album_path / "01.flac").write_bytes(b"fake audio")
        album = Album(
            library=library,
            relative_path="Album",
            state="queued",
            file_count=1,
            total_size=10,
            source_fingerprint="a",
            config_fingerprint="b",
        )
        session.add(album)
        await session.flush()
        job = Job(
            library=library,
            album=album,
            status="queued",
            reason="test",
            queued_at=utcnow(),
            source_fingerprint="a",
            config_fingerprint="b",
        )
        session.add(job)
        await session.commit()
        return job.id, library


@pytest.mark.asyncio
async def test_runner_streams_output_and_marks_verified_job_success(db, tmp_path: Path):
    settings, _engine, session_factory = db
    script = tmp_path / "fake-rsgain"
    script.write_text(
        "#!/bin/sh\nprintf '\\033[1;32manalyzing\\033[0m\\n'\n"
        "printf '\\033[1;31mwarning\\033[0m\\n' >&2\nexit 0\n"
    )
    script.chmod(0o755)
    settings.rsgain_binary = str(script)
    job_id, _library = await _fixture_job(session_factory, tmp_path / "music", script)
    runner = JobRunner(session_factory, settings, "fake-rsgain", verifier=PassingVerifier())
    claimed = await runner.claim_next_job()
    assert claimed == job_id
    await runner.run_job(job_id)
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        album = await session.scalar(select(Album).where(Album.id == job.album_id))
        logs = (await session.scalars(select(JobLog).where(JobLog.job_id == job_id))).all()
        assert job.status == "succeeded"
        assert album.state == "processed"
        assert "analyzing" in job.stdout_tail
        assert "warning" in job.stderr_tail
        assert "\x1b" not in job.stdout_tail
        assert "\x1b" not in job.stderr_tail
        assert len(logs) == 2


@pytest.mark.asyncio
async def test_runner_surfaces_nonzero_exit_and_stderr(db, tmp_path: Path):
    settings, _engine, session_factory = db
    script = tmp_path / "fake-rsgain-fails"
    script.write_text("#!/bin/sh\necho broken >&2\nexit 7\n")
    script.chmod(0o755)
    settings.rsgain_binary = str(script)
    job_id, _library = await _fixture_job(session_factory, tmp_path / "music", script)
    runner = JobRunner(session_factory, settings, "fake-rsgain", verifier=PassingVerifier())
    await runner.claim_next_job()
    await runner.run_job(job_id)
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == "failed"
        assert job.exit_code == 7
        assert "broken" in job.stderr_tail


@pytest.mark.asyncio
async def test_restart_requeues_running_job_by_default(db, tmp_path: Path):
    settings, _engine, session_factory = db
    root = tmp_path / "music"
    async with session_factory() as session:
        library = Library(
            name="recovery-library",
            path=str(root),
            enabled=True,
            scan_interval_seconds=10,
            settle_seconds=0,
            include_extensions=[".flac"],
            exclude_patterns=[],
        )
        root.mkdir()
        album = Album(
            library=library, relative_path="Album", state="processing", file_count=1, total_size=1
        )
        session.add(album)
        await session.flush()
        job = Job(
            library=library,
            album=album,
            status="running",
            reason="test",
            queued_at=utcnow(),
            started_at=utcnow(),
        )
        session.add(job)
        await session.commit()
        job_id = job.id
    runner = JobRunner(session_factory, settings, "rsgain 3.6")
    assert await runner.recover_jobs() == 1
    async with session_factory() as session:
        recovered = await session.get(Job, job_id)
        assert recovered.status == "queued"
        assert recovered.error_message == "requeued after application restart"

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Album, Job, Library, utcnow
from app.services.reconciliation import Reconciler
from app.services.verification import VerificationResult


class AlreadyTaggedVerifier:
    def verify(self, files, album_gain_enabled):
        return VerificationResult(ok=True, checked_files=len(files))


async def _add_library(session_factory, path: Path, settle_seconds: int = 0) -> Library:
    async with session_factory() as session:
        library = Library(
            name=path.name,
            path=str(path),
            enabled=True,
            scan_interval_seconds=10,
            settle_seconds=settle_seconds,
            include_extensions=[".flac"],
            exclude_patterns=[],
        )
        session.add(library)
        await session.commit()
        return library


@pytest.mark.asyncio
async def test_new_album_waits_then_queues_and_unchanged_album_is_not_duplicated(
    db, tmp_path: Path
):
    settings, _engine, session_factory = db
    library_root = tmp_path / "library"
    album_dir = library_root / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    library = await _add_library(session_factory, library_root, settle_seconds=1)
    reconciler = Reconciler(session_factory, settings, "rsgain 3.6")

    first = await reconciler.reconcile_library(library.id)
    assert first.discovered == 1
    async with session_factory() as session:
        album = await session.scalar(select(Album).where(Album.library_id == library.id))
        assert album.state == "waiting_for_stability"
        album.stable_since = utcnow() - timedelta(seconds=2)
        await session.commit()

    second = await reconciler.reconcile_library(library.id)
    assert second.queued == 1
    third = await reconciler.reconcile_library(library.id)
    assert third.queued == 0
    async with session_factory() as session:
        jobs = (await session.scalars(select(Job))).all()
        assert len(jobs) == 1


@pytest.mark.asyncio
async def test_partial_copy_is_not_queued(db, tmp_path: Path):
    settings, _engine, session_factory = db
    library_root = tmp_path / "library"
    album_dir = library_root / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    (album_dir / "02.flac.part").write_bytes(b"partial")
    library = await _add_library(session_factory, library_root, settle_seconds=0)
    result = await Reconciler(session_factory, settings, "rsgain 3.6").reconcile_library(library.id)
    assert result.queued == 0
    async with session_factory() as session:
        album = await session.scalar(select(Album).where(Album.library_id == library.id))
        assert album.temporary_files_present is True
        assert album.state == "waiting_for_stability"


@pytest.mark.asyncio
async def test_already_tagged_album_is_skipped_without_a_job(db, tmp_path: Path):
    settings, _engine, session_factory = db
    library_root = tmp_path / "library"
    album_dir = library_root / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"already tagged")
    library = await _add_library(session_factory, library_root, settle_seconds=0)
    reconciler = Reconciler(session_factory, settings, "rsgain 3.6")
    reconciler.verifier = AlreadyTaggedVerifier()

    result = await reconciler.reconcile_library(library.id)

    assert result.queued == 0
    assert result.skipped == 1
    async with session_factory() as session:
        album = await session.scalar(select(Album).where(Album.library_id == library.id))
        jobs = (await session.scalars(select(Job))).all()
        assert album.state == "skipped"
        assert album.processed_source_fingerprint == album.source_fingerprint
        assert len(jobs) == 0

    second = await reconciler.reconcile_library(library.id)
    assert second.queued == 0
    async with session_factory() as session:
        album = await session.scalar(select(Album).where(Album.library_id == library.id))
        assert album.state == "skipped"


@pytest.mark.asyncio
async def test_two_claimers_do_not_claim_same_album(db, tmp_path: Path):
    settings, _engine, session_factory = db
    library_root = tmp_path / "library"
    library_root.mkdir()
    library = await _add_library(session_factory, library_root)
    async with session_factory() as session:
        album = Album(
            library_id=library.id,
            relative_path=".",
            state="queued",
            file_count=1,
            total_size=1,
            source_fingerprint="a",
            config_fingerprint="b",
        )
        session.add(album)
        await session.flush()
        session.add(
            Job(
                library_id=library.id,
                album_id=album.id,
                status="queued",
                reason="test",
                queued_at=utcnow(),
            )
        )
        await session.commit()
    from app.services.runner import JobRunner

    runner = JobRunner(session_factory, settings, "rsgain 3.6")
    claimed = await asyncio.gather(runner.claim_next_job(), runner.claim_next_job())
    assert sorted(item for item in claimed if item is not None) == [1]


@pytest.mark.asyncio
async def test_rename_reuses_album_and_active_job(db, tmp_path: Path):
    settings, _engine, session_factory = db
    library_root = tmp_path / "library"
    old_dir = library_root / "Old"
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"audio")
    library = await _add_library(session_factory, library_root, settle_seconds=0)
    reconciler = Reconciler(session_factory, settings, "rsgain 3.6")
    await reconciler.reconcile_library(library.id)
    async with session_factory() as session:
        album = await session.scalar(select(Album).where(Album.library_id == library.id))
        album.stable_since = utcnow() - timedelta(seconds=2)
        await session.commit()
    await reconciler.reconcile_library(library.id)
    async with session_factory() as session:
        original_job = await session.scalar(select(Job).where(Job.album_id == album.id))
        original_job_id = original_job.id
    old_dir.rename(library_root / "Renamed")
    result = await reconciler.reconcile_library(library.id)
    assert result.renamed == 1
    async with session_factory() as session:
        albums = (await session.scalars(select(Album).where(Album.library_id == library.id))).all()
        jobs = (await session.scalars(select(Job).where(Job.album_id == albums[0].id))).all()
        assert len(albums) == 1
        assert albums[0].relative_path == "Renamed"
        assert [job.id for job in jobs] == [original_job_id]


@pytest.mark.asyncio
async def test_configuration_change_marks_processed_album_stale(db, tmp_path: Path):
    settings, _engine, session_factory = db
    library_root = tmp_path / "library"
    album_dir = library_root / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"audio")
    library = await _add_library(session_factory, library_root, settle_seconds=0)
    reconciler = Reconciler(session_factory, settings, "rsgain 3.6")
    await reconciler.reconcile_library(library.id)
    async with session_factory() as session:
        album = await session.scalar(select(Album).where(Album.library_id == library.id))
        album.processed_source_fingerprint = album.source_fingerprint
        album.processed_config_fingerprint = album.config_fingerprint
        album.state = "processed"
        await session.commit()
    settings.target_loudness = "-16 LUFS"
    await reconciler.reconcile_library(library.id)
    async with session_factory() as session:
        album = await session.scalar(select(Album).where(Album.library_id == library.id))
        assert album.state == "changed"
        assert album.processed_config_fingerprint != album.config_fingerprint

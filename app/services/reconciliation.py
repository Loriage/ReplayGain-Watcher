"""Periodic reconciliation and album stability state machine."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Album, AudioFile, Job, Library, utcnow
from app.services.filesystem import AlbumObservation, scan_library

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconciliationResult:
    library_id: int
    library_name: str
    discovered: int = 0
    changed: int = 0
    waiting: int = 0
    queued: int = 0
    missing: int = 0
    renamed: int = 0
    unsupported_files: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


def _age_seconds(value: datetime | None, now: datetime) -> float:
    if value is None:
        return 0
    return max(0.0, (now - value).total_seconds())


class Reconciler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        rsgain_version: str,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.rsgain_version = rsgain_version

    async def sync_configured_libraries(self) -> None:
        """Upsert startup configuration and disable removed declarations."""

        configured = self.settings.load_libraries()
        configured_names = {item.name for item in configured}
        async with self.session_factory() as session:
            existing = {item.name: item for item in (await session.scalars(select(Library))).all()}
            for item in configured:
                current = existing.get(item.name)
                values = {
                    "path": str(item.path),
                    "enabled": item.enabled,
                    "scan_interval_seconds": item.scan_interval_seconds
                    or self.settings.reconciliation_interval_seconds,
                    "settle_seconds": item.settle_seconds
                    if item.settle_seconds is not None
                    else self.settings.settle_seconds,
                    "include_extensions": self.settings.effective_extensions(item),
                    "exclude_patterns": self.settings.effective_excludes(item),
                    "updated_at": utcnow(),
                }
                if current is None:
                    session.add(
                        Library(
                            name=item.name,
                            created_at=utcnow(),
                            **values,
                        )
                    )
                else:
                    for key, value in values.items():
                        setattr(current, key, value)
            for name, library in existing.items():
                if name not in configured_names:
                    library.enabled = False
                    library.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise ValueError("library names and paths must be unique")

    async def reconcile_all(self) -> list[ReconciliationResult]:
        await self.sync_configured_libraries()
        async with self.session_factory() as session:
            libraries = (
                await session.scalars(select(Library).where(Library.enabled.is_(True)))
            ).all()
        results: list[ReconciliationResult] = []
        for library in libraries:
            results.append(await self.reconcile_library(library.id))
        return results

    async def reconcile_library(self, library_id: int) -> ReconciliationResult:
        async with self.session_factory() as session:
            library = await session.get(Library, library_id)
            if library is None:
                raise ValueError(f"library {library_id} does not exist")
            scan = await asyncio.to_thread(
                scan_library,
                Path(library.path),
                list(library.include_extensions),
                list(library.exclude_patterns),
                list(self.settings.temporary_suffixes),
                self.settings.follow_symlinks,
                self.settings.stay_on_filesystem,
            )
            result = ReconciliationResult(
                library_id=library.id,
                library_name=library.name,
                unsupported_files=len(scan.unsupported_files),
                errors=list(scan.errors),
                duration_seconds=scan.duration_seconds,
            )
            now = utcnow()
            library.last_reconciliation_at = now
            library.last_error = "\n".join(scan.errors) if scan.errors else None
            if scan.errors and not scan.albums:
                await session.commit()
                return result

            albums = list(
                (await session.scalars(select(Album).where(Album.library_id == library.id))).all()
            )
            by_path = {album.relative_path: album for album in albums}
            seen_paths: set[str] = set()
            config_fingerprint = self.settings.configuration_fingerprint(self.rsgain_version)

            # A directory rename should retain the indexed album and its job history when
            # the cheap source fingerprint still matches. This prevents duplicate jobs.
            missing_albums = [album for album in albums if album.relative_path not in scan.albums]
            rename_candidates: dict[str, list[Album]] = defaultdict(list)
            for album in missing_albums:
                if album.source_fingerprint:
                    rename_candidates[album.source_fingerprint].append(album)
            for relative_path, observation in scan.albums.items():
                if relative_path in by_path:
                    continue
                candidates = rename_candidates.get(observation.source_fingerprint, [])
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.relative_path != relative_path
                ]
                if len(candidates) == 1:
                    old = candidates[0]
                    by_path.pop(old.relative_path, None)
                    old.relative_path = relative_path
                    old.last_seen_at = now
                    old.state = "waiting_for_stability"
                    old.stable_since = now
                    old.temporary_files_present = observation.has_temporary_files
                    by_path[relative_path] = old
                    seen_paths.add(relative_path)
                    result.renamed += 1

            for relative_path, observation in scan.albums.items():
                album = by_path.get(relative_path)
                if album is None:
                    album = Album(
                        library_id=library.id,
                        relative_path=relative_path,
                        state="waiting_for_stability",
                        discovered_at=now,
                        last_seen_at=now,
                        stable_since=now,
                        temporary_files_present=observation.has_temporary_files,
                        file_count=len(observation.files),
                        total_size=observation.total_size,
                        source_fingerprint=observation.source_fingerprint,
                        config_fingerprint=config_fingerprint,
                    )
                    session.add(album)
                    await session.flush()
                    result.discovered += 1
                else:
                    await self._update_existing_album(
                        session,
                        album,
                        observation,
                        config_fingerprint,
                        now,
                        result,
                    )
                seen_paths.add(relative_path)
                await self._upsert_audio_files(session, album, observation, now)
                if await self._maybe_queue(
                    session,
                    album,
                    observation,
                    config_fingerprint,
                    now,
                    library.settle_seconds,
                ):
                    result.queued += 1
                if album.state == "waiting_for_stability":
                    result.waiting += 1

            if not scan.errors:
                for album in albums:
                    if album.relative_path in seen_paths:
                        continue
                    if album.state != "missing":
                        result.missing += 1
                    album.state = "missing"
                    album.last_seen_at = now
                    album.stable_since = None
                    await session.execute(
                        update(Job)
                        .where(Job.album_id == album.id, Job.status == "queued")
                        .values(
                            status="cancelled",
                            finished_at=now,
                            error_message="album disappeared during reconciliation",
                        )
                    )

            await self._mark_configuration_staleness(session, albums, config_fingerprint, result)
            await session.commit()
            return result

    async def _update_existing_album(
        self,
        session: AsyncSession,
        album: Album,
        observation: AlbumObservation,
        config_fingerprint: str,
        now: datetime,
        result: ReconciliationResult,
    ) -> None:
        source_changed = album.source_fingerprint != observation.source_fingerprint
        config_changed = (
            album.config_fingerprint is not None and album.config_fingerprint != config_fingerprint
        )
        temporary_changed = album.temporary_files_present != observation.has_temporary_files
        album.last_seen_at = now
        album.file_count = len(observation.files)
        album.total_size = observation.total_size
        album.config_fingerprint = config_fingerprint
        album.temporary_files_present = observation.has_temporary_files
        if source_changed:
            album.source_fingerprint = observation.source_fingerprint
            album.stable_since = now
            album.state = (
                "changed" if album.processed_source_fingerprint else "waiting_for_stability"
            )
            album.last_error = None
            result.changed += 1
        if source_changed or config_changed:
            reason = (
                "source changed before queued job started"
                if source_changed
                else "configuration changed before queued job started"
            )
            await session.execute(
                update(Job)
                .where(Job.album_id == album.id, Job.status == "queued")
                .values(status="cancelled", finished_at=now, error_message=reason)
            )
        elif temporary_changed and not observation.has_temporary_files:
            # Stability starts after the temporary artifact disappears, not while a
            # downloader is still holding the directory open.
            album.stable_since = now
            if album.state not in {"processed", "failed"}:
                album.state = "waiting_for_stability"
        elif album.stable_since is None and album.state not in {"processed", "failed", "missing"}:
            album.stable_since = now
            album.state = "waiting_for_stability"

    async def _upsert_audio_files(
        self,
        session: AsyncSession,
        album: Album,
        observation: AlbumObservation,
        now: datetime,
    ) -> None:
        current = {
            item.relative_path: item
            for item in (
                await session.scalars(select(AudioFile).where(AudioFile.album_id == album.id))
            ).all()
        }
        observed_paths = set()
        for item in observation.files:
            observed_paths.add(item.relative_path)
            stored = current.get(item.relative_path)
            if stored is None:
                session.add(
                    AudioFile(
                        album_id=album.id,
                        relative_path=item.relative_path,
                        size=item.size,
                        mtime_ns=item.mtime_ns,
                        format=item.format,
                        last_seen_at=now,
                    )
                )
            else:
                if stored.size != item.size or stored.mtime_ns != item.mtime_ns:
                    stored.replaygain_track_gain_present = False
                    stored.replaygain_album_gain_present = False
                stored.size = item.size
                stored.mtime_ns = item.mtime_ns
                stored.format = item.format
                stored.last_seen_at = now
        for relative_path, stored in current.items():
            if relative_path not in observed_paths:
                await session.delete(stored)

    async def _maybe_queue(
        self,
        session: AsyncSession,
        album: Album,
        observation: AlbumObservation,
        config_fingerprint: str,
        now: datetime,
        settle_seconds: int,
    ) -> bool:
        if observation.has_temporary_files or not album.stable_since:
            return False
        if _age_seconds(album.stable_since, now) < settle_seconds:
            return False
        if album.state == "failed":
            return False
        source_needs_processing = (
            album.processed_source_fingerprint != observation.source_fingerprint
        )
        config_needs_processing = album.processed_config_fingerprint != config_fingerprint
        if not source_needs_processing and not config_needs_processing:
            if album.state not in {"missing", "failed"}:
                album.state = "processed"
            return False
        if (
            config_needs_processing
            and not source_needs_processing
            and self.settings.config_change_policy == "mark"
        ):
            album.state = "changed"
            return False

        active = await session.execute(
            select(Job.id, Job.status).where(
                Job.album_id == album.id, Job.status.in_(["queued", "running"])
            )
        )
        active_row = active.first()
        if active_row is not None:
            album.state = "processing" if active_row.status == "running" else "queued"
            return False
        reason = "discovered"
        if album.processed_source_fingerprint is not None:
            reason = "source_changed" if source_needs_processing else "config_changed"
        job = Job(
            library_id=album.library_id,
            album_id=album.id,
            kind="analyze" if album.processed_source_fingerprint is None else "reanalyze",
            status="queued",
            reason=reason,
            priority=100,
            queued_at=now,
            source_fingerprint=observation.source_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        try:
            async with session.begin_nested():
                session.add(job)
                await session.flush()
        except IntegrityError:
            logger.info("job already queued for album %s", album.id)
            album.state = "queued"
            return False
        album.state = "queued"
        album.last_error = None
        album.last_job_id = job.id
        return True

    async def _mark_configuration_staleness(
        self,
        session: AsyncSession,
        albums: list[Album],
        config_fingerprint: str,
        result: ReconciliationResult,
    ) -> None:
        # Existing albums absent from this scan are handled as missing above. For
        # visible albums, _maybe_queue applies the configured mark/requeue policy.
        for album in albums:
            if (
                album.state == "processed"
                and album.processed_config_fingerprint != config_fingerprint
            ):
                album.config_fingerprint = config_fingerprint
                album.state = "changed"

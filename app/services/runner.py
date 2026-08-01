"""Persistent queue workers and the safe rsgain subprocess boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app import __version__
from app.config import Settings
from app.models import Album, AudioFile, Job, JobLog, Library, utcnow
from app.services.filesystem import scan_library
from app.services.fingerprints import source_fingerprint
from app.services.metrics import Metrics
from app.services.path_security import UnsafePathError, resolve_album_path
from app.services.verification import MetadataVerifier, VerificationResult

logger = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _clean_output(value: str) -> str:
    return _ANSI_ESCAPE.sub("", value).replace("\r", "")


@dataclass(frozen=True, slots=True)
class ToolStatus:
    available: bool
    version: str
    error: str | None = None


async def detect_rsgain(binary: str) -> ToolStatus:
    """Detect rsgain without a shell and retain a useful version string."""

    if not shutil.which(binary) and not Path(binary).is_absolute():
        return ToolStatus(False, "unavailable", f"{binary!r} was not found on PATH")
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except (TimeoutError, OSError) as exc:
        return ToolStatus(False, "unavailable", f"could not execute rsgain: {exc}")
    text = _ANSI_ESCAPE.sub("", output.decode("utf-8", errors="replace")).strip()
    if process.returncode != 0:
        return ToolStatus(
            False, text or "unavailable", f"rsgain --version exited {process.returncode}"
        )
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "unknown")
    match = re.search(r"\brsgain\s+([0-9][^\s]*)", first_line, flags=re.IGNORECASE)
    version = f"rsgain {match.group(1)}" if match else first_line[:128]
    return ToolStatus(True, version)


def _tail_append(target: deque[str], line: str, max_lines: int) -> None:
    target.append(_clean_output(line).rstrip("\n"))
    while len(target) > max_lines:
        target.popleft()


async def _terminate_process(process: asyncio.subprocess.Process, grace_seconds: int) -> None:
    if process.returncode is not None:
        return
    try:
        if process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    try:
        if process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        return
    await process.wait()


class JobRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        rsgain_version: str,
        verifier: MetadataVerifier | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.rsgain_version = rsgain_version
        self.verifier = verifier or MetadataVerifier()
        self.metrics = metrics
        self._stop_event = asyncio.Event()
        self._worker_tasks: list[asyncio.Task[None]] = []

    async def recover_jobs(self) -> int:
        """Turn jobs from a previous process into explicit interrupted/retry states."""

        async with self.session_factory() as session:
            now = utcnow()
            recoverable = (
                await session.scalars(
                    select(Job)
                    .where(Job.status.in_(["running", "interrupted"]))
                    .options(selectinload(Job.album))
                )
            ).all()
            count = 0
            for job in recoverable:
                if job.status == "running":
                    job.status = "interrupted"
                    job.finished_at = now
                    job.error_message = "application stopped while job was running"
                    count += 1
                if self.settings.recovery_policy == "requeue" and job.album is not None:
                    job.status = "queued"
                    job.finished_at = None
                    job.error_message = "requeued after application restart"
                    job.queued_at = now
                    job.album.state = "queued"
            await session.commit()
            return count

    async def start(self) -> None:
        self._stop_event.clear()
        self._worker_tasks = [
            asyncio.create_task(self.worker_loop(index), name=f"replaygain-worker-{index}")
            for index in range(self.settings.worker_concurrency)
        ]

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

    async def worker_loop(self, worker_index: int) -> None:
        while not self._stop_event.is_set():
            job_id = await self.claim_next_job()
            if job_id is None:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                except TimeoutError:
                    continue
                continue
            try:
                await self.run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker %s crashed while running job %s", worker_index, job_id)

    async def claim_next_job(self) -> int | None:
        """Claim one queued row with a single conditional UPDATE and commit before exec."""

        async with self.session_factory() as session:
            now = utcnow()
            candidate = (
                select(Job.id)
                .where(Job.status == "queued")
                .order_by(Job.priority.asc(), Job.queued_at.asc(), Job.id.asc())
                .limit(1)
                .scalar_subquery()
            )
            result = await session.execute(
                update(Job)
                .where(Job.id == candidate, Job.status == "queued")
                .values(
                    status="running",
                    started_at=now,
                    heartbeat_at=now,
                    command_version=self._command_version(),
                )
                .returning(Job.id)
            )
            job_id = result.scalar_one_or_none()
            if job_id is None:
                await session.rollback()
                return None
            await session.execute(
                update(Album)
                .where(Album.id == select(Job.album_id).where(Job.id == job_id).scalar_subquery())
                .values(state="processing", last_job_id=job_id)
            )
            await session.commit()
            return int(job_id)

    async def run_job(self, job_id: int) -> None:
        async with self.session_factory() as session:
            job = await session.scalar(
                select(Job)
                .where(Job.id == job_id)
                .options(selectinload(Job.album), selectinload(Job.library))
            )
            if job is None or job.album is None or job.library is None:
                return
            album = job.album
            library = job.library
            try:
                album_path = resolve_album_path(Path(library.path), album.relative_path)
            except (UnsafePathError, OSError) as exc:
                await self._finish_failure(
                    session, job, album, f"unsafe or unavailable album path: {exc}"
                )
                return

            command = [self.settings.rsgain_binary, "easy", str(album_path)]
            log_album_path = (
                f"/libraries/{library.name}/{album.relative_path}"
                if self.settings.redact_host_paths
                else str(album_path)
            )
            log_command = shlex.join(
                [
                    Path(self.settings.rsgain_binary).name
                    if self.settings.redact_host_paths
                    else self.settings.rsgain_binary,
                    "easy",
                    log_album_path,
                ]
            )
            logger.info("rsgain command started: %s", log_command)
            roots = [Path(library.path)]
            process: asyncio.subprocess.Process | None = None
            heartbeat_task: asyncio.Task[None] | None = None
            stdout_tail: deque[str] = deque(maxlen=self.settings.log_tail_lines)
            stderr_tail: deque[str] = deque(maxlen=self.settings.log_tail_lines)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                heartbeat_task = asyncio.create_task(self._heartbeat(job.id))
                capture_task = asyncio.create_task(
                    self._capture_process_output(
                        session,
                        job,
                        process,
                        stdout_tail,
                        stderr_tail,
                        roots,
                    )
                )
                timed_out = False
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self.settings.job_timeout_seconds
                    )
                except TimeoutError:
                    timed_out = True
                    await _terminate_process(process, self.settings.job_termination_grace_seconds)
                await capture_task
                if process.returncode is None:
                    await process.wait()
                logger.info(
                    "rsgain command finished: library=%s folder=%s exit_code=%s",
                    library.name,
                    album.relative_path,
                    process.returncode,
                )
                if timed_out:
                    job.stdout_tail = "\n".join(stdout_tail)
                    job.stderr_tail = "\n".join(stderr_tail)
                    await self._finish_failure(
                        session, job, album, "rsgain timed out", exit_code=process.returncode
                    )
                    return
                if process.returncode != 0:
                    job.stdout_tail = "\n".join(stdout_tail)
                    job.stderr_tail = "\n".join(stderr_tail)
                    message = f"rsgain exited with code {process.returncode}"
                    if stderr_tail:
                        message = f"{message}: {list(stderr_tail)[-1]}"
                    await self._finish_failure(
                        session, job, album, message, exit_code=process.returncode
                    )
                    return

                scan = await asyncio.to_thread(
                    scan_library,
                    album_path,
                    list(library.include_extensions),
                    [],
                    list(self.settings.temporary_suffixes),
                    self.settings.follow_symlinks,
                    self.settings.stay_on_filesystem,
                )
                observation = scan.albums.get(".")
                if observation is None or observation.has_temporary_files:
                    await self._finish_failure(
                        session,
                        job,
                        album,
                        "post-run verification could not find a complete album",
                        exit_code=process.returncode,
                    )
                    return
                verification = await asyncio.to_thread(
                    self.verifier.verify,
                    [album_path / item.relative_path for item in observation.files],
                    self.settings.album_gain_enabled,
                )
                job.stdout_tail = "\n".join(stdout_tail)
                job.stderr_tail = "\n".join(stderr_tail)
                job.verification_result = json.dumps(verification.as_dict(), sort_keys=True)
                if not verification.ok:
                    await self._finish_failure(
                        session,
                        job,
                        album,
                        "post-run ReplayGain verification failed",
                        exit_code=process.returncode,
                        verification=verification,
                    )
                    return
                await self._finish_success(session, job, album, library, observation, verification)
            except asyncio.CancelledError:
                if process is not None:
                    await _terminate_process(process, self.settings.job_termination_grace_seconds)
                await self._finish_interrupted(job_id)
                raise
            except (OSError, asyncio.SubprocessError) as exc:
                await self._finish_failure(session, job, album, f"could not execute rsgain: {exc}")
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _capture_process_output(
        self,
        session: AsyncSession,
        job: Job,
        process: asyncio.subprocess.Process,
        stdout_tail: deque[str],
        stderr_tail: deque[str],
        roots: list[Path],
    ) -> None:
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def pump(stream: asyncio.StreamReader | None, name: str) -> None:
            if stream is None:
                await queue.put(None)
                return
            async for raw in stream:
                await queue.put((name, raw.decode("utf-8", errors="replace").rstrip("\n")))
            await queue.put(None)

        pumps = [
            asyncio.create_task(pump(process.stdout, "stdout")),
            asyncio.create_task(pump(process.stderr, "stderr")),
        ]
        completed_streams = 0
        pending_logs: list[JobLog] = []
        try:
            while completed_streams < 2:
                item = await queue.get()
                if item is None:
                    completed_streams += 1
                    continue
                stream, message = item
                sanitized = _clean_output(message)
                for root in roots:
                    sanitized = sanitized.replace(str(root), "<library>")
                if stream == "stdout":
                    _tail_append(stdout_tail, sanitized, self.settings.log_tail_lines)
                else:
                    _tail_append(stderr_tail, sanitized, self.settings.log_tail_lines)
                if sanitized:
                    logger.info("rsgain %s: %s", stream, sanitized)
                pending_logs.append(
                    JobLog(job_id=job.id, stream=stream, level="INFO", message=sanitized)
                )
                if len(pending_logs) >= 25:
                    session.add_all(pending_logs)
                    pending_logs.clear()
                    await session.flush()
            if pending_logs:
                session.add_all(pending_logs)
                await session.flush()
        finally:
            await asyncio.gather(*pumps, return_exceptions=True)

    async def _heartbeat(self, job_id: int) -> None:
        interval = min(30, max(1, self.settings.job_timeout_seconds // 10))
        while True:
            await asyncio.sleep(interval)
            async with self.session_factory() as session:
                await session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "running")
                    .values(heartbeat_at=utcnow())
                )
                await session.commit()

    async def _finish_success(
        self,
        session: AsyncSession,
        job: Job,
        album: Album,
        library: Library,
        observation,
        verification: VerificationResult,
    ) -> None:
        now = utcnow()
        fingerprint = source_fingerprint(observation.files)
        job.status = "succeeded"
        job.finished_at = now
        job.exit_code = 0
        job.error_message = None
        album.source_fingerprint = fingerprint
        album.processed_source_fingerprint = fingerprint
        album.config_fingerprint = job.config_fingerprint
        album.processed_config_fingerprint = job.config_fingerprint
        album.processed_at = now
        album.state = "processed"
        album.last_error = None
        library.last_success_at = now
        await self._sync_verified_files(session, album, observation, verification)
        self._observe_duration(job, failed=False)
        await session.commit()

    async def _finish_failure(
        self,
        session: AsyncSession,
        job: Job,
        album: Album,
        message: str,
        exit_code: int | None = None,
        verification: VerificationResult | None = None,
    ) -> None:
        now = utcnow()
        job.status = "failed"
        job.finished_at = now
        job.exit_code = exit_code
        job.error_message = message
        if verification is not None:
            job.verification_result = json.dumps(verification.as_dict(), sort_keys=True)
        album.state = "failed"
        album.last_error = message
        self._observe_duration(job, failed=True)
        await session.commit()

    async def _finish_interrupted(self, job_id: int) -> None:
        async with self.session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            job.status = "interrupted"
            job.finished_at = utcnow()
            job.error_message = "worker task cancelled"
            await session.commit()

    async def _sync_verified_files(
        self, session: AsyncSession, album: Album, observation, verification
    ) -> None:
        current = {
            item.relative_path: item
            for item in (
                await session.scalars(select(AudioFile).where(AudioFile.album_id == album.id))
            ).all()
        }
        track_missing = set(verification.missing_track_gain)
        album_missing = set(verification.missing_album_gain)
        observed_paths = set()
        for item in observation.files:
            observed_paths.add(item.relative_path)
            stored = current.get(item.relative_path)
            if stored is None:
                stored = AudioFile(
                    album_id=album.id,
                    relative_path=item.relative_path,
                    size=item.size,
                    mtime_ns=item.mtime_ns,
                    format=item.format,
                )
                session.add(stored)
            stored.size = item.size
            stored.mtime_ns = item.mtime_ns
            stored.format = item.format
            stored.replaygain_track_gain_present = item.relative_path not in track_missing
            stored.replaygain_album_gain_present = item.relative_path not in album_missing
            stored.last_seen_at = utcnow()
        for relative_path, stored in current.items():
            if relative_path not in observed_paths:
                await session.delete(stored)

    async def retry_job(self, job_id: int) -> bool:
        if not self.settings.ui_actions_enabled:
            return False
        async with self.session_factory() as session:
            job = await session.scalar(
                select(Job).where(Job.id == job_id).options(selectinload(Job.album))
            )
            if (
                job is None
                or job.status not in {"failed", "interrupted", "cancelled"}
                or job.album is None
            ):
                return False
            active = await session.scalar(
                select(Job.id).where(
                    Job.album_id == job.album_id, Job.status.in_(["queued", "running"])
                )
            )
            if active is not None:
                return False
            retry = Job(
                library_id=job.library_id,
                album_id=job.album_id,
                kind="reanalyze",
                status="queued",
                reason="manual_retry",
                priority=job.priority,
                queued_at=utcnow(),
                source_fingerprint=job.album.source_fingerprint,
                config_fingerprint=job.album.config_fingerprint,
            )
            session.add(retry)
            await session.flush()
            job.album.state = "queued"
            job.album.last_job_id = retry.id
            await session.commit()
            return True

    async def cancel_job(self, job_id: int) -> bool:
        if not self.settings.ui_actions_enabled:
            return False
        async with self.session_factory() as session:
            job = await session.scalar(
                select(Job).where(Job.id == job_id).options(selectinload(Job.album))
            )
            if job is None or job.status != "queued":
                return False
            job.status = "cancelled"
            job.finished_at = utcnow()
            job.error_message = "cancelled by operator"
            if job.album is not None and job.album.state == "queued":
                job.album.state = (
                    "processed"
                    if job.album.processed_source_fingerprint == job.album.source_fingerprint
                    else "changed"
                )
            await session.commit()
            return True

    def _command_version(self) -> str:
        return f"app={__version__};rsgain={self.rsgain_version}"

    def _observe_duration(self, job: Job, failed: bool) -> None:
        if self.metrics is None or job.started_at is None:
            return
        finished_at = utcnow()
        self.metrics.job_duration.observe(max(0.0, (finished_at - job.started_at).total_seconds()))
        if failed:
            self.metrics.rsgain_failures.inc()

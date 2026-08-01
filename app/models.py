"""Persistent domain model for libraries, albums, files, jobs, and logs."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    path: Mapped[str] = mapped_column(String(4096), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    settle_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    include_extensions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exclude_patterns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    albums: Mapped[list[Album]] = relationship(
        back_populates="library", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="library")


class Album(Base):
    __tablename__ = "albums"
    __table_args__ = (
        UniqueConstraint("library_id", "relative_path", name="uq_album_library_path"),
        Index("ix_albums_library_state", "library_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="discovered")
    discovered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    stable_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    temporary_files_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    library: Mapped[Library] = relationship(back_populates="albums", foreign_keys=[library_id])
    audio_files: Mapped[list[AudioFile]] = relationship(
        back_populates="album", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[Job]] = relationship(
        back_populates="album", foreign_keys="Job.album_id", cascade="all, delete-orphan"
    )
    last_job: Mapped[Job | None] = relationship(foreign_keys=[last_job_id], post_update=True)


class AudioFile(Base):
    __tablename__ = "audio_files"
    __table_args__ = (UniqueConstraint("album_id", "relative_path", name="uq_audio_album_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    album_id: Mapped[int] = mapped_column(
        ForeignKey("albums.id", ondelete="CASCADE"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    optional_fast_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    replaygain_track_gain_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    replaygain_album_gain_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    album: Mapped[Album] = relationship(back_populates="audio_files")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_queue", "status", "priority", "queued_at"),
        Index(
            "uq_jobs_active_album",
            "album_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False
    )
    album_id: Mapped[int] = mapped_column(
        ForeignKey("albums.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="analyze")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    reason: Mapped[str] = mapped_column(String(128), nullable=False, default="discovered")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stdout_tail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stderr_tail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    library: Mapped[Library] = relationship(back_populates="jobs")
    album: Mapped[Album] = relationship(back_populates="jobs", foreign_keys=[album_id])
    logs: Mapped[list[JobLog]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"
    __table_args__ = (Index("ix_job_logs_job_timestamp", "job_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    stream: Mapped[str] = mapped_column(String(16), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    message: Mapped[str] = mapped_column(Text, nullable=False)

    job: Mapped[Job] = relationship(back_populates="logs")

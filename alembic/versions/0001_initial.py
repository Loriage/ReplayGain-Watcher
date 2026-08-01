"""Initial ReplayGain Watcher schema.

Revision ID: 0001_initial
Revises:
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "libraries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=4096), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("scan_interval_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("settle_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("include_extensions", sa.JSON(), nullable=False),
        sa.Column("exclude_patterns", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_reconciliation_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("name", name="uq_libraries_name"),
        sa.UniqueConstraint("path", name="uq_libraries_path"),
    )
    op.create_table(
        "albums",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relative_path", sa.String(length=4096), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="discovered"),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("stable_since", sa.DateTime(), nullable=True),
        sa.Column("temporary_files_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("processed_source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("processed_config_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("last_job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("library_id", "relative_path", name="uq_album_library_path"),
    )
    op.create_index("ix_albums_library_state", "albums", ["library_id", "state"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("album_id", sa.Integer(), sa.ForeignKey("albums.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="analyze"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("reason", sa.String(length=128), nullable=False, server_default="discovered"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("command_version", sa.String(length=128), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("stdout_tail", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr_tail", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("verification_result", sa.Text(), nullable=True),
    )
    op.create_index("ix_jobs_queue", "jobs", ["status", "priority", "queued_at"])
    op.create_index(
        "uq_jobs_active_album",
        "jobs",
        ["album_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "audio_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("album_id", sa.Integer(), sa.ForeignKey("albums.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relative_path", sa.String(length=4096), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("optional_fast_hash", sa.String(length=64), nullable=True),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("replaygain_track_gain_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("replaygain_album_gain_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("album_id", "relative_path", name="uq_audio_album_path"),
    )
    op.create_table(
        "job_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("stream", sa.String(length=16), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="INFO"),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_index("ix_job_logs_job_timestamp", "job_logs", ["job_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_job_logs_job_timestamp", table_name="job_logs")
    op.drop_table("job_logs")
    op.drop_table("audio_files")
    op.drop_index("uq_jobs_active_album", table_name="jobs")
    op.drop_index("ix_jobs_queue", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_albums_library_state", table_name="albums")
    op.drop_table("albums")
    op.drop_table("libraries")

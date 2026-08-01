"""Application configuration loaded from environment and an optional YAML file."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_EXTENSIONS = [
    ".flac",
    ".mp3",
    ".m4a",
    ".mp4",
    ".ogg",
    ".opus",
    ".wv",
    ".ape",
    ".wav",
    ".aiff",
    ".aif",
]
DEFAULT_EXCLUDE_PATTERNS = ["lost+found", ".recycle", ".Trash", "@eaDir"]
DEFAULT_TEMPORARY_SUFFIXES = [
    ".part",
    ".partial",
    ".tmp",
    ".download",
    ".crdownload",
    ".!qB",
]


class LibraryConfig(BaseModel):
    """A library root declared at startup; HTTP never creates arbitrary roots."""

    name: str = Field(min_length=1, max_length=128)
    path: Path
    enabled: bool = True
    scan_interval_seconds: int | None = Field(default=None, ge=10, le=604800)
    settle_seconds: int | None = Field(default=None, ge=0, le=604800)
    include_extensions: list[str] | None = None
    exclude_patterns: list[str] | None = None

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("library paths must be absolute")
        return Path(value)

    @field_validator("include_extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return [item.lower() if item.startswith(".") else f".{item.lower()}" for item in value]


class Settings(BaseSettings):
    """Runtime settings. Values can be supplied as environment variables."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "ReplayGain Watcher"
    app_version: str = "0.1.0"
    environment: str = "production"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:////data/replaygain-watcher.db"
    config_file: Path | None = Path("/app/config.yml")

    rsgain_binary: str = "rsgain"
    ffprobe_binary: str = "ffprobe"
    reconciliation_interval_seconds: int = Field(default=900, ge=10, le=604800)
    settle_seconds: int = Field(default=300, ge=0, le=604800)
    worker_concurrency: int = Field(default=1, ge=1, le=32)
    job_timeout_seconds: int = Field(default=14400, ge=1, le=604800)
    job_termination_grace_seconds: int = Field(default=30, ge=0, le=3600)
    config_change_policy: Literal["mark", "requeue"] = "mark"
    recovery_policy: Literal["requeue", "leave_interrupted"] = "requeue"
    log_retention_days: int = Field(default=30, ge=1, le=3650)
    log_tail_lines: int = Field(default=200, ge=10, le=10000)
    ui_actions_enabled: bool = False
    redact_host_paths: bool = True
    follow_symlinks: bool = False
    stay_on_filesystem: bool = True
    include_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    exclude_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    temporary_suffixes: list[str] = Field(default_factory=lambda: list(DEFAULT_TEMPORARY_SUFFIXES))
    album_gain_enabled: bool = True
    target_loudness: str = "-18 LUFS"
    true_peak_enabled: bool = True
    clipping_protection: bool = True
    maximum_peak: str = "0 dBTP"
    opus_tag_mode: str = "vorbis"
    rsgain_preset_contents: str = "easy"
    navidrome_rescan_mode: Literal["none", "webhook", "command"] = "none"

    @field_validator("include_extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str]) -> list[str]:
        return [item.lower() if item.startswith(".") else f".{item.lower()}" for item in value]

    def load_libraries(self) -> list[LibraryConfig]:
        """Load the startup-declared library list from YAML.

        A missing file is valid for development and means no libraries are configured.
        Invalid YAML is intentionally raised during startup so readiness cannot be green
        with an ambiguous filesystem scope.
        """

        if self.config_file is None or not self.config_file.exists():
            return []
        with self.config_file.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        raw_libraries = payload.get("libraries", [])
        if not isinstance(raw_libraries, list):
            raise ValueError("config.yml 'libraries' must be a list")
        return [LibraryConfig.model_validate(item) for item in raw_libraries]

    def effective_extensions(self, library: LibraryConfig) -> list[str]:
        return library.include_extensions or self.include_extensions

    def effective_excludes(self, library: LibraryConfig) -> list[str]:
        return library.exclude_patterns or self.exclude_patterns

    def configuration_fingerprint(self, rsgain_version: str) -> str:
        """Hash every setting that can change generated ReplayGain metadata."""

        values = {
            "rsgain_version": rsgain_version,
            "target_loudness": self.target_loudness,
            "album_gain_enabled": self.album_gain_enabled,
            "true_peak_enabled": self.true_peak_enabled,
            "clipping_protection": self.clipping_protection,
            "maximum_peak": self.maximum_peak,
            "opus_tag_mode": self.opus_tag_mode,
            "preset_contents": self.rsgain_preset_contents,
        }
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

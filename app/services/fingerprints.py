"""Cheap deterministic fingerprints for source files and runtime configuration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileObservation:
    relative_path: str
    size: int
    mtime_ns: int
    format: str


def source_fingerprint(files: list[FileObservation]) -> str:
    """Fingerprint metadata only; normal scans never read audio bytes."""

    canonical = "\n".join(
        f"{item.relative_path}\0{item.size}\0{item.mtime_ns}"
        for item in sorted(files, key=lambda value: value.relative_path)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extension_for(path: Path) -> str:
    return path.suffix.lower()

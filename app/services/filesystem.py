"""Safe, periodic filesystem traversal used as the discovery source of truth."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.services.fingerprints import FileObservation, extension_for, source_fingerprint


@dataclass(slots=True)
class AlbumObservation:
    relative_path: str
    files: list[FileObservation]
    source_fingerprint: str
    total_size: int
    has_temporary_files: bool = False
    temporary_files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LibraryScan:
    albums: dict[str, AlbumObservation]
    unsupported_files: list[str]
    errors: list[str]
    duration_seconds: float = 0.0


def _matches_exclusion(name: str, relative_path: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(relative_path, pattern)
        for pattern in patterns
    )


def validate_library_root(root: Path) -> tuple[Path, str | None]:
    """Return a resolved directory and a user-facing error, without raising on bad mounts."""

    try:
        resolved = root.expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        return root, f"library root is unavailable: {exc}"
    if not resolved.is_dir():
        return resolved, "library root is not a directory"
    if not os.access(resolved, os.R_OK):
        return resolved, "library root is not readable"
    if not os.access(resolved, os.W_OK):
        return resolved, "library root is not writable"
    return resolved, None


def scan_library(
    root: Path,
    include_extensions: list[str],
    exclude_patterns: list[str],
    temporary_suffixes: list[str],
    follow_symlinks: bool = False,
    stay_on_filesystem: bool = True,
) -> LibraryScan:
    """Walk a library without following symlink escapes by default.

    An album is a directory with at least one supported audio file directly inside it.
    The scan records unsupported files and traversal errors for observability but does
    not fail the entire library because one entry is unusual.
    """

    import time

    started = time.perf_counter()
    resolved_root, root_error = validate_library_root(root)
    if root_error:
        return LibraryScan({}, [], [root_error], time.perf_counter() - started)

    root_stat = resolved_root.stat()
    root_device = root_stat.st_dev
    albums: dict[str, AlbumObservation] = {}
    unsupported_files: list[str] = []
    errors: list[str] = []
    stack: list[Path] = [resolved_root]

    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
            continue

        try:
            relative_directory = directory.relative_to(resolved_root).as_posix()
        except ValueError:
            errors.append(f"skipped path outside library root: {directory}")
            continue
        relative_directory = relative_directory or "."

        audio_files: list[FileObservation] = []
        temporary_files: list[str] = []
        for entry in entries:
            entry_relative = (
                entry.name if relative_directory == "." else f"{relative_directory}/{entry.name}"
            )
            if _matches_exclusion(entry.name, entry_relative, exclude_patterns):
                continue
            try:
                is_link = entry.is_symlink()
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    if is_link and not follow_symlinks:
                        continue
                    if (
                        stay_on_filesystem
                        and entry.stat(follow_symlinks=follow_symlinks).st_dev != root_device
                    ):
                        continue
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=follow_symlinks):
                    continue
                if is_link and not follow_symlinks:
                    continue
                stat = entry.stat(follow_symlinks=follow_symlinks)
                if stay_on_filesystem and stat.st_dev != root_device:
                    continue
            except OSError as exc:
                errors.append(f"{entry.path}: {exc}")
                continue

            suffix = extension_for(Path(entry.name))
            if any(entry.name.casefold().endswith(item.casefold()) for item in temporary_suffixes):
                temporary_files.append(entry_relative)
                continue
            if suffix in include_extensions:
                audio_files.append(
                    FileObservation(
                        entry.name,
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                        suffix.removeprefix("."),
                    )
                )
            else:
                unsupported_files.append(entry_relative)

        if audio_files:
            fingerprint = source_fingerprint(audio_files)
            albums[relative_directory] = AlbumObservation(
                relative_path=relative_directory,
                files=audio_files,
                source_fingerprint=fingerprint,
                total_size=sum(item.size for item in audio_files),
                has_temporary_files=bool(temporary_files),
                temporary_files=temporary_files,
            )

    return LibraryScan(
        albums=albums,
        unsupported_files=sorted(unsupported_files),
        errors=errors,
        duration_seconds=time.perf_counter() - started,
    )

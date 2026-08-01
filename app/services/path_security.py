"""Path validation shared by reconciliation and subprocess execution."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """Raised when a path is not within a configured library root."""


def relative_album_path(value: str) -> str:
    if not value or "\x00" in value:
        raise UnsafePathError("album path is empty or contains a null byte")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise UnsafePathError("album path must be relative to its configured library")
    normalized = path.as_posix()
    return "." if normalized in ("", ".") else normalized


def resolve_album_path(root: Path, relative_path: str) -> Path:
    """Resolve an indexed album path and reject symlink escapes."""

    safe_relative = relative_album_path(relative_path)
    root_resolved = root.expanduser().resolve(strict=True)
    candidate = (root_resolved / safe_relative).resolve(strict=True)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError("album path resolves outside configured library") from exc
    if not candidate.is_dir():
        raise UnsafePathError("album path is not a directory")
    return candidate


def redact_path(path: str | Path, roots: list[Path], enabled: bool = True) -> str:
    if not enabled:
        return str(path)
    value = str(path)
    for root in roots:
        try:
            relative = Path(value).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        return f"<library>/{relative}" if relative != "." else "<library>"
    return value

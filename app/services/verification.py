"""Read-only ReplayGain metadata verification."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from mutagen import File as MutagenFile


@dataclass(slots=True)
class VerificationResult:
    ok: bool
    checked_files: int
    missing_track_gain: list[str] = field(default_factory=list)
    missing_album_gain: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _has_replaygain_tag(metadata: object, tag_name: str) -> bool:
    if metadata is None:
        return False
    needle = tag_name.casefold()
    try:
        keys = metadata.keys()  # type: ignore[union-attr]
    except AttributeError:
        return False
    for key in keys:
        normalized = str(key).casefold().replace(" ", "_")
        if needle not in normalized:
            continue
        try:
            value = metadata[key]  # type: ignore[index]
        except (KeyError, TypeError):
            continue
        if isinstance(value, list | tuple):
            return any(str(item).strip() for item in value)
        if isinstance(value, bytes):
            return bool(value.strip())
        return bool(str(value).strip())
    return False


class MetadataVerifier:
    """Inspect tags with mutagen; this class never writes metadata."""

    def verify(self, files: list[Path], album_gain_enabled: bool) -> VerificationResult:
        missing_track: list[str] = []
        missing_album: list[str] = []
        errors: list[str] = []
        for path in files:
            try:
                parsed = MutagenFile(path, easy=False)
            except Exception as exc:  # mutagen has format-specific parser exceptions
                errors.append(f"{path.name}: metadata read failed: {exc}")
                continue
            if parsed is None:
                errors.append(f"{path.name}: unsupported or unreadable metadata")
                continue
            if not _has_replaygain_tag(parsed, "replaygain_track_gain"):
                missing_track.append(path.name)
            if album_gain_enabled and not _has_replaygain_tag(parsed, "replaygain_album_gain"):
                missing_album.append(path.name)
        return VerificationResult(
            ok=not missing_track and not missing_album and not errors and bool(files),
            checked_files=len(files),
            missing_track_gain=missing_track,
            missing_album_gain=missing_album,
            errors=errors,
        )

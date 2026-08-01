from pathlib import Path

from app.services.filesystem import scan_library
from app.services.path_security import UnsafePathError, relative_album_path, resolve_album_path


def test_scan_finds_direct_audio_and_defers_temporary_album(tmp_path: Path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"art")
    result = scan_library(tmp_path, [".flac"], [], [".part"], False, True)
    assert set(result.albums) == {"Artist/Album"}
    assert result.albums["Artist/Album"].files[0].relative_path == "01.flac"
    assert "Artist/Album/cover.jpg" in result.unsupported_files

    (album / "02.flac.part").write_bytes(b"partial")
    result = scan_library(tmp_path, [".flac"], [], [".part"], False, True)
    assert result.albums["Artist/Album"].has_temporary_files is True


def test_symlink_escape_is_not_traversed_by_default(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.flac").write_bytes(b"secret")
    root = tmp_path / "music"
    root.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    result = scan_library(root, [".flac"], [], [], False, True)
    assert not result.albums


def test_path_security_rejects_traversal_and_symlink_escape(tmp_path: Path):
    root = tmp_path / "music"
    root.mkdir()
    (root / "Album").mkdir()
    assert relative_album_path("Album") == "Album"
    try:
        relative_album_path("../outside")
        raise AssertionError("expected traversal rejection")
    except UnsafePathError:
        pass
    assert resolve_album_path(root, "Album") == (root / "Album").resolve()

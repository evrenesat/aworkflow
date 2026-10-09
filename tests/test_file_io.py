from __future__ import annotations

from contextlib import contextmanager
import os
import stat
from pathlib import Path

import pytest

from aflow import file_io
from aflow.file_io import (
    atomic_replace_file,
    create_exclusive_file,
    fsync_directory,
    read_bounded_regular_file,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _leftover_temporaries(path: Path) -> list[Path]:
    prefix = f".{path.name}."
    return [entry for entry in path.parent.iterdir() if entry.name.startswith(prefix)]


@contextmanager
def _pinned_umask(mask: int):
    """Pin the process umask so explicit mode assertions are deterministic."""
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


class _FailingWriteHandle:
    """Context-manager handle whose first write raises an I/O error."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def __enter__(self) -> "_FailingWriteHandle":
        return self

    def __exit__(self, *exc_info: object) -> None:
        os.close(self._descriptor)

    def write(self, data: bytes) -> int:
        raise OSError("injected write failure")

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return self._descriptor


# -- create_exclusive_file ---------------------------------------------------


def test_create_exclusive_file_writes_exact_bytes_and_mode(tmp_path: Path) -> None:
    with _pinned_umask(0o022):
        target = tmp_path / "receipt.bin"
        create_exclusive_file(target, b"exact bytes", mode=0o640)
    assert target.read_bytes() == b"exact bytes"
    assert _mode(target) == 0o640
    assert not _leftover_temporaries(target)


def test_create_exclusive_file_default_mode_follows_umask(tmp_path: Path) -> None:
    with _pinned_umask(0o022):
        target = tmp_path / "receipt.bin"
        create_exclusive_file(target, b"data")
    assert _mode(target) == 0o644


def test_create_exclusive_file_rejects_existing_without_change(tmp_path: Path) -> None:
    target = tmp_path / "receipt.bin"
    target.write_bytes(b"original")
    original_stat = target.stat()
    with pytest.raises(FileExistsError):
        create_exclusive_file(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert target.stat().st_ino == original_stat.st_ino
    assert not _leftover_temporaries(target)


def test_create_exclusive_file_rejects_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"real")
    link = tmp_path / "link.bin"
    os.symlink(real, link)
    with pytest.raises(OSError):
        create_exclusive_file(link, b"via link")
    assert real.read_bytes() == b"real"
    assert not _leftover_temporaries(link)


def test_create_exclusive_file_write_failure_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "receipt.bin"
    monkeypatch.setattr(
        file_io.os, "fdopen", lambda descriptor, mode: _FailingWriteHandle(descriptor)
    )
    with pytest.raises(OSError, match="injected write failure"):
        create_exclusive_file(target, b"data")
    assert not target.exists()
    assert not _leftover_temporaries(target)


def test_create_exclusive_file_fsync_failure_before_publish_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "receipt.bin"

    def failing_fsync(descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(file_io.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        create_exclusive_file(target, b"data")
    assert not target.exists()
    assert not _leftover_temporaries(target)


def test_create_exclusive_file_publish_failure_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "receipt.bin"

    def failing_link(source: Path, destination: Path) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(file_io.os, "link", failing_link)
    with pytest.raises(OSError, match="injected publish failure"):
        create_exclusive_file(target, b"data")
    assert not target.exists()
    assert not _leftover_temporaries(target)


def test_create_exclusive_file_directory_sync_failure_keeps_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "receipt.bin"

    def compose() -> None:
        create_exclusive_file(target, b"data")
        fsync_directory(target.parent)

    calls = {"count": 0}

    def failing_directory_fsync(descriptor: int) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected directory fsync failure")

    monkeypatch.setattr(file_io.os, "fsync", failing_directory_fsync)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        compose()
    # The file publish happened before the directory sync failed; the
    # published file must remain.
    assert target.read_bytes() == b"data"
    assert not _leftover_temporaries(target)


# -- atomic_replace_file -----------------------------------------------------


def test_atomic_replace_file_replaces_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    atomic_replace_file(target, b"new")
    assert target.read_bytes() == b"new"
    assert not _leftover_temporaries(target)


def test_atomic_replace_file_honors_explicit_mode(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_replace_file(target, b"new", mode=0o600)
    assert _mode(target) == 0o600


def test_atomic_replace_file_fsync_failure_preserves_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    def failing_fsync(descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(file_io.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        atomic_replace_file(target, b"new")
    assert target.read_bytes() == b"old"
    assert not _leftover_temporaries(target)


def test_atomic_replace_file_publish_failure_preserves_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    def failing_replace(source: Path, destination: Path) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(file_io.os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected publish failure"):
        atomic_replace_file(target, b"new")
    assert target.read_bytes() == b"old"
    assert not _leftover_temporaries(target)


def test_atomic_replace_file_write_failure_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    monkeypatch.setattr(
        file_io.os, "fdopen", lambda descriptor, mode: _FailingWriteHandle(descriptor)
    )
    with pytest.raises(OSError, match="injected write failure"):
        atomic_replace_file(target, b"new")
    assert target.read_bytes() == b"old"
    assert not _leftover_temporaries(target)


# -- fsync_directory ---------------------------------------------------------


def test_fsync_directory_syncs_existing_directory(tmp_path: Path) -> None:
    fsync_directory(tmp_path)


def test_fsync_directory_propagates_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fsync_directory(tmp_path / "absent")


# -- read_bounded_regular_file -------------------------------------------------


def test_read_bounded_regular_file_reads_exact_bytes(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"0123456789")
    assert read_bounded_regular_file(target, 10) == b"0123456789"
    assert read_bounded_regular_file(target, 100) == b"0123456789"


def test_read_bounded_regular_file_rejects_oversize(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"0123456789")
    with pytest.raises(ValueError, match="exceeds 9 bytes"):
        read_bounded_regular_file(target, 9)


def test_read_bounded_regular_file_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"data")
    link = tmp_path / "link.bin"
    os.symlink(real, link)
    with pytest.raises(ValueError, match="must not be a symlink"):
        read_bounded_regular_file(link, 100)


def test_read_bounded_regular_file_rejects_non_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a regular file"):
        read_bounded_regular_file(tmp_path, 100)


def test_read_bounded_regular_file_rejects_invalid_bound(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"data")
    with pytest.raises(ValueError, match="max_bytes"):
        read_bounded_regular_file(target, -1)
    with pytest.raises(ValueError, match="max_bytes"):
        read_bounded_regular_file(target, True)  # type: ignore[arg-type]


def test_read_bounded_regular_file_rejects_growing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"0123")

    real_lstat = os.lstat

    def growing_lstat(path: object, **kwargs: object) -> os.stat_result:
        info = real_lstat(path, **kwargs)  # type: ignore[arg-type]
        # Report a size within the bound, then grow the file before the read.
        if path == str(target) or Path(path) == target:
            target.write_bytes(b"0123456789")
        return os.stat_result(tuple(list(info)[:-1] + [3]))

    monkeypatch.setattr(file_io.os, "lstat", growing_lstat)
    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        read_bounded_regular_file(target, 5)

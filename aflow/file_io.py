"""Small explicit file operations shared by AFlow persistence callers.

Each operation carries exactly one guarantee set, with an explicit mode where
the operation creates a file:

- :func:`create_exclusive_file` creates one new regular file and never
  overwrites an existing path.
- :func:`atomic_replace_file` durably replaces one file without exposing a
  partial write.
- :func:`fsync_directory` syncs one directory.
- :func:`read_bounded_regular_file` reads one regular file up to an exact
  byte bound.

Callers keep their own validation, locks, schema checks, and transaction
ordering. They choose the durability order by composing these operations
(for example publishing first and then calling :func:`fsync_directory` only
when their contract requires a durable rename). A single-file helper never
makes a two-file save atomic.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

__all__ = [
    "atomic_replace_file",
    "create_exclusive_file",
    "fsync_directory",
    "read_bounded_regular_file",
]


def _temporary_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid4().hex}.tmp")


def _write_temporary(path: Path, data: bytes, *, mode: int) -> Path:
    """Write one owned temporary file in the target directory and fsync it.

    The temporary name is unique per call, created exclusively in the same
    directory as the final path, and removed if the write or file fsync
    fails. On success the caller owns publishing the temporary file.
    """
    temporary = _temporary_path_for(path)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return temporary


def _remove_temporary(temporary: Path) -> None:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass


def create_exclusive_file(path: Path, data: bytes, *, mode: int = 0o666) -> None:
    """Durable exclusive creation of one new regular file.

    The bytes are written to an owned temporary file in the target directory,
    fsynced, then published with ``os.link`` so the final name appears
    atomically and only when it does not already exist. ``FileExistsError``
    is raised when the target path is present; the existing bytes are never
    changed and the temporary file is removed on every failure path. The
    caller remains responsible for directory synchronization.
    """
    temporary = _write_temporary(path, data, mode=mode)
    try:
        os.link(temporary, path)
    finally:
        _remove_temporary(temporary)


def atomic_replace_file(path: Path, data: bytes, *, mode: int = 0o666) -> None:
    """Durable atomic replacement of one file.

    The bytes are written to an owned temporary file in the target directory,
    fsynced, then published with ``os.replace``. No partial final write is
    ever exposed, and the temporary file is removed on every failure path.
    The published file carries ``mode`` (subject to the process umask);
    callers that require a durable rename additionally call
    :func:`fsync_directory` on the target directory.
    """
    temporary = _write_temporary(path, data, mode=mode)
    try:
        os.replace(temporary, path)
    finally:
        _remove_temporary(temporary)


def fsync_directory(path: Path) -> None:
    """Synchronize one directory, propagating I/O failures to the caller.

    This helper never swallows a failure silently; the open, fsync, and
    close errors all reach the caller. Callers whose established contract
    tolerates directory-sync failures (for example ``plan_backups``) catch
    ``OSError`` at the call site. runlog keeps a narrower domain adapter
    (``runlog._sync_runlog_directory``) that tolerates only the directory
    open and still propagates fsync and close failures.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_bounded_regular_file(path: Path, max_bytes: int) -> bytes:
    """Read one non-symlink regular file of at most ``max_bytes`` bytes.

    Raises ``ValueError`` when ``max_bytes`` is not a non-negative integer,
    when the path is a symlink or not a regular file, or when the file
    exceeds the bound. The size is checked before and during the read so a
    growing file cannot slip past the bound between the check and the read.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"cannot stat file: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"file must not be a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"file must be a regular file: {path}")
    if info.st_size > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    return data

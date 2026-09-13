"""Durable, per-issue receipts for the one-shot issue intake command.

The intake command deliberately keeps its durable state separate from AFlow's
run artifacts.  This module owns only the small receipt and lock protocol:
numeric repository/issue identities address one receipt, an advisory lock is
held for the command lifetime, and completed JSON is atomically published.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterator, Mapping


RECEIPT_SCHEMA_VERSION = 1
RECEIPT_MAX_BYTES = 128 * 1024
RECEIPT_STATES = frozenset(
    {
        "claimed",
        "planning",
        "plan_ready",
        "dispatching",
        "started",
        "ignored",
        "needs_attention",
    }
)


class IntakeStoreError(ValueError):
    """The receipt store is unavailable or contains unsafe state."""


class ReceiptCorruptionError(IntakeStoreError):
    """A receipt is not a valid version-1 intake record."""


def _numeric_component(value: int, *, label: str) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > (2**63 - 1)
    ):
        raise IntakeStoreError(f"{label} must be a positive integer")
    return str(value)


def _check_regular(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise IntakeStoreError(f"{label} does not exist") from exc
    except OSError as exc:
        raise IntakeStoreError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise IntakeStoreError(f"{label} must be a private regular file")
    return metadata


def _check_directory(path: Path, *, label: str, create: bool) -> Path:
    if path.is_symlink():
        raise IntakeStoreError(f"{label} must not be a symlink")
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise IntakeStoreError(f"{label} is unavailable") from exc
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise IntakeStoreError(f"{label} does not exist") from exc
    except OSError as exc:
        raise IntakeStoreError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise IntakeStoreError(f"{label} must be a directory")
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise IntakeStoreError(f"{label} permissions are unavailable") from exc
    return path


def _reject_path_escape(path: Path, root: Path, *, label: str) -> None:
    if path.is_symlink():
        raise IntakeStoreError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise IntakeStoreError(f"{label} escapes the state root") from exc


def _json_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptCorruptionError("receipt contains duplicate JSON keys")
        result[key] = value
    return result


def _bounded_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IntakeStoreError("receipt is not JSON-safe") from exc
    if len(encoded) > RECEIPT_MAX_BYTES:
        raise IntakeStoreError("receipt exceeds the size limit")
    return encoded


class IssueIntakeStore:
    """Store one private receipt and lock namespace per repository/issue."""

    def __init__(self, state_root: Path) -> None:
        candidate = Path(state_root).expanduser()
        if not candidate.is_absolute():
            raise IntakeStoreError("state root must be absolute")
        if candidate.is_symlink():
            raise IntakeStoreError("state root must not be a symlink")
        self.root = _check_directory(candidate, label="state root", create=True)
        self._receipts = _check_directory(
            self.root / "receipts", label="receipt directory", create=True
        )
        self._locks = _check_directory(
            self.root / "locks", label="lock directory", create=True
        )

    def _repository_directory(self, parent: Path, repository_id: int) -> Path:
        component = _numeric_component(repository_id, label="repository id")
        directory = parent / component
        _reject_path_escape(directory, self.root, label="repository state path")
        return _check_directory(directory, label="repository state directory", create=True)

    def receipt_path(self, repository_id: int, issue_number: int) -> Path:
        repository = self._repository_directory(self._receipts, repository_id)
        issue = _numeric_component(issue_number, label="issue number")
        path = repository / f"{issue}.json"
        _reject_path_escape(path, self.root, label="receipt path")
        return path

    def lock_path(self, repository_id: int, issue_number: int) -> Path:
        repository = self._repository_directory(self._locks, repository_id)
        issue = _numeric_component(issue_number, label="issue number")
        path = repository / f"{issue}.lock"
        _reject_path_escape(path, self.root, label="lock path")
        return path

    @contextmanager
    def issue_lock(self, repository_id: int, issue_number: int) -> Iterator[None]:
        """Hold the one per-issue advisory lock until the caller finishes."""
        path = self.lock_path(repository_id, issue_number)
        flags = os.O_RDWR | os.O_CREAT
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags | nofollow, 0o600)
        except OSError as exc:
            raise IntakeStoreError("issue lock is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise IntakeStoreError("issue lock must be a private regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def read(self, repository_id: int, issue_number: int) -> dict[str, object] | None:
        path = self.receipt_path(repository_id, issue_number)
        if path.is_symlink():
            raise IntakeStoreError("receipt must not be a symlink")
        if not path.exists():
            return None
        metadata = _check_regular(path, label="receipt")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ReceiptCorruptionError("receipt permissions must be 0600")
        if metadata.st_size > RECEIPT_MAX_BYTES:
            raise ReceiptCorruptionError("receipt exceeds the size limit")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise IntakeStoreError("receipt is unavailable") from exc
        if len(data) > RECEIPT_MAX_BYTES:
            raise ReceiptCorruptionError("receipt exceeds the size limit")
        try:
            value = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_json_without_duplicates,
            )
        except (UnicodeError, json.JSONDecodeError, ReceiptCorruptionError) as exc:
            raise ReceiptCorruptionError("receipt is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ReceiptCorruptionError("receipt must be a JSON object")
        if (
            type(value.get("schema_version")) is not int
            or value.get("schema_version") != RECEIPT_SCHEMA_VERSION
        ):
            raise ReceiptCorruptionError("receipt schema version is unsupported")
        state = value.get("state")
        if not isinstance(state, str) or state not in RECEIPT_STATES:
            raise ReceiptCorruptionError("receipt state is invalid")
        if (
            type(value.get("repository_id")) is not int
            or type(value.get("issue_number")) is not int
            or value.get("repository_id") != repository_id
            or value.get("issue_number") != issue_number
        ):
            raise ReceiptCorruptionError("receipt identity does not match its path")
        return value

    def write(self, repository_id: int, issue_number: int, payload: Mapping[str, Any]) -> Path:
        path = self.receipt_path(repository_id, issue_number)
        if path.exists() or path.is_symlink():
            _check_regular(path, label="receipt")
        if not isinstance(payload, Mapping):
            raise ReceiptCorruptionError("receipt must be a JSON object")
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        ):
            raise ReceiptCorruptionError("receipt schema version is unsupported")
        if payload.get("state") not in RECEIPT_STATES:
            raise ReceiptCorruptionError("receipt state is invalid")
        if (
            type(payload.get("repository_id")) is not int
            or type(payload.get("issue_number")) is not int
            or payload.get("repository_id") != repository_id
            or payload.get("issue_number") != issue_number
        ):
            raise ReceiptCorruptionError("receipt identity does not match its path")
        encoded = _bounded_json_bytes(payload)
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if path.is_symlink():
                raise IntakeStoreError("receipt must not be a symlink")
            if path.exists():
                _check_regular(path, label="receipt")
            os.replace(temporary, path)
            temporary = None
            _fsync_directory(path.parent)
        except IntakeStoreError:
            raise
        except OSError as exc:
            raise IntakeStoreError("receipt publication failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return path


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise IntakeStoreError("receipt directory synchronization failed") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise IntakeStoreError("receipt directory synchronization failed") from exc
    finally:
        os.close(descriptor)


__all__ = [
    "IssueIntakeStore",
    "IntakeStoreError",
    "RECEIPT_MAX_BYTES",
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_STATES",
    "ReceiptCorruptionError",
]

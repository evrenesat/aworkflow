"""Core owner for the workflow configuration pair's lock and crash-safe writes.

The pair is ``aflow.toml`` plus its optional sibling ``workflows.toml`` in one
directory.  Every supported reader and writer acquires the pair lock through
this module, and every write goes through :func:`commit_configuration_pair`,
which records a bounded version-1 write-ahead journal
(``.aflow-config-pair.transaction.json``) in the same directory before any
document is replaced.

Recovery (:func:`recover_pending_transaction`) is idempotent and must run
while the pair lock is held: before the durable committed marker it restores
both old documents; after it it installs and verifies both new documents,
then removes the record.  A current document whose bytes match neither
recorded generation is treated as a third-party edit: recovery fails with a
bounded explicit error and preserves both the record and the file bytes.

Record format (exact top-level fields, no arbitrary target paths):

.. code-block:: json

    {
      "schema_version": 1,
      "phase": "prepared" | "committed",
      "documents": {
        "aflow.toml": {
          "old": null | {"base64": "...", "sha256": "..."},
          "new": {"base64": "...", "sha256": "..."}
        },
        "workflows.toml": { ... }
      }
    }

``old`` is ``null`` for a document that did not exist before the commit
(a missing ``workflows.toml`` is a supported pre-save state).
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import errno
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Callable, Iterator, Mapping

logger = logging.getLogger(__name__)

DOCUMENT_NAMES = ("aflow.toml", "workflows.toml")
PAIR_LOCK_NAME = ".aflow-config-pair.lock"
TRANSACTION_RECORD_NAME = ".aflow-config-pair.transaction.json"
SCHEMA_VERSION = 1
MAX_CONFIG_DOCUMENT_BYTES = 256 * 1024
MAX_TRANSACTION_RECORD_BYTES = 6 * MAX_CONFIG_DOCUMENT_BYTES + 16 * 1024

_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_FIELDS = frozenset({"old", "new"})
_PAYLOAD_FIELDS = frozenset({"base64", "sha256"})
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "phase", "documents"})
_PHASES = ("prepared", "committed")


class ConfigPairError(RuntimeError):
    """A bounded configuration pair lock, record, or recovery failure."""


class ConfigPairRevisionConflict(ConfigPairError):
    """The expected revision no longer matches the committed pair."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("configuration revision does not match the committed revision")
        self.current_revision = current_revision


@dataclass(frozen=True)
class PairDocumentTransaction:
    """The recorded old and new generations for one document."""

    old_bytes: bytes | None
    new_bytes: bytes


@dataclass(frozen=True)
class ConfigPairTransaction:
    """One validated write-ahead record for the configuration pair."""

    phase: str
    documents: dict[str, PairDocumentTransaction]


def pair_revision(aflow_bytes: bytes, workflows_bytes: bytes) -> str:
    """Compute the compare-and-swap revision over the exact pair bytes."""
    digest = sha256()
    digest.update(b"aflow.toml\x00")
    digest.update(aflow_bytes)
    digest.update(b"\x00workflows.toml\x00")
    digest.update(workflows_bytes)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Pair lock
# ---------------------------------------------------------------------------

# Locks the calling thread already holds, keyed by (thread, resolved lock
# path).  flock is per open file description, so a second acquisition from the
# same thread would deadlock; a reader that already holds the lock re-enters
# instead.  Other threads still block as before.
_HELD_LOCKS: dict[tuple[int, str], Path] = {}


def _lock_key(lock_path: Path) -> tuple[int, str]:
    return threading.get_ident(), os.path.realpath(str(lock_path))


def _open_lock_file(pair_dir: Path) -> tuple[int, Path]:
    pair_dir.mkdir(parents=True, exist_ok=True)
    lock_path = pair_dir / PAIR_LOCK_NAME
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EPERM, errno.EACCES):
            raise
        # Read-only degradation (for example a hardened deployment whose unit
        # cannot write the global configuration directory): honor an existing
        # lock file; with none available the caller proceeds unlocked because
        # writes are impossible there anyway.
        existing = pair_dir / PAIR_LOCK_NAME
        if existing.is_file() and not existing.is_symlink():
            return os.open(existing, os.O_WRONLY | os.O_NOFOLLOW), existing
        raise _NoLockAvailable from None
    return fd, lock_path


class _NoLockAvailable(Exception):
    """Internal marker: no lock file is available on this read-only path."""


@contextmanager
def configuration_pair_lock(pair_dir: Path) -> Iterator[Path]:
    """Serialize reads and saves for one configuration pair.

    Yields the lock file path.  A read-only file system without a lock file
    degrades to a best-effort unlocked read (writes are impossible there).
    Re-entering the lock from the same process yields the held lock instead
    of deadlocking on a second flock.
    """
    pair_dir = Path(pair_dir)
    key = _lock_key(pair_dir / PAIR_LOCK_NAME)
    existing = _HELD_LOCKS.get(key)
    if existing is not None:
        yield existing
        return
    try:
        fd, lock_path = _open_lock_file(pair_dir)
    except _NoLockAvailable:
        yield None
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    _HELD_LOCKS[key] = lock_path
    try:
        yield lock_path
    finally:
        _HELD_LOCKS.pop(key, None)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def configuration_pair_lock_nonblocking(pair_dir: Path) -> Iterator[Path | None]:
    """Acquire the configuration pair lock without blocking.

    Yields ``None`` when the lock is already held by another process (or
    unavailable on a read-only file system).  Callers inside a
    control-servicing wait loop must treat a ``None`` yield as "yield to the
    loop" and keep polling; they must never fall back to an unlocked read.
    """
    pair_dir = Path(pair_dir)
    key = _lock_key(pair_dir / PAIR_LOCK_NAME)
    existing = _HELD_LOCKS.get(key)
    if existing is not None:
        yield existing
        return
    try:
        fd, lock_path = _open_lock_file(pair_dir)
    except _NoLockAvailable:
        yield None
        return
    import fcntl

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        _HELD_LOCKS[key] = lock_path
        try:
            yield lock_path
        finally:
            _HELD_LOCKS.pop(key, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Write-ahead record
# ---------------------------------------------------------------------------


def transaction_record_path(pair_dir: Path) -> Path:
    return Path(pair_dir) / TRANSACTION_RECORD_NAME


def canonical_pair_directory(config_path: Path) -> Path | None:
    """Return the canonical directory behind a supported whole-pair alias.

    A supported alias directory holds ``aflow.toml`` and ``workflows.toml``
    as leaf symlinks that both resolve to the canonical documents in one
    other directory.  That directory is the pair's transaction owner.  Any
    other selection — regular files, a missing sibling, a separately
    selected sibling, targets in different directories, non-canonical target
    names, a target in the supplied directory itself, or an inspection I/O
    failure — returns ``None`` so the supplied directory remains the
    transaction owner and the existing selection semantics are unchanged.
    """
    aflow = Path(config_path)
    sibling = aflow.with_name("workflows.toml")
    try:
        aflow_info = aflow.lstat()
        sibling_info = sibling.lstat()
    except (FileNotFoundError, OSError):
        return None
    if not stat.S_ISLNK(aflow_info.st_mode) or not stat.S_ISLNK(sibling_info.st_mode):
        return None
    aflow_target = aflow.resolve()
    sibling_target = sibling.resolve()
    if aflow_target.name != "aflow.toml" or sibling_target.name != "workflows.toml":
        return None
    target_dir = sibling_target.parent
    if aflow_target.parent != target_dir:
        return None
    if target_dir == aflow.parent.resolve():
        return None
    return target_dir


def _encode_payload(payload: bytes | None) -> dict | None:
    if payload is None:
        return None
    return {
        "base64": base64.b64encode(payload).decode("ascii"),
        "sha256": sha256(payload).hexdigest(),
    }


def _record_bytes(
    phase: str,
    old: Mapping[str, bytes | None],
    new: Mapping[str, bytes],
) -> bytes:
    documents = {
        name: {"old": _encode_payload(old[name]), "new": _encode_payload(new[name])}
        for name in DOCUMENT_NAMES
    }
    payload = json.dumps(
        {"schema_version": SCHEMA_VERSION, "phase": phase, "documents": documents},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_TRANSACTION_RECORD_BYTES:
        raise ConfigPairError("configuration transaction record exceeds the supported size")
    return payload


def _decode_payload(field: str, entry: object, *, allow_null: bool) -> bytes | None:
    if entry is None:
        if not allow_null:
            raise ConfigPairError(f"transaction record field {field} is missing")
        return None
    if not isinstance(entry, dict) or frozenset(entry) != _PAYLOAD_FIELDS:
        raise ConfigPairError(f"transaction record field {field} is malformed")
    encoded = entry["base64"]
    digest = entry["sha256"]
    if (
        not isinstance(encoded, str)
        or not isinstance(digest, str)
        or _HEX_SHA256_RE.fullmatch(digest) is None
    ):
        raise ConfigPairError(f"transaction record field {field} is malformed")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigPairError(f"transaction record field {field} is malformed") from exc
    if len(payload) > MAX_CONFIG_DOCUMENT_BYTES:
        raise ConfigPairError(f"transaction record field {field} exceeds the supported size")
    if sha256(payload).hexdigest() != digest:
        raise ConfigPairError(f"transaction record field {field} digest does not match")
    return payload


def parse_transaction_record(raw: bytes) -> ConfigPairTransaction:
    """Strictly parse and validate one write-ahead record.

    Rejects unknown schema versions, unknown fields, unsupported document
    names, oversized payloads, and invalid digests.  Never raises anything
    but :class:`ConfigPairError`.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ConfigPairError("configuration transaction record is malformed")
    if len(raw) > MAX_TRANSACTION_RECORD_BYTES:
        raise ConfigPairError("configuration transaction record exceeds the supported size")
    try:
        parsed = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigPairError("configuration transaction record is malformed") from exc
    if not isinstance(parsed, dict) or frozenset(parsed) != _TOP_LEVEL_FIELDS:
        raise ConfigPairError("configuration transaction record is malformed")
    if parsed["schema_version"] is not SCHEMA_VERSION:
        raise ConfigPairError("configuration transaction record: unsupported schema version")
    phase = parsed["phase"]
    if phase not in _PHASES:
        raise ConfigPairError("configuration transaction record is malformed")
    documents_raw = parsed["documents"]
    if not isinstance(documents_raw, dict) or set(documents_raw) != set(DOCUMENT_NAMES):
        raise ConfigPairError("configuration transaction record is malformed")
    documents: dict[str, PairDocumentTransaction] = {}
    for name in DOCUMENT_NAMES:
        entry = documents_raw[name]
        if not isinstance(entry, dict) or frozenset(entry) != _DOCUMENT_FIELDS:
            raise ConfigPairError("configuration transaction record is malformed")
        old_bytes = _decode_payload(f"documents.{name}.old", entry["old"], allow_null=True)
        new_bytes = _decode_payload(f"documents.{name}.new", entry["new"], allow_null=False)
        documents[name] = PairDocumentTransaction(old_bytes=old_bytes, new_bytes=new_bytes)
    return ConfigPairTransaction(phase=phase, documents=documents)


def _atomic_write_file(path: Path, payload: bytes, mode: int) -> None:
    """Write ``payload`` to ``path`` durably, replacing it atomically.

    The staging name is unique per call, so concurrent and repeated writers
    cannot collide, and only this helper's own unpublished staging file is
    removed when a write, fsync, or replace fails.  A transient failure
    therefore never blocks a healthy same-process retry.
    """
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp = Path(temp_name)
    try:
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    """Durable-sync the directory containing ``path``.

    Failed open/fsync are propagated: a commit must not acknowledge a
    generation whose directory durability was not established.  Pure reads
    without a pending transaction never call this helper.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_record(
    record_path: Path,
    phase: str,
    old: Mapping[str, bytes | None],
    new: Mapping[str, bytes],
) -> None:
    _atomic_write_file(record_path, _record_bytes(phase, old, new), 0o600)
    _fsync_directory(record_path.parent)


def _read_document(pair_dir: Path, name: str) -> bytes | None:
    """Read one current document; ``None`` when it does not exist yet."""
    path = pair_dir / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigPairError(f"{name} is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ConfigPairError(f"{name} must be a regular file")
    if info.st_size > MAX_CONFIG_DOCUMENT_BYTES:
        raise ConfigPairError(f"{name} exceeds the maximum supported size")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ConfigPairError(f"{name} is unreadable") from exc
    if len(payload) > MAX_CONFIG_DOCUMENT_BYTES:
        raise ConfigPairError(f"{name} exceeds the maximum supported size")
    return payload


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def recover_pending_transaction(
    pair_dir: Path,
    *,
    barrier: Callable[[str], None] | None = None,
) -> None:
    """Complete one interrupted pair save; the caller holds the pair lock.

    Before the durable committed marker the old documents are restored;
    after it the new documents are installed and verified.  Repeating the
    call is idempotent.  A third-party edit (a document whose bytes match
    neither recorded generation, an unsafe link, a malformed record, or a
    missing document that had an old generation) raises
    :class:`ConfigPairError` and preserves the record and file bytes.

    ``barrier`` is an optional crash-injection hook for tests; it is called
    with a point name after each durable step and never in production.
    """
    pair_dir = Path(pair_dir)
    record_path = transaction_record_path(pair_dir)
    try:
        record_info = record_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConfigPairError("configuration transaction record is unavailable") from exc
    if not stat.S_ISREG(record_info.st_mode):
        raise ConfigPairError("configuration transaction record must be a regular file")
    if record_info.st_size > MAX_TRANSACTION_RECORD_BYTES:
        raise ConfigPairError("configuration transaction record exceeds the supported size")
    try:
        raw = record_path.read_bytes()
    except OSError as exc:
        raise ConfigPairError("configuration transaction record is unreadable") from exc
    transaction = parse_transaction_record(raw)

    for name in DOCUMENT_NAMES:
        expected = transaction.documents[name]
        payload = _read_document(pair_dir, name)
        if payload is None:
            if expected.old_bytes is not None:
                raise ConfigPairError(
                    f"{name} changed while a configuration transaction was pending"
                )
            continue
        digest = sha256(payload).hexdigest()
        candidates = {sha256(expected.new_bytes).hexdigest()}
        if expected.old_bytes is not None:
            candidates.add(sha256(expected.old_bytes).hexdigest())
        if digest not in candidates:
            raise ConfigPairError(
                f"{name} changed while a configuration transaction was pending"
            )

    try:
        for name in DOCUMENT_NAMES:
            recorded = transaction.documents[name]
            target = (
                recorded.old_bytes if transaction.phase == "prepared" else recorded.new_bytes
            )
            path = pair_dir / name
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                current = None
            except OSError as exc:
                raise ConfigPairError(
                    "configuration transaction recovery failed"
                ) from exc
            if target is None:
                if current is not None:
                    path.unlink()
            elif current != target:
                _atomic_write_file(path, target, 0o644)
            _fsync_directory(pair_dir)
            if barrier is not None:
                barrier(f"after_document:{name}")
        record_path.unlink()
        _fsync_directory(pair_dir)
    except OSError as exc:
        raise ConfigPairError("configuration transaction recovery failed") from exc
    if barrier is not None:
        barrier("after_cleanup")


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def _stage_document(pair_dir: Path, name: str, payload: bytes) -> Path:
    """Durable-stage one new document under a unique task-local name.

    The staging file is left in place for the commit's atomic replacement.
    Unique names prevent collisions between documents, the transaction
    record, and rollback staging.
    """
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".staging.tmp", dir=str(pair_dir)
    )
    temp = Path(temp_name)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        os.chmod(temp, 0o644)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return temp


def _rollback_pre_commit(
    pair_dir: Path,
    record_path: Path,
    current: Mapping[str, bytes | None],
    payloads: Mapping[str, bytes],
    staged: Mapping[str, Path | None],
) -> None:
    """Restore the validated old generation after a pre-commit failure.

    Called with the pair lock still held.  Cleans only this commit's own
    staging files, then uses the transaction owner's validated
    old-generation recovery.  When the committed marker had become visible
    without its durable directory sync, the prepared record is durably
    re-established first so an interrupted rollback restarts as
    old-generation recovery.  A failed ownership validation, recovery, journal read, or prepared-record
    re-establishment preserves the journal and file evidence and raises
    :class:`ConfigPairError` without claiming any restoration.
    """
    for temp in staged.values():
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    try:
        if record_path.is_file():
            record = parse_transaction_record(record_path.read_bytes())
            if record.phase == "committed":
                _write_record(record_path, "prepared", current, payloads)
        recover_pending_transaction(pair_dir)
    except (OSError, ConfigPairError) as exc:
        raise ConfigPairError(
            "configuration pair commit failed and old-generation recovery "
            "is pending; no generation was acknowledged"
        ) from exc


def _complete_cleanup(record_path: Path, pair_dir: Path) -> None:
    """Remove the committed record and durable-sync the directory."""
    record_path.unlink()
    _fsync_directory(pair_dir)


def commit_configuration_pair(
    pair_dir: Path,
    *,
    payloads: Mapping[str, bytes],
    expected_revision: str | None = None,
    barrier: Callable[[str], None] | None = None,
) -> None:
    """Atomically commit one new generation of the configuration pair.

    The candidate contents are the caller's responsibility to validate
    (TOML and schema); this owner enforces document-name and byte limits and
    the expected-revision compare-and-swap.  The commit runs under the pair
    lock: recover, read the current documents, durably record the old and
    new generations, replace the changed documents, fsync the directory,
    durably mark the record committed, then remove the record.

    Any ordinary write failure before the durable committed marker runs the
    transaction owner's validated old-generation recovery under the still
    held pair lock and raises :class:`ConfigPairError`; the restoration is
    claimed only when that recovery verified it.  A third-party edit blocks
    the rollback with recovery left pending and the evidence preserved.  After
    the marker, a cleanup failure keeps the acknowledged generation, retains
    a committed journal for a later reader when the filesystem permits, and
    emits a bounded diagnostic without document payloads.

    ``barrier`` is an optional crash-injection hook for tests.
    """
    pair_dir = Path(pair_dir)
    if set(payloads) != set(DOCUMENT_NAMES):
        raise ValueError("payloads must cover exactly the supported configuration documents")
    for name in DOCUMENT_NAMES:
        payload = payloads[name]
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(f"{name} payload must be bytes")
        if len(payload) > MAX_CONFIG_DOCUMENT_BYTES:
            raise ConfigPairError(f"{name} exceeds the maximum supported size")

    with configuration_pair_lock(pair_dir):
        recover_pending_transaction(pair_dir)
        record_path = transaction_record_path(pair_dir)

        def at(point: str) -> None:
            if barrier is not None:
                barrier(point)

        current = {name: _read_document(pair_dir, name) for name in DOCUMENT_NAMES}
        if expected_revision is not None:
            revision = pair_revision(
                current["aflow.toml"] or b"", current["workflows.toml"] or b""
            )
            if revision != expected_revision:
                raise ConfigPairRevisionConflict(revision)

        staged: dict[str, Path | None] = {name: None for name in DOCUMENT_NAMES}
        try:
            _write_record(record_path, "prepared", current, payloads)
            at("after_record")
            for name in DOCUMENT_NAMES:
                if bytes(payloads[name]) != current[name]:
                    staged[name] = _stage_document(pair_dir, name, bytes(payloads[name]))
            for name in DOCUMENT_NAMES:
                temp = staged[name]
                if temp is not None:
                    os.replace(temp, pair_dir / name)
                    staged[name] = None
                    at(f"after_replacement:{name}")
            _fsync_directory(pair_dir)
            at("after_directory_sync")
            # The durable committed marker (record rename plus directory
            # sync) is the commit's success boundary.
            _write_record(record_path, "committed", current, payloads)
            at("after_committed_marker")
        except OSError as exc:
            _rollback_pre_commit(pair_dir, record_path, current, payloads, staged)
            raise ConfigPairError(
                "configuration pair commit failed; the previous pair was restored"
            ) from exc
        finally:
            for temp in staged.values():
                if temp is not None:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass

        try:
            _complete_cleanup(record_path, pair_dir)
        except OSError:
            # The acknowledged generation is durable in place; only the
            # bookkeeping record (or its directory sync) is incomplete.
            # Retain a committed journal for a later reader when the
            # filesystem permits.
            try:
                _write_record(record_path, "committed", current, payloads)
                logger.warning(
                    "configuration transaction record cleanup is incomplete; the "
                    "acknowledged generation is in place and a later read will "
                    "complete cleanup"
                )
            except OSError:
                logger.warning(
                    "configuration transaction record cleanup is incomplete and "
                    "journal retention could not be confirmed; the acknowledged "
                    "generation is in place"
                )
        at("after_cleanup")

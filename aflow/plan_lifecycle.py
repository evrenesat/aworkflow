"""Crash-recoverable moves for plans that need correction or failed execution.

The original plan's stable owner, not its filename or a repair overlay, is the
unit of lifecycle state.  A prepared journal entry precedes the no-clobber
file move; replay completes only that exact byte revision and owner.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterator, Literal
from uuid import uuid4

from aflow.plan_backups import (
    bind_unowned_plan_history,
    ensure_plan_identity,
    plan_identity_for_path,
    record_plan_lifecycle_move,
)


PlanLocation = Literal["in_progress", "failed", "needs_plan_change"]
_DIRECTORIES = {
    "in_progress": "in-progress",
    "failed": "failed",
    "needs_plan_change": "needs-plan-change",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.md$")
_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_PLAN_BYTES = 256 * 1024
_MAX_JOURNAL_BYTES = 16 * 1024


class PlanLifecycleError(RuntimeError):
    """A lifecycle move cannot be proven safe."""


class PlanLifecycleConflict(PlanLifecycleError):
    """The source revision or destination is no longer the requested one."""


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_bytes(path: Path, *, allow_linked: bool = False) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanLifecycleError("plan entry is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink < 1
        or (info.st_nlink != 1 and not allow_linked)
        or info.st_size > _MAX_PLAN_BYTES
    ):
        raise PlanLifecycleError("plan entry must be a bounded regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(_MAX_PLAN_BYTES + 1)
        if len(data) > _MAX_PLAN_BYTES:
            raise PlanLifecycleError("plan exceeds the size limit")
        data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise PlanLifecycleError("plan bytes are unavailable") from exc
    return data


class PlanLifecycle:
    """Move one exact plan revision under a project-wide filesystem lock."""

    def __init__(self, root: Path) -> None:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise PlanLifecycleError("project root is unavailable")
        self.root = root.resolve(strict=True)

    def _directory(self, location: PlanLocation, *, create: bool) -> Path:
        if location not in _DIRECTORIES:
            raise PlanLifecycleError("plan location is invalid")
        plans = self.root / "plans"
        directory = plans / _DIRECTORIES[location]
        for candidate in (plans, directory):
            if candidate.is_symlink():
                raise PlanLifecycleError("plan directory must not be a symlink")
            if create:
                candidate.mkdir(exist_ok=True)
            if not candidate.is_dir() or candidate.resolve(strict=True).parent not in {
                self.root, plans,
            }:
                raise PlanLifecycleError("plan directory is unavailable")
        return directory

    def path(self, location: PlanLocation, name: str, *, create: bool = False) -> Path:
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise PlanLifecycleError("plan filename is invalid")
        return self._directory(location, create=create) / name

    @contextmanager
    def _locked(self) -> Iterator[Path]:
        private = self.root / ".aflow"
        if private.is_symlink():
            raise PlanLifecycleError("plan journal directory is unsafe")
        private.mkdir(exist_ok=True)
        journal = private / "plan-lifecycle"
        if journal.is_symlink():
            raise PlanLifecycleError("plan journal directory is unsafe")
        journal.mkdir(exist_ok=True)
        lock_path = private / "plan-lifecycle.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield journal
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _write(self, path: Path, record: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, path: Path) -> dict[str, object] | None:
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JOURNAL_BYTES:
            raise PlanLifecycleError("plan journal is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise PlanLifecycleError("plan journal is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != 1
            or value.get("phase") not in {"prepared", "moved"}
            or not isinstance(value.get("identity"), str)
            or _IDENTITY_RE.fullmatch(value["identity"]) is None
            or path.name != f"{value['identity']}.json"
            or not isinstance(value.get("name"), str)
            or _NAME_RE.fullmatch(value["name"]) is None
            or value.get("source") not in _DIRECTORIES
            or value.get("destination") not in _DIRECTORIES
            or not isinstance(value.get("revision"), str)
            or _REVISION_RE.fullmatch(value["revision"]) is None
            or type(value.get("source_dev")) is not int
            or type(value.get("source_inode")) is not int
            or value["source_dev"] < 0
            or value["source_inode"] <= 0
        ):
            raise PlanLifecycleError("plan journal has invalid identity")
        return value

    def _complete(self, record: dict[str, object], journal_path: Path) -> Path:
        source = self.path(record["source"], record["name"])
        destination = self.path(record["destination"], record["name"], create=True)
        revision = record["revision"]
        source_bytes = _regular_bytes(source, allow_linked=True)
        destination_bytes = _regular_bytes(destination, allow_linked=True)
        expected_file = (record["source_dev"], record["source_inode"])
        if source_bytes is not None:
            source_info = source.lstat()
            if (source_info.st_dev, source_info.st_ino) != expected_file:
                raise PlanLifecycleConflict("source plan identity changed during its lifecycle move")
        if destination_bytes is not None:
            destination_info = destination.lstat()
            if (destination_info.st_dev, destination_info.st_ino) != expected_file:
                raise PlanLifecycleConflict("destination plan already exists")
        if source_bytes is not None and hashlib.sha256(source_bytes).hexdigest() != revision:
            raise PlanLifecycleConflict("source plan changed during its lifecycle move")
        if destination_bytes is not None and hashlib.sha256(destination_bytes).hexdigest() != revision:
            raise PlanLifecycleConflict("destination plan collides with lifecycle move")
        if source_bytes is not None and destination_bytes is not None:
            source_info, destination_info = source.stat(), destination.stat()
            if (source_info.st_dev, source_info.st_ino) != (destination_info.st_dev, destination_info.st_ino):
                raise PlanLifecycleConflict("destination plan already exists")
        elif source_bytes is not None:
            source_info = source.lstat()
            try:
                os.link(source, destination, follow_symlinks=False)
            except FileExistsError as exc:
                raise PlanLifecycleConflict("destination plan already exists") from exc
            _fsync_dir(destination.parent)
            linked_info = destination.lstat()
            current_source_info = source.lstat()
            linked_bytes = (
                _regular_bytes(destination, allow_linked=True)
                if stat.S_ISREG(linked_info.st_mode) else None
            )
            if (
                not stat.S_ISREG(source_info.st_mode)
                or (source_info.st_dev, source_info.st_ino)
                != (linked_info.st_dev, linked_info.st_ino)
                or (source_info.st_dev, source_info.st_ino)
                != (current_source_info.st_dev, current_source_info.st_ino)
                or linked_bytes is None
                or hashlib.sha256(linked_bytes).hexdigest() != revision
            ):
                # Remove only the link we just created. Keep a replacement
                # source or independently published destination untouched.
                current_destination_info = destination.lstat()
                if (
                    current_destination_info.st_dev,
                    current_destination_info.st_ino,
                ) == (linked_info.st_dev, linked_info.st_ino):
                    destination.unlink()
                    _fsync_dir(destination.parent)
                raise PlanLifecycleConflict("source plan changed during its lifecycle move")
        elif destination_bytes is None:
            raise PlanLifecycleConflict("recorded plan move has no source or destination")
        if source.exists() or source.is_symlink():
            source_info, destination_info = source.lstat(), destination.lstat()
            if (
                not stat.S_ISREG(source_info.st_mode)
                or (source_info.st_dev, source_info.st_ino)
                != (destination_info.st_dev, destination_info.st_ino)
            ):
                raise PlanLifecycleConflict("source plan changed during its lifecycle move")
            source.unlink()
            _fsync_dir(source.parent)
        try:
            record_plan_lifecycle_move(
                self.root, source_plan_path=source, destination_plan_path=destination
            )
        except OSError as exc:
            raise PlanLifecycleError("plan identity move is incomplete") from exc
        record["phase"] = "moved"
        record["current_location"] = record["destination"]
        self._write(journal_path, record)
        return destination

    def move(
        self,
        source: Path,
        destination: PlanLocation,
        *,
        expected_revision: str,
        reason_code: str,
        reason: str,
        source_run_id: str | None = None,
    ) -> Path:
        """Journal and complete an exact, no-clobber original-plan move."""
        if _REVISION_RE.fullmatch(expected_revision) is None:
            raise PlanLifecycleError("expected plan revision is invalid")
        if not isinstance(reason_code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code) is None:
            raise PlanLifecycleError("plan lifecycle reason code is invalid")
        if not isinstance(reason, str) or len(reason) > 256 or not reason.isprintable():
            raise PlanLifecycleError("plan lifecycle reason is invalid")
        if source_run_id is not None and (
            not isinstance(source_run_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", source_run_id) is None
        ):
            raise PlanLifecycleError("source run identity is invalid")
        source = Path(source)
        if not source.is_absolute() or source.name != source.name.strip():
            raise PlanLifecycleError("source plan path is invalid")
        source_location = next(
            (
                location for location, dirname in _DIRECTORIES.items()
                if source == self.root / "plans" / dirname / source.name
            ), None
        )
        if source_location is None or source_location == destination:
            raise PlanLifecycleError("source plan location is invalid")
        with self._locked() as journal:
            data = _regular_bytes(source)
            if data is None:
                # A replay may arrive after the move; match the journal's
                # exact prior source, target and revision before returning.
                for path in sorted(journal.glob("*.json")):
                    record = self._read(path)
                    if record is not None and (
                        record["name"] == source.name
                        and record["source"] == source_location
                        and record["destination"] == destination
                        and record["revision"] == expected_revision
                    ):
                        return self._complete(record, path)
                raise PlanLifecycleConflict("source plan is missing")
            if hashlib.sha256(data).hexdigest() != expected_revision:
                raise PlanLifecycleConflict("source plan revision changed")
            source_info = source.lstat()
            target = self.path(destination, source.name, create=True)
            if target.exists() or target.is_symlink():
                raise PlanLifecycleConflict("destination plan already exists")
            identity = plan_identity_for_path(self.root, source)
            if identity is None:
                identity = ensure_plan_identity(self.root, source)
                bind_unowned_plan_history(self.root, source, plan_identity_id=identity)
            journal_path = journal / f"{identity}.json"
            prior = self._read(journal_path)
            if prior is not None and prior["phase"] == "prepared":
                self._complete(prior, journal_path)
                raise PlanLifecycleConflict("a prior plan move completed; retry this request")
            original_path = (
                prior.get("original_path") if prior is not None
                else str(self.path("in_progress", source.name))
            )
            record: dict[str, object] = {
                "schema": 1, "identity": identity, "name": source.name,
                "source": source_location, "destination": destination,
                "original_path": original_path, "current_location": source_location,
                "revision": expected_revision, "phase": "prepared",
                "source_dev": source_info.st_dev, "source_inode": source_info.st_ino,
                "reason_code": reason_code, "reason": reason,
                "source_run_id": source_run_id or (prior.get("source_run_id") if prior else None),
            }
            self._write(journal_path, record)
            return self._complete(record, journal_path)

    def recover(self) -> tuple[Path, ...]:
        """Complete interrupted prepared moves without guessing new targets."""
        completed: list[Path] = []
        with self._locked() as journal:
            for path in sorted(journal.glob("*.json")):
                record = self._read(path)
                if record is not None and record["phase"] == "prepared":
                    completed.append(self._complete(record, path))
        return tuple(completed)

    def recover_for_path(self, plan_path: Path) -> tuple[Path, ...]:
        """Recover prepared moves touching one direct lifecycle plan path."""
        path = Path(plan_path)
        location = next(
            (
                key for key, directory in _DIRECTORIES.items()
                if path == self.root / "plans" / directory / path.name
            ), None
        )
        if location is None or _NAME_RE.fullmatch(path.name) is None:
            return ()
        completed: list[Path] = []
        with self._locked() as journal:
            for entry in sorted(journal.glob("*.json")):
                record = self._read(entry)
                if (
                    record is not None
                    and record["phase"] == "prepared"
                    and record["name"] == path.name
                    and location in {record["source"], record["destination"]}
                ):
                    completed.append(self._complete(record, entry))
        return tuple(completed)

    def record_for(self, plan_path: Path) -> dict[str, object] | None:
        """Read current lifecycle state for one stable plan owner."""
        identity = plan_identity_for_path(self.root, plan_path)
        if identity is None:
            return None
        with self._locked() as journal:
            return self._read(journal / f"{identity}.json")

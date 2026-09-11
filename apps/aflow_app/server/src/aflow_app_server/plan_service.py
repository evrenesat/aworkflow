"""Revisioned filesystem plan management for registered projects."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import os
from pathlib import Path
import re
import stat
from threading import RLock
from typing import Literal

from aflow.plan_backups import (
    backup_reference_for_plan,
    baseline_status_for_plan,
    backup_provenance_for_plan,
    bind_unowned_plan_history,
    create_plan_identity,
    ensure_plan_identity,
    mark_ready_baseline,
    plan_identity_for_path,
    record_plan_lifecycle_move,
    revert_plan_lifecycle_move,
)

from .project_registry import ProjectRegistry, ProjectRegistryError

PlanStatus = Literal["todo", "in_progress", "done"]
_STATUS_DIRS: dict[PlanStatus, str] = {"todo": "todo", "in_progress": "in-progress", "done": "done"}
_NEXT_STATUS: dict[PlanStatus, PlanStatus] = {"todo": "in_progress", "in_progress": "done"}
_PLAN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.md$")
_MAX_PLAN_BYTES = 256 * 1024

class PlanServiceError(RuntimeError):
    """A plan operation was unsafe, invalid, or could not be completed."""

class PlanProjectNotFound(PlanServiceError):
    """The requested project is absent from the canonical registry."""


class PlanNotFound(PlanServiceError):
    """The requested plan document does not exist."""


class PlanInvalid(PlanServiceError):
    """The requested plan path, content, or filesystem entry is unsafe."""


class PlanAlreadyExists(PlanServiceError):
    """The requested create or promotion target is already present."""


class PlanRevisionConflict(PlanServiceError):
    """The caller edited an outdated plan revision."""
    def __init__(self, current_revision: str) -> None:
        super().__init__("plan revision does not match")
        self.current_revision = current_revision


def _load_draft_template() -> str:
    """Load the packaged new-draft skeleton before any file is written.

    Only a null/omitted create payload reaches this loader; every explicit
    string (including empty text) stays authoritative with the caller.
    """
    try:
        return files("aflow").joinpath("templates/draft-plan.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PlanServiceError("plan template is unavailable") from exc

@dataclass(frozen=True)
class PlanDocument:
    project_id: str
    name: str
    path: str
    status: PlanStatus
    revision: str
    size_bytes: int
    content: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "project_id": self.project_id, "name": self.name, "path": self.path,
            "status": self.status, "revision": self.revision, "size_bytes": self.size_bytes,
        }
        if self.content is not None:
            payload["content"] = self.content
        return payload

class PlanService:
    """Manage only direct regular Markdown files in canonical plan directories."""

    def __init__(self, registry: ProjectRegistry, *, max_plan_bytes: int = _MAX_PLAN_BYTES) -> None:
        self._registry = registry
        self._max_plan_bytes = max_plan_bytes
        self._locks_guard = RLock()
        self._locks: dict[str, RLock] = {}

    def _project_lock(self, project_id: str) -> RLock:
        with self._locks_guard:
            return self._locks.setdefault(project_id, RLock())

    def list(self, project_id: str, status_filter: PlanStatus | None = None) -> tuple[PlanDocument, ...]:
        with self._project_lock(project_id):
            root = self._root(project_id)
            statuses = (status_filter,) if status_filter is not None else tuple(_STATUS_DIRS)
            documents: list[PlanDocument] = []
            for plan_status in statuses:
                directory = self._status_dir(root, plan_status, create=False)
                if directory is None:
                    continue
                for path in sorted(directory.iterdir(), key=lambda item: item.name):
                    try:
                        self._validate_name(path.name)
                        data = self._read_regular(path)
                    except PlanServiceError:
                        continue
                    documents.append(self._document(project_id, plan_status, path.name, data))
            return tuple(documents)

    def read(self, project_id: str, status_value: PlanStatus, name: str) -> PlanDocument:
        with self._project_lock(project_id):
            root = self._root(project_id)
            path = self._plan_path(root, status_value, name, create_dir=False)
            data = self._read_regular(path)
            return self._document(project_id, status_value, name, data, include_content=True)

    def list_backups(
        self,
        project_id: str,
        status_value: PlanStatus,
        name: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        """Return bounded provenance summaries for one current plan path."""
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 200
        ):
            raise PlanInvalid("backup pagination is invalid")
        with self._project_lock(project_id):
            root = self._root(project_id)
            path = self._plan_path(root, status_value, name, create_dir=False)
            self._read_regular(path)
            records = backup_provenance_for_plan(root, path)
            summaries: list[dict[str, object]] = []
            for backup_path, record in records:
                baseline_status = baseline_status_for_plan(root, path, record)
                reference = backup_reference_for_plan(root, path, record)
                summaries.append(
                    {
                        "backup_filename": backup_path.name,
                        "kind": record.get("kind"),
                        "baseline_status": baseline_status,
                        "capture_event": (
                            reference.get("event") if reference is not None else None
                        ),
                        "timestamp": (
                            reference.get("timestamp")
                            if reference is not None
                            else None
                        ),
                        "run_id": (
                            reference.get("run_id") if reference is not None else None
                        ),
                        "turn_number": (
                            reference.get("turn_number")
                            if reference is not None
                            else None
                        ),
                        "content_sha256": record.get("content_sha256"),
                    }
                )
            total_items = len(summaries)
            page = summaries[offset : offset + limit]
            next_offset = offset + limit if offset + limit < total_items else None
            return {
                "backups": page,
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset,
                "total_items": total_items,
            }

    def create(self, project_id: str, name: str, content: str | None = None) -> PlanDocument:
        if content is None:
            content = _load_draft_template()
        data = self._validate_content(content)
        with self._project_lock(project_id):
            root = self._root(project_id)
            path = self._plan_path(root, "todo", name, create_dir=True)
            if path.exists() or path.is_symlink():
                raise PlanAlreadyExists("plan already exists")
            self._atomic_write(path, data, replace=False)
            try:
                create_plan_identity(root, path)
            except OSError as exc:
                try:
                    path.unlink()
                    self._fsync_directory(path.parent)
                except OSError as rollback_exc:
                    raise PlanServiceError(
                        "plan identity rollback failed"
                    ) from rollback_exc
                raise PlanServiceError("plan identity unavailable") from exc
            return self._document(project_id, "todo", name, data, include_content=True)

    def update(self, project_id: str, status_value: PlanStatus, name: str, content: str, expected_revision: str) -> PlanDocument:
        data = self._validate_content(content)
        self._validate_revision(expected_revision)
        with self._project_lock(project_id):
            root = self._root(project_id)
            path = self._plan_path(root, status_value, name, create_dir=False)
            current = self._read_regular(path)
            self._require_revision(current, expected_revision)
            self._atomic_write(path, data, replace=True)
            return self._document(project_id, status_value, name, data, include_content=True)

    def promote(self, project_id: str, status_value: PlanStatus, name: str, expected_revision: str, target_name: str | None = None) -> PlanDocument:
        self._validate_revision(expected_revision)
        target_status = _NEXT_STATUS.get(status_value)
        if target_status is None:
            raise PlanInvalid("done plans cannot be promoted")
        target_name = target_name or name
        with self._project_lock(project_id):
            root = self._root(project_id)
            source = self._plan_path(root, status_value, name, create_dir=False)
            current = self._read_regular(source)
            self._require_revision(current, expected_revision)
            target = self._plan_path(root, target_status, target_name, create_dir=True)
            if target.exists() or target.is_symlink():
                raise PlanAlreadyExists("promotion target already exists")
            existing_source_identity = plan_identity_for_path(root, source)
            try:
                source_identity_id = ensure_plan_identity(root, source)
                if existing_source_identity is None:
                    bind_unowned_plan_history(
                        root,
                        source,
                        plan_identity_id=source_identity_id,
                    )
            except OSError as exc:
                raise PlanServiceError("plan promotion failed") from exc
            source_identity = self._file_identity(source, current)
            baseline_backup: Path | None = None
            if status_value == "todo":
                try:
                    from aflow.workflow import _backup_original_plan

                    baseline_backup = _backup_original_plan(
                        root,
                        source,
                        event="ready_promotion",
                    )
                    if baseline_backup.read_bytes() != current:
                        raise PlanServiceError("captured baseline bytes changed")
                except Exception as exc:
                    raise PlanServiceError("plan baseline capture failed") from exc
            moved = False
            try:
                os.replace(source, target)
                moved = True
                self._fsync_directory(target.parent)
                if source.parent != target.parent:
                    self._fsync_directory(source.parent)
                try:
                    if self._read_regular(target) != current:
                        raise OSError("promoted plan bytes changed")
                except PlanServiceError as exc:
                    raise OSError("promoted plan is not unchanged") from exc
                if baseline_backup is not None:
                    mark_ready_baseline(
                        root,
                        baseline_backup,
                        source_plan_path=source,
                        destination_plan_path=target,
                    )
                record_plan_lifecycle_move(
                    root,
                    source_plan_path=source,
                    destination_plan_path=target,
                )
            except OSError as exc:
                if moved:
                    metadata_rollback_error: OSError | None = None
                    try:
                        revert_plan_lifecycle_move(
                            root,
                            source_plan_path=source,
                            destination_plan_path=target,
                            baseline_backup_path=baseline_backup,
                        )
                    except OSError as rollback_exc:
                        metadata_rollback_error = rollback_exc
                    self._rollback_promote(
                        source,
                        target,
                        source_identity,
                    )
                    if metadata_rollback_error is not None:
                        raise PlanServiceError(
                            "plan promotion metadata rollback failed"
                        ) from metadata_rollback_error
                raise PlanServiceError("plan promotion failed") from exc
            return self._document(project_id, target_status, target_name, current, include_content=True)

    def _root(self, project_id: str) -> Path:
        try:
            _, root = self._registry.resolve(project_id)
        except ProjectRegistryError as exc:
            raise PlanProjectNotFound("project is not registered") from exc
        return root

    def _status_dir(self, root: Path, status_value: PlanStatus, *, create: bool) -> Path | None:
        if not isinstance(status_value, str) or status_value not in _STATUS_DIRS:
            raise PlanInvalid("plan status is invalid")
        plans = root / "plans"
        directory = plans / _STATUS_DIRS[status_value]
        for candidate in (plans, directory):
            if candidate.is_symlink():
                raise PlanInvalid("plan directory must not be a symlink")
        if create:
            try:
                plans.mkdir(exist_ok=True)
                directory.mkdir(exist_ok=True)
            except OSError as exc:
                raise PlanInvalid("plan directory is unavailable") from exc
        if not directory.exists():
            return None
        if not directory.is_dir():
            raise PlanInvalid("plan directory must be a directory")
        try:
            directory.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise PlanInvalid("plan directory is outside the project") from exc
        return directory

    def _plan_path(self, root: Path, status_value: PlanStatus, name: str, *, create_dir: bool) -> Path:
        self._validate_name(name)
        directory = self._status_dir(root, status_value, create=create_dir)
        if directory is None:
            raise PlanNotFound("plan not found")
        return directory / name

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or _PLAN_NAME_RE.fullmatch(name) is None:
            raise PlanInvalid("plan name must be a Markdown filename")
        if name in {".md", "..md"} or "/" in name or "\\" in name or "\x00" in name:
            raise PlanInvalid("plan name must be a Markdown filename")

    def _validate_content(self, content: str) -> bytes:
        if not isinstance(content, str) or "\x00" in content:
            raise PlanInvalid("plan content must be UTF-8 text")
        data = content.encode("utf-8")
        if len(data) > self._max_plan_bytes:
            raise PlanInvalid("plan content exceeds the size limit")
        return data

    def _read_regular(self, path: Path) -> bytes:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise PlanNotFound("plan not found") from exc
        except OSError as exc:
            raise PlanServiceError("plan is unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.is_symlink():
            raise PlanInvalid("plan must be a regular file")
        if metadata.st_size > self._max_plan_bytes:
            raise PlanInvalid("plan content exceeds the size limit")
        try:
            data = path.read_bytes()
            data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise PlanInvalid("plan content is unavailable") from exc
        if len(data) > self._max_plan_bytes:
            raise PlanInvalid("plan content exceeds the size limit")
        return data

    def _file_identity(self, path: Path, data: bytes) -> tuple[int, int, int, str]:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PlanServiceError("plan identity is unavailable") from exc
        return (
            metadata.st_dev,
            metadata.st_ino,
            len(data),
            self._revision(data),
        )

    def _rollback_promote(
        self,
        source: Path,
        target: Path,
        source_identity: tuple[int, int, int, str],
    ) -> None:
        try:
            os.replace(target, source)
            self._fsync_directory(source.parent)
            restored = self._read_regular(source)
            if self._file_identity(source, restored) != source_identity:
                raise OSError("restored source identity does not match")
        except (OSError, PlanServiceError) as exc:
            raise PlanServiceError("plan promotion rollback failed") from exc

    def _atomic_write(self, path: Path, data: bytes, *, replace: bool) -> None:
        temporary = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if not replace and (path.exists() or path.is_symlink()):
                raise PlanAlreadyExists("plan already exists")
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except PlanServiceError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PlanServiceError("plan update failed") from exc

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _revision(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _require_revision(self, data: bytes, expected_revision: str) -> None:
        current = self._revision(data)
        if current != expected_revision:
            raise PlanRevisionConflict(current)

    @staticmethod
    def _validate_revision(revision: str) -> None:
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
            raise PlanServiceError("expected revision must be a SHA-256 digest")

    def _document(self, project_id: str, status_value: PlanStatus, name: str, data: bytes, *, include_content: bool = False) -> PlanDocument:
        relative = f"plans/{_STATUS_DIRS[status_value]}/{name}"
        return PlanDocument(
            project_id=project_id, name=name, path=relative, status=status_value,
            revision=self._revision(data), size_bytes=len(data),
            content=data.decode("utf-8") if include_content else None,
        )

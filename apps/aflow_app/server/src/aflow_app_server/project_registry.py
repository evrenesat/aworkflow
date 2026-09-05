"""Persistent exact-root project authorization for the remote application."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from threading import RLock
from typing import Any

from .models import ProjectInfo


PROJECT_REGISTRY_SCHEMA_VERSION = 1
_PROJECT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_STORE_LOCKS_GUARD = RLock()
_STORE_LOCKS: dict[str, RLock] = {}


class ProjectRegistryError(RuntimeError):
    """A registry file or requested registration is unsafe or invalid."""


@dataclass(frozen=True)
class ProjectRegistryRecord:
    """One versioned project authorization record."""

    id: str
    display_name: str
    relative_root: str
    created_at: datetime
    updated_at: datetime
    schema_version: int = PROJECT_REGISTRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "display_name": self.display_name,
            "relative_root": self.relative_root,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _canonical_managed_root(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ProjectRegistryError("managed projects root must not be a symlink")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise ProjectRegistryError("managed projects root is unavailable") from exc
    if not resolved.is_dir():
        raise ProjectRegistryError("managed projects root must be an existing directory")
    return resolved


class ProjectRegistry:
    """Atomic JSON store and resolver for one dynamic exact-root allowlist."""

    def __init__(self, managed_root: Path, registry_path: Path) -> None:
        self._managed_root = _canonical_managed_root(managed_root)
        self._path = registry_path.expanduser().absolute()
        if self._path.is_symlink():
            raise ProjectRegistryError("project registry path must not be a symlink")
        lock_key = os.path.normcase(str(self._path))
        with _STORE_LOCKS_GUARD:
            self._lock = _STORE_LOCKS.setdefault(lock_key, RLock())
        # Fail closed at construction rather than discovering corruption on a
        # request after the process has advertised readiness.
        self._read_records()

    @property
    def managed_root(self) -> Path:
        return self._managed_root

    @property
    def path(self) -> Path:
        return self._path

    def list_records(self) -> tuple[ProjectRegistryRecord, ...]:
        with self._lock:
            records = self._read_records()
            return tuple(records[key] for key in sorted(records))

    def get(self, project_id: str) -> ProjectRegistryRecord | None:
        with self._lock:
            return self._read_records().get(project_id)

    def resolve(self, project_id: str) -> tuple[ProjectRegistryRecord, Path]:
        with self._lock:
            records = self._read_records()
            record = records.get(project_id)
            if record is None:
                raise ProjectRegistryError("project is not registered")
            return record, self._resolve_record(record)

    def declared_root(self, project_id: str) -> Path:
        """Return the contained lexical root for a structurally valid record."""
        with self._lock:
            record = self._read_records().get(project_id)
            if record is None:
                raise ProjectRegistryError("project is not registered")
            return self._managed_root.joinpath(
                *PurePosixPath(record.relative_root).parts
            )

    def register(
        self,
        project_id: str,
        display_name: str,
        relative_root: str,
    ) -> ProjectRegistryRecord:
        with self._lock:
            records = self._read_records()
            now = datetime.now(timezone.utc)
            record = ProjectRegistryRecord(
                id=project_id,
                display_name=display_name,
                relative_root=relative_root,
                created_at=now,
                updated_at=now,
            )
            self._validate_record_shape(record)
            candidate = self._resolve_record(record)
            if project_id in records:
                raise ProjectRegistryError("project id is already registered")
            self._reject_root_conflicts(candidate, records.values())
            records[project_id] = record
            self._write_records(records)
            return record

    def unregister(self, project_id: str) -> ProjectRegistryRecord | None:
        with self._lock:
            records = self._read_records()
            record = records.pop(project_id, None)
            if record is None:
                return None
            self._write_records(records)
            return record

    def rename(self, project_id: str, display_name: str) -> ProjectRegistryRecord | None:
        with self._lock:
            records = self._read_records()
            record = records.get(project_id)
            if record is None:
                return None
            updated = replace(
                record,
                display_name=display_name,
                updated_at=datetime.now(timezone.utc),
            )
            self._validate_record_shape(updated)
            records[project_id] = updated
            self._write_records(records)
            return updated

    def _read_records(self) -> dict[str, ProjectRegistryRecord]:
        if not self._path.exists():
            return {}
        if self._path.is_symlink() or not self._path.is_file():
            raise ProjectRegistryError("project registry must be a regular non-symlink file")
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectRegistryError("project registry is unreadable or corrupt") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != PROJECT_REGISTRY_SCHEMA_VERSION
        ):
            raise ProjectRegistryError("project registry schema version is unsupported")
        raw_records = payload.get("projects")
        if not isinstance(raw_records, list):
            raise ProjectRegistryError("project registry projects must be a list")
        records: dict[str, ProjectRegistryRecord] = {}
        declared_roots: list[Path] = []
        for raw in raw_records:
            record = self._record_from_dict(raw)
            if record.id in records:
                raise ProjectRegistryError("project registry contains duplicate ids")
            declared = self._managed_root.joinpath(*PurePosixPath(record.relative_root).parts)
            normalized = os.path.normcase(str(declared))
            if any(os.path.normcase(str(existing)) == normalized for existing in declared_roots):
                raise ProjectRegistryError("project registry contains duplicate roots")
            if any(
                declared in existing.parents or existing in declared.parents
                for existing in declared_roots
            ):
                raise ProjectRegistryError("project registry contains overlapping roots")
            records[record.id] = record
            declared_roots.append(declared)
        return records

    def _record_from_dict(self, raw: Any) -> ProjectRegistryRecord:
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "id", "display_name", "relative_root", "created_at", "updated_at"
        }:
            raise ProjectRegistryError("project registry record has an invalid shape")
        try:
            record = ProjectRegistryRecord(
                schema_version=raw["schema_version"],
                id=raw["id"],
                display_name=raw["display_name"],
                relative_root=raw["relative_root"],
                created_at=datetime.fromisoformat(raw["created_at"]),
                updated_at=datetime.fromisoformat(raw["updated_at"]),
            )
        except (TypeError, ValueError) as exc:
            raise ProjectRegistryError("project registry record is malformed") from exc
        self._validate_record_shape(record)
        return record

    @staticmethod
    def _validate_record_shape(record: ProjectRegistryRecord) -> None:
        if (
            not isinstance(record.schema_version, int)
            or isinstance(record.schema_version, bool)
            or record.schema_version != PROJECT_REGISTRY_SCHEMA_VERSION
        ):
            raise ProjectRegistryError("project registry record version is unsupported")
        if not isinstance(record.id, str) or _PROJECT_ID_RE.fullmatch(record.id) is None:
            raise ProjectRegistryError("project id must be a path-safe slug")
        if (
            not isinstance(record.display_name, str)
            or not record.display_name.strip()
            or len(record.display_name) > 200
            or any(marker in record.display_name for marker in ("\x00", "\n", "\r"))
        ):
            raise ProjectRegistryError("project display name is invalid")
        if not isinstance(record.relative_root, str):
            raise ProjectRegistryError("project relative root is invalid")
        relative = PurePosixPath(record.relative_root)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or str(relative) != record.relative_root
        ):
            raise ProjectRegistryError("project root must be a normalized relative path")
        for timestamp in (record.created_at, record.updated_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ProjectRegistryError("project timestamps must include a timezone")
        if record.updated_at < record.created_at:
            raise ProjectRegistryError("project timestamps are inconsistent")

    def _resolve_record(self, record: ProjectRegistryRecord) -> Path:
        self._validate_record_shape(record)
        candidate = self._managed_root
        for part in PurePosixPath(record.relative_root).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ProjectRegistryError("project root contains a symlink component")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._managed_root)
        except (OSError, ValueError) as exc:
            raise ProjectRegistryError("project root is unavailable or outside the managed root") from exc
        if not resolved.is_dir():
            raise ProjectRegistryError("project root must be a directory")
        git_entry = resolved / ".git"
        if git_entry.is_symlink() or not git_entry.exists():
            raise ProjectRegistryError("project root must be a Git repository root")
        try:
            completed = subprocess.run(
                ("git", "-C", str(resolved), "rev-parse", "--show-toplevel"),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectRegistryError("project Git identity could not be verified") from exc
        if completed.returncode != 0:
            raise ProjectRegistryError("project root must be a Git repository root")
        try:
            observed_root = Path(completed.stdout.strip()).resolve(strict=True)
        except OSError as exc:
            raise ProjectRegistryError("project Git identity could not be verified") from exc
        if os.path.normcase(str(observed_root)) != os.path.normcase(str(resolved)):
            raise ProjectRegistryError("project path must name the exact Git repository root")
        return resolved

    def _reject_root_conflicts(
        self,
        candidate: Path,
        records: Iterable[ProjectRegistryRecord],
    ) -> None:
        """Reject identity conflicts against durable declared roots only.

        Existing records are structurally validated when the store is read,
        and their declared roots reserve identity even while the underlying
        directory is unavailable, so no per-record resolution or Git
        verification is required here.
        """
        candidate_key = os.path.normcase(str(candidate))
        for record in records:
            declared = self._managed_root.joinpath(
                *PurePosixPath(record.relative_root).parts
            )
            if os.path.normcase(str(declared)) == candidate_key:
                raise ProjectRegistryError("project root is already registered")
            if candidate in declared.parents or declared in candidate.parents:
                raise ProjectRegistryError("nested or overlapping project roots are not allowed")

    def _write_records(self, records: dict[str, ProjectRegistryRecord]) -> None:
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink():
            raise ProjectRegistryError("project registry directory must not be a symlink")
        payload = {
            "schema_version": PROJECT_REGISTRY_SCHEMA_VERSION,
            "projects": [records[key].to_dict() for key in sorted(records)],
        }
        temporary = parent / f".{self._path.name}.{os.getpid()}.{id(self)}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProjectRegistryError("project registry update failed") from exc


class ProjectRegistryCatalog:
    """Compatibility view that prevents planning routes from scanning projects."""

    def __init__(self, registry: ProjectRegistry) -> None:
        self._registry = registry

    def list_projects(self, sessions: Iterable[object] | None = None) -> list[ProjectInfo]:
        return self._snapshot(self._registry.list_records(), tuple(sessions or ()))

    def get_project(
        self,
        project_id: str,
        sessions: Iterable[object] | None = None,
    ) -> ProjectInfo | None:
        record = self._registry.get(project_id)
        if record is None:
            return None
        return self._snapshot((record,), tuple(sessions or ()))[0]

    def _snapshot(
        self,
        records: tuple[ProjectRegistryRecord, ...],
        sessions: tuple[object, ...],
    ) -> list[ProjectInfo]:
        """Build one internally consistent informational project snapshot.

        Every registered project is resolved at most once and every supplied
        session cwd is normalized at most once, so attribution never performs
        project-by-session resolution or Git subprocess work.
        """
        resolved = self._resolution_snapshot(records)
        counts = self._linked_session_counts(resolved, sessions)
        projects: list[ProjectInfo] = []
        for record in records:
            root = resolved.get(record.id)
            if root is None:
                projects.append(self._unavailable_info(record))
                continue
            projects.append(
                replace(
                    self._resolved_info(record, root),
                    linked_session_count=counts.get(record.id, 0),
                )
            )
        return projects

    def _resolution_snapshot(
        self,
        records: tuple[ProjectRegistryRecord, ...],
    ) -> dict[str, Path]:
        """Resolve each structurally valid record at most once for the snapshot."""
        resolved: dict[str, Path] = {}
        for record in records:
            try:
                _, root = self._registry.resolve(record.id)
            except ProjectRegistryError:
                continue
            resolved[record.id] = root
        return resolved

    def _linked_session_counts(
        self,
        resolved: dict[str, Path],
        sessions: tuple[object, ...],
    ) -> dict[str, int]:
        """Attribute sessions only to their exact, currently resolved root."""
        by_key = {
            os.path.normcase(str(root)): project_id
            for project_id, root in resolved.items()
        }
        counts: dict[str, int] = {}
        for session in sessions:
            root = self._normalized_session_root(getattr(session, "cwd", None))
            if root is None:
                continue
            project_id = by_key.get(os.path.normcase(str(root)))
            if project_id is not None:
                counts[project_id] = counts.get(project_id, 0) + 1
        return counts

    @staticmethod
    def _normalized_session_root(cwd: object) -> Path | None:
        """Validate and canonicalize one session cwd, or reject it outright.

        Missing, relative, traversing, and symlinked paths are rejected so
        only a genuine exact root can be attributed.
        """
        if cwd is None:
            return None
        try:
            candidate = Path(cwd).expanduser()
        except (TypeError, ValueError):
            return None
        if not candidate.is_absolute() or ".." in candidate.parts:
            return None
        current = Path(candidate.anchor)
        try:
            for part in candidate.parts[1:]:
                current /= part
                if current.is_symlink():
                    return None
            return candidate.resolve(strict=True)
        except (OSError, ValueError):
            return None

    def get_project_fast(self, project_id: str) -> ProjectInfo | None:
        try:
            record, root = self._registry.resolve(project_id)
        except ProjectRegistryError:
            return None
        return self._resolved_info(record, root)

    def update_project(
        self,
        project_id: str,
        *,
        display_name: str | None = None,
        current_path: str | None = None,
        alias: str | None = None,
    ) -> ProjectInfo | None:
        if alias is not None:
            raise ProjectRegistryError("project roots and aliases are not mutable")
        record = self._registry.get(project_id)
        if record is None:
            return None
        if current_path is not None:
            submitted = Path(current_path).expanduser()
            declared = self._registry.declared_root(project_id)
            _, resolved = self._registry.resolve(project_id)
            if not submitted.is_absolute() or submitted not in {declared, resolved}:
                raise ProjectRegistryError("project roots and aliases are not mutable")
        if display_name is not None:
            record = self._registry.rename(project_id, display_name)
        return None if record is None else self._to_info(record)

    def project_owns_path(
        self,
        project: ProjectInfo,
        path: Path | str,
        *,
        projects: list[ProjectInfo] | None = None,
        sessions: object = None,
    ) -> bool:
        """Authorize only an exact canonical root from the current registry."""
        del projects, sessions
        try:
            record = self._registry.get(project.id)
            if record is None:
                return False
            _, root = self._registry.resolve(project.id)
            current = self._resolved_info(record, root)
            if (
                project.id != current.id
                or project.display_name != current.display_name
                or project.current_path != current.current_path
                or project.historical_aliases != current.historical_aliases
                or project.detection_source != current.detection_source
                or project.is_git_root != current.is_git_root
                or project.registered_at != current.registered_at
            ):
                return False

            candidate = Path(path).expanduser()
            if not candidate.is_absolute() or ".." in candidate.parts:
                return False
            current_component = Path(candidate.anchor)
            for part in candidate.parts[1:]:
                current_component /= part
                if current_component.is_symlink():
                    return False
            resolved = candidate.resolve(strict=True)
        except (OSError, TypeError, ValueError, ProjectRegistryError):
            return False
        return os.path.normcase(str(resolved)) == os.path.normcase(str(root))

    @staticmethod
    def _resolved_info(record: ProjectRegistryRecord, root: Path) -> ProjectInfo:
        return ProjectInfo(
            id=record.id,
            display_name=record.display_name,
            current_path=root,
            historical_aliases=(),
            detection_source="registry",
            linked_session_count=0,
            is_git_root=True,
            registered_at=record.created_at,
        )

    def _unavailable_info(self, record: ProjectRegistryRecord) -> ProjectInfo:
        return ProjectInfo(
            id=record.id,
            display_name=record.display_name,
            current_path=self._registry.declared_root(record.id),
            historical_aliases=(),
            detection_source="registry",
            linked_session_count=0,
            is_git_root=False,
            registered_at=record.created_at,
        )

    def _to_info(self, record: ProjectRegistryRecord) -> ProjectInfo:
        try:
            _, root = self._registry.resolve(record.id)
        except ProjectRegistryError:
            return self._unavailable_info(record)
        return self._resolved_info(record, root)

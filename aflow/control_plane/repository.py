"""Read-authoritative repository access for durable AFlow control-plane state."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from aflow.run_state import load_override_request

from .models import (
    CONTROL_PLANE_SCHEMA_VERSION,
    LaunchManifest,
    PlanRecord,
    ProjectRecord,
    RunPage,
    RunStatus,
)
from .persistence import PersistenceError, RunIdentityError, read_events, validate_run_id


MAX_PAGE_SIZE = 1_000
_LEGACY_DIRECT_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_PLAN_DIRECTORIES = (
    ("drafts", "draft"),
    ("todo", "todo"),
    ("in-progress", "in_progress"),
    ("done", "done"),
)


class RepositoryError(PersistenceError):
    """Base class for safe repository reads."""


class RepositoryNotFoundError(RepositoryError):
    """A requested project artifact does not exist."""


class RepositorySchemaError(RepositoryError):
    """A control-plane artifact cannot satisfy its declared schema."""


def _bounded_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise RepositoryError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return limit


class RunRepository:
    """Expose one project root without creating or rewriting its durable state."""

    def __init__(self, repo_root: Path) -> None:
        root = Path(repo_root).resolve()
        if not root.is_dir():
            raise RepositoryError(f"repository root does not exist: {root}")
        self._root = root

    @property
    def repo_root(self) -> Path:
        return self._root

    def project(self) -> ProjectRecord:
        return ProjectRecord(project_id=self._root.name, root=str(self._root))

    def list_projects(self) -> tuple[ProjectRecord, ...]:
        return (self.project(),)

    def list_plans(self, *, limit: int = 100, cursor: str | None = None) -> tuple[PlanRecord, ...]:
        """Return stable plan metadata without exposing the plan prose."""
        _bounded_limit(limit)
        records: list[PlanRecord] = []
        for directory, status in _PLAN_DIRECTORIES:
            root = self._contained_path("plans", directory)
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.md"), key=lambda item: item.name):
                resolved = self._contained_existing(path)
                if not resolved.is_file():
                    continue
                try:
                    modified_at = datetime.fromtimestamp(
                        resolved.stat().st_mtime, tz=timezone.utc
                    ).isoformat()
                except OSError as exc:
                    raise RepositoryError(f"cannot stat plan: {path.name}") from exc
                records.append(
                    PlanRecord(
                        path=str(resolved.relative_to(self._root)),
                        status=status,
                        modified_at=modified_at,
                    )
                )
        records.sort(key=lambda record: (record.path, record.status))
        if cursor is not None:
            records = [record for record in records if record.path > cursor]
        return tuple(records[:limit])

    def get_launch_manifest(self, run_id: str) -> LaunchManifest | None:
        valid = validate_run_id(run_id)
        path = self._launch_manifest_path(valid)
        if not path.exists():
            return None
        if path.is_symlink():
            raise RepositoryError("launch manifest may not be a symlink")
        return self._parse_manifest(path)

    def get_run_status(self, run_id: str) -> RunStatus:
        valid, is_legacy_identity = self._readable_run_id(run_id)
        run_dir = (
            self._contained_path(".aflow", "runs", valid)
            if is_legacy_identity
            else self.run_directory(valid)
        )
        manifest = None if is_legacy_identity else self.get_launch_manifest(valid)
        if manifest is None and not run_dir.is_dir():
            raise RepositoryNotFoundError(f"run '{valid}' does not exist")

        metadata = self._read_run_metadata(run_dir) if run_dir.is_dir() else {}
        if manifest is None:
            # Legacy state has no immutable launch evidence.  Even a stale
            # ``running`` record must never be interpreted as a live process.
            return RunStatus(
                run_id=valid,
                status="interrupted",
                ownership="legacy",
                reason="legacy run has no control-plane launch manifest",
                workflow_name=_optional_text(metadata.get("workflow_name")),
                team=_optional_text(metadata.get("team")),
                current_step=_optional_text(metadata.get("current_step_name")),
                turns_completed=_optional_int(metadata.get("turns_completed")),
                max_turns=_optional_int(metadata.get("max_turns")),
                evidence={"recorded_status": metadata.get("status")},
            )

        phase = self._launch_phase(valid)
        reconciled = self._latest_reconciliation(run_dir) if run_dir.is_dir() else {}
        metadata_status = _optional_text(metadata.get("status"))
        reconciled_status = _optional_text(reconciled.get("status"))
        # A controller terminal record written after the latest daemon
        # observation is authoritative when its launch phase agrees.  This
        # prevents a pre-restart "running" reconciliation event from masking
        # durable completion after the independent workflow unit exits.
        if metadata_status in {"completed", "failed", "interrupted"} and phase == metadata_status:
            recorded_status = metadata_status
        else:
            recorded_status = reconciled_status or metadata_status
        # A collected transient unit is not completion evidence.  Only a
        # durable controller terminal record (or the daemon's explicit owner
        # stop control) may classify a missing unit as terminal.
        if phase == "owner_stopped":
            status = "owner_stopped"
        elif recorded_status is not None:
            status = recorded_status
        elif phase in {"completed", "failed", "interrupted"}:
            status = "needs_attention"
        else:
            status = phase or "manifest_only"
        return RunStatus(
            run_id=valid,
            status=status,
            ownership="control_plane",
            revision=self._override_revision(run_dir) if run_dir.is_dir() else 0,
            reason=_optional_text(reconciled.get("reason")) or _optional_text(metadata.get("failure_reason")),
            unit_name=manifest.intended_unit or f"aflow-run-{valid}.service",
            launch_phase=phase,
            workflow_name=_optional_text(metadata.get("workflow_name")) or manifest.workflow_name,
            team=_optional_text(metadata.get("team")) or manifest.team,
            current_step=_optional_text(metadata.get("current_step_name")),
            turns_completed=_optional_int(metadata.get("turns_completed")),
            max_turns=_optional_int(metadata.get("max_turns")) or manifest.max_turns,
            evidence={
                "has_run_metadata": bool(metadata),
                "manifest_created_at": manifest.created_at,
                "reconciled": bool(reconciled),
            },
        )

    def list_runs(self, *, limit: int = 100, cursor: str | None = None) -> RunPage:
        """Return a stable, cursorable union of legacy and owned run identities."""
        _bounded_limit(limit)
        if cursor is not None:
            self._readable_run_id(cursor)
        run_ids = self._run_ids()
        if cursor is not None:
            run_ids = [run_id for run_id in run_ids if run_id > cursor]
        selected = run_ids[:limit]
        next_cursor = selected[-1] if len(run_ids) > len(selected) and selected else None
        return RunPage(runs=tuple(self.get_run_status(run_id) for run_id in selected), next_cursor=next_cursor)

    def tail_events(
        self,
        run_id: str,
        *,
        limit: int = 100,
        after_sequence: int | None = None,
    ) -> tuple[object, ...]:
        _bounded_limit(limit)
        if after_sequence is not None and (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or after_sequence < 0
        ):
            raise RepositoryError("after_sequence must be a non-negative integer")
        run_dir = self.run_directory(run_id)
        if not run_dir.is_dir():
            raise RepositoryNotFoundError(f"run '{run_id}' does not exist")
        events = read_events(run_dir, limit=MAX_PAGE_SIZE)
        if after_sequence is not None:
            events = [event for event in events if event.sequence > after_sequence]
        return tuple(events[-limit:])

    def list_startup_questions(self) -> tuple[object, ...]:
        """Durable runs have no startup-question file; questions remain transient."""
        return ()

    def run_directory(self, run_id: str) -> Path:
        valid = validate_run_id(run_id)
        return self._contained_path(".aflow", "runs", valid)

    def _run_ids(self) -> list[str]:
        ids: set[str] = set()
        runs_root = self._contained_path(".aflow", "runs")
        if runs_root.is_dir():
            for path in runs_root.iterdir():
                resolved = self._contained_existing(path)
                if resolved.is_dir():
                    try:
                        ids.add(validate_run_id(path.name))
                    except RunIdentityError as exc:
                        if _LEGACY_DIRECT_RUN_ID_RE.fullmatch(path.name) is None:
                            raise RepositorySchemaError(
                                "runs directory contains an invalid identity"
                            ) from exc
                        ids.add(path.name)
        launches_root = self._contained_path(".aflow", "launches")
        if launches_root.is_dir():
            for path in launches_root.glob("*.json"):
                if path.name.endswith(".state.json"):
                    continue
                resolved = self._contained_existing(path)
                if not resolved.is_file():
                    continue
                try:
                    ids.add(validate_run_id(path.stem))
                except RunIdentityError as exc:
                    raise RepositorySchemaError("launch directory contains an invalid identity") from exc
        return sorted(ids)

    def _readable_run_id(self, run_id: str) -> tuple[str, bool]:
        """Return a canonical identity or an old direct-CLI identity for reads.

        Historical direct CLI runs predate the canonical lowercase identity.
        They remain read-only: callers that need paths for writes, event tails,
        manifests, or unit names must continue through :meth:`run_directory`
        and therefore canonical validation.
        """
        try:
            return validate_run_id(run_id), False
        except RunIdentityError:
            if isinstance(run_id, str) and _LEGACY_DIRECT_RUN_ID_RE.fullmatch(run_id):
                return run_id, True
            raise

    def _launch_manifest_path(self, run_id: str) -> Path:
        return self._contained_path(".aflow", "launches", f"{run_id}.json")

    def _launch_phase(self, run_id: str) -> str | None:
        path = self._contained_path(".aflow", "launches", f"{run_id}.state.json")
        if not path.exists():
            return None
        if path.is_symlink():
            raise RepositoryError("launch phase may not be a symlink")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositorySchemaError("launch phase is unreadable") from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != CONTROL_PLANE_SCHEMA_VERSION
            or payload.get("run_id") != run_id
            or not isinstance(payload.get("phase"), str)
        ):
            raise RepositorySchemaError("launch phase has an unsupported schema")
        return str(payload["phase"])

    def _parse_manifest(self, path: Path) -> LaunchManifest:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositorySchemaError("launch manifest is unreadable") from exc
        if not isinstance(payload, Mapping):
            raise RepositorySchemaError("launch manifest is not an object")
        try:
            manifest = LaunchManifest(
                schema_version=int(payload["schema_version"]),
                run_id=validate_run_id(str(payload["run_id"])),
                project_root=str(payload["project_root"]),
                plan_path=str(payload["plan_path"]),
                workflow_name=str(payload["workflow_name"]),
                max_turns=int(payload["max_turns"]),
                team=_optional_text(payload.get("team")),
                start_step=_optional_text(payload.get("start_step")),
                idempotency_key=_optional_text(payload.get("idempotency_key")),
                caller_scope=_optional_text(payload.get("caller_scope")),
                request_digest=_optional_text(payload.get("request_digest")),
                frozen_config_fingerprint=_optional_text(payload.get("frozen_config_fingerprint")),
                intended_unit=_optional_text(payload.get("intended_unit")),
                created_at=str(payload["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RepositorySchemaError("launch manifest has an invalid schema") from exc
        if manifest.schema_version != CONTROL_PLANE_SCHEMA_VERSION or manifest.max_turns < 1:
            raise RepositorySchemaError("launch manifest schema is unsupported")
        return manifest

    def _read_run_metadata(self, run_dir: Path) -> dict[str, Any]:
        path = run_dir / "run.json"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise RepositoryError("run metadata may not be a symlink")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositorySchemaError("run metadata is unreadable") from exc
        if not isinstance(payload, Mapping):
            raise RepositorySchemaError("run metadata is not an object")
        return dict(payload)

    def _latest_reconciliation(self, run_dir: Path) -> Mapping[str, Any]:
        for event in reversed(read_events(run_dir, limit=MAX_PAGE_SIZE)):
            if event.event_type == "reconciled" and isinstance(event.data, Mapping):
                return event.data
        return {}

    def _override_revision(self, run_dir: Path) -> int:
        path = run_dir / "overrides.toml"
        if path.is_symlink():
            raise RepositoryError("overrides.toml may not be a symlink")
        if not path.exists():
            return 0
        if not path.is_file():
            raise RepositorySchemaError("overrides.toml is not a regular file")
        loaded = load_override_request(path)
        if loaded.status != "valid" or loaded.request is None:
            message = loaded.message or "override file is not valid"
            raise RepositorySchemaError(f"overrides.toml is invalid: {message}")
        return loaded.request.revision

    def _contained_path(self, *parts: str) -> Path:
        path = self._root
        for part in parts:
            component = Path(part)
            if component.is_absolute() or len(component.parts) != 1 or component.name != part:
                raise RepositoryError("repository path component is not safe")
            candidate = path / component
            if candidate.exists() or candidate.is_symlink():
                path = self._contained_existing(candidate)
            else:
                path = candidate
        return path

    def _contained_existing(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepositoryError("repository artifact escapes project root") from exc
        return resolved


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None

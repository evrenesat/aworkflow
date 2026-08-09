"""Atomic persistence primitives for engine-owned control-plane artifacts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any, Iterator, Literal, Mapping
from uuid import uuid4

from .models import (
    CONTROL_PLANE_SCHEMA_VERSION,
    ContextBundle,
    LaunchManifest,
    RunControlRequest,
    RunEvent,
    StartRunResult,
    bounded_redacted,
)


RUN_ID_MAX_LENGTH = 64
_RUN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_LAUNCH_PHASES = frozenset({
    "manifest_only",
    "launch_requested",
    "launch_started",
    "unit_started",
    "completed",
    "failed",
    "interrupted",
    "owner_stopped",
})
_OVERRIDE_KEYS = frozenset({
    "revision", "next_step", "team", "max_turns", "notes", "roles", "owner_stop",
})


class PersistenceError(ValueError):
    """Base error for rejected durable control-plane operations."""


class RunIdentityError(PersistenceError):
    """A supplied run identity is not canonical or safely contained."""


class RunIdentityConflict(PersistenceError):
    """A durable identity is already reserved for a different launch intent."""


class JournalCorruptionError(PersistenceError):
    """A completed journal record is malformed or non-monotonic."""


class ControlConflictError(PersistenceError):
    """The caller's compare-and-swap revision is stale."""

    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(f"override revision conflict; current revision is {current_revision}")


class RestartRequiredControlError(PersistenceError):
    """A requested change is structurally unsafe to apply live."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        super().__init__("restart required for: " + ", ".join(fields))


@dataclass(frozen=True)
class ControlWriteResult:
    revision: int
    changed: bool
    owner_stop: bool
    path: Path


def validate_run_id(run_id: str) -> str:
    """Validate the one lowercase identity usable in URLs, paths, and unit names."""
    if not isinstance(run_id, str):
        raise RunIdentityError("run id must be a string")
    if len(run_id) > RUN_ID_MAX_LENGTH or _RUN_ID_RE.fullmatch(run_id) is None:
        raise RunIdentityError(
            "run id must be lowercase ASCII letters/digits/hyphens, bounded, and path-safe"
        )
    if run_id in {".", ".."} or "--" in run_id and run_id.startswith("-"):
        raise RunIdentityError("run id is not a safe unit/path component")
    return run_id


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
    return f"{stamp}-{uuid4().hex[:8]}"


def _contained_directory(repo_root: Path, *parts: str) -> Path:
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise PersistenceError(f"repository root does not exist: {root}")

    current = root
    for part in parts:
        component = Path(part)
        if component.is_absolute() or len(component.parts) != 1 or component.name != part:
            raise RunIdentityError("control-plane path component is not safe")
        candidate = current / component
        if candidate.exists() or candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RunIdentityError("control-plane path cannot be resolved safely") from exc
        else:
            # ``current`` has already been resolved and proven contained.  Only
            # create one missing child at a time so an existing symlink is
            # validated before any descendant can be created through it.
            try:
                candidate.mkdir()
            except FileExistsError:
                # A concurrent reservation created this component first.  It
                # still must pass the same containment check below.
                pass
            except OSError as exc:
                raise RunIdentityError("control-plane path cannot be created safely") from exc
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RunIdentityError("control-plane path cannot be resolved safely") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RunIdentityError("control-plane path escapes repository root") from exc
        if not resolved.is_dir():
            raise RunIdentityError("control-plane path component is not a directory")
        current = resolved
    return current


def _runs_root(repo_root: Path) -> Path:
    return _contained_directory(repo_root, ".aflow", "runs")


def _launches_root(repo_root: Path) -> Path:
    return _contained_directory(repo_root, ".aflow", "launches")


def _safe_run_dir(repo_root: Path, run_id: str) -> Path:
    valid = validate_run_id(run_id)
    root = _runs_root(repo_root)
    path = root / valid
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RunIdentityError("run directory escapes .aflow/runs") from exc
    return path


def reserve_run_id(repo_root: Path, requested_run_id: str | None = None) -> str:
    """Choose or validate a canonical run ID before any workflow child starts."""
    _runs_root(repo_root)
    _launches_root(repo_root)
    if requested_run_id is not None:
        return validate_run_id(requested_run_id)
    return _new_run_id()


def normalized_request_digest(manifest: LaunchManifest) -> str:
    payload = {
        "project_root": str(Path(manifest.project_root).resolve()),
        "plan_path": str(Path(manifest.plan_path).resolve()),
        "workflow_name": manifest.workflow_name,
        "team": manifest.team,
        "start_step": manifest.start_step,
        "max_turns": manifest.max_turns,
        "extra_instructions": list(manifest.extra_instructions),
        "caller_scope": manifest.caller_scope,
        "frozen_config_fingerprint": manifest.frozen_config_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably publish JSON without exposing a partial final-path artifact.

    ``os.link`` creates the final name only when it does not already exist.
    Publishing a fully fsynced same-directory temporary file through that
    operation preserves the immutable/exclusive contract without a window in
    which a crash can leave a partial final manifest behind.
    """
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link is an atomic no-replace publish on the same filesystem.
        # It raises FileExistsError for a completed/corrupt competing final
        # manifest, which the caller classifies without any overwrite.
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _manifest_from_payload(payload: Mapping[str, Any]) -> LaunchManifest:
    try:
        manifest = LaunchManifest(
            schema_version=int(payload["schema_version"]),
            run_id=validate_run_id(str(payload["run_id"])),
            project_root=str(payload["project_root"]),
            plan_path=str(payload["plan_path"]),
            workflow_name=str(payload["workflow_name"]),
            max_turns=int(payload["max_turns"]),
            team=str(payload["team"]) if payload.get("team") is not None else None,
            start_step=(str(payload["start_step"]) if payload.get("start_step") is not None else None),
            # Prompt-like inputs are deliberately not persisted.  Ignore this
            # legacy field if an older manifest contains it rather than making
            # it observable through a later serialization.
            extra_instructions=(),
            idempotency_key=(str(payload["idempotency_key"]) if payload.get("idempotency_key") is not None else None),
            caller_scope=(str(payload["caller_scope"]) if payload.get("caller_scope") is not None else None),
            request_digest=(str(payload["request_digest"]) if payload.get("request_digest") is not None else None),
            frozen_config_fingerprint=(
                str(payload["frozen_config_fingerprint"])
                if payload.get("frozen_config_fingerprint") is not None
                else None
            ),
            intended_unit=(str(payload["intended_unit"]) if payload.get("intended_unit") is not None else None),
            created_at=str(payload["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError("invalid launch manifest") from exc
    if manifest.schema_version != CONTROL_PLANE_SCHEMA_VERSION or manifest.max_turns < 1:
        raise PersistenceError("unsupported launch manifest")
    return manifest


def _same_idempotent_request(existing: LaunchManifest, requested: LaunchManifest) -> bool:
    return bool(
        existing.idempotency_key
        and existing.idempotency_key == requested.idempotency_key
        and existing.caller_scope == requested.caller_scope
        and existing.request_digest == requested.request_digest
    )


def create_launch_manifest(repo_root: Path, manifest: LaunchManifest) -> StartRunResult:
    """Exclusively create immutable launch intent, or return an identical replay."""
    run_id = validate_run_id(manifest.run_id)
    run_dir = _safe_run_dir(repo_root, run_id)
    launches = _launches_root(repo_root)
    path = launches / f"{run_id}.json"
    if path.is_symlink():
        raise RunIdentityError("launch manifest may not be a symlink")
    # An existing manifest owns the identity and remains the authority for an
    # idempotent replay.  Without one, an existing legacy/controller run
    # directory must never be claimed by publishing a new launch artifact.
    if not path.exists() and run_dir.exists():
        raise RunIdentityConflict(
            f"run id '{run_id}' already has a run directory without a launch manifest"
        )
    resolved_manifest = LaunchManifest(
        **{**asdict(manifest), "run_id": run_id,
           # The durable idempotency digest belongs to this persistence
           # boundary.  Never trust a caller-supplied digest to describe the
           # launch request being reserved.
           "request_digest": normalized_request_digest(manifest),
           "intended_unit": manifest.intended_unit or f"aflow-run-{run_id}.service"}
    )
    try:
        _write_exclusive_json(path, resolved_manifest.to_dict())
    except FileExistsError:
        try:
            existing_payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunIdentityConflict("existing launch manifest is unreadable") from exc
        if not isinstance(existing_payload, Mapping):
            raise RunIdentityConflict("existing launch manifest is malformed")
        existing = _manifest_from_payload(existing_payload)
        if _same_idempotent_request(existing, resolved_manifest):
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="existing",
                manifest_path=str(path),
            )
        raise RunIdentityConflict(f"run id '{run_id}' is already reserved")
    write_launch_phase(repo_root, run_id, "manifest_only")
    return StartRunResult(
        run_id=run_id,
        created=True,
        status="manifest_only",
        manifest_path=str(path),
    )


def write_launch_phase(repo_root: Path, run_id: str, phase: str) -> Path:
    """Durably classify the launch gap without modifying the immutable manifest."""
    valid = validate_run_id(run_id)
    if phase not in _LAUNCH_PHASES:
        raise PersistenceError(f"unsupported launch phase: {phase}")
    path = _launches_root(repo_root) / f"{valid}.state.json"
    payload = {
        "schema_version": CONTROL_PLANE_SCHEMA_VERSION,
        "run_id": valid,
        "phase": phase,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_atomic_bytes(path, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
    return path


@contextmanager
def _locked_file(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _parse_events(raw: bytes) -> list[RunEvent]:
    if not raw:
        return []
    complete = raw.splitlines(keepends=True)
    if complete and not complete[-1].endswith(b"\n"):
        complete.pop()  # One torn final line is deliberately non-authoritative.
    events: list[RunEvent] = []
    expected_sequence = 1
    for line in complete:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalCorruptionError("interior event journal corruption") from exc
        if not isinstance(payload, Mapping):
            raise JournalCorruptionError("event journal record is not an object")
        try:
            event = RunEvent(
                schema_version=int(payload["schema_version"]),
                sequence=int(payload["sequence"]),
                event_type=str(payload["event_type"]),
                timestamp=str(payload["timestamp"]),
                data=dict(payload.get("data", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JournalCorruptionError("event journal record is malformed") from exc
        if event.schema_version != CONTROL_PLANE_SCHEMA_VERSION or event.sequence != expected_sequence:
            raise JournalCorruptionError("event journal sequence is not strictly monotonic")
        expected_sequence += 1
        events.append(event)
    return events


class EventJournal:
    """A lock-serialized, fsyncing append-only event journal for one run."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "events.jsonl"
        self.lock_path = self.run_dir / ".events.lock"

    def append(self, event_type: str, data: Mapping[str, Any] | None = None) -> RunEvent:
        if not isinstance(event_type, str) or not event_type or len(event_type) > 120:
            raise PersistenceError("event type must be a bounded non-empty string")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with _locked_file(self.lock_path):
            raw = self.path.read_bytes() if self.path.exists() else b""
            events = _parse_events(raw)
            if raw and not raw.endswith(b"\n"):
                last_newline = raw.rfind(b"\n")
                _write_atomic_bytes(self.path, raw[:last_newline + 1] if last_newline >= 0 else b"")
            event = RunEvent(
                sequence=len(events) + 1,
                event_type=event_type,
                data=bounded_redacted(dict(data or {})),
            )
            encoded = (json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_directory(self.path.parent)
            return event

    def tail(self, *, limit: int = 100) -> list[RunEvent]:
        if limit < 1 or limit > 1_000:
            raise ValueError("event tail limit must be between 1 and 1000")
        raw = self.path.read_bytes() if self.path.exists() else b""
        return _parse_events(raw)[-limit:]


def append_run_event(run_dir: Path, event_type: str, data: Mapping[str, Any] | None = None) -> RunEvent:
    return EventJournal(run_dir).append(event_type, data)


def read_events(run_dir: Path, *, limit: int = 100) -> list[RunEvent]:
    return EventJournal(run_dir).tail(limit=limit)


def _read_override_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise RunIdentityError("overrides.toml may not be a symlink")
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PersistenceError("cannot safely update malformed overrides.toml") from exc
    if not isinstance(payload, dict) or set(payload) - _OVERRIDE_KEYS:
        raise PersistenceError("cannot safely update unsupported overrides.toml")
    revision = payload.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PersistenceError("overrides.toml has an invalid revision")
    return payload


def _render_toml(payload: Mapping[str, Any]) -> str:
    lines: list[str] = [f"revision = {int(payload.get('revision', 0))}"]
    for key in ("max_turns", "owner_stop", "team", "next_step"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{key} = {value}")
        elif isinstance(value, str):
            lines.append(f"{key} = {json.dumps(value)}")
    notes = payload.get("notes")
    if isinstance(notes, list):
        lines.append("notes = [" + ", ".join(json.dumps(str(note)) for note in notes) + "]")
    roles = payload.get("roles")
    if isinstance(roles, Mapping) and roles:
        lines.append("")
        lines.append("[roles]")
        for role in sorted(roles):
            lines.append(f"{json.dumps(str(role))} = {json.dumps(str(roles[role]))}")
    return "\n".join(lines) + "\n"


def compare_and_swap_overrides(
    repo_root: Path,
    run_id: str,
    request: RunControlRequest,
) -> ControlWriteResult:
    """Apply live-safe controls through `overrides.toml` with a revision CAS."""
    if request.unsafe_changes:
        raise RestartRequiredControlError(tuple(sorted(str(key) for key in request.unsafe_changes)))
    if request.expected_revision < 0:
        raise ValueError("expected revision must be non-negative")
    if request.max_turns is not None and request.max_turns < 1:
        raise ValueError("max_turns must be positive")
    if request.team is not None and not request.team.strip():
        raise ValueError("team must be non-empty")
    if any(not role.strip() or not selector.strip() or "." not in selector for role, selector in request.role_selectors.items()):
        raise ValueError("role selectors must be non-empty fully qualified selectors")
    run_dir = _safe_run_dir(repo_root, run_id)
    if not run_dir.is_dir():
        raise PersistenceError(f"run '{run_id}' does not exist")
    path = run_dir / "overrides.toml"
    with _locked_file(run_dir / ".overrides.lock"):
        payload = _read_override_payload(path)
        current_revision = int(payload.get("revision", 0))
        if request.expected_revision != current_revision:
            raise ControlConflictError(current_revision)
        updated = dict(payload)
        if request.max_turns is not None:
            updated["max_turns"] = request.max_turns
        if request.owner_stop is not None:
            updated["owner_stop"] = request.owner_stop
        if request.team is not None:
            updated["team"] = request.team.strip()
        if request.role_selectors:
            roles = dict(updated.get("roles", {}))
            roles.update({str(role): str(selector) for role, selector in request.role_selectors.items()})
            updated["roles"] = roles
        changed = updated != payload
        if changed:
            updated["revision"] = current_revision + 1
            _write_atomic_bytes(path, _render_toml(updated).encode("utf-8"))
        result = ControlWriteResult(
            revision=int(updated.get("revision", current_revision)),
            changed=changed,
            owner_stop=bool(updated.get("owner_stop", False)),
            path=path,
        )
    if result.changed:
        append_run_event(
            run_dir,
            "control_changed",
            {
                "revision": result.revision,
                "max_turns": updated.get("max_turns"),
                "owner_stop": result.owner_stop,
                "team": updated.get("team"),
                "roles": updated.get("roles", {}),
            },
        )
    return result


def build_context_bundle(
    run_dir: Path,
    *,
    level: Literal["lite", "full"] = "lite",
    full_scope: bool = False,
) -> ContextBundle:
    """Adapt existing manager context, with Lite default and explicit Full scope."""
    if level not in {"lite", "full"}:
        raise ValueError("context level must be 'lite' or 'full'")
    if level == "full" and not full_scope:
        raise PermissionError("full context requires explicit full_scope")
    root = Path(run_dir)
    metadata: dict[str, Any] = {}
    try:
        parsed = json.loads((root / "run.json").read_text(encoding="utf-8"))
        if isinstance(parsed, Mapping):
            metadata = dict(parsed)
    except (OSError, json.JSONDecodeError):
        pass
    data: dict[str, Any] = {
        "run_metadata": bounded_redacted(metadata),
        "events": [event.to_dict() for event in read_events(root, limit=100)],
    }
    try:
        from aflow.manager_context import build_manager_context

        data["manager_context"] = bounded_redacted(
            build_manager_context(root, level=level, trigger="control_plane_context")
        )
    except (OSError, ValueError):
        # A pre-turn run has no finalized artifact yet; metadata/events remain
        # the authoritative bounded context until the first boundary exists.
        pass
    return ContextBundle(run_id=root.name, level=level, data=data)

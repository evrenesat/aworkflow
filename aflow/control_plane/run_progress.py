"""Read-only progress projection for control-plane observers.

The workflow controller owns plan and turn state.  This module only projects
that state for REST/MCP consumers; it never repairs, normalizes, or rewrites
the artifacts it reads.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
import heapq
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Literal, Mapping

from aflow.plan import ParsedPlan, PlanParseError, parse_plan_text
from aflow.plan_backups import plan_identity_for_path

from .models import (
    ProgressCheckpointStatus,
    RunProgressChange,
    RunProgressCheckpoint,
    RunProgressCount,
    RunProgressDeliveryStage,
    RunProgressDetail,
    RunProgressEvent,
    ProgressEventAssociation,
    RunProgressExecutor,
    RunProgressSummary,
    RunProgressTruncation,
    bounded_redacted,
)


_MAX_PATH_LENGTH = 512
_MAX_SUMMARY_LENGTH = 512
_ACTIVE_TURN_STATUSES = frozenset({"starting", "running", "active", "in_progress"})
_PROGRESS_REASONS = frozenset({
    "missing_plan",
    "unreadable_plan",
    "non_checkpoint_plan",
    "missing_scope",
    "invalid_evidence",
})


@dataclass(frozen=True)
class _PathResolution:
    path: Path | None
    reason: str | None


@dataclass(frozen=True)
class _PlanRead:
    parsed: ParsedPlan | None
    path: Path | None
    reason: str | None


@dataclass(frozen=True)
class _ScopeProjection:
    index: int | None
    name: str | None
    valid: bool

    @property
    def checkpoint(self) -> dict[str, object] | None:
        if not self.valid:
            return None
        return {"index": self.index, "name": self.name}


def _bounded_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) <= _MAX_PATH_LENGTH:
        return text
    return text[: _MAX_PATH_LENGTH - 1] + "…"


def _bounded_summary(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if not text:
        return None
    if len(text) <= _MAX_SUMMARY_LENGTH:
        return text
    return text[: _MAX_SUMMARY_LENGTH - 1] + "…"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _append_root(roots: list[Path], value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            return
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return
    if not resolved.is_dir() or resolved in roots:
        return
    roots.append(resolved)


def _declared_roots(
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return declared primary and execution roots in deterministic order."""
    primary: list[Path] = []
    execution: list[Path] = []

    for key in ("repo_root", "primary_repo_root", "repository_root"):
        _append_root(primary, metadata.get(key))
    for key in ("execution_repo_root", "worktree_path", "execution_worktree"):
        _append_root(execution, metadata.get(key))

    controller = (
        manager_context.get("controller_state")
        if isinstance(manager_context, Mapping)
        else None
    )
    artifact_roots = (
        controller.get("artifact_roots")
        if isinstance(controller, Mapping)
        else None
    )
    if isinstance(artifact_roots, Mapping):
        _append_root(primary, artifact_roots.get("repository"))
        _append_root(execution, artifact_roots.get("execution"))
        _append_root(execution, artifact_roots.get("worktree"))

    return tuple(primary), tuple(execution)


def _inside_any(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_declared_file(
    value: object,
    *,
    primary_roots: tuple[Path, ...],
    execution_roots: tuple[Path, ...] = (),
) -> _PathResolution:
    if not isinstance(value, str) or not value.strip():
        return _PathResolution(None, "missing_plan")
    try:
        raw = Path(value)
    except (OSError, RuntimeError, ValueError):
        return _PathResolution(None, "invalid_evidence")

    roots = (*primary_roots, *execution_roots)
    if not roots:
        return _PathResolution(None, "invalid_evidence")
    candidates = (raw,) if raw.is_absolute() else tuple(root / raw for root in roots)
    saw_existing = False
    saw_escape = False
    saw_unreadable = False
    for candidate in candidates:
        try:
            # Check containment before requiring the file to exist so a
            # missing absolute escape is still rejected rather than reported
            # as an ordinary missing plan.
            resolved_candidate = candidate.resolve(strict=False)
            if not _inside_any(resolved_candidate, roots):
                saw_escape = True
                continue
            if candidate.exists() or candidate.is_symlink():
                saw_existing = True
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError):
            saw_unreadable = True
            continue
        if not _inside_any(resolved, roots):
            saw_escape = True
            continue
        if not resolved.is_file():
            saw_unreadable = True
            continue
        return _PathResolution(resolved, None)
    if saw_escape:
        return _PathResolution(None, "invalid_evidence")
    if saw_existing or saw_unreadable:
        return _PathResolution(None, "unreadable_plan")
    return _PathResolution(None, "missing_plan")


def _parse_plan_bytes(raw: bytes, *, source_path: Path) -> _PlanRead:
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return _PlanRead(None, None, "unreadable_plan")
    try:
        parsed = parse_plan_text(text, source_path=source_path)
    except PlanParseError as exc:
        if "no checkpoint sections were found" in str(exc):
            return _PlanRead(None, source_path, "non_checkpoint_plan")
        return _PlanRead(None, source_path, "invalid_evidence")
    return _PlanRead(parsed, source_path, None)


def _read_plan_path(resolution: _PathResolution) -> _PlanRead:
    if resolution.path is None:
        return _PlanRead(None, None, resolution.reason)
    try:
        raw = resolution.path.read_bytes()
    except (OSError, UnicodeError):
        return _PlanRead(None, resolution.path, "unreadable_plan")
    return _parse_plan_bytes(raw, source_path=resolution.path)


def _run_paths(run_dir: Path, primary_roots: tuple[Path, ...]):
    from aflow.runlog import RunPaths

    try:
        repo_root = primary_roots[0]
    except IndexError:
        repo_root = run_dir.parent.parent.parent.resolve()
    return RunPaths(
        repo_root=repo_root,
        runs_root=run_dir.parent,
        run_dir=run_dir,
        turns_dir=run_dir / "turns",
        manager_dir=run_dir / "manager",
        run_json=run_dir / "run.json",
    )


def _read_reference_text(
    run_dir: Path,
    reference: Mapping[str, Any],
    *,
    primary_roots: tuple[Path, ...],
) -> str | None:
    try:
        from aflow.runlog import resolve_evidence_artifact

        raw = resolve_evidence_artifact(
            _run_paths(run_dir, primary_roots), reference
        )
        return raw.decode("utf-8", "strict")
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _validated_scope_plan_text(
    run_dir: Path,
    scope: Mapping[str, Any],
    *,
    primary_roots: tuple[Path, ...],
) -> tuple[str | None, str | None]:
    """Return envelope-bound original bytes, or an explicit invalid verdict."""
    values = tuple(
        scope.get(key)
        for key in (
            "envelope_artifact_path",
            "envelope_artifact_sha256",
            "envelope_canonical_sha256",
        )
    )
    if not any(value is not None for value in values):
        return None, None
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None, "invalid_evidence"
    try:
        from aflow.manager_context import _resolve_validated_envelope

        envelope = _resolve_validated_envelope(
            run_dir,
            values[0],  # type: ignore[arg-type]
            values[1],  # type: ignore[arg-type]
            values[2],  # type: ignore[arg-type]
            scope,
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        envelope = None
    if envelope is None:
        return None, "invalid_evidence"
    embedded = envelope.get("plan_text")
    if isinstance(embedded, str):
        return embedded, None
    reference = envelope.get("plan_ref")
    if isinstance(reference, Mapping):
        text = _read_reference_text(
            run_dir, reference, primary_roots=primary_roots
        )
        return (text, None) if text is not None else (None, "invalid_evidence")
    return None, "invalid_evidence"


def _validated_context_envelope_plan_text(
    run_dir: Path,
    manager_context: Mapping[str, Any] | None,
    scope: Mapping[str, Any],
    *,
    primary_roots: tuple[Path, ...],
) -> tuple[str | None, str | None]:
    """Read original bytes from an already validated manager envelope."""
    candidates: list[tuple[Mapping[str, Any], bool]] = []
    if isinstance(manager_context, Mapping):
        envelope = manager_context.get("envelope")
        if isinstance(envelope, Mapping):
            candidates.append((envelope, False))
        controller = manager_context.get("controller_state")
        repartition = (
            controller.get("repartition_evidence")
            if isinstance(controller, Mapping)
            else None
        )
        summary = (
            repartition.get("envelope_summary")
            if isinstance(repartition, Mapping)
            else None
        )
        if isinstance(summary, Mapping):
            candidates.append((summary, repartition.get("status") == "validated"))

    for envelope, status_validated in candidates:
        if not status_validated:
            if envelope.get("available") is not True:
                continue
            if envelope.get("validated") is not True:
                return None, "invalid_evidence"
        for key in ("scope_id", "checkpoint_index", "checkpoint_name"):
            value = envelope.get(key)
            expected = scope.get(key)
            if expected is not None and value != expected:
                return None, "invalid_evidence"
        embedded = envelope.get("plan_text")
        if isinstance(embedded, str):
            return embedded, None
        if "plan_text" in envelope and embedded is not None:
            return None, "invalid_evidence"
        reference = envelope.get("plan_ref")
        if isinstance(reference, Mapping):
            text = _read_reference_text(
                run_dir, reference, primary_roots=primary_roots
            )
            return (text, None) if text is not None else (None, "invalid_evidence")
        if "plan_ref" in envelope and reference is not None:
            return None, "invalid_evidence"
        # Lite contexts intentionally carry only validated envelope metadata;
        # the declared original path or a separate v3 evidence reference can
        # still provide the bytes.
        continue
    return None, None


def _evidence_plan_text(
    run_dir: Path,
    manager_context: Mapping[str, Any] | None,
    *,
    primary_roots: tuple[Path, ...],
) -> tuple[str | None, str | None]:
    if not isinstance(manager_context, Mapping):
        return None, None
    evidence = manager_context.get("evidence")
    if not isinstance(evidence, Mapping):
        return None, None
    original = evidence.get("original_plan")
    if not isinstance(original, Mapping):
        return None, None
    if original.get("available") is not True:
        return None, None
    reference = original.get("reference")
    if original.get("shared_with") == "active_plan":
        # The v3 manifest deliberately omits a duplicate reference when the
        # original and active bytes are identical.  The declared original
        # path remains the exact source in that case.  If that path is gone,
        # the active reference is the same immutable byte sequence.
        active = evidence.get("active_plan")
        if isinstance(active, Mapping) and active.get("available") is True:
            active_reference = active.get("reference")
            if isinstance(active_reference, Mapping):
                text = _read_reference_text(
                    run_dir, active_reference, primary_roots=primary_roots
                )
                return (
                    (text, None)
                    if text is not None
                    else (None, "invalid_evidence")
                )
        return None, None
    if not isinstance(reference, Mapping):
        return None, "invalid_evidence"
    text = _read_reference_text(
        run_dir, reference, primary_roots=primary_roots
    )
    return (text, None) if text is not None else (None, "invalid_evidence")


def _load_boundary_capture(
    run_dir: Path, metadata: Mapping[str, Any], budget: _ProgressBudget | None = None
) -> Mapping[str, Any] | None:
    number = metadata.get("manager_decision_number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    path = run_dir / "manager" / f"decision-{number:03d}" / "boundary.json"
    if budget is None:
        if not _canonical_artifact_is_contained(run_dir, path):
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, Mapping) else None
    payload, _ = _canonical_read_json(
        path,
        budget,
        label=f"manager/decision-{number:03d}/boundary.json",
        root=run_dir,
    )
    return dict(payload) if payload is not None else None


def _captured_plan_state(
    metadata: Mapping[str, Any], boundary: Mapping[str, Any] | None
) -> tuple[object | None, bool]:
    if "captured_plan_state" in metadata:
        return metadata.get("captured_plan_state"), True
    if isinstance(boundary, Mapping):
        if "captured_plan_state" in boundary:
            return boundary.get("captured_plan_state"), True
    return None, False


def _captured_plan_read(
    state: object | None,
    *,
    scope: _ScopeProjection,
) -> _PlanRead:
    if state is None:
        return _PlanRead(None, None, None)
    if not isinstance(state, Mapping):
        return _PlanRead(None, None, "invalid_evidence")
    if state.get("active_repair_plan") is True:
        return _PlanRead(None, None, "invalid_evidence")
    checkpoints = state.get("checkpoints")
    complete = state.get("is_complete")
    if not isinstance(checkpoints, (list, tuple)) or not checkpoints:
        return _PlanRead(None, None, "invalid_evidence")
    if not isinstance(complete, bool):
        return _PlanRead(None, None, "invalid_evidence")
    names: list[str] = []
    for expected, item in enumerate(checkpoints, start=1):
        if not isinstance(item, Mapping):
            return _PlanRead(None, None, "invalid_evidence")
        if item.get("index") != expected or not isinstance(item.get("name"), str):
            return _PlanRead(None, None, "invalid_evidence")
        name = item["name"].strip()
        if not name:
            return _PlanRead(None, None, "invalid_evidence")
        names.append(name)
    if scope.valid and (
        scope.index is None
        or scope.index > len(names)
        or names[scope.index - 1] != scope.name
    ):
        return _PlanRead(None, None, "invalid_evidence")
    return _PlanRead(None, None, None)


def _scope_from_sources(
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, bool, bool]:
    """Return the current scope, preserving an explicit closed-scope null."""
    if "active_implementation_scope" in metadata:
        value = metadata.get("active_implementation_scope")
        if value is None:
            return None, True, False
        if isinstance(value, Mapping):
            return value, True, False
        return None, True, True
    if isinstance(manager_context, Mapping):
        controller = manager_context.get("controller_state")
        if isinstance(controller, Mapping) and "active_implementation_scope" in controller:
            value = controller.get("active_implementation_scope")
            if value is None:
                return None, True, False
            if isinstance(value, Mapping):
                return value, True, False
            return None, True, True
    return None, False, False


def _scope_projection(scope: Mapping[str, Any] | None) -> _ScopeProjection:
    if not isinstance(scope, Mapping):
        return _ScopeProjection(None, None, False)
    scope_id = scope.get("scope_id")
    index = scope.get("checkpoint_index")
    name = scope.get("checkpoint_name")
    valid = (
        isinstance(scope_id, str)
        and bool(scope_id.strip())
        and isinstance(index, int)
        and not isinstance(index, bool)
        and index > 0
        and isinstance(name, str)
        and bool(name.strip())
    )
    return _ScopeProjection(index if valid else None, name.strip() if valid else None, valid)


def _plan_value(
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    *,
    original: bool,
) -> object:
    if original:
        scope, _, _ = _scope_from_sources(metadata, manager_context)
        if isinstance(scope, Mapping) and isinstance(scope.get("original_plan_path"), str):
            return scope["original_plan_path"]
        for key in ("original_plan_path", "plan_path"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(manager_context, Mapping):
            plan_state = manager_context.get("plan_state")
            if isinstance(plan_state, Mapping):
                return plan_state.get("original_plan_path")
        return None
    value = metadata.get("active_plan_path")
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(metadata.get("plan_path"), str):
        return metadata.get("plan_path")
    if isinstance(manager_context, Mapping):
        plan_state = manager_context.get("plan_state")
        if isinstance(plan_state, Mapping):
            return plan_state.get("active_plan_path")
    return None


def _same_declared_file(left: Path | None, right: Path | None) -> bool:
    return left is not None and right is not None and left == right


def _repair_metadata(
    *,
    scope: Mapping[str, Any] | None,
    original_value: object,
    active_value: object,
    original_path: Path | None,
    active_path: Path | None,
) -> tuple[bool, str | None]:
    if not isinstance(scope, Mapping) or not _scope_projection(scope).valid:
        return False, None
    if active_path is not None and original_path is not None:
        repairing = not _same_declared_file(active_path, original_path)
    else:
        repairing = (
            isinstance(active_value, str)
            and isinstance(original_value, str)
            and active_value.strip() != original_value.strip()
        )
    return repairing, _bounded_path(active_value) if repairing else None


def _base_progress(
    *,
    availability: str,
    checkpoint: dict[str, object] | None,
    total: int | None,
    complete: bool | None,
    repairing: bool,
    overlay_path: str | None,
    reason: str | None,
) -> dict[str, Any]:
    if availability not in {"available", "partial", "unavailable"}:
        availability = "unavailable"
    if reason not in _PROGRESS_REASONS:
        reason = None
    return {
        "availability": availability,
        "checkpoint": checkpoint,
        "total": total,
        "complete": complete,
        "repairing": repairing,
        "overlay_path": overlay_path,
        "reason": reason,
    }


def _read_turn_records(run_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    turns_dir = run_dir / "turns"
    try:
        directories = sorted(path for path in turns_dir.iterdir() if path.is_dir())
    except OSError:
        return []
    records: list[tuple[Path, dict[str, Any]]] = []
    for turn_dir in directories:
        payload = _read_json(turn_dir / "result.json")
        if payload:
            records.append((turn_dir, payload))
    records.sort(key=lambda item: (
        item[1].get("turn_number")
        if isinstance(item[1].get("turn_number"), int)
        else 0,
        item[0].name,
    ))
    return records


def _finish_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _turn_summary(turn_dir: Path, record: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    returncode = record.get("returncode")
    if isinstance(returncode, int) and not isinstance(returncode, bool):
        parts.append(f"exit {returncode}")
    try:
        stdout = (turn_dir / "stdout.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        stdout = record.get("stdout") if isinstance(record.get("stdout"), str) else ""
    if stdout:
        try:
            from aflow.manager_context import extract_semantic_result
            from aflow.stop_marker import resolve_semantic_output_source

            result = extract_semantic_result(
                stdout,
                output_source=resolve_semantic_output_source(
                    record.get("semantic_output_source"), artifact_dir=turn_dir
                ),
            ).result
        except (OSError, TypeError, ValueError):
            result = stdout
        result_text = _bounded_summary(result)
        if result_text:
            parts.append(result_text)
    if len(parts) == 1 and parts[0].startswith("exit "):
        error = _bounded_summary(record.get("error"))
        if error:
            parts.append(error)
    return ": ".join(parts) if parts else None


def _turn_projection(
    turn_dir: Path,
    record: Mapping[str, Any],
    *,
    finalized: bool,
) -> dict[str, Any]:
    number = record.get("turn_number")
    number = number if isinstance(number, int) and not isinstance(number, bool) else None
    step = record.get("step_name")
    step = step.strip() if isinstance(step, str) and step.strip() else None
    status = record.get("status")
    status = status.strip() if isinstance(status, str) and status.strip() else None
    return {
        "turn_number": number,
        "step": step,
        "status": status,
        "summary": _turn_summary(turn_dir, record) if finalized else None,
    }


def _project_turns(run_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    records = _read_turn_records(run_dir)
    finished: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    for turn_dir, record in records:
        status = record.get("status")
        normalized_status = (
            status.strip().casefold()
            if isinstance(status, str) and status.strip()
            else None
        )
        is_finalized = (
            normalized_status is not None
            and normalized_status not in _ACTIVE_TURN_STATUSES
            and _finish_timestamp(record.get("finished_at"))
        )
        if is_finalized:
            finished = _turn_projection(turn_dir, record, finalized=True)
        elif normalized_status in _ACTIVE_TURN_STATUSES:
            current = _turn_projection(turn_dir, record, finalized=False)
    return finished, current


def _project_original_plan(
    plan: _PlanRead,
    *,
    repairing: bool,
    overlay_path: str | None,
) -> dict[str, Any]:
    if plan.parsed is None:
        return _base_progress(
            availability="unavailable",
            checkpoint=None,
            total=None,
            complete=None,
            repairing=repairing,
            overlay_path=overlay_path,
            reason=plan.reason or "missing_plan",
        )
    snapshot = plan.parsed.snapshot
    current = None
    if snapshot.current_checkpoint_index is not None:
        section = plan.parsed.sections[snapshot.current_checkpoint_index - 1]
        current = {"index": snapshot.current_checkpoint_index, "name": section.name}
    return _base_progress(
        availability="available",
        checkpoint=current,
        total=len(plan.parsed.sections),
        complete=snapshot.is_complete,
        repairing=repairing,
        overlay_path=overlay_path,
        reason=None,
    )


def _project_scope(
    scope: _ScopeProjection,
    plan: _PlanRead,
    *,
    repairing: bool,
    overlay_path: str | None,
) -> dict[str, Any]:
    checkpoint = scope.checkpoint
    if not scope.valid:
        return _base_progress(
            availability="unavailable",
            checkpoint=None,
            total=None,
            complete=None,
            repairing=repairing,
            overlay_path=overlay_path,
            reason="missing_scope",
        )
    if plan.parsed is None:
        return _base_progress(
            availability="partial",
            checkpoint=checkpoint,
            total=None,
            complete=None,
            repairing=repairing,
            overlay_path=overlay_path,
            reason=plan.reason or "missing_plan",
        )
    assert scope.index is not None
    assert scope.name is not None
    if scope.index > len(plan.parsed.sections):
        reason = "invalid_evidence"
        return _base_progress(
            availability="partial",
            checkpoint=checkpoint,
            total=None,
            complete=None,
            repairing=repairing,
            overlay_path=overlay_path,
            reason=reason,
        )
    section = plan.parsed.sections[scope.index - 1]
    if section.name != scope.name:
        return _base_progress(
            availability="partial",
            checkpoint=checkpoint,
            total=None,
            complete=None,
            repairing=repairing,
            overlay_path=overlay_path,
            reason="invalid_evidence",
        )
    return _base_progress(
        availability="available",
        checkpoint=checkpoint,
        total=len(plan.parsed.sections),
        complete=False,
        repairing=repairing,
        overlay_path=overlay_path,
        reason=None,
    )


def project_run_progress(
    run_dir: Path,
    *,
    run_metadata: Mapping[str, Any] | None = None,
    manager_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project truthful plan and turn progress without changing run artifacts."""
    root = Path(run_dir)
    metadata = (
        dict(run_metadata)
        if isinstance(run_metadata, Mapping)
        else _read_json(root / "run.json")
    )
    primary_roots, execution_roots = _declared_roots(
        metadata, manager_context
    )
    scope_mapping, scope_was_declared, scope_is_invalid = _scope_from_sources(
        metadata, manager_context
    )
    scope = _scope_projection(scope_mapping)

    original_value = _plan_value(metadata, manager_context, original=True)
    active_value = _plan_value(metadata, manager_context, original=False)
    original_resolution = _resolve_declared_file(
        original_value,
        primary_roots=primary_roots,
        execution_roots=(),
    )
    active_resolution = _resolve_declared_file(
        active_value,
        primary_roots=execution_roots or primary_roots,
    )
    repairing, overlay_path = _repair_metadata(
        scope=scope_mapping,
        original_value=original_value,
        active_value=active_value,
        original_path=original_resolution.path,
        active_path=active_resolution.path,
    )

    plan: _PlanRead
    if scope_was_declared and scope_mapping is not None:
        envelope_text, envelope_reason = _validated_scope_plan_text(
            root, scope_mapping, primary_roots=primary_roots
        )
        if envelope_reason is None and envelope_text is None:
            envelope_text, envelope_reason = _validated_context_envelope_plan_text(
                root,
                manager_context,
                scope_mapping,
                primary_roots=primary_roots,
            )
        if envelope_reason is not None:
            plan = _PlanRead(None, None, envelope_reason)
        elif envelope_text is not None:
            plan = _parse_plan_bytes(
                envelope_text.encode("utf-8"),
                source_path=original_resolution.path or Path("<captured original plan>"),
            )
        else:
            evidence_text, evidence_reason = _evidence_plan_text(
                root, manager_context, primary_roots=primary_roots
            )
            if evidence_reason is not None:
                plan = _PlanRead(None, None, evidence_reason)
            elif evidence_text is not None:
                plan = _parse_plan_bytes(
                    evidence_text.encode("utf-8"),
                    source_path=original_resolution.path or Path("<captured original plan>"),
                )
            else:
                plan = _read_plan_path(original_resolution)
                if plan.parsed is None and plan.reason in {"missing_plan", "unreadable_plan"}:
                    boundary = _load_boundary_capture(root, metadata)
                    captured, captured_present = _captured_plan_state(metadata, boundary)
                    captured_plan = _captured_plan_read(captured, scope=scope)
                    if captured_present and captured_plan.reason == "invalid_evidence":
                        plan = captured_plan
    else:
        evidence_text, evidence_reason = _validated_context_envelope_plan_text(
            root,
            manager_context,
            scope_mapping or {},
            primary_roots=primary_roots,
        )
        if evidence_reason is None and evidence_text is None:
            evidence_text, evidence_reason = _evidence_plan_text(
                root, manager_context, primary_roots=primary_roots
            )
        if evidence_reason is not None:
            plan = _PlanRead(None, None, evidence_reason)
        elif evidence_text is not None:
            plan = _parse_plan_bytes(
                evidence_text.encode("utf-8"),
                source_path=original_resolution.path or Path("<captured original plan>"),
            )
        else:
            plan = _read_plan_path(original_resolution)

    if scope_is_invalid:
        projected = _base_progress(
            availability="unavailable",
            checkpoint=None,
            total=None,
            complete=None,
            repairing=False,
            overlay_path=None,
            reason="missing_scope",
        )
    elif scope_was_declared and scope_mapping is not None:
        projected = _project_scope(
            scope,
            plan,
            repairing=repairing,
            overlay_path=overlay_path,
        )
    else:
        projected = _project_original_plan(
            plan,
            repairing=False,
            overlay_path=None,
        )

    last_finished_turn, current_turn = _project_turns(root)
    projected["last_finished_turn"] = last_finished_turn
    projected["current_turn"] = current_turn
    return bounded_redacted(projected)


_PROGRESS_MAX_JSON_BYTES = 8 * 1024 * 1024
_PROGRESS_MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
_PROGRESS_MAX_RECORDS = 2_000
_PROGRESS_MAX_CHECKPOINTS = 500
_PROGRESS_MAX_VISIBLE_EVENTS = 200
_PROGRESS_CACHE_TTL_SECONDS = 5.0
_PROGRESS_CACHE_MAX_RUNS = 128
_PROGRESS_CACHE_DISCOVERY_LIMIT = 2_000
_PROGRESS_CACHE: OrderedDict[
    tuple[object, ...], tuple[float, RunProgressDetail]
] = OrderedDict()
_PROGRESS_AVAILABILITIES = frozenset(
    {"complete", "partial", "unavailable", "not_applicable"}
)
_PROGRESS_COVERAGES = frozenset({"complete", "partial", "unavailable"})
_PROGRESS_CHECKPOINT_STATUSES = frozenset(
    {
        "pending",
        "implementing",
        "reviewing",
        "repairing",
        "approved",
        "recorded_complete",
        "blocked",
        "unknown",
    }
)


@dataclass
class _ProgressBudget:
    """Shared read budget for one immutable projection attempt."""

    evidence_bytes: int = 0
    records_read: int = 0
    checkpoints_read: int = 0
    events_read: int = 0
    omitted_records: int = 0
    omitted_checkpoints: int = 0
    response_limit_records: int = 0
    response_limit_checkpoints: int = 0
    unreadable_turn_numbers: set[int] = field(default_factory=set)
    unreadable_turn_order_unknown: bool = False
    notices: list[str] | None = None

    def __post_init__(self) -> None:
        if self.notices is None:
            self.notices = []

    def notice(self, value: str) -> None:
        assert self.notices is not None
        if value not in self.notices:
            self.notices.append(value)

    def reserve(self, size: int, *, label: str) -> bool:
        if size > _PROGRESS_MAX_JSON_BYTES:
            self.notice(f"{label} exceeds the 8 MiB artifact limit")
            return False
        if self.evidence_bytes + size > _PROGRESS_MAX_EVIDENCE_BYTES:
            self.notice("Earlier history unavailable in this bounded view")
            return False
        self.evidence_bytes += size
        return True

    @property
    def remaining_records(self) -> int:
        return max(0, _PROGRESS_MAX_RECORDS - self.records_read)

    @property
    def remaining_evidence_bytes(self) -> int:
        return max(0, _PROGRESS_MAX_EVIDENCE_BYTES - self.evidence_bytes)

    def reserve_record(self, *, label: str) -> bool:
        if self.records_read >= _PROGRESS_MAX_RECORDS:
            self.omitted_records += 1
            self.response_limit_records += 1
            self.notice("Earlier history unavailable in this bounded view")
            return False
        self.records_read += 1
        return True

    def truncation(self) -> RunProgressTruncation:
        return RunProgressTruncation(
            evidence_bytes=self.evidence_bytes,
            records_read=self.records_read,
            checkpoints_read=self.checkpoints_read,
            events_read=self.events_read,
            omitted_records=self.omitted_records,
            omitted_checkpoints=self.omitted_checkpoints,
            response_limit_records=self.response_limit_records,
            response_limit_checkpoints=self.response_limit_checkpoints,
            notices=tuple(self.notices or ()),
        )


@dataclass
class _CanonicalDiscovery:
    """Bounded direct-entry discovery shared by one projection attempt."""

    children: dict[str, tuple[tuple[Path, ...], int, bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class _CanonicalPlan:
    sections: tuple[Mapping[str, Any], ...] = ()
    path: Path | None = None
    raw: bytes | None = None
    identity: str | None = None
    display_name: str | None = None
    reason: str | None = None
    complete: bool | None = None
    captured: bool = False
    total_sections: int | None = None


@dataclass(frozen=True)
class _CanonicalTurn:
    record: Mapping[str, Any]
    path: Path | None


@dataclass(frozen=True)
class _Invocation:
    record: Mapping[str, Any]
    kind: Literal["worker", "reviewer"]
    source_run_id: str
    scope_id: str | None
    checkpoint_index: int | None
    checkpoint_name: str | None
    turn_number: int | None
    inherited: bool = False
    repair: bool = False
    runtime_retry: bool = False


def _canonical_text(value: object, *, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if not text:
        return None
    safe = bounded_redacted(text)
    if not isinstance(safe, str):
        return None
    return safe if len(safe) <= limit else safe[: limit - 1] + "…"


def _canonical_title(value: object) -> str | None:
    return _canonical_text(value, limit=240)


def _canonical_int(value: object, *, positive: bool = False) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if positive and value < 1:
        return None
    return value


def _canonical_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _canonical_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return text


def _canonical_duration(record: Mapping[str, Any]) -> float | None:
    value = record.get("duration_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    started = _canonical_timestamp(record.get("started_at"))
    finished = _canonical_timestamp(record.get("finished_at"))
    if started is None or finished is None:
        return None
    try:
        result = (
            datetime.fromisoformat(finished.replace("Z", "+00:00"))
            - datetime.fromisoformat(started.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return None
    return float(result) if result >= 0 else None


def _canonical_status(value: object) -> str | None:
    text = _canonical_text(value, limit=120)
    return text.casefold() if text is not None else None


def _canonical_source_run_id(value: object, *, current_run_id: str) -> str:
    text = _canonical_text(value, limit=128)
    return text or current_run_id


def _canonical_scope_id(record: Mapping[str, Any], fallback: str | None = None) -> str | None:
    for key in ("scope_id", "checkpoint_identity", "scope"):
        value = _canonical_text(record.get(key), limit=300)
        if value is not None:
            return value
    return fallback


def _canonical_checkpoint_index(record: Mapping[str, Any]) -> int | None:
    for key in ("checkpoint_index", "checkpoint_ordinal", "ordinal"):
        value = _canonical_int(record.get(key), positive=True)
        if value is not None:
            return value
    scope = record.get("active_implementation_scope")
    if isinstance(scope, Mapping):
        return _canonical_int(scope.get("checkpoint_index"), positive=True)
    return None


def _canonical_checkpoint_name(record: Mapping[str, Any]) -> str | None:
    for key in ("checkpoint_name", "checkpoint_title", "title"):
        value = _canonical_title(record.get(key))
        if value is not None:
            return value
    scope = record.get("active_implementation_scope")
    if isinstance(scope, Mapping):
        return _canonical_title(scope.get("checkpoint_name"))
    return None


def _canonical_file_signature(path: Path) -> tuple[object, ...]:
    try:
        stat = path.lstat()
    except OSError:
        return (False,)
    return (
        True,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        getattr(stat, "st_ino", 0),
    )


def _canonical_fingerprint(value: object, *, depth: int = 0) -> object:
    """Keep cache keys structural and exclude prompt/transcript-like values."""
    if depth > 6:
        return "[depth]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in list(value.items())[:256]:
            name = str(key)
            lowered = name.casefold()
            if any(
                part in lowered
                for part in ("prompt", "stdout", "stderr", "transcript", "secret", "token")
            ):
                continue
            result[name] = _canonical_fingerprint(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_fingerprint(item, depth=depth + 1) for item in list(value)[:256]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _canonical_artifact_is_contained(root: Path, path: Path) -> bool:
    """Accept only ordinary, non-symlink paths inside one run root."""
    try:
        if root.is_symlink() or not root.is_dir():
            return False
        relative = path.relative_to(root)
        current = root
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                return False
            current.lstat()
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _canonical_history_name_key(name: str) -> tuple[int, str]:
    suffix = name.rsplit("-", 1)[-1]
    try:
        return int(suffix), name
    except ValueError:
        return -1, name


def _canonical_direct_names(directory: Path) -> tuple[tuple[str, ...], int, bool]:
    """Retain bounded direct names without walking unbounded history."""
    selected: list[tuple[tuple[int, str], str]] = []
    count = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if count >= _PROGRESS_CACHE_DISCOVERY_LIMIT:
                    # The sentinel proves that coverage is incomplete without
                    # traversing the rest of an append-only history directory.
                    return (
                        tuple(name for _, name in sorted(selected, reverse=True)),
                        count + 1,
                        False,
                    )
                count += 1
                item = (_canonical_history_name_key(entry.name), entry.name)
                if len(selected) < _PROGRESS_CACHE_DISCOVERY_LIMIT:
                    heapq.heappush(selected, item)
                elif item > selected[0]:
                    heapq.heapreplace(selected, item)
    except OSError:
        return (), 0, True
    selected.sort(reverse=True)
    return tuple(name for _, name in selected), count, True


def _canonical_discovery_omission(
    budget: _ProgressBudget, *, directory_name: str, lower_bound: int
) -> None:
    """Disclose that an unordered bounded listing cannot establish recency."""
    budget.omitted_records += max(1, lower_bound)
    budget.notice(
        f"{directory_name.capitalize()} discovery stopped before the directory was exhausted; "
        "omitted history may include newer entries and omitted counts are lower bounds"
    )


def _canonical_discovery_is_incomplete(
    discovery: _CanonicalDiscovery | None, *directory_names: str
) -> bool:
    return bool(
        discovery is not None
        and any(
            cached is not None and not cached[2]
            for directory_name in directory_names
            for cached in (discovery.children.get(directory_name),)
        )
    )


def _canonical_history_children(
    root: Path,
    directory_name: str,
    *,
    budget: _ProgressBudget | None = None,
    discovery: _CanonicalDiscovery | None = None,
) -> list[Path]:
    directory = root / directory_name
    try:
        if not _canonical_artifact_is_contained(root, directory):
            if budget is not None and (directory.is_symlink() or directory.exists()):
                budget.omitted_records += 1
                budget.notice(
                    f"Some {directory_name} history is unavailable because an artifact is outside the run root"
                )
            return []
    except OSError:
        return []
    cached = discovery.children.get(directory_name) if discovery is not None else None
    if cached is not None:
        children, discovered_count, complete = cached
        if discovered_count > len(children) and budget is not None:
            if complete:
                budget.omitted_records += discovered_count - len(children)
                budget.notice("Earlier history unavailable in this bounded view")
            else:
                _canonical_discovery_omission(
                    budget,
                    directory_name=directory_name,
                    lower_bound=discovered_count - len(children),
                )
        return list(children)
    names, discovered_count, complete = _canonical_direct_names(directory)
    children = tuple(directory / name for name in names)
    result: list[Path] = []
    for child in children:
        try:
            if child.is_symlink():
                if budget is not None:
                    budget.omitted_records += 1
                    budget.notice(
                        f"Some {directory_name} history is unavailable because an artifact is symlinked"
                    )
                continue
            if child.is_dir():
                if _canonical_artifact_is_contained(root, child):
                    result.append(child)
                elif budget is not None:
                    budget.omitted_records += 1
                    budget.notice(
                        f"Some {directory_name} history is unavailable because an artifact is outside the run root"
                    )
        except OSError:
            continue
    result.sort(key=lambda path: _canonical_history_name_key(path.name), reverse=True)
    if discovery is not None and cached is None:
        discovery.children[directory_name] = (tuple(result), discovered_count, complete)
    if discovered_count > len(children) and budget is not None:
        if complete:
            budget.omitted_records += discovered_count - len(children)
            budget.notice("Earlier history unavailable in this bounded view")
        else:
            _canonical_discovery_omission(
                budget,
                directory_name=directory_name,
                lower_bound=discovered_count - len(children),
            )
    return result


def _canonical_category_available(
    root: Path, category: str, *, discovery: _CanonicalDiscovery | None = None
) -> bool:
    if category == "events":
        path = root / "events.jsonl"
        try:
            return _canonical_artifact_is_contained(root, path) and path.is_file()
        except OSError:
            return False
    return bool(_canonical_history_children(root, category, discovery=discovery))


def _canonical_later_category_reservation(
    root: Path,
    categories: tuple[str, ...],
    *,
    discovery: _CanonicalDiscovery | None = None,
) -> int:
    """Keep one newest record available for each later evidence category."""
    return sum(
        _canonical_category_available(root, category, discovery=discovery)
        for category in categories
    )


def _canonical_artifact_size(root: Path, path: Path) -> int | None:
    try:
        if not _canonical_artifact_is_contained(root, path):
            return None
        return path.lstat().st_size
    except OSError:
        return None


def _canonical_later_category_byte_reservation(
    root: Path,
    categories: tuple[str, ...],
    *,
    budget: _ProgressBudget,
    discovery: _CanonicalDiscovery | None = None,
) -> int:
    """Reserve the newest retained artifact in each later evidence category."""
    total = 0

    def available(path: Path) -> int:
        size = _canonical_artifact_size(root, path)
        if (
            size is None
            or size > _PROGRESS_MAX_JSON_BYTES
            or total + size > budget.remaining_evidence_bytes
        ):
            return 0
        return size

    for category in categories:
        if category == "events":
            total += available(root / "events.jsonl")
            continue
        children = _canonical_history_children(root, category, discovery=discovery)
        if not children:
            continue
        for name in ("result.json", "boundary.json"):
            total += available(children[0] / name)
    return total


def _canonical_artifact_fits_remaining_budget(
    root: Path,
    path: Path,
    budget: _ProgressBudget,
    *,
    label: str,
    reserved_bytes: int,
) -> bool:
    """Skip one unreadable artifact without abandoning its bounded category."""
    size = _canonical_artifact_size(root, path)
    if size is None:
        return True
    if size > _PROGRESS_MAX_JSON_BYTES:
        budget.omitted_records += 1
        budget.notice(f"{label} exceeds the 8 MiB artifact limit")
        return False
    if size + reserved_bytes > budget.remaining_evidence_bytes:
        budget.omitted_records += 1
        budget.notice("Earlier history unavailable in this bounded view")
        return False
    return True


def _canonical_cache_artifact_paths(
    root: Path, directory_name: str, *, discovery: _CanonicalDiscovery | None = None
) -> list[Path]:
    """Discover only the bounded, newest direct evidence paths for caching."""
    directory = root / directory_name
    try:
        if not _canonical_artifact_is_contained(root, directory):
            return []
    except OSError:
        return []
    if directory_name == "evidence":
        names, _, _ = _canonical_direct_names(directory)
        entries = tuple(directory / name for name in names)
    else:
        entries = _canonical_history_children(root, directory_name, discovery=discovery)

    paths: list[Path] = []
    if directory_name == "evidence":
        files = [
            path
            for path in entries
            if not path.is_symlink()
            and path.is_file()
            and path.suffix in {".json", ".md"}
            and _canonical_artifact_is_contained(root, path)
        ]
        files.sort(key=lambda path: _canonical_history_name_key(path.name), reverse=True)
        return files

    for child in entries:
        for name in ("result.json", "boundary.json"):
            candidate = child / name
            if (
                not candidate.is_symlink()
                and candidate.is_file()
                and _canonical_artifact_is_contained(root, candidate)
            ):
                paths.append(candidate)
    return paths


def _canonical_cache_key(
    root: Path,
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    *,
    activity: str | None = None,
    phase: str | None = None,
    run_status: str | None = None,
    discovery: _CanonicalDiscovery | None = None,
) -> tuple[object, ...]:
    original = _plan_value(metadata, manager_context, original=True)
    active = _plan_value(metadata, manager_context, original=False)
    paths = [
        root / "run.json",
        root / "events.jsonl",
        root / "publication.json",
    ]
    declared_roots, execution_roots = _declared_roots(metadata, manager_context)
    for value in (original, active):
        if isinstance(value, str):
            candidate = Path(value)
            if not candidate.is_absolute():
                roots = (*declared_roots, *execution_roots, root)
                candidate = next(
                    (base / candidate for base in roots if (base / candidate).exists()),
                    root / candidate,
                )
            paths.append(candidate)
    for directory_name in ("turns", "manager", "evidence"):
        paths.extend(
            _canonical_cache_artifact_paths(
                root, directory_name, discovery=discovery
            )
        )
    project_identity = metadata.get("repo_root") or metadata.get("primary_repo_root")
    structural = json.dumps(
        {
            "metadata": _canonical_fingerprint(metadata),
            "manager_context": _canonical_fingerprint(manager_context),
            "activity": activity,
            "phase": phase,
            "run_status": run_status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        str(root.resolve()),
        str(project_identity) if project_identity is not None else None,
        tuple((str(path), _canonical_file_signature(path)) for path in paths),
        hashlib.sha256(structural.encode("utf-8")).hexdigest(),
    )


def _canonical_read_bytes(
    path: Path,
    budget: _ProgressBudget,
    *,
    label: str,
    tail: bool = False,
    root: Path | None = None,
) -> bytes | None:
    try:
        if root is not None and not _canonical_artifact_is_contained(root, path):
            if path.is_symlink() or path.exists():
                budget.omitted_records += 1
                budget.notice(f"{label} is outside the authorized run root")
            return None
        if path.is_symlink() or not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None
    if size > _PROGRESS_MAX_JSON_BYTES:
        budget.notice(f"{label} exceeds the 8 MiB artifact limit")
        budget.omitted_records += 1
        return None
    if not budget.reserve(size, label=label):
        budget.omitted_records += 1
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != size:
        budget.notice(f"{label} changed during the bounded read")
        return None
    if tail and len(data) > _PROGRESS_MAX_VISIBLE_EVENTS * 1024:
        return data[-_PROGRESS_MAX_VISIBLE_EVENTS * 1024 :]
    return data


def _canonical_read_json(
    path: Path,
    budget: _ProgressBudget,
    *,
    label: str,
    root: Path | None = None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    if root is not None and not _canonical_artifact_is_contained(root, path):
        if path.is_symlink() or path.exists():
            budget.omitted_records += 1
            budget.notice(f"{label} is outside the authorized run root")
        return None, "unavailable"
    try:
        if path.is_symlink() or not path.is_file():
            return None, "unavailable"
    except OSError:
        return None, "unavailable"
    if not budget.reserve_record(label=label):
        return None, "truncated"
    data = _canonical_read_bytes(path, budget, label=label, root=root)
    if data is None:
        return None, "unavailable"
    try:
        payload = json.loads(data.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        budget.omitted_records += 1
        budget.notice(f"{label} is malformed")
        return None, "invalid_evidence"
    if not isinstance(payload, Mapping):
        budget.omitted_records += 1
        budget.notice(f"{label} is not an object")
        return None, "invalid_evidence"
    return dict(payload), None


def _canonical_resolve_plan(
    run_dir: Path,
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    budget: _ProgressBudget,
) -> _CanonicalPlan:
    primary_roots, execution_roots = _declared_roots(metadata, manager_context)
    scope_mapping, scope_declared, scope_invalid = _scope_from_sources(
        metadata, manager_context
    )
    if scope_invalid:
        return _CanonicalPlan(reason="invalid_evidence")

    original_value = _plan_value(metadata, manager_context, original=True)
    resolution = _resolve_declared_file(
        original_value,
        primary_roots=primary_roots,
        execution_roots=(),
    )
    raw: bytes | None = None
    path = resolution.path
    reason = resolution.reason
    scope_evidence_invalid = False

    # A complete scope envelope is preferred over the mutable plan path.  The
    # existing resolver validates its digest, containment, and scope binding;
    # this function only adds the projection's byte budget around that read.
    if scope_declared and scope_mapping is not None:
        envelope_text, envelope_reason = _validated_scope_plan_text(
            run_dir, scope_mapping, primary_roots=primary_roots
        )
        if envelope_reason is None and envelope_text is None:
            envelope_text, envelope_reason = _validated_context_envelope_plan_text(
                run_dir,
                manager_context,
                scope_mapping,
                primary_roots=primary_roots,
            )
        if envelope_reason is not None:
            reason = envelope_reason
            scope_evidence_invalid = True
        elif envelope_text is not None:
            raw = envelope_text.encode("utf-8")
            if len(raw) > _PROGRESS_MAX_JSON_BYTES or not budget.reserve(
                len(raw), label="validated plan evidence"
            ):
                return _CanonicalPlan(
                    path=path,
                    reason="invalid_evidence",
                    display_name=path.name if path is not None else None,
                )

    if raw is None and path is not None and not scope_evidence_invalid:
        raw = _canonical_read_bytes(path, budget, label="original plan")
        if raw is None:
            reason = reason or "unreadable_plan"

    if (
        raw is None
        and not scope_evidence_invalid
        and reason in {"missing_plan", "unreadable_plan", None}
    ):
        boundary = _load_boundary_capture(run_dir, metadata, budget)
        captured, captured_present = _captured_plan_state(metadata, boundary)
        captured_plan = _captured_plan_read(captured, scope=_scope_projection(scope_mapping))
        if captured_present and captured_plan.reason == "invalid_evidence":
            reason = "invalid_evidence"
        elif captured_present and isinstance(captured, Mapping):
            raw_names = captured.get("checkpoints")
            if isinstance(raw_names, (list, tuple)):
                total_sections = len(raw_names)
                omitted_sections = max(0, total_sections - _PROGRESS_MAX_CHECKPOINTS)
                if omitted_sections:
                    budget.omitted_checkpoints += omitted_sections
                    budget.response_limit_checkpoints += omitted_sections
                    budget.notice("Earlier checkpoints unavailable in this bounded view")
                sections: list[Mapping[str, Any]] = []
                for item in raw_names[:_PROGRESS_MAX_CHECKPOINTS]:
                    if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                        sections.append(dict(item))
                if sections:
                    budget.checkpoints_read = len(sections)
                    return _CanonicalPlan(
                        sections=tuple(sections),
                        path=path,
                        identity=_canonical_plan_identity(
                            path, None, metadata, lineage_value=original_value
                        ),
                        display_name=path.name if path is not None else None,
                        complete=(
                            captured.get("is_complete")
                            if isinstance(captured.get("is_complete"), bool)
                            else None
                        ),
                        captured=True,
                        total_sections=total_sections,
                    )

    if raw is None:
        return _CanonicalPlan(
            path=path,
            display_name=path.name if path is not None else None,
            reason=reason or "missing_plan",
        )
    parsed = _parse_plan_bytes(raw, source_path=path or Path("<captured original plan>"))
    if parsed.parsed is None:
        return _CanonicalPlan(
            path=path,
            raw=raw,
            display_name=path.name if path is not None else None,
            reason=parsed.reason or "invalid_evidence",
        )
    if len(parsed.parsed.sections) > _PROGRESS_MAX_CHECKPOINTS:
        budget.omitted_checkpoints += len(parsed.parsed.sections) - _PROGRESS_MAX_CHECKPOINTS
        budget.response_limit_checkpoints += len(parsed.parsed.sections) - _PROGRESS_MAX_CHECKPOINTS
        budget.notice("Earlier checkpoints unavailable in this bounded view")
    sections = tuple(
        {
            "index": index,
            "name": section.name,
            "heading_checked": section.heading_checked,
            "checked_step_count": section.checked_step_count,
            "unchecked_step_count": section.unchecked_step_count,
        }
        for index, section in enumerate(
            parsed.parsed.sections[:_PROGRESS_MAX_CHECKPOINTS], start=1
        )
    )
    budget.checkpoints_read = len(sections)
    return _CanonicalPlan(
        sections=sections,
        path=path,
        raw=raw,
        identity=_canonical_plan_identity(
            path, raw, metadata, lineage_value=original_value
        ),
        display_name=path.name if path is not None else None,
        complete=parsed.parsed.snapshot.is_complete,
        total_sections=len(parsed.parsed.sections),
    )


def _canonical_plan_identity(
    path: Path | None,
    raw: bytes | None,
    metadata: Mapping[str, Any],
    *,
    lineage_value: object | None = None,
) -> str | None:
    explicit = _canonical_text(
        metadata.get("original_plan_identity") or metadata.get("plan_identity"),
        limit=240,
    )
    if explicit is not None:
        return explicit
    lineage_path = path
    if lineage_path is None:
        value = lineage_value
        if not isinstance(value, str) or not value.strip():
            value = metadata.get("original_plan_path") or metadata.get("plan_path")
        if isinstance(value, str) and value.strip():
            try:
                lineage_path = Path(value)
                if not lineage_path.is_absolute():
                    repo_root = metadata.get("repo_root") or metadata.get(
                        "primary_repo_root"
                    )
                    if isinstance(repo_root, str) and repo_root.strip():
                        lineage_path = Path(repo_root) / lineage_path
                lineage_path = lineage_path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                lineage_path = None
    if lineage_path is not None:
        repo_root_value = metadata.get("repo_root") or metadata.get("primary_repo_root")
        if isinstance(repo_root_value, str) and repo_root_value.strip():
            try:
                owner = plan_identity_for_path(Path(repo_root_value), lineage_path)
            except (OSError, RuntimeError, ValueError):
                owner = None
            if owner is not None:
                return owner
        try:
            lineage = str(lineage_path.resolve(strict=False))
        except (OSError, RuntimeError):
            lineage = str(lineage_path)
        return "plan-lineage:" + hashlib.sha256(lineage.encode("utf-8")).hexdigest()
    return None


def _canonical_turns(
    run_dir: Path, budget: _ProgressBudget, *, discovery: _CanonicalDiscovery | None = None
) -> tuple[_CanonicalTurn, ...]:
    children = _canonical_history_children(
        run_dir, "turns", budget=budget, discovery=discovery
    )
    records: list[_CanonicalTurn] = []
    reserved_for_later = _canonical_later_category_reservation(
        run_dir, ("manager", "events"), discovery=discovery
    )
    reserved_bytes_for_later = _canonical_later_category_byte_reservation(
        run_dir, ("manager", "events"), budget=budget, discovery=discovery
    )
    selected = children[: max(0, budget.remaining_records - reserved_for_later)]
    if len(selected) < len(children):
        budget.omitted_records += len(children) - len(selected)
        budget.response_limit_records += len(children) - len(selected)
        budget.notice("Earlier history unavailable in this bounded view")
    for child in selected:
        path = child / "result.json"
        label = f"{child.name}/result.json"
        turn_number = _canonical_history_name_key(child.name)[0]

        def mark_unreadable_turn() -> None:
            if turn_number > 0:
                budget.unreadable_turn_numbers.add(turn_number)
            else:
                budget.unreadable_turn_order_unknown = True

        if not _canonical_artifact_fits_remaining_budget(
            run_dir,
            path,
            budget,
            label=label,
            reserved_bytes=reserved_bytes_for_later,
        ):
            mark_unreadable_turn()
            continue
        payload, reason = _canonical_read_json(
            path,
            budget,
            label=label,
            root=run_dir,
        )
        if payload is not None:
            records.append(_CanonicalTurn(payload, child))
        else:
            mark_unreadable_turn()
            if reason == "unavailable":
                budget.omitted_records += 1
            budget.notice("Some turn evidence is unavailable")
    records.sort(
        key=lambda item: (
            _canonical_int(item.record.get("turn_number")) or 0,
            str(item.path) if item.path is not None else "",
        )
    )
    return tuple(records)


def _canonical_manager_records(
    run_dir: Path, budget: _ProgressBudget, *, discovery: _CanonicalDiscovery | None = None
) -> tuple[Mapping[str, Any], ...]:
    children = _canonical_history_children(
        run_dir, "manager", budget=budget, discovery=discovery
    )
    records: list[Mapping[str, Any]] = []
    reserved_for_later = _canonical_later_category_reservation(
        run_dir, ("events",), discovery=discovery
    )
    reserved_bytes_for_later = _canonical_later_category_byte_reservation(
        run_dir, ("events",), budget=budget, discovery=discovery
    )
    for position, child in enumerate(children):
        if budget.remaining_records <= reserved_for_later:
            budget.omitted_records += len(children) - position
            budget.response_limit_records += len(children) - position
            budget.notice("Earlier manager history unavailable in this bounded view")
            break
        result_path = child / "result.json"
        result_label = f"{child.name}/result.json"
        payload: Mapping[str, Any] | None = None
        if _canonical_artifact_fits_remaining_budget(
            run_dir,
            result_path,
            budget,
            label=result_label,
            reserved_bytes=reserved_bytes_for_later,
        ):
            payload, _ = _canonical_read_json(
                result_path,
                budget,
                label=result_label,
                root=run_dir,
            )
        boundary_path = child / "boundary.json"
        boundary_label = f"{child.name}/boundary.json"
        boundary: Mapping[str, Any] | None = None
        if (
            budget.remaining_records > reserved_for_later
            and _canonical_artifact_fits_remaining_budget(
                run_dir,
                boundary_path,
                budget,
                label=boundary_label,
                reserved_bytes=reserved_bytes_for_later,
            )
        ):
            boundary, _ = _canonical_read_json(
                boundary_path,
                budget,
                label=boundary_label,
                root=run_dir,
            )
        if payload is not None or boundary is not None:
            item = dict(payload or {})
            item["_artifact"] = f"manager/{child.name}/result.json"
            if boundary is not None:
                nested = boundary.get("boundary")
                if isinstance(nested, Mapping):
                    for key in (
                        "active_implementation_scope",
                        "implementation_attempts",
                        "review_rejection_history",
                        "repartition_history",
                        "original_plan_path",
                        "active_plan_path",
                    ):
                        if key in nested and key not in item:
                            item[key] = nested[key]
                    context = dict(nested)
                    for key in ("run_metadata", "validated_controller_context"):
                        value = boundary.get(key)
                        if isinstance(value, Mapping):
                            context[key] = dict(value)
                    item["_boundary"] = context
            records.append(item)
    return tuple(records)


def _canonical_events(
    run_dir: Path, budget: _ProgressBudget
) -> tuple[Mapping[str, Any], ...]:
    path = run_dir / "events.jsonl"
    try:
        if not _canonical_artifact_is_contained(run_dir, path):
            return ()
    except OSError:
        return ()
    raw = _canonical_read_bytes(path, budget, label="events.jsonl", root=run_dir)
    if raw is None:
        try:
            if path.is_file():
                budget.omitted_records += 1
                budget.notice("Some event history is unavailable")
        except OSError:
            pass
        return ()
    lines = raw.splitlines()
    selected = lines[-budget.remaining_records:] if budget.remaining_records else []
    if len(selected) < len(lines):
        budget.omitted_records += len(lines) - len(selected)
        budget.response_limit_records += len(lines) - len(selected)
        budget.notice("Earlier event history unavailable in this bounded view")
    records: list[Mapping[str, Any]] = []
    for line in selected:
        if not budget.reserve_record(label="events.jsonl record"):
            break
        try:
            payload = json.loads(line.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            budget.omitted_records += 1
            budget.notice("Some event history is malformed")
            continue
        if isinstance(payload, Mapping):
            records.append(dict(payload))
            budget.events_read += 1
        else:
            budget.omitted_records += 1
            budget.notice("Some event history is malformed")
    return tuple(records)


def _canonical_scope_checkpoint_index(scope_id: str | None) -> int | None:
    """Read only the stable ``original::checkpoint-N`` scope-key suffix."""
    if scope_id is None:
        return None
    prefix, marker, suffix = scope_id.rpartition("::checkpoint-")
    if not prefix or not marker:
        return None
    try:
        return _canonical_int(int(suffix), positive=True)
    except ValueError:
        return None


def _canonical_original_plan_matches(
    record: Mapping[str, Any], *, plan: _CanonicalPlan
) -> bool:
    if plan.path is None:
        return False
    original_path = _canonical_text(
        record.get("original_plan_path"), limit=_MAX_PATH_LENGTH
    )
    if original_path is None:
        return False
    try:
        candidate = Path(original_path)
        return (
            candidate.is_absolute()
            and candidate.resolve(strict=False) == plan.path.resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _canonical_whole_plan_review(
    record: Mapping[str, Any], *, plan: _CanonicalPlan, current_run_id: str
) -> bool:
    """Recognize a reviewer that ran after a complete, identity-bound plan."""
    if _canonical_turn_role(record) != "reviewer":
        return False
    if (
        _canonical_source_run_id(
            record.get("source_run_id") or record.get("run_id"),
            current_run_id=current_run_id,
        )
        != current_run_id
        or _canonical_scope_id(record) is not None
        or _canonical_checkpoint_index(record) is not None
        or _canonical_checkpoint_name(record) is not None
        or not _canonical_original_plan_matches(record, plan=plan)
    ):
        return False
    snapshot = record.get("snapshot_before")
    if not isinstance(snapshot, Mapping):
        return False
    total = _canonical_int(snapshot.get("total_checkpoint_count"), positive=True)
    current_name = snapshot.get("current_checkpoint_name")
    return (
        snapshot.get("is_complete") is True
        and snapshot.get("current_checkpoint_index") is None
        and (current_name is None or current_name == "")
        and snapshot.get("unchecked_checkpoint_count") == 0
        and snapshot.get("current_checkpoint_unchecked_step_count") == 0
        and total is not None
        and plan.total_sections is not None
        and total == plan.total_sections
    )


def _canonical_historical_turn_association(
    record: Mapping[str, Any], *, plan: _CanonicalPlan, current_run_id: str
) -> tuple[int, str | None] | None:
    """Validate a result's pre-turn original-plan checkpoint, never post-turn state."""
    if plan.path is None:
        return None
    source_run_id = _canonical_source_run_id(
        record.get("source_run_id") or record.get("run_id"),
        current_run_id=current_run_id,
    )
    if source_run_id != current_run_id:
        return None
    if not _canonical_original_plan_matches(record, plan=plan):
        return None
    snapshot = record.get("snapshot_before")
    if not isinstance(snapshot, Mapping):
        return None
    ordinal = _canonical_int(snapshot.get("current_checkpoint_index"), positive=True)
    if ordinal is None or ordinal > len(plan.sections):
        return None
    name = _canonical_title(snapshot.get("current_checkpoint_name"))
    expected_name = _canonical_title(plan.sections[ordinal - 1].get("name"))
    if name is not None and expected_name is not None and name != expected_name:
        return None
    return ordinal, expected_name


def _canonical_reviewer_scope_association(
    record: Mapping[str, Any],
    *,
    turn_number: int | None,
    workers: list[_Invocation],
    active_scope: Mapping[str, Any] | None,
    activity: str,
    manager_records: tuple[Mapping[str, Any], ...],
    current_run_id: str,
) -> tuple[str, int, str | None] | None:
    """Resolve reviewer lineage without treating its post-worker snapshot as scope."""
    if turn_number is None:
        return None

    def scope_values(scope: Mapping[str, Any] | None) -> tuple[str, int, str | None] | None:
        if not isinstance(scope, Mapping):
            return None
        scope_id = _canonical_scope_id(scope)
        index = _canonical_checkpoint_index(scope)
        if scope_id is None or index is None:
            return None
        return scope_id, index, _canonical_checkpoint_name(scope)

    def compatible(scope: tuple[str, int, str | None]) -> bool:
        supplied_scope = _canonical_scope_id(record)
        supplied_index = _canonical_checkpoint_index(record)
        supplied_name = _canonical_checkpoint_name(record)
        return (
            (supplied_scope is None or supplied_scope == scope[0])
            and (supplied_index is None or supplied_index == scope[1])
            and (
                supplied_name is None
                or scope[2] is None
                or supplied_name == scope[2]
            )
        )

    def same_path(left: str, right: str) -> bool:
        try:
            return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return left == right

    record_original_path = _canonical_text(
        record.get("original_plan_path"), limit=_MAX_PATH_LENGTH
    )

    def retained_scope(
        scope: Mapping[str, Any] | None,
        boundary: Mapping[str, Any],
        *,
        awaiting_review: bool | None = None,
    ) -> tuple[str, int, str | None] | None:
        """Validate a scope retained by a local manager boundary."""
        values = scope_values(scope)
        if values is None or not compatible(values) or not isinstance(scope, Mapping):
            return None
        if awaiting_review is not None and scope.get("awaiting_review") is not awaiting_review:
            return None
        scope_original_path = _canonical_text(
            scope.get("original_plan_path"), limit=_MAX_PATH_LENGTH
        )
        boundary_original_path = _canonical_text(
            boundary.get("original_plan_path"), limit=_MAX_PATH_LENGTH
        )
        if (
            scope_original_path is None
            or record_original_path is None
            or not same_path(scope_original_path, record_original_path)
            or (
                boundary_original_path is not None
                and not same_path(boundary_original_path, scope_original_path)
            )
        ):
            return None
        return values

    def completed_worker(worker: _Invocation, scope_id: str) -> bool:
        if worker.source_run_id != current_run_id or worker.scope_id != scope_id:
            return False
        if worker.turn_number is not None and worker.turn_number >= turn_number:
            return False
        status = _canonical_status(worker.record.get("status"))
        outcome = _canonical_status(worker.record.get("outcome"))
        if status in {"failed", "interrupted", "stopped"} or outcome in {
            "failed",
            "rejected",
            "interrupted",
            "stopped",
        }:
            return False
        return (
            status in {"completed", "complete", "succeeded"}
            or outcome in {"completed", "complete", "accepted", "succeeded"}
            or _canonical_timestamp(worker.record.get("finished_at")) is not None
        )

    def completed_reviewer() -> bool:
        status = _canonical_status(record.get("status"))
        outcome = _canonical_status(record.get("outcome"))
        if status in {"failed", "interrupted", "stopped"} or outcome in {
            "failed",
            "rejected",
            "interrupted",
            "stopped",
        }:
            return False
        return (
            status in {"completed", "complete", "succeeded"}
            or outcome in {"completed", "complete", "accepted", "succeeded"}
            or _canonical_timestamp(record.get("finished_at")) is not None
        )

    live_scope = scope_values(active_scope)
    if (
        activity == "active"
        and _canonical_status(record.get("status")) in _ACTIVE_TURN_STATUSES
        and isinstance(active_scope, Mapping)
        and active_scope.get("awaiting_review") is True
        and live_scope is not None
        and compatible(live_scope)
        and any(completed_worker(worker, live_scope[0]) for worker in workers)
    ):
        return live_scope

    same_turn_scopes: set[tuple[str, int, str | None]] = set()
    for manager_record in manager_records:
        boundary = manager_record.get("_boundary")
        if not isinstance(boundary, Mapping):
            continue
        finalized_turn = _canonical_int(
            boundary.get("finalized_turn_number"), positive=True
        )
        if finalized_turn != turn_number:
            continue
        role = _canonical_turn_role(boundary)
        if role is not None and role != "reviewer":
            continue
        scope = boundary.get("active_implementation_scope")
        raw_scope = scope_values(scope if isinstance(scope, Mapping) else None)
        if raw_scope is None:
            continue
        resolved_scope = retained_scope(
            scope if isinstance(scope, Mapping) else None, boundary
        )
        if resolved_scope is None:
            return None
        same_turn_scopes.add(resolved_scope)
    if len(same_turn_scopes) == 1:
        return next(iter(same_turn_scopes))
    if len(same_turn_scopes) > 1 or not completed_reviewer() or turn_number <= 1:
        return None

    pre_review_scopes: set[tuple[str, int, str | None]] = set()
    for manager_record in manager_records:
        boundary = manager_record.get("_boundary")
        if not isinstance(boundary, Mapping):
            continue
        finalized_turn = _canonical_int(
            boundary.get("finalized_turn_number"), positive=True
        )
        if finalized_turn != turn_number - 1:
            continue
        role = _canonical_turn_role(boundary)
        if role is not None and role != "worker":
            continue
        scope = boundary.get("active_implementation_scope")
        raw_scope = scope_values(scope if isinstance(scope, Mapping) else None)
        if raw_scope is None:
            continue
        resolved_scope = retained_scope(
            scope if isinstance(scope, Mapping) else None,
            boundary,
            awaiting_review=True,
        )
        if resolved_scope is None:
            return None
        pre_review_scopes.add(resolved_scope)
    if len(pre_review_scopes) != 1:
        return None
    resolved_scope = next(iter(pre_review_scopes))
    if any(
        worker.turn_number == turn_number - 1
        and completed_worker(worker, resolved_scope[0])
        for worker in workers
    ):
        return resolved_scope
    return None


def _canonical_attempt_sources(
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
) -> list[tuple[str | None, Mapping[str, Any]]]:
    """Yield structured attempt records with their enclosing scope identity."""
    sources: list[tuple[str | None, Mapping[str, Any]]] = []

    def collect(value: object) -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    sources.append((None, dict(item)))
            return
        if not isinstance(value, Mapping):
            return
        if isinstance(value.get("attempts"), (list, tuple)):
            group_scope = _canonical_scope_id(value)
            for item in value["attempts"]:
                if isinstance(item, Mapping):
                    sources.append((group_scope, dict(item)))
            return
        for key, items in value.items():
            if not isinstance(items, (list, tuple)):
                continue
            group_scope = _canonical_text(key, limit=300)
            for item in items:
                if isinstance(item, Mapping):
                    sources.append((group_scope, dict(item)))

    collect(metadata.get("implementation_attempts"))
    if isinstance(manager_context, Mapping):
        collect(manager_context.get("implementation_attempts"))
        controller = manager_context.get("controller_state")
        if isinstance(controller, Mapping):
            collect(controller.get("implementation_attempts"))
    return sources


def _canonical_rejection_sources(
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    manager_records: tuple[Mapping[str, Any], ...] = (),
) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []

    def collect(value: object) -> None:
        if isinstance(value, (list, tuple)):
            records.extend(dict(item) for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            records.append(dict(value))

    collect(metadata.get("review_rejection_history"))
    if isinstance(manager_context, Mapping):
        collect(manager_context.get("active_scope_rejection_ledger"))
        collect(manager_context.get("review_rejection_history"))
        controller = manager_context.get("controller_state")
        if isinstance(controller, Mapping):
            collect(controller.get("review_rejection_history"))
            collect(controller.get("latest_full_rejection"))
    for manager_record in manager_records:
        collect(manager_record.get("review_rejection_history"))
        boundary = manager_record.get("_boundary")
        if isinstance(boundary, Mapping):
            collect(boundary.get("review_rejection_history"))
    return records


def _canonical_scope_projection(
    metadata: Mapping[str, Any], manager_context: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    scope, declared, invalid = _scope_from_sources(metadata, manager_context)
    if invalid or not declared or not isinstance(scope, Mapping):
        return None
    return scope


def _canonical_turn_role(record: Mapping[str, Any]) -> str | None:
    value = _canonical_text(record.get("step_role") or record.get("role"), limit=80)
    return value.casefold() if value is not None else None


def _canonical_turn_number(record: Mapping[str, Any]) -> int | None:
    return _canonical_int(record.get("turn_number"), positive=True)


def _canonical_is_runtime_retry(record: Mapping[str, Any]) -> bool:
    if record.get("was_retry") is True or record.get("runtime_retry") is True:
        return True
    if _canonical_int(record.get("retry_attempt"), positive=True) is not None:
        return True
    if record.get("recovery_action") is not None or record.get("recovery_source") is not None:
        return True
    if _canonical_status(record.get("outcome")) in {
        "retry",
        "retrying",
        "harness-retry",
        "harness_retry",
    }:
        return True
    recovery = record.get("recovery")
    return isinstance(recovery, Mapping) or record.get("recovery_executed") is True


def _canonical_is_harness_relaunch(record: Mapping[str, Any]) -> bool:
    """Return true only for a retry artifact that never became a logical turn."""
    status = _canonical_status(record.get("status"))
    return status in {
        "retry-scheduled",
        "harness-retry",
        "relaunch",
    } or (
        _canonical_is_runtime_retry(record)
        and _canonical_status(record.get("outcome")) in {"retry", "retrying"}
    )


def _canonical_retry_pair_matches(
    scheduling: Mapping[str, Any], completion: Mapping[str, Any]
) -> bool:
    """Return whether adjacent records prove one scheduled retry completed."""
    scheduling_turn = _canonical_int(scheduling.get("_turn_number"), positive=True)
    completion_turn = _canonical_int(completion.get("_turn_number"), positive=True)
    scheduling_attempt = _canonical_int(scheduling.get("retry_attempt"), positive=True)
    completion_attempt = _canonical_int(completion.get("retry_attempt"), positive=True)
    scheduling_role = _canonical_turn_role(scheduling)
    completion_role = _canonical_turn_role(completion)
    scheduling_step = _canonical_text(scheduling.get("step_name"), limit=160)
    completion_step = _canonical_text(completion.get("step_name"), limit=160)
    scheduling_scope = _canonical_text(scheduling.get("_scope_id"), limit=300)
    completion_scope = _canonical_text(completion.get("_scope_id"), limit=300)
    return (
        scheduling.get("retry_next_turn") is True
        and completion.get("was_retry") is True
        and scheduling_turn is not None
        and completion_turn == scheduling_turn + 1
        and scheduling_attempt is not None
        and completion_attempt == scheduling_attempt
        and scheduling.get("_source_run_id") == completion.get("_source_run_id")
        and scheduling_role is not None
        and scheduling_role == completion_role
        and scheduling_step is not None
        and scheduling_step == completion_step
        and not (
            scheduling_scope is not None
            and completion_scope is not None
            and scheduling_scope != completion_scope
        )
    )


def _canonical_normalize_runtime_retries(
    retries: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Collapse only proven retry scheduling/completion pairs into one retry."""
    enriched: dict[int, Mapping[str, Any]] = {}
    consumed_completions: set[int] = set()
    for scheduling_index, scheduling in enumerate(retries):
        if scheduling.get("retry_next_turn") is not True:
            continue
        for completion_index, completion in enumerate(retries):
            if completion_index == scheduling_index:
                continue
            if not _canonical_retry_pair_matches(scheduling, completion):
                continue
            merged = dict(scheduling)
            merged["was_retry"] = True
            merged["_retry_completion_turn_number"] = completion.get("_turn_number")
            merged["_retry_completion_status"] = completion.get("status")
            merged["_retry_completion_finished_at"] = completion.get("finished_at")
            enriched[scheduling_index] = merged
            if completion.get("retry_next_turn") is not True:
                consumed_completions.add(completion_index)
            break
    return [
        enriched.get(index, record)
        for index, record in enumerate(retries)
        if index not in consumed_completions
    ]


def _canonical_explicit_repair(record: Mapping[str, Any]) -> bool:
    return any(
        record.get(key) is True
        for key in ("repair", "is_repair", "repair_pass", "repair_attempt")
    )


def _canonical_scope_matches(
    record_scope: str | None,
    record_index: int | None,
    record_name: str | None,
    scope: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(scope, Mapping):
        return False
    scope_id = _canonical_scope_id(scope)
    expected_index = _canonical_checkpoint_index(scope)
    expected_name = _canonical_checkpoint_name(scope)
    if record_scope is not None and scope_id is not None:
        return record_scope == scope_id
    if record_index is not None and expected_index is not None:
        if record_index != expected_index:
            return False
        if record_name is not None and expected_name is not None:
            return record_name == expected_name
        return True
    return False


def _canonical_record_identity(
    record: Mapping[str, Any],
    *,
    kind: str,
    source_run_id: str,
    scope_id: str | None,
    turn_number: int | None,
) -> str:
    invocation_id = _canonical_text(
        record.get("invocation_id")
        or record.get("stable_invocation_id")
        or record.get("attempt_id"),
        limit=240,
    )
    if turn_number is not None:
        # Turn artifacts intentionally omit the enclosing scope and executor
        # fields.  A source run's finalized turn is the durable invocation
        # boundary; scope and executor metadata only enrich that identity.
        token = f"{source_run_id}|{kind}|turn:{turn_number}"
    elif invocation_id is not None:
        token = f"{source_run_id}|{kind}|invocation:{invocation_id}"
    else:
        token = "|".join(
            str(item or "")
            for item in (
                source_run_id,
                kind,
                scope_id,
                turn_number,
                record.get("selector") or record.get("resolved_selector"),
                record.get("step_name"),
            )
        )
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_approval_record_identity(
    record: Mapping[str, Any],
    *,
    source_run_id: str,
    scope_id: str | None,
    turn_number: int | None,
) -> str:
    """Deduplicate approvals without dropping checkpoint decision identity."""
    identity = _canonical_record_identity(
        record,
        kind="checkpoint_approval",
        source_run_id=source_run_id,
        scope_id=scope_id,
        turn_number=turn_number,
    )
    checkpoint_index = _canonical_checkpoint_index(record)
    decision_number = _canonical_int(record.get("decision_number"), positive=True)
    if checkpoint_index is None and decision_number is None:
        return identity
    return hashlib.sha256(
        f"{identity}|checkpoint:{checkpoint_index}|decision:{decision_number}".encode(
            "utf-8"
        )
    ).hexdigest()


def _canonical_executor(
    record: Mapping[str, Any],
    *,
    role: str | None,
    source_run_id: str,
    turn_number: int | None,
) -> RunProgressExecutor:
    model_display = _canonical_text(
        record.get("model_display") or record.get("resolved_model_display"),
        limit=240,
    )
    return RunProgressExecutor(
        role=role,
        team=_canonical_text(
            record.get("team") or record.get("actual_team") or record.get("baseline_team"),
            limit=160,
        ),
        selector=_canonical_text(
            record.get("selector")
            or record.get("resolved_selector")
            or record.get("actual_selector"),
            limit=240,
        ),
        harness=_canonical_text(
            record.get("harness") or record.get("resolved_harness_name"),
            limit=160,
        ),
        model=_canonical_text(
            record.get("model") or record.get("resolved_model"),
            limit=240,
        ),
        model_display=model_display,
        effort=_canonical_text(
            record.get("effort") or record.get("resolved_effort"),
            limit=120,
        ),
        source_run_id=source_run_id,
        invocation_id=_canonical_text(
            record.get("invocation_id")
            or record.get("stable_invocation_id")
            or record.get("attempt_id"),
            limit=240,
        ),
        turn_number=turn_number,
        started_at=_canonical_timestamp(record.get("started_at")),
        ended_at=_canonical_timestamp(record.get("finished_at") or record.get("ended_at")),
        duration_seconds=_canonical_duration(record),
    )


def _canonical_recorded_approval(record: Mapping[str, Any]) -> bool:
    for key in (
        "approved",
        "review_approved",
        "checkpoint_approved",
        "accepted_checkpoint",
    ):
        if record.get(key) is True:
            return True
    for key in (
        "approval",
        "review_outcome",
        "checkpoint_status",
        "outcome",
        "status",
    ):
        value = _canonical_status(record.get(key))
        if value in {"approved", "approval", "accepted", "pass", "passed"}:
            return True
    action = _canonical_status(record.get("action"))
    status = _canonical_status(record.get("status"))
    trigger = _canonical_status(record.get("trigger"))
    return (
        action in {"approve", "approved", "accept", "accepted"}
        and status in {"accepted", "approved", "completed", "succeeded"}
        and (trigger is None or "review" in trigger)
    )


def _canonical_manager_recorded_approval(record: Mapping[str, Any]) -> bool:
    """Require an explicit approval signal from a manager routing record."""
    for key in (
        "approved",
        "review_approved",
        "checkpoint_approved",
        "accepted_checkpoint",
    ):
        if record.get(key) is True:
            return True
    for key in ("approval", "review_outcome", "checkpoint_status"):
        value = _canonical_status(record.get(key))
        if value in {"approved", "approval", "accepted", "pass", "passed"}:
            return True
    trigger = _canonical_status(record.get("trigger"))
    if trigger is None or "review" not in trigger:
        return False
    status = _canonical_status(record.get("status"))
    if status in {
        "approved",
        "approval",
        "pass",
        "passed",
    }:
        return True
    return (
        _canonical_status(record.get("action")) in {
            "approve",
            "approved",
            "accept",
            "accepted",
        }
        and status in {"accepted", "approved", "completed", "succeeded"}
    )


def _canonical_recorded_rejection(record: Mapping[str, Any]) -> bool:
    if record.get("rejected") is True or record.get("review_rejected") is True:
        return True
    for key in ("outcome", "review_outcome", "status", "action"):
        value = _canonical_status(record.get(key))
        if value in {
            "rejected",
            "reject",
            "failed",
            "review_rejected",
            "repair",
            "repair_required",
        }:
            return True
    return False


def _canonical_status_override(
    record: Mapping[str, Any],
) -> tuple[int | None, str | None]:
    index = _canonical_checkpoint_index(record)
    status = _canonical_status(record.get("status"))
    if status in _PROGRESS_CHECKPOINT_STATUSES:
        return index, status
    return index, None


def _canonical_explicit_history_complete(
    metadata: Mapping[str, Any], manager_context: Mapping[str, Any] | None
) -> bool:
    for key in (
        "progress_history_complete",
        "history_complete",
        "review_history_complete",
        "approval_evidence_complete",
    ):
        if metadata.get(key) is True:
            return True
    if isinstance(manager_context, Mapping):
        for key in ("history_complete", "review_history_complete"):
            if manager_context.get(key) is True:
                return True
        disclosure = manager_context.get("history_disclosure")
        if isinstance(disclosure, Mapping):
            if disclosure.get("available") is True and disclosure.get("omitted") in {
                0,
                None,
            }:
                return True
    return False


def _canonical_repartition_records(
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    *,
    manager_records: tuple[Mapping[str, Any], ...] = (),
) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []

    def collect(value: object) -> None:
        if isinstance(value, (list, tuple)):
            records.extend(dict(item) for item in value if isinstance(item, Mapping))

    collect(metadata.get("repartition_history"))
    if isinstance(manager_context, Mapping):
        collect(manager_context.get("repartition_history"))
        controller = manager_context.get("controller_state")
        if isinstance(controller, Mapping):
            collect(controller.get("checkpoint_repartitions"))
    for manager_record in manager_records:
        collect(manager_record.get("repartition_history"))
        boundary = manager_record.get("_boundary")
        if isinstance(boundary, Mapping):
            collect(boundary.get("repartition_history"))
    return records


def _canonical_verified_repartition_record(record: Mapping[str, Any]) -> bool:
    """Recognize the complete durable record written after repartition applies."""
    required_text = (
        "scope_id",
        "generation_id",
        "envelope_sha256",
        "envelope_artifact_sha256",
        "source_plan_sha256",
        "proposal_sha256",
        "candidate_plan_sha256",
        "current_disposition",
        "resolved_target_step",
        "resolved_target_role",
        "current_partition_id",
        "envelope_artifact_path",
        "proposal_artifact_path",
        "candidate_artifact_path",
        "mechanical_validation_artifact_path",
        "semantic_verdict_artifact_path",
    )
    if _canonical_int(record.get("schema_version"), positive=True) != 1:
        return False
    if _canonical_int(record.get("decision_number")) is None:
        return False
    if any(_canonical_text(record.get(key), limit=_MAX_PATH_LENGTH) is None for key in required_text):
        return False
    partition_ids = record.get("partition_ids")
    summaries = record.get("child_summaries")
    return (
        isinstance(partition_ids, (list, tuple))
        and bool(partition_ids)
        and all(_canonical_text(value, limit=500) is not None for value in partition_ids)
        and isinstance(summaries, (list, tuple))
        and len(summaries) == len(partition_ids)
        and all(_canonical_text(value, limit=500) is not None for value in summaries)
    )


def _canonical_change_sources(
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    *,
    manager_records: tuple[Mapping[str, Any], ...] = (),
    current_run_id: str,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    applied: list[Mapping[str, Any]] = []
    pending: list[Mapping[str, Any]] = []
    durable: dict[tuple[str, int], Mapping[str, Any]] = {}
    consumed: set[tuple[str, int]] = set()

    def collect(value: object, destination: list[Mapping[str, Any]]) -> None:
        if isinstance(value, (list, tuple)):
            destination.extend(dict(item) for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            destination.append(dict(value))

    def durable_override(value: object) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        item = dict(value)
        source_team = _canonical_text(item.get("source_team"), limit=160)
        target_team = _canonical_text(item.get("target_team"), limit=160)
        item.update(
            {
                "kind": (
                    "configuration_change"
                    if source_team is not None and source_team == target_team
                    else "team_upgrade"
                    if target_team is not None
                    else "configuration_change"
                ),
                "status": "pending",
                "old_team": source_team,
                "new_team": target_team,
                "new_selector": item.get("selector"),
                "role": item.get("role"),
                "_source_run_id": item.get("source_run_id") or current_run_id,
            }
        )
        return item

    def durable_boundary(value: object) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        item = dict(value)
        action = _canonical_status(item.get("action"))
        explicit_change = action in {
            "upgrade_next_implementation",
            "upgrade",
            "team_upgrade",
            "change_team",
            "team_override",
            "configuration_change",
        } or item.get("implementation_upgrade") is True
        if not explicit_change:
            if item.get("consumed") is not True:
                return None
            return {
                "_suppress_change": True,
                "decision_number": item.get("decision_number"),
                "_source_run_id": item.get("source_run_id") or current_run_id,
            }
        item.update(
            {
                "kind": "team_upgrade" if "upgrade" in action else "configuration_change",
                "status": "applied" if item.get("applied") is True else "pending",
                "old_team": item.get("source_team") or item.get("baseline_team"),
                "new_team": item.get("target_team"),
                "new_selector": item.get("target_selector"),
                "role": item.get("target_role"),
                "_source_run_id": item.get("source_run_id") or current_run_id,
            }
        )
        return item

    def durable_identity(item: Mapping[str, Any]) -> tuple[str, int] | None:
        decision = _canonical_int(item.get("decision_number"), positive=True)
        source = _canonical_text(item.get("_source_run_id"), limit=128)
        if decision is None or source is None:
            return None
        return source, decision

    def merge_durable(item: Mapping[str, Any]) -> None:
        identity = durable_identity(item)
        if identity is None:
            return
        if item.get("_suppress_change") is True or item.get("consumed") is True:
            consumed.add(identity)
        if item.get("_suppress_change") is True:
            return
        previous = durable.get(identity)
        if previous is None:
            durable[identity] = dict(item)
            return
        merged = dict(previous)
        for key, value in item.items():
            if value is not None:
                merged[key] = value
        if previous.get("applied") is True or item.get("applied") is True:
            merged["applied"] = True
            merged["status"] = "applied"
        if previous.get("kind") == "team_upgrade" or item.get("kind") == "team_upgrade":
            merged["kind"] = "team_upgrade"
        durable[identity] = merged

    def collect_durable(container: Mapping[str, Any]) -> None:
        override = durable_override(container.get("pending_step_team_override"))
        if override is not None:
            merge_durable(override)
        boundary = durable_boundary(container.get("pending_boundary_decision"))
        if boundary is not None:
            merge_durable(boundary)

    def collect_durable_context(container: Mapping[str, Any]) -> None:
        collect_durable(container)
        for key in ("run_metadata", "validated_controller_context"):
            nested = container.get(key)
            if isinstance(nested, Mapping):
                collect_durable_context(nested)
        controller = container.get("controller_state")
        if isinstance(controller, Mapping):
            collect_durable_context(controller)

    collect(metadata.get("applied_upgrades"), applied)
    collect(metadata.get("applied_changes"), applied)
    collect(metadata.get("pending_changes"), pending)
    collect(metadata.get("pending_upgrades"), pending)
    collect_durable_context(metadata)
    if isinstance(manager_context, Mapping):
        collect(manager_context.get("applied_upgrades"), applied)
        collect(manager_context.get("applied_changes"), applied)
        collect(manager_context.get("pending_changes"), pending)
        collect(manager_context.get("pending_upgrades"), pending)
        collect_durable_context(manager_context)
        controller = manager_context.get("controller_state")
        if isinstance(controller, Mapping):
            collect(controller.get("applied_upgrades"), applied)
            collect(controller.get("applied_changes"), applied)
            collect(controller.get("pending_changes"), pending)
            collect(controller.get("pending_upgrades"), pending)
    for manager_record in manager_records:
        boundary = manager_record.get("_boundary")
        if isinstance(boundary, Mapping):
            collect_durable_context(boundary)
    for identity, item in durable.items():
        if identity in consumed and item.get("applied") is not True:
            continue
        (applied if item.get("applied") is True else pending).append(item)
    return applied, pending


def _canonical_hotplug_change(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Adapt durable hotplug transaction fields to the public change contract."""
    normalized = dict(record)
    normalized["kind"] = "hotplug"
    if "source_run_id" not in normalized and "_source_run_id" not in normalized:
        normalized["source_run_id"] = record.get("run_id")
    if "source_model_display" in record:
        normalized["old_model"] = record.get("source_model_display")
    if "target_model_display" in record:
        normalized["new_model"] = record.get("target_model_display")
    return normalized


def _canonical_hotplug_changes(
    metadata: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    applied: list[Mapping[str, Any]] = []
    pending: list[Mapping[str, Any]] = []
    value = metadata.get("hotplug_history")
    if not isinstance(value, (list, tuple)):
        value = ()
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identity = _canonical_text(item.get("transaction_id"), limit=300)
        if identity is None:
            identity = json.dumps(_canonical_fingerprint(item), sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        stage = _canonical_status(item.get("stage"))
        if stage == "applied":
            applied.append(_canonical_hotplug_change(item))
        elif stage not in {"failed", "closed"}:
            pending.append(_canonical_hotplug_change(item))
    for key in ("current_hotplug_transaction", "pending_hotplug_transaction"):
        item = metadata.get(key)
        if isinstance(item, Mapping):
            identity = _canonical_text(item.get("transaction_id"), limit=300)
            if identity not in seen:
                pending.append(_canonical_hotplug_change(item))
                seen.add(identity)
    return applied, pending


def _canonical_checkpoint_id(
    plan: _CanonicalPlan,
    ordinal: int | None,
    *,
    generation_id: str | None = None,
) -> str | None:
    """Return the stable original-parent row ID, never a transient generation ID."""
    if ordinal is None or ordinal < 1:
        return None
    identity = plan.identity
    if identity is None:
        return None
    del generation_id
    return f"{identity}::checkpoint-{ordinal}"


def _canonical_rejection_info(
    records: list[Mapping[str, Any]], *, current_run_id: str
) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        scope_id = _canonical_scope_id(record)
        review_turn = _canonical_int(
            record.get("review_turn_number") or record.get("turn_number"),
            positive=True,
        )
        source_run_id = _canonical_source_run_id(
            record.get("source_run_id"), current_run_id=current_run_id
        )
        identity = _canonical_record_identity(
            record,
            kind="review_rejection",
            source_run_id=source_run_id,
            scope_id=scope_id,
            turn_number=review_turn,
        )
        if identity in seen:
            continue
        seen.add(identity)
        item = dict(record)
        item.update(
            {
                "_identity": identity,
                "_scope_id": scope_id,
                "_checkpoint_index": _canonical_checkpoint_index(record),
                "_checkpoint_name": _canonical_checkpoint_name(record),
                "_review_turn": review_turn,
                "_source_run_id": source_run_id,
            }
        )
        result.append(item)
    result.sort(key=lambda item: (_canonical_int(item.get("_review_turn")) or 0, str(item.get("_identity"))))
    return tuple(result)


def _canonical_collect_invocations(
    *,
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    turns: tuple[_CanonicalTurn, ...],
    rejections: tuple[Mapping[str, Any], ...],
    manager_records: tuple[Mapping[str, Any], ...],
    current_run_id: str,
    budget: _ProgressBudget,
    plan: _CanonicalPlan,
    active_scope: Mapping[str, Any] | None,
    activity: str,
) -> tuple[tuple[_Invocation, ...], tuple[_Invocation, ...], tuple[Mapping[str, Any], ...]]:
    workers: list[_Invocation] = []
    reviewers: list[_Invocation] = []
    retries: list[Mapping[str, Any]] = []
    seen_workers: set[str] = set()
    seen_reviews: set[str] = set()
    seen_retries: set[str] = set()
    explicit_attempts = _canonical_attempt_sources(metadata, manager_context)
    for manager_record in manager_records:
        explicit_attempts.extend(
            _canonical_attempt_sources(
                {"implementation_attempts": manager_record.get("implementation_attempts")},
                None,
            )
        )

    def is_repair(
        record: Mapping[str, Any],
        *,
        scope_id: str | None,
        source_run_id: str,
        turn_number: int | None,
    ) -> bool:
        if _canonical_explicit_repair(record):
            return True
        if scope_id is None or turn_number is None:
            return False
        return any(
            item.get("_scope_id") == scope_id
            and item.get("_source_run_id") == source_run_id
            and (_canonical_int(item.get("_review_turn")) or 0) < turn_number
            for item in rejections
        )

    def add_worker(
        record: Mapping[str, Any],
        *,
        fallback_scope: str | None = None,
        from_turn: bool = False,
    ) -> None:
        role = _canonical_turn_role(record) or "worker"
        if role != "worker":
            return
        turn_number = _canonical_turn_number(record)
        scope_id = _canonical_scope_id(record, fallback_scope)
        source_run_id = _canonical_source_run_id(
            record.get("source_run_id") or record.get("run_id"),
            current_run_id=current_run_id,
        )
        historical_association = _canonical_historical_turn_association(
            record, plan=plan, current_run_id=current_run_id
        )
        if historical_association is not None:
            historical_index, historical_name = historical_association
            supplied_index = _canonical_checkpoint_index(record)
            scope_index = _canonical_scope_checkpoint_index(scope_id)
            if (
                (supplied_index is not None and supplied_index != historical_index)
                or (scope_index is not None and scope_index != historical_index)
            ):
                historical_association = None
            else:
                record = {
                    **dict(record),
                    "checkpoint_index": historical_index,
                    "checkpoint_name": historical_name,
                }
        runtime_retry = _canonical_is_runtime_retry(record)
        if runtime_retry:
            retry_record = {
                **dict(record),
                "_source_run_id": source_run_id,
                "_scope_id": scope_id,
                "_turn_number": turn_number,
            }
            retry_identity = _canonical_record_identity(
                retry_record,
                kind="runtime_retry",
                source_run_id=source_run_id,
                scope_id=scope_id,
                turn_number=turn_number,
            )
            if retry_identity not in seen_retries:
                seen_retries.add(retry_identity)
                retries.append(retry_record)
        if _canonical_is_harness_relaunch(record):
            return
        inherited = source_run_id != current_run_id or record.get("inherited") is True
        identity = _canonical_record_identity(
            record,
            kind="worker",
            source_run_id=source_run_id,
            scope_id=scope_id,
            turn_number=turn_number,
        )
        if identity in seen_workers:
            for position, existing in enumerate(workers):
                existing_identity = _canonical_record_identity(
                    existing.record,
                    kind="worker",
                    source_run_id=existing.source_run_id,
                    scope_id=existing.scope_id,
                    turn_number=existing.turn_number,
                )
                if existing_identity != identity:
                    continue
                merged = dict(existing.record)
                for key, value in record.items():
                    if value is not None:
                        merged[key] = value
                merged_scope = existing.scope_id or scope_id
                merged_index = _canonical_checkpoint_index(merged)
                merged_name = _canonical_checkpoint_name(merged)
                if historical_association is not None:
                    historical_index, historical_name = historical_association
                    scope_index = _canonical_scope_checkpoint_index(merged_scope)
                    if (
                        (merged_index is not None and merged_index != historical_index)
                        or (scope_index is not None and scope_index != historical_index)
                    ):
                        merged_index = None
                    else:
                        merged_index = historical_index
                        merged_name = historical_name
                if merged_scope is not None:
                    merged.setdefault("scope_id", merged_scope)
                if merged_index is None:
                    merged_index = existing.checkpoint_index
                if merged_name is None:
                    merged_name = existing.checkpoint_name
                workers[position] = _Invocation(
                    record=merged,
                    kind=existing.kind,
                    source_run_id=existing.source_run_id,
                    scope_id=merged_scope,
                    checkpoint_index=merged_index,
                    checkpoint_name=merged_name,
                    turn_number=existing.turn_number,
                    inherited=existing.inherited or inherited,
                    repair=is_repair(
                        merged,
                        scope_id=merged_scope,
                        source_run_id=existing.source_run_id,
                        turn_number=existing.turn_number,
                    ),
                    runtime_retry=runtime_retry or existing.runtime_retry,
                )
                break
            return
        seen_workers.add(identity)
        workers.append(
            _Invocation(
                record=dict(record),
                kind="worker",
                source_run_id=source_run_id,
                scope_id=scope_id,
                checkpoint_index=_canonical_checkpoint_index(record),
                checkpoint_name=_canonical_checkpoint_name(record),
                turn_number=turn_number,
                inherited=inherited,
                repair=is_repair(
                    record,
                    scope_id=scope_id,
                    source_run_id=source_run_id,
                    turn_number=turn_number,
                ),
                runtime_retry=runtime_retry,
            )
        )

    for group_scope, raw in explicit_attempts:
        item = dict(raw)
        if group_scope is not None and "scope_id" not in item:
            item["scope_id"] = group_scope
        add_worker(item, fallback_scope=group_scope)

    # Turn artifacts are authoritative for started invocations and reviewer
    # counts.  They also fill old runs that predate durable attempt records.
    for turn in turns:
        record = turn.record
        role = _canonical_turn_role(record)
        if role == "worker":
            add_worker(record, from_turn=True)
        elif role == "reviewer":
            turn_number = _canonical_turn_number(record)
            scope_id = _canonical_scope_id(record)
            checkpoint_index = _canonical_checkpoint_index(record)
            checkpoint_name = _canonical_checkpoint_name(record)
            source_run_id = _canonical_source_run_id(
                record.get("source_run_id") or record.get("run_id"),
                current_run_id=current_run_id,
            )
            if source_run_id == current_run_id:
                reviewer_scope = _canonical_reviewer_scope_association(
                    record,
                    turn_number=turn_number,
                    workers=workers,
                    active_scope=active_scope,
                    activity=activity,
                    manager_records=manager_records,
                    current_run_id=current_run_id,
                )
                if reviewer_scope is not None:
                    scope_id, checkpoint_index, checkpoint_name = reviewer_scope
                    record = {**dict(record), "scope_id": scope_id}
                else:
                    association_hint: ProgressEventAssociation = "unassigned"
                    if _canonical_whole_plan_review(
                        record, plan=plan, current_run_id=current_run_id
                    ):
                        association_hint = "whole_plan"
                    elif _canonical_outside_returned_page(
                        record,
                        plan=plan,
                        budget=budget,
                        scope_indices={},
                        ambiguous_scopes=set(),
                    ):
                        association_hint = "outside_returned"
                    original_scope_id = _canonical_scope_id(record)
                    original_checkpoint_index = _canonical_checkpoint_index(record)
                    original_checkpoint_name = _canonical_checkpoint_name(record)
                    scope_id = None
                    checkpoint_index = None
                    checkpoint_name = None
                    stripped_record = {
                        key: value
                        for key, value in record.items()
                        if key not in {
                            "scope_id",
                            "checkpoint_identity",
                            "scope",
                            "checkpoint_index",
                            "checkpoint_ordinal",
                            "ordinal",
                            "checkpoint_name",
                            "checkpoint_title",
                            "title",
                        }
                    }
                    if association_hint in {"whole_plan", "outside_returned"}:
                        record = {
                            **stripped_record,
                            "_progress_association": association_hint,
                        }
                        if association_hint == "outside_returned":
                            if original_scope_id is not None:
                                record["_scope_id"] = original_scope_id
                            if original_checkpoint_index is not None:
                                record["_checkpoint_index"] = original_checkpoint_index
                            if original_checkpoint_name is not None:
                                record["_checkpoint_name"] = original_checkpoint_name
                    else:
                        record = stripped_record
            identity = _canonical_record_identity(
                record,
                kind="reviewer",
                source_run_id=source_run_id,
                scope_id=scope_id,
                turn_number=turn_number,
            )
            if identity in seen_reviews:
                continue
            seen_reviews.add(identity)
            reviewers.append(
                _Invocation(
                    record=dict(record),
                    kind="reviewer",
                    source_run_id=source_run_id,
                    scope_id=scope_id,
                    checkpoint_index=checkpoint_index,
                    checkpoint_name=checkpoint_name,
                    turn_number=turn_number,
                    inherited=source_run_id != current_run_id,
                    repair=False,
                    runtime_retry=_canonical_is_runtime_retry(record),
                )
            )
            if _canonical_is_runtime_retry(record):
                retry_record = {
                    **dict(record),
                    "_source_run_id": source_run_id,
                    "_scope_id": scope_id,
                    "_turn_number": turn_number,
                }
                retry_identity = _canonical_record_identity(
                    retry_record,
                    kind="runtime_retry",
                    source_run_id=source_run_id,
                    scope_id=scope_id,
                    turn_number=turn_number,
                )
                if retry_identity not in seen_retries:
                    seen_retries.add(retry_identity)
                    retries.append(retry_record)

    # A rejection record proves that a reviewer invocation occurred even when
    # the corresponding result artifact was pruned.  It does not prove a
    # repair started, and therefore never creates a worker attempt by itself.
    for rejection in rejections:
        normalized_rejection = dict(rejection)
        turn_number = _canonical_int(rejection.get("_review_turn"), positive=True)
        if turn_number is not None:
            normalized_rejection.setdefault("turn_number", turn_number)
        normalized_rejection.setdefault("step_role", "reviewer")
        normalized_rejection.setdefault(
            "step_name", rejection.get("review_step_name") or "review"
        )
        normalized_rejection.setdefault("selector", rejection.get("reviewer_selector"))
        normalized_rejection["outcome"] = "rejected"
        normalized_rejection["review_rejected"] = True
        source_run_id = _canonical_source_run_id(
            rejection.get("_source_run_id"), current_run_id=current_run_id
        )
        identity = _canonical_record_identity(
            normalized_rejection,
            kind="reviewer",
            source_run_id=source_run_id,
            scope_id=_canonical_scope_id(rejection),
            turn_number=turn_number,
        )
        if identity in seen_reviews:
            for position, existing in enumerate(reviewers):
                existing_identity = _canonical_record_identity(
                    existing.record,
                    kind="reviewer",
                    source_run_id=existing.source_run_id,
                    scope_id=existing.scope_id,
                    turn_number=existing.turn_number,
                )
                if existing_identity != identity:
                    continue
                merged = dict(existing.record)
                for key, value in normalized_rejection.items():
                    if value is not None and merged.get(key) is None:
                        merged[key] = value
                merged["outcome"] = "rejected"
                merged["review_rejected"] = True
                merged_scope = existing.scope_id or _canonical_scope_id(rejection)
                if merged_scope is not None and merged.get("scope_id") is None:
                    merged["scope_id"] = merged_scope
                reviewers[position] = _Invocation(
                    record=merged,
                    kind=existing.kind,
                    source_run_id=existing.source_run_id,
                    scope_id=merged_scope,
                    checkpoint_index=(
                        _canonical_checkpoint_index(merged)
                        or existing.checkpoint_index
                    ),
                    checkpoint_name=(
                        _canonical_checkpoint_name(merged)
                        or existing.checkpoint_name
                    ),
                    turn_number=existing.turn_number,
                    inherited=existing.inherited or source_run_id != current_run_id,
                    repair=False,
                    runtime_retry=existing.runtime_retry,
                )
                break
            continue
        seen_reviews.add(identity)
        reviewers.append(
            _Invocation(
                record=normalized_rejection,
                kind="reviewer",
                source_run_id=source_run_id,
                scope_id=_canonical_scope_id(rejection),
                checkpoint_index=_canonical_checkpoint_index(rejection),
                checkpoint_name=_canonical_checkpoint_name(rejection),
                turn_number=turn_number,
                inherited=source_run_id != current_run_id,
            )
        )

    workers.sort(key=lambda item: (item.turn_number or 0, item.source_run_id))
    reviewers.sort(key=lambda item: (item.turn_number or 0, item.source_run_id))
    retries = _canonical_normalize_runtime_retries(retries)
    if len(workers) + len(reviewers) > _PROGRESS_MAX_RECORDS:
        budget.omitted_records += len(workers) + len(reviewers) - _PROGRESS_MAX_RECORDS
        budget.notice("Earlier invocation history unavailable in this bounded view")
    return tuple(workers), tuple(reviewers), tuple(retries)


def _canonical_successful_review_approval(
    invocation: _Invocation, *, current_run_id: str
) -> Mapping[str, Any] | None:
    """Normalize a runlog-proven no-repair review into approval evidence."""
    if (
        invocation.kind != "reviewer"
        or invocation.inherited
        or invocation.source_run_id != current_run_id
        or invocation.scope_id is None
        or invocation.checkpoint_index is None
        or invocation.turn_number is None
    ):
        return None
    record = invocation.record
    if (
        _canonical_timestamp(record.get("finished_at")) is None
        or record.get("returncode") != 0
        or "review_rejection" not in record
        or record.get("review_rejection") is not None
        or record.get("error") is not None
        or _canonical_recorded_rejection(record)
    ):
        return None
    status = _canonical_status(record.get("status"))
    if status in {"failed", "interrupted", "stopped", "transition-failed"}:
        return None
    conditions = record.get("conditions")
    if not isinstance(conditions, Mapping) or (
        conditions.get("NEW_PLAN_EXISTS") is not False
        or conditions.get("MAX_TURNS_REACHED") is not False
    ):
        return None
    transition = _canonical_text(record.get("chosen_transition"), limit=120)
    if transition is None:
        return None
    if transition.casefold() == "end":
        if conditions.get("DONE") is not True:
            return None
    elif _canonical_text(record.get("chosen_transition_condition"), limit=240) is None:
        return None
    return {
        **dict(record),
        "source_run_id": invocation.source_run_id,
        "scope_id": invocation.scope_id,
        "checkpoint_index": invocation.checkpoint_index,
        "checkpoint_name": invocation.checkpoint_name,
        "review_approved": True,
        "outcome": "approved",
    }


def _canonical_approval_records(
    *,
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    turns: tuple[_CanonicalTurn, ...],
    reviewers: tuple[_Invocation, ...],
    manager_records: tuple[Mapping[str, Any], ...],
    current_run_id: str,
) -> tuple[set[int], tuple[Mapping[str, Any], ...]]:
    approved_ordinals: set[int] = set()
    records: list[Mapping[str, Any]] = []

    def collect(value: object, *, manager_source: bool = False) -> None:
        values: list[object]
        if isinstance(value, Mapping):
            values = [value]
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            return
        for item in values:
            if isinstance(item, int) and not isinstance(item, bool):
                if item > 0:
                    approved_ordinals.add(item)
                continue
            if not isinstance(item, Mapping) or not (
                _canonical_manager_recorded_approval(item)
                if manager_source
                else _canonical_recorded_approval(item)
            ):
                continue
            records.append(dict(item))

    for key in (
        "approved_checkpoints",
        "approved_checkpoint_history",
        "review_approvals",
        "accepted_checkpoints",
    ):
        collect(metadata.get(key))
    if isinstance(metadata.get("approved_checkpoint_indices"), (list, tuple)):
        for value in metadata["approved_checkpoint_indices"]:
            index = _canonical_int(value, positive=True)
            if index is not None:
                approved_ordinals.add(index)
    for turn in turns:
        if _canonical_turn_role(turn.record) == "reviewer":
            collect(turn.record)
    for reviewer in reviewers:
        successful = _canonical_successful_review_approval(
            reviewer, current_run_id=current_run_id
        )
        if successful is not None:
            collect(successful)
    for record in manager_records:
        collect(record, manager_source=True)
    if isinstance(manager_context, Mapping):
        collect(manager_context.get("review_approvals"))
        collect(manager_context.get("approved_checkpoints"))
        controller = manager_context.get("controller_state")
        if isinstance(controller, Mapping):
            collect(controller.get("review_approvals"))
            collect(controller.get("approved_checkpoints"))

    # Preserve explicit approval source identity in the ordered event stream.
    deduped: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        identity = _canonical_approval_record_identity(
            record,
            source_run_id=_canonical_source_run_id(
                record.get("source_run_id"), current_run_id=current_run_id
            ),
            scope_id=_canonical_scope_id(record),
            turn_number=_canonical_turn_number(record),
        )
        if identity not in seen:
            seen.add(identity)
            deduped.append(record)
    return approved_ordinals, tuple(deduped)


def _canonical_record_text(
    record: Mapping[str, Any], keys: tuple[str, ...], *, limit: int = 500
) -> str | None:
    for key in keys:
        value = _canonical_text(record.get(key), limit=limit)
        if value is not None:
            return value
    return None


def _canonical_record_outcome(record: Mapping[str, Any]) -> str | None:
    return _canonical_record_text(
        record,
        (
            "outcome",
            "review_outcome",
            "result",
            "status",
            "action",
        ),
        limit=120,
    )


def _canonical_record_reason(record: Mapping[str, Any]) -> str | None:
    return _canonical_record_text(
        record,
        (
            "reason",
            "review_summary",
            "summary",
            "failure_reason",
            "rejection_reason",
            "error",
        ),
        limit=500,
    )


def _canonical_relative_artifact(value: object) -> str | None:
    text = _canonical_text(value, limit=512)
    if text is None:
        return None
    try:
        path = Path(text)
    except (OSError, RuntimeError, ValueError):
        return None
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _canonical_source_reference(
    record: Mapping[str, Any],
    *,
    source_run_id: str,
    default_artifact: str | None = None,
    turn_number: int | None = None,
    decision_number: int | None = None,
) -> Mapping[str, Any]:
    reference: dict[str, Any] = {"run_id": source_run_id}
    supplied = record.get("source_reference")
    if isinstance(supplied, Mapping):
        artifact = _canonical_relative_artifact(
            supplied.get("artifact")
            or supplied.get("path")
            or supplied.get("relative_path")
        )
        if artifact is not None:
            reference["artifact"] = artifact
        supplied_turn = _canonical_int(supplied.get("turn_number"), positive=True)
        supplied_decision = _canonical_int(
            supplied.get("decision_number"), positive=True
        )
        if supplied_turn is not None:
            reference["turn_number"] = supplied_turn
        if supplied_decision is not None:
            reference["decision_number"] = supplied_decision
    artifact = _canonical_relative_artifact(
        record.get("_artifact")
        or record.get("artifact_path")
        or record.get("review_stdout_artifact_path")
        or record.get("source_artifact")
        or default_artifact
    )
    if artifact is not None:
        reference["artifact"] = artifact
    if turn_number is not None:
        reference["turn_number"] = turn_number
    if decision_number is not None:
        reference["decision_number"] = decision_number
    return reference


def _canonical_scope_fields(
    record: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    scope_id = _canonical_text(
        record.get("_scope_id") or record.get("scope_id"), limit=300
    )
    generation_id = _canonical_text(
        record.get("generation_id")
        or record.get("repartition_generation_id")
        or record.get("current_partition_generation_id"),
        limit=240,
    )
    parent_scope_id = _canonical_text(
        record.get("parent_scope_id") or record.get("parent_checkpoint_id"),
        limit=300,
    )
    return scope_id, generation_id, parent_scope_id


def _canonical_scope_index_map(
    records: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    scope: Mapping[str, Any] | None,
) -> tuple[dict[str, int], set[str]]:
    values: dict[str, int] = {}
    ambiguous: set[str] = set()

    def add(record: Mapping[str, Any]) -> None:
        scope_id = _canonical_text(
            record.get("_scope_id") or record.get("scope_id"), limit=300
        )
        explicit_index = _canonical_int(
            record.get("_checkpoint_index")
            if isinstance(record.get("_checkpoint_index"), int)
            else record.get("checkpoint_index"),
            positive=True,
        )
        scope_index = (
            _canonical_scope_checkpoint_index(scope_id)
            if _canonical_verified_repartition_record(record)
            else None
        )
        if (
            explicit_index is not None
            and scope_index is not None
            and explicit_index != scope_index
        ):
            ambiguous.add(scope_id)
            return
        index = explicit_index or scope_index
        if scope_id is None or index is None:
            return
        previous = values.get(scope_id)
        if previous is not None and previous != index:
            ambiguous.add(scope_id)
        else:
            values[scope_id] = index

    if isinstance(scope, Mapping):
        add(scope)
    for record in records:
        add(record)
        boundary = record.get("_boundary")
        if isinstance(boundary, Mapping):
            active_scope = boundary.get("active_implementation_scope")
            if isinstance(active_scope, Mapping):
                add(active_scope)
    for scope_id in ambiguous:
        values.pop(scope_id, None)
    return values, ambiguous


def _canonical_checkpoint_association(
    record: Mapping[str, Any],
    *,
    plan: _CanonicalPlan,
    scope_indices: Mapping[str, int],
    ambiguous_scopes: set[str],
) -> tuple[int | None, str | None, str | None, str | None, bool]:
    """Resolve only explicit checkpoint identity; the final flag means assigned."""
    scope_id, generation_id, parent_scope_id = _canonical_scope_fields(record)
    ordinal_value = (
        record.get("_checkpoint_index")
        if isinstance(record.get("_checkpoint_index"), int)
        else _canonical_checkpoint_index(record)
    )
    ordinal = _canonical_int(ordinal_value, positive=True)
    name = _canonical_title(
        record.get("_checkpoint_name")
        if isinstance(record.get("_checkpoint_name"), str)
        else _canonical_checkpoint_name(record)
    )
    if scope_id is not None:
        if scope_id in ambiguous_scopes:
            return None, scope_id, generation_id, parent_scope_id, False
        mapped = scope_indices.get(scope_id)
        if mapped is not None:
            if ordinal is not None and ordinal != mapped:
                return None, scope_id, generation_id, parent_scope_id, False
            ordinal = mapped
    if ordinal is None and name is not None and plan.sections:
        matches = [
            index
            for index, section in enumerate(plan.sections, start=1)
            if _canonical_title(section.get("name")) == name
        ]
        if len(matches) == 1:
            ordinal = matches[0]
        else:
            return None, scope_id, generation_id, parent_scope_id, False
    if not plan.sections or ordinal is None or ordinal > len(plan.sections):
        return None, scope_id, generation_id, parent_scope_id, False
    expected_name = _canonical_title(plan.sections[ordinal - 1].get("name"))
    if name is not None and expected_name is not None and name != expected_name:
        return None, scope_id, generation_id, parent_scope_id, False
    return ordinal, scope_id, generation_id, parent_scope_id, True


def _canonical_referenced_checkpoint_index(
    record: Mapping[str, Any],
    *,
    scope_indices: Mapping[str, int],
    ambiguous_scopes: set[str],
) -> int | None:
    """Return a non-conflicting durable checkpoint reference, if present."""
    scope_id = _canonical_scope_id(record)
    if scope_id is not None and scope_id in ambiguous_scopes:
        return None
    explicit = _canonical_int(
        record.get("_checkpoint_index")
        if isinstance(record.get("_checkpoint_index"), int)
        else _canonical_checkpoint_index(record),
        positive=True,
    )
    mapped = scope_indices.get(scope_id) if scope_id is not None else None
    suffix = _canonical_scope_checkpoint_index(scope_id)
    for candidate in (mapped, suffix):
        if explicit is not None and candidate is not None and explicit != candidate:
            return None
    if mapped is not None and suffix is not None and mapped != suffix:
        return None
    return explicit or mapped or suffix


def _canonical_outside_returned_page(
    record: Mapping[str, Any],
    *,
    plan: _CanonicalPlan,
    budget: _ProgressBudget,
    scope_indices: Mapping[str, int],
    ambiguous_scopes: set[str],
) -> bool:
    """Recognize a stable checkpoint that the bounded plan page omitted."""
    if (
        plan.total_sections is None
        or len(plan.sections) >= plan.total_sections
        or budget.omitted_checkpoints <= 0
    ):
        return False
    ordinal = _canonical_referenced_checkpoint_index(
        record,
        scope_indices=scope_indices,
        ambiguous_scopes=ambiguous_scopes,
    )
    return ordinal is not None and len(plan.sections) < ordinal <= plan.total_sections


def _canonical_event_association(
    record: Mapping[str, Any],
    *,
    assigned: bool,
    plan: _CanonicalPlan,
    budget: _ProgressBudget,
    current_run_id: str,
    scope_indices: Mapping[str, int],
    ambiguous_scopes: set[str],
) -> ProgressEventAssociation:
    if assigned:
        return "checkpoint"
    hinted = record.get("_progress_association")
    if isinstance(hinted, str) and hinted in {"whole_plan", "outside_returned"}:
        return hinted  # type: ignore[return-value]
    if _canonical_whole_plan_review(
        record, plan=plan, current_run_id=current_run_id
    ):
        return "whole_plan"
    if _canonical_outside_returned_page(
        record,
        plan=plan,
        budget=budget,
        scope_indices=scope_indices,
        ambiguous_scopes=ambiguous_scopes,
    ):
        return "outside_returned"
    return "unassigned"


def _canonical_count(value: int | None, coverage: str) -> RunProgressCount:
    if coverage not in _PROGRESS_COVERAGES:
        coverage = "unavailable"
    if coverage == "unavailable":
        value = None
    return RunProgressCount(value=value, coverage=coverage)  # type: ignore[arg-type]


def _canonical_coverage(
    *,
    value: int,
    has_evidence: bool,
    history_complete: bool,
    omitted: bool,
) -> str:
    if omitted:
        return "partial"
    if history_complete:
        return "complete"
    if has_evidence:
        return "partial"
    return "unavailable"


def _canonical_status_from_record(record: Mapping[str, Any]) -> str | None:
    status = _canonical_status(record.get("status"))
    if status in _PROGRESS_CHECKPOINT_STATUSES:
        return status
    if status in {"complete", "completed", "done", "finished"}:
        return "recorded_complete"
    if status in {"failed", "failure", "rejected", "stopped"}:
        return "blocked"
    if status in {"awaiting_review", "pending_review"}:
        return "reviewing"
    return None


def _canonical_recorded_at(record: Mapping[str, Any]) -> str | None:
    for key in (
        "recorded_at",
        "finished_at",
        "ended_at",
        "applied_at",
        "created_at",
        "timestamp",
    ):
        value = _canonical_timestamp(record.get(key))
        if value is not None:
            return value
    return None


def _canonical_event_id(
    *,
    kind: str,
    record: Mapping[str, Any],
    source_run_id: str,
    scope_id: str | None,
    turn_number: int | None,
    ordinal: int | None,
) -> str:
    if kind == "checkpoint_approval":
        identity = _canonical_approval_record_identity(
            record,
            source_run_id=source_run_id,
            scope_id=scope_id,
            turn_number=turn_number,
        )
    else:
        identity = _canonical_record_identity(
            record,
            kind=kind,
            source_run_id=source_run_id,
            scope_id=scope_id,
            turn_number=turn_number,
        )
    suffix = f"|cp:{ordinal}" if ordinal is not None else ""
    return hashlib.sha256(f"{kind}|{identity}{suffix}".encode("utf-8")).hexdigest()


def _canonical_change_id(
    record: Mapping[str, Any], *, kind: str, source_run_id: str | None = None
) -> str:
    explicit = _canonical_record_text(
        record,
        ("change_id", "transaction_id", "upgrade_id", "id"),
        limit=300,
    )
    if explicit is not None:
        return explicit
    decision_number = _canonical_int(record.get("decision_number"), positive=True)
    decision_source = _canonical_text(
        record.get("_source_run_id") or record.get("source_run_id") or source_run_id,
        limit=128,
    )
    if decision_number is not None and decision_source is not None:
        return f"{kind}:decision:{decision_source}:{decision_number}"
    structural = json.dumps(
        _canonical_fingerprint({"kind": kind, "record": record}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return "change-" + hashlib.sha256(structural.encode("utf-8")).hexdigest()


def _canonical_roles(record: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    raw = record.get("roles")
    if isinstance(raw, (list, tuple)):
        values.extend(
            value
            for item in raw
            if (value := _canonical_text(item, limit=80)) is not None
        )
    for key in ("role", "target_role", "current_role"):
        value = _canonical_text(record.get(key), limit=80)
        if value is not None:
            values.append(value)
    return tuple(dict.fromkeys(values))


def _canonical_change_value(
    record: Mapping[str, Any], keys: tuple[str, ...], *, limit: int = 240
) -> str | None:
    return _canonical_record_text(record, keys, limit=limit)


def _canonical_change_status(record: Mapping[str, Any], default: str) -> str:
    status = _canonical_status(record.get("status") or record.get("stage"))
    if status in {"applied", "committed", "succeeded", "complete", "completed"}:
        return "applied"
    if status in {"failed", "rejected", "error"}:
        return "failed"
    if status in {"pending", "proposed", "accepted", "validated", "in_flight"}:
        return "pending"
    return default


def _canonical_change_kind(record: Mapping[str, Any], *, fallback: str) -> str:
    if _canonical_verified_repartition_record(record):
        return "repartition"
    explicit = _canonical_status(
        record.get("kind") or record.get("change_kind") or record.get("type")
    )
    if explicit in {
        "upgrade",
        "worker_upgrade",
        "team_upgrade",
        "implementation_upgrade",
        "configuration_change",
        "hotplug",
        "repartition",
    }:
        return explicit
    if any(
        record.get(key) is True
        for key in ("upgrade", "applied_upgrade", "implementation_upgrade")
    ):
        return "upgrade"
    return fallback


def _canonical_change(
    record: Mapping[str, Any],
    *,
    plan: _CanonicalPlan,
    scope_indices: Mapping[str, int],
    ambiguous_scopes: set[str],
    status: str,
    fallback_kind: str,
    current_run_id: str,
) -> RunProgressChange:
    ordinal, scope_id, generation_id, _parent_scope_id, assigned = (
        _canonical_checkpoint_association(
            record,
            plan=plan,
            scope_indices=scope_indices,
            ambiguous_scopes=ambiguous_scopes,
        )
    )
    checkpoint_id = _canonical_checkpoint_id(
        plan, ordinal, generation_id=generation_id
    )
    reason = _canonical_record_reason(record)
    if not assigned:
        reason = f"{reason}; Unassigned history" if reason else "Unassigned history"
    source_run_id = _canonical_source_run_id(
        record.get("source_run_id") or record.get("_source_run_id"),
        current_run_id=current_run_id,
    )
    kind = _canonical_change_kind(record, fallback=fallback_kind)
    return RunProgressChange(
        change_id=_canonical_change_id(record, kind=kind, source_run_id=source_run_id),
        status=status,  # type: ignore[arg-type]
        kind=kind,
        roles=_canonical_roles(record),
        old_team=_canonical_change_value(
            record, ("old_team", "source_team", "from_team", "baseline_team"), limit=160
        ),
        new_team=_canonical_change_value(
            record, ("new_team", "target_team", "to_team", "actual_team"), limit=160
        ),
        old_selector=_canonical_change_value(
            record,
            ("old_selector", "source_selector", "from_selector", "baseline_selector"),
        ),
        new_selector=_canonical_change_value(
            record,
            ("new_selector", "target_selector", "to_selector", "actual_selector"),
        ),
        old_model=_canonical_change_value(
            record, ("old_model", "source_model", "from_model")
        ),
        new_model=_canonical_change_value(
            record, ("new_model", "target_model", "to_model", "model")
        ),
        old_effort=_canonical_change_value(
            record, ("old_effort", "source_effort", "from_effort"), limit=120
        ),
        new_effort=_canonical_change_value(
            record, ("new_effort", "target_effort", "to_effort", "effort"), limit=120
        ),
        turn_number=_canonical_int(
            record.get("turn_number") or record.get("applied_turn_number"),
            positive=True,
        ),
        checkpoint_id=checkpoint_id,
        generation_id=generation_id,
        reason=reason,
        recorded_at=_canonical_recorded_at(record),
        source_reference=_canonical_source_reference(
            record,
            source_run_id=source_run_id,
            turn_number=_canonical_int(record.get("turn_number"), positive=True),
            decision_number=_canonical_int(
                record.get("decision_number"), positive=True
            ),
        ),
    )


def _canonical_changes(
    *,
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    plan: _CanonicalPlan,
    scope_indices: Mapping[str, int],
    ambiguous_scopes: set[str],
    current_run_id: str,
    manager_records: tuple[Mapping[str, Any], ...] = (),
) -> tuple[tuple[RunProgressChange, ...], tuple[RunProgressChange, ...]]:
    applied, pending = _canonical_change_sources(
        metadata,
        manager_context,
        manager_records=manager_records,
        current_run_id=current_run_id,
    )
    hotplug_applied, hotplug_pending = _canonical_hotplug_changes(metadata)
    applied.extend(hotplug_applied)
    pending.extend(hotplug_pending)
    for record in _canonical_repartition_records(
        metadata, manager_context, manager_records=manager_records
    ):
        stage = _canonical_status(record.get("stage") or record.get("status"))
        if stage in {"applied", "committed", "succeeded"} or _canonical_verified_repartition_record(record):
            applied.append(record)
        else:
            pending.append(record)

    def build(
        values: list[Mapping[str, Any]], *, default_status: str, fallback_kind: str
    ) -> tuple[RunProgressChange, ...]:
        result: list[RunProgressChange] = []
        seen: set[str] = set()
        for record in values[:_PROGRESS_MAX_RECORDS]:
            change = _canonical_change(
                record,
                plan=plan,
                scope_indices=scope_indices,
                ambiguous_scopes=ambiguous_scopes,
                status=_canonical_change_status(record, default_status),
                fallback_kind=fallback_kind,
                current_run_id=current_run_id,
            )
            if change.change_id in seen:
                continue
            seen.add(change.change_id)
            result.append(change)
        return tuple(result)

    applied_changes = build(
        applied, default_status="applied", fallback_kind="configuration_change"
    )
    applied_ids = {change.change_id for change in applied_changes}
    pending_changes = tuple(
        change
        for change in build(
            pending, default_status="pending", fallback_kind="configuration_change"
        )
        if change.change_id not in applied_ids
    )
    return applied_changes, pending_changes


def _canonical_delivery(
    run_dir: Path, budget: _ProgressBudget, *, current_run_id: str
) -> tuple[RunProgressDeliveryStage, ...]:
    stages = ("Final review", "Merge", "Publish", "CI", "Live verification")
    payload, reason = _canonical_read_json(
        run_dir / "publication.json",
        budget,
        label="publication.json",
        root=run_dir,
    )
    if payload is None:
        default = "unknown" if reason == "invalid_evidence" else "unknown"
        return tuple(RunProgressDeliveryStage(stage=stage, status=default) for stage in stages)  # type: ignore[arg-type]

    source_reference = _canonical_source_reference(
        payload, source_run_id=current_run_id, default_artifact="publication.json"
    )
    raw_stages = payload.get("stages")
    stage_values: dict[str, object] = {}
    if isinstance(raw_stages, Mapping):
        for key, value in raw_stages.items():
            stage_values[str(key).casefold()] = value

    def stage_payload(label: str) -> object:
        lowered = label.casefold()
        for key, value in stage_values.items():
            if key == lowered or key.replace("_", " ") == lowered:
                return value
        return payload.get(label) or payload.get(label.casefold().replace(" ", "_"))

    def status_from(value: object) -> str:
        status = _canonical_status(value)
        if status in {"pending", "running", "succeeded", "failed", "unknown", "not_applicable"}:
            return status
        if status in {"published", "committed", "complete", "completed", "success"}:
            return "succeeded"
        return "unknown"

    result: list[RunProgressDeliveryStage] = []
    lifecycle = payload.get("plan_lifecycle")
    lifecycle_mapping = lifecycle if isinstance(lifecycle, Mapping) else {}
    receipt_status = _canonical_status(payload.get("status"))
    for stage in stages:
        item = stage_payload(stage)
        if isinstance(item, Mapping):
            status = status_from(item.get("status") or item.get("state"))
            reference = _canonical_source_reference(
                item,
                source_run_id=current_run_id,
                default_artifact="publication.json",
            )
            result.append(
                RunProgressDeliveryStage(
                    stage=stage,
                    status=status,  # type: ignore[arg-type]
                    recorded_at=_canonical_recorded_at(item),
                    reason=_canonical_record_reason(item),
                    source_reference=reference,
                )
            )
            continue
        status = status_from(item)
        if stage == "Final review":
            status = status_from(
                payload.get("final_review_status")
                or payload.get("review_status")
                or lifecycle_mapping.get("status")
            )
        elif stage == "Merge":
            status = status_from(
                payload.get("merge_status") or lifecycle_mapping.get("merge_status")
            )
        elif stage == "Publish":
            status = status_from(payload.get("publish_status") or receipt_status)
        elif stage == "CI":
            status = status_from(payload.get("ci_status"))
        elif stage == "Live verification":
            status = status_from(
                payload.get("live_verification_status") or payload.get("live_status")
            )
        result.append(
            RunProgressDeliveryStage(
                stage=stage,
                status=status,  # type: ignore[arg-type]
                recorded_at=_canonical_recorded_at(payload),
                reason=_canonical_record_reason(payload),
                source_reference=source_reference,
            )
        )
    return tuple(result)


def _canonical_plan_path(plan: _CanonicalPlan, metadata: Mapping[str, Any]) -> str | None:
    if plan.path is not None:
        roots, execution_roots = _declared_roots(metadata, None)
        for root in (*roots, *execution_roots):
            try:
                return plan.path.relative_to(root).as_posix()
            except ValueError:
                continue
        return plan.path.name
    for key in ("original_plan_path", "plan_path"):
        value = _canonical_relative_artifact(metadata.get(key))
        if value is not None:
            return value
    return None


def _canonical_sections(plan: _CanonicalPlan) -> tuple[Mapping[str, Any], ...]:
    sections: list[Mapping[str, Any]] = []
    for position, raw in enumerate(plan.sections, start=1):
        if not isinstance(raw, Mapping):
            continue
        index = _canonical_int(raw.get("index"), positive=True) or position
        name = _canonical_title(raw.get("name")) or f"Checkpoint {index}"
        sections.append(
            {
                "index": index,
                "name": name,
                "heading_checked": raw.get("heading_checked") is True,
                "checked_step_count": _canonical_int(raw.get("checked_step_count")) or 0,
                "unchecked_step_count": _canonical_int(raw.get("unchecked_step_count")) or 0,
            }
        )
    return tuple(sections)


def _canonical_invocation_record(invocation: _Invocation) -> dict[str, Any]:
    record = dict(invocation.record)
    if invocation.scope_id is not None:
        record["_scope_id"] = invocation.scope_id
    if invocation.checkpoint_index is not None:
        record["_checkpoint_index"] = invocation.checkpoint_index
    if invocation.checkpoint_name is not None:
        record["_checkpoint_name"] = invocation.checkpoint_name
    return record


def _canonical_event_time(event: RunProgressEvent) -> datetime:
    for value in (event.started_at, event.ended_at):
        if value is not None:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _canonical_latest_timestamp(values: list[str | None]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for value in values:
        if value is None:
            continue
        try:
            parsed.append(
                (datetime.fromisoformat(value.replace("Z", "+00:00")), value)
            )
        except ValueError:
            continue
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[0])[1]


def _canonical_active_and_last_executors(
    *,
    workers: tuple[_Invocation, ...],
    reviewers: tuple[_Invocation, ...],
    metadata: Mapping[str, Any],
    current_run_id: str,
    activity: str,
    discovery_incomplete: bool = False,
    unreadable_turn_numbers: frozenset[int] = frozenset(),
    unreadable_turn_order_unknown: bool = False,
) -> tuple[RunProgressExecutor | None, RunProgressExecutor | None]:
    invocations = [
        invocation
        for invocation in (*workers, *reviewers)
        if invocation.source_run_id == current_run_id and not invocation.inherited
    ]

    def is_open(invocation: _Invocation) -> bool:
        record = invocation.record
        status = _canonical_status(record.get("status"))
        return (
            status in _ACTIVE_TURN_STATUSES
            and _canonical_timestamp(record.get("finished_at")) is None
        )

    def executor(invocation: _Invocation) -> RunProgressExecutor:
        return _canonical_executor(
            invocation.record,
            role=invocation.kind,
            source_run_id=invocation.source_run_id,
            turn_number=invocation.turn_number,
        )

    def recency_obscured(invocation: _Invocation) -> bool:
        return (
            discovery_incomplete
            or unreadable_turn_order_unknown
            or invocation.turn_number is None
            or any(number > invocation.turn_number for number in unreadable_turn_numbers)
        )

    def supplied_executor() -> RunProgressExecutor | None:
        supplied = metadata.get("current_executor")
        if not isinstance(supplied, Mapping):
            return None
        return _canonical_executor(
            supplied,
            role=_canonical_text(supplied.get("role"), limit=80),
            source_run_id=_canonical_source_run_id(
                supplied.get("source_run_id"),
                current_run_id=_canonical_text(metadata.get("run_id"), limit=128)
                or "current",
            ),
            turn_number=_canonical_int(supplied.get("turn_number"), positive=True),
        )

    active = [invocation for invocation in invocations if is_open(invocation)]
    current: RunProgressExecutor | None = None
    if activity == "active" and active:
        selected = max(active, key=lambda item: (item.turn_number or 0, item.source_run_id))
        current = supplied_executor() if recency_obscured(selected) else executor(selected)
    elif activity == "active":
        current = supplied_executor()

    finished = [
        invocation
        for invocation in invocations
        if (
            _canonical_timestamp(
                invocation.record.get("finished_at")
                or invocation.record.get("ended_at")
            )
            is not None
            or _canonical_status(invocation.record.get("status"))
            in {"completed", "complete", "failed", "interrupted", "stopped"}
            or (
                _canonical_status(invocation.record.get("status"))
                not in _ACTIVE_TURN_STATUSES
                and (
                    _canonical_record_outcome(invocation.record) is not None
                    or _canonical_recorded_rejection(invocation.record)
                )
            )
        )
    ]
    last: RunProgressExecutor | None = None
    if finished:
        selected = max(
            finished,
            key=lambda item: (
                item.turn_number or 0,
                _canonical_recorded_at(item.record) or "",
                item.source_run_id,
            ),
        )
        if not recency_obscured(selected):
            last = executor(selected)
    return current, last


def _canonical_detail_uncached(
    run_dir: Path,
    metadata: Mapping[str, Any],
    manager_context: Mapping[str, Any] | None,
    *,
    activity: str | None,
    phase: str | None,
    run_status: str | None,
    budget: _ProgressBudget | None = None,
    discovery: _CanonicalDiscovery | None = None,
) -> RunProgressDetail:
    budget = budget or _ProgressBudget()
    current_run_id = _canonical_text(metadata.get("run_id"), limit=128) or run_dir.name
    plan = _canonical_resolve_plan(run_dir, metadata, manager_context, budget)
    sections = _canonical_sections(plan)
    scope = _canonical_scope_projection(metadata, manager_context)
    turns = _canonical_turns(run_dir, budget, discovery=discovery)
    manager_records = _canonical_manager_records(run_dir, budget, discovery=discovery)
    raw_events = _canonical_events(run_dir, budget)
    rejection_records = _canonical_rejection_info(
        _canonical_rejection_sources(
            metadata, manager_context, manager_records=manager_records
        ),
        current_run_id=current_run_id,
    )
    collection_activity = _canonical_status(activity or metadata.get("activity")) or "unknown"
    if collection_activity not in {"active", "inactive", "unknown"}:
        collection_activity = "unknown"
    workers, reviewers, retries = _canonical_collect_invocations(
        metadata=metadata,
        manager_context=manager_context,
        turns=turns,
        rejections=rejection_records,
        manager_records=manager_records,
        current_run_id=current_run_id,
        budget=budget,
        plan=plan,
        active_scope=scope,
        activity=collection_activity,
    )
    approved_ordinals, approval_records = _canonical_approval_records(
        metadata=metadata,
        manager_context=manager_context,
        turns=turns,
        reviewers=reviewers,
        manager_records=manager_records,
        current_run_id=current_run_id,
    )

    association_records: list[Mapping[str, Any]] = [
        _canonical_invocation_record(item) for item in (*workers, *reviewers)
    ]
    association_records.extend(rejection_records)
    association_records.extend(approval_records)
    association_records.extend(manager_records)
    association_records.extend(raw_events)
    association_records.extend(
        _canonical_repartition_records(
            metadata, manager_context, manager_records=manager_records
        )
    )
    scope_indices, ambiguous_scopes = _canonical_scope_index_map(
        association_records,
        scope,
    )
    applied_changes, pending_changes = _canonical_changes(
        metadata=metadata,
        manager_context=manager_context,
        plan=plan,
        scope_indices=scope_indices,
        ambiguous_scopes=ambiguous_scopes,
        current_run_id=current_run_id,
        manager_records=manager_records,
    )

    activity_value = collection_activity
    phase_value = _canonical_text(
        phase or metadata.get("phase") or metadata.get("launch_phase"), limit=120
    )
    status_value = _canonical_status(run_status or metadata.get("status"))
    discovery_incomplete = _canonical_discovery_is_incomplete(
        discovery, "turns", "manager"
    )
    current_executor, last_executor = _canonical_active_and_last_executors(
        workers=workers,
        reviewers=reviewers,
        metadata=metadata,
        current_run_id=current_run_id,
        activity=activity_value,
        discovery_incomplete=discovery_incomplete,
        unreadable_turn_numbers=frozenset(budget.unreadable_turn_numbers),
        unreadable_turn_order_unknown=budget.unreadable_turn_order_unknown,
    )

    history_complete = _canonical_explicit_history_complete(metadata, manager_context)
    omitted = budget.omitted_records > 0 or budget.omitted_checkpoints > 0
    current_scope_id = _canonical_scope_id(scope or {}) if scope is not None else None
    current_ordinal = _canonical_checkpoint_index(scope or {}) if scope is not None else None
    if current_ordinal is None:
        current_ordinal = _canonical_int(metadata.get("current_checkpoint_index"), positive=True)
    if current_ordinal is None and scope is None:
        current_ordinal = next(
            (
                index
                for index, section in enumerate(sections, start=1)
                if section.get("heading_checked") is not True
            ),
            None,
        )
    current_title = _canonical_checkpoint_name(scope or {}) if scope is not None else None
    if current_title is None and current_ordinal is not None and current_ordinal <= len(sections):
        current_title = _canonical_title(sections[current_ordinal - 1].get("name"))
    current_generation_id = _canonical_text(
        (scope or {}).get("current_partition_generation_id"), limit=240
    )
    current_checkpoint_id = _canonical_checkpoint_id(
        plan, current_ordinal, generation_id=current_generation_id
    )

    def association(record: Mapping[str, Any]) -> tuple[int | None, str | None, str | None, str | None, bool]:
        return _canonical_checkpoint_association(
            record,
            plan=plan,
            scope_indices=scope_indices,
            ambiguous_scopes=ambiguous_scopes,
        )

    def event_association(
        record: Mapping[str, Any], *, assigned: bool
    ) -> ProgressEventAssociation:
        return _canonical_event_association(
            record,
            assigned=assigned,
            plan=plan,
            budget=budget,
            current_run_id=current_run_id,
            scope_indices=scope_indices,
            ambiguous_scopes=ambiguous_scopes,
        )

    def event_reason(
        record: Mapping[str, Any], *, association_kind: ProgressEventAssociation
    ) -> str | None:
        reason = _canonical_record_reason(record)
        if association_kind == "unassigned":
            return f"{reason}; Unassigned history" if reason else "Unassigned history"
        return reason

    worker_by_checkpoint: dict[int, list[_Invocation]] = {}
    reviewer_by_checkpoint: dict[int, list[_Invocation]] = {}
    retry_by_checkpoint: dict[int, list[Mapping[str, Any]]] = {}
    rejection_by_checkpoint: dict[int, list[Mapping[str, Any]]] = {}
    for invocation in workers:
        ordinal, _, _, _, assigned = association(_canonical_invocation_record(invocation))
        if assigned and ordinal is not None:
            worker_by_checkpoint.setdefault(ordinal, []).append(invocation)
    for invocation in reviewers:
        ordinal, _, _, _, assigned = association(_canonical_invocation_record(invocation))
        if assigned and ordinal is not None:
            reviewer_by_checkpoint.setdefault(ordinal, []).append(invocation)
    for retry in retries:
        ordinal, _, _, _, assigned = association(retry)
        if assigned and ordinal is not None:
            retry_by_checkpoint.setdefault(ordinal, []).append(retry)
    for rejection in rejection_records:
        ordinal, _, _, _, assigned = association(rejection)
        if assigned and ordinal is not None:
            rejection_by_checkpoint.setdefault(ordinal, []).append(rejection)

    applied_by_checkpoint: dict[int, list[RunProgressChange]] = {}
    for change in applied_changes:
        if change.checkpoint_id is None:
            continue
        for index, _section in enumerate(sections, start=1):
            if change.checkpoint_id == _canonical_checkpoint_id(plan, index) or (
                change.checkpoint_id is not None
                and change.checkpoint_id.startswith(
                f"{_canonical_checkpoint_id(plan, index)}::generation-"
                )
            ):
                applied_by_checkpoint.setdefault(index, []).append(change)
                break

    explicit_statuses: dict[int, list[str]] = {}
    status_sources = [*association_records, *manager_records]
    for record in status_sources:
        ordinal, _, _, _, assigned = association(record)
        explicit_status = _canonical_status_from_record(record)
        if assigned and ordinal is not None and explicit_status is not None:
            explicit_statuses.setdefault(ordinal, []).append(explicit_status)

    approved_set = {
        index
        for index in approved_ordinals
        if plan.total_sections is None or index <= plan.total_sections
    }
    for record in approval_records:
        ordinal, _scope_id, _generation_id, _parent, assigned = association(record)
        if assigned and ordinal is not None:
            approved_set.add(ordinal)

    active_worker: _Invocation | None = None
    active_reviewer: _Invocation | None = None
    if activity_value == "active":
        open_workers = [
            item
            for item in workers
            if _canonical_status(item.record.get("status")) in _ACTIVE_TURN_STATUSES
            and _canonical_timestamp(item.record.get("finished_at")) is None
        ]
        open_reviewers = [
            item
            for item in reviewers
            if _canonical_status(item.record.get("status")) in _ACTIVE_TURN_STATUSES
            and _canonical_timestamp(item.record.get("finished_at")) is None
        ]
        if open_workers:
            active_worker = max(open_workers, key=lambda item: item.turn_number or 0)
        if open_reviewers:
            active_reviewer = max(open_reviewers, key=lambda item: item.turn_number or 0)

    checkpoints: list[RunProgressCheckpoint] = []

    def retained_generation(position: int) -> str | None:
        candidates: list[Mapping[str, Any]] = [
            _canonical_invocation_record(item)
            for item in (*workers, *reviewers)
        ]
        candidates.extend(rejection_records)
        candidates.extend(association_records)
        for manager_record in manager_records:
            active_scope = manager_record.get("active_implementation_scope")
            if isinstance(active_scope, Mapping):
                candidates.append(active_scope)
            boundary = manager_record.get("_boundary")
            if isinstance(boundary, Mapping):
                active_scope = boundary.get("active_implementation_scope")
                if isinstance(active_scope, Mapping):
                    candidates.append(active_scope)
        if scope is not None and current_ordinal == position:
            candidates.insert(0, scope)
        for candidate in candidates:
            ordinal, _scope_id, generation_id, _parent, assigned = association(candidate)
            if assigned and ordinal == position and generation_id is not None:
                return generation_id
        return None

    for position, section in enumerate(sections, start=1):
        base_checkpoint_id = _canonical_checkpoint_id(plan, position)
        section_workers = [item for item in worker_by_checkpoint.get(position, []) if not item.inherited]
        section_reviewers = [item for item in reviewer_by_checkpoint.get(position, []) if not item.inherited]
        section_retries = [
            item
            for item in retry_by_checkpoint.get(position, [])
            if _canonical_source_run_id(item.get("_source_run_id"), current_run_id=current_run_id)
            == current_run_id
        ]
        section_repairs = [item for item in section_workers if item.repair]
        section_changes = applied_by_checkpoint.get(position, [])
        statuses = explicit_statuses.get(position, [])
        status: str | None = None
        awaiting_review = (
            scope is not None
            and current_ordinal == position
            and scope.get("awaiting_review") is True
            and bool(section_workers)
            and not (
                activity_value == "active"
                and active_reviewer is not None
                and association(_canonical_invocation_record(active_reviewer))[0] == position
            )
            and not (
                activity_value == "active"
                and active_worker is not None
                and association(_canonical_invocation_record(active_worker))[0] == position
            )
        )
        if position in approved_set:
            status = "approved"
        elif (
            activity_value == "active"
            and active_reviewer is not None
            and association(_canonical_invocation_record(active_reviewer))[0] == position
        ):
            status = "reviewing"
        elif (
            activity_value == "active"
            and active_worker is not None
            and association(_canonical_invocation_record(active_worker))[0] == position
        ):
            status = "repairing" if active_worker.repair else "implementing"
        elif "blocked" in statuses:
            status = "blocked"
        elif awaiting_review:
            # A persisted review boundary is an awaiting-review state, not a
            # claim that a reviewer process is currently alive.
            status = "reviewing"
        elif section.get("heading_checked") is True or "recorded_complete" in statuses:
            status = "recorded_complete"
        elif "reviewing" in statuses:
            status = "reviewing"
        elif "repairing" in statuses:
            status = "repairing"
        elif "implementing" in statuses:
            status = "implementing"
        elif not omitted:
            status = "pending"
        else:
            status = "unknown"

        def count_for(value: int, *, has_evidence: bool) -> RunProgressCount:
            return _canonical_count(
                value,
                _canonical_coverage(
                    value=value,
                    has_evidence=has_evidence,
                    history_complete=history_complete,
                    omitted=omitted,
                ),
            )

        recorded_at = _canonical_latest_timestamp(
            [
                _canonical_recorded_at(item.record)
                for item in (*section_workers, *section_reviewers)
            ]
        )
        durations = [
            _canonical_duration(item.record)
            for item in (*section_workers, *section_reviewers)
        ]
        known_durations = [item for item in durations if item is not None]
        scope_id = next(
            (item.scope_id for item in (*section_workers, *section_reviewers) if item.scope_id),
            current_scope_id if position == current_ordinal else None,
        )
        generation_id = retained_generation(position)
        checkpoint_id = base_checkpoint_id
        parent_checkpoint_id = None
        checkpoints.append(
            RunProgressCheckpoint(
                checkpoint_id=checkpoint_id,
                ordinal=position,
                title=_canonical_title(section.get("name")),
                status=status,  # type: ignore[arg-type]
                awaiting_review=awaiting_review,
                worker_attempts=count_for(len(section_workers), has_evidence=bool(section_workers)),
                repair_passes=count_for(len(section_repairs), has_evidence=bool(section_repairs)),
                reviews=count_for(len(section_reviewers), has_evidence=bool(section_reviewers)),
                runtime_retries=count_for(len(section_retries), has_evidence=bool(section_retries)),
                applied_upgrades=count_for(
                    sum(
                        1
                        for item in section_changes
                        if item.kind
                        in {
                            "upgrade",
                            "worker_upgrade",
                            "team_upgrade",
                            "implementation_upgrade",
                        }
                    ),
                    has_evidence=bool(section_changes),
                ),
                recorded_at=recorded_at,
                duration_seconds=(sum(known_durations) if known_durations else None),
                scope_id=scope_id,
                generation_id=generation_id,
                parent_checkpoint_id=parent_checkpoint_id,
                source_run_id=(
                    section_workers[-1].source_run_id
                    if section_workers
                    else section_reviewers[-1].source_run_id
                    if section_reviewers
                    else None
                ),
            )
        )

    events: list[RunProgressEvent] = []
    seen_event_ids: set[str] = set()

    def append_event(event: RunProgressEvent) -> None:
        if event.event_id in seen_event_ids:
            return
        seen_event_ids.add(event.event_id)
        events.append(event)

    def invocation_event(invocation: _Invocation) -> None:
        record = _canonical_invocation_record(invocation)
        ordinal, scope_id, generation_id, _parent, assigned = association(record)
        association_kind = event_association(record, assigned=assigned)
        event_kind = (
            "review_rejection"
            if invocation.kind == "reviewer" and _canonical_recorded_rejection(record)
            else "review"
            if invocation.kind == "reviewer"
            else "repair_attempt"
            if invocation.repair
            else "worker_attempt"
        )
        reason = event_reason(record, association_kind=association_kind)
        turn_number = invocation.turn_number
        default_artifact = (
            f"turns/turn-{turn_number:03d}/result.json" if turn_number is not None else None
        )
        explicit_id = _canonical_text(record.get("event_id"), limit=300)
        event_id = explicit_id or _canonical_event_id(
            kind=event_kind,
            record=record,
            source_run_id=invocation.source_run_id,
            scope_id=scope_id,
            turn_number=turn_number,
            ordinal=ordinal,
        )
        append_event(
            RunProgressEvent(
                event_id=event_id,
                checkpoint_id=_canonical_checkpoint_id(
                    plan, ordinal, generation_id=generation_id
                ),
                scope_id=scope_id,
                source_run_id=invocation.source_run_id,
                turn_number=turn_number,
                decision_number=_canonical_int(
                    record.get("manager_decision_number") or record.get("decision_number"),
                    positive=True,
                ),
                kind=event_kind,
                outcome=_canonical_record_outcome(record),
                executor=_canonical_executor(
                    record,
                    role=invocation.kind,
                    source_run_id=invocation.source_run_id,
                    turn_number=turn_number,
                ),
                started_at=_canonical_timestamp(record.get("started_at")),
                ended_at=_canonical_timestamp(
                    record.get("finished_at") or record.get("ended_at")
                ),
                duration_seconds=_canonical_duration(record),
                reason=reason,
                source_reference=_canonical_source_reference(
                    record,
                    source_run_id=invocation.source_run_id,
                    default_artifact=default_artifact,
                    turn_number=turn_number,
                    decision_number=_canonical_int(
                        record.get("manager_decision_number")
                        or record.get("decision_number"),
                        positive=True,
                    ),
                ),
                association=association_kind,
            )
        )

    for invocation in (*workers, *reviewers):
        invocation_event(invocation)

    for retry in retries:
        ordinal, scope_id, generation_id, _parent, assigned = association(retry)
        association_kind = event_association(retry, assigned=assigned)
        source_run_id = _canonical_source_run_id(
            retry.get("_source_run_id"), current_run_id=current_run_id
        )
        turn_number = _canonical_int(retry.get("_turn_number"), positive=True)
        reason = event_reason(retry, association_kind=association_kind)
        source_reference = dict(
            _canonical_source_reference(
                retry,
                source_run_id=source_run_id,
                default_artifact=(
                    f"turns/turn-{turn_number:03d}/result.json"
                    if turn_number is not None
                    else None
                ),
                turn_number=turn_number,
            )
        )
        completion_turn = _canonical_int(
            retry.get("_retry_completion_turn_number"), positive=True
        )
        if completion_turn is not None:
            source_reference["completion_turn_number"] = completion_turn
        append_event(
            RunProgressEvent(
                event_id=_canonical_event_id(
                    kind="runtime_retry",
                    record=retry,
                    source_run_id=source_run_id,
                    scope_id=scope_id,
                    turn_number=turn_number,
                    ordinal=ordinal,
                ),
                checkpoint_id=_canonical_checkpoint_id(
                    plan, ordinal, generation_id=generation_id
                ),
                scope_id=scope_id,
                source_run_id=source_run_id,
                turn_number=turn_number,
                kind="runtime_retry",
                outcome=_canonical_record_outcome(retry),
                executor=_canonical_executor(
                    retry,
                    role=_canonical_turn_role(retry),
                    source_run_id=source_run_id,
                    turn_number=turn_number,
                ),
                started_at=_canonical_timestamp(retry.get("started_at")),
                ended_at=_canonical_timestamp(retry.get("finished_at")),
                duration_seconds=_canonical_duration(retry),
                reason=reason,
                source_reference=source_reference,
                association=association_kind,
            )
        )

    for record in approval_records:
        ordinal, scope_id, generation_id, _parent, assigned = association(record)
        association_kind = event_association(record, assigned=assigned)
        source_run_id = _canonical_source_run_id(
            record.get("source_run_id"), current_run_id=current_run_id
        )
        reason = event_reason(record, association_kind=association_kind)
        decision_number = _canonical_int(record.get("decision_number"), positive=True)
        turn_number = _canonical_turn_number(record)
        append_event(
            RunProgressEvent(
                event_id=_canonical_text(record.get("event_id"), limit=300)
                or _canonical_event_id(
                    kind="checkpoint_approval",
                    record=record,
                    source_run_id=source_run_id,
                    scope_id=scope_id,
                    turn_number=turn_number,
                    ordinal=ordinal,
                ),
                checkpoint_id=_canonical_checkpoint_id(
                    plan, ordinal, generation_id=generation_id
                ),
                scope_id=scope_id,
                source_run_id=source_run_id,
                turn_number=turn_number,
                decision_number=decision_number,
                kind="checkpoint_approval",
                outcome="approved",
                executor=(
                    _canonical_executor(
                        record,
                        role=_canonical_text(record.get("role"), limit=80) or "reviewer",
                        source_run_id=source_run_id,
                        turn_number=turn_number,
                    )
                    if any(
                        record.get(key) is not None
                        for key in ("selector", "resolved_selector", "team", "actual_team")
                    )
                    else None
                ),
                started_at=_canonical_timestamp(record.get("started_at")),
                ended_at=_canonical_timestamp(
                    record.get("finished_at") or record.get("recorded_at")
                ),
                duration_seconds=_canonical_duration(record),
                reason=reason,
                source_reference=_canonical_source_reference(
                    record,
                    source_run_id=source_run_id,
                    default_artifact="manager/decision-approval/result.json",
                    turn_number=turn_number,
                    decision_number=decision_number,
                ),
                association=association_kind,
            )
        )

    for change in applied_changes:
        if change.status != "applied":
            continue
        source_run_id = _canonical_source_run_id(
            change.source_reference.get("run_id") if change.source_reference else None,
            current_run_id=current_run_id,
        )
        append_event(
            RunProgressEvent(
                event_id=hashlib.sha256(
                    f"applied-change|{source_run_id}|{change.change_id}".encode("utf-8")
                ).hexdigest(),
                checkpoint_id=change.checkpoint_id,
                source_run_id=source_run_id,
                turn_number=change.turn_number,
                kind="applied_upgrade" if "upgrade" in change.kind else "applied_change",
                outcome="applied",
                started_at=change.recorded_at,
                ended_at=change.recorded_at,
                reason=change.reason,
                source_reference=change.source_reference,
                association=("checkpoint" if change.checkpoint_id is not None else "unassigned"),
            )
        )

    for raw in raw_events:
        raw_kind = _canonical_status(raw.get("kind") or raw.get("type") or raw.get("event")) or "history"
        if "retry" in raw_kind:
            event_kind = "runtime_retry"
        elif "review" in raw_kind:
            event_kind = "review_rejection" if _canonical_recorded_rejection(raw) else "review"
        elif "upgrade" in raw_kind or "hotplug" in raw_kind:
            event_kind = "applied_upgrade"
        elif "worker" in raw_kind or "implementation" in raw_kind:
            event_kind = "worker_attempt"
        else:
            event_kind = "history"
        ordinal, scope_id, generation_id, _parent, assigned = association(raw)
        association_kind = event_association(raw, assigned=assigned)
        source_run_id = _canonical_source_run_id(
            raw.get("source_run_id"), current_run_id=current_run_id
        )
        turn_number = _canonical_turn_number(raw)
        reason = event_reason(raw, association_kind=association_kind)
        append_event(
            RunProgressEvent(
                event_id=_canonical_text(raw.get("event_id"), limit=300)
                or _canonical_event_id(
                    kind=event_kind,
                    record=raw,
                    source_run_id=source_run_id,
                    scope_id=scope_id,
                    turn_number=turn_number,
                    ordinal=ordinal,
                ),
                checkpoint_id=_canonical_checkpoint_id(
                    plan, ordinal, generation_id=generation_id
                ),
                scope_id=scope_id,
                source_run_id=source_run_id,
                turn_number=turn_number,
                decision_number=_canonical_int(raw.get("decision_number"), positive=True),
                kind=event_kind,
                outcome=_canonical_record_outcome(raw),
                executor=(
                    _canonical_executor(
                        raw,
                        role=_canonical_turn_role(raw),
                        source_run_id=source_run_id,
                        turn_number=turn_number,
                    )
                    if _canonical_turn_role(raw) is not None
                    else None
                ),
                started_at=_canonical_timestamp(raw.get("started_at")),
                ended_at=_canonical_timestamp(
                    raw.get("finished_at") or raw.get("ended_at")
                ),
                duration_seconds=_canonical_duration(raw),
                reason=reason,
                source_reference=_canonical_source_reference(
                    raw,
                    source_run_id=source_run_id,
                    default_artifact=_canonical_relative_artifact(raw.get("artifact")),
                    turn_number=turn_number,
                    decision_number=_canonical_int(raw.get("decision_number"), positive=True),
                ),
                association=association_kind,
            )
        )

    events.sort(key=lambda item: (_canonical_event_time(item), item.turn_number or 0, item.event_id))
    if len(events) > _PROGRESS_MAX_VISIBLE_EVENTS:
        budget.omitted_records += len(events) - _PROGRESS_MAX_VISIBLE_EVENTS
        budget.response_limit_records += len(events) - _PROGRESS_MAX_VISIBLE_EVENTS
        budget.notice("Earlier history unavailable in this bounded view")
        events = events[-_PROGRESS_MAX_VISIBLE_EVENTS:]
    budget.events_read = max(budget.events_read, len(events))

    def global_count(
        value: int,
        *,
        has_evidence: bool,
        coverage_complete: bool = False,
    ) -> RunProgressCount:
        return _canonical_count(
            value,
            _canonical_coverage(
                value=value,
                has_evidence=has_evidence,
                history_complete=history_complete or coverage_complete,
                omitted=omitted,
            ),
        )

    current_workers = [item for item in workers if item.source_run_id == current_run_id]
    current_reviewers = [item for item in reviewers if item.source_run_id == current_run_id]
    current_retries = [
        item
        for item in retries
        if _canonical_source_run_id(item.get("_source_run_id"), current_run_id=current_run_id)
        == current_run_id
    ]
    upgrade_kinds = {
        "upgrade",
        "worker_upgrade",
        "team_upgrade",
        "implementation_upgrade",
    }
    applied_upgrades = [item for item in applied_changes if item.kind in upgrade_kinds]

    total_count = _canonical_count(
        len(sections) if sections else None,
        "partial" if budget.omitted_checkpoints else "complete" if sections else "unavailable",
    )
    recorded_value = sum(
        1 for section in sections if section.get("heading_checked") is True
    )
    recorded_count = _canonical_count(
        recorded_value if sections else None,
        "complete" if sections and not budget.omitted_checkpoints else "unavailable",
    )
    approved_count = _canonical_count(
        len({index for index in approved_set if 1 <= index <= len(sections)})
        if approved_set
        else 0
        if history_complete and sections
        else None,
        "complete"
        if history_complete and sections and not omitted
        else "partial"
        if approved_set
        else "unavailable",
    )
    checkpoint_states: Mapping[str, ProgressCheckpointStatus] = {}
    if (
        total_count.coverage == "complete"
        and total_count.value is not None
        and total_count.value <= 30
        and len(checkpoints) == total_count.value
    ):
        checkpoint_states = {
            str(checkpoint.ordinal): checkpoint.status
            for checkpoint in checkpoints
            if checkpoint.ordinal is not None
        }

    evidence_values = [
        *[_canonical_recorded_at(item.record) for item in (*workers, *reviewers)],
        *[_canonical_recorded_at(item) for item in rejection_records],
        *[_canonical_recorded_at(item) for item in approval_records],
        *[item.recorded_at for item in (*applied_changes, *pending_changes)],
    ]
    evidence_at = _canonical_latest_timestamp(evidence_values)

    reason_codes: list[str] = []
    if plan.reason is not None:
        reason_codes.append(plan.reason)
    if omitted:
        reason_codes.append("evidence_truncated")
    if not history_complete and sections:
        reason_codes.append("history_partial")
    if any(item.association == "unassigned" for item in events):
        reason_codes.append("unassigned_history")
    if any(item.association == "whole_plan" for item in events):
        reason_codes.append("whole_plan_review")
    if any(item.association == "outside_returned" for item in events):
        reason_codes.append("history_outside_returned")
    if any("malformed" in notice.casefold() for notice in budget.notices or ()):
        reason_codes.append("malformed_optional_evidence")
    reason_codes = list(dict.fromkeys(reason_codes))

    if plan.reason == "non_checkpoint_plan":
        availability = "not_applicable"
    elif not sections:
        availability = "partial" if scope is not None else "unavailable"
    elif omitted or not history_complete:
        availability = "partial"
    else:
        availability = "complete"

    current_executor = current_executor
    if current_executor is None and activity_value != "active":
        current_executor = None
    delivery = _canonical_delivery(run_dir, budget, current_run_id=current_run_id)
    return RunProgressDetail(
        availability=availability,  # type: ignore[arg-type]
        observed_at=datetime.now(timezone.utc).isoformat(),
        evidence_at=evidence_at,
        reason_codes=tuple(reason_codes),
        original_plan_identity=plan.identity,
        original_plan_display_name=plan.display_name,
        original_plan_path=_canonical_plan_path(plan, metadata),
        total_checkpoints=total_count,
        approved_checkpoints=approved_count,
        recorded_complete_checkpoints=recorded_count,
        checkpoint_states=checkpoint_states,
        current_checkpoint_id=current_checkpoint_id,
        current_checkpoint_ordinal=current_ordinal,
        current_checkpoint_title=current_title,
        activity=activity_value,
        phase=phase_value,
        run_status=status_value,
        current_executor=current_executor,
        last_executor=last_executor,
        worker_attempts=global_count(
            len(current_workers), has_evidence=bool(current_workers)
        ),
        repair_passes=global_count(
            sum(1 for item in current_workers if item.repair),
            has_evidence=any(item.repair for item in current_workers),
        ),
        reviews=global_count(len(current_reviewers), has_evidence=bool(current_reviewers)),
        runtime_retries=global_count(
            len(current_retries), has_evidence=bool(current_retries)
        ),
        applied_upgrades=global_count(
            len(applied_upgrades), has_evidence=bool(applied_upgrades)
        ),
        checkpoints=tuple(checkpoints),
        events=tuple(events),
        applied_changes=applied_changes,
        pending_changes=pending_changes,
        delivery=delivery,
        truncation=budget.truncation(),
    )


def _canonical_empty_detail(
    *,
    reason: str,
    activity: str | None = None,
    phase: str | None = None,
    run_status: str | None = None,
    budget: _ProgressBudget | None = None,
) -> RunProgressDetail:
    safe_activity = _canonical_status(activity) or "unknown"
    if safe_activity not in {"active", "inactive", "unknown"}:
        safe_activity = "unknown"
    return RunProgressDetail(
        availability="unavailable",
        observed_at=datetime.now(timezone.utc).isoformat(),
        reason_codes=(reason,),
        activity=safe_activity,
        phase=_canonical_text(phase, limit=120),
        run_status=_canonical_status(run_status),
        truncation=(budget or _ProgressBudget()).truncation(),
    )


def invalidate_run_progress_cache(run_dir: Path) -> None:
    """Drop cached projections for one run without touching durable evidence."""
    try:
        identity = str(Path(run_dir).resolve())
    except (OSError, RuntimeError):
        return
    for key in tuple(_PROGRESS_CACHE):
        if key and key[0] == identity:
            _PROGRESS_CACHE.pop(key, None)


def project_run_progress_detail(
    run_dir: Path,
    *,
    metadata: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    manager_context: Mapping[str, Any] | None = None,
    activity: str | None = None,
    phase: str | None = None,
    run_status: str | None = None,
    use_cache: bool = True,
) -> RunProgressDetail:
    """Return one bounded, read-only canonical run-progress detail projection."""
    if metadata is not None and run_metadata is not None and metadata != run_metadata:
        raise ValueError("metadata and run_metadata disagree")
    supplied_metadata = metadata if metadata is not None else run_metadata
    candidate = Path(run_dir)
    if candidate.is_symlink() or not candidate.is_dir():
        return _canonical_empty_detail(
            reason="missing_run",
            activity=activity,
            phase=phase,
            run_status=run_status,
        )
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return _canonical_empty_detail(
            reason="unavailable_run",
            activity=activity,
            phase=phase,
            run_status=run_status,
        )
    initial_budget = _ProgressBudget()
    if supplied_metadata is None:
        loaded, load_reason = _canonical_read_json(
            root / "run.json", initial_budget, label="run.json", root=root
        )
        if loaded is None:
            if load_reason == "invalid_evidence":
                return _canonical_empty_detail(
                    reason="invalid_evidence",
                    activity=activity,
                    phase=phase,
                    run_status=run_status,
                    budget=initial_budget,
                )
            supplied_metadata = {}
        else:
            supplied_metadata = loaded
    elif not isinstance(supplied_metadata, Mapping):
        return _canonical_empty_detail(
            reason="invalid_evidence",
            activity=activity,
            phase=phase,
            run_status=run_status,
            budget=initial_budget,
        )
    metadata_mapping = dict(supplied_metadata)
    discovery = _CanonicalDiscovery()
    key = _canonical_cache_key(
        root,
        metadata_mapping,
        manager_context,
        activity=activity,
        phase=phase,
        run_status=run_status,
        discovery=discovery,
    )
    now = time.monotonic()
    if use_cache:
        cached = _PROGRESS_CACHE.get(key)
        if cached is not None:
            cached_at, detail = cached
            if now - cached_at <= _PROGRESS_CACHE_TTL_SECONDS:
                _PROGRESS_CACHE.move_to_end(key)
                return detail
            _PROGRESS_CACHE.pop(key, None)
    detail = _canonical_detail_uncached(
        root,
        metadata_mapping,
        manager_context,
        activity=activity,
        phase=phase,
        run_status=run_status,
        budget=initial_budget,
        discovery=discovery,
    )
    if use_cache:
        _PROGRESS_CACHE[key] = (now, detail)
        _PROGRESS_CACHE.move_to_end(key)
        while len(_PROGRESS_CACHE) > _PROGRESS_CACHE_MAX_RUNS:
            _PROGRESS_CACHE.popitem(last=False)
    return detail


def project_run_progress_summary(
    run_dir: Path,
    *,
    metadata: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    manager_context: Mapping[str, Any] | None = None,
    activity: str | None = None,
    phase: str | None = None,
    run_status: str | None = None,
    use_cache: bool = True,
) -> RunProgressSummary:
    """Return the array-free summary projection from the canonical reducer."""
    detail = project_run_progress_detail(
        run_dir,
        metadata=metadata,
        run_metadata=run_metadata,
        manager_context=manager_context,
        activity=activity,
        phase=phase,
        run_status=run_status,
        use_cache=use_cache,
    )
    return RunProgressSummary(
        **{field.name: getattr(detail, field.name) for field in fields(RunProgressSummary)}
    )


def project_run_progress_canonical(
    run_dir: Path,
    *,
    metadata: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    manager_context: Mapping[str, Any] | None = None,
    activity: str | None = None,
    phase: str | None = None,
    run_status: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return the JSON-safe canonical detail shape for read consumers."""
    return project_run_progress_detail(
        run_dir,
        metadata=metadata,
        run_metadata=run_metadata,
        manager_context=manager_context,
        activity=activity,
        phase=phase,
        run_status=run_status,
        use_cache=use_cache,
    ).to_dict()


__all__ = [
    "invalidate_run_progress_cache",
    "project_run_progress",
    "project_run_progress_canonical",
    "project_run_progress_detail",
    "project_run_progress_summary",
]

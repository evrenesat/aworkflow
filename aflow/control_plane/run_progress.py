"""Read-only progress projection for control-plane observers.

The workflow controller owns plan and turn state.  This module only projects
that state for REST/MCP consumers; it never repairs, normalizes, or rewrites
the artifacts it reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

from aflow.plan import ParsedPlan, PlanParseError, parse_plan_text

from .models import bounded_redacted


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
    run_dir: Path, metadata: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    number = metadata.get("manager_decision_number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    path = run_dir / "manager" / f"decision-{number:03d}" / "boundary.json"
    payload = _read_json(path)
    return payload or None


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


__all__ = ["project_run_progress"]

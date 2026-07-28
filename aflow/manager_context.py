"""Versioned, plan-safe semantic evidence for manager supervision.

The builder deliberately reads turn artifacts and result metadata only.  Lite
contexts never read prompt artifacts or active-plan contents.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Literal

from .analyzer import analyze_progress_tail, extract_text_signals, snapshot_signature
from .plan import PlanParseError, load_plan_tolerant
from .stop_marker import extract_stop_markers


MANAGER_CONTEXT_SCHEMA_VERSION = 1
DIAGNOSTIC_LIMIT = 2_000
ManagerLevel = Literal["lite", "full"]


@dataclass(frozen=True)
class RawArtifactReference:
    path: str
    byte_size: int
    status: str


@dataclass(frozen=True)
class SemanticTurnOutcome:
    extraction: str
    result: str
    fallback: bool


@dataclass(frozen=True)
class ProgressSignals:
    unchanged_snapshot_turns: int
    same_step_stall_turns: int
    alternating_two_step_tail: bool
    reviewer_rejection_count: int
    reviewer_non_convergence: bool


@dataclass(frozen=True)
class StructuredPlanState:
    original_plan_path: str | None
    active_plan_path: str | None
    active_repair_plan: bool
    checkpoints: tuple[dict[str, Any], ...]
    current_checkpoint: dict[str, Any] | None
    is_complete: bool | None
    parse_error: str | None


@dataclass(frozen=True)
class CompactRunRecord:
    kind: Literal["workflow_turn", "manager_decision"]
    number: int
    status: str | None
    step_name: str | None
    team: str | None
    selector: str | None
    semantic_summary: str | None
    plan_delta: bool | None
    signals: tuple[str, ...]
    routing: dict[str, Any]


@dataclass(frozen=True)
class ManagerContextV1:
    schema_version: int
    run_id: str
    decision_number: int
    level: ManagerLevel
    trigger: str
    finished_turn: dict[str, Any]
    run_extract: tuple[dict[str, Any], ...]
    plan_state: dict[str, Any]
    controller_state: dict[str, Any]
    active_plan_content: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _artifact_ref(run_dir: Path, path: Path) -> RawArtifactReference:
    try:
        size = path.stat().st_size
        status = "present"
    except OSError:
        size = 0
        status = "missing"
    return RawArtifactReference(path=str(path.relative_to(run_dir)), byte_size=size, status=status)


def _bounded(text: str) -> str:
    if len(text) <= DIAGNOSTIC_LIMIT:
        return text
    return text[:DIAGNOSTIC_LIMIT] + "\n[diagnostic excerpt truncated]"


def _text_content(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_text_content(item) for item in value]
        joined = "".join(part for part in parts if part)
        return joined or None
    if isinstance(value, dict):
        for key in ("text", "content", "output_text"):
            candidate = _text_content(value.get(key))
            if candidate:
                return candidate
    return None


def _structured_final_assistant_result(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("role") == "assistant":
            candidate = _text_content(value.get("content", value.get("text")))
            if candidate:
                return candidate
        for key in ("result", "final", "final_output", "output", "message", "item"):
            candidate = _structured_final_assistant_result(value.get(key))
            if candidate:
                return candidate
        candidates = [_structured_final_assistant_result(item) for item in value.get("messages", [])] if isinstance(value.get("messages"), list) else []
        return next((item for item in reversed(candidates) if item), None)
    if isinstance(value, list):
        candidates = [_structured_final_assistant_result(item) for item in value]
        return next((item for item in reversed(candidates) if item), None)
    return None


def extract_semantic_result(stdout: str) -> SemanticTurnOutcome:
    """Extract a complete final assistant response from common JSON/event streams."""
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(stdout))
    except json.JSONDecodeError:
        pass
    for line in stdout.splitlines():
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    for candidate in reversed(candidates):
        result = _structured_final_assistant_result(candidate)
        if result:
            return SemanticTurnOutcome("structured_stream", result, False)
    if candidates:
        return SemanticTurnOutcome("unrecognized_structured_stream", stdout, True)
    return SemanticTurnOutcome("plain_text", stdout, False)


def _load_turns(run_dir: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    turns_dir = run_dir / "turns"
    if not turns_dir.is_dir():
        return turns
    for turn_dir in sorted(path for path in turns_dir.iterdir() if path.is_dir()):
        result_path = turn_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result["_turn_dir"] = turn_dir
        turns.append(result)
    return turns


def _path_from_metadata(run_dir: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_file():
        return candidate
    relative = run_dir.parent.parent.parent / candidate
    return relative if relative.is_file() else None


def _plan_state(run_dir: Path, run_json: dict[str, Any], finished_turn: dict[str, Any]) -> tuple[StructuredPlanState, Path | None]:
    active_value = finished_turn.get("active_plan_path") or run_json.get("active_plan_path") or run_json.get("plan_path")
    original_value = finished_turn.get("original_plan_path") or run_json.get("original_plan_path") or run_json.get("plan_path")
    active_path = _path_from_metadata(run_dir, active_value)
    original_path = _path_from_metadata(run_dir, original_value)
    checkpoints: tuple[dict[str, Any], ...] = ()
    current: dict[str, Any] | None = None
    complete: bool | None = None
    parse_error: str | None = None
    if active_path is not None:
        try:
            loaded = load_plan_tolerant(active_path)
            checkpoints = tuple({
                "index": index,
                "name": section.name,
                "heading_checked": section.heading_checked,
                "checked_step_count": section.checked_step_count,
                "unchecked_step_count": section.unchecked_step_count,
            } for index, section in enumerate(loaded.parsed_plan.sections, start=1))
            snapshot = loaded.parsed_plan.snapshot
            complete = snapshot.is_complete
            if snapshot.current_checkpoint_index is not None:
                current = next((item for item in checkpoints if item["index"] == snapshot.current_checkpoint_index), None)
            parse_error = str(loaded.parse_error) if loaded.parse_error else None
        except (OSError, PlanParseError, ValueError) as exc:
            parse_error = str(exc)
    return StructuredPlanState(
        original_plan_path=str(original_value) if isinstance(original_value, str) else None,
        active_plan_path=str(active_value) if isinstance(active_value, str) else None,
        active_repair_plan=bool(active_value and original_value and active_value != original_value),
        checkpoints=checkpoints,
        current_checkpoint=current,
        is_complete=complete,
        parse_error=parse_error,
    ), active_path


def analyze_manager_progress(turns: list[dict[str, Any]]) -> ProgressSignals:
    progress = analyze_progress_tail(turns)
    return ProgressSignals(
        unchanged_snapshot_turns=progress["unchanged_snapshot_turns"],
        same_step_stall_turns=progress["same_step_stall_turns"],
        alternating_two_step_tail=progress["alternating_two_step_tail"],
        reviewer_rejection_count=progress["reviewer_rejection_count"],
        reviewer_non_convergence=progress["reviewer_non_convergence"],
    )


def _duration_seconds(turn: dict[str, Any]) -> float | None:
    explicit = turn.get("duration_seconds")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return float(explicit)
    started = turn.get("started_at")
    finished = turn.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return None


def _compact_turn(run_dir: Path, turn: dict[str, Any]) -> CompactRunRecord:
    turn_dir = Path(turn["_turn_dir"])
    semantic = extract_semantic_result(_read_text(turn_dir / "stdout.txt") or str(turn.get("stdout", "")))
    before = snapshot_signature(turn.get("snapshot_before"))
    after = snapshot_signature(turn.get("snapshot_after"))
    return CompactRunRecord(
        kind="workflow_turn",
        number=int(turn.get("turn_number", 0)),
        status=turn.get("status") if isinstance(turn.get("status"), str) else None,
        step_name=turn.get("step_name") if isinstance(turn.get("step_name"), str) else None,
        team=turn.get("team") if isinstance(turn.get("team"), str) else None,
        selector=turn.get("selector") if isinstance(turn.get("selector"), str) else None,
        semantic_summary=_bounded(semantic.result),
        plan_delta=before is not None and after is not None and before != after,
        signals=tuple(sorted(set(extract_text_signals(semantic.result) + (["explicit_stop"] if extract_stop_markers(semantic.result) else [])))),
        routing={"chosen_transition": turn.get("chosen_transition"), "recovery_action": turn.get("recovery_action")},
    )


def _manager_records(run_dir: Path, *, before_decision_number: int | None = None) -> list[dict[str, Any]]:
    root = run_dir / "manager"
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for decision_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        result_path = decision_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        decision_number = result.get("decision_number", len(records) + 1)
        if before_decision_number is not None and isinstance(decision_number, int) and decision_number >= before_decision_number:
            continue
        records.append({
            "kind": "manager_decision", "number": decision_number,
            "status": result.get("status"), "step_name": None, "team": None, "selector": None,
            "semantic_summary": result.get("reason"), "plan_delta": None, "signals": [],
            "routing": {"action": result.get("action")},
        })
    return records


def build_manager_context(
    run_dir: Path,
    *,
    level: ManagerLevel = "lite",
    trigger: str = "post_turn",
    decision_number: int | None = None,
    turns: list[dict[str, Any]] | None = None,
    run_metadata: dict[str, Any] | None = None,
    boundary: dict[str, Any] | None = None,
    active_plan_content: str | None = None,
) -> dict[str, Any]:
    """Build deterministic manager context from durable run artifacts.

    The exact same function is intentionally suitable for runtime and later
    analysis.  Full is the sole path that reads the active plan text.
    """
    run_dir = Path(run_dir)
    run_json_path = run_dir / "run.json"
    if run_metadata is not None:
        run_json = dict(run_metadata)
    else:
        try:
            run_json = json.loads(run_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            run_json = {}
    turns = list(turns) if turns is not None else _load_turns(run_dir)
    if not turns:
        raise ValueError(f"{run_dir}: no finalized workflow turn artifacts found")
    finished = turns[-1]
    finished_dir = Path(finished["_turn_dir"])
    stdout = _read_text(finished_dir / "stdout.txt") or str(finished.get("stdout", ""))
    stderr = _read_text(finished_dir / "stderr.txt") or str(finished.get("stderr", ""))
    semantic = extract_semantic_result(stdout)
    boundary = dict(boundary or {})
    # Boundary inputs are finalized before manager invocation.  They take
    # precedence over mutable run metadata so the same durable inputs rebuild
    # the exact runtime context later.
    if boundary.get("original_plan_path") is not None:
        run_json["original_plan_path"] = boundary["original_plan_path"]
    if boundary.get("active_plan_path") is not None:
        run_json["active_plan_path"] = boundary["active_plan_path"]
    plan_state, active_plan_path = _plan_state(run_dir, run_json, finished)
    captured_plan_state = boundary.get("captured_plan_state")
    if isinstance(captured_plan_state, dict):
        # The controller captured this before invoking the manager.  Reuse it
        # during historical analysis so later plan edits cannot alter the
        # boundary that the manager actually saw.
        plan_state_payload = captured_plan_state
    else:
        plan_state_payload = asdict(plan_state)
    whole_run_progress = analyze_manager_progress(turns)
    scope_was_explicit = "active_implementation_scope" in boundary
    scope = (
        boundary.get("active_implementation_scope")
        if scope_was_explicit
        else run_json.get("active_implementation_scope")
    )
    legacy_scoped_boundary = (
        scope_was_explicit
        and isinstance(scope, dict)
        and "opened_turn_number" not in scope
    )
    progress_scope = None
    scoped_progress = whole_run_progress
    if isinstance(scope, dict):
        opened_turn_number = scope.get("opened_turn_number")
        if isinstance(opened_turn_number, int):
            scoped_turns = [
                turn for turn in turns
                if int(turn.get("turn_number", 0) or 0) >= opened_turn_number
            ]
            scoped_progress = analyze_manager_progress(scoped_turns)
            progress_scope = {
                "scope_id": scope.get("scope_id"),
                "opened_turn_number": opened_turn_number,
            }
    elif scope_was_explicit:
        scoped_progress = ProgressSignals(
            unchanged_snapshot_turns=whole_run_progress.unchanged_snapshot_turns,
            same_step_stall_turns=whole_run_progress.same_step_stall_turns,
            alternating_two_step_tail=whole_run_progress.alternating_two_step_tail,
            reviewer_rejection_count=0,
            reviewer_non_convergence=False,
        )
    progress = ProgressSignals(
        unchanged_snapshot_turns=whole_run_progress.unchanged_snapshot_turns,
        same_step_stall_turns=scoped_progress.same_step_stall_turns,
        alternating_two_step_tail=whole_run_progress.alternating_two_step_tail,
        reviewer_rejection_count=scoped_progress.reviewer_rejection_count,
        reviewer_non_convergence=scoped_progress.reviewer_non_convergence,
    )
    progress_payload = asdict(progress)
    if legacy_scoped_boundary:
        progress_payload.pop("same_step_stall_turns", None)
    raw_artifacts = [asdict(_artifact_ref(run_dir, finished_dir / name)) for name in ("stdout.txt", "stderr.txt")]
    finished_turn = {
        "turn_number": finished.get("turn_number"), "step_name": finished.get("step_name"),
        "role": finished.get("step_role"), "team": finished.get("team") or run_json.get("team"),
        "selector": finished.get("selector"), "status": finished.get("status"),
        "returncode": finished.get("returncode"),
        "duration_seconds": (
            finished.get("duration_seconds")
            if legacy_scoped_boundary
            else _duration_seconds(finished)
        ),
        "semantic_result": asdict(semantic), "error": finished.get("error"),
        "snapshot_before": finished.get("snapshot_before"), "snapshot_after": finished.get("snapshot_after"),
        "snapshot_changed": snapshot_signature(finished.get("snapshot_before")) != snapshot_signature(finished.get("snapshot_after")),
        "proposed_transition": boundary.get("proposed_transition", finished.get("chosen_transition")),
        "recovery": finished.get("recovery_action"),
        "conditions": finished.get("conditions"), "detected_stop": extract_stop_markers(stdout) + extract_stop_markers(stderr),
        "diagnostics": {"signals": sorted(set(extract_text_signals("\n".join((stdout, stderr))))), "stdout_excerpt": _bounded(stdout), "stderr_excerpt": _bounded(stderr)},
        "raw_artifacts": raw_artifacts,
    }
    manager_records = _manager_records(run_dir, before_decision_number=decision_number)
    extract = [asdict(_compact_turn(run_dir, turn)) for turn in turns]
    extract.extend(manager_records)
    extract.sort(key=lambda item: (item["number"], item["kind"] != "workflow_turn"))
    context = ManagerContextV1(
        schema_version=MANAGER_CONTEXT_SCHEMA_VERSION,
        run_id=run_dir.name,
        decision_number=decision_number if decision_number is not None else len(manager_records) + 1,
        level=level,
        trigger=trigger,
        finished_turn=finished_turn,
        run_extract=tuple(extract),
        plan_state=plan_state_payload,
        controller_state={
            "baseline_team": boundary.get("baseline_team", run_json.get("team")),
            "actual_team": boundary.get("actual_team"),
            "actual_selector": boundary.get("actual_selector"),
            "turn_budget": {"completed": run_json.get("turns_completed"), "maximum": run_json.get("max_turns")},
            "same_node_counter": (
                progress.unchanged_snapshot_turns
                if legacy_scoped_boundary else progress.same_step_stall_turns
            ),
            "semantic_stall_count": (
                progress.unchanged_snapshot_turns
                if legacy_scoped_boundary else progress.same_step_stall_turns
            ),
            "reviewer_rejection_count": progress.reviewer_rejection_count, "reviewer_non_convergence": progress.reviewer_non_convergence,
            "progress": progress_payload,
            "proposed_action": boundary.get("proposed_action", "transition"),
            "proposed_next_step": boundary.get("proposed_transition", finished.get("chosen_transition")),
            "terminal": bool(boundary.get("terminal", False)),
            "retry_safely": bool(boundary.get("safely_retryable", False)),
            "operational_failure": bool(boundary.get("operational_failure", False)),
            "backup_route": {
                "team": boundary.get("backup_team"), "selector": boundary.get("backup_selector"),
            },
            "eligible_upgrade": boundary.get("implementation_upgrade"),
            "active_implementation_scope": boundary.get("active_implementation_scope"),
            "eligible_actions": list(boundary.get("eligible_actions", [])),
            "lite_evidence": boundary.get("evidence"),
            "workspace_state": boundary.get("workspace_state", {}),
        },
        active_plan_content=(
            active_plan_content if level == "full"
            else None
        ) if active_plan_content is not None else (
            _read_text(active_plan_path) if level == "full" and active_plan_path is not None else None
        ),
    )
    if not legacy_scoped_boundary:
        context.controller_state["progress_scope"] = progress_scope
    # Runtime prompts, artifacts, and later API analysis all use JSON.  Return
    # that canonical shape here too, so tuple/list representation cannot cause
    # a false drift report after a context has been persisted and rebuilt.
    return json.loads(json.dumps(context.to_dict(), sort_keys=True))

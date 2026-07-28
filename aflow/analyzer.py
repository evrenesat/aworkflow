from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .runlog import resolve_last_run_id
from .stop_marker import extract_stop_markers


TEXT_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("blocked_review_precondition", re.compile(r"Blocked on a required `aflow-review-checkpoint` precondition\.", re.IGNORECASE)),
    ("branch_mismatch_review_block", re.compile(r"current checkout is .*requires me to stop and escalate|Plan Branch.*current checkout is", re.IGNORECASE | re.DOTALL)),
    ("needs_human_direction", re.compile(r"Need your direction on one point|Choose one:\s*$", re.IGNORECASE | re.MULTILINE)),
    ("original_plan_missing", re.compile(r"original plan file is missing after the turn", re.IGNORECASE)),
    ("dirty_merge_verification", re.compile(r"merge verification cannot be completed safely|feature worktree and primary checkout are dirty", re.IGNORECASE)),
)

HIGHLIGHT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^AFLOW_STOP:\s*.+$", re.MULTILINE),
    re.compile(r"^Blocked on a required .+$", re.MULTILINE),
    re.compile(r"^Need your direction on one point.*$", re.MULTILINE),
    re.compile(r"^The original plan .*current checkout is .*$", re.MULTILINE),
    re.compile(r"^.*original plan file is missing after the turn.*$", re.MULTILINE),
    re.compile(r"^.*merge verification cannot be completed safely.*$", re.MULTILINE),
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def relpath(path: Path, base: Path | None) -> str:
    if base is None:
        return str(path)
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def extract_aflow_stop(text: str) -> list[str]:
    return extract_stop_markers(text)


def extract_text_signals(text: str) -> list[str]:
    found: list[str] = []
    for name, pattern in TEXT_SIGNAL_PATTERNS:
        if pattern.search(text):
            found.append(name)
    return found


def snapshot_signature(snapshot: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if snapshot is None:
        return None
    return (
        snapshot.get("current_checkpoint_index"),
        snapshot.get("current_checkpoint_name"),
        snapshot.get("current_checkpoint_unchecked_step_count"),
        snapshot.get("is_complete"),
        snapshot.get("total_checkpoint_count"),
        snapshot.get("unchecked_checkpoint_count"),
    )


def is_noise_run(run_json: dict[str, Any]) -> bool:
    return (
        run_json.get("workflow_name") == "other"
        and run_json.get("turns_completed") == 0
        and run_json.get("end_reason") == "already_complete"
    )


def collect_run_dirs(runs_root: Path) -> list[Path]:
    return sorted(path for path in runs_root.iterdir() if path.is_dir())


def find_latest_run_dir(
    run_dirs: list[Path],
    *,
    include_noise: bool,
) -> tuple[Path, int]:
    skipped_noise = 0
    for run_dir in reversed(run_dirs):
        run_json_path = run_dir / "run.json"
        if not run_json_path.is_file():
            continue
        run_json = load_json(run_json_path)
        if is_noise_run(run_json) and not include_noise:
            skipped_noise += 1
            continue
        return run_dir, skipped_noise
    if skipped_noise:
        raise SystemExit("No substantive runs found under the selected runs root; rerun with --include-noise if you want test-noise runs.")
    raise SystemExit("No runs with run.json were found under the selected runs root.")


def load_turns(run_dir: Path) -> list[dict[str, Any]]:
    turns_dir = run_dir / "turns"
    if not turns_dir.is_dir():
        return []
    turn_dirs = sorted(path for path in turns_dir.iterdir() if path.is_dir())
    turns: list[dict[str, Any]] = []
    for turn_dir in turn_dirs:
        result_path = turn_dir / "result.json"
        if not result_path.is_file():
            continue
        payload = load_json(result_path)
        payload["_turn_dir"] = turn_dir
        turns.append(payload)
    return turns


def read_turn_stream(turn_dir: Path, turn: dict[str, Any], *, filename: str, inline_key: str) -> str:
    text = read_text(turn_dir / filename)
    if text:
        return text
    inline_text = turn.get(inline_key)
    if isinstance(inline_text, str):
        return inline_text
    return ""


def summarize_text_lines(text: str) -> list[str]:
    highlights: list[str] = []
    seen: set[str] = set()
    for pattern in HIGHLIGHT_PATTERNS:
        for match in pattern.finditer(text):
            line = " ".join(match.group(0).strip().split())
            if line and line not in seen:
                highlights.append(line)
                seen.add(line)
    return highlights


def summarize_reason_text(reason: str | None) -> list[str]:
    if not reason:
        return []
    lines = [" ".join(line.strip().split()) for line in str(reason).splitlines() if line.strip()]
    if not lines:
        return []
    return [lines[0]]


def _normalize_recovery_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    normalized = {
        "source": payload.get("source", payload.get("recovery_source")),
        "action": payload.get("action", payload.get("recovery_action")),
        "match_terms": list(payload.get("match_terms", payload.get("recovery_match_terms", [])) or []),
        "matched_terms": list(payload.get("matched_terms", payload.get("recovery_matched_terms", [])) or []),
        "delay_seconds": payload.get("delay_seconds", payload.get("recovery_delay_seconds")),
        "from_team": payload.get("from_team", payload.get("recovery_from_team")),
        "to_team": payload.get("to_team", payload.get("recovery_to_team")),
        "reason": payload.get("reason", payload.get("recovery_reason")),
        "consecutive_count": payload.get("consecutive_count", payload.get("recovery_consecutive_count")),
        "suggested_keywords": list(payload.get("suggested_keywords", payload.get("recovery_suggested_keywords", [])) or []),
        "suggested_action": payload.get("suggested_action", payload.get("recovery_suggested_action")),
        "executed": payload.get("executed", payload.get("recovery_executed")),
        "rejection_reason": payload.get("rejection_reason", payload.get("recovery_rejection_reason")),
    }
    if not any(value is not None and value != [] for value in normalized.values()):
        return None
    return normalized


def _recovery_signal_names(recovery: dict[str, Any] | None) -> list[str]:
    if recovery is None:
        return []

    signals: list[str] = ["harness_recovery_present"]
    source = recovery.get("source")
    action = recovery.get("action")
    if source == "deterministic":
        signals.append("harness_recovery_deterministic")
    elif source == "team_lead":
        signals.append("harness_recovery_team_lead")

    if action == "switch_to_backup_team_and_retry" or recovery.get("from_team") != recovery.get("to_team"):
        signals.append("harness_recovery_team_switch")
    if action == "fail_immediately":
        signals.append("harness_recovery_fail_immediately")
    if recovery.get("suggested_keywords"):
        signals.append("harness_recovery_keyword_suggestions")
    if recovery.get("suggested_action"):
        signals.append("harness_recovery_suggested_action")
    return signals


def analyze_progress_tail(turns: list[dict[str, Any]]) -> dict[str, Any]:
    finalized_turns = list(turns)
    while finalized_turns and (
        finalized_turns[-1].get("snapshot_after") is None
        or finalized_turns[-1].get("status") == "starting"
    ):
        finalized_turns.pop()

    unchanged_tail: list[dict[str, Any]] = []
    for turn in reversed(finalized_turns):
        before_sig = snapshot_signature(turn.get("snapshot_before"))
        after_sig = snapshot_signature(turn.get("snapshot_after"))
        if before_sig is None or after_sig is None or before_sig != after_sig:
            break
        unchanged_tail.append(turn)
    unchanged_tail.reverse()
    same_step_stall_tail: list[dict[str, Any]] = []
    latest_step = finalized_turns[-1].get("step_name") if finalized_turns else None
    if isinstance(latest_step, str) and latest_step:
        for turn in reversed(finalized_turns):
            before_sig = snapshot_signature(turn.get("snapshot_before"))
            after_sig = snapshot_signature(turn.get("snapshot_after"))
            if (
                turn.get("step_name") != latest_step
                or before_sig is None
                or after_sig is None
                or before_sig != after_sig
            ):
                break
            same_step_stall_tail.append(turn)
    step_names = [turn.get("step_name") for turn in unchanged_tail if turn.get("step_name")]
    turn_numbers = [turn.get("turn_number") for turn in unchanged_tail if turn.get("turn_number") is not None]
    alternating_two_step = len(set(step_names)) == 2 if step_names else False
    legacy_reviewer_rejection_count = 0
    for previous, current in zip(finalized_turns, finalized_turns[1:]):
        same_checkpoint = snapshot_signature(previous.get("snapshot_after")) == snapshot_signature(current.get("snapshot_before"))
        previous_role = str(previous.get("step_role", "")).lower()
        current_role = str(current.get("step_role", "")).lower()
        previous_name = str(previous.get("step_name", "")).lower()
        current_name = str(current.get("step_name", "")).lower()
        if same_checkpoint and ("review" in previous_role or "review" in previous_name) and ("implement" in current_role or "implement" in current_name):
            legacy_reviewer_rejection_count += 1
    reviewer_rejection_count = 0
    for turn in finalized_turns:
        role = str(turn.get("step_role", "")).lower()
        name = str(turn.get("step_name", "")).lower()
        before_sig = snapshot_signature(turn.get("snapshot_before"))
        after_sig = snapshot_signature(turn.get("snapshot_after"))
        if (
            ("review" in role or "review" in name)
            and before_sig is not None
            and before_sig == after_sig
        ):
            reviewer_rejection_count += 1
    return {
        "unchanged_snapshot_turns": len(unchanged_tail),
        "same_step_stall_turns": len(same_step_stall_tail),
        "alternating_two_step_tail": alternating_two_step,
        "tail_step_names": step_names,
        "tail_turn_numbers": turn_numbers,
        "tail_start_turn": turn_numbers[0] if turn_numbers else None,
        "tail_end_turn": turn_numbers[-1] if turn_numbers else None,
        "reviewer_rejection_count": reviewer_rejection_count,
        "reviewer_non_convergence": reviewer_rejection_count >= 2,
        "legacy_reviewer_rejection_count": legacy_reviewer_rejection_count,
    }


def analyze_turn(turn: dict[str, Any]) -> dict[str, Any]:
    turn_dir = Path(turn["_turn_dir"])
    stdout_text = read_turn_stream(turn_dir, turn, filename="stdout.txt", inline_key="stdout")
    stderr_text = read_turn_stream(turn_dir, turn, filename="stderr.txt", inline_key="stderr")
    combined_text = "\n".join(part for part in (stdout_text, stderr_text) if part)

    signals = set(extract_text_signals(combined_text))
    if turn.get("status") == "plan-invalid":
        signals.add("plan_invalid")
        if "inconsistent checkpoint state" in str(turn.get("error", "")).lower():
            signals.add("inconsistent_checkpoint_state")
    if turn.get("status") == "retry-scheduled":
        signals.add("retry_scheduled")
        signals.add("inconsistent_checkpoint_state")
    if turn.get("returncode") not in (None, 0):
        signals.add("nonzero_returncode")

    recovery = _normalize_recovery_payload(turn)
    if recovery is not None:
        signals.update(_recovery_signal_names(recovery))

    aflow_stop_messages = extract_aflow_stop(stdout_text) + extract_aflow_stop(stderr_text)
    highlights = summarize_text_lines(combined_text)
    if turn.get("error"):
        highlights = summarize_reason_text(str(turn.get("error"))) + highlights
    if recovery is not None and recovery.get("reason"):
        highlights = summarize_reason_text(str(recovery["reason"])) + highlights

    before_sig = snapshot_signature(turn.get("snapshot_before"))
    after_sig = snapshot_signature(turn.get("snapshot_after"))
    snapshot_changed = before_sig is not None and after_sig is not None and before_sig != after_sig
    snapshot_unchanged = before_sig is not None and before_sig == after_sig

    return {
        "aflow_stop_messages": aflow_stop_messages,
        "artifact_paths": {
            "result_json": str(turn_dir / "result.json"),
            "stderr": str(turn_dir / "stderr.txt"),
            "stdout": str(turn_dir / "stdout.txt"),
        },
        "chosen_transition": turn.get("chosen_transition"),
        "conditions": turn.get("conditions"),
        "error": turn.get("error"),
        "finished_at": turn.get("finished_at"),
        "highlights": highlights,
        "recovery": recovery,
        "returncode": turn.get("returncode"),
        "selector": turn.get("selector"),
        "signals": sorted(signals),
        "snapshot_changed": snapshot_changed,
        "snapshot_unchanged": snapshot_unchanged,
        "started_at": turn.get("started_at"),
        "status": turn.get("status"),
        "step_name": turn.get("step_name"),
        "turn_number": turn.get("turn_number"),
    }


def classify_outcome(run_json: dict[str, Any], signals: set[str], last_turn_status: str | None) -> str:
    if "merge_failure" in signals:
        return "merge_failure"
    if last_turn_status == "plan-invalid":
        return "plan_invalid"
    if last_turn_status == "retry-scheduled":
        return "retry_scheduled"
    if "workflow_failure" in signals:
        return "workflow_failure"
    if "interrupted_or_abandoned" in signals:
        return "interrupted_or_abandoned"
    if "no_progress_tail" in signals:
        return "running_no_progress"
    status = run_json.get("status")
    if isinstance(status, str) and status:
        return status
    return "unknown"


def summarize_focus_turns(
    analyzed_turns: list[dict[str, Any]],
    progress: dict[str, Any],
    *,
    include_progress_context: bool,
) -> list[dict[str, Any]]:
    if not analyzed_turns:
        return []

    focus_numbers: set[int] = set()
    latest_turn_number = analyzed_turns[-1].get("turn_number")
    finalized_turns = [turn for turn in analyzed_turns if turn.get("status") != "starting"]
    last_finalized_number = finalized_turns[-1].get("turn_number") if finalized_turns else None
    for turn in analyzed_turns:
        turn_number = turn.get("turn_number")
        if turn_number is None:
            continue
        if turn["signals"]:
            focus_numbers.add(turn_number)
        if turn["status"] not in ("running", "completed"):
            focus_numbers.add(turn_number)
        if turn["returncode"] not in (None, 0):
            focus_numbers.add(turn_number)
        if turn["chosen_transition"] == "END":
            focus_numbers.add(turn_number)

    for turn_number in (
        latest_turn_number,
        last_finalized_number,
    ):
        if isinstance(turn_number, int):
            focus_numbers.add(turn_number)

    if include_progress_context or latest_turn_number != last_finalized_number:
        progress_turn_numbers = [turn["turn_number"] for turn in analyzed_turns if turn["snapshot_changed"]]
        last_progress_number = progress_turn_numbers[-1] if progress_turn_numbers else None
        for turn_number in (
            last_progress_number,
            progress.get("tail_start_turn"),
            progress.get("tail_end_turn"),
        ):
            if isinstance(turn_number, int):
                focus_numbers.add(turn_number)

    by_number = {turn["turn_number"]: turn for turn in analyzed_turns if isinstance(turn.get("turn_number"), int)}
    focus_turns = [by_number[number] for number in sorted(focus_numbers) if number in by_number]
    return focus_turns


def compact_turn(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "aflow_stop_messages": turn["aflow_stop_messages"],
        "artifact_paths": turn["artifact_paths"],
        "chosen_transition": turn["chosen_transition"],
        "conditions": turn["conditions"],
        "error": turn["error"],
        "finished_at": turn["finished_at"],
        "highlights": turn["highlights"],
        "recovery": turn["recovery"],
        "returncode": turn["returncode"],
        "selector": turn["selector"],
        "signals": turn["signals"],
        "started_at": turn["started_at"],
        "status": turn["status"],
        "step_name": turn["step_name"],
        "turn_number": turn["turn_number"],
    }


def _manager_summary(run_json: dict[str, Any]) -> dict[str, Any]:
    """Return durable manager metadata without exposing prompts or contexts."""
    history = run_json.get("manager_history")
    normalized_history = [
        {
            "decision_number": item.get("decision_number"),
            "level": item.get("level"),
            "trigger": item.get("trigger"),
            "action": item.get("action"),
            "reason": item.get("reason"),
            "artifact_path": item.get("artifact_path"),
        }
        for item in history
        if isinstance(item, dict)
    ] if isinstance(history, list) else []
    return {
        "decision_count": int(run_json.get("manager_decision_number", len(normalized_history)) or 0),
        "history": normalized_history,
        "last_level": normalized_history[-1]["level"] if normalized_history else None,
        "last_trigger": normalized_history[-1]["trigger"] if normalized_history else None,
        "last_action": normalized_history[-1]["action"] if normalized_history else None,
        "pending_notes": run_json.get("pending_manager_notes"),
        "pending_upgrade": run_json.get("pending_step_team_override"),
        "report_path": run_json.get("last_manager_report_path"),
    }


def summarize_run(run_dir: Path, run_json: dict[str, Any], turns: list[dict[str, Any]], base: Path | None) -> dict[str, Any]:
    signals: set[str] = set()
    notes: list[str] = []
    aflow_stop_messages: list[str] = []
    recovery_summary = _normalize_recovery_payload(
        run_json.get("recovery_summary") if isinstance(run_json.get("recovery_summary"), dict) else run_json
    )
    recovery_history_raw = run_json.get("recovery_history")
    recovery_history = [
        normalized
        for item in recovery_history_raw
        if (normalized := _normalize_recovery_payload(item)) is not None
    ] if isinstance(recovery_history_raw, list) else []

    if run_json.get("failure_reason"):
        signals.add("workflow_failure")
        if "original plan file is missing after the turn" in str(run_json["failure_reason"]):
            signals.add("original_plan_missing")

    if run_json.get("merge_failure_reason"):
        signals.add("merge_failure")
        if "dirty" in str(run_json["merge_failure_reason"]).lower():
            signals.add("dirty_merge_verification")

    if "inconsistent checkpoint state" in str(run_json.get("startup_recovery_reason", "")).lower():
        signals.add("inconsistent_checkpoint_state")
    if recovery_summary is not None:
        signals.update(_recovery_signal_names(recovery_summary))
        notes.append(f"recovery {recovery_summary.get('source')}:{recovery_summary.get('action')}")
        if recovery_summary.get("from_team") or recovery_summary.get("to_team"):
            notes.append(
                f"recovery team switch {recovery_summary.get('from_team')} -> {recovery_summary.get('to_team')}"
            )
        if recovery_summary.get("suggested_keywords"):
            notes.append(
                "recovery suggested keywords: "
                + ", ".join(str(keyword) for keyword in recovery_summary.get("suggested_keywords", []))
            )

    last_turn_status = turns[-1].get("status") if turns else None
    if last_turn_status == "starting":
        signals.add("interrupted_or_abandoned")

    progress = analyze_progress_tail(turns)
    if run_json.get("status") == "running" and progress["unchanged_snapshot_turns"] >= 3:
        signals.add("no_progress_tail")
        if progress["alternating_two_step_tail"]:
            signals.add("alternating_review_execute_loop")

    analyzed_turns = [analyze_turn(turn) for turn in turns]
    for turn in analyzed_turns:
        signals.update(turn["signals"])
        aflow_stop_messages.extend(turn["aflow_stop_messages"])
        if turn["signals"]:
            notes.append(f"turn {turn['turn_number']}: {', '.join(turn['signals'])}")

    last_snapshot = run_json.get("last_snapshot")
    if isinstance(last_snapshot, dict) and last_snapshot.get("is_complete") is False and progress["unchanged_snapshot_turns"] >= 3:
        notes.append("latest snapshots show no checkpoint progress across multiple turns")

    status_counts = Counter(turn.get("status") for turn in turns if turn.get("status"))
    step_counts = Counter(turn.get("step_name") for turn in turns if turn.get("step_name"))
    transition_counts = Counter(turn.get("chosen_transition") for turn in turns if turn.get("chosen_transition"))
    signal_turns = [compact_turn(turn) for turn in analyzed_turns if turn["signals"]]
    focus_turns = [compact_turn(turn) for turn in summarize_focus_turns(
        analyzed_turns,
        progress,
        include_progress_context=(
            (run_json.get("status") == "running" and progress["unchanged_snapshot_turns"] >= 3)
            or last_turn_status == "starting"
        ),
    )]
    changed_snapshot_turns = [turn["turn_number"] for turn in analyzed_turns if turn["snapshot_changed"]]
    outcome = classify_outcome(run_json, signals, last_turn_status)

    summary: dict[str, Any] = {
        "activity": {
            "status_counts": dict(sorted(status_counts.items())),
            "step_counts": dict(sorted(step_counts.items())),
            "transition_counts": dict(sorted(transition_counts.items())),
        },
        "aflow_stop_messages": aflow_stop_messages,
        "current_step_name": run_json.get("current_step_name"),
        "failure": {
            "failure_reason": run_json.get("failure_reason"),
            "failure_reason_highlights": summarize_reason_text(run_json.get("failure_reason")),
            "merge_failure_reason": run_json.get("merge_failure_reason"),
            "merge_failure_reason_highlights": summarize_reason_text(run_json.get("merge_failure_reason")),
            "signals": sorted(signals),
            "startup_recovery_reason": run_json.get("startup_recovery_reason"),
        },
        "focus_turns": focus_turns,
        "last_turn_status": last_turn_status,
        "notes": notes,
        "outcome": {
            "kind": outcome,
            "status": run_json.get("status"),
        },
        "paths": {
            "active_plan_path": run_json.get("active_plan_path"),
            "execution_repo_root": run_json.get("execution_repo_root"),
            "new_plan_path": run_json.get("new_plan_path"),
            "original_plan_path": run_json.get("original_plan_path"),
            "repo_root": run_json.get("repo_root"),
            "run_dir": relpath(run_dir, base),
            "run_json": str(run_dir / "run.json"),
            "worktree_path": run_json.get("worktree_path"),
        },
        "progress": {
            "alternating_two_step_tail": progress["alternating_two_step_tail"],
            "changed_snapshot_turn_count": len(changed_snapshot_turns),
            "last_snapshot_change_turn": changed_snapshot_turns[-1] if changed_snapshot_turns else None,
            "tail_end_turn": progress["tail_end_turn"],
            "tail_start_turn": progress["tail_start_turn"],
            "tail_step_names": progress["tail_step_names"],
            "tail_turn_numbers": progress["tail_turn_numbers"],
            "same_step_stall_turns": progress["same_step_stall_turns"],
            "unchanged_snapshot_turns": progress["unchanged_snapshot_turns"],
        },
        "recovery_history": recovery_history,
        "recovery_summary": recovery_summary,
        "manager": _manager_summary(run_json),
        "run_id": run_dir.name,
        "selected_start_step": run_json.get("selected_start_step"),
        "signal_turns": signal_turns,
        "turns_completed": run_json.get("turns_completed"),
        "workflow_name": run_json.get("workflow_name"),
    }
    return summary


def summarize_run_compact(run_dir: Path, run_json: dict[str, Any], turns: list[dict[str, Any]], base: Path | None) -> dict[str, Any]:
    detailed = summarize_run(run_dir, run_json, turns, base)
    return {
        "current_step_name": detailed["current_step_name"],
        "failure": {
            "failure_reason": detailed["failure"]["failure_reason"],
            "merge_failure_reason": detailed["failure"]["merge_failure_reason"],
            "signals": detailed["failure"]["signals"],
        },
        "last_turn_status": detailed["last_turn_status"],
        "outcome": detailed["outcome"],
        "recovery_history": detailed["recovery_history"],
        "recovery_summary": detailed["recovery_summary"],
        "manager": detailed["manager"],
        "paths": {
            "run_dir": detailed["paths"]["run_dir"],
            "run_json": detailed["paths"]["run_json"],
        },
        "progress": {
            "alternating_two_step_tail": detailed["progress"]["alternating_two_step_tail"],
            "same_step_stall_turns": detailed["progress"]["same_step_stall_turns"],
            "tail_start_turn": detailed["progress"]["tail_start_turn"],
            "unchanged_snapshot_turns": detailed["progress"]["unchanged_snapshot_turns"],
        },
        "run_id": detailed["run_id"],
        "turns_completed": detailed["turns_completed"],
        "workflow_name": detailed["workflow_name"],
    }


def analyze_single_run(
    run_dir: Path,
    runs_root: Path | None,
    selection: str,
    include_noise: bool,
    skipped_noise: int = 0,
) -> dict[str, Any]:
    run_json = load_json(run_dir / "run.json")
    turns = load_turns(run_dir)
    base = runs_root.parent.parent if runs_root is not None else None
    payload = {
        "analysis_scope": {
            "include_noise": include_noise,
            "mode": "single_run",
            "run_count_considered": 1,
            "run_count_skipped_as_noise": skipped_noise,
            "runs_root": str(runs_root) if runs_root is not None else None,
            "selection": selection,
        },
        "run": summarize_run(run_dir, run_json, turns, base),
        "version": 2,
    }
    return payload


def analyze_corpus(
    run_dirs: list[Path],
    runs_root: Path,
    selection: str,
    include_noise: bool,
) -> dict[str, Any]:
    analyzed_runs: list[dict[str, Any]] = []
    skipped_noise = 0
    signal_counts: Counter[str] = Counter()
    manager_action_counts: Counter[str] = Counter()
    manager_full_escalations = 0
    manager_upgrade_usage = 0
    manager_stops = 0
    base = runs_root.parent.parent

    for run_dir in run_dirs:
        run_json_path = run_dir / "run.json"
        if not run_json_path.is_file():
            continue
        run_json = load_json(run_json_path)
        if is_noise_run(run_json) and not include_noise:
            skipped_noise += 1
            continue
        turns = load_turns(run_dir)
        summary = summarize_run_compact(run_dir, run_json, turns, base)
        analyzed_runs.append(summary)
        signal_counts.update(summary["failure"]["signals"])
        for decision in summary["manager"]["history"]:
            action = decision.get("action")
            if isinstance(action, str):
                manager_action_counts[action] += 1
                manager_upgrade_usage += action == "upgrade_next_implementation"
                manager_stops += action == "stop"
            manager_full_escalations += decision.get("level") == "full"

    payload = {
        "analysis_scope": {
            "include_noise": include_noise,
            "mode": "corpus",
            "run_count_considered": len(analyzed_runs),
            "run_count_skipped_as_noise": skipped_noise,
            "runs_root": str(runs_root),
            "selection": selection,
        },
        "runs": analyzed_runs,
        "signal_counts": dict(sorted(signal_counts.items())),
        "manager": {
            "action_counts": dict(sorted(manager_action_counts.items())),
            "full_escalations": manager_full_escalations,
            "upgrade_usage": manager_upgrade_usage,
            "stops": manager_stops,
        },
        "version": 2,
    }
    return payload


def resolve_run_id(
    explicit_run_id: str | None,
    repo_root: Path | None,
) -> tuple[Path | None, str | None]:
    """Resolve run_id from explicit argument, shell-scoped state, env, or repo fallback.

    Returns (resolved_run_dir, selection_source) where selection_source is one of:
    - "explicit_run_id": from the explicit RUN_ID argument
    - "shell_last_run_id_file": from .aflow/last_run_ids/<shell-id>
    - "env_var": from AFLOW_LAST_RUN_ID environment variable
    - "last_run_id_file": from .aflow/last_run_id file
    - None: could not resolve a run_id
    """
    return resolve_last_run_id(explicit_run_id, repo_root)

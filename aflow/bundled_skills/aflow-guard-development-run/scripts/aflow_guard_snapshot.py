#!/usr/bin/env python3
"""Emit a bounded, durable snapshot for an explicitly guarded AFlow run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
HELPER_PROVENANCE_SCHEMA_VERSION = 1
MAX_HISTORY = 32
SAFE_TRANSIENT_ENVIRONMENT_KINDS = (
    "missing_reasonix_bubblewrap",
)
HARNESS_TERMS = (
    "reasonix",
    "codex",
    "claude",
    "gemini",
    "deepseek",
    "pytest",
    "go test",
    "npm test",
    "pnpm test",
)
THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    ppid: int
    state: str
    elapsed: str
    command: str
    cwd: str | None = None
    pgid: int | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_file_provenance(path: Path) -> tuple[str | None, bool | None]:
    try:
        root_result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if root_result.returncode != 0:
        return None, None
    root = Path(root_result.stdout.strip())
    try:
        relative = path.resolve().relative_to(root.resolve())
        commit_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        status_result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--",
                str(relative),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, None
    commit = (
        commit_result.stdout.strip()
        if commit_result.returncode == 0 and commit_result.stdout.strip()
        else None
    )
    clean = (
        not status_result.stdout.strip()
        if status_result.returncode == 0
        else None
    )
    return commit, clean


def _helper_provenance() -> dict[str, Any]:
    invoked = Path(sys.argv[0]).expanduser()
    if not invoked.is_absolute():
        invoked = Path.cwd() / invoked
    resolved = Path(__file__).resolve()
    sha256 = _file_sha256(resolved)
    git_commit, git_file_clean = _git_file_provenance(resolved)
    return {
        "schema_version": HELPER_PROVENANCE_SCHEMA_VERSION,
        "invoked_path": str(invoked.absolute()),
        "resolved_path": str(resolved),
        "sha256": sha256,
        "build_id": sha256[:16],
        "git_commit": git_commit,
        "git_file_clean": git_file_clean,
    }


def _provenance_failure_snapshot(
    repo: Path,
    run_id: str,
    provenance: dict[str, Any],
    expected_sha256: str,
    error: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": _utc_now(),
        "repo": str(repo.resolve()),
        "run_id": run_id,
        "classification": "invalid_state",
        "recommended_action": "pause_and_notify",
        "error": error,
        "helper": {
            **provenance,
            "expected_sha256": expected_sha256,
            "matches_expected": False,
        },
        "processes": {
            "inspection_skipped": True,
            "controller_count": None,
            "controller_pids": [],
            "wrapper_pids": [],
            "child_pids": [],
            "harness_pids": [],
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _compact_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    last = run.get("last_snapshot")
    if not isinstance(last, dict):
        last = {}
    scope = run.get("active_implementation_scope")
    if not isinstance(scope, dict):
        scope = {}
    return {
        "status": run.get("status"),
        "end_reason": run.get("end_reason"),
        "status_message": run.get("status_message"),
        "failure_reason_present": bool(run.get("failure_reason")),
        "workflow_name": run.get("workflow_name"),
        "team": run.get("team"),
        "current_step_name": run.get("current_step_name"),
        "turns_completed": run.get("turns_completed"),
        "max_turns": run.get("effective_max_turns", run.get("max_turns")),
        "checkpoint_index": last.get("current_checkpoint_index"),
        "checkpoint_name": last.get("current_checkpoint_name"),
        "checkpoint_unchecked_steps": last.get(
            "current_checkpoint_unchecked_step_count"
        ),
        "unchecked_checkpoints": last.get("unchecked_checkpoint_count"),
        "plan_complete": last.get("is_complete"),
        "scope_id": scope.get("scope_id"),
        "reviewer_rejection_count": run.get("reviewer_rejection_count"),
        "semantic_stall_count": run.get("semantic_stall_count"),
        "manager_decision_number": run.get("manager_decision_number"),
        "active_turn": run.get("active_turn"),
        "resumed_from_run_id": run.get("resumed_from_run_id"),
    }


def _latest_entry(parent: Path, prefix: str) -> dict[str, Any] | None:
    if not parent.is_dir():
        return None
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    candidates: list[tuple[int, Path]] = []
    for child in parent.iterdir():
        match = pattern.match(child.name)
        if child.is_dir() and match:
            candidates.append((int(match.group(1)), child))
    if not candidates:
        return None
    number, path = max(candidates)
    mtimes = [path.stat().st_mtime]
    for name in ("result.json", "boundary.json", "stdout.txt", "stderr.txt"):
        candidate = path / name
        if candidate.exists():
            mtimes.append(candidate.stat().st_mtime)
    return {
        "number": number,
        "path": str(path),
        "mtime_epoch": int(max(mtimes)),
    }


def _list_processes() -> list[ProcessRecord]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,state=,etime=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    records: list[ProcessRecord] = []
    for raw in completed.stdout.splitlines():
        parts = raw.strip().split(None, 5)
        if len(parts) != 6:
            continue
        pid, ppid, pgid, state, elapsed, command = parts
        try:
            records.append(
                ProcessRecord(
                    pid=int(pid),
                    ppid=int(ppid),
                    pgid=int(pgid),
                    state=state,
                    elapsed=elapsed,
                    command=command,
                )
            )
        except ValueError:
            continue
    return records


def _process_cwd(pid: int) -> str | None:
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    if proc_cwd.exists():
        try:
            return str(proc_cwd.resolve())
        except OSError:
            return None
    try:
        completed = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _is_controller_candidate(record: ProcessRecord) -> bool:
    command = record.command
    if "aflow run" not in command:
        return False
    executable = Path(command.split(None, 1)[0]).name
    return executable not in {
        "SCREEN",
        "screen",
        "zsh",
        "bash",
        "sh",
        "login",
    }


def _command_tokens(command: str) -> tuple[str, ...] | None:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return None


def _aflow_invocation(command: str) -> tuple[str, ...] | None:
    tokens = _command_tokens(command)
    if tokens is None:
        return None
    for index, token in enumerate(tokens[:-1]):
        if Path(token).name == "aflow" and tokens[index + 1] == "run":
            return ("aflow", "run", *tokens[index + 2 :])
    return None


def _is_supported_launcher(record: ProcessRecord) -> bool:
    tokens = _command_tokens(record.command)
    if not tokens or len(tokens) < 2:
        return False
    return Path(tokens[0]).name == "uv" and tokens[1] == "run"


def _invocation_option(
    invocation: tuple[str, ...],
    *names: str,
) -> str | None:
    for index, token in enumerate(invocation):
        if token in names:
            if index + 1 < len(invocation):
                value = invocation[index + 1]
                return value if value and not value.startswith("-") else None
            return None
        for name in names:
            prefix = f"{name}="
            if token.startswith(prefix):
                value = token[len(prefix) :]
                return value or None
    return None


def _resolved_invocation_path(value: str, cwd: str | None) -> str | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if cwd is None:
            return None
        candidate = Path(cwd) / candidate
    try:
        return str(candidate.resolve())
    except OSError:
        return None


def _controller_matches_run(record: ProcessRecord, run: dict[str, Any]) -> bool:
    invocation = _aflow_invocation(record.command)
    if invocation is None:
        return False

    expected_plans = {
        str(Path(value).expanduser().resolve())
        for key in ("plan_path", "original_plan_path")
        if isinstance((value := run.get(key)), str) and value
    }
    plan_value = _invocation_option(invocation, "--plan", "-p")
    if plan_value is not None:
        invocation_plan = _resolved_invocation_path(plan_value, record.cwd)
        plan_matches = invocation_plan in expected_plans
    else:
        plan_matches = any(
            _resolved_invocation_path(token, record.cwd) in expected_plans
            for token in invocation[2:]
            if token and not token.startswith("-")
        )
    if not plan_matches:
        return False

    expected_resume_ids: set[str] = set()
    run_dir = run.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        expected_resume_ids.add(Path(run_dir).name)
    resumed_from = run.get("resumed_from_run_id")
    if isinstance(resumed_from, str) and resumed_from:
        expected_resume_ids.add(resumed_from)

    resume_value = _invocation_option(invocation, "--resume")
    if resume_value is not None:
        return resume_value in expected_resume_ids
    return not isinstance(resumed_from, str) or not resumed_from


def _matching_controller_candidates(
    records: Iterable[ProcessRecord],
    repo: Path,
    run: dict[str, Any],
) -> list[ProcessRecord]:
    matches: list[ProcessRecord] = []
    for record in records:
        if not _is_controller_candidate(record):
            continue
        cwd = record.cwd if record.cwd is not None else _process_cwd(record.pid)
        enriched = ProcessRecord(
            pid=record.pid,
            ppid=record.ppid,
            pgid=record.pgid,
            state=record.state,
            elapsed=record.elapsed,
            command=record.command,
            cwd=cwd,
        )
        if _controller_matches_run(enriched, run):
            matches.append(enriched)
    return matches


def _logical_controller_matches(
    candidates: Iterable[ProcessRecord],
    process_records: Iterable[ProcessRecord] | None = None,
) -> tuple[list[ProcessRecord], list[ProcessRecord]]:
    matches = list(candidates)
    all_records = list(process_records) if process_records is not None else matches
    wrapper_pids: set[int] = set()
    for wrapper in matches:
        if not _is_supported_launcher(wrapper):
            continue
        controller_children = [
            record
            for record in all_records
            if record.ppid == wrapper.pid and _is_controller_candidate(record)
        ]
        if len(controller_children) != 1:
            continue
        child = next(
            (
                candidate
                for candidate in matches
                if candidate.pid == controller_children[0].pid
            ),
            None,
        )
        if child is None:
            continue
        if _is_supported_launcher(child):
            continue
        if (
            wrapper.pgid is None
            or child.pgid is None
            or wrapper.pgid != child.pgid
        ):
            continue
        if not wrapper.cwd or wrapper.cwd != child.cwd:
            continue
        wrapper_invocation = _aflow_invocation(wrapper.command)
        child_invocation = _aflow_invocation(child.command)
        if (
            wrapper_invocation is None
            or child_invocation is None
            or wrapper_invocation != child_invocation
        ):
            continue
        wrapper_pids.add(wrapper.pid)
    controllers = [
        candidate for candidate in matches if candidate.pid not in wrapper_pids
    ]
    wrappers = [
        candidate for candidate in matches if candidate.pid in wrapper_pids
    ]
    return controllers, wrappers


def _matching_controllers(
    records: Iterable[ProcessRecord],
    repo: Path,
    run: dict[str, Any],
) -> list[ProcessRecord]:
    process_records = list(records)
    candidates = _matching_controller_candidates(process_records, repo, run)
    controllers, _ = _logical_controller_matches(candidates, process_records)
    return controllers


def _descendants(
    records: Iterable[ProcessRecord], roots: Iterable[int]
) -> list[ProcessRecord]:
    by_parent: dict[int, list[ProcessRecord]] = {}
    for record in records:
        by_parent.setdefault(record.ppid, []).append(record)
    found: list[ProcessRecord] = []
    pending = list(roots)
    seen = set(pending)
    while pending:
        parent = pending.pop()
        for child in by_parent.get(parent, []):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            found.append(child)
            pending.append(child.pid)
    return found


def _screen_present(session: str | None) -> bool | None:
    if not session:
        return None
    completed = subprocess.run(
        ["screen", "-ls"],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = completed.stdout + completed.stderr
    return bool(re.search(rf"\d+\.{re.escape(session)}\b", combined))


def _fingerprint(
    run_id: str,
    run: dict[str, Any],
    latest_turn: dict[str, Any] | None,
    latest_manager: dict[str, Any] | None,
    controller_count: int,
) -> str:
    material = {
        "run_id": run_id,
        "run": _compact_snapshot(run),
        "latest_turn": latest_turn,
        "latest_manager": latest_manager,
        "controller_count": controller_count,
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _classify(
    run: dict[str, Any],
    controller_count: int,
    child_count: int,
    changed: bool,
    transient_environment_kind: str | None = None,
) -> tuple[str, str]:
    status = run.get("status")
    last = run.get("last_snapshot")
    complete = isinstance(last, dict) and last.get("is_complete") is True

    if (
        status == "failed"
        and controller_count == 0
        and transient_environment_kind in SAFE_TRANSIENT_ENVIRONMENT_KINDS
    ):
        return "terminal_transient_environment", "remediate_once_or_pause"
    if controller_count > 1:
        return "unsafe_duplicate_controllers", "pause_and_notify"
    if controller_count == 1 and status != "running":
        return "unsafe_inconsistent", "pause_and_notify"
    if controller_count == 1:
        if changed:
            return "active_progress", "stay_silent"
        if child_count:
            return "active_waiting_child", "stay_silent"
        return "active_waiting", "stay_silent_then_inspect"
    if status == "running":
        return "recoverable_orphan", "attempt_recovery_once"
    if status == "completed" and complete:
        return "terminal_success", "pause_and_notify"
    if status == "completed":
        return "terminal_incomplete", "inspect_recover_or_pause"
    if status == "failed":
        return "terminal_failed", "inspect_recover_or_pause"
    return "invalid_state", "pause_and_notify"


def _default_state_path(repo: Path, run_id: str) -> Path:
    codex_root = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    repo_key = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
    return codex_root / "aflow-guardian" / repo_key / run_id / "state.json"


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _bounded_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))[-MAX_HISTORY:]


def _resolve_routing(
    previous: dict[str, Any],
    report_thread_id: str | None,
    guard_thread_id: str | None,
    current_thread_id: str | None,
    *,
    write_state: bool,
) -> dict[str, str]:
    if bool(report_thread_id) != bool(guard_thread_id):
        raise ValueError(
            "--report-thread-id and --guard-thread-id must be provided together"
        )

    previous_routing = previous.get("routing")
    if not isinstance(previous_routing, dict):
        previous_routing = {}
    persisted = {
        key: value
        for key in ("report_thread_id", "guard_thread_id")
        if isinstance((value := previous_routing.get(key)), str) and value
    }
    if persisted and set(persisted) != {"report_thread_id", "guard_thread_id"}:
        raise ValueError("persisted guardian routing is incomplete")

    supplied: dict[str, str] = {}
    if report_thread_id and guard_thread_id:
        supplied = {
            "report_thread_id": report_thread_id,
            "guard_thread_id": guard_thread_id,
        }

    routing = supplied or persisted
    for key, value in routing.items():
        if not THREAD_ID_PATTERN.fullmatch(value):
            raise ValueError(f"invalid {key}: {value!r}")
    if persisted and supplied and persisted != supplied:
        raise ValueError("guardian routing does not match persisted task IDs")
    if routing and write_state:
        if not current_thread_id:
            raise ValueError("CODEX_THREAD_ID is required for routed guardian writes")
        if current_thread_id != routing["guard_thread_id"]:
            raise ValueError(
                "current task does not match the persisted guardian task"
            )
    return routing


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _replacement_recovery_fingerprint(
    previous: dict[str, Any],
    requested: str | None,
) -> str:
    recovery = {
        value
        for value in previous.get("recovery_fingerprints", [])
        if isinstance(value, str) and value
    }
    notified = {
        value
        for value in previous.get("notified_fingerprints", [])
        if isinstance(value, str) and value
    }
    candidates = sorted(recovery & notified)
    if requested is not None:
        if requested not in candidates:
            raise ValueError(
                "requested replacement recovery fingerprint is not durable recovery evidence"
            )
        return requested
    if len(candidates) != 1:
        raise ValueError(
            "replacement linkage requires exactly one shared recovery and notification fingerprint"
        )
    return candidates[0]


def _replacement_identity_value_is_valid(
    run: dict[str, Any], key: str, value: Any
) -> bool:
    if key in {"workflow_name", "team", "selected_start_step"}:
        return isinstance(value, str) and bool(value)
    if key == "effective_max_turns":
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if key == "extra_instructions":
        return isinstance(value, list) and all(
            isinstance(item, str) for item in value
        )
    if key == "frozen_config":
        return (
            isinstance(value, dict)
            and isinstance(value.get("workflow_name"), str)
            and bool(value["workflow_name"])
            and value["workflow_name"] == run.get("workflow_name")
            and isinstance(value.get("config_path"), str)
            and bool(value["config_path"])
            and isinstance(value.get("config_fingerprint"), str)
            and bool(SHA256_PATTERN.fullmatch(value["config_fingerprint"]))
        )
    raise ValueError(f"unknown replacement identity field: {key}")


def _replacement_identity_errors(
    repo: Path,
    predecessor: dict[str, Any],
    successor: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if (
        predecessor.get("status") != "failed"
        or predecessor.get("turns_completed") != 0
        or predecessor.get("feature_branch") is not None
        or predecessor.get("worktree_path") is not None
        or predecessor.get("resumed_from_run_id")
    ):
        errors.append("predecessor is not an eligible zero-turn in-place replacement")
    if successor.get("status") != "running" or successor.get("resumed_from_run_id"):
        errors.append("successor is not a fresh running replacement")
    if (
        successor.get("feature_branch") is not None
        or successor.get("worktree_path") is not None
    ):
        errors.append("successor changed the in-place lifecycle")
    required_fields = (
        "workflow_name",
        "team",
        "selected_start_step",
        "effective_max_turns",
        "extra_instructions",
        "frozen_config",
    )
    for key in required_fields:
        predecessor_valid = (
            key in predecessor
            and _replacement_identity_value_is_valid(
                predecessor, key, predecessor[key]
            )
        )
        successor_valid = (
            key in successor
            and _replacement_identity_value_is_valid(
                successor, key, successor[key]
            )
        )
        if not predecessor_valid:
            errors.append(f"predecessor {key} is missing or invalid")
        if not successor_valid:
            errors.append(f"successor {key} is missing or invalid")
        if (
            predecessor_valid
            and successor_valid
            and predecessor[key] != successor[key]
        ):
            errors.append(f"successor {key} does not match predecessor")
    predecessor_plan = predecessor.get("original_plan_path")
    successor_plan = successor.get("original_plan_path")
    predecessor_resolved = (
        _resolved_invocation_path(predecessor_plan, str(repo))
        if isinstance(predecessor_plan, str) and predecessor_plan
        else None
    )
    successor_resolved = (
        _resolved_invocation_path(successor_plan, str(repo))
        if isinstance(successor_plan, str) and successor_plan
        else None
    )
    if (
        predecessor_resolved is None
        or successor_resolved is None
        or predecessor_resolved != successor_resolved
    ):
        errors.append("successor original plan does not match predecessor")
    elif not Path(predecessor_resolved).is_file():
        errors.append("predecessor original plan does not exist")
    return errors


def _collect_replacement_linkage(
    *,
    repo: Path,
    predecessor_run_id: str,
    predecessor: dict[str, Any],
    successor_run_id: str,
    state_path: Path,
    previous: dict[str, Any],
    write_state: bool,
    helper: dict[str, Any],
    expected_sha256: str | None,
    report_thread_id: str | None,
    guard_thread_id: str | None,
    current_thread_id: str | None,
    replacement_recovery_fingerprint: str | None,
    process_records: list[ProcessRecord],
) -> dict[str, Any]:
    routing = _resolve_routing(
        previous,
        report_thread_id,
        guard_thread_id,
        current_thread_id,
        write_state=write_state,
    )
    recovery_fingerprint = _replacement_recovery_fingerprint(
        previous, replacement_recovery_fingerprint
    )
    successor_path = (
        repo / ".aflow" / "runs" / successor_run_id / "run.json"
    )
    successor = _read_json(successor_path)
    errors = _replacement_identity_errors(repo, predecessor, successor)
    candidates = _matching_controller_candidates(process_records, repo, successor)
    controllers, wrappers = _logical_controller_matches(candidates, process_records)
    if len(controllers) != 1:
        errors.append("replacement successor does not have exactly one controller")
    existing = previous.get("replacement_successor")
    if isinstance(existing, dict) and existing.get("successor_run_id") != successor_run_id:
        errors.append("a different replacement successor is already recorded")
    if errors:
        raise ValueError("; ".join(errors))

    observed_at = _utc_now()
    linkage: dict[str, Any] = {
        "predecessor_run_id": predecessor_run_id,
        "successor_run_id": successor_run_id,
        "predecessor_fingerprint": recovery_fingerprint,
        "linked_at": observed_at,
        "successor_run_json": str(successor_path),
        "successor_controller_pids": [record.pid for record in controllers],
        "successor_wrapper_pids": [record.pid for record in wrappers],
        "identity_verified": True,
    }
    if isinstance(existing, dict):
        old_fingerprint = existing.get("predecessor_fingerprint")
        if isinstance(old_fingerprint, str) and old_fingerprint != recovery_fingerprint:
            linkage["migrated_from_predecessor_fingerprint"] = old_fingerprint
            linkage["migration_reason"] = "successor activity must not replace recovery lineage"

    state = dict(previous)
    state["schema_version"] = SCHEMA_VERSION
    state["replacement_successor"] = linkage
    if routing:
        state["routing"] = routing
    if write_state:
        _write_state(state_path, state)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repo": str(repo.resolve()),
        "run_id": predecessor_run_id,
        "classification": "replacement_linked",
        "recommended_action": "repin_guard_to_successor",
        "fingerprint": recovery_fingerprint,
        "helper": {
            **helper,
            "expected_sha256": expected_sha256,
            "matches_expected": True if expected_sha256 is not None else None,
        },
        "routing": {
            **routing,
            "current_thread_id": current_thread_id,
            "guard_thread_matches_current": bool(routing)
            and current_thread_id == routing["guard_thread_id"],
        },
        "replacement_successor": linkage,
        "processes": {
            "controller_count": len(controllers),
            "controller_pids": [record.pid for record in controllers],
            "wrapper_pids": [record.pid for record in wrappers],
        },
    }

def collect_snapshot(
    repo: Path,
    run_id: str,
    screen_session: str | None,
    state_path: Path,
    *,
    write_state: bool,
    mark_recovery_attempt: bool,
    mark_notified: bool,
    expected_helper_sha256: str | None = None,
    process_records: list[ProcessRecord] | None = None,
    report_thread_id: str | None = None,
    guard_thread_id: str | None = None,
    current_thread_id: str | None = None,
    transient_environment_kind: str | None = None,
    replacement_successor_run_id: str | None = None,
    replacement_recovery_fingerprint: str | None = None,
) -> dict[str, Any]:
    helper = _helper_provenance()
    if (
        transient_environment_kind is not None
        and transient_environment_kind not in SAFE_TRANSIENT_ENVIRONMENT_KINDS
    ):
        raise ValueError("unknown transient environment failure kind")
    expected_sha256 = (
        expected_helper_sha256.strip().lower()
        if isinstance(expected_helper_sha256, str)
        else None
    )
    if expected_sha256 is not None and not SHA256_PATTERN.fullmatch(
        expected_sha256
    ):
        return _provenance_failure_snapshot(
            repo,
            run_id,
            helper,
            expected_sha256,
            "expected helper SHA-256 must be 64 lowercase hexadecimal characters",
        )
    if expected_sha256 is not None and helper["sha256"] != expected_sha256:
        return _provenance_failure_snapshot(
            repo,
            run_id,
            helper,
            expected_sha256,
            "guardian helper provenance mismatch",
        )

    run_path = repo / ".aflow" / "runs" / run_id / "run.json"
    run = _read_json(run_path)
    records = process_records if process_records is not None else _list_processes()
    previous = _load_state(state_path)
    if replacement_successor_run_id:
        return _collect_replacement_linkage(
            repo=repo,
            predecessor_run_id=run_id,
            predecessor=run,
            successor_run_id=replacement_successor_run_id,
            state_path=state_path,
            previous=previous,
            write_state=write_state,
            helper=helper,
            expected_sha256=expected_sha256,
            report_thread_id=report_thread_id,
            guard_thread_id=guard_thread_id,
            current_thread_id=current_thread_id,
            replacement_recovery_fingerprint=replacement_recovery_fingerprint,
            process_records=records,
        )
    controller_candidates = _matching_controller_candidates(records, repo, run)
    controllers, controller_wrappers = _logical_controller_matches(
        controller_candidates, records
    )
    descendants = _descendants(records, [record.pid for record in controllers])
    harnesses = [
        record
        for record in descendants
        if any(term in record.command.lower() for term in HARNESS_TERMS)
    ]
    run_dir = run_path.parent
    latest_turn = _latest_entry(run_dir / "turns", "turn-")
    latest_manager = _latest_entry(run_dir / "manager", "decision-")
    fingerprint = _fingerprint(
        run_id, run, latest_turn, latest_manager, len(controllers)
    )
    routing = _resolve_routing(
        previous,
        report_thread_id,
        guard_thread_id,
        current_thread_id,
        write_state=write_state,
    )
    changed = previous.get("last_fingerprint") != fingerprint
    unchanged_count = 0 if changed else int(previous.get("unchanged_count", 0)) + 1
    recovery_fingerprints = list(previous.get("recovery_fingerprints", []))
    notified_fingerprints = list(previous.get("notified_fingerprints", []))
    if mark_recovery_attempt:
        recovery_fingerprints.append(fingerprint)
    if mark_notified:
        notified_fingerprints.append(fingerprint)

    classification, recommended_action = _classify(
        run,
        len(controllers),
        len(descendants),
        changed,
        transient_environment_kind=transient_environment_kind,
    )
    latest_mtime = max(
        [
            int(run_path.stat().st_mtime),
            *(
                item["mtime_epoch"]
                for item in (latest_turn, latest_manager)
                if item is not None
            ),
        ]
    )
    observed_at = _utc_now()
    state = {
        "schema_version": SCHEMA_VERSION,
        "last_fingerprint": fingerprint,
        "unchanged_count": unchanged_count,
        "last_observed_at": observed_at,
        "recovery_fingerprints": _bounded_unique(recovery_fingerprints),
        "notified_fingerprints": _bounded_unique(notified_fingerprints),
    }
    if routing:
        state["routing"] = routing
    if write_state:
        _write_state(state_path, state)

    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repo": str(repo.resolve()),
        "run_id": run_id,
        "run_json": str(run_path),
        "state_file": str(state_path),
        "classification": classification,
        "recommended_action": recommended_action,
        "fingerprint": fingerprint,
        "changed_since_previous": changed,
        "unchanged_intervals": unchanged_count,
        "recovery_already_attempted": fingerprint in recovery_fingerprints,
        "notification_already_sent": fingerprint in notified_fingerprints,
        "helper": {
            **helper,
            "expected_sha256": expected_sha256,
            "matches_expected": (
                True if expected_sha256 is not None else None
            ),
        },
        "routing": {
            **routing,
            "current_thread_id": current_thread_id,
            "guard_thread_matches_current": bool(routing)
            and current_thread_id == routing["guard_thread_id"],
        },
        "progress_age_seconds": max(
            0, int(datetime.now(timezone.utc).timestamp()) - latest_mtime
        ),
        "run": _compact_snapshot(run),
        "latest_turn": latest_turn,
        "latest_manager": latest_manager,
        "transient_environment_kind": transient_environment_kind,
        "replacement_successor": state.get("replacement_successor"),
        "processes": {
            "controller_count": len(controllers),
            "controller_pids": [record.pid for record in controllers],
            "controller_cwds": [record.cwd for record in controllers],
            "wrapper_pids": [record.pid for record in controller_wrappers],
            "child_pids": [record.pid for record in descendants],
            "harness_pids": [record.pid for record in harnesses],
            "screen_session": screen_session,
            "screen_present": (
                _screen_present(screen_session)
                if process_records is None
                else None
            ),
        },
    }


def _self_test() -> None:
    running = {"status": "running", "last_snapshot": {"is_complete": False}}
    failed = {"status": "failed", "last_snapshot": {"is_complete": False}}
    completed = {"status": "completed", "last_snapshot": {"is_complete": True}}
    incomplete = {"status": "completed", "last_snapshot": {"is_complete": False}}
    assert _classify(running, 1, 0, True)[0] == "active_progress"
    assert _classify(running, 1, 1, False)[0] == "active_waiting_child"
    assert _classify(running, 1, 0, False)[0] == "active_waiting"
    assert _classify(running, 0, 0, False)[0] == "recoverable_orphan"
    assert _classify(failed, 0, 0, False)[0] == "terminal_failed"
    assert (
        _classify(
            failed,
            0,
            0,
            False,
            transient_environment_kind="missing_reasonix_bubblewrap",
        )[0]
        == "terminal_transient_environment"
    )
    assert _classify(completed, 0, 0, False)[0] == "terminal_success"
    assert _classify(incomplete, 0, 0, False)[0] == "terminal_incomplete"
    assert _classify(failed, 1, 0, False)[0] == "unsafe_inconsistent"
    assert (
        _classify(running, 2, 1, False)[0]
        == "unsafe_duplicate_controllers"
    )
    report_thread_id = "019fb305-3f3a-7f73-9cfb-a58486cba9a6"
    guard_thread_id = "019fb35a-dffd-7243-b6a9-663d1fad952c"
    assert _resolve_routing(
        {},
        report_thread_id,
        guard_thread_id,
        guard_thread_id,
        write_state=True,
    ) == {
        "report_thread_id": report_thread_id,
        "guard_thread_id": guard_thread_id,
    }
    try:
        _resolve_routing(
            {
                "routing": {
                    "report_thread_id": report_thread_id,
                    "guard_thread_id": guard_thread_id,
                }
            },
            None,
            None,
            report_thread_id,
            write_state=True,
        )
    except ValueError as exc:
        assert "current task" in str(exc)
    else:
        raise AssertionError("routing accepted a write from the report task")
    try:
        _resolve_routing(
            {"routing": {"report_thread_id": report_thread_id}},
            None,
            None,
            report_thread_id,
            write_state=True,
        )
    except ValueError as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError("routing accepted an incomplete persisted route")

    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / "repo"
        run_id = "test-run"
        run_dir = repo / ".aflow" / "runs" / run_id
        turn_dir = run_dir / "turns" / "turn-001"
        turn_dir.mkdir(parents=True)
        plan = repo / "plan.md"
        plan.write_text("# test\n", encoding="utf-8")
        run_path = run_dir / "run.json"
        run_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "plan_path": str(plan),
                    "original_plan_path": str(plan),
                    "last_snapshot": {"is_complete": False},
                    "turns_completed": 1,
                }
            ),
            encoding="utf-8",
        )
        (turn_dir / "result.json").write_text("{}\n", encoding="utf-8")
        state_path = Path(temporary) / "state.json"
        fake_controller = ProcessRecord(
            pid=999_999,
            ppid=1,
            state="S",
            elapsed="00:01",
            command=f"/usr/bin/python /tmp/aflow run --plan {plan.resolve()}",
        )
        helper_sha256 = _helper_provenance()["sha256"]

        class UnreadableProcesses(list[ProcessRecord]):
            def __iter__(self):
                raise AssertionError(
                    "process evidence was read before provenance validation"
                )

        stale_state_path = Path(temporary) / "stale-state.json"
        stale = collect_snapshot(
            repo,
            run_id,
            None,
            stale_state_path,
            write_state=True,
            mark_recovery_attempt=False,
            mark_notified=False,
            expected_helper_sha256="0" * 64,
            process_records=UnreadableProcesses(),
        )
        assert stale["classification"] == "invalid_state"
        assert stale["recommended_action"] == "pause_and_notify"
        assert stale["processes"]["inspection_skipped"] is True
        assert stale["helper"]["matches_expected"] is False
        assert not stale_state_path.exists()

        first = collect_snapshot(
            repo,
            run_id,
            None,
            state_path,
            write_state=True,
            mark_recovery_attempt=False,
            mark_notified=False,
            expected_helper_sha256=helper_sha256,
            process_records=[fake_controller],
        )
        second = collect_snapshot(
            repo,
            run_id,
            None,
            state_path,
            write_state=True,
            mark_recovery_attempt=False,
            mark_notified=False,
            process_records=[fake_controller],
        )
        marked = collect_snapshot(
            repo,
            run_id,
            None,
            state_path,
            write_state=True,
            mark_recovery_attempt=True,
            mark_notified=True,
            process_records=[fake_controller],
        )
        routed = collect_snapshot(
            repo,
            run_id,
            None,
            state_path,
            write_state=True,
            mark_recovery_attempt=False,
            mark_notified=False,
            process_records=[fake_controller],
            report_thread_id=report_thread_id,
            guard_thread_id=guard_thread_id,
            current_thread_id=guard_thread_id,
        )
        assert first["classification"] == "active_progress"
        assert first["helper"]["sha256"] == helper_sha256
        assert first["helper"]["matches_expected"] is True
        assert second["classification"] == "active_waiting"
        assert second["unchanged_intervals"] == 1
        assert marked["recovery_already_attempted"] is True
        assert marked["notification_already_sent"] is True
        assert routed["routing"]["guard_thread_matches_current"] is True

        fixture_run = {
            "plan_path": str(plan),
            "original_plan_path": str(plan),
            "run_dir": str(run_dir),
        }
        fixture_cwd = str(repo.resolve())

        def process(
            pid: int,
            ppid: int,
            pgid: int,
            command: str,
            *,
            cwd: str = fixture_cwd,
        ) -> ProcessRecord:
            return ProcessRecord(
                pid=pid,
                ppid=ppid,
                pgid=pgid,
                state="S",
                elapsed="00:01",
                command=command,
                cwd=cwd,
            )

        invocation = f"aflow run --plan {plan.resolve()}"
        direct = process(100, 1, 100, f"/usr/bin/python /tmp/{invocation}")
        wrapper = process(
            200,
            1,
            200,
            f"/opt/homebrew/bin/uv run --project /tmp/aflow {invocation}",
        )
        wrapped = process(
            201,
            200,
            200,
            f"/tmp/.venv/bin/python /tmp/.venv/bin/{invocation}",
        )
        assert len(_matching_controllers([direct], repo, fixture_run)) == 1
        assert len(_matching_controllers([wrapper, wrapped], repo, fixture_run)) == 1
        wrapped_snapshot = collect_snapshot(
            repo,
            run_id,
            None,
            Path(temporary) / "wrapped-state.json",
            write_state=False,
            mark_recovery_attempt=False,
            mark_notified=False,
            process_records=[wrapper, wrapped],
        )
        assert wrapped_snapshot["processes"]["controller_count"] == 1
        assert wrapped_snapshot["processes"]["controller_pids"] == [201]
        assert wrapped_snapshot["processes"]["wrapper_pids"] == [200]

        independent = process(
            300,
            1,
            300,
            f"/tmp/.venv/bin/python /tmp/.venv/bin/{invocation}",
        )
        assert (
            len(
                _matching_controllers(
                    [direct, independent], repo, fixture_run
                )
            )
            == 2
        )

        sibling = process(
            202,
            200,
            200,
            f"/tmp/.venv/bin/python /tmp/.venv/bin/{invocation}",
        )
        assert (
            len(
                _matching_controllers(
                    [wrapper, wrapped, sibling], repo, fixture_run
                )
            )
            > 1
        )

        wrong_group = process(
            203,
            200,
            203,
            f"/tmp/.venv/bin/python /tmp/.venv/bin/{invocation}",
        )
        assert (
            len(
                _matching_controllers(
                    [wrapper, wrong_group], repo, fixture_run
                )
            )
            == 2
        )

        other_plan = repo / "other-plan.md"
        mismatched = process(
            204,
            200,
            200,
            f"/tmp/.venv/bin/python /tmp/.venv/bin/aflow run "
            f"--plan {other_plan.resolve()}",
        )
        assert (
            len(
                _matching_controllers(
                    [wrapper, mismatched], repo, fixture_run
                )
            )
            == 1
        )

        other_repo = Path(temporary) / "other-repo"
        other_repo.mkdir()
        other_repo_plan = other_repo / "plan.md"
        other_repo_plan.write_text("# other\n", encoding="utf-8")
        other_controller = process(
            400,
            1,
            400,
            f"/usr/bin/python /tmp/aflow run "
            f"--plan {other_repo_plan.resolve()}",
            cwd=str(other_repo.resolve()),
        )
        assert (
            len(
                _matching_controllers(
                    [direct, other_controller], repo, fixture_run
                )
            )
            == 1
        )
        other_repo_child = process(
            401,
            200,
            200,
            f"/usr/bin/python /tmp/aflow run "
            f"--plan {other_repo_plan.resolve()}",
            cwd=str(other_repo.resolve()),
        )
        assert (
            len(
                _matching_controllers(
                    [wrapper, wrapped, other_repo_child],
                    repo,
                    fixture_run,
                )
            )
            > 1
        )

        resumed_run = {
            **fixture_run,
            "resumed_from_run_id": "source-run",
        }
        resumed_invocation = (
            f"aflow run --plan {plan.resolve()} --resume source-run"
        )
        resumed_wrapper = process(
            500,
            1,
            500,
            f"/usr/bin/uv run {resumed_invocation}",
        )
        resumed_child = process(
            501,
            500,
            500,
            f"/tmp/.venv/bin/python /tmp/.venv/bin/{resumed_invocation}",
        )
        unrelated_invocation = (
            f"aflow run --plan {other_plan.resolve()} --resume other-run"
        )
        unrelated_wrapper = process(
            600,
            1,
            600,
            f"/usr/bin/uv run {unrelated_invocation}",
        )
        unrelated_child = process(
            601,
            600,
            600,
            f"/tmp/.venv/bin/python /tmp/.venv/bin/{unrelated_invocation}",
        )
        assert len(
            _matching_controllers(
                [resumed_wrapper, resumed_child], repo, resumed_run
            )
        ) == 1
        assert len(
            _matching_controllers(
                [unrelated_wrapper, unrelated_child], repo, resumed_run
            )
        ) == 0
        assert len(
            _matching_controllers(
                [
                    resumed_wrapper,
                    resumed_child,
                    unrelated_wrapper,
                    unrelated_child,
                ],
                repo,
                resumed_run,
            )
        ) == 1

        run_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "plan_path": str(plan),
                    "original_plan_path": str(plan),
                    "last_snapshot": {"is_complete": True},
                    "turns_completed": 1,
                }
            ),
            encoding="utf-8",
        )
        finished = collect_snapshot(
            repo,
            run_id,
            None,
            state_path,
            write_state=False,
            mark_recovery_attempt=False,
            mark_notified=False,
            process_records=[],
        )
        assert finished["classification"] == "terminal_success"
    print("self-test passed")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--screen-session")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--no-write-state", action="store_true")
    parser.add_argument("--mark-recovery-attempt", action="store_true")
    parser.add_argument("--mark-notified", action="store_true")
    parser.add_argument("--report-thread-id")
    parser.add_argument("--guard-thread-id")
    parser.add_argument("--expected-helper-sha256")
    parser.add_argument(
        "--transient-environment-kind",
        choices=SAFE_TRANSIENT_ENVIRONMENT_KINDS,
        help="A bounded failure kind proven from the allowed log tail.",
    )
    parser.add_argument(
        "--replacement-successor-run-id",
        help="Link a verified replacement successor without classifying it as a predecessor controller.",
    )
    parser.add_argument(
        "--replacement-recovery-fingerprint",
        help="The durable original recovery fingerprint; required when prior evidence is ambiguous.",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.self_test:
        _self_test()
        return 0
    if args.repo is None or not args.run_id:
        print("--repo and --run-id are required", file=sys.stderr)
        return 2
    repo = args.repo.expanduser().resolve()
    if args.expected_helper_sha256 is None:
        snapshot = _provenance_failure_snapshot(
            repo,
            args.run_id,
            _helper_provenance(),
            "",
            "--expected-helper-sha256 is required",
        )
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 2
    state_path = (
        args.state_file.expanduser().resolve()
        if args.state_file
        else _default_state_path(repo, args.run_id)
    )
    try:
        snapshot = collect_snapshot(
            repo,
            args.run_id,
            args.screen_session,
            state_path,
            write_state=not args.no_write_state,
            mark_recovery_attempt=args.mark_recovery_attempt,
            mark_notified=args.mark_notified,
            expected_helper_sha256=args.expected_helper_sha256,
            report_thread_id=args.report_thread_id,
            guard_thread_id=args.guard_thread_id,
            current_thread_id=os.environ.get("CODEX_THREAD_ID"),
            transient_environment_kind=args.transient_environment_kind,
            replacement_successor_run_id=args.replacement_successor_run_id,
            replacement_recovery_fingerprint=args.replacement_recovery_fingerprint,
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "classification": "invalid_state",
                    "recommended_action": "pause_and_notify",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    helper = snapshot.get("helper")
    if isinstance(helper, dict) and helper.get("matches_expected") is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

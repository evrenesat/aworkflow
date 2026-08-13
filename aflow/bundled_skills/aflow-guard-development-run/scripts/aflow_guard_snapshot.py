#!/usr/bin/env python3
"""Emit a compact read-only observation for one pinned AFlow run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3
MAX_HISTORY = 32
THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    ppid: int
    pgid: int
    state: str
    elapsed: str
    command: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _compact_run(run: dict[str, Any]) -> dict[str, Any]:
    last = run.get("last_snapshot")
    if not isinstance(last, dict):
        last = {}
    plan_value = run.get("plan_path") or run.get("original_plan_path")
    plan_name = Path(plan_value).name if isinstance(plan_value, str) else None
    return {
        "status": run.get("status"),
        "turns_completed": run.get("turns_completed"),
        "current_step": run.get("current_step") or last.get("current_step"),
        "is_complete": last.get("is_complete"),
        "plan_name": plan_name,
        "workflow_name": run.get("workflow_name"),
        "team": run.get("team"),
    }


def _compact_result(path: Path) -> dict[str, Any] | None:
    try:
        value = _read_json(path)
        stat = path.stat()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return {
        "name": path.parent.name,
        "mtime_epoch": int(stat.st_mtime),
        "status": value.get("status"),
        "verdict": value.get("verdict"),
        "role": value.get("role"),
        "step": value.get("step") or value.get("step_name"),
    }


def _latest_result(run_dir: Path) -> dict[str, Any] | None:
    candidates: list[tuple[float, Path]] = []
    try:
        for path in run_dir.glob("**/result.json"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return None
    if not candidates:
        return None
    return _compact_result(max(candidates, key=lambda item: item[0])[1])


def _list_processes() -> list[ProcessRecord]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,state=,etime=,command="],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise RuntimeError("unable to inspect process table")
    records: list[ProcessRecord] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) != 6:
            continue
        try:
            records.append(
                ProcessRecord(
                    pid=int(fields[0]),
                    ppid=int(fields[1]),
                    pgid=int(fields[2]),
                    state=fields[3],
                    elapsed=fields[4],
                    command=fields[5],
                )
            )
        except ValueError:
            continue
    return records


def _is_uv_wrapper(record: ProcessRecord) -> bool:
    first = record.command.strip().split(None, 1)[0]
    return Path(first).name == "uv"


def _matching_controllers(
    records: Iterable[ProcessRecord],
    run_id: str,
    plan_path: str | None,
) -> tuple[list[ProcessRecord], list[ProcessRecord]]:
    matches: list[ProcessRecord] = []
    for record in records:
        command = record.command
        if not re.search(r"(?:^|[/\s])aflow(?:\s|$).*?\brun\b", command):
            continue
        if run_id not in command and not (
            isinstance(plan_path, str) and plan_path and plan_path in command
        ):
            continue
        matches.append(record)

    grouped: dict[int, list[ProcessRecord]] = {}
    for record in matches:
        grouped.setdefault(record.pgid or record.pid, []).append(record)

    controllers: list[ProcessRecord] = []
    wrappers: list[ProcessRecord] = []
    for group in grouped.values():
        non_wrappers = [record for record in group if not _is_uv_wrapper(record)]
        selected = sorted(non_wrappers or group, key=lambda record: record.pid)[0]
        controllers.append(selected)
        wrappers.extend(record for record in group if _is_uv_wrapper(record))
    return sorted(controllers, key=lambda record: record.pid), sorted(
        wrappers, key=lambda record: record.pid
    )


def _descendants(
    records: Iterable[ProcessRecord], roots: Iterable[int]
) -> list[ProcessRecord]:
    by_parent: dict[int, list[ProcessRecord]] = {}
    for record in records:
        by_parent.setdefault(record.ppid, []).append(record)
    pending = list(roots)
    seen = set(pending)
    found: list[ProcessRecord] = []
    while pending:
        parent = pending.pop()
        for child in by_parent.get(parent, []):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            found.append(child)
            pending.append(child.pid)
    return found


def _tmux_present(session: str | None) -> bool | None:
    if not session:
        return None
    try:
        completed = subprocess.run(
            ["tmux", "has-session", "-t", f"={session}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _classify(
    run: dict[str, Any],
    controller_count: int,
    child_count: int,
    changed: bool,
) -> tuple[str, str]:
    status = run.get("status")
    last = run.get("last_snapshot")
    complete = isinstance(last, dict) and last.get("is_complete") is True

    if controller_count > 1:
        return "unsafe_duplicate_controllers", "report_and_pause"
    if controller_count == 1 and status != "running":
        return "unsafe_inconsistent", "report_and_pause"
    if controller_count == 1:
        if changed:
            return "active_progress", "stay_silent"
        if child_count:
            return "active_waiting_child", "stay_silent"
        return "active_waiting", "stay_silent"
    if status == "running":
        return "orphaned_controller", "report_and_pause"
    if status == "completed" and complete:
        return "terminal_success", "audit_and_pause"
    if status == "completed":
        return "terminal_incomplete", "report_and_pause"
    if status == "failed":
        return "terminal_failed", "report_and_pause"
    return "invalid_state", "report_and_pause"


def _fingerprint(
    run_id: str,
    run: dict[str, Any],
    latest_result: dict[str, Any] | None,
    controller_count: int,
) -> str:
    material = {
        "run_id": run_id,
        "run": _compact_run(run),
        "latest_result": latest_result,
        "controller_count": controller_count,
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _default_state_path(repo: Path, run_id: str) -> Path:
    codex_root = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    repo_key = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
    return codex_root / "aflow-guardian" / repo_key / run_id / "observer-state.json"


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if value.get("schema_version") == SCHEMA_VERSION else {}


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".observer-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _resolve_thread(
    previous: dict[str, Any],
    supplied: str | None,
    current: str | None,
    write_state: bool,
) -> str | None:
    persisted = previous.get("thread_id")
    thread_id = supplied or (persisted if isinstance(persisted, str) else None)
    if thread_id is None:
        return None
    if not THREAD_ID_PATTERN.fullmatch(thread_id):
        raise ValueError("thread ID must be a lowercase UUID")
    if isinstance(persisted, str) and supplied and persisted != supplied:
        raise ValueError("observer task does not match persisted task")
    if write_state and current != thread_id:
        raise ValueError("current task does not match observer task")
    return thread_id


def collect_snapshot(
    repo: Path,
    run_id: str,
    tmux_session: str | None,
    state_path: Path,
    *,
    write_state: bool,
    mark_notified: bool,
    process_records: list[ProcessRecord] | None = None,
    thread_id: str | None = None,
    current_thread_id: str | None = None,
) -> dict[str, Any]:
    run_path = repo / ".aflow" / "runs" / run_id / "run.json"
    run = _read_json(run_path)
    previous = _load_state(state_path)
    resolved_thread = _resolve_thread(
        previous, thread_id, current_thread_id, write_state
    )
    records = process_records if process_records is not None else _list_processes()
    plan_path = run.get("original_plan_path") or run.get("plan_path")
    controllers, wrappers = _matching_controllers(
        records,
        run_id,
        plan_path if isinstance(plan_path, str) else None,
    )
    descendants = _descendants(records, [record.pid for record in controllers])
    latest_result = _latest_result(run_path.parent)
    fingerprint = _fingerprint(run_id, run, latest_result, len(controllers))
    changed = previous.get("last_fingerprint") != fingerprint
    unchanged = 0 if changed else int(previous.get("unchanged_intervals", 0)) + 1
    notified = [
        item
        for item in previous.get("notified_fingerprints", [])
        if isinstance(item, str)
    ]
    if mark_notified and fingerprint not in notified:
        notified.append(fingerprint)
    notified = notified[-MAX_HISTORY:]
    classification, action = _classify(
        run, len(controllers), len(descendants), changed
    )
    observed_at = _utc_now()
    state = {
        "schema_version": SCHEMA_VERSION,
        "thread_id": resolved_thread,
        "last_fingerprint": fingerprint,
        "unchanged_intervals": unchanged,
        "notified_fingerprints": notified,
        "last_observed_at": observed_at,
    }
    if write_state:
        _write_state(state_path, state)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repo": str(repo.resolve()),
        "run_id": run_id,
        "state_file": str(state_path),
        "classification": classification,
        "recommended_action": action,
        "fingerprint": fingerprint,
        "changed_since_previous": changed,
        "unchanged_intervals": unchanged,
        "notification_already_sent": fingerprint in notified,
        "thread_id": resolved_thread,
        "run": _compact_run(run),
        "latest_result": latest_result,
        "processes": {
            "controller_count": len(controllers),
            "controller_pids": [record.pid for record in controllers],
            "wrapper_pids": [record.pid for record in wrappers],
            "child_pids": [record.pid for record in descendants],
            "tmux_session": tmux_session,
            "tmux_present": (
                _tmux_present(tmux_session)
                if process_records is None
                else None
            ),
        },
    }


def _self_test() -> None:
    running = {"status": "running", "last_snapshot": {"is_complete": False}}
    failed = {"status": "failed", "last_snapshot": {"is_complete": False}}
    complete = {"status": "completed", "last_snapshot": {"is_complete": True}}
    assert _classify(running, 1, 0, True)[0] == "active_progress"
    assert _classify(running, 1, 1, False)[0] == "active_waiting_child"
    assert _classify(running, 0, 0, False)[0] == "orphaned_controller"
    assert _classify(failed, 0, 0, False)[1] == "report_and_pause"
    assert _classify(complete, 0, 0, False)[1] == "audit_and_pause"
    print("ok")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--tmux-session")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--thread-id")
    parser.add_argument("--mark-notified", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.self_test:
        _self_test()
        return 0
    if args.repo is None or not args.run_id:
        raise SystemExit("--repo and --run-id are required")
    repo = args.repo.expanduser().resolve()
    state_path = args.state_file or _default_state_path(repo, args.run_id)
    try:
        result = collect_snapshot(
            repo,
            args.run_id,
            args.tmux_session,
            state_path,
            write_state=not args.no_write,
            mark_notified=args.mark_notified,
            thread_id=args.thread_id,
            current_thread_id=os.environ.get("CODEX_THREAD_ID"),
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "observed_at": _utc_now(),
            "repo": str(repo),
            "run_id": args.run_id,
            "classification": "invalid_state",
            "recommended_action": "report_and_pause",
            "error": str(exc),
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

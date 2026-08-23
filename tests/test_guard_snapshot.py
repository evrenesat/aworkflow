from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "aflow"
    / "bundled_skills"
    / "aflow-guard-development-run"
    / "scripts"
    / "aflow_guard_snapshot.py"
)


def _helper_module():
    spec = importlib.util.spec_from_file_location("guard_snapshot", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_run(
    repo: Path,
    run_id: str,
    *,
    status: str,
    complete: bool,
    resumed_from_run_id: str | None = None,
) -> Path:
    run_dir = repo / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    plan = repo / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    payload = {
        "status": status,
        "turns_completed": 1,
        "plan_path": str(plan),
        "original_plan_path": str(plan),
        "workflow_name": "workflow",
        "team": "team",
        "last_snapshot": {"is_complete": complete},
    }
    if resumed_from_run_id is not None:
        payload["resumed_from_run_id"] = resumed_from_run_id
    (run_dir / "run.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return plan


def _process(helper, pid: int, ppid: int, pgid: int, command: str):
    return helper.ProcessRecord(
        pid=pid,
        ppid=ppid,
        pgid=pgid,
        state="S",
        elapsed="00:01",
        command=command,
    )


def test_healthy_progress_waiting_and_child_are_silent(tmp_path: Path) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "20260811T120000Z-abcd1234"
    plan = _write_run(repo, run_id, status="running", complete=False)
    state = tmp_path / "state.json"
    controller = _process(
        helper, 100, 1, 100, f"/usr/bin/aflow run --plan {plan}"
    )

    first = helper.collect_snapshot(
        repo,
        run_id,
        "session",
        state,
        write_state=True,
        mark_notified=False,
        process_records=[controller],
    )
    second = helper.collect_snapshot(
        repo,
        run_id,
        "session",
        state,
        write_state=True,
        mark_notified=False,
        process_records=[controller],
    )
    child = _process(helper, 101, 100, 100, "codex exec task")
    third = helper.collect_snapshot(
        repo,
        run_id,
        "session",
        state,
        write_state=True,
        mark_notified=False,
        process_records=[controller, child],
    )

    assert first["classification"] == "active_progress"
    assert first["recommended_action"] == "stay_silent"
    assert first["processes"]["controller_count"] == 1
    assert second["classification"] == "active_waiting"
    assert second["unchanged_intervals"] == 1
    assert third["classification"] == "active_waiting_child"
    assert third["recommended_action"] == "stay_silent"


@pytest.mark.parametrize("resume_option", ["--resume source-id", "--resume=source-id"])
def test_resumed_controller_matches_exact_durable_source_identity(
    tmp_path: Path, resume_option: str
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "continuation-id"
    source_id = "source-id"
    _write_run(
        repo,
        run_id,
        status="running",
        complete=False,
        resumed_from_run_id=source_id,
    )
    controller = _process(helper, 100, 1, 100, f"/usr/bin/aflow run {resume_option}")
    state = tmp_path / "state.json"

    first = helper.collect_snapshot(
        repo,
        run_id,
        None,
        state,
        write_state=True,
        mark_notified=False,
        process_records=[controller],
    )
    second = helper.collect_snapshot(
        repo,
        run_id,
        None,
        state,
        write_state=True,
        mark_notified=False,
        process_records=[controller],
    )

    assert first["processes"]["controller_count"] == 1
    assert first["classification"] == "active_progress"
    assert first["run"]["resumed_from_run_id"] == source_id
    assert second["classification"] == "active_waiting"
    assert second["changed_since_previous"] is False


def test_resumed_uv_wrapper_keeps_one_controller_and_selected_descendants(
    tmp_path: Path,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "continuation-id"
    source_id = "source-id"
    _write_run(
        repo,
        run_id,
        status="running",
        complete=False,
        resumed_from_run_id=source_id,
    )
    wrapper = _process(
        helper,
        100,
        1,
        100,
        "/usr/bin/uv run aflow run --resume source-id",
    )
    controller = _process(
        helper,
        101,
        1,
        100,
        "/usr/bin/aflow run --resume source-id",
    )
    child = _process(helper, 102, 101, 101, "codex exec task")
    state = tmp_path / "state.json"

    helper.collect_snapshot(
        repo,
        run_id,
        None,
        state,
        write_state=True,
        mark_notified=False,
        process_records=[wrapper, controller],
    )
    second = helper.collect_snapshot(
        repo,
        run_id,
        None,
        state,
        write_state=True,
        mark_notified=False,
        process_records=[wrapper, controller, child],
    )

    assert second["processes"]["controller_pids"] == [controller.pid]
    assert second["processes"]["wrapper_pids"] == [wrapper.pid]
    assert second["processes"]["child_pids"] == [child.pid]
    assert second["classification"] == "active_waiting_child"
    assert second["changed_since_previous"] is False


def test_resumed_independent_controllers_remain_unsafe_duplicates(
    tmp_path: Path,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "continuation-id"
    _write_run(
        repo,
        run_id,
        status="running",
        complete=False,
        resumed_from_run_id="source-id",
    )
    processes = [
        _process(helper, 100, 1, 100, "/usr/bin/aflow run --resume source-id"),
        _process(helper, 200, 1, 200, "/usr/bin/aflow run --resume source-id"),
    ]

    result = helper.collect_snapshot(
        repo,
        run_id,
        None,
        tmp_path / "state.json",
        write_state=False,
        mark_notified=False,
        process_records=processes,
    )

    assert result["processes"]["controller_count"] == 2
    assert result["classification"] == "unsafe_duplicate_controllers"


@pytest.mark.parametrize(
    "command_args",
    [
        "--resume other-source-id",
        "--resume source-id-suffix",
        "--resume=source-id-suffix",
        "--note source-id",
        "--note=source-id",
        "-- --resume source-id",
        "--resume",
        "--resume-reset-scope source-id",
        "--resume source-id --resume source-id",
        "--resume source-id --resume other-source-id",
        "--note 'unterminated",
        "--note continuation-id",
    ],
)
def test_resumed_controller_rejects_colliding_or_malformed_commands(
    tmp_path: Path, command_args: str
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "continuation-id"
    _write_run(
        repo,
        run_id,
        status="running",
        complete=False,
        resumed_from_run_id="source-id",
    )
    controller = _process(helper, 100, 1, 100, f"/usr/bin/aflow run {command_args}")

    result = helper.collect_snapshot(
        repo,
        run_id,
        None,
        tmp_path / "state.json",
        write_state=False,
        mark_notified=False,
        process_records=[controller],
    )

    assert result["processes"]["controller_count"] == 0
    assert result["classification"] == "orphaned_controller"


def test_plan_match_remains_independent_of_unrelated_resume_identity(
    tmp_path: Path,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "continuation-id"
    plan = _write_run(
        repo,
        run_id,
        status="running",
        complete=False,
        resumed_from_run_id="source-id",
    )
    controller = _process(
        helper,
        100,
        1,
        100,
        f"/usr/bin/aflow run --resume other-source-id --plan {plan}",
    )

    result = helper.collect_snapshot(
        repo,
        run_id,
        None,
        tmp_path / "state.json",
        write_state=False,
        mark_notified=False,
        process_records=[controller],
    )

    # Plan-path evidence remains an independent legacy ownership path.
    assert result["processes"]["controller_count"] == 1
    assert result["classification"] == "active_progress"


@pytest.mark.parametrize(
    ("status", "complete", "process_count", "classification", "action"),
    [
        ("running", False, 0, "orphaned_controller", "report_and_pause"),
        ("failed", False, 0, "terminal_failed", "report_and_pause"),
        ("completed", False, 0, "terminal_incomplete", "report_and_pause"),
        ("completed", True, 0, "terminal_success", "audit_and_pause"),
    ],
)
def test_terminal_and_orphan_states_never_recover(
    tmp_path: Path,
    status: str,
    complete: bool,
    process_count: int,
    classification: str,
    action: str,
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "20260811T120000Z-abcd1234"
    _write_run(repo, run_id, status=status, complete=complete)

    result = helper.collect_snapshot(
        repo,
        run_id,
        "session",
        tmp_path / "state.json",
        write_state=False,
        mark_notified=False,
        process_records=[],
    )

    assert process_count == 0
    assert result["classification"] == classification
    assert result["recommended_action"] == action
    assert "recovery" not in result


def test_duplicate_controller_is_unsafe(tmp_path: Path) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "20260811T120000Z-abcd1234"
    plan = _write_run(repo, run_id, status="running", complete=False)
    processes = [
        _process(helper, 100, 1, 100, f"/usr/bin/aflow run --plan {plan}"),
        _process(helper, 200, 1, 200, f"/usr/bin/aflow run --plan {plan}"),
    ]

    result = helper.collect_snapshot(
        repo,
        run_id,
        None,
        tmp_path / "state.json",
        write_state=False,
        mark_notified=False,
        process_records=processes,
    )

    assert result["classification"] == "unsafe_duplicate_controllers"
    assert result["recommended_action"] == "report_and_pause"


def test_tmux_presence_never_substitutes_for_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "20260811T120000Z-abcd1234"
    _write_run(repo, run_id, status="running", complete=False)
    monkeypatch.setattr(helper, "_list_processes", lambda: [])
    monkeypatch.setattr(helper, "_tmux_present", lambda _: True)

    result = helper.collect_snapshot(
        repo,
        run_id,
        "session",
        tmp_path / "state.json",
        write_state=False,
        mark_notified=False,
    )

    assert result["processes"]["tmux_present"] is True
    assert result["classification"] == "orphaned_controller"


def test_same_task_routing_and_notification_deduplication(tmp_path: Path) -> None:
    helper = _helper_module()
    repo = tmp_path / "repo"
    run_id = "20260811T120000Z-abcd1234"
    plan = _write_run(repo, run_id, status="running", complete=False)
    thread_id = "019ff1a9-a23f-7c33-9e4f-d2f14270fd9d"
    controller = _process(
        helper, 100, 1, 100, f"/usr/bin/aflow run --plan {plan}"
    )
    state = tmp_path / "state.json"

    result = helper.collect_snapshot(
        repo,
        run_id,
        None,
        state,
        write_state=True,
        mark_notified=True,
        process_records=[controller],
        thread_id=thread_id,
        current_thread_id=thread_id,
    )
    repeated = helper.collect_snapshot(
        repo,
        run_id,
        None,
        state,
        write_state=True,
        mark_notified=False,
        process_records=[controller],
        thread_id=thread_id,
        current_thread_id=thread_id,
    )

    assert result["notification_already_sent"] is True
    assert repeated["notification_already_sent"] is True
    with pytest.raises(ValueError, match="current task"):
        helper.collect_snapshot(
            repo,
            run_id,
            None,
            state,
            write_state=True,
            mark_notified=False,
            process_records=[controller],
            thread_id=thread_id,
            current_thread_id="019fa876-6d0b-7c42-b5d9-a0f0467a204a",
        )


def test_cli_exposes_tmux_and_no_recovery_options() -> None:
    helper = _helper_module()
    help_text = helper._parser().format_help()

    assert "--tmux-session" in help_text
    assert "--screen-session" not in help_text
    assert "--mark-recovery-attempt" not in help_text
    assert "--replacement-successor-run-id" not in help_text

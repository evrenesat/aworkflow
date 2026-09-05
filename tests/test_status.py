from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path

from aflow.config import (
    GoTransition,
    TeamConfig,
    WorkflowConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.hotplug import HotplugTransactionV1, HarnessSessionRefV1, hotplug_transaction_id
from aflow.plan import PlanSnapshot
from aflow.run_state import (
    CheckpointRepartitionRecord,
    ControllerState,
    FrozenRunIdentity,
    ManagerDecisionSummary,
    OverrideResult,
    PendingManagerNotes,
    PendingRepartitionV1,
    PendingTeamOverride,
    TurnRecord,
)
from aflow.status import (
    BannerRenderer,
    _status_display,
    build_workflow_show,
)

_FIXED_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


def _renderer(stream: object, **kwargs: object) -> BannerRenderer:
    defaults: dict[str, object] = {
        "config_max_turns": 5,
        "config_plan_path": Path("plans/demo.md"),
    }
    defaults.update(kwargs)
    return BannerRenderer(
        stream=stream,
        clock=lambda: _FIXED_NOW,
        **defaults,  # type: ignore[arg-type]
    )


def _records(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line.startswith("aflow ")]


def _basic_state() -> ControllerState:
    state = ControllerState(
        last_snapshot=PlanSnapshot("Checkpoint 1: Test", 2, 1, False, 5, 2),
        run_id="20260905T120000Z-abc123",
    )
    state.status_message = "running turn 3"
    state.active_turn = 3
    return state


def test_records_are_ordered_append_only_key_value_lines() -> None:
    stream = io.StringIO()
    renderer = _renderer(stream, workflow_name="demo")
    renderer.start(_basic_state())
    state = _basic_state()
    state.status_message = "running turn 4"
    state.active_turn = 4
    renderer.update(state)
    renderer.stop(state)

    records = _records(stream)
    assert [line.split()[2] for line in records] == ["event=start", "event=update", "event=final"]
    for line in records:
        assert line.startswith("aflow time=2026-09-05T12:00:00Z ")
        assert "\x1b" not in line
    assert "run=20260905T120000Z-abc123" in records[0]
    assert "workflow=demo" in records[0]
    assert "checkpoint=2/5" in records[0]
    assert 'checkpoint_name="Checkpoint 1: Test"' in records[0]
    assert "turn=3/5" in records[0]
    assert "elapsed=" in records[2]


def test_identical_updates_dedupe_but_final_summary_always_emits() -> None:
    stream = io.StringIO()
    renderer = _renderer(stream)
    state = _basic_state()
    renderer.start(state)
    renderer.update(state)
    renderer.update(state)
    renderer.stop(state)

    records = _records(stream)
    assert len(records) == 2
    assert "event=start" in records[0]
    assert "event=final" in records[1]


def test_status_displays_hotplug_history_capability_and_active_selector_without_session_id() -> None:
    digest = "a" * 64
    transaction = HotplugTransactionV1(
        transaction_id=hotplug_transaction_id("run", digest, 1), run_id="run",
        accepted_override_digest=digest, transaction_number=1,
        source_role="worker", target_role="worker", source_selector="codex.low",
        target_selector="codex.high", source_harness="codex", target_harness="codex",
        source_profile="low", target_profile="high", source_model_display="codex / low",
        target_model_display="codex / high", capability_path="native_resume", stage="applied",
    )
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.status_message = "completed"
    state.end_reason = "done"
    state.hotplug_history = [transaction]
    state.active_role_sessions = [HarnessSessionRefV1(
        session_id="private-session", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    )]
    rendered = _status_display(state)
    assert "native session resume" in rendered
    assert "codex.low -> codex.high" in rendered
    assert "active codex.high" in rendered
    assert "private-session" not in rendered


def test_record_surfaces_hotplug_evidence_without_session_ids() -> None:
    digest = "a" * 64
    transaction = HotplugTransactionV1(
        transaction_id=hotplug_transaction_id("run", digest, 1), run_id="run",
        accepted_override_digest=digest, transaction_number=1,
        source_role="worker", target_role="worker", source_selector="codex.low",
        target_selector="codex.high", source_harness="codex", target_harness="codex",
        source_profile="low", target_profile="high", source_model_display="codex / low",
        target_model_display="codex / high", capability_path="native_resume", stage="applied",
    )
    state = _basic_state()
    state.hotplug_history = [transaction]
    state.active_role_sessions = [HarnessSessionRefV1(
        session_id="private-session", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    )]
    stream = io.StringIO()
    _renderer(stream).update(state)

    record = _records(stream)[0]
    assert "hotplug applied: codex.low -> codex.high" in record
    assert "private-session" not in record


def test_record_surfaces_compact_manager_state() -> None:
    state = _basic_state()
    state.manager_history.append(ManagerDecisionSummary(
        decision_number=2, level="full", trigger="lite_escalation",
        action="upgrade_next_implementation", reason="retry needs a stronger team",
        artifact_path="manager/decision-002",
    ))
    state.pending_manager_notes = PendingManagerNotes("implement", ("Focus on the failing test.",), 2)
    state.pending_step_team_override = PendingTeamOverride(
        target_step="implement", role="worker", source_team="base", target_team="strong",
        selector="codex.high", checkpoint_identity="plans/in-progress/plan.md", decision_number=2,
    )
    state.last_manager_report_path = "manager-report.md"

    stream = io.StringIO()
    _renderer(stream).update(state)

    record = _records(stream)[0]
    assert "manager=full/lite_escalation/upgrade_next_implementation" in record
    assert 'manager_notes="pending for implement"' in record
    assert "manager_upgrade=implement:strong" in record
    assert "manager_report=manager-report.md" in record


def test_record_surfaces_safe_override_diagnostics() -> None:
    state = _basic_state()
    state.frozen_run_identity = FrozenRunIdentity(
        workflow_name="test",
        config_path="/config",
        config_fingerprint="1234567890abcdef",
    )
    state.override_file_present = True
    state.override_result = OverrideResult(
        status="rejected",
        digest="abc",
        message="team is incompatible",
        source_text='notes = ["private status note"]',
    )
    state.pending_override_notes = ("private status note",)

    stream = io.StringIO()
    _renderer(stream).update(state)

    record = _records(stream)[0]
    assert "frozen=1234567890ab" in record
    assert "override_file=present" in record
    assert 'override_result="rejected: team is incompatible"' in record
    assert 'override_action="correct overrides.toml and resume"' in record
    assert "private status note" not in record


def test_record_surfaces_literal_repartition_observability() -> None:
    state = ControllerState(
        last_snapshot=PlanSnapshot("Checkpoint 1 / Partition 1", 1, 1, False, 2, 2)
    )
    state.scope_pressure_reason = "[bold red]split safely[/bold red]"
    state.pending_repartition = PendingRepartitionV1(
        schema_version=1,
        decision_number=4,
        scope_id="scope-1",
        stage="failed",
        envelope_sha256="a" * 64,
        source_plan_sha256="b" * 64,
        failed_stage="validate",
    )
    state.repartition_history.append(CheckpointRepartitionRecord(
        schema_version=1,
        decision_number=3,
        scope_id="scope-1",
        generation_id="gen-123",
        envelope_sha256="a" * 64,
        envelope_artifact_sha256="c" * 64,
        source_plan_sha256="b" * 64,
        proposal_sha256="d" * 64,
        candidate_plan_sha256="e" * 64,
        partition_ids=("part-1", "part-2"),
        child_summaries=("Part one", "Part two"),
        current_disposition="review_current_partition",
        resolved_target_step="review",
        resolved_target_role="reviewer",
        current_partition_id="part-1",
        scope_pressure_reason="[bold red]split safely[/bold red]",
        envelope_artifact_path="scope/envelope.json",
        proposal_artifact_path="manager/proposal.json",
        candidate_artifact_path="manager/candidate.md",
        mechanical_validation_artifact_path="manager/mechanical.json",
        semantic_verdict_artifact_path="manager/verdict.json",
    ))

    stream = io.StringIO()
    _renderer(stream).update(state)

    record = _records(stream)[0]
    assert 'scope_pressure="[bold red]split safely[/bold red]"' in record
    assert 'repartition="failed / failed: validate"' in record
    assert 'split="gen-123 / 2 parts / review_current_partition"' in record
    assert "split_artifact=manager/candidate.md" in record


def test_turn_record_finalization_fields_appear_in_records() -> None:
    state = _basic_state()
    state.turn_history.append(TurnRecord(
        turn_number=3,
        step_name="implement",
        resolved_harness_name="claude",
        resolved_model_display="claude / opus",
        step_role="worker",
        resolved_selector="claude.opus",
        chosen_transition="review",
        chosen_transition_condition=None,
        outcome="completed",
        stdout_artifact_path=".aflow/runs/r/turns/0003/stdout.txt",
    ))
    stream = io.StringIO()
    _renderer(stream).update(state)

    record = _records(stream)[0]
    assert "step=implement" in record
    assert "role=worker:claude.opus" in record
    assert 'model="claude / opus"' in record
    assert "transition=review" in record
    assert "outcome=completed" in record
    assert "artifact=.aflow/runs/r/turns/0003/stdout.txt" in record


def test_failed_turn_final_record_preserves_untruncated_stderr_artifact() -> None:
    state = _basic_state()
    state.status_message = "failed"
    state.end_reason = "harness-failed"
    stderr_path = ".aflow/runs/failure-run/" + "deep/" * 40 + "turns/0001/stderr.txt"
    state.turn_history.append(TurnRecord(
        turn_number=1,
        step_name="implement",
        resolved_harness_name="reasonix",
        resolved_model_display="reasonix / flash",
        outcome="harness-failed",
        stderr_artifact_path=stderr_path,
    ))
    stream = io.StringIO()
    renderer = _renderer(stream)
    renderer.start(state)
    renderer.stop(state)

    final_record = _records(stream)[-1]
    assert "event=final" in final_record
    assert "outcome=harness-failed" in final_record
    assert f"stderr_artifact={stderr_path}" in final_record
    assert " artifact=" not in final_record


def test_control_bytes_flattened_and_unicode_content_remains_readable() -> None:
    state = _basic_state()
    state.status_message = "running 中文 ✓\nnext\x1b[31mred\x1b[0m line\r\nend"
    stream = io.StringIO()
    _renderer(stream).update(state)

    output = stream.getvalue()
    assert "中文 ✓" in output
    assert "\x1b" not in output
    assert "\r" not in output
    assert "running 中文 ✓ next [31mred [0m line  end" in output


def test_display_values_bounded_but_artifact_paths_never_truncated() -> None:
    state = _basic_state()
    state.status_message = "x" * 500
    deep_artifact_path = ".aflow/runs/r/" + "deep/" * 40 + "stdout.txt"
    state.turn_history.append(TurnRecord(
        turn_number=1,
        step_name="implement",
        resolved_harness_name="claude",
        resolved_model_display="claude / opus",
        outcome="completed",
        stdout_artifact_path=deep_artifact_path,
    ))
    stream = io.StringIO()
    _renderer(stream).update(state)

    record = _records(stream)[0]
    status_value = record.split("status=", 1)[1].split(" step=", 1)[0]
    assert status_value.startswith("x")
    assert status_value.endswith("...")
    assert len(status_value) <= 200
    assert deep_artifact_path in record


def test_tty_and_non_tty_streams_receive_identical_ordered_output() -> None:
    def _drive(renderer: BannerRenderer) -> None:
        state = _basic_state()
        renderer.start(state)
        state.status_message = "turn 3 finished"
        renderer.update(state)
        renderer.stop(state)

    plain = io.StringIO()
    _drive(_renderer(plain))

    master, slave = os.openpty()
    assert os.isatty(slave)
    try:
        with os.fdopen(slave, "w", encoding="utf-8") as tty_stream:
            _drive(_renderer(tty_stream))
        tty_output = b""
        while tty_output.count(b"\n") < 3:
            chunk = os.read(master, 65536)
            if not chunk:
                break
            tty_output += chunk
    finally:
        os.close(master)
    tty_text = tty_output.decode("utf-8")

    tty_records = [
        line for line in tty_text.splitlines() if line.startswith("aflow ")
    ]
    assert tty_records == _records(plain)


def test_broken_stream_disables_output_without_raising() -> None:
    class BrokenStream:
        def __init__(self) -> None:
            self.closed = False

        def write(self, _text: str) -> int:
            raise OSError("stream closed")

        def flush(self) -> None:
            raise OSError("stream closed")

    broken = BrokenStream()
    renderer = _renderer(broken)
    state = _basic_state()
    renderer.start(state)
    renderer.update(state)
    renderer.stop(state)


def _show_config() -> WorkflowUserConfig:
    review = WorkflowStepConfig(role="reviewer", go=(GoTransition(to="implement"),))
    implement = WorkflowStepConfig(role="architect", go=(GoTransition(to="END"),))
    ship = WorkflowStepConfig(role="architect", go=(GoTransition(to="END"),))
    alpha = WorkflowConfig(
        declared_steps={"review": review, "implement": implement},
        steps={"implement": implement},
        excluded_steps=("review",),
        team="7teen",
    )
    beta = WorkflowConfig(declared_steps={"ship": ship}, steps={"ship": ship})
    return WorkflowUserConfig(
        roles={"reviewer": "claude.opus", "architect": "codex.default"},
        teams={
            "7teen": TeamConfig(roles={"architect": "codex.default"}),
            "reviewers": TeamConfig(roles={"reviewer": "claude.opus"}),
        },
        workflows={"alpha": alpha, "beta": beta},
    )


def test_workflow_show_all_mode_is_plain_ascii_with_declared_states() -> None:
    output = build_workflow_show(config=_show_config())

    assert "\x1b" not in output
    assert "Roles / Teams" in output
    assert "role reviewer -> claude.opus" in output
    assert "role architect -> codex.default" in output
    assert "team 7teen: architect -> codex.default" in output
    assert "team reviewers: reviewer -> claude.opus" in output
    assert "workflow alpha" in output
    assert "workflow beta" in output
    assert "step review [excluded] role=reviewer" in output
    assert "step implement [executable] role=architect" in output
    assert "step ship [executable] role=architect" in output
    assert "go -> implement" in output
    assert "go -> END [terminal]" in output


def test_workflow_show_single_mode_filters_roles_and_marks_default_team() -> None:
    output = build_workflow_show(config=_show_config(), workflow_name="alpha")

    assert "workflow beta" not in output
    assert "role architect -> codex.default" in output
    assert "role reviewer" not in output
    assert "team 7teen (default)" in output
    assert "team reviewers:" not in output
    assert "step review [excluded] role=reviewer" in output
    assert "go -> implement" in output


def test_workflow_show_renders_transition_conditions_and_missing_selectors() -> None:
    conditional = WorkflowStepConfig(
        role="worker",
        go=(GoTransition(to="review", when="tests_pass"),),
    )
    workflow = WorkflowConfig(
        declared_steps={"implement": conditional},
        steps={"implement": conditional},
    )
    config = WorkflowUserConfig(
        roles={"worker": "codex.default"},
        workflows={"solo": workflow},
    )
    output = build_workflow_show(config=config, workflow_name="solo")
    assert "go -> review when tests_pass" in output
    assert "role worker -> codex.default" in output


from aflow.analyzer import summarize_run


def test_analyzer_renders_safe_environment_preflight_and_ignores_malformed_payload(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "status": "failed",
        "failure_kind": "environment_preflight",
        "failure_reason": "environment preflight blocked",
        "environment_preflight": {
            "schema_version": 1,
            "classification": "harness_environment_preflight",
            "reason_code": "harness_executable_missing",
            "harness": "codex",
            "invocation_kind": "workflow_turn",
            "required_executable": "codex",
            "checked_command": ["codex"],
            "remediation": "Install the trusted package that provides the required executable.",
            "safe_diagnostics": {},
            "step_name": "implement",
            "turn_number": 1,
        },
        "turns_completed": 0,
    }
    summary = summarize_run(tmp_path, payload, [], tmp_path)
    assert summary["failure"]["failure_kind"] == "environment_preflight"
    assert summary["failure"]["environment_preflight"]["reason_code"] == (
        "harness_executable_missing"
    )
    assert "environment_preflight" in summary["failure"]["signals"]

    payload["environment_preflight"] = {"raw": "/private/secret/config.toml"}
    malformed = summarize_run(tmp_path, payload, [], tmp_path)
    assert malformed["failure"]["environment_preflight"] is None

from __future__ import annotations

import io
import os
import select
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
    IssueRecord,
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
_OWNED_SGR = ("\x1b[1m", "\x1b[0m")


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


class _CapabilityStream(io.StringIO):
    def __init__(self, is_tty: bool, error: Exception | None = None) -> None:
        super().__init__()
        self._is_tty = is_tty
        self._error = error
        self.isatty_calls = 0

    def isatty(self) -> bool:
        self.isatty_calls += 1
        if self._error is not None:
            raise self._error
        return self._is_tty


class _MissingIsattyStream:
    def __init__(self) -> None:
        self._buffer = io.StringIO()

    def write(self, value: str) -> int:
        return self._buffer.write(value)

    def flush(self) -> None:
        self._buffer.flush()

    def getvalue(self) -> str:
        return self._buffer.getvalue()


def _strip_owned_sgr(value: str) -> str:
    for sequence in _OWNED_SGR:
        value = value.replace(sequence, "")
    return value


def _records(stream: io.StringIO) -> list[str]:
    return [block for block in stream.getvalue().strip().split("\n\n") if block]


def _basic_state() -> ControllerState:
    state = ControllerState(
        last_snapshot=PlanSnapshot("Checkpoint 1: Test", 2, 1, False, 5, 2),
        run_id="20260905T120000Z-abc123",
    )
    state.status_message = "running turn 3"
    state.active_turn = 3
    return state


def test_blocks_are_ordered_append_only_human_readable_sections() -> None:
    stream = io.StringIO()
    renderer = _renderer(stream, workflow_name="demo")
    renderer.start(_basic_state())
    state = _basic_state()
    state.status_message = "running turn 4"
    state.active_turn = 4
    renderer.update(state)
    renderer.stop(state)

    records = _records(stream)
    assert len(records) == 4
    assert records[0].startswith("AFlow run 20260905T120000Z-abc123")
    assert "Workflow: demo" in records[0]
    assert "plans/demo.md" in records[0]
    assert records[1].startswith("Preparing run")
    assert "Preparation update" in records[2]
    assert "Status:   running turn 4" in records[2]
    assert records[3].startswith("AFlow run 20260905T120000Z-abc123 -")
    assert "Elapsed:" in records[3]
    assert all("\x1b" not in block for block in records)


def test_identical_updates_dedupe_but_final_summary_always_emits() -> None:
    stream = io.StringIO()
    renderer = _renderer(stream)
    state = _basic_state()
    renderer.start(state)
    renderer.update(state)
    renderer.update(state)
    renderer.stop(state)

    records = _records(stream)
    assert len(records) == 3
    assert records[0].startswith("AFlow run")
    assert records[1] == "Preparing run\n  Checkpoint 2 of 5\n  Checkpoint 1: Test"
    assert records[2].startswith("AFlow run")
    assert "Elapsed:" in records[2]


def test_cp4_transcript_has_one_header_and_identifies_the_running_turn() -> None:
    stream = io.StringIO()
    renderer = _renderer(
        stream,
        config_max_turns=40,
        config_plan_path=Path(
            "/full/path/skills-manager-workflow-settings-cp4-restart-20260909.md"
        ),
        original_plan_path=Path(
            "/full/path/skills-manager-workflow-settings-cp4-restart-20260909.md"
        ),
        workflow_name="cumulative_delivery",
    )
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        run_id="20260909t003427z-ea0bff22",
        run_started_at=_FIXED_NOW,
    )
    state.current_team = "MusparkGLM"
    renderer.start(state)

    state.last_snapshot = PlanSnapshot(
        "Checkpoint 4: Manager instructions come from live skill Markdown",
        7,
        2,
        False,
        10,
        4,
    )
    renderer.update(state)
    state.active_turn = 1
    state.status_message = "running turn 1: implement_plan"
    state.turn_history.append(TurnRecord(
        turn_number=1,
        step_name="implement_plan",
        resolved_harness_name="muse",
        resolved_model_display="muse / muse-spark-1.3-contributor / high",
        step_role="worker",
        resolved_selector="muse.spark-1.3-contributor",
    ))
    renderer.update(state)
    renderer.update(state)

    blocks = _records(stream)
    assert sum(block.startswith("AFlow run ") for block in blocks) == 1
    assert blocks[0].startswith("AFlow run 20260909t003427z-ea0bff22")
    assert "Preparing run" in blocks[1]
    running = blocks[-1]
    nonblank = [line for line in running.splitlines() if line]
    assert nonblank[0].endswith("Turn 1 of 40 - Running")
    assert nonblank[1] == "  Checkpoint 4 of 10"
    assert nonblank[2] == "  Manager instructions come from live skill Markdown"
    assert "Team:     MusparkGLM" in running
    assert "Worker: muse / muse-spark-1.3-contributor / high" in running
    assert "Step:     implement_plan" in running
    assert "override_file" not in running
    assert "clean since start" not in running


def test_finalized_turn_keeps_cached_checkpoint_before_next_checkpoint_runs() -> None:
    stream = io.StringIO()
    renderer = _renderer(stream, config_max_turns=40)
    state = ControllerState(
        last_snapshot=PlanSnapshot(
            "Checkpoint 4: CP4 worker", 6, 2, False, 10, 4
        ),
        run_id="run-cp4",
        run_started_at=_FIXED_NOW,
        active_turn=1,
        status_message="running turn 1",
    )
    state.turn_history.append(TurnRecord(
        turn_number=1,
        step_name="implement_plan",
        resolved_harness_name="muse",
        resolved_model_display="muse / worker",
        step_role="worker",
        outcome="running",
    ))
    renderer.start(state)

    state.turn_history[0].outcome = "completed"
    state.turn_history[0].chosen_transition = "implement_plan"
    state.last_snapshot = PlanSnapshot("Checkpoint 5: CP5 worker", 5, 1, False, 10, 5)
    state.turns_completed = 1
    renderer.update(state)
    state.active_turn = 2
    state.status_message = "running turn 2"
    state.turn_history.append(TurnRecord(
        turn_number=2,
        step_name="implement_plan",
        resolved_harness_name="muse",
        resolved_model_display="muse / worker",
        step_role="worker",
        outcome="running",
    ))
    renderer.update(state)

    blocks = _records(stream)
    assert "Checkpoint 4 of 10" in blocks[1]
    assert "CP4 worker" in blocks[1]
    assert "Checkpoint 5 of 10" not in blocks[2]
    assert "Checkpoint 4 of 10" in blocks[2]
    assert "CP4 worker" in blocks[2]
    assert "Checkpoint 5 of 10" in blocks[3]
    assert "CP5 worker" in blocks[3]


def test_changed_active_plan_is_visible_at_finalization_before_repair_worker() -> None:
    stream = io.StringIO()
    original = Path("/full/path/original-plan.md")
    repair = Path("/full/path/repair-plan.md")
    renderer = _renderer(
        stream,
        config_plan_path=original,
        original_plan_path=original,
        active_plan_path=original,
    )
    state = ControllerState(
        last_snapshot=PlanSnapshot("Checkpoint 1: Repair", 1, 1, False, 2, 1),
        run_id="repair-run",
        run_started_at=_FIXED_NOW,
        active_turn=1,
        current_team="synthetic",
    )
    state.turn_history.append(TurnRecord(
        turn_number=1,
        step_name="review",
        resolved_harness_name="fake",
        resolved_model_display="fake / worker",
        step_role="worker",
        active_plan_path=str(original),
    ))
    renderer.start(state)

    state.turn_history[0].outcome = "completed"
    state.turns_completed = 1
    renderer.set_context(active_plan_path=repair)
    renderer.update(state)
    finalized_blocks = _records(stream)
    finalized = finalized_blocks[-1]
    assert f"  Active plan:\n    {repair}" in finalized

    state.active_turn = 2
    state.turn_history.append(TurnRecord(
        turn_number=2,
        step_name="implement",
        resolved_harness_name="fake",
        resolved_model_display="fake / worker",
        step_role="worker",
        active_plan_path=str(repair),
    ))
    renderer.update(state)
    after_worker = _records(stream)
    assert sum(str(repair) in block for block in after_worker) == 1
    block_count = len(after_worker)
    renderer.update(state)
    assert len(_records(stream)) == block_count


def test_generated_plan_is_visible_during_preparation_and_identical_updates_dedupe(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    original = tmp_path / "original.md"
    generated = tmp_path.joinpath(
        *(f"long-segment-{index:02d}" for index in range(20)),
        "generated.md",
    )
    generated.parent.mkdir(parents=True)
    generated.write_text("# generated\n", encoding="utf-8")
    renderer = _renderer(
        stream,
        config_plan_path=original,
        original_plan_path=original,
        active_plan_path=original,
    )
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        run_id="preparation-generated",
        run_started_at=_FIXED_NOW,
    )
    renderer.start(state)

    renderer.set_context(new_plan_path=generated)
    renderer.update(state)
    blocks = _records(stream)
    assert f"  Generated plan:\n    {generated}" in blocks[-1]
    block_count = len(blocks)
    renderer.update(state)
    assert len(_records(stream)) == block_count


def test_generated_plan_is_visible_at_finalization_and_not_repeated(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    original = tmp_path / "original.md"
    generated = tmp_path.joinpath(
        *(f"long-segment-{index:02d}" for index in range(20)),
        "generated-final.md",
    )
    generated.parent.mkdir(parents=True)
    generated.write_text("# generated\n", encoding="utf-8")
    renderer = _renderer(
        stream,
        config_plan_path=original,
        original_plan_path=original,
        active_plan_path=original,
    )
    state = ControllerState(
        last_snapshot=PlanSnapshot("Checkpoint 1: Finalize", 1, 1, False, 2, 1),
        run_id="finalization-generated",
        run_started_at=_FIXED_NOW,
        active_turn=1,
    )
    state.turn_history.append(TurnRecord(
        turn_number=1,
        step_name="review",
        resolved_harness_name="fake",
        resolved_model_display="fake / worker",
        step_role="worker",
        active_plan_path=str(original),
    ))
    renderer.start(state)

    state.turn_history[0].outcome = "completed"
    state.turns_completed = 1
    renderer.set_context(new_plan_path=generated)
    renderer.update(state)
    blocks = _records(stream)
    assert f"  Generated plan:\n    {generated}" in blocks[-1]
    block_count = len(blocks)
    renderer.update(state)
    assert len(_records(stream)) == block_count


def test_new_manager_identity_emits_even_when_action_is_unchanged() -> None:
    stream = io.StringIO()
    renderer = _renderer(stream)
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.manager_history.append(ManagerDecisionSummary(
        decision_number=1,
        level="lite",
        trigger="turn_finished",
        action="continue",
        reason="same action",
        artifact_path="manager/decision-001",
    ))
    renderer.update(state)
    state.manager_history.append(ManagerDecisionSummary(
        decision_number=2,
        level="lite",
        trigger="turn_finished",
        action="continue",
        reason="same action again",
        artifact_path="manager/decision-002",
    ))
    renderer.update(state)
    renderer.update(state)

    blocks = _records(stream)
    assert len(blocks) == 2
    assert "Manager decision #1: lite/turn_finished/continue" in blocks[0]
    assert "Manager decision #2: lite/turn_finished/continue" in blocks[1]


def test_failed_turn_uses_matching_issue_reason_and_keeps_only_available_logs() -> None:
    stderr_path = "/full/path/to/turn/stderr.txt"
    state = ControllerState(
        last_snapshot=PlanSnapshot("Checkpoint 4: Failure", 1, 1, False, 10, 1),
        run_id="failure-run",
        run_started_at=_FIXED_NOW,
        active_turn=4,
        status_message="failed",
    )
    state.turn_history.append(TurnRecord(
        turn_number=4,
        step_name="implement_plan",
        resolved_harness_name="dsh",
        resolved_model_display="dsh / ACP",
        step_role="worker",
        outcome="harness-failed",
        stderr_artifact_path=stderr_path,
    ))
    state.issue_history.append(IssueRecord(
        issue_number=1,
        kind="harness-failed",
        message="DSH ACP request failed: Usage limit reached for 5 hour.",
        turn_number=4,
        stderr_artifact_path=stderr_path,
    ))
    stream = io.StringIO()
    _renderer(stream).stop(state)

    output = stream.getvalue()
    assert "AFlow run failure-run - Failed" in output
    assert "Reason: DSH ACP request failed: Usage limit reached for 5 hour." in output
    assert stderr_path in output
    assert "stdout:" not in output


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
    assert "Hotplug transaction #1: applied (codex.low -> codex.high; native session resume)" in record
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
    assert "Manager decision #2: full/lite_escalation/upgrade_next_implementation" in record
    assert "Pending manager notes: implement (decision 2)" in record
    assert "Manager team override: implement -> strong" in record
    assert "manager-report.md" in record


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
    assert "Config fingerprint (diagnostic): 1234567890ab" in record
    assert "Override file: present" in record
    assert "Override result: rejected (abc): team is incompatible" in record
    assert "Override action: correct overrides.toml and resume" in record
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
    assert "Scope pressure: [bold red]split safely[/bold red]" in record
    assert "Repartition: failed" in record
    assert "Repartition failed stage: validate" in record
    assert "Partition split: gen-123 / 2 parts / review_current_partition" in record
    assert "manager/candidate.md" in record


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
    assert "Step:     implement" in record
    assert "Worker: claude / opus" in record
    assert "Selector: claude.opus" in record
    assert "Next:     review" in record
    assert "Outcome:  completed" in record
    assert ".aflow/runs/r/turns/0003/stdout.txt" in record


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
    assert final_record.startswith("AFlow run")
    assert "harness-failed" in final_record
    assert f"stderr: {stderr_path}" in final_record
    assert "stdout:" not in final_record


def test_control_bytes_flattened_and_unicode_content_remains_readable() -> None:
    state = _basic_state()
    state.status_message = "preparing"
    stream = io.StringIO()
    renderer = _renderer(stream)
    renderer.start(state)
    state.status_message = "running 中文 ✓\nnext\x1b[31mred\x1b[0m line\r\nend"
    renderer.update(state)

    output = stream.getvalue()
    assert "中文 ✓" in output
    assert "\x1b" not in output
    assert "\r" not in output
    assert "running 中文 ✓ next [31mred [0m line  end" in output


def test_bold_policy_requires_capable_tty_and_environment(monkeypatch) -> None:
    cases = (
        ("capable tty", _CapabilityStream(True), "xterm", None, True),
        ("term dumb", _CapabilityStream(True), "dumb", None, False),
        ("term missing", _CapabilityStream(True), None, None, False),
        ("term empty", _CapabilityStream(True), "", None, False),
        ("no color empty", _CapabilityStream(True), "xterm", "", False),
        ("no color value", _CapabilityStream(True), "xterm", "1", False),
        ("non tty", _CapabilityStream(False), "xterm", None, False),
        ("isatty missing", _MissingIsattyStream(), "xterm", None, False),
        (
            "isatty raises",
            _CapabilityStream(True, RuntimeError("isatty unavailable")),
            "xterm",
            None,
            False,
        ),
    )
    for name, stream, term, no_color, expected in cases:
        if term is None:
            monkeypatch.delenv("TERM", raising=False)
        else:
            monkeypatch.setenv("TERM", term)
        if no_color is None:
            monkeypatch.delenv("NO_COLOR", raising=False)
        else:
            monkeypatch.setenv("NO_COLOR", no_color)

        renderer = _renderer(stream)
        renderer.start(_basic_state())
        output = stream.getvalue()
        assert (_OWNED_SGR[0] in output) is expected, name
        if not expected:
            assert "\x1b" not in output, name


def test_force_color_variables_do_not_enable_pipe_styling(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("CLICOLOR_FORCE", "1")

    stream = _CapabilityStream(False)
    _renderer(stream).start(_basic_state())

    assert "\x1b" not in stream.getvalue()


def test_styling_is_cached_per_stream_and_environment_changes_do_not_redraw_policy(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _CapabilityStream(True)
    renderer = _renderer(stream)
    state = _basic_state()
    state.run_started_at = _FIXED_NOW
    renderer.start(state)
    assert stream.isatty_calls == 1

    monkeypatch.setenv("NO_COLOR", "")
    state.status_message = "preparation changed"
    renderer.update(state)

    assert stream.isatty_calls == 1
    assert _OWNED_SGR[0] in stream.getvalue().split("\n\n")[-1]


def test_styled_output_strips_to_plain_output_and_styles_only_headings(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)

    def _drive(renderer: BannerRenderer) -> None:
        state = _basic_state()
        state.run_started_at = _FIXED_NOW
        state.run_id = "run-\x1b[2J"
        renderer.start(state)
        state.status_message = "running 中文 ✓\nnext\x1b[31mred\x1b[0m line\r\nend"
        renderer.update(state)
        renderer.stop(state)

    styled = _CapabilityStream(True)
    plain = io.StringIO()
    _drive(_renderer(styled))
    _drive(_renderer(plain))

    styled_output = styled.getvalue()
    assert _strip_owned_sgr(styled_output) == plain.getvalue()
    assert styled_output.count(_OWNED_SGR[0]) == styled_output.count(_OWNED_SGR[1])
    assert "\x1b" not in _strip_owned_sgr(styled_output)
    for block in styled_output.strip().split("\n\n"):
        lines = block.splitlines()
        assert lines[0].startswith(_OWNED_SGR[0])
        assert lines[0].endswith(_OWNED_SGR[1])
        assert "\x1b" not in "\n".join(lines[1:])


def test_display_values_bounded_but_artifact_paths_never_truncated() -> None:
    state = _basic_state()
    state.status_message = "preparing"
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
    renderer = _renderer(stream)
    renderer.start(state)
    state.status_message = "x" * 500
    renderer.update(state)
    renderer.stop(state)

    output = stream.getvalue()
    assert "x" * 197 + "..." in output
    assert deep_artifact_path in output


def test_tty_and_non_tty_streams_receive_identical_ordered_output(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)

    def _drive(renderer: BannerRenderer) -> None:
        state = _basic_state()
        state.run_started_at = _FIXED_NOW
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
            tty_stream.flush()
            # BSD PTYs can discard unread output when the last slave closes.
            # Drain while the writer remains open on every supported platform.
            tty_output = b""
            while True:
                readable, _, _ = select.select((master,), (), (), 1.0)
                if not readable:
                    break
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                tty_output += chunk
    finally:
        os.close(master)
    tty_text = tty_output.decode("utf-8").replace("\r\n", "\n")
    assert _strip_owned_sgr(tty_text) == plain.getvalue()
    assert "\x1b" not in _strip_owned_sgr(tty_text)
    assert "\r" not in tty_text


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

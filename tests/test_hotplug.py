from pathlib import Path
from dataclasses import replace
import subprocess
import hashlib
import json

import pytest

from aflow.hotplug import (
    HOTPLUG_STAGES,
    HANDOVER_HEADINGS,
    HarnessSessionRefV1,
    HandoverContextV1,
    HotplugTransactionV1,
    build_handover_context_v1,
    bounded_hotplug_history,
    classify_hotplug_resume_stage,
    copy_hotplug_resume_artifacts,
    hotplug_artifact_dir,
    hotplug_transaction_id,
    render_handover_prompt,
    safe_hotplug_artifact_path,
    validate_handover_output,
    workspace_fingerprint,
    write_handover_artifacts,
    write_hotplug_artifact,
)
from aflow.plan import PlanSnapshot
from aflow.run_state import ControllerState, RetryContext, ResumeContext, hotplug_resume_fields, hotplug_state_payload, load_override_request
from aflow.config import GoTransition, HarnessProfileConfig, TeamConfig, WorkflowConfig, WorkflowHarnessConfig, WorkflowStepConfig, WorkflowUserConfig
from aflow.harnesses.codex import CodexAdapter
from aflow.harnesses.base import HarnessInvocation
from aflow.harnesses.session import SessionCapabilities, SessionRequest, SessionResult
from aflow.harnesses.reasonix import ReasonixAcpDriver
from aflow.run_state import PendingTeamOverride
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, resolve_role_selector, run_workflow
from aflow.api.events import CollectingObserver, ExecutionEventType, HotplugEvent
from aflow.analyzer import _hotplug_summary, summarize_run


def make_transaction(stage: str = "accepted") -> HotplugTransactionV1:
    digest = "a" * 64
    return HotplugTransactionV1(
        transaction_id=hotplug_transaction_id("run-1", digest, 1),
        run_id="run-1", accepted_override_digest=digest, transaction_number=1,
        source_role="worker", target_role="worker",
        source_selector="reasonix.flash", target_selector="codex.high",
        source_harness="reasonix", target_harness="codex",
        source_profile="flash", target_profile="high",
        source_model_display="reasonix / flash", target_model_display="codex / high",
        stage=stage,
    )


def test_hotplug_events_are_secret_safe_and_structured() -> None:
    transaction = make_transaction("handover_ready")
    event = HotplugEvent.create(ExecutionEventType.HOTPLUG_STAGE_CHANGED, transaction)
    assert event.event_type == ExecutionEventType.HOTPLUG_STAGE_CHANGED
    assert event.transaction_id == transaction.transaction_id
    assert event.source_selector == transaction.source_selector
    assert event.target_selector == transaction.target_selector
    assert not hasattr(event, "session_id")
    assert not hasattr(event, "prompt")


def test_analyzer_hotplug_summary_exposes_session_presence_not_ids() -> None:
    summary = _hotplug_summary({
        "current_hotplug_transaction": {
            **make_transaction("handover_ready").to_dict(),
            "source_session": {"session_id": "private-source"},
        },
        "active_role_sessions": [{
            "role": "worker", "selector": "codex.high", "status": "active",
            "session_id": "private-target",
        }],
        "hotplug_history": [],
    })
    current = summary["current"]
    assert current["source_session_present"] is True
    assert current["target_session_present"] is True
    assert "private-source" not in repr(summary)
    assert "private-target" not in repr(summary)


@pytest.mark.parametrize("stage_value", [None, 7])
def test_analyzer_ignores_malformed_hotplug_stage_without_crashing(
    tmp_path: Path, stage_value: object,
) -> None:
    payload = make_transaction("handover_ready").to_dict()
    if stage_value is None:
        del payload["stage"]
    else:
        payload["stage"] = stage_value
    run_json = {
        "current_hotplug_transaction": payload,
        "hotplug_history": [],
        "active_role_sessions": [],
    }

    assert _hotplug_summary(run_json)["current"] is None
    analyzed = summarize_run(tmp_path, run_json, [], tmp_path)
    assert analyzed["hotplug"]["current"] is None


def test_every_transaction_stage_round_trips() -> None:
    for stage in HOTPLUG_STAGES:
        transaction = make_transaction(stage)
        assert HotplugTransactionV1.from_dict(transaction.to_dict()) == transaction


def test_session_reference_round_trips() -> None:
    session = HarnessSessionRefV1(
        session_id="session-1", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    )
    assert HarnessSessionRefV1.from_dict(session.to_dict()) == session


def test_same_harness_native_resume_uses_exact_session_id(tmp_path: Path) -> None:
    driver = CodexAdapter().session_driver(
        exec_help="codex exec --json resume",
        resume_help="resume [SESSION_ID] -m, --model MODEL",
    )
    request = SessionRequest(
        repo_root=tmp_path,
        selector="codex.high",
        model="high-model",
        effort="high",
        system_prompt="system",
        user_prompt="continue",
        session_id="source-session",
    )
    invocation = driver.build_invocation(request)
    assert invocation.argv[:4] == ("codex", "exec", "resume", "source-session")
    result = driver.parse_result(
        request,
        '{"type":"thread.started","thread_id":"source-session"}\n'
        '{"type":"message.completed","text":"continued"}\n',
        returncode=0,
    )
    assert result.session_id == "source-session"


def test_live_run_workflow_consumes_semantic_session_output(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    config = WorkflowUserConfig(
        roles={"worker": "codex.high"},
        harnesses={"codex": WorkflowHarnessConfig(profiles={"high": HarnessProfileConfig(model="high-model")})},
        workflows={"live": WorkflowConfig(
            steps={"implement": WorkflowStepConfig(
                role="worker", prompts=("p",), go=(GoTransition(to="END", when="DONE"),)
            )}, first_step="implement",
        )},
        prompts={"p": "Work."},
    )
    driver = CodexAdapter().session_driver(
        exec_help="codex exec --json resume",
        resume_help="resume [SESSION_ID] -m, --model MODEL",
    )

    def runner(argv, **kwargs):
        plan.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0,
            '{"type":"thread.started","thread_id":"live-session"}\n'
            '{"type":"message.completed","text":"DONE"}\n',
            "",
        )

    result = run_workflow(
        ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=2),
        config, "live", config_dir=tmp_path, adapter=CodexAdapter(),
        runner=runner, session_driver=driver,
    )
    assert result.final_snapshot.is_complete


def test_live_run_workflow_activates_target_after_source_and_resumes_exact_session(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    config = WorkflowUserConfig(
        roles={"worker": "codex.high"},
        harnesses={"codex": WorkflowHarnessConfig(profiles={
            "high": HarnessProfileConfig(model="high-model"),
            "low": HarnessProfileConfig(model="low-model"),
        })},
        workflows={"live": WorkflowConfig(
            steps={"implement": WorkflowStepConfig(
                role="worker", prompts=("p",), go=(
                    GoTransition(to="END", when="DONE"),
                    GoTransition(to="implement"),
                )
            )}, first_step="implement",
        )}, prompts={"p": "Work."},
    )
    driver = CodexAdapter().session_driver(
        exec_help="codex exec --json resume",
        resume_help="resume [SESSION_ID] -m, --model MODEL",
    )
    calls: list[tuple[str, ...]] = []
    override_written = False

    def control_source() -> str | None:
        nonlocal override_written
        run_dirs = sorted((tmp_path / ".aflow" / "runs").glob("*"))
        if run_dirs and not override_written:
            path = run_dirs[-1] / "overrides.toml"
            path.write_text('[roles]\nworker = "codex.low"\n', encoding="utf-8")
            override_written = True
        if not run_dirs:
            return None
        path = run_dirs[-1] / "overrides.toml"
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        if len(calls) == 1:
            output = "continue"
        else:
            plan.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n", encoding="utf-8")
            output = "DONE"
        return subprocess.CompletedProcess(
            argv, 0,
            '{"type":"thread.started","thread_id":"live-session"}\n'
            f'{{"type":"message.completed","text":"{output}"}}\n', "",
        )

    result = run_workflow(
        ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=3),
        config, "live", config_dir=tmp_path, adapter=CodexAdapter(),
        runner=runner, session_driver=driver, control_source=control_source,
    )
    assert result.final_snapshot.is_complete
    assert len(calls) == 2
    assert calls[1][:4] == ("codex", "exec", "resume", "live-session")
    assert "low-model" in calls[1]
    run_json = next((tmp_path / ".aflow" / "runs").glob("*/run.json"))
    persisted = json.loads(run_json.read_text(encoding="utf-8"))
    assert persisted["role_selectors"]["worker"] == "codex.low"
    assert persisted["current_hotplug_transaction"] is None
    assert persisted["pending_hotplug_transaction"] is None
    assert [item["stage"] for item in persisted["hotplug_history"]] == ["applied"]


def _run_pending_target_without_resume_driver(
    tmp_path: Path, driver, *, target_output: str = "continue",
    target_session_id: str = "source", preflight_probe=None,
) -> tuple[list[tuple[str, ...]], dict]:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    config = WorkflowUserConfig(
        roles={"worker": "codex.high"},
        harnesses={"codex": WorkflowHarnessConfig(profiles={
            "high": HarnessProfileConfig(model="high-model"),
            "low": HarnessProfileConfig(model="low-model"),
        })},
        workflows={"live": WorkflowConfig(steps={"implement": WorkflowStepConfig(
            role="worker", prompts=("p",), go=(
                GoTransition(to="END", when="DONE"), GoTransition(to="implement")
            )
        )}, first_step="implement")}, prompts={"p": "Work."},
    )
    calls: list[tuple[str, ...]] = []
    written = False

    def control_source() -> str | None:
        nonlocal written
        runs = sorted((tmp_path / ".aflow" / "runs").glob("*"))
        if runs and not written:
            (runs[-1] / "overrides.toml").write_text(
                '[roles]\nworker = "codex.low"\n', encoding="utf-8"
            )
            written = True
        if not runs or not (runs[-1] / "overrides.toml").is_file():
            return None
        return hashlib.sha256((runs[-1] / "overrides.toml").read_bytes()).hexdigest()

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        output = target_output if len(calls) > 1 else "continue"
        session_id = target_session_id if len(calls) > 1 else "source"
        return subprocess.CompletedProcess(
            argv, 0,
            f'{{"type":"thread.started","thread_id":"{session_id}"}}\n'
            f'{{"type":"message.completed","text":"{output}"}}\n', "",
        )

    with pytest.raises(WorkflowError):
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=3),
            config, "live", config_dir=tmp_path, adapter=CodexAdapter(),
            runner=runner, session_driver=driver, control_source=control_source,
            preflight_probe=preflight_probe,
        )
    run_json = next((tmp_path / ".aflow" / "runs").glob("*/run.json"))
    return calls, json.loads(run_json.read_text(encoding="utf-8"))


def test_run_level_no_driver_never_invokes_pending_target(tmp_path: Path) -> None:
    calls, state = _run_pending_target_without_resume_driver(tmp_path, None)
    assert len(calls) == 1
    assert state["role_selectors"]["worker"] == "codex.high"
    assert state["hotplug_history"][-1]["stage"] == "failed"


def test_run_level_unsupported_resume_never_invokes_pending_target(tmp_path: Path) -> None:
    unsupported = CodexAdapter().session_driver(
        exec_help="codex exec --json resume", resume_help="resume [SESSION_ID]"
    )
    calls, state = _run_pending_target_without_resume_driver(tmp_path, unsupported)
    assert len(calls) == 1
    assert state["role_selectors"]["worker"] == "codex.high"
    assert state["hotplug_history"][-1]["stage"] == "failed"


def test_run_level_mismatched_target_session_restores_source_once(tmp_path: Path) -> None:
    driver = CodexAdapter().session_driver(
        exec_help="codex exec --json resume",
        resume_help="resume [SESSION_ID] -m, --model MODEL",
    )
    calls, state = _run_pending_target_without_resume_driver(
        tmp_path, driver,
        target_output="target result",
        target_session_id="wrong-target",
    )
    # The helper's wire session id remains source for both turns; a real
    # mismatched-id fixture is covered by the parser contract suite.
    assert len(calls) == 2
    assert state["role_selectors"]["worker"] == "codex.high"
    assert [item["stage"] for item in state["hotplug_history"]].count("failed") == 1


def test_run_level_target_preflight_failure_restores_source_and_never_invokes_target(tmp_path: Path) -> None:
    class FailTargetPreflight:
        calls = 0

        def resolve_executable(self, command: str, *, env):
            self.calls += 1
            return None if self.calls >= 2 else command

        def run_diagnostic(self, argv, *, cwd, env, timeout_seconds):
            return None

    probe = FailTargetPreflight()
    driver = CodexAdapter().session_driver(
        exec_help="codex exec --json resume",
        resume_help="resume [SESSION_ID] -m, --model MODEL",
    )
    calls, state = _run_pending_target_without_resume_driver(
        tmp_path, driver, preflight_probe=probe
    )
    assert len(calls) == 1
    assert state["role_selectors"]["worker"] == "codex.high"
    assert state["hotplug_history"][-1]["stage"] == "failed"


def _controller_config(*, with_review: bool = False, with_team: bool = False) -> WorkflowUserConfig:
    steps = {
        "implement": WorkflowStepConfig(
            role="worker", prompts=("p",), go=(GoTransition(to="END", when="DONE"), GoTransition(to="review" if with_review else "implement"))
        )
    }
    if with_review:
        steps["review"] = WorkflowStepConfig(
            role="reviewer", prompts=("review",), go=(GoTransition(to="implement"),)
        )
    teams = {}
    if with_team:
        teams = {
            "base": TeamConfig(roles={"worker": "codex.low"}),
            "strong": TeamConfig(roles={"worker": "codex.high"}),
        }
    return WorkflowUserConfig(
        roles={"worker": "codex.high", "reviewer": "codex.review"},
        harnesses={"codex": WorkflowHarnessConfig(profiles={
            "high": HarnessProfileConfig(model="high-model"),
            "low": HarnessProfileConfig(model="low-model"),
            "review": HarnessProfileConfig(model="review-model"),
        })},
        teams=teams,
        workflows={"live": WorkflowConfig(steps=steps, first_step="implement")},
        prompts={"p": "Work.", "review": "Review."},
    )


def _run_scripted_controller(
    tmp_path: Path, *, config: WorkflowUserConfig | None = None,
    writes: tuple[str | None, ...] = (), max_turns: int = 4,
    driver=None, runner_mode: str = "normal",
) -> tuple[list[tuple[str, ...]], list[dict], Path]:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    provider_ops = 0
    tick = 0
    written: list[Path] = []
    config = config or _controller_config()

    def control_source() -> str | None:
        nonlocal tick
        tick += 1
        run_dirs = sorted((tmp_path / ".aflow" / "runs").glob("*"))
        if run_dirs and tick <= len(writes) and writes[tick - 1] is not None:
            path = run_dirs[-1] / "overrides.toml"
            path.write_text(writes[tick - 1] or "", encoding="utf-8")
            written.append(path)
        if not run_dirs or not (run_dirs[-1] / "overrides.toml").is_file():
            return None
        path = run_dirs[-1] / "overrides.toml"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def runner(argv, **kwargs):
        nonlocal provider_ops
        calls.append(tuple(argv))
        if runner_mode == "crash_target" and len(calls) == 2:
            raise RuntimeError("crash before target provider operation")
        provider_ops += 1
        model = argv[argv.index("--model") + 1] if "--model" in argv else "unknown"
        output = "reviewed" if model == "review-model" else "continue"
        if len(calls) >= max_turns or runner_mode == "complete_target" and len(calls) >= 2:
            output = "DONE"
            plan.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n", encoding="utf-8")
        session = "source" if len(calls) == 1 else "source"
        return subprocess.CompletedProcess(
            argv, 0,
            f'{{"type":"thread.started","thread_id":"{session}"}}\n'
            f'{{"type":"message.completed","text":"{output}"}}\n', "",
        )

    result = run_workflow(
        ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=max_turns),
        config, "live", config_dir=tmp_path, adapter=CodexAdapter(),
        runner=runner, session_driver=driver, control_source=control_source,
    )
    run_json = result.run_dir / "run.json"
    state = json.loads(run_json.read_text(encoding="utf-8"))
    assert provider_ops <= len(calls)
    return calls, [state], run_json


def test_controller_repeated_digest_and_unchanged_selector_are_consumed_noops(tmp_path: Path) -> None:
    source = '[roles]\nworker = "codex.high"\n'
    calls, states, run_json = _run_scripted_controller(tmp_path, writes=(source, source), max_turns=2)
    state = states[-1]
    assert len(calls) == 2
    assert [item["stage"] for item in state["hotplug_history"]] == []
    assert state["role_selectors"]["worker"] == "codex.high"
    assert json.loads(run_json.read_text(encoding="utf-8"))["override_result"]["applied"] is True


def test_controller_second_changed_digest_while_pending_does_not_mutate_transaction(tmp_path: Path) -> None:
    second = '[roles]\nworker = "codex.low"\n'
    transaction = replace(
        make_transaction("accepted"), source_selector="codex.high", target_selector="codex.low",
        source_harness="codex", target_harness="codex", source_profile="high", target_profile="low",
        source_model_display="codex / high", target_model_display="codex / low",
    )
    session = HarnessSessionRefV1(
        session_id="source", role="worker", selector=transaction.source_selector,
        harness="codex", profile="flash", model_display="codex / source",
    )
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    override = tmp_path / "overrides.toml"
    override.write_text(second, encoding="utf-8")
    resume = ResumeContext(
        resumed_from_run_id="pending-source", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", role_selectors={"worker": transaction.source_selector},
        current_hotplug_transaction=transaction, pending_hotplug_transaction=transaction,
        active_role_sessions=(session,), hotplug_transaction_number=1,
        override_source_run_dir=tmp_path,
    )
    calls: list[tuple[str, ...]] = []
    with pytest.raises(WorkflowError) as error:
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
            _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: (calls.append(tuple(argv)) or subprocess.CompletedProcess(argv, 0, "continue", "")),
            resume=resume,
        )
    state = json.loads((error.value.run_dir / "run.json").read_text(encoding="utf-8"))
    assert calls == []
    assert state["current_hotplug_transaction"]["transaction_id"] == transaction.transaction_id
    assert state["current_hotplug_transaction"]["stage"] == "accepted"
    assert state["hotplug_history"] == []
    assert "hotplug_in_progress" in state["override_result"]["message"]


def test_reviewer_interposition_keeps_target_mapping_and_session_dormant(tmp_path: Path) -> None:
    first = '[roles]\nworker = "codex.low"\n'
    driver = CodexAdapter().session_driver(exec_help="codex exec --json resume", resume_help="resume [SESSION_ID] -m, --model MODEL")
    calls, states, _ = _run_scripted_controller(tmp_path, config=_controller_config(with_review=True), writes=(first,), max_turns=3, driver=driver)
    state = states[-1]
    assert "review-model" in calls[1]
    assert calls[2][:4] == ("codex", "exec", "resume", "source")
    assert state["role_selectors"]["worker"] == "codex.low"
    assert state["hotplug_history"][-1]["stage"] == "applied"


def test_retry_preserves_transaction_and_source_session_identity(tmp_path: Path) -> None:
    transaction = replace(
        make_transaction("accepted"), source_selector="codex.high", target_selector="codex.low",
        source_harness="codex", target_harness="codex", source_profile="high", target_profile="low",
        source_model_display="codex / high", target_model_display="codex / low",
    )
    session = HarnessSessionRefV1(
        session_id="source", role="worker", selector=transaction.source_selector,
        harness="codex", profile="flash", model_display="codex / source",
    )
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    resume = ResumeContext(
        resumed_from_run_id="retry-source", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", role_selectors={"worker": transaction.target_selector},
        current_hotplug_transaction=transaction, pending_hotplug_transaction=transaction,
        active_role_sessions=(session,), hotplug_transaction_number=1,
    )
    retry = RetryContext(
        step_name="implement", step_role="worker", resolved_selector=transaction.target_selector,
        resolved_harness_name="codex", resolved_model="low-model", resolved_effort=None,
        snapshot_before=PlanSnapshot("First", 1, 1, False), active_plan_path=plan,
        new_plan_path=tmp_path / "new-plan.md", base_user_prompt="retry", parse_error_str="inconsistent checkpoint",
        attempt=1, retry_limit=1,
    )
    calls: list[tuple[str, ...]] = []
    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        plan.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, '{"type":"thread.started","thread_id":"source"}\n{"type":"message.completed","text":"DONE"}\n', "")
    result = run_workflow(
        ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=2),
        _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
        runner=runner, session_driver=CodexAdapter().session_driver(exec_help="codex exec --json resume", resume_help="resume [SESSION_ID] -m, --model MODEL"),
        resume=resume, startup_retry=retry,
    )
    state = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert calls[0][:4] == ("codex", "exec", "resume", "source")
    assert state["hotplug_history"][-1]["transaction_id"] == transaction.transaction_id
    assert state["hotplug_history"][-1]["transaction_number"] == transaction.transaction_number
    assert state["hotplug_history"][-1]["stage"] == "applied"
    assert state["active_role_sessions"][0]["session_id"] == "source"


def test_manager_one_turn_override_precedes_run_local_then_returns(tmp_path: Path) -> None:
    config = _controller_config(with_team=True)
    pending = PendingTeamOverride(target_step="implement", role="worker", source_team="base", target_team="strong", selector="codex.high", checkpoint_identity=None, decision_number=1)
    resume = ResumeContext(resumed_from_run_id="manager-source", feature_branch=None, worktree_path=None, main_branch=None, setup=(), teardown=(), pending_step_team_override=pending, role_selectors={"worker": "codex.low"}, interrupted_step_name="implement")
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    seen: list[str] = []
    def runner(argv, **kwargs):
        seen.append(argv[argv.index("--model") + 1])
        return subprocess.CompletedProcess(argv, 0, "continue", "")
    with pytest.raises(WorkflowError) as error:
        run_workflow(ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=2, team="base"), config, "live", config_dir=tmp_path, adapter=CodexAdapter(), runner=runner, resume=resume)
    state = json.loads((error.value.run_dir / "run.json").read_text(encoding="utf-8"))
    assert seen == ["high-model", "low-model"]
    assert state["pending_step_team_override"] is None


def test_crash_resume_before_target_start_or_success_keeps_transaction_nonterminal(tmp_path: Path) -> None:
    first = '[roles]\nworker = "codex.low"\n'
    driver = CodexAdapter().session_driver(exec_help="codex exec --json resume", resume_help="resume [SESSION_ID] -m, --model MODEL")
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    config = _controller_config()
    calls: list[tuple[str, ...]] = []
    provider_operations: list[str] = []
    def control():
        runs = sorted((tmp_path / ".aflow" / "runs").glob("*"))
        if runs and not (runs[-1] / "overrides.toml").exists():
            (runs[-1] / "overrides.toml").write_text(first, encoding="utf-8")
        path = runs[-1] / "overrides.toml" if runs else None
        return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.exists() else None
    def crashing_runner(argv, **kwargs):
        calls.append(tuple(argv))
        if len(calls) == 2:
            raise RuntimeError("crash before target provider operation")
        provider_operations.append("source")
        return subprocess.CompletedProcess(argv, 0, '{"type":"thread.started","thread_id":"source"}\n{"type":"message.completed","text":"continue"}\n', "")
    with pytest.raises(WorkflowError) as error:
        run_workflow(ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=3), config, "live", config_dir=tmp_path, adapter=CodexAdapter(), runner=crashing_runner, session_driver=driver, control_source=control)
    assert isinstance(error.value.__cause__, RuntimeError)
    run_dir = error.value.run_dir
    persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    failed_turn = json.loads(
        (run_dir / "turns" / "turn-002" / "result.json").read_text(encoding="utf-8")
    )
    assert persisted["status"] == "failed"
    assert persisted["turns_completed"] == 1
    assert failed_turn["status"] == "harness-failed"
    assert failed_turn["error"].startswith("RuntimeError: crash before target provider operation")
    assert len(failed_turn["error"]) <= 512
    assert persisted["current_hotplug_transaction"]["stage"] == "accepted"
    assert persisted["pending_hotplug_transaction"]["stage"] == "accepted"
    assert not persisted["hotplug_history"]
    fields = hotplug_resume_fields(persisted)
    resume = ResumeContext(
        resumed_from_run_id=run_dir.name, feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", **fields,
    )
    def resumed_runner(argv, **kwargs):
        calls.append(tuple(argv))
        provider_operations.append("target")
        plan.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, '{"type":"thread.started","thread_id":"source"}\n{"type":"message.completed","text":"DONE"}\n', "")
    resumed = run_workflow(
        ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=2), config, "live",
        config_dir=tmp_path, adapter=CodexAdapter(), runner=resumed_runner,
        session_driver=driver, resume=resume,
    )
    after = json.loads((resumed.run_dir / "run.json").read_text(encoding="utf-8"))
    assert provider_operations == ["source", "target"]
    # The first target invocation is an ambiguous crash boundary; resume adds
    # exactly one provider operation, while the durable operation list has no
    # duplicate operation id.
    assert sum(call[:4] == ("codex", "exec", "resume", "source") for call in calls) == 2
    assert after["hotplug_history"][-1]["transaction_id"] == persisted["current_hotplug_transaction"]["transaction_id"]
    assert after["hotplug_history"][-1]["stage"] == "applied"


def _valid_handover() -> str:
    return "\n".join(f"## {heading}\n- bounded operational evidence" for heading in HANDOVER_HEADINGS)


def test_cross_harness_handover_context_is_bounded_and_redacted(tmp_path: Path) -> None:
    transaction = make_transaction()
    context = build_handover_context_v1(
        transaction,
        {
            "plan_state": {"checkpoint": 1, "name": "First"},
            "active_implementation_scope": {"scope_id": "scope-1"},
            "completed_work": ["implemented boundary"],
            "implementation_attempts": [{"turn": 1, "selector": "reasonix.flash"}],
            "latest_full_rejection": {"summary": "repair required"},
            "run_summary": {"turns_completed": 1},
            "workspace_facts": {"dirty": True},
            "manager_action": "must-not-cross-boundary",
            "prompt": "must-not-cross-boundary",
        },
        artifact_refs=("hotplugs/hotplug-001/transaction.json",),
    )
    assert isinstance(context, HandoverContextV1)
    payload = json.dumps(context.to_dict(), sort_keys=True)
    assert "must-not-cross-boundary" not in payload
    assert len(payload.encode()) < 8192
    prompt = render_handover_prompt(transaction, context)
    assert "Source worker handover" not in prompt
    assert transaction.transaction_id in prompt
    assert "## Objective And Checkpoint" in prompt


def test_cross_harness_handover_requires_all_sections_and_8k_bound() -> None:
    assert validate_handover_output(_valid_handover()).endswith("\n")
    with pytest.raises(ValueError, match="exact required section order"):
        validate_handover_output("## Objective And Checkpoint\n- incomplete")
    with pytest.raises(ValueError, match="8 KiB"):
        validate_handover_output(_valid_handover() + "x" * 9000)
    with pytest.raises(ValueError, match="hidden reasoning"):
        validate_handover_output(_valid_handover().replace("bounded operational evidence", "hidden reasoning"))
    with pytest.raises(ValueError, match="placeholder"):
        validate_handover_output(_valid_handover().replace("bounded operational evidence", "TBD"))
    reordered = _valid_handover().replace(
        "## Completed Work\n", "## Verification\n- evidence\n## Completed Work\n", 1
    )
    with pytest.raises(ValueError, match="exact required section order"):
        validate_handover_output(reordered)


def test_reasonix_production_driver_does_not_claim_unsupported_handover() -> None:
    driver = ReasonixAcpDriver.from_initialize({
        "result": {
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"embeddedContext": True},
                "_meta": {"reasonix.io": {"sessionSteer": {"method": "_reasonix.io/session/steer"}}},
            },
        },
    })
    assert driver.capabilities.read_only_teardown is False
    assert not callable(getattr(driver, "handover", None))


def test_cross_harness_artifacts_are_hash_bound_and_workspace_fingerprint_detects_mutation(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n", encoding="utf-8")
    before = workspace_fingerprint(tmp_path, (plan,))
    transaction = make_transaction()
    context = build_handover_context_v1(transaction, {"plan_state": {"checkpoint": 1}})
    paths, hashes = write_handover_artifacts(
        tmp_path, 1, context, _valid_handover(), {"plan_state": {"checkpoint": 1}}
    )
    assert paths == (
        "hotplugs/hotplug-001/handover.md",
        "hotplugs/hotplug-001/context.json",
        "hotplugs/hotplug-001/full-context.json",
    )
    assert all(len(value) == 64 for value in hashes)
    assert json.loads((tmp_path / paths[2]).read_text()) == {"plan_state": {"checkpoint": 1}}
    plan.write_text("# Mutated\n", encoding="utf-8")
    after = workspace_fingerprint(tmp_path, (plan,))
    assert before["sha256"] != after["sha256"]


@pytest.mark.parametrize("observer_fails", [False, True], ids=("success", "observer-failure"))
def test_cross_harness_run_handles_success_and_hotplug_observer_failure(
    tmp_path: Path,
    observer_fails: bool,
) -> None:
    transaction = make_transaction("accepted")
    source = HarnessSessionRefV1(
        session_id="reasonix-source", role="worker", selector=transaction.source_selector,
        harness="reasonix", profile="flash", model_display="reasonix / flash",
    )
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    resume = ResumeContext(
        resumed_from_run_id="reasonix-source-run", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", role_selectors={"worker": transaction.target_selector},
        current_hotplug_transaction=transaction, pending_hotplug_transaction=transaction,
        active_role_sessions=(source,), hotplug_transaction_number=1,
    )
    class SourceDriver:
        capabilities = SessionCapabilities(
            session_identity=True, followup_turn=True, read_only_teardown=True,
            idempotent_turn_start=True,
        )
        handovers = 0
        starts = 0
        prompt = ""
        idempotency_key = None
        def build_full_context(self, run_dir):
            return {"plan_state": {"checkpoint": 1}, "workspace_facts": {"dirty": False}}
        def handover(self, request, prompt):
            self.handovers += 1
            return _valid_handover()
        def build_invocation(self, request):
            raise AssertionError("source driver must not start the target")

    class TargetDriver:
        capabilities = SessionCapabilities(session_identity=True, idempotent_turn_start=True)
        handovers = 0
        starts = 0
        prompt = ""
        idempotency_key = None
        def build_invocation(self, request):
            self.prompt = request.user_prompt
            self.idempotency_key = request.idempotency_key
            return HarnessInvocation(
                label="fake-codex", argv=("codex", "exec", "--json"), env={},
                prompt_mode="stdin", system_prompt=request.system_prompt,
                user_prompt=request.user_prompt, effective_prompt=request.user_prompt,
                stdin_text=request.user_prompt,
            )
        def parse_result(self, request, stdout, *, returncode=0):
            self.starts += 1
            return SessionResult(
                session_id="codex-target", selector=request.selector, model=request.model,
                effort=request.effort, final_output="DONE", capabilities=self.capabilities,
            )
    source_driver = SourceDriver()
    target_driver = TargetDriver()
    def runner(argv, **kwargs):
        plan.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "wire", "")
    class HotplugObserver(CollectingObserver):
        def on_event(self, event):
            super().on_event(event)
            if observer_fails and event.event_type == ExecutionEventType.HOTPLUG_APPLIED:
                raise KeyError("hotplug observer failed")

    observer = HotplugObserver()
    if observer_fails:
        with pytest.raises(WorkflowError) as ctx:
            run_workflow(
                ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
                _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
                runner=runner, session_driver=target_driver,
                source_session_driver=source_driver, resume=resume, observer=observer,
            )
        assert isinstance(ctx.value.__cause__, KeyError)
        state = json.loads((ctx.value.run_dir / "run.json").read_text(encoding="utf-8"))
        turn = json.loads(
            (ctx.value.run_dir / "turns" / "turn-001" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert state["status"] == "failed"
        assert turn["status"] == "harness-failed"
        assert state["turns_completed"] == 0
        return

    result = run_workflow(
        ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
        _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
        runner=runner, session_driver=target_driver,
        source_session_driver=source_driver, resume=resume, observer=observer,
    )
    state = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert source_driver.handovers == 1
    assert source_driver.starts == 0
    assert target_driver.handovers == 0
    assert target_driver.starts == 1
    assert target_driver.idempotency_key == transaction.transaction_id
    assert "Source worker handover:" in target_driver.prompt
    assert "## Objective And Checkpoint" in target_driver.prompt
    assert "Controller continuity context:" in target_driver.prompt
    for ref in state["hotplug_history"][-1]["artifact_paths"]:
        advertised = next(line.split(": ", 1)[1].split(" (sha256=", 1)[0]
                          for line in target_driver.prompt.splitlines()
                          if ref in line)
        assert Path(advertised).is_absolute()
        assert Path(advertised).exists()
        assert str(Path(advertised)) in target_driver.prompt
    assert state["hotplug_history"][-1]["stage"] == "applied"
    assert len(state["hotplug_history"][-1]["artifact_hashes"]) == 3
    hotplug_events = [event for event in observer.events if isinstance(event, HotplugEvent)]
    assert [event.event_type for event in hotplug_events] == [
        ExecutionEventType.HOTPLUG_STAGE_CHANGED,
        ExecutionEventType.HOTPLUG_APPLIED,
    ]


def test_cross_harness_target_failure_restores_source_session_active(tmp_path: Path) -> None:
    transaction = make_transaction("accepted")
    source = HarnessSessionRefV1(
        session_id="reasonix-source", role="worker", selector=transaction.source_selector,
        harness="reasonix", profile="flash", model_display="reasonix / flash",
    )
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    resume = ResumeContext(
        resumed_from_run_id="reasonix-source-run", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", role_selectors={"worker": transaction.target_selector},
        current_hotplug_transaction=transaction, pending_hotplug_transaction=transaction,
        active_role_sessions=(source,), hotplug_transaction_number=1,
    )

    class SourceDriver:
        capabilities = SessionCapabilities(followup_turn=True, read_only_teardown=True)
        def build_full_context(self, run_dir):
            return {"plan_state": {"checkpoint": 1}}
        def handover(self, request, prompt):
            return _valid_handover()

    class TargetDriver:
        capabilities = SessionCapabilities(session_identity=True, idempotent_turn_start=True)
        def build_invocation(self, request):
            return HarnessInvocation(
                label="fake-codex", argv=("codex", "exec", "--json"), env={},
                prompt_mode="stdin", system_prompt=request.system_prompt,
                user_prompt=request.user_prompt, effective_prompt=request.user_prompt,
                stdin_text=request.user_prompt,
            )
        def parse_result(self, request, stdout, *, returncode=0):
            raise RuntimeError("target start failed")

    with pytest.raises(WorkflowError):
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
            _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "wire", ""),
            session_driver=TargetDriver(), source_session_driver=SourceDriver(), resume=resume,
        )
    run_json = next((tmp_path / ".aflow" / "runs").glob("*/run.json"))
    state = json.loads(run_json.read_text(encoding="utf-8"))
    assert state["active_role_sessions"][0]["session_id"] == "reasonix-source"
    assert state["active_role_sessions"][0]["status"] == "active"
    assert state["hotplug_history"][-1]["stage"] == "failed"


def test_transaction_identity_is_digest_and_number_bound() -> None:
    first = make_transaction()
    second = HotplugTransactionV1(
        **{**first.to_dict(), "transaction_id": hotplug_transaction_id("run-1", "b" * 64, 2),
           "accepted_override_digest": "b" * 64, "transaction_number": 2}
    )
    assert first.transaction_id != second.transaction_id


def test_artifact_is_atomic_and_hash_bound(tmp_path: Path) -> None:
    relative, digest = write_hotplug_artifact(
        tmp_path, "hotplugs/hotplug-001/transaction.json", {"stage": "accepted"}
    )
    assert relative == "hotplugs/hotplug-001/transaction.json"
    assert len(digest) == 64
    assert hotplug_artifact_dir(tmp_path, 1).is_dir()
    assert (tmp_path / relative).read_text(encoding="utf-8") == '{\n  "stage": "accepted"\n}\n'
    with pytest.raises(FileExistsError):
        write_hotplug_artifact(tmp_path, relative, "duplicate")


@pytest.mark.parametrize("relative", ["/tmp/outside", "../outside", "a/../../outside"])
def test_artifact_paths_reject_escape(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        safe_hotplug_artifact_path(tmp_path, relative)


def test_history_is_bounded() -> None:
    assert len(bounded_hotplug_history([make_transaction() for _ in range(20)])) == 16


def test_hotplug_state_round_trips_and_malformed_state_fails_closed() -> None:
    transaction = make_transaction()
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.role_selectors = {"worker": "codex.high"}
    state.current_hotplug_transaction = transaction
    state.pending_hotplug_transaction = transaction
    state.hotplug_transaction_number = 1
    state.hotplug_history = [transaction]
    restored = hotplug_resume_fields(hotplug_state_payload(state))
    assert restored["role_selectors"] == {"worker": "codex.high"}
    assert restored["current_hotplug_transaction"] == transaction
    assert restored["hotplug_history"] == (transaction,)
    with pytest.raises(ValueError, match="schema version"):
        hotplug_resume_fields({"hotplug_schema_version": 99})
    with pytest.raises(ValueError, match="transaction number"):
        hotplug_resume_fields({
            **hotplug_state_payload(state), "hotplug_transaction_number": 0,
        })


def test_pending_waiting_for_recovery_round_trips_as_nonterminal_state() -> None:
    transaction = make_transaction("waiting_for_hotplug_recovery")
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.pending_hotplug_transaction = transaction
    state.hotplug_transaction_number = 1
    restored = hotplug_resume_fields(hotplug_state_payload(state))
    assert restored["pending_hotplug_transaction"] == transaction
    assert restored["pending_hotplug_transaction"].stage == "waiting_for_hotplug_recovery"


@pytest.mark.parametrize("stage", ["accepted", "target_preflighted", "source_finalized", "applied", "failed"])
def test_hotplug_resume_replays_only_safe_durable_stages(tmp_path: Path, stage: str) -> None:
    transaction = make_transaction(stage)
    assert classify_hotplug_resume_stage(tmp_path, transaction) == transaction


@pytest.mark.parametrize("stage", ["handover_starting", "target_starting"])
def test_hotplug_resume_marks_provider_boundary_ambiguous(tmp_path: Path, stage: str) -> None:
    transaction = make_transaction(stage)
    source = HarnessSessionRefV1(
        session_id="source", role="worker", selector=transaction.source_selector,
        harness=transaction.source_harness, profile=transaction.source_profile,
        model_display=transaction.source_model_display,
    )
    recovered = classify_hotplug_resume_stage(
        tmp_path, replace(transaction, source_session=source)
    )
    assert recovered.stage == "waiting_for_hotplug_recovery"
    assert "provider operation result" in (recovered.remediation or "")


def test_hotplug_resume_binds_and_copies_handover_artifacts_and_rejects_drift(tmp_path: Path) -> None:
    predecessor = tmp_path / "predecessor"
    successor = tmp_path / "successor"
    transaction = make_transaction("handover_ready")
    source = HarnessSessionRefV1(
        session_id="source", role="worker", selector=transaction.source_selector,
        harness=transaction.source_harness, profile=transaction.source_profile,
        model_display=transaction.source_model_display,
    )
    refs, hashes = write_handover_artifacts(
        predecessor, 1, build_handover_context_v1(transaction, {"plan_state": {"checkpoint": 1}}),
        _valid_handover(), {"plan_state": {"checkpoint": 1}},
    )
    bound = replace(transaction, source_session=source, artifact_paths=refs, artifact_hashes=hashes)
    copy_hotplug_resume_artifacts(predecessor, successor, bound)
    assert all((successor / ref).is_file() for ref in refs)
    (predecessor / refs[0]).write_text("corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        classify_hotplug_resume_stage(predecessor, bound)


def test_hotplug_resume_rejects_missing_source_session_and_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exact source session"):
        classify_hotplug_resume_stage(tmp_path, make_transaction("handover_ready"))
    source = HarnessSessionRefV1(
        session_id="source", role="worker", selector="reasonix.flash",
        harness="reasonix", profile="flash", model_display="reasonix / flash",
    )
    with pytest.raises(ValueError, match="exactly three"):
        classify_hotplug_resume_stage(
            tmp_path, replace(make_transaction("handover_ready"), source_session=source)
        )


def test_run_resume_ambiguous_target_start_never_launches_harness(tmp_path: Path) -> None:
    predecessor = tmp_path / ".aflow" / "runs" / "predecessor"
    predecessor.mkdir(parents=True)
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    transaction = make_transaction("target_starting")
    source = HarnessSessionRefV1(
        session_id="source", role="worker", selector=transaction.source_selector,
        harness=transaction.source_harness, profile=transaction.source_profile,
        model_display=transaction.source_model_display,
    )
    transaction = replace(transaction, source_session=source)
    resume = ResumeContext(
        resumed_from_run_id="predecessor", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", role_selectors={"worker": transaction.target_selector},
        current_hotplug_transaction=transaction, pending_hotplug_transaction=transaction,
        active_role_sessions=(source,), hotplug_transaction_number=1,
    )
    calls: list[object] = []
    with pytest.raises(WorkflowError, match="waiting_for_hotplug_recovery"):
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
            _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
            runner=lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args[0], 0, "", ""),
            resume=resume,
        )
    assert calls == []
    run_json = next((tmp_path / ".aflow" / "runs").glob("*/run.json"))
    persisted = json.loads(run_json.read_text(encoding="utf-8"))
    assert persisted["current_hotplug_transaction"]["stage"] == "waiting_for_hotplug_recovery"


def test_run_resume_applied_transaction_is_normalized_into_history(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    transaction = make_transaction("applied")
    target = HarnessSessionRefV1(
        session_id="codex-target", role="worker", selector=transaction.target_selector,
        harness="codex", profile="high", model_display="codex / high",
    )
    resume = ResumeContext(
        resumed_from_run_id="applied-predecessor", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", role_selectors={"worker": transaction.target_selector},
        current_hotplug_transaction=transaction, pending_hotplug_transaction=transaction,
        active_role_sessions=(target,), hotplug_transaction_number=1,
    )
    calls: list[object] = []
    with pytest.raises(WorkflowError) as error:
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
            _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "DONE", ""),
            resume=resume,
        )
    persisted = json.loads((error.value.run_dir / "run.json").read_text(encoding="utf-8"))
    assert calls
    assert persisted["current_hotplug_transaction"] is None
    assert persisted["pending_hotplug_transaction"] is None
    assert [item["transaction_id"] for item in persisted["hotplug_history"]].count(transaction.transaction_id) == 1


@pytest.mark.parametrize("evidence_case", ["valid", "wrong_selector", "failure", "empty_session", "missing_idempotency"])
def test_run_resume_imports_durable_provider_result_once(tmp_path: Path, evidence_case: str) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n", encoding="utf-8")
    base = make_transaction("waiting_for_hotplug_recovery")
    source = HarnessSessionRefV1(
        session_id="source", role="worker", selector=base.source_selector,
        harness=base.source_harness, profile=base.source_profile,
        model_display=base.source_model_display,
    )
    transaction = replace(base, source_session=source, provider_operation_id="provider-1", idempotency_key=base.transaction_id)
    resume = ResumeContext(
        resumed_from_run_id="provider-predecessor", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement", role_selectors={"worker": transaction.target_selector},
        current_hotplug_transaction=transaction, pending_hotplug_transaction=transaction,
        active_role_sessions=(source,), hotplug_transaction_number=1,
    )
    class ProviderDriver:
        capabilities = SessionCapabilities(session_identity=True, idempotent_turn_start=True)
        reconciliations = 0
        def reconcile_provider_operation(self, operation_id, idempotency_key):
            self.reconciliations += 1
            assert (operation_id, idempotency_key) == ("provider-1", transaction.transaction_id)
            if evidence_case == "wrong_selector":
                selector = "codex.other"
            else:
                selector = "codex.high"
            return SessionResult(
                session_id="" if evidence_case == "empty_session" else "codex-target",
                selector=selector, model="high-model", effort=None,
                final_output="DONE", provider_operation_id=operation_id,
                idempotency_key=(None if evidence_case == "missing_idempotency" else idempotency_key),
                capabilities=self.capabilities,
                failure="provider failed" if evidence_case == "failure" else None,
            )
        def build_invocation(self, request):
            return HarnessInvocation(
                label="fake", argv=("codex", "exec", "--json"), env={}, prompt_mode="stdin",
                system_prompt=request.system_prompt, user_prompt=request.user_prompt,
                effective_prompt=request.user_prompt, stdin_text=request.user_prompt,
            )
        def parse_result(self, request, stdout, *, returncode=0):
            return SessionResult(
                session_id="codex-target", selector=request.selector, model=request.model,
                effort=request.effort, final_output="DONE", capabilities=self.capabilities,
            )
    driver = ProviderDriver()
    with pytest.raises(WorkflowError) as error:
        run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=1),
            _controller_config(), "live", config_dir=tmp_path, adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
            session_driver=driver, resume=resume,
        )
    persisted = json.loads((error.value.run_dir / "run.json").read_text(encoding="utf-8"))
    assert driver.reconciliations == 1
    if evidence_case == "valid":
        assert persisted["current_hotplug_transaction"] is None
        assert persisted["hotplug_history"][-1]["provider_operation_id"] == "provider-1"
        assert persisted["role_selectors"]["worker"] == transaction.target_selector
        assert persisted["active_role_sessions"][-1]["session_id"] == "codex-target"
        assert persisted["active_role_sessions"][-1]["harness"] == transaction.target_harness
        assert persisted["active_role_sessions"][-1]["profile"] == transaction.target_profile
    else:
        assert persisted["current_hotplug_transaction"]["stage"] == "waiting_for_hotplug_recovery"
        assert persisted["active_role_sessions"][0]["session_id"] == "source"


@pytest.mark.parametrize("field", [
    "transaction_id", "run_id", "accepted_override_digest", "source_role",
    "source_selector", "source_harness", "source_profile", "source_model_display",
])
def test_transaction_reader_rejects_non_string_authority(field: str) -> None:
    payload = make_transaction().to_dict()
    payload[field] = 7
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload)


def test_session_reader_rejects_non_string_authority() -> None:
    payload = HarnessSessionRefV1(
        session_id="session-1", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    ).to_dict()
    payload["session_id"] = {"bad": True}
    with pytest.raises(ValueError):
        HarnessSessionRefV1.from_dict(payload)


@pytest.mark.parametrize("field", ["schema_version", "status"])
def test_modern_session_reader_requires_strict_discriminators(field: str) -> None:
    payload = HarnessSessionRefV1(
        session_id="session-1", role="worker", selector="codex.high",
        harness="codex", profile="high", model_display="codex / high",
    ).to_dict()
    payload.pop(field)
    with pytest.raises(ValueError):
        HarnessSessionRefV1.from_dict(payload, strict=True)
    payload[field] = True
    with pytest.raises(ValueError):
        HarnessSessionRefV1.from_dict(payload, strict=True)


@pytest.mark.parametrize("field", ["schema_version", "stage"])
def test_modern_transaction_reader_requires_strict_discriminators(field: str) -> None:
    payload = make_transaction().to_dict()
    payload.pop(field)
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload, strict=True)
    payload[field] = True
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload, strict=True)


@pytest.mark.parametrize("created_at", [
    "not-a-timestamp", "2026-08-09T00:00:00", "2026-08-09T00:00:00Z\ninvalid",
    "x" * 129,
])
def test_modern_transaction_reader_rejects_invalid_timestamps(created_at: str) -> None:
    payload = make_transaction().to_dict()
    payload["created_at"] = created_at
    with pytest.raises(ValueError):
        HotplugTransactionV1.from_dict(payload, strict=True)


def test_override_roles_are_strict_and_frozen_selector_bound(tmp_path: Path) -> None:
    path = tmp_path / "overrides.toml"
    path.write_text('[roles]\nworker = "codex.high"\n', encoding="utf-8")
    loaded = load_override_request(
        path,
        allowed_roles={"worker"},
        configured_selectors={"codex.high"},
    )
    assert loaded.status == "valid"
    assert loaded.request is not None
    assert loaded.request.role_selectors == {"worker": "codex.high"}


def test_override_roles_reject_unknown_role_or_selector(tmp_path: Path) -> None:
    path = tmp_path / "overrides.toml"
    path.write_text('[roles]\nreviewer = "codex.high"\n', encoding="utf-8")
    loaded = load_override_request(path, allowed_roles={"worker"})
    assert loaded.status == "invalid"
    path.write_text('[roles]\nworker = "codex.unknown"\n', encoding="utf-8")
    loaded = load_override_request(
        path, allowed_roles={"worker"}, configured_selectors={"codex.high"}
    )
    assert loaded.status == "invalid"


def test_selector_precedence_is_manager_then_run_local_then_team_then_global() -> None:
    config = WorkflowUserConfig(
        roles={"worker": "codex.low"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"low": HarnessProfileConfig(model="low")}
            ),
            "reasonix": WorkflowHarnessConfig(
                profiles={"flash": HarnessProfileConfig(model="flash")}
            ),
        },
        teams={"team": TeamConfig(roles={"worker": "reasonix.flash"})},
    )
    pending = PendingTeamOverride(
        target_step="implement", role="worker", source_team="team",
        target_team="team", selector="codex.low", checkpoint_identity=None,
        decision_number=1,
    )
    assert resolve_role_selector("worker", "team", config, step_name="implement", pending_team_override=pending) == "codex.low"
    assert resolve_role_selector("worker", "team", config, step_name="other", pending_team_override=pending, run_local_role_selectors={"worker": "reasonix.flash"}) == "reasonix.flash"
    assert resolve_role_selector("worker", "team", config) == "reasonix.flash"
    assert resolve_role_selector("worker", None, config) == "codex.low"


@pytest.mark.parametrize("source", ["notes = ['keep']\n[roles]\n", "team = 'team'\n[roles]\n"])
def test_explicit_empty_roles_are_rejected_in_mixed_requests(tmp_path: Path, source: str) -> None:
    path = tmp_path / "overrides.toml"
    path.write_text(source, encoding="utf-8")
    assert load_override_request(path).status == "invalid"

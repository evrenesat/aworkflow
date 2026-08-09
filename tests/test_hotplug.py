from pathlib import Path
from dataclasses import replace
import subprocess
import hashlib
import json

import pytest

from aflow.hotplug import (
    HOTPLUG_STAGES,
    HarnessSessionRefV1,
    HotplugTransactionV1,
    bounded_hotplug_history,
    hotplug_artifact_dir,
    hotplug_transaction_id,
    safe_hotplug_artifact_path,
    write_hotplug_artifact,
)
from aflow.plan import PlanSnapshot
from aflow.run_state import ControllerState, RetryContext, ResumeContext, hotplug_resume_fields, hotplug_state_payload, load_override_request
from aflow.config import GoTransition, HarnessProfileConfig, TeamConfig, WorkflowConfig, WorkflowHarnessConfig, WorkflowStepConfig, WorkflowUserConfig
from aflow.harnesses.codex import CodexAdapter
from aflow.harnesses.session import SessionRequest
from aflow.run_state import PendingTeamOverride
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, resolve_role_selector, run_workflow


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
    with pytest.raises(RuntimeError):
        run_workflow(ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=3), config, "live", config_dir=tmp_path, adapter=CodexAdapter(), runner=crashing_runner, session_driver=driver, control_source=control)
    run_dir = sorted((tmp_path / ".aflow" / "runs").glob("*"))[-1]
    persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
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

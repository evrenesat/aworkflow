from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from aflow.config import (
    AflowSection,
    ErrorHandlingConfig,
    GoTransition,
    HarnessErrorRecoveryConfig,
    HarnessErrorRecoveryRuleConfig,
    HarnessProfileConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.control_plane import (
    RecoveryEvidenceReference,
    RecoveryIntent,
    persist_recovery_intent,
    recovery_intent_digest,
)
from aflow.daemon import (
    DaemonError,
    _build_durable_recovery_brief,
    _prepare_recovery_resume_context,
    _recovery_fingerprint_plan_paths,
    _read_recovery_target_runtime,
    _validate_recovery_evidence_for_worker,
)
from aflow.harnesses.base import HarnessInvocation
from aflow.harnesses.codex import CodexAdapter
from aflow.harnesses.session import (
    SessionCapabilities,
    SessionRequest,
    SessionResult,
)
from aflow.hotplug import HarnessSessionRefV1, workspace_fingerprint
from aflow.plan import PlanSnapshot
from aflow.run_state import (
    ActiveImplementationScope,
    ControllerConfig,
    ControllerState,
    RecoverySessionContext,
    ResumeContext,
)
from aflow.runlog import (
    RunPaths,
    RunMetadataWriter,
    capture_checkpoint_evidence,
    capture_plan_evidence,
    create_run_paths,
)
from aflow.workflow import (
    WorkflowError,
    _append_durable_recovery_context,
    load_scope_evidence_for_resume,
    run_workflow,
)


def _workflow_config(*, with_review: bool = False) -> WorkflowUserConfig:
    steps = {
        "implement": WorkflowStepConfig(
            role="worker",
            prompts=("work",),
            go=(
                GoTransition(to="END", when="DONE"),
                GoTransition(to="implement"),
            ),
        )
    }
    roles = {
        "worker": "dsh.source",
        "reviewer": "dsh.reviewer",
    }
    profiles = {
        "source": HarnessProfileConfig(model="source-model"),
        "replacement": HarnessProfileConfig(model="replacement-model"),
        "reviewer": HarnessProfileConfig(model="review-model"),
    }
    if with_review:
        steps["review"] = WorkflowStepConfig(
            role="reviewer",
            prompts=("review",),
            go=(GoTransition(to="END", when="DONE"),),
        )
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=3),
        harnesses={
            "dsh": WorkflowHarnessConfig(profiles=dict(profiles)),
            "muse": WorkflowHarnessConfig(profiles=dict(profiles)),
        },
        roles=roles,
        workflows={
            "live": WorkflowConfig(
                steps=steps,
                first_step="implement",
            )
        },
        prompts={"work": "Work from the durable evidence.", "review": "Review the work."},
    )


def _intent(target_selector: str = "muse.replacement") -> RecoveryIntent:
    return RecoveryIntent(
        source_run_id="source-run",
        target_run_id="target-run",
        source_selector="dsh.source",
        target_selector=target_selector,
        evidence=(
            RecoveryEvidenceReference(
                path="plan.md",
                sha256="0" * 64,
                size=0,
            ),
        ),
    )


def _recovery_context(
    *,
    target_selector: str = "muse.replacement",
    operation_state: str = "pending",
) -> RecoverySessionContext:
    intent = _intent(target_selector)
    return RecoverySessionContext(
        intent=intent,
        brief=(
            "## Durable provider-recovery evidence\n"
            "Fresh replacement session; source session context is unavailable."
        ),
        intent_digest=recovery_intent_digest(intent),
        consumed=operation_state == "consumed",
        operation_state=operation_state,
    )


def _source_session() -> HarnessSessionRefV1:
    return HarnessSessionRefV1(
        session_id="source-private-session",
        role="worker",
        selector="dsh.source",
        harness="dsh",
        profile="source",
        model_display="dsh / source-model",
    )


def _runtime_marker(
    target_run_id: str,
    *,
    operation_state: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "durable_evidence",
        "source_run_id": "older-source",
        "target_run_id": target_run_id,
        "source_selector": "dsh.source",
        "target_selector": "muse.replacement",
        "intent_digest": "a" * 64,
        "brief_sha256": "b" * 64,
        "source_session_context_transferred": False,
        "consumed": operation_state == "consumed",
        "operation_state": operation_state,
    }


def _canonical_resume_source(
    tmp_path: Path,
    *,
    runtime: dict[str, object] | None = None,
    session: HarnessSessionRefV1 | None = None,
) -> tuple[RunPaths, Path, dict[str, object]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan, max_turns=3)
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    if session is not None:
        state.role_selectors = {"worker": session.selector}
        state.active_role_sessions = (session,)
    paths = create_run_paths(config)
    RunMetadataWriter(
        paths=paths,
        config=config,
        state=state,
        workflow_name="live",
    ).write(
        status="failed",
        original_plan_path=plan,
        active_plan_path=plan,
        current_step_name="implement",
    )
    payload = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload["effective_max_turns"] == 3
    assert payload["hotplug_schema_version"] == 1
    assert payload["active_role_sessions"] == (
        [session.to_dict()] if session is not None else []
    )
    if runtime is not None:
        payload["recovery_runtime"] = runtime
        paths.run_json.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
    return paths, plan, payload


@dataclass
class _FakeTargetDriver:
    harness: str
    outputs: list[str]
    requests: list[SessionRequest] = field(default_factory=list)

    capabilities: SessionCapabilities = SessionCapabilities(
        session_identity=True,
        followup_turn=True,
        resume_with_model=True,
        idempotent_turn_start=True,
    )

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        self.requests.append(request)
        prompt = f"{request.system_prompt}\n\n{request.user_prompt}"
        return HarnessInvocation(
            label=f"fake-{self.harness}",
            argv=(f"fake-{self.harness}",),
            env={},
            prompt_mode="stdin",
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            effective_prompt=prompt,
            stdin_text=prompt,
        )

    def parse_result(
        self,
        request: SessionRequest,
        _stdout: str,
        *,
        returncode: int = 0,
    ) -> SessionResult:
        output = self.outputs.pop(0)
        return SessionResult(
            session_id=request.session_id or f"{self.harness}-target-1",
            selector=request.selector,
            model=request.model,
            effort=request.effort,
            final_output=output,
            capabilities=self.capabilities,
            failure=(f"return code {returncode}" if returncode else None),
        )


class _UnavailableSourceDriver:
    capabilities = SessionCapabilities(
        session_identity=False,
        resume_with_model=False,
        read_only_teardown=False,
    )

    def build_invocation(self, _request: SessionRequest) -> HarnessInvocation:
        raise AssertionError("the unavailable source driver must not be called")

    def parse_result(self, *_args, **_kwargs):
        raise AssertionError("the unavailable source driver must not be called")


@pytest.mark.parametrize(
    ("target_selector", "target_harness"),
    (("dsh.replacement", "dsh"), ("muse.replacement", "muse")),
)
def test_recovery_starts_fresh_target_and_then_uses_only_target_session(
    tmp_path: Path,
    target_selector: str,
    target_harness: str,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    driver = _FakeTargetDriver(target_harness, ["continue", "DONE"])
    source_driver = _UnavailableSourceDriver()
    resume = ResumeContext(
        resumed_from_run_id="source-run",
        feature_branch=None,
        worktree_path=None,
        main_branch=None,
        setup=(),
        teardown=(),
        active_plan_path=plan,
        interrupted_step_name="implement",
        role_selectors={"worker": "dsh.source"},
        active_role_sessions=(_source_session(),),
        recovery_context=_recovery_context(target_selector=target_selector),
    )

    def runner(argv, **kwargs):
        if len(driver.requests) == 2:
            plan.write_text(
                "# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(argv, 0, "transport", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=tmp_path,
            plan_path=plan,
            max_turns=2,
            reserved_run_id="target-run",
            idempotency_key="recovery-test",
            caller_scope="test",
        ),
        _workflow_config(),
        "live",
        config_dir=tmp_path,
        adapter=CodexAdapter(),
        runner=runner,
        session_driver=driver,
        source_session_driver=source_driver,
        resume=resume,
        snapshot_config=False,
    )

    assert result.final_snapshot.is_complete
    assert [request.session_id for request in driver.requests] == [
        None,
        f"{target_harness}-target-1",
    ]
    assert driver.requests[0].idempotency_key == resume.recovery_context.intent_digest
    assert driver.requests[1].idempotency_key is None
    assert "source-private-session" not in driver.requests[0].user_prompt
    assert "Durable provider-recovery evidence" in driver.requests[0].user_prompt
    assert "Durable provider-recovery evidence" not in driver.requests[1].user_prompt

    state = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["recovery_runtime"]["operation_state"] == "consumed"
    assert state["recovery_runtime"]["consumed"] is True
    assert state["role_selectors"]["worker"] == target_selector
    assert state["active_role_sessions"][0]["session_id"] == f"{target_harness}-target-1"
    assert state["resumed_from_run_id"] == "source-run"
    assert state["active_plan_path"] == str(plan)


def test_recovery_target_failure_does_not_trigger_legacy_automatic_failover(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    driver = _FakeTargetDriver("muse", ["provider failure"])
    resume = ResumeContext(
        resumed_from_run_id="source-run",
        feature_branch=None,
        worktree_path=None,
        main_branch=None,
        setup=(),
        teardown=(),
        active_plan_path=plan,
        interrupted_step_name="implement",
        role_selectors={"worker": "dsh.source"},
        active_role_sessions=(_source_session(),),
        recovery_context=_recovery_context(),
    )

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 17, "provider failure", "")

    workflow_config = _workflow_config()
    workflow_config = WorkflowUserConfig(
        aflow=workflow_config.aflow,
        harnesses=workflow_config.harnesses,
        roles=workflow_config.roles,
        manager=workflow_config.manager,
        workflows=workflow_config.workflows,
        prompts=workflow_config.prompts,
        error_handling=ErrorHandlingConfig(
            harness_error_recovery=HarnessErrorRecoveryConfig(
                rules=(
                    HarnessErrorRecoveryRuleConfig(
                        action="retry_same_team_after_delay",
                        match=("provider failure",),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(WorkflowError, match="exited with code 17"):
        run_workflow(
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan,
                max_turns=2,
                reserved_run_id="target-run",
                idempotency_key="recovery-failure-test",
                caller_scope="test",
            ),
            workflow_config,
            "live",
            config_dir=tmp_path,
            adapter=CodexAdapter(),
            runner=runner,
            session_driver=driver,
            resume=resume,
            snapshot_config=False,
        )
    assert len(driver.requests) == 1


def test_recovery_pending_review_keeps_review_context_and_does_not_consume(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    reviewer = HarnessSessionRefV1(
        session_id="review-session",
        role="reviewer",
        selector="dsh.reviewer",
        harness="dsh",
        profile="reviewer",
        model_display="dsh / review-model",
    )
    resume = ResumeContext(
        resumed_from_run_id="source-run",
        feature_branch=None,
        worktree_path=None,
        main_branch=None,
        setup=(),
        teardown=(),
        active_plan_path=plan,
        interrupted_step_name="review",
        role_selectors={"worker": "dsh.source"},
        active_role_sessions=(_source_session(), reviewer),
        recovery_context=_recovery_context(),
    )

    seen_prompts: list[str] = []

    def runner(argv, **kwargs):
        seen_prompts.append(str(kwargs.get("input", "")))
        plan.write_text(
            "# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "DONE", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=tmp_path,
            plan_path=plan,
            max_turns=1,
            reserved_run_id="target-run",
            idempotency_key="review-test",
            caller_scope="test",
        ),
        _workflow_config(with_review=True),
        "live",
        config_dir=tmp_path,
        adapter=CodexAdapter(),
        runner=runner,
        resume=resume,
        snapshot_config=False,
    )

    state = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["recovery_runtime"]["operation_state"] == "pending"
    assert state["recovery_runtime"]["consumed"] is False
    assert [item["session_id"] for item in state["active_role_sessions"]] == [
        "review-session"
    ]
    assert seen_prompts and "Durable provider-recovery evidence" not in seen_prompts[0]


def test_recovery_consumption_and_session_identity_share_one_durable_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aflow.cli import _reconstruct_resume_context

    class _SimulatedCrash(BaseException):
        pass

    def setup_root(root: Path) -> tuple[Path, ResumeContext]:
        root.mkdir()
        plan = root / "plan.md"
        plan.write_text(
            "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
            encoding="utf-8",
        )
        return plan, ResumeContext(
            resumed_from_run_id="source-run",
            feature_branch=None,
            worktree_path=None,
            main_branch=None,
            setup=(),
            teardown=(),
            active_plan_path=plan,
            interrupted_step_name="implement",
            role_selectors={"worker": "dsh.source"},
            active_role_sessions=(_source_session(),),
            recovery_context=_recovery_context(),
        )

    def is_combined_consumption_write(writer: RunMetadataWriter) -> bool:
        state = writer.state
        return bool(
            state is not None
            and state.recovery_operation_state == "consumed"
            and any(
                session.selector == "muse.replacement"
                for session in state.active_role_sessions
            )
        )

    original_write = RunMetadataWriter.write
    before_combined: dict[str, object] = {}
    before_root = tmp_path / "before"
    before_plan, before_resume = setup_root(before_root)

    def crash_before_combined(self, *args, **kwargs):
        if is_combined_consumption_write(self):
            before_combined.update(
                json.loads(self.paths.run_json.read_text(encoding="utf-8"))
            )
            raise _SimulatedCrash()
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(RunMetadataWriter, "write", crash_before_combined)
    with pytest.raises(_SimulatedCrash):
        run_workflow(
            ControllerConfig(
                repo_root=before_root,
                plan_path=before_plan,
                max_turns=1,
                reserved_run_id="target-run",
                idempotency_key="before-consumed-write",
                caller_scope="test",
            ),
            _workflow_config(),
            "live",
            config_dir=before_root,
            adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "transport", ""
            ),
            session_driver=_FakeTargetDriver("muse", ["DONE"]),
            source_session_driver=_UnavailableSourceDriver(),
            resume=before_resume,
            snapshot_config=False,
        )

    assert before_combined["recovery_runtime"]["operation_state"] == "in_flight"
    assert before_combined["active_role_sessions"] == []
    before_payload = json.loads(
        (before_root / ".aflow" / "runs" / "target-run" / "run.json").read_text(
            encoding="utf-8"
        )
    )
    with pytest.raises(ValueError, match="unknown liveness"):
        _reconstruct_resume_context(
            resolved_run_id=before_root / ".aflow" / "runs" / "target-run",
            run_dir=before_root / ".aflow" / "runs" / "target-run",
            prev_run=before_payload,
            plan_path=before_plan,
            frozen_run_identity=None,
            reset_scope=False,
            require_resume=True,
            workflow_steps=_workflow_config().workflows["live"].steps,
        )

    monkeypatch.setattr(RunMetadataWriter, "write", original_write)
    after_combined: dict[str, object] = {}
    after_root = tmp_path / "after"
    after_plan, after_resume = setup_root(after_root)

    def crash_after_combined(self, *args, **kwargs):
        if is_combined_consumption_write(self):
            result = original_write(self, *args, **kwargs)
            after_combined.update(
                json.loads(self.paths.run_json.read_text(encoding="utf-8"))
            )
            raise _SimulatedCrash()
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(RunMetadataWriter, "write", crash_after_combined)
    with pytest.raises(_SimulatedCrash):
        run_workflow(
            ControllerConfig(
                repo_root=after_root,
                plan_path=after_plan,
                max_turns=1,
                reserved_run_id="target-run",
                idempotency_key="after-consumed-write",
                caller_scope="test",
            ),
            _workflow_config(),
            "live",
            config_dir=after_root,
            adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "transport", ""
            ),
            session_driver=_FakeTargetDriver("muse", ["DONE"]),
            source_session_driver=_UnavailableSourceDriver(),
            resume=after_resume,
            snapshot_config=False,
        )

    assert after_combined["recovery_runtime"]["operation_state"] == "consumed"
    assert after_combined["recovery_runtime"]["consumed"] is True
    assert [
        session["session_id"] for session in after_combined["active_role_sessions"]
    ] == ["muse-target-1"]

    monkeypatch.setattr(RunMetadataWriter, "write", original_write)
    after_run_dir = after_root / ".aflow" / "runs" / "target-run"
    after_payload = json.loads(
        (after_run_dir / "run.json").read_text(encoding="utf-8")
    )
    resumed_context = _reconstruct_resume_context(
        resolved_run_id=after_run_dir,
        run_dir=after_run_dir,
        prev_run=after_payload,
        plan_path=after_plan,
        frozen_run_identity=None,
        reset_scope=False,
        require_resume=True,
        workflow_steps=_workflow_config().workflows["live"].steps,
    )
    assert resumed_context is not None
    assert resumed_context.recovery_context is None
    assert [session.session_id for session in resumed_context.active_role_sessions] == [
        "muse-target-1"
    ]

    continuation_driver = _FakeTargetDriver("muse", ["DONE"])

    def finish_continuation(argv, **kwargs):
        after_plan.write_text(
            "# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "transport", "")

    run_workflow(
        ControllerConfig(
            repo_root=after_root,
            plan_path=after_plan,
            max_turns=1,
            reserved_run_id="resumed-run",
            idempotency_key="ordinary-after-crash",
            caller_scope="test",
        ),
        _workflow_config(),
        "live",
        config_dir=after_root,
        adapter=CodexAdapter(),
        runner=finish_continuation,
        session_driver=continuation_driver,
        source_session_driver=_UnavailableSourceDriver(),
        resume=resumed_context,
        snapshot_config=False,
    )
    assert [request.session_id for request in continuation_driver.requests] == [
        "muse-target-1"
    ]
    assert continuation_driver.requests[0].idempotency_key is None


def test_recovery_unknown_inflight_operation_rejects_before_provider_launch(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unknown recovery liveness must fail before launch")

    resume = ResumeContext(
        resumed_from_run_id="source-run",
        feature_branch=None,
        worktree_path=None,
        main_branch=None,
        setup=(),
        teardown=(),
        active_plan_path=plan,
        interrupted_step_name="implement",
        recovery_context=_recovery_context(operation_state="in_flight"),
    )
    with pytest.raises(WorkflowError, match="unknown liveness"):
        run_workflow(
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan,
                max_turns=1,
                reserved_run_id="target-run",
                idempotency_key="inflight-test",
                caller_scope="test",
            ),
            _workflow_config(),
            "live",
            config_dir=tmp_path,
            adapter=CodexAdapter(),
            runner=runner,
            resume=resume,
            snapshot_config=False,
        )
    assert calls == []


@pytest.mark.parametrize(
    "runtime",
    ("in_flight", "pending", "malformed"),
    ids=("in-flight", "pending", "malformed"),
)
def test_canonical_ordinary_resume_rejects_unresolved_runtime(
    tmp_path: Path,
    runtime: str,
) -> None:
    from aflow.cli import _reconstruct_resume_context

    paths, plan, payload = _canonical_resume_source(tmp_path)
    marker = (
        _runtime_marker(paths.run_dir.name, operation_state=runtime)
        if runtime in {"in_flight", "pending"}
        else {"operation_state": "in_flight"}
    )
    payload["recovery_runtime"] = marker
    paths.run_json.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    payload = json.loads(paths.run_json.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="unknown liveness"):
        _reconstruct_resume_context(
            resolved_run_id=paths.run_dir,
            run_dir=paths.run_dir,
            prev_run=payload,
            plan_path=plan,
            frozen_run_identity=None,
            reset_scope=False,
            require_resume=True,
            workflow_steps=_workflow_config().workflows["live"].steps,
        )


def test_canonical_ordinary_resume_keeps_consumed_session_and_no_metadata_resume(
    tmp_path: Path,
) -> None:
    from aflow.cli import _reconstruct_resume_context

    session = HarnessSessionRefV1(
        session_id="replacement-session",
        role="worker",
        selector="muse.replacement",
        harness="muse",
        profile="replacement",
        model_display="muse / replacement-model",
    )
    paths, plan, payload = _canonical_resume_source(tmp_path, session=session)
    payload["recovery_runtime"] = _runtime_marker(
        paths.run_dir.name,
        operation_state="consumed",
    )
    paths.run_json.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    payload = json.loads(paths.run_json.read_text(encoding="utf-8"))

    consumed_context = _reconstruct_resume_context(
        resolved_run_id=paths.run_dir,
        run_dir=paths.run_dir,
        prev_run=payload,
        plan_path=plan,
        frozen_run_identity=None,
        reset_scope=False,
        require_resume=True,
        workflow_steps=_workflow_config().workflows["live"].steps,
    )
    assert consumed_context is not None
    assert consumed_context.recovery_context is None
    assert consumed_context.role_selectors == {"worker": "muse.replacement"}
    assert consumed_context.active_role_sessions == (session,)
    assert consumed_context.effective_max_turns == 3

    plain_paths, plain_plan, plain_payload = _canonical_resume_source(
        tmp_path / "plain"
    )
    plain_context = _reconstruct_resume_context(
        resolved_run_id=plain_paths.run_dir,
        run_dir=plain_paths.run_dir,
        prev_run=plain_payload,
        plan_path=plain_plan,
        frozen_run_identity=None,
        reset_scope=False,
        require_resume=True,
        workflow_steps=_workflow_config().workflows["live"].steps,
    )
    assert plain_context is not None
    assert plain_context.recovery_context is None
    assert plain_context.active_role_sessions == ()
    assert plain_context.effective_max_turns == 3


@pytest.mark.parametrize("write_run_json", (True, False))
def test_recovery_target_turn_artifact_without_marker_rejects_unknown_liveness(
    tmp_path: Path,
    write_run_json: bool,
) -> None:
    target_dir = tmp_path / ".aflow" / "runs" / "target-run"
    turn_dir = target_dir / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    if write_run_json:
        (target_dir / "run.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DaemonError, match="prior turn evidence"):
        _read_recovery_target_runtime(
            target_dir,
            intent=_intent(),
            intent_digest=recovery_intent_digest(_intent()),
        )


def test_schema_v2_scope_markdown_is_included_in_recovery_workspace_fingerprint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main", str(root)), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.name", "AFlow Test"), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.email", "aflow-test@example.invalid"), check=True)
    (root / ".gitignore").write_text(".aflow/\n", encoding="utf-8")
    plan = root / "plan.md"
    plan_text = (
        "# Plan\n\n"
        "### [x] Checkpoint 1: Completed\n"
        "- [x] setup\n\n"
        "### [ ] Checkpoint 2: Replacement\n"
        "- [ ] replace\n"
    )
    plan.write_text(plan_text, encoding="utf-8")
    subprocess.run(("git", "-C", str(root), "add", ".gitignore", "plan.md"), check=True)
    subprocess.run(
        (
            "git", "-C", str(root), "-c", "user.name=AFlow Test",
            "-c", "user.email=aflow-test@example.invalid", "commit", "-q", "-m", "fixture",
        ),
        check=True,
    )
    worktree = tmp_path / "execution"
    subprocess.run(
        ("git", "-C", str(root), "worktree", "add", "-q", "-b", "recovery-worktree", str(worktree), "HEAD"),
        check=True,
    )

    source_dir = root / ".aflow" / "runs" / "source-run"
    source_dir.mkdir(parents=True)
    paths = RunPaths(
        repo_root=root,
        runs_root=root / ".aflow" / "runs",
        run_dir=source_dir,
        turns_dir=source_dir / "turns",
        manager_dir=source_dir / "manager",
        run_json=source_dir / "run.json",
    )
    plan_ref = capture_plan_evidence(paths, plan_text)
    checkpoint_text = "### [ ] Checkpoint 2: Replacement\n- [ ] replace\n"
    checkpoint_ref = capture_checkpoint_evidence(paths, checkpoint_text)
    from aflow.repartition import (
        EvidenceArtifactReferenceV2,
        create_envelope_v2,
        write_envelope_atomic,
    )

    envelope = create_envelope_v2(
        scope_id="plan.md::checkpoint-2::Replacement",
        original_plan_path=plan,
        plan_text=plan_text,
        checkpoint_index=2,
        repo_root=root,
        plan_ref=EvidenceArtifactReferenceV2(
            kind=plan_ref.kind,
            path=plan_ref.path,
            sha256=plan_ref.sha256,
            byte_size=plan_ref.byte_size,
        ),
        checkpoint_ref=EvidenceArtifactReferenceV2(
            kind=checkpoint_ref.kind,
            path=checkpoint_ref.path,
            sha256=checkpoint_ref.sha256,
            byte_size=checkpoint_ref.byte_size,
        ),
    )
    envelope_path = write_envelope_atomic(
        envelope,
        source_dir / "scopes" / envelope.scope_digest,
    )
    envelope_bytes = envelope_path.read_bytes()
    scope = ActiveImplementationScope(
        scope_id=envelope.scope_id,
        original_plan_path=str(plan),
        checkpoint_index=2,
        checkpoint_name=envelope.checkpoint_name,
        opened_turn_number=1,
        envelope_artifact_path=envelope_path.relative_to(source_dir).as_posix(),
        envelope_artifact_sha256=hashlib.sha256(envelope_bytes).hexdigest(),
        envelope_canonical_sha256=envelope.canonical_envelope_sha256,
    )
    artifacts = load_scope_evidence_for_resume(source_dir, scope, envelope_bytes)
    artifact_paths = [source_dir / relative for relative in artifacts]
    file_paths = [plan, *artifact_paths]
    evidence = tuple(
        RecoveryEvidenceReference(
            path=path.resolve().relative_to(root.resolve()).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size=path.stat().st_size,
        )
        for path in file_paths
    )
    intent = RecoveryIntent(
        source_run_id="source-run",
        target_run_id="target-run",
        source_selector="dsh.source",
        target_selector="muse.replacement",
        evidence=(
            *evidence,
            RecoveryEvidenceReference(
                path=f"worktree:{worktree.resolve()}",
                sha256=workspace_fingerprint(
                    worktree,
                    _recovery_fingerprint_plan_paths(file_paths),
                )["sha256"],
                kind="workspace",
            ),
        ),
    )

    evidence_paths, workspace_evidence = _validate_recovery_evidence_for_worker(
        root,
        intent,
    )
    assert set(evidence_paths) == set(file_paths)
    assert workspace_evidence["workspace"] == worktree.resolve()
    assert tuple(_recovery_fingerprint_plan_paths(file_paths)) == tuple(
        path for path in file_paths if path.suffix.lower() == ".md"
    )

    artifact_paths[0].write_bytes(b"changed scope evidence")
    with pytest.raises(DaemonError, match="evidence bytes changed"):
        _validate_recovery_evidence_for_worker(root, intent)


def test_recovery_worker_rechecks_exact_evidence_and_builds_reference_only_brief(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    source_dir = tmp_path / ".aflow" / "runs" / "source-run"
    target_dir = tmp_path / ".aflow" / "runs" / "target-run"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    source_run = {
        "status": "failed",
        "workflow_name": "live",
        "role_selectors": {"worker": "dsh.source"},
        "api_token": "PRIVATE-SOURCE-CREDENTIAL",
    }
    source_run_path = source_dir / "run.json"
    source_run_path.write_text(json.dumps(source_run, sort_keys=True), encoding="utf-8")
    turn_dir = source_dir / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    (turn_dir / "result.json").write_text(
        json.dumps(
            {
                "turn_number": 1,
                "step_name": "implement",
                "step_role": "worker",
                "selector": "dsh.source",
                "status": "harness-failed",
                "returncode": 17,
            }
        ),
        encoding="utf-8",
    )
    fingerprint = workspace_fingerprint(tmp_path, (plan,))
    evidence = (
        RecoveryEvidenceReference(
            path=".aflow/runs/source-run/run.json",
            sha256=hashlib.sha256(source_run_path.read_bytes()).hexdigest(),
            size=source_run_path.stat().st_size,
        ),
        RecoveryEvidenceReference(
            path="plan.md",
            sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
            size=plan.stat().st_size,
        ),
        RecoveryEvidenceReference(
            path=f"worktree:{tmp_path.resolve()}",
            sha256=str(fingerprint["sha256"]),
            size=0,
            kind="workspace",
        ),
    )
    intent = RecoveryIntent(
        source_run_id="source-run",
        target_run_id="target-run",
        source_selector="dsh.source",
        target_selector="muse.replacement",
        evidence=evidence,
    )
    artifact_path, artifact_sha256 = persist_recovery_intent(target_dir, intent)
    record = {
        "run_id": "target-run",
        "resumed_from_run_id": "source-run",
        "recovery": {
            "mode": "durable_evidence",
            "worker_selector": "muse.replacement",
        },
        "recovery_intent_digest": recovery_intent_digest(intent),
        "recovery_artifact_path": artifact_path,
        "recovery_artifact_sha256": artifact_sha256,
    }
    bootstrap = SimpleNamespace(
        plan_path=plan,
        resume_context=ResumeContext(
            resumed_from_run_id="source-run",
            feature_branch=None,
            worktree_path=None,
            main_branch=None,
            setup=(),
            teardown=(),
            active_plan_path=plan,
        ),
    )

    prepared_context = _prepare_recovery_resume_context(
        record=record,
        repo_root=tmp_path,
        run_id="target-run",
        workflow_config=_workflow_config(),
        bootstrap=bootstrap,
    )

    assert prepared_context.recovery_context.operation_state == "pending"
    brief = prepared_context.recovery_context.brief
    assert "Original/active plans" in brief
    assert "Last finalized worker/reviewer outcome" in brief
    assert "Hidden source-session context is unavailable" in brief
    assert "PRIVATE-SOURCE-CREDENTIAL" not in brief
    assert "source-run/run.json" in brief
    assert f"Exact execution worktree: {tmp_path}" in brief
    assert f"Exact worktree HEAD: {fingerprint['head']}" in brief

    oversized_intent = RecoveryIntent(
        source_run_id="source-run",
        target_run_id="target-run",
        source_selector="dsh.source",
        target_selector="muse.replacement",
        evidence=tuple(
            RecoveryEvidenceReference(
                path=f"artifacts/{index}-{'x' * 1800}",
                sha256="0" * 64,
                size=0,
            )
            for index in range(32)
        ),
    )
    bounded_brief = _build_durable_recovery_brief(
        repo_root=tmp_path,
        source_dir=source_dir,
        source_run_json=source_run,
        intent=oversized_intent,
        context=bootstrap.resume_context,
        plan_path=plan,
        evidence_paths=(source_run_path, plan),
        workspace_evidence={
            "workspace": tmp_path,
            "workspace_fingerprint": fingerprint,
        },
    )
    assert len(bounded_brief.encode("utf-8")) <= 16 * 1024
    assert "additional evidence reference(s) omitted" in bounded_brief

    plan.write_text(plan.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    with pytest.raises(DaemonError, match="evidence bytes changed|worktree evidence changed"):
        _prepare_recovery_resume_context(
            record=record,
            repo_root=tmp_path,
            run_id="target-run",
            workflow_config=_workflow_config(),
            bootstrap=bootstrap,
        )


def test_recovery_prompt_helper_skips_reviewers_and_consumed_workers() -> None:
    context = _recovery_context()
    assert _append_durable_recovery_context(
        "review", step_role="reviewer", recovery_context=context
    ) == "review"
    consumed = _recovery_context(operation_state="consumed")
    assert _append_durable_recovery_context(
        "work", step_role="worker", recovery_context=consumed
    ) == "work"

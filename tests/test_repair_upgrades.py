from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest

from aflow.cli import _pending_finalized_resume_turn
from aflow.config import (
    GoTransition,
    HarnessProfileConfig,
    ManagerConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
    resolve_team_config,
)
from aflow.harnesses.codex import CodexAdapter
from aflow.manager import determine_repair_upgrade_policy
from aflow.run_state import (
    ControllerConfig,
    ControllerState,
    ImplementationAttempt,
    PlanSnapshot,
    ReviewRejectionRecord,
    ResumeContext,
    _mark_validated_resume_context,
    manager_resume_fields,
    manager_resume_fields_strict,
)
from aflow.runlog import RunMetadataWriter
from aflow.workflow import (
    _append_replayed_review_rejection,
    load_scope_evidence_for_resume,
    run_workflow,
)


_PLAN = """# Plan

### [ ] Checkpoint 1: First
- [ ] implement the first checkpoint
"""
_COMPLETE_PLAN = """# Plan

### [x] Checkpoint 1: First
- [x] implement the first checkpoint
"""
_REPAIR_PLAN = """# Repair

### [ ] Checkpoint 1: Repair
- [ ] repair the first checkpoint
"""


def _workflow_config(*, manager_enabled: bool, threshold: int) -> WorkflowUserConfig:
    workflow = WorkflowConfig(
        manager_enabled=manager_enabled,
        upgrade_after_repairs=threshold,
        team="base",
        first_step="implement",
        steps={
            "implement": WorkflowStepConfig(
                role="worker",
                prompts=("p",),
                go=(
                    GoTransition(to="END", when="DONE"),
                    GoTransition(to="review"),
                ),
            ),
            "review": WorkflowStepConfig(
                role="reviewer",
                prompts=("p",),
                go=(
                    GoTransition(to="END", when="DONE"),
                    GoTransition(to="implement"),
                ),
            ),
        },
    )
    profiles = {
        name: HarnessProfileConfig(model=name)
        for name in ("worker-base", "worker-high", "reviewer", "final-reviewer", "manager")
    }
    base_roles = {
        "worker": "codex.worker-base",
        "reviewer": "codex.reviewer",
        "final_reviewer": "codex.final-reviewer",
    }
    if manager_enabled:
        base_roles["manager"] = "codex.manager"
    return WorkflowUserConfig(
        harnesses={"codex": WorkflowHarnessConfig(profiles=profiles)},
        teams={
            "base": TeamConfig(roles=base_roles, upgrade_to="high"),
            # The reviewer role is inherited from base, so an implementation
            # upgrade cannot silently retarget review.
            "high": TeamConfig(
                roles={"worker": "codex.worker-high"},
                extends="base",
            ),
        },
        workflows={"repair": workflow},
        prompts={"p": "{ACTIVE_PLAN_PATH}"},
        manager=(
            ManagerConfig(lite_role="manager", full_role="manager")
            if manager_enabled
            else ManagerConfig()
        ),
    )


def _rejection(
    *,
    scope_id: str = "scope",
    number: int,
    reviewed_turn: int,
    review_turn: int,
    team: str | None = "base",
    selector: str | None = "codex.worker-base",
    attempt_ordinal: int | None = None,
) -> ReviewRejectionRecord:
    return ReviewRejectionRecord(
        scope_id=scope_id,
        rejection_number=number,
        source_run_id="run",
        review_turn_number=review_turn,
        review_step_name="review",
        reviewer_selector="codex.reviewer",
        checkpoint_index=1,
        checkpoint_name="First",
        reviewed_implementation_turn_number=reviewed_turn,
        reviewed_worker_team=team,
        reviewed_worker_selector=selector,
        review_summary="rejected",
        repair_plan_summary="repair required",
        review_stdout_artifact_path="turns/review/stdout.txt",
        repair_plan_path="plan-cp01-v01.md",
        reviewed_attempt_ordinal=attempt_ordinal,
    )


def _attempt(
    turn: int,
    team: str = "base",
    *,
    outcome: str = "progress",
    ordinal: int | None = None,
) -> ImplementationAttempt:
    return ImplementationAttempt(
        turn_number=turn,
        step_name="implement",
        role="worker",
        team=team,
        selector=f"codex.worker-{team}",
        outcome=outcome,
        attempt_ordinal=ordinal,
    )


def _policy(
    *,
    threshold: int,
    attempts: list[ImplementationAttempt],
    rejections: list[ReviewRejectionRecord],
    scope_id: str = "scope",
    teams: dict[str, TeamConfig] | None = None,
):
    workflow = WorkflowConfig(upgrade_after_repairs=threshold)
    config = WorkflowUserConfig(
        teams=teams
        or {
            "base": TeamConfig(roles={"worker": "codex.worker-base"}, upgrade_to="high"),
            "high": TeamConfig(roles={"worker": "codex.worker-high"}, upgrade_to="max"),
            "max": TeamConfig(roles={"worker": "codex.worker-max"}),
        },
        workflows={"repair": workflow},
    )
    return determine_repair_upgrade_policy(
        config,
        threshold=threshold,
        role="worker",
        baseline_team="base",
        scope_id=scope_id,
        attempts=attempts,
        rejections=rejections,
    )


@pytest.mark.parametrize(
    ("threshold", "attempts", "rejections", "expected"),
    [
        (
            0,
            [_attempt(1)],
            [_rejection(number=1, reviewed_turn=1, review_turn=2)],
            (1, 0, True, True, "high"),
        ),
        (
            1,
            [_attempt(1)],
            [_rejection(number=1, reviewed_turn=1, review_turn=2)],
            (1, 0, False, False, "high"),
        ),
        (
            1,
            [_attempt(1), _attempt(3)],
            [
                _rejection(number=1, reviewed_turn=1, review_turn=2),
                _rejection(number=2, reviewed_turn=3, review_turn=4),
            ],
            (2, 1, True, True, "high"),
        ),
        (
            2,
            [_attempt(1), _attempt(3), _attempt(5)],
            [
                _rejection(number=1, reviewed_turn=1, review_turn=2),
                _rejection(number=2, reviewed_turn=3, review_turn=4),
                _rejection(number=3, reviewed_turn=5, review_turn=6),
            ],
            (3, 2, True, True, "high"),
        ),
    ],
)
def test_repair_policy_counts_only_failed_repairs(
    threshold: int,
    attempts: list[ImplementationAttempt],
    rejections: list[ReviewRejectionRecord],
    expected: tuple[int, int, bool, bool, str],
) -> None:
    policy = _policy(
        threshold=threshold,
        attempts=attempts,
        rejections=rejections,
    )
    assert (
        policy.repair_ordinal,
        policy.team_repairs_completed,
        policy.due,
        policy.forced,
        policy.eligible_upgrade.target_team,
    ) == expected


def test_repair_policy_rejects_unlinked_or_wrong_selector_evidence() -> None:
    policy = _policy(
        threshold=1,
        attempts=[_attempt(1), _attempt(3)],
        rejections=[
            _rejection(
                number=1,
                reviewed_turn=1,
                review_turn=2,
                selector="codex.other",
            ),
            _rejection(number=2, reviewed_turn=3, review_turn=4),
        ],
    )
    assert policy.active is True
    assert policy.repair_ordinal == 1
    assert policy.team_repairs_completed == 0
    assert policy.due is False


@pytest.mark.parametrize("threshold", [-1, True, 0.5])
def test_repair_policy_rejects_invalid_threshold(threshold: object) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        _policy(threshold=threshold, attempts=[], rejections=[])


def test_zero_threshold_needs_latest_authoritative_rejection() -> None:
    initial = _policy(threshold=0, attempts=[_attempt(1)], rejections=[])
    assert initial.active is False
    assert initial.due is False
    assert initial.forced is False

    retry = _policy(
        threshold=0,
        attempts=[_attempt(1), _attempt(2, outcome="retry-scheduled")],
        rejections=[],
    )
    assert retry.active is False
    assert retry.forced is False

    wrong_selector = _policy(
        threshold=0,
        attempts=[_attempt(1)],
        rejections=[_rejection(number=1, reviewed_turn=1, review_turn=2, selector="codex.other")],
    )
    assert wrong_selector.active is False
    assert wrong_selector.forced is False

    stale = _policy(
        threshold=0,
        attempts=[_attempt(1), _attempt(3)],
        rejections=[_rejection(number=1, reviewed_turn=1, review_turn=2)],
    )
    assert stale.active is False
    assert stale.forced is False

    ambiguous = _policy(
        threshold=0,
        attempts=[_attempt(1), _attempt(1)],
        rejections=[_rejection(number=1, reviewed_turn=1, review_turn=2)],
    )
    assert ambiguous.active is False
    assert ambiguous.forced is False

    duplicate = _policy(
        threshold=0,
        attempts=[_attempt(1)],
        rejections=[
            _rejection(number=1, reviewed_turn=1, review_turn=2),
            _rejection(number=2, reviewed_turn=1, review_turn=3),
        ],
    )
    assert duplicate.active is True
    assert duplicate.repair_ordinal == 1
    assert duplicate.team_repairs_completed == 0
    assert duplicate.forced is True


def test_zero_threshold_without_upgrade_edge_keeps_baseline() -> None:
    policy = _policy(
        threshold=0,
        attempts=[_attempt(1)],
        rejections=[_rejection(number=1, reviewed_turn=1, review_turn=2)],
        teams={"base": TeamConfig(roles={"worker": "codex.worker-base"})},
    )
    assert policy.active is True
    assert policy.due is True
    assert policy.forced is False
    assert policy.current_team == "base"


@pytest.mark.parametrize("manager_enabled", [False, True])
def test_worker_upgrade_preserves_inherited_review_roles(manager_enabled: bool) -> None:
    config = _workflow_config(manager_enabled=manager_enabled, threshold=0)
    roles = resolve_team_config(config, "high").effective_roles
    assert roles["worker"] == "codex.worker-high"
    assert roles["reviewer"] == "codex.reviewer"
    assert roles["final_reviewer"] == "codex.final-reviewer"


def test_initial_rejection_after_early_upgrade_does_not_force_next_edge() -> None:
    policy = _policy(
        threshold=1,
        attempts=[_attempt(1), _attempt(3, "high")],
        rejections=[
            _rejection(
                number=1,
                reviewed_turn=3,
                review_turn=4,
                team="high",
                selector="codex.worker-high",
            ),
        ],
    )

    assert policy.current_team == "high"
    assert policy.repair_ordinal == 1
    assert policy.team_repairs_completed == 0
    assert policy.due is False
    assert policy.forced is False
    assert policy.eligible_upgrade.target_team == "max"


def test_operational_retry_before_first_rejection_does_not_consume_repair() -> None:
    policy = _policy(
        threshold=1,
        attempts=[
            _attempt(1),
            _attempt(2, outcome="retry-scheduled"),
        ],
        rejections=[
            _rejection(number=1, reviewed_turn=2, review_turn=3),
        ],
    )

    assert policy.repair_ordinal == 1
    assert policy.team_repairs_completed == 0
    assert policy.due is False
    assert policy.forced is False


def test_durable_attempt_identity_survives_colliding_resume_turns() -> None:
    policy = _policy(
        threshold=1,
        attempts=[
            _attempt(1, ordinal=1),
            _attempt(1, ordinal=2),
        ],
        rejections=[
            _rejection(
                number=1,
                reviewed_turn=1,
                review_turn=2,
                attempt_ordinal=1,
            ),
            _rejection(
                number=2,
                reviewed_turn=1,
                review_turn=2,
                attempt_ordinal=2,
            ),
        ],
    )

    assert policy.active is True
    assert policy.repair_ordinal == 2
    assert policy.team_repairs_completed == 1
    assert policy.due is True
    assert policy.forced is True


def test_ambiguous_legacy_turn_collision_contributes_zero_credit() -> None:
    policy = _policy(
        threshold=1,
        attempts=[_attempt(1), _attempt(1)],
        rejections=[_rejection(number=1, reviewed_turn=1, review_turn=2)],
    )

    assert policy.active is False
    assert policy.repair_ordinal is None
    assert policy.team_repairs_completed == 0


def test_reviewer_reinvocation_does_not_consume_an_extra_repair() -> None:
    policy = _policy(
        threshold=3,
        attempts=[_attempt(1), _attempt(3), _attempt(5)],
        rejections=[
            _rejection(number=1, reviewed_turn=1, review_turn=2),
            _rejection(number=2, reviewed_turn=3, review_turn=4),
            # A second reviewer invocation assessed the same worker turn.
            _rejection(number=3, reviewed_turn=3, review_turn=6),
            _rejection(number=4, reviewed_turn=5, review_turn=7),
        ],
    )
    assert policy.repair_ordinal == 3
    assert policy.team_repairs_completed == 2
    assert policy.due is False
    assert policy.forced is False


def test_repair_credit_resets_for_approval_and_independent_scopes() -> None:
    attempts = [_attempt(1), _attempt(3)]
    rejections = [
        _rejection(number=1, reviewed_turn=1, review_turn=2),
        _rejection(number=2, reviewed_turn=3, review_turn=4),
    ]
    due = _policy(threshold=1, attempts=attempts, rejections=rejections)
    assert due.due is True

    independent = _policy(
        threshold=1,
        attempts=[_attempt(1)],
        rejections=rejections,
        scope_id="other-scope",
    )
    assert independent.active is False
    assert independent.repair_ordinal is None
    assert independent.team_repairs_completed == 0

    approved = _policy(
        threshold=1,
        attempts=[_attempt(5)],
        rejections=[],
        scope_id="other-scope",
    )
    assert approved.active is False
    assert approved.due is False
    assert approved.team_repairs_completed == 0


@pytest.mark.parametrize(
    "teams",
    [
        {
            "base": TeamConfig(roles={"worker": "codex.worker-base"}),
        },
        {
            "base": TeamConfig(
                roles={"worker": "codex.worker-base"},
                upgrade_to="same",
            ),
            "same": TeamConfig(roles={"worker": "codex.worker-base"}),
        },
        {
            "base": TeamConfig(
                roles={"worker": "codex.worker-base"},
                upgrade_to="high",
            ),
            "high": TeamConfig(
                roles={"worker": "codex.worker-high"},
                upgrade_to="max",
            ),
            "max": TeamConfig(roles={"worker": "codex.worker-max"}),
        },
    ],
)
def test_due_repair_without_distinct_edge_retains_current_team(
    teams: dict[str, TeamConfig],
) -> None:
    policy = _policy(
        threshold=1,
        attempts=[_attempt(1), _attempt(3)],
        rejections=[
            _rejection(number=1, reviewed_turn=1, review_turn=2),
            _rejection(number=2, reviewed_turn=3, review_turn=4),
        ],
        teams=teams,
    )
    if "high" not in teams:
        assert policy.due is True
        assert policy.forced is False
    else:
        # An exhausted chain is still due at max, but has no usable edge.
        max_attempts = [_attempt(1), _attempt(3, "high"), _attempt(5, "max")]
        max_rejections = [
            _rejection(number=1, reviewed_turn=1, review_turn=2),
            _rejection(number=2, reviewed_turn=3, review_turn=4, team="high", selector="codex.worker-high"),
            _rejection(number=3, reviewed_turn=5, review_turn=6, team="max", selector="codex.worker-max"),
        ]
        exhausted = _policy(
            threshold=1,
            attempts=max_attempts,
            rejections=max_rejections,
            teams=teams,
        )
        assert exhausted.current_team == "max"
        assert exhausted.due is True
        assert exhausted.forced is False


def test_old_resume_records_have_zero_repair_credit_and_remain_readable() -> None:
    payload = {
        "manager_history": [],
        "review_rejection_history": [],
        "implementation_attempts": {
            "scope": [{
                "turn_number": 1,
                "step_name": "implement",
                "role": "worker",
                "team": "base",
                "selector": "codex.worker-base",
                "outcome": "progress",
                "manager_decision_number": None,
            }],
        },
        "active_implementation_scope": None,
        "pending_manager_notes": None,
        "pending_step_team_override": None,
        "pending_boundary_decision": None,
        "pending_repartition": None,
        "repartition_history": [],
    }
    manager_resume_fields_strict(payload)
    restored = manager_resume_fields(payload)
    attempt = restored["implementation_attempts"]["scope"][0]
    assert attempt.repair_ordinal is None
    assert attempt.team_repairs_completed is None
    assert attempt.attempt_ordinal is None


def test_resume_records_preserve_durable_attempt_identity() -> None:
    payload = {
        "manager_history": [],
        "review_rejection_history": [asdict(_rejection(
            number=1,
            reviewed_turn=1,
            review_turn=1,
            attempt_ordinal=1,
        ))],
        "implementation_attempts": {
            "scope": [asdict(_attempt(1, ordinal=1))],
        },
        "active_implementation_scope": None,
        "pending_manager_notes": None,
        "pending_step_team_override": None,
        "pending_boundary_decision": None,
        "pending_repartition": None,
        "repartition_history": [],
    }

    manager_resume_fields_strict(payload)
    restored = manager_resume_fields(payload)
    attempt = restored["implementation_attempts"]["scope"][0]
    rejection = restored["review_rejection_history"][0]
    assert attempt.attempt_ordinal == 1
    assert rejection.reviewed_attempt_ordinal == 1


def test_replayed_rejection_is_idempotent() -> None:
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    rejection = _rejection(number=1, reviewed_turn=1, review_turn=2)
    assert _append_replayed_review_rejection(state, rejection) is True
    assert _append_replayed_review_rejection(state, rejection) is False
    assert state.review_rejection_history == [rejection]


def test_pending_finalized_turn_decodes_authoritative_rejection(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    turn_dir = run_dir / "turns" / "turn-002"
    turn_dir.mkdir(parents=True)
    rejection = asdict(_rejection(
        number=1,
        reviewed_turn=1,
        review_turn=2,
        attempt_ordinal=1,
    ))
    snapshot = PlanSnapshot(
        "First", 1, 1, False, 1, current_checkpoint_index=1
    ).to_dict()
    (turn_dir / "result.json").write_text(
        json.dumps({
            "turn_number": 2,
            "status": "completed",
            "returncode": 0,
            "step_name": "review",
            "step_role": "reviewer",
            "selector": "codex.reviewer",
            "active_plan_path": "plan.md",
            "new_plan_path": "repair.md",
            "snapshot_before": snapshot,
            "snapshot_after": snapshot,
            "conditions": {
                "DONE": False,
                "NEW_PLAN_EXISTS": True,
                "MAX_TURNS_REACHED": False,
            },
            "chosen_transition": "implement",
            "chosen_transition_condition": None,
            "review_rejection": rejection,
        }),
        encoding="utf-8",
    )
    pending = _pending_finalized_resume_turn(
        run_dir,
        {
            "status": "running",
            "active_turn": 2,
            "turns_completed": 1,
            "current_step_name": "review",
        },
    )
    assert pending is not None
    assert pending.review_rejection is not None
    assert pending.review_rejection.rejection_number == 1
    assert pending.review_rejection.reviewed_attempt_ordinal == 1


def _run_fake_sequence(
    tmp_path: Path,
    *,
    manager_enabled: bool,
    threshold: int,
) -> tuple[list[str], list[dict[str, object]], Path]:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_PLAN, encoding="utf-8")
    config = _workflow_config(
        manager_enabled=manager_enabled,
        threshold=threshold,
    )
    calls: list[str] = []
    manager_contexts: list[dict[str, object]] = []
    review_count = 0

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal review_count
        model = argv[argv.index("--model") + 1]
        calls.append(model)
        cwd = Path(str(kwargs["cwd"]))
        if model == "manager":
            marker = "MANAGER_CONTEXT_JSON:\n"
            prompt = str(kwargs.get("input", ""))
            manager_context = json.loads(prompt.split(marker, 1)[1])
            manager_contexts.append(manager_context)
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({
                    "schema_version": 1,
                    "action": "continue",
                    "reason": "synthetic continue",
                    "next_step_notes": [],
                    "stop_report": None,
                }),
                "",
            )
        if model == "reviewer":
            review_count += 1
            (cwd / f"plan-repair-{review_count}.md").write_text(
                _REPAIR_PLAN,
                encoding="utf-8",
            )
        elif model == "worker-high":
            for candidate in cwd.glob("*.md"):
                candidate.write_text(_COMPLETE_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    result = run_workflow(
        config=ControllerConfig(
            repo_root=tmp_path,
            plan_path=plan_path,
            max_turns=10,
            team="base",
        ),
        workflow_config=config,
        workflow_name="repair",
        config_dir=tmp_path,
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=runner,
    )
    return calls, manager_contexts, result.run_dir


@pytest.mark.parametrize("manager_enabled", [False, True])
@pytest.mark.parametrize("threshold", [0, 1])
def test_selected_worker_route_survives_prelaunch_process_stop(
    tmp_path: Path,
    manager_enabled: bool,
    threshold: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_PLAN, encoding="utf-8")
    config = _workflow_config(manager_enabled=manager_enabled, threshold=threshold)
    calls: list[str] = []
    review_count = 0
    crashed = False

    class PrelaunchStop(BaseException):
        pass

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal review_count
        model = argv[argv.index("--model") + 1]
        calls.append(model)
        cwd = Path(str(kwargs["cwd"]))
        if model == "manager":
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({
                    "schema_version": 1,
                    "action": "continue",
                    "reason": "synthetic continue",
                    "next_step_notes": [],
                    "stop_report": None,
                }),
                "",
            )
        if model == "reviewer":
            review_count += 1
            (cwd / f"plan-repair-{review_count}.md").write_text(
                _REPAIR_PLAN,
                encoding="utf-8",
            )
        elif model == "worker-high":
            for candidate in cwd.glob("*.md"):
                candidate.write_text(_COMPLETE_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    original_write = RunMetadataWriter.write

    def write_and_stop(
        writer: RunMetadataWriter,
        **kwargs: object,
    ) -> None:
        nonlocal crashed
        original_write(writer, **kwargs)
        if crashed or kwargs.get("status") != "running":
            return
        payload = json.loads(writer.paths.run_json.read_text(encoding="utf-8"))
        active_turn = payload.get("active_turn")
        turns_completed = payload.get("turns_completed")
        pending_override = payload.get("pending_step_team_override")
        if not (
            isinstance(active_turn, int)
            and isinstance(turns_completed, int)
            and active_turn == turns_completed + 1
            and isinstance(pending_override, dict)
            and pending_override.get("target_team") == "high"
            and pending_override.get("selector") == "codex.worker-high"
        ):
            return
        turn_result = writer.paths.run_dir / "turns" / f"turn-{active_turn:03d}" / "result.json"
        if json.loads(turn_result.read_text(encoding="utf-8")).get("status") != "starting":
            return
        crashed = True
        raise PrelaunchStop

    monkeypatch.setattr(RunMetadataWriter, "write", write_and_stop)
    with pytest.raises(PrelaunchStop):
        run_workflow(
            config=ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan_path,
                max_turns=10,
                team="base",
            ),
            workflow_config=config,
            workflow_name="repair",
            config_dir=tmp_path,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=runner,
        )

    run_dirs = sorted((tmp_path / ".aflow" / "runs").iterdir())
    assert len(run_dirs) == 1
    source_run = run_dirs[0]
    source_payload = json.loads(
        (source_run / "run.json").read_text(encoding="utf-8")
    )
    persisted_override = source_payload["pending_step_team_override"]
    assert persisted_override["target_team"] == "high"
    assert persisted_override["selector"] == "codex.worker-high"
    assert source_payload["active_turn"] == source_payload["turns_completed"] + 1

    # The synthetic BaseException has ended the source controller in this
    # process; record that ownership transition before starting its successor.
    (source_run / "run.json").write_text(
        json.dumps({**source_payload, "status": "interrupted"}) + "\n",
        encoding="utf-8",
    )

    fields = manager_resume_fields(source_payload)
    scope = fields["active_implementation_scope"]
    assert scope is not None
    envelope_path = source_run / str(scope.envelope_artifact_path)
    resume = _mark_validated_resume_context(ResumeContext(
        resumed_from_run_id=source_run.name,
        feature_branch=None,
        worktree_path=None,
        main_branch=None,
        setup=(),
        teardown=(),
        active_plan_path=Path(source_payload["active_plan_path"]),
        interrupted_step_name="implement",
        effective_max_turns=10,
        scope_envelope_bytes=envelope_path.read_bytes(),
        scope_evidence_artifact_bytes=load_scope_evidence_for_resume(
            source_run, scope, envelope_path.read_bytes()
        ),
        **fields,
    ))

    result = run_workflow(
        config=ControllerConfig(
            repo_root=tmp_path,
            plan_path=plan_path,
            max_turns=10,
            team="base",
        ),
        workflow_config=config,
        workflow_name="repair",
        config_dir=tmp_path,
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=runner,
        resume=resume,
    )

    assert result.final_snapshot.is_complete
    worker_calls = [model for model in calls if model.startswith("worker-")]
    assert worker_calls[-1] == "worker-high"
    assert worker_calls.count("worker-high") == 1


@pytest.mark.parametrize("threshold", [0, 1, 2])
@pytest.mark.parametrize("manager_enabled", [False, True])
def test_repair_threshold_survives_resume_with_reset_turn_numbers(
    tmp_path: Path,
    threshold: int,
    manager_enabled: bool,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_PLAN, encoding="utf-8")
    config = _workflow_config(
        manager_enabled=manager_enabled,
        threshold=threshold,
    )
    calls: list[str] = []

    def manager_result(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({
                "schema_version": 1,
                "action": "continue",
                "reason": "synthetic continue",
                "next_step_notes": [],
                "stop_report": None,
            }),
            "",
        )

    def source_runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal stopped
        model = argv[argv.index("--model") + 1]
        if model == "reviewer":
            stopped = True
            raise ReviewBoundaryStop
        calls.append(model)
        if model == "manager":
            return manager_result(argv)
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    class ReviewBoundaryStop(BaseException):
        pass

    stopped = False
    with pytest.raises(ReviewBoundaryStop):
        run_workflow(
            config=ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan_path,
                max_turns=10,
                team="base",
            ),
            workflow_config=config,
            workflow_name="repair",
            config_dir=tmp_path,
            snapshot_config=False,
            adapter=CodexAdapter(),
            runner=source_runner,
        )

    assert stopped is True
    source_run = next((tmp_path / ".aflow" / "runs").iterdir())
    source_payload = json.loads(
        (source_run / "run.json").read_text(encoding="utf-8")
    )
    attempt_rows = next(iter(source_payload["implementation_attempts"].values()))
    assert len(attempt_rows) == 1
    assert attempt_rows[0]["attempt_ordinal"] == 1
    (source_run / "run.json").write_text(
        json.dumps({**source_payload, "status": "interrupted"}) + "\n",
        encoding="utf-8",
    )
    fields = manager_resume_fields(source_payload)
    scope = fields["active_implementation_scope"]
    assert scope is not None
    envelope_path = source_run / str(scope.envelope_artifact_path)
    resume = _mark_validated_resume_context(ResumeContext(
        resumed_from_run_id=source_run.name,
        feature_branch=None,
        worktree_path=None,
        main_branch=None,
        setup=(),
        teardown=(),
        active_plan_path=Path(source_payload["active_plan_path"]),
        interrupted_step_name="review",
        effective_max_turns=10,
        scope_envelope_bytes=envelope_path.read_bytes(),
        scope_evidence_artifact_bytes=load_scope_evidence_for_resume(
            source_run, scope, envelope_path.read_bytes()
        ),
        **fields,
    ))
    review_count = 0

    def resume_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal review_count
        model = argv[argv.index("--model") + 1]
        calls.append(model)
        cwd = Path(str(kwargs["cwd"]))
        if model == "manager":
            return manager_result(argv)
        if model == "reviewer":
            review_count += 1
            (cwd / f"plan-repair-{review_count}.md").write_text(
                _REPAIR_PLAN,
                encoding="utf-8",
            )
        elif model == "worker-high":
            for candidate in cwd.glob("*.md"):
                candidate.write_text(_COMPLETE_PLAN, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    result = run_workflow(
        config=ControllerConfig(
            repo_root=tmp_path,
            plan_path=plan_path,
            max_turns=10,
            team="base",
        ),
        workflow_config=config,
        workflow_name="repair",
        config_dir=tmp_path,
        snapshot_config=False,
        adapter=CodexAdapter(),
        runner=resume_runner,
        resume=resume,
    )

    expected = (
        ["worker-base", "reviewer", "worker-high"]
        if threshold == 0
        else ["worker-base", "reviewer", "worker-base", "reviewer", "worker-high"]
        if threshold == 1
        else [
            "worker-base",
            "reviewer",
            "worker-base",
            "reviewer",
            "worker-base",
            "reviewer",
            "worker-high",
        ]
    )
    provider_calls = [
        model
        for model in calls
        if model.startswith("worker-") or model == "reviewer"
    ]
    assert provider_calls == expected
    assert result.final_snapshot.is_complete
    run_payload = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    attempt_rows = next(iter(run_payload["implementation_attempts"].values()))
    assert [row["attempt_ordinal"] for row in attempt_rows] == list(
        range(1, len(attempt_rows) + 1)
    )
    first_rejection = run_payload["review_rejection_history"][0]
    assert first_rejection["review_turn_number"] == 1
    assert first_rejection["reviewed_implementation_turn_number"] == 1
    assert first_rejection["reviewed_attempt_ordinal"] == 1
    assert attempt_rows[-1]["team"] == "high"


@pytest.mark.parametrize("threshold, expected_workers", [
    (0, ["worker-base", "reviewer", "worker-high"]),
    (1, ["worker-base", "reviewer", "worker-base", "reviewer", "worker-high"]),
    (2, ["worker-base", "reviewer", "worker-base", "reviewer", "worker-base", "reviewer", "worker-high"]),
])
@pytest.mark.parametrize("manager_enabled", [False, True])
def test_fake_provider_sequences_apply_threshold_in_both_manager_modes(
    tmp_path: Path,
    threshold: int,
    expected_workers: list[str],
    manager_enabled: bool,
) -> None:
    calls, manager_contexts, run_dir = _run_fake_sequence(
        tmp_path,
        manager_enabled=manager_enabled,
        threshold=threshold,
    )
    workers = [model for model in calls if model.startswith("worker-") or model == "reviewer"]
    assert workers == expected_workers
    if manager_enabled:
        due_contexts = [
            context
            for context in manager_contexts
            if context["controller_state"]["repair_upgrade_policy"]["forced"]
        ]
        assert due_contexts
        assert all(
            context["controller_state"]["repair_upgrade_policy"]["threshold"] == threshold
            for context in due_contexts
        )
    else:
        assert manager_contexts == []
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    attempt_rows = next(iter(run_json["implementation_attempts"].values()))
    expected_progress = (
        [(0, 0), (1, 0)]
        if threshold == 0
        else [(0, 0), (1, 0), (2, 1)]
        if threshold == 1
        else [(0, 0), (1, 0), (2, 1), (3, 2)]
    )
    assert [
        (row["repair_ordinal"], row["team_repairs_completed"])
        for row in attempt_rows
    ] == expected_progress
    assert run_json["active_implementation_scope"] is None

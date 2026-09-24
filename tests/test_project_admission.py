from __future__ import annotations

from multiprocessing.context import BaseContext
import json
from pathlib import Path
import multiprocessing
import subprocess
from types import SimpleNamespace

import pytest

import aflow.project_admission as admission_module
from aflow.config import WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig
from aflow.plan_backups import move_plan_identity
from aflow.control_plane import (
    InMemoryUnitManager,
    LaunchManifest,
    UnitState,
    append_run_event,
    create_launch_manifest,
    write_launch_phase,
)
from aflow.project_admission import (
    ProjectAdmission,
    ProjectAdmissionConflict,
    ProjectAdmissionError,
    ProjectAdmissionSafetyError,
    ProjectCapacityReached,
    ProjectPlanDependencyBlocked,
)
from aflow.project_settings import ProjectSettings, ProjectSettingsService
from aflow.run_state import ControllerConfig, ResumeContext
from aflow.workflow import WorkflowError, run_workflow


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _committed_separate_git_repo(root: Path) -> Path:
    root.mkdir()
    git_dir = root.parent / "external-git" / f"{root.name}.git"
    git_dir.parent.mkdir()
    subprocess.run(
        (
            "git",
            "init",
            "-q",
            "--separate-git-dir",
            str(git_dir),
            "-b",
            "main",
            str(root),
        ),
        check=True,
    )
    _git(root, "config", "user.name", "Project admission test")
    _git(root, "config", "user.email", "project-admission@example.test")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    _git(root, "config", "core.worktree", str(root))
    return root


def _manifest(root: Path, run_id: str, *, phase: str) -> None:
    plan = root / "plan.md"
    plan.write_text("# Plan\n", encoding="utf-8")
    create_launch_manifest(
        root,
        LaunchManifest(
            run_id=run_id,
            project_root=str(root.resolve()),
            plan_path=str(plan.resolve()),
            workflow_name="managed",
            max_turns=2,
            idempotency_key=f"key-{run_id}",
            caller_scope="test",
        ),
    )
    write_launch_phase(root, run_id, phase)


def _terminal_manifest(root: Path, run_id: str) -> None:
    _manifest(root, run_id, phase="completed")
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        '{"schema_version":2,"status":"completed"}\n', encoding="utf-8"
    )


def _race_worker(
    root: str,
    run_id: str,
    barrier: object,
    release: object,
    results: object,
) -> None:
    barrier.wait()  # type: ignore[attr-defined]
    try:
        reservation = ProjectAdmission(Path(root)).acquire(
            run_id,
            idempotency_key=f"race-{run_id}",
        )
        results.put(("reserved", run_id, reservation.nonce))  # type: ignore[attr-defined]
    except ProjectCapacityReached:
        results.put(("capacity", run_id, None))  # type: ignore[attr-defined]
    finally:
        release.wait(timeout=10)  # type: ignore[attr-defined]


def _successor_race_worker(
    root: str, run_id: str, source_run_id: str, barrier: object, release: object,
    results: object,
) -> None:
    barrier.wait()  # type: ignore[attr-defined]
    try:
        reservation = ProjectAdmission(Path(root)).acquire(
            run_id,
            idempotency_key=f"key-{run_id}",
            source_run_id=source_run_id,
        )
        results.put(("reserved", run_id, reservation.nonce))  # type: ignore[attr-defined]
    except ProjectAdmissionConflict:
        results.put(("conflict", run_id, None))  # type: ignore[attr-defined]
    finally:
        release.wait(timeout=10)  # type: ignore[attr-defined]


def _plan_race_worker(
    root: str, run_id: str, barrier: object, release: object, results: object,
) -> None:
    barrier.wait()  # type: ignore[attr-defined]
    try:
        reservation = ProjectAdmission(Path(root)).acquire(
            run_id,
            plan_path=Path(root) / "plan.md",
            idempotency_key=f"key-{run_id}",
        )
        results.put(("reserved", run_id, reservation.nonce))  # type: ignore[attr-defined]
    except ProjectAdmissionConflict:
        results.put(("conflict", run_id, None))  # type: ignore[attr-defined]
    finally:
        release.wait(timeout=10)  # type: ignore[attr-defined]


def _crash_worker(root: str, results: object) -> None:
    reservation = ProjectAdmission(Path(root)).acquire(
        "crashed-before-manifest", idempotency_key="crash-key"
    )
    results.put(reservation.nonce)  # type: ignore[attr-defined]


def _start_race_processes(
    ctx: BaseContext,
    root: Path,
    count: int,
) -> tuple[list[object], object, object]:
    barrier = ctx.Barrier(count)
    release = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_race_worker,
            args=(str(root), f"race-{index}", barrier, release, results),
        )
        for index in range(count)
    ]
    for process in processes:
        process.start()
    return processes, release, results


def test_multiprocess_race_reserves_at_most_the_default_two_slots(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    processes, release, results = _start_race_processes(ctx, tmp_path, 3)
    observed = [results.get(timeout=10) for _ in processes]

    assert [item[0] for item in observed].count("reserved") == 2
    assert [item[0] for item in observed].count("capacity") == 1
    snapshot = ProjectAdmission(tmp_path).snapshot()
    assert snapshot.occupied_count == 2
    assert snapshot.available_slots == 0

    release.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0


def test_two_processes_cannot_claim_one_predecessor(tmp_path: Path) -> None:
    _terminal_manifest(tmp_path, "source")
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    release = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_successor_race_worker,
            args=(str(tmp_path), f"successor-{index}", "source", barrier, release, results),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        observed = [results.get(timeout=10) for _ in processes]
        assert sorted(item[0] for item in observed) == ["conflict", "reserved"]
        assert ProjectAdmission(tmp_path).snapshot().occupied_count == 1
    finally:
        release.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0


def test_two_processes_with_distinct_keys_cannot_claim_one_plan(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("# Plan\n", encoding="utf-8")
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    release = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_plan_race_worker,
            args=(str(tmp_path), f"plan-run-{index}", barrier, release, results),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        observed = [results.get(timeout=10) for _ in processes]
        assert sorted(item[0] for item in observed) == ["conflict", "reserved"]
        assert ProjectAdmission(tmp_path).snapshot().occupied_count == 1
        rejected_id = next(run_id for kind, run_id, _ in observed if kind == "conflict")
        assert ProjectAdmission(tmp_path).reservation(rejected_id) is None
    finally:
        release.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0


def test_plan_claim_replay_pending_question_and_independent_capacity(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    other_plan = tmp_path / "other.md"
    third_plan = tmp_path / "third.md"
    for path in (plan, other_plan, third_plan):
        path.write_text("# Plan\n", encoding="utf-8")
    admission = ProjectAdmission(tmp_path)
    first = admission.acquire("first-plan-run", plan_path=plan, idempotency_key="first")
    replay = admission.acquire("first-plan-run", plan_path=plan, idempotency_key="first")
    assert replay.nonce == first.nonce
    with pytest.raises(ProjectAdmissionConflict, match="different launch request"):
        admission.acquire("first-plan-run", plan_path=other_plan, idempotency_key="first")
    with pytest.raises(ProjectAdmissionConflict, match="plan already has"):
        admission.acquire("second-plan-run", plan_path=plan, idempotency_key="second")

    _manifest(tmp_path, "first-plan-run", phase="manifest_only")
    startup = tmp_path / ".aflow" / "start-requests"
    startup.mkdir(parents=True, exist_ok=True)
    (startup / "first-plan-run.json").write_text(
        '{"schema_version":1,"run_id":"first-plan-run",'
        '"state":"awaiting_startup_answer","question_generation":1,'
        '"question":{"kind":"pick_step","message":"Choose",'
        '"options":{},"choices":["implement"]}}\n',
        encoding="utf-8",
    )
    admission.release(first.run_id, first.nonce, reason="startup_question_waiting", claim_retained=True)
    assert admission.snapshot().occupied_count == 0
    with pytest.raises(ProjectAdmissionConflict, match="plan already has"):
        admission.acquire("second-plan-run", plan_path=plan, idempotency_key="second")
    assert admission.reservation("second-plan-run") is None

    other = admission.acquire("other-plan-run", plan_path=other_plan, idempotency_key="other")
    assert other.state == "reserved"
    assert admission.snapshot().occupied_count == 1
    admission.ensure("first-plan-run", plan_path=plan, idempotency_key="first")
    assert admission.snapshot().occupied_count == 2
    with pytest.raises(ProjectCapacityReached):
        admission.acquire("third-plan-run", plan_path=third_plan, idempotency_key="third")


def test_terminal_plan_owner_allows_requeue_and_valid_continuation(tmp_path: Path) -> None:
    _terminal_manifest(tmp_path, "source")
    plan = tmp_path / "plan.md"
    admission = ProjectAdmission(tmp_path)
    fresh = admission.acquire("fresh", plan_path=plan, idempotency_key="fresh")
    admission.release(fresh.run_id, fresh.nonce, reason="prelaunch_requeue")
    first = admission.acquire(
        "continuation-one", plan_path=plan,
        idempotency_key="first", source_run_id="source",
    )
    assert first.source_run_id == "source"
    create_launch_manifest(
        tmp_path,
        LaunchManifest(
            run_id="continuation-one", project_root=str(tmp_path.resolve()),
            plan_path=str(plan.resolve()), workflow_name="managed", max_turns=2,
            restarted_from_run_id="source",
        ),
    )
    write_launch_phase(tmp_path, "continuation-one", "completed")
    run_dir = tmp_path / ".aflow" / "runs" / "continuation-one"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        '{"schema_version":2,"status":"completed"}\n', encoding="utf-8"
    )
    second = admission.acquire(
        "continuation-two", plan_path=plan,
        idempotency_key="second", source_run_id="continuation-one",
    )
    assert second.source_run_id == "continuation-one"


def test_published_plan_claim_blocks_after_journal_loss(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n", encoding="utf-8")
    admission = ProjectAdmission(tmp_path)
    admission.acquire("first-plan", plan_path=plan, idempotency_key="first")
    _manifest(tmp_path, "first-plan", phase="launch_requested")
    admission.state_path.unlink()

    with pytest.raises(ProjectAdmissionConflict, match="plan already has"):
        admission.acquire("second-plan", plan_path=plan, idempotency_key="second")
    assert admission.reservation("second-plan") is None


def test_linked_worktrees_share_a_plan_claim(tmp_path: Path) -> None:
    primary = _committed_separate_git_repo(tmp_path / "primary")
    (primary / "plan.md").write_text("# Plan\n", encoding="utf-8")
    _git(primary, "add", "plan.md")
    _git(primary, "commit", "-q", "-m", "add plan")
    first = tmp_path / "first-worktree"
    second = tmp_path / "second-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "first", str(first))
    _git(primary, "worktree", "add", "-q", "-b", "second", str(second))

    ProjectAdmission(first).acquire(
        "first-worktree-run", plan_path=first / "plan.md", idempotency_key="first"
    )
    with pytest.raises(ProjectAdmissionConflict, match="plan already has"):
        ProjectAdmission(second).acquire(
            "second-worktree-run", plan_path=second / "plan.md", idempotency_key="second"
        )
    assert ProjectAdmission(primary).snapshot().occupied_count == 1


def test_linked_worktree_sequence_predecessor_blocks_before_reservation(tmp_path: Path) -> None:
    primary = _committed_separate_git_repo(tmp_path / "primary")
    linked = tmp_path / "linked-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked))
    plans = linked / "plans" / "in-progress"
    plans.mkdir(parents=True)
    predecessor = plans / "feature_P01_intro.md"
    predecessor.write_text("# First\n", encoding="utf-8")
    successor = plans / "feature_P03_finish.md"
    successor.write_text("# Third\n", encoding="utf-8")
    unrelated = plans / "other_P07_work.md"
    unrelated.write_text("# Other\n", encoding="utf-8")

    admission = ProjectAdmission(linked)
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        admission.acquire("linked-successor", plan_path=successor)
    assert ProjectAdmission(primary).snapshot().occupied_count == 0
    assert admission.acquire("linked-unrelated", plan_path=unrelated).run_id == "linked-unrelated"
    predecessor.unlink()
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        admission.acquire("linked-successor-retry", plan_path=successor)


def test_primary_and_linked_plan_copies_are_one_sequence_member(tmp_path: Path) -> None:
    primary = _committed_separate_git_repo(tmp_path / "primary")
    linked = tmp_path / "linked-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked))
    for root in (primary, linked):
        plans = root / "plans" / "in-progress"
        plans.mkdir(parents=True)
        (plans / "feature_P03_finish.md").write_text("# Third\n", encoding="utf-8")

    successor = linked / "plans" / "in-progress" / "feature_P03_finish.md"
    admitted = ProjectAdmission(primary).acquire(
        "one-logical-plan", plan_path=successor, idempotency_key="copy",
    )
    assert admitted.run_id == "one-logical-plan"
    ProjectAdmission(primary).release(admitted.run_id, admitted.nonce)
    for root in (primary, linked):
        (root / "plans" / "in-progress" / "feature_P01_intro.md").write_text(
            "# First\n", encoding="utf-8"
        )
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        ProjectAdmission(linked).acquire(
            "blocked-successor", plan_path=successor, idempotency_key="blocked",
        )


def test_distinct_cross_worktree_names_at_one_position_block_series(tmp_path: Path) -> None:
    primary = _committed_separate_git_repo(tmp_path / "primary")
    linked = tmp_path / "linked-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked))
    primary_plans = primary / "plans" / "in-progress"
    linked_plans = linked / "plans" / "in-progress"
    primary_plans.mkdir(parents=True)
    linked_plans.mkdir(parents=True)
    (primary_plans / "feature_P01_intro.md").write_text("# One\n", encoding="utf-8")
    (linked_plans / "feature_P01_alternative.md").write_text("# Other one\n", encoding="utf-8")
    successor = linked_plans / "feature_P03_finish.md"
    successor.write_text("# Third\n", encoding="utf-8")

    with pytest.raises(ProjectPlanDependencyBlocked, match="correction"):
        ProjectAdmission(linked).acquire("duplicate-series", plan_path=successor)


def test_linked_predecessor_requires_its_published_delivery(tmp_path: Path) -> None:
    primary = _committed_separate_git_repo(tmp_path / "primary")
    linked = tmp_path / "linked-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked))
    plans = linked / "plans" / "in-progress"
    plans.mkdir(parents=True)
    predecessor = plans / "feature_P01_intro.md"
    predecessor.write_text("# First\n", encoding="utf-8")
    successor = plans / "feature_P03_finish.md"
    successor.write_text("# Third\n", encoding="utf-8")
    admission = ProjectAdmission(linked)
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        admission.acquire("linked-successor", plan_path=successor)

    done = linked / "plans" / "done" / predecessor.name
    done.parent.mkdir()
    predecessor.rename(done)
    assert move_plan_identity(
        linked, source_plan_path=predecessor, destination_plan_path=done,
    )
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        admission.acquire("linked-successor", plan_path=successor)
    receipt = linked / ".aflow" / "runs" / "delivered-run" / "publication.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "status": "published", "commit": "a" * 40, "source_commit": "b" * 40,
        "remote": "origin", "branch": "main",
        "plan_lifecycle": {
            "phase": "committed", "complete": True,
            "source": "plans/in-progress/feature_P01_intro.md",
            "destination": "plans/done/feature_P01_intro.md",
        },
    }), encoding="utf-8")
    assert admission.acquire("linked-successor", plan_path=successor).run_id == "linked-successor"


def test_linked_delivery_remains_valid_for_primary_successor(tmp_path: Path) -> None:
    primary = _committed_separate_git_repo(tmp_path / "primary")
    linked = tmp_path / "linked-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked))
    linked_plans = linked / "plans" / "in-progress"
    primary_plans = primary / "plans" / "in-progress"
    linked_plans.mkdir(parents=True)
    primary_plans.mkdir(parents=True)
    predecessor = linked_plans / "feature_P01_intro.md"
    predecessor.write_text("# First\n", encoding="utf-8")
    linked_successor = linked_plans / "feature_P03_finish.md"
    primary_successor = primary_plans / linked_successor.name
    for path in (linked_successor, primary_successor):
        path.write_text("# Third\n", encoding="utf-8")
    linked_admission = ProjectAdmission(linked)
    primary_admission = ProjectAdmission(primary)
    for admission, plan in (
        (linked_admission, linked_successor),
        (primary_admission, primary_successor),
    ):
        with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
            admission.acquire("before-delivery", plan_path=plan)

    done = linked / "plans" / "done" / predecessor.name
    done.parent.mkdir()
    predecessor.rename(done)
    assert move_plan_identity(
        linked, source_plan_path=predecessor, destination_plan_path=done,
    )
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        primary_admission.acquire("done-only", plan_path=primary_successor)
    receipt = linked / ".aflow" / "runs" / "delivered-run" / "publication.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"published","commit":"invalid"}', encoding="utf-8")
    with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
        primary_admission.acquire("invalid-receipt", plan_path=primary_successor)
    receipt.write_text(json.dumps({
        "status": "published", "commit": "a" * 40, "source_commit": "b" * 40,
        "remote": "origin", "branch": "main",
        "plan_lifecycle": {
            "phase": "committed", "complete": True,
            "source": "plans/in-progress/feature_P01_intro.md",
            "destination": "plans/done/feature_P01_intro.md",
        },
    }), encoding="utf-8")
    linked_run = linked_admission.acquire("linked-ready", plan_path=linked_successor)
    linked_admission.release(linked_run.run_id, linked_run.nonce)
    primary_run = primary_admission.acquire("primary-ready", plan_path=primary_successor)
    primary_admission.release(primary_run.run_id, primary_run.nonce)
    receipt.unlink()
    for admission, plan in (
        (primary_admission, primary_successor),
        (linked_admission, linked_successor),
    ):
        with pytest.raises(ProjectPlanDependencyBlocked, match="predecessor"):
            admission.acquire("after-receipt-loss", plan_path=plan)


def test_cli_and_managed_admission_share_idempotent_reservation_state(tmp_path: Path) -> None:
    managed = ProjectAdmission(tmp_path)
    foreground_cli = ProjectAdmission(tmp_path)

    first = managed.acquire("managed-run", idempotency_key="managed-key")
    replay = foreground_cli.acquire("managed-run", idempotency_key="managed-key")
    assert replay.nonce == first.nonce
    with pytest.raises(ProjectAdmissionConflict):
        foreground_cli.acquire("managed-run", idempotency_key="different-key")

    second = foreground_cli.acquire("cli-run", idempotency_key="cli-key")
    assert second.run_id == "cli-run"
    with pytest.raises(ProjectCapacityReached) as rejected:
        managed.acquire("third-run", idempotency_key="third-key")
    assert rejected.value.code == "project_capacity_reached"
    assert len(rejected.value.safe_message) < 128


def test_worker_nonce_consumption_is_idempotent_and_nonce_bound(tmp_path: Path) -> None:
    admission = ProjectAdmission(tmp_path)
    reservation = admission.acquire("worker-run", idempotency_key="worker-key")

    consumed = admission.consume("worker-run", reservation.nonce)
    assert consumed.state == "active"
    replay = admission.consume("worker-run", reservation.nonce)
    assert replay.nonce == reservation.nonce
    with pytest.raises(ProjectAdmissionConflict):
        admission.consume("worker-run", "0" * 32)


def test_direct_controller_releases_unbound_admission_on_manifest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    workflow_config = WorkflowUserConfig(
        workflows={
            "managed": WorkflowConfig(
                steps={"implement": WorkflowStepConfig(role="worker")},
                first_step="implement",
            )
        }
    )

    # Keep this regression at the controller admission boundary.  All
    # startup/lifecycle work is replaced with isolated no-op seams so no
    # provider, queue, or shared service is involved.
    monkeypatch.setattr(
        "aflow.workflow._validate_current_branch_execution",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "aflow.workflow.probe_repo_state", lambda _root: object()
    )
    monkeypatch.setattr(
        "aflow.workflow._lifecycle_is_bootstrap_eligible",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "aflow.workflow._lifecycle_preflight",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "aflow.workflow._backup_original_plan",
        lambda *_args, **_kwargs: plan_path,
    )
    monkeypatch.setattr(
        "aflow.workflow._prepare_required_git_tracking_before_allocation",
        lambda **kwargs: (kwargs["parsed_plan"], False),
    )

    def fail_manifest(*_args: object, **_kwargs: object) -> None:
        raise ValueError("synthetic launch-manifest publication failure")

    monkeypatch.setattr("aflow.control_plane.create_launch_manifest", fail_manifest)

    run_id = "direct-manifest-failure"
    with pytest.raises(WorkflowError, match="cannot reserve run identity"):
        run_workflow(
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan_path,
                max_turns=1,
                reserved_run_id=run_id,
                idempotency_key="direct-manifest-failure-key",
            ),
            workflow_config,
            "managed",
            parsed_plan=object(),
            config_dir=tmp_path,
            working_dir=tmp_path,
        )

    reservation = ProjectAdmission(tmp_path).reservation(run_id)
    assert reservation is not None
    assert reservation.state == "released"
    assert reservation.bound is False
    assert ProjectAdmission(tmp_path).snapshot().occupied_count == 0


def test_direct_controller_rejects_a_second_successor_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    _terminal_manifest(tmp_path, "source")
    admission = ProjectAdmission(tmp_path)
    admission.acquire(
        "first-successor", idempotency_key="first-key", source_run_id="source"
    )
    workflow_config = WorkflowUserConfig(
        workflows={
            "managed": WorkflowConfig(
                steps={"implement": WorkflowStepConfig(role="worker")},
                first_step="implement",
            )
        }
    )
    monkeypatch.setattr(
        "aflow.workflow._validate_current_branch_execution", lambda *_a, **_k: None
    )
    monkeypatch.setattr("aflow.workflow.probe_repo_state", lambda _root: object())
    monkeypatch.setattr(
        "aflow.workflow._lifecycle_is_bootstrap_eligible", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "aflow.workflow._lifecycle_preflight", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "aflow.workflow._backup_original_plan", lambda *_a, **_k: plan_path
    )
    monkeypatch.setattr(
        "aflow.workflow._prepare_required_git_tracking_before_allocation",
        lambda **kwargs: (kwargs["parsed_plan"], False),
    )
    launched = False

    def runner(*_args: object, **_kwargs: object) -> None:
        nonlocal launched
        launched = True

    with pytest.raises(ProjectAdmissionConflict, match="unresolved successor"):
        run_workflow(
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan_path,
                max_turns=1,
                reserved_run_id="direct-successor",
                idempotency_key="direct-key",
            ),
            workflow_config,
            "managed",
            parsed_plan=object(),
            config_dir=tmp_path,
            working_dir=tmp_path,
            runner=runner,
            resume=ResumeContext(
                resumed_from_run_id="source",
                feature_branch=None,
                worktree_path=None,
                main_branch=None,
                setup=(),
                teardown=(),
            ),
        )
    assert launched is False
    assert admission.reservation("direct-successor") is None
    assert admission.snapshot().occupied_count == 1


def test_direct_controller_rejects_a_second_plan_claim_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    admission = ProjectAdmission(tmp_path)
    admission.acquire("first-direct", plan_path=plan_path, idempotency_key="first-key")
    workflow_config = WorkflowUserConfig(
        workflows={
            "managed": WorkflowConfig(
                steps={"implement": WorkflowStepConfig(role="worker")},
                first_step="implement",
            )
        }
    )
    monkeypatch.setattr("aflow.workflow._validate_current_branch_execution", lambda *_a, **_k: None)
    monkeypatch.setattr("aflow.workflow.probe_repo_state", lambda _root: object())
    monkeypatch.setattr("aflow.workflow._lifecycle_is_bootstrap_eligible", lambda *_a, **_k: False)
    monkeypatch.setattr("aflow.workflow._lifecycle_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr("aflow.workflow._backup_original_plan", lambda *_a, **_k: plan_path)
    monkeypatch.setattr(
        "aflow.workflow._prepare_required_git_tracking_before_allocation",
        lambda **kwargs: (kwargs["parsed_plan"], False),
    )
    launched = False

    def runner(*_args: object, **_kwargs: object) -> None:
        nonlocal launched
        launched = True

    with pytest.raises(ProjectAdmissionConflict, match="plan already has"):
        run_workflow(
            ControllerConfig(
                repo_root=tmp_path, plan_path=plan_path, max_turns=1,
                reserved_run_id="second-direct", idempotency_key="second-key",
            ),
            workflow_config, "managed", parsed_plan=object(),
            config_dir=tmp_path, working_dir=tmp_path, runner=runner,
        )
    assert launched is False
    assert admission.reservation("second-direct") is None
    assert not (tmp_path / ".aflow" / "launches" / "second-direct.json").exists()


def test_unbound_release_keeps_partial_manifest_fail_closed(tmp_path: Path) -> None:
    admission = ProjectAdmission(tmp_path)
    reservation = admission.acquire(
        "partial-manifest", idempotency_key="partial-manifest-key"
    )
    _manifest(tmp_path, "partial-manifest", phase="manifest_only")

    with pytest.raises(ProjectAdmissionSafetyError, match="run evidence exists"):
        admission.release(
            reservation.run_id,
            reservation.nonce,
            reason="launch_manifest_failed",
        )

    assert admission.snapshot().occupied_count == 1


def test_nonce_less_resume_worker_rechecks_predecessor_inactivity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed_sources: list[str | None] = []
    run_id = "resume-worker"
    predecessor = "resume-predecessor"

    class FakeAdmission:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def ensure(
            self,
            _run_id: str,
            *,
            plan_path: Path,
            idempotency_key: str | None,
            source_run_id: str | None,
        ) -> object:
            assert idempotency_key == "resume-key"
            assert plan_path == tmp_path / "plan.md"
            observed_sources.append(source_run_id)
            raise ProjectAdmissionError(
                "resume predecessor inactivity is not proven"
            )

    application = SimpleNamespace(
        repository=SimpleNamespace(
            get_launch_manifest=lambda _run_id: SimpleNamespace(
                intended_unit=f"aflow-run-{run_id}.service",
                idempotency_key="resume-key",
                plan_path=str(tmp_path / "plan.md"),
            )
        ),
        units=SimpleNamespace(),
    )
    monkeypatch.setattr("aflow.daemon.ProjectAdmission", FakeAdmission)
    monkeypatch.setattr(
        "aflow.daemon.compose_control_plane",
        lambda *_args, **_kwargs: application,
    )
    monkeypatch.setattr(
        "aflow.daemon.load_live_config",
        lambda *_args, **_kwargs: SimpleNamespace(workflow_config=object()),
    )
    monkeypatch.setattr(
        "aflow.daemon.DaemonService._read_record",
        lambda _service, _run_id: {
            "schema_version": 1,
            "run_id": run_id,
            "resumed_from_run_id": predecessor,
            "mode": "resume",
        },
    )

    from aflow.daemon import worker_main

    result = worker_main(
        repo_root=tmp_path,
        config_path=tmp_path / "aflow.toml",
        run_id=run_id,
    )

    assert result == 1
    assert observed_sources == [predecessor]
    assert "predecessor inactivity" in capsys.readouterr().err


def test_startup_question_releases_capacity_but_retains_claim(tmp_path: Path) -> None:
    admission = ProjectAdmission(tmp_path)
    reservation = admission.acquire("question-run", idempotency_key="question-key")
    _manifest(tmp_path, "question-run", phase="manifest_only")
    startup = tmp_path / ".aflow" / "start-requests"
    startup.mkdir(parents=True, exist_ok=True)
    (startup / "question-run.json").write_text(
        '{"schema_version":1,"run_id":"question-run",'
        '"state":"awaiting_startup_answer","question_generation":1,'
        '"question":{"kind":"pick_step","message":"Choose",'
        '"options":{},"choices":["implement"]}}\n',
        encoding="utf-8",
    )

    snapshot = admission.snapshot()
    assert snapshot.occupied_count == 0
    assert snapshot.claim_retained_run_ids == ("question-run",)
    reacquired = admission.ensure("question-run", idempotency_key="question-key")
    assert reacquired.nonce != reservation.nonce
    assert admission.snapshot().occupied_count == 1


def test_successor_startup_question_keeps_predecessor_claim(tmp_path: Path) -> None:
    _terminal_manifest(tmp_path, "source")
    admission = ProjectAdmission(tmp_path)
    admission.acquire(
        "question-successor", idempotency_key="question-key", source_run_id="source"
    )
    _manifest(tmp_path, "question-successor", phase="manifest_only")
    startup_root = tmp_path / ".aflow" / "start-requests"
    startup_root.mkdir(parents=True, exist_ok=True)
    (startup_root / "question-successor.json").write_text(
        json.dumps({
            "schema_version": 1,
            "run_id": "question-successor",
            "operation": "resume",
            "resumed_from_run_id": "source",
            "state": "awaiting_startup_answer",
            "question_generation": 1,
            "question": {
                "kind": "pick_step", "message": "Choose", "options": {},
                "choices": ["implement"],
            },
        }) + "\n",
        encoding="utf-8",
    )
    assert admission.snapshot().occupied_count == 0
    with pytest.raises(ProjectAdmissionConflict, match="unresolved successor"):
        admission.acquire(
            "other-successor", idempotency_key="other-key", source_run_id="source"
        )
    assert admission.reservation("other-successor") is None


def test_reasoned_ordinary_releases_do_not_fill_bounded_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admission_module, "MAX_RESERVATIONS", 3)
    admission = ProjectAdmission(tmp_path)

    for index in range(12):
        reservation = admission.acquire(
            f"ordinary-failure-{index}",
            idempotency_key=f"ordinary-failure-key-{index}",
        )
        released = admission.release(
            reservation.run_id,
            reservation.nonce,
            reason="startup_preparation_failed",
        )
        assert released.claim_retained is False

    assert admission.snapshot().occupied_count == 0
    assert admission.snapshot().claim_retained_run_ids == ()
    fresh = admission.acquire("ordinary-failure-fresh", idempotency_key="fresh-key")
    assert fresh.state == "reserved"


def test_uncertain_launch_is_not_freed_when_the_unit_or_pid_disappears(tmp_path: Path) -> None:
    _manifest(tmp_path, "uncertain-run", phase="launch_started")
    admission = ProjectAdmission(tmp_path)

    snapshot = admission.snapshot()
    assert snapshot.uncertain_count == 1
    assert snapshot.occupied_count == 1

    # Reconciliation sees the same ambiguous canonical evidence again; there
    # is no timeout or PID heuristic that can release this slot.
    assert admission.snapshot().uncertain_count == 1


@pytest.mark.parametrize(
    ("reason", "observed_unit_state"),
    [
        ("running state has no exact active workflow unit; explicit resume is required", "missing"),
        ("exact workflow unit failed; explicit resume is required", "failed"),
    ],
)
def test_needs_attention_unit_reconciliation_does_not_prove_predecessor_inactive(
    tmp_path: Path,
    reason: str,
    observed_unit_state: str,
) -> None:
    predecessor = "needs-attention-predecessor"
    _manifest(tmp_path, predecessor, phase="launch_started")
    run_dir = tmp_path / ".aflow" / "runs" / predecessor
    append_run_event(
        run_dir,
        "reconciled",
        {
            "status": "needs_attention",
            "reason": reason,
            "unit_name": f"aflow-run-{predecessor}.service",
            "observed_unit_state": observed_unit_state,
        },
    )

    with pytest.raises(ProjectAdmissionConflict, match="predecessor inactivity"):
        ProjectAdmission(tmp_path).acquire(
            "needs-attention-successor",
            idempotency_key="needs-attention-successor-key",
            source_run_id=predecessor,
        )


def test_stale_reconciliation_cannot_bypass_later_launch_evidence(tmp_path: Path) -> None:
    predecessor = "stale-reconciliation-predecessor"
    _manifest(tmp_path, predecessor, phase="launch_started")
    run_dir = tmp_path / ".aflow" / "runs" / predecessor
    append_run_event(
        run_dir,
        "reconciled",
        {
            "status": "needs_attention",
            "reason": "running state has no exact active workflow unit; explicit resume is required",
            "unit_name": f"aflow-run-{predecessor}.service",
            "observed_unit_state": "missing",
        },
    )
    append_run_event(
        run_dir,
        "daemon_start_attempt",
        {"unit_name": f"aflow-run-{predecessor}.service"},
    )

    with pytest.raises(ProjectAdmissionConflict, match="predecessor inactivity"):
        ProjectAdmission(tmp_path).acquire(
            "stale-reconciliation-successor",
            idempotency_key="stale-reconciliation-successor-key",
            source_run_id=predecessor,
        )


def test_unbound_owner_keeps_capacity_when_birth_identity_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = ProjectAdmission(tmp_path)
    reservation = admission.acquire("unobservable-owner", idempotency_key="owner-key")
    monkeypatch.setattr(admission_module, "process_birth_identity", lambda _pid: None)
    monkeypatch.setattr(admission_module, "process_liveness", lambda _pid: "unknown")

    retained = admission.reservation(reservation.run_id)
    assert retained is not None
    assert retained.state in {"reserved", "uncertain"}
    assert admission.snapshot().occupied_count == 1

    monkeypatch.setattr(admission_module, "process_liveness", lambda _pid: "absent")
    assert admission.snapshot().occupied_count == 0


def test_unbound_owner_keeps_capacity_when_birth_identity_source_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ProjectSettingsService(tmp_path)
    current_settings = settings.read()
    settings.update(
        ProjectSettings(max_concurrent_implementations=1),
        expected_revision=current_settings.revision,
    )

    observed_births = iter(
        (
            "linux-start-ticks:4242",
            "ps-lstart:Mon Sep  8 12:34:56 2026",
        )
    )

    def fake_birth_identity(_pid: int) -> str:
        try:
            return next(observed_births)
        except StopIteration:
            return "ps-lstart:Mon Sep  8 12:34:56 2026"

    monkeypatch.setattr(admission_module, "process_birth_identity", fake_birth_identity)
    monkeypatch.setattr(admission_module, "process_liveness", lambda _pid: "present")

    admission = ProjectAdmission(tmp_path)
    reservation = admission.acquire("source-switch-owner", idempotency_key="owner-key")
    assert reservation.owner_birth == "linux-start-ticks:4242"

    snapshot = admission.snapshot()
    assert snapshot.occupied_count == 1
    assert snapshot.available_slots == 0
    with pytest.raises(ProjectCapacityReached):
        admission.acquire("source-switch-successor", idempotency_key="successor-key")


def test_reacquired_startup_claim_keeps_capacity_when_owner_birth_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = ProjectAdmission(tmp_path)
    reservation = admission.acquire("reacquired-claim", idempotency_key="claim-key")
    _manifest(tmp_path, "reacquired-claim", phase="manifest_only")
    startup = tmp_path / ".aflow" / "start-requests"
    startup.mkdir(parents=True, exist_ok=True)
    (startup / "reacquired-claim.json").write_text(
        '{"schema_version":1,"run_id":"reacquired-claim",'
        '"state":"awaiting_startup_answer","question_generation":1,'
        '"question":{"kind":"pick_step","message":"Choose",'
        '"options":{},"choices":["implement"]}}\n',
        encoding="utf-8",
    )
    assert admission.snapshot().occupied_count == 0
    reacquired = admission.ensure("reacquired-claim", idempotency_key="claim-key")
    assert reacquired.nonce != reservation.nonce

    monkeypatch.setattr(admission_module, "process_birth_identity", lambda _pid: None)
    monkeypatch.setattr(admission_module, "process_liveness", lambda _pid: "unknown")

    assert admission.snapshot().occupied_count == 1
    retained = admission.reservation("reacquired-claim")
    assert retained is not None
    assert retained.state == "reserved"
    assert retained.claim_retained is True


@pytest.mark.parametrize("artifact", ["malformed", "unsafe"])
def test_settings_read_failures_are_bounded_admission_errors(
    tmp_path: Path, artifact: str
) -> None:
    settings_path = tmp_path / ".aflow" / "project-settings.json"
    settings_path.parent.mkdir()
    if artifact == "malformed":
        settings_path.write_text(
            '{"provider":"raw-provider-detail",', encoding="utf-8"
        )
    else:
        outside = tmp_path / "outside-settings.json"
        outside.write_text('{"provider":"raw-provider-detail"}\n', encoding="utf-8")
        settings_path.symlink_to(outside)

    with pytest.raises(ProjectAdmissionSafetyError) as raised:
        ProjectAdmission(tmp_path).snapshot()

    assert raised.value.code == "project_admission_error"
    assert str(raised.value) == "project admission settings are unavailable or invalid"
    assert str(tmp_path) not in raised.value.safe_message
    assert "raw-provider-detail" not in raised.value.safe_message


def test_admission_rejects_unsafe_legacy_run_directory_identity(tmp_path: Path) -> None:
    (tmp_path / ".aflow" / "runs" / "prior.before").mkdir(parents=True)

    with pytest.raises(ProjectAdmissionSafetyError, match="invalid identity"):
        ProjectAdmission(tmp_path).snapshot()


def test_unbound_reservation_crash_recovery_uses_owner_birth_evidence(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    process = ctx.Process(target=_crash_worker, args=(str(tmp_path), results))
    process.start()
    nonce = results.get(timeout=10)
    process.join(timeout=10)
    assert process.exitcode == 0
    assert nonce
    assert ProjectAdmission(tmp_path).snapshot().occupied_count == 0


@pytest.mark.parametrize("predecessor_kind", ("absent", "uncertain", "active"))
def test_resume_rejects_non_authoritative_predecessor_inactivity(
    tmp_path: Path, predecessor_kind: str
) -> None:
    root = tmp_path / predecessor_kind
    root.mkdir()
    predecessor = f"{predecessor_kind}-predecessor"
    units = InMemoryUnitManager()
    if predecessor_kind == "uncertain":
        _manifest(root, predecessor, phase="launch_started")
    elif predecessor_kind == "active":
        _manifest(root, predecessor, phase="launch_started")
        units.units[f"aflow-run-{predecessor}.service"] = UnitState(
            name=f"aflow-run-{predecessor}.service",
            active_state="active",
            sub_state="running",
        )

    admission = ProjectAdmission(root, unit_manager=units)
    with pytest.raises(ProjectAdmissionConflict, match="predecessor inactivity"):
        admission.acquire(
            f"{predecessor_kind}-successor",
            idempotency_key=f"{predecessor_kind}-successor-key",
            source_run_id=predecessor,
            # The compatibility flag must not be able to bypass canonical
            # predecessor evidence.
            predecessor_inactive_proven=True,
        )


@pytest.mark.parametrize("predecessor_kind", ("terminal", "owner-stopped"))
def test_resume_accepts_only_authoritative_inactive_predecessor(
    tmp_path: Path, predecessor_kind: str
) -> None:
    root = tmp_path / predecessor_kind
    root.mkdir()
    predecessor = f"{predecessor_kind}-predecessor"
    if predecessor_kind == "terminal":
        _terminal_manifest(root, predecessor)
    else:
        _manifest(root, predecessor, phase="owner_stopped")

    reservation = ProjectAdmission(root).acquire(
        f"{predecessor_kind}-successor",
        idempotency_key=f"{predecessor_kind}-successor-key",
        source_run_id=predecessor,
    )
    assert reservation.source_run_id == predecessor


def test_successor_claim_replays_exactly_and_blocks_distinct_requests(
    tmp_path: Path,
) -> None:
    _terminal_manifest(tmp_path, "source")
    admission = ProjectAdmission(tmp_path)
    first = admission.acquire(
        "successor-one", idempotency_key="first-key", source_run_id="source"
    )
    replay = admission.acquire(
        "successor-one", idempotency_key="first-key", source_run_id="source"
    )
    assert replay.nonce == first.nonce
    with pytest.raises(ProjectAdmissionConflict, match="different launch request"):
        admission.acquire(
            "successor-one", idempotency_key="changed-key", source_run_id="source"
        )
    with pytest.raises(ProjectAdmissionConflict, match="unresolved successor"):
        admission.acquire(
            "successor-two", idempotency_key="second-key", source_run_id="source"
        )
    assert admission.snapshot().occupied_count == 1
    assert admission.reservation("successor-two") is None

    _terminal_manifest(tmp_path, "successor-one")
    assert admission.snapshot().occupied_count == 0
    second = admission.acquire(
        "successor-two", idempotency_key="second-key", source_run_id="source"
    )
    assert second.source_run_id == "source"


def test_unpublished_successor_release_allows_a_new_claim(tmp_path: Path) -> None:
    _terminal_manifest(tmp_path, "source")
    admission = ProjectAdmission(tmp_path)
    first = admission.acquire(
        "unpublished-successor", idempotency_key="first-key", source_run_id="source"
    )
    admission.release(first.run_id, first.nonce, reason="publication_failed")

    second = admission.acquire(
        "replacement-successor", idempotency_key="second-key", source_run_id="source"
    )
    assert second.source_run_id == "source"
    assert admission.snapshot().occupied_count == 1


def test_uncertain_published_successor_blocks_after_journal_loss(tmp_path: Path) -> None:
    _terminal_manifest(tmp_path, "source")
    admission = ProjectAdmission(tmp_path)
    first = admission.acquire(
        "partial-successor", idempotency_key="first-key", source_run_id="source"
    )
    _manifest(tmp_path, "partial-successor", phase="launch_requested")
    startup_root = tmp_path / ".aflow" / "start-requests"
    startup_root.mkdir(parents=True)
    (startup_root / "partial-successor.json").write_text(
        json.dumps({
            "schema_version": 1,
            "run_id": "partial-successor",
            "operation": "resume",
            "resumed_from_run_id": "source",
            "state": "prepared",
        }) + "\n",
        encoding="utf-8",
    )
    admission.bind(first.run_id, first.nonce)
    admission.state_path.unlink()

    with pytest.raises(ProjectAdmissionConflict, match="unresolved successor"):
        admission.acquire(
            "other-successor", idempotency_key="other-key", source_run_id="source"
        )
    assert admission.reservation("other-successor") is None
    assert admission.snapshot().occupied_count == 1


def test_direct_controller_lineage_blocks_without_a_journal_claim(tmp_path: Path) -> None:
    _terminal_manifest(tmp_path, "source")
    _manifest(tmp_path, "direct-successor", phase="unit_started")
    run_dir = tmp_path / ".aflow" / "runs" / "direct-successor"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"status": "running", "resumed_from_run_id": "source"}) + "\n",
        encoding="utf-8",
    )

    admission = ProjectAdmission(tmp_path)
    with pytest.raises(ProjectAdmissionConflict, match="unresolved successor"):
        admission.acquire(
            "other-successor", idempotency_key="other-key", source_run_id="source"
        )
    assert admission.reservation("other-successor") is None


def test_existing_canonical_runs_count_without_a_reservation(tmp_path: Path) -> None:
    _manifest(tmp_path, "existing-run", phase="launch_started")
    admission = ProjectAdmission(tmp_path)
    first = admission.acquire("new-run", idempotency_key="new-key")
    assert first.run_id == "new-run"
    with pytest.raises(ProjectCapacityReached):
        admission.acquire("another-run", idempotency_key="another-key")
    assert admission.snapshot().unmanaged_run_ids == ("existing-run",)


def test_lowering_limit_blocks_new_work_without_interrupting_existing_claims(tmp_path: Path) -> None:
    service = ProjectSettingsService(tmp_path)
    first_settings = service.read()
    service.update(
        ProjectSettings(max_concurrent_implementations=2),
        expected_revision=first_settings.revision,
    )
    admission = ProjectAdmission(tmp_path)
    first = admission.acquire("first-run", idempotency_key="first")
    second = admission.acquire("second-run", idempotency_key="second")
    lowered = service.read()
    service.update(
        ProjectSettings(max_concurrent_implementations=1),
        expected_revision=lowered.revision,
    )

    snapshot = admission.snapshot()
    assert snapshot.occupied_count == 2
    assert snapshot.limit == 1
    with pytest.raises(ProjectCapacityReached):
        admission.acquire("third-run", idempotency_key="third")

    _terminal_manifest(tmp_path, "first-run")
    assert admission.snapshot().occupied_count == 1
    with pytest.raises(ProjectCapacityReached):
        admission.acquire("third-run", idempotency_key="third")
    assert admission.reservation("second-run").nonce == second.nonce
    assert first.nonce != second.nonce


def test_automatic_capacity_rejection_is_bounded_and_deferable(tmp_path: Path) -> None:
    admission = ProjectAdmission(tmp_path)
    admission.acquire("one", idempotency_key="one")
    admission.acquire("two", idempotency_key="two")
    with pytest.raises(ProjectCapacityReached) as rejected:
        admission.acquire("queued", idempotency_key="queued", automatic=True)
    assert rejected.value.automatic is True
    assert rejected.value.code == "project_capacity_reached"


def test_external_git_metadata_admission_discovers_linked_siblings(tmp_path: Path) -> None:
    primary = _committed_separate_git_repo(tmp_path / "primary")
    first = tmp_path / "first-worktree"
    second = tmp_path / "second-worktree"
    _git(primary, "worktree", "add", "-q", "-b", "first", str(first))
    _git(primary, "worktree", "add", "-q", "-b", "second", str(second))

    _manifest(second, "sibling-run", phase="launch_started")
    admission = ProjectAdmission(first)

    assert (first / ".git").is_file()
    assert second.resolve() in admission._repository_roots()
    assert admission.snapshot().occupied_count == 1

    admission.acquire("new-run", idempotency_key="new-key")
    assert admission.snapshot().occupied_count == 2
    with pytest.raises(ProjectCapacityReached):
        admission.acquire("third-run", idempotency_key="third-key")


def test_released_history_compacts_before_capacity_state_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build a full journal first, then lower the bound to model an existing
    # state written under an older/equivalent bounded configuration.
    monkeypatch.setattr(admission_module, "MAX_RESERVATIONS", 5)
    admission = ProjectAdmission(tmp_path)
    live = admission.acquire("live-run", idempotency_key="live-key")
    claim = admission.acquire("claim-run", idempotency_key="claim-key")
    admission.release(claim.run_id, claim.nonce, reason="startup_question", claim_retained=True)
    replay = admission.acquire("replay-run", idempotency_key="replay-key")
    admission.release(replay.run_id, replay.nonce)
    old_one = admission.acquire("old-one", idempotency_key="old-one-key")
    admission.release(old_one.run_id, old_one.nonce)
    old_two = admission.acquire("old-two", idempotency_key="old-two-key")
    admission.release(old_two.run_id, old_two.nonce)

    state = json.loads(admission.state_path.read_text(encoding="utf-8"))
    assert len(state["reservations"]) == 5

    monkeypatch.setattr(admission_module, "MAX_RESERVATIONS", 4)
    replay_identity = admission.reservation("replay-run")
    assert replay_identity is not None
    assert replay_identity.nonce == replay.nonce
    assert admission.reservation("old-one") is None
    assert admission.reservation("old-two") is None
    assert admission.reservation("live-run").nonce == live.nonce
    assert admission.reservation("claim-run").nonce == claim.nonce

    admission.acquire("fresh-run", idempotency_key="fresh-key")
    snapshot = admission.snapshot()
    assert snapshot.occupied_count == 2
    assert snapshot.claim_retained_run_ids == ("claim-run",)
    state = json.loads(admission.state_path.read_text(encoding="utf-8"))
    assert len(state["reservations"]) <= 4

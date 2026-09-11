from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
from threading import Event

import pytest

import aflow.workflow as workflow_module
from aflow.config import (
    GoTransition,
    HarnessProfileConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.control_plane import (
    ControlConflictError,
    RestartRequiredControlError,
    RunControlRequest,
    compare_and_swap_overrides,
    read_events,
)
from aflow.control_plane.persistence import PersistenceError
from aflow.cli import _bootstrap_resume_invocation
from aflow.run_state import (
    ControllerConfig,
    load_override_request,
)
from aflow.workflow import WorkflowError, run_workflow


def _run_dir(tmp_path: Path, run_id: str = "control-run-6") -> Path:
    path = tmp_path / ".aflow" / "runs" / run_id
    path.mkdir(parents=True)
    return path


def test_compare_and_swap_controls_preserves_override_authority(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    result = compare_and_swap_overrides(
        tmp_path,
        run_dir.name,
        RunControlRequest(
            expected_revision=0,
            max_turns=9,
            team="focused",
            role_selectors={"worker": "codex.high"},
        ),
    )
    assert result.revision == 1
    loaded = load_override_request(run_dir / "overrides.toml")
    assert loaded.status == "valid"
    assert loaded.request is not None
    assert loaded.request.revision == 1
    assert loaded.request.max_turns == 9
    assert loaded.request.team == "focused"
    assert loaded.request.role_selectors == {"worker": "codex.high"}
    assert read_events(run_dir)[-1].event_type == "control_changed"


def test_stale_control_cas_and_restart_required_leave_file_unchanged(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    compare_and_swap_overrides(
        tmp_path, run_dir.name, RunControlRequest(expected_revision=0, max_turns=4)
    )
    original = (run_dir / "overrides.toml").read_bytes()

    with pytest.raises(ControlConflictError) as conflict:
        compare_and_swap_overrides(
            tmp_path, run_dir.name, RunControlRequest(expected_revision=0, max_turns=5)
        )
    assert conflict.value.current_revision == 1
    assert (run_dir / "overrides.toml").read_bytes() == original

    with pytest.raises(RestartRequiredControlError, match="workflow"):
        compare_and_swap_overrides(
            tmp_path,
            run_dir.name,
            RunControlRequest(expected_revision=1, unsafe_changes={"workflow": "other"}),
        )
    assert (run_dir / "overrides.toml").read_bytes() == original


def test_concurrent_control_writes_allow_one_revision_winner(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)

    def write(value: int) -> str:
        try:
            compare_and_swap_overrides(
                tmp_path,
                run_dir.name,
                RunControlRequest(expected_revision=0, max_turns=value),
            )
        except ControlConflictError:
            return "conflict"
        return "written"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(write, range(2, 10)))
    assert outcomes.count("written") == 1
    assert outcomes.count("conflict") == 7


def test_generic_control_cas_rejects_a_missing_run_without_allocating_it(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / ".aflow" / "runs" / "missing-run"

    with pytest.raises(PersistenceError, match="does not exist"):
        compare_and_swap_overrides(
            tmp_path,
            "missing-run",
            RunControlRequest(expected_revision=0, owner_stop=True),
        )

    assert not run_dir.exists()


def test_owner_stop_is_a_valid_revisioned_override(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    result = compare_and_swap_overrides(
        tmp_path, run_dir.name, RunControlRequest(expected_revision=0, owner_stop=True)
    )
    assert result.owner_stop is True
    loaded = load_override_request(run_dir / "overrides.toml")
    assert loaded.request is not None
    assert loaded.request.owner_stop is True


def test_owner_stop_terminalizes_at_the_existing_pre_turn_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    config = WorkflowUserConfig(
        roles={"worker": "codex.high"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"high": HarnessProfileConfig(model="high-model")}
            )
        },
        workflows={
            "live": WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="END", when="DONE"),),
                    )
                },
                first_step="implement",
            )
        },
        prompts={"p": "Work."},
    )
    original_create_paths = workflow_module.create_run_paths

    def create_paths_with_owner_stop(controller_config: ControllerConfig):
        paths = original_create_paths(controller_config)
        (paths.run_dir / "overrides.toml").write_text("owner_stop = true\n")
        return paths

    monkeypatch.setattr(workflow_module, "create_run_paths", create_paths_with_owner_stop)
    result = run_workflow(
        ControllerConfig(
            repo_root=tmp_path,
            plan_path=plan,
            max_turns=2,
            reserved_run_id="owner-stop-1",
        ),
        config,
        "live",
        config_dir=tmp_path,
        snapshot_config=False,
        runner=lambda *args, **kwargs: pytest.fail("owner stop must precede child launch"),
    )

    assert result.status == "owner_stopped"
    assert result.end_reason == "owner_stopped"
    metadata = (result.run_dir / "run.json").read_text()
    assert '"status": "owner_stopped"' in metadata
    assert read_events(result.run_dir)[-1].event_type == "owner_stopped"


def test_owner_stop_waits_for_a_blocked_turn_and_never_launches_the_next_one(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    config = WorkflowUserConfig(
        roles={"worker": "codex.high"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"high": HarnessProfileConfig(model="high-model")}
            )
        },
        workflows={
            "live": WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="implement"),),
                    )
                },
                first_step="implement",
            )
        },
        prompts={"p": "Work."},
    )
    entered = Event()
    release = Event()
    invocations: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        invocations.append(argv)
        entered.set()
        assert release.wait(timeout=5), "fake worker did not receive its release"
        return subprocess.CompletedProcess(argv, 0, "turn finished\n", "")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_workflow,
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan,
                max_turns=3,
                reserved_run_id="delayed-owner-stop-1",
            ),
            config,
            "live",
            config_dir=tmp_path,
            snapshot_config=False,
            runner=runner,
        )
        assert entered.wait(timeout=5), "fake worker did not start"
        run_dir = tmp_path / ".aflow" / "runs" / "delayed-owner-stop-1"
        active_metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert active_metadata["status"] == "running"

        requested = compare_and_swap_overrides(
            tmp_path,
            run_dir.name,
            RunControlRequest(expected_revision=0, owner_stop=True),
        )
        assert requested.owner_stop is True
        # Saving the intent does not terminalize a live turn or invoke a stop.
        still_active = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert still_active["status"] == "running"
        assert not future.done()

        release.set()
        result = future.result(timeout=5)

    assert result.status == "owner_stopped"
    assert len(invocations) == 1
    terminal_metadata = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert terminal_metadata["status"] == "owner_stopped"
    turn_result = json.loads(
        (result.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert turn_result["status"] == "completed"
    assert read_events(result.run_dir)[-1].event_type == "owner_stopped"


def test_graceful_stop_resume_reviews_finalized_worker_before_new_implementation(
    tmp_path: Path,
) -> None:
    _run_graceful_stop_resume(
        tmp_path,
        complete_worker=False,
    )


def test_graceful_stop_resume_reviews_finalized_complete_worker_before_new_implementation(
    tmp_path: Path,
) -> None:
    _run_graceful_stop_resume(
        tmp_path,
        complete_worker=True,
    )


def _run_graceful_stop_resume(
    tmp_path: Path,
    *,
    complete_worker: bool,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n"
        "### [ ] Checkpoint 1: First\n"
        "- [ ] step\n",
        encoding="utf-8",
    )
    config = WorkflowUserConfig(
        roles={"worker": "codex.high", "reviewer": "codex.high"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"high": HarnessProfileConfig(model="high-model")}
            )
        },
        workflows={
            "live": WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("implement",),
                        go=(GoTransition(to="review"),),
                    ),
                    "review": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("review",),
                        go=(GoTransition(to="END"),),
                    ),
                },
                first_step="implement",
            )
        },
        prompts={
            "implement": "Implement the checkpoint.",
            "review": "Review the completed worker turn.",
        },
    )
    entered = Event()
    release = Event()
    source_invocations: list[str] = []

    def delayed_worker(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        source_invocations.append(str(kwargs.get("input", "")))
        entered.set()
        assert release.wait(timeout=5), "fake worker did not receive its release"
        if complete_worker:
            plan.write_text(
                "# Plan\n\n"
                "### [x] Checkpoint 1: First\n"
                "- [x] step\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(argv, 0, "worker output\n", "")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_workflow,
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan,
                max_turns=3,
                reserved_run_id="graceful-stop-source",
            ),
            config,
            "live",
            config_dir=tmp_path,
            snapshot_config=False,
            runner=delayed_worker,
        )
        assert entered.wait(timeout=5), "fake worker did not start"
        source_dir = tmp_path / ".aflow" / "runs" / "graceful-stop-source"
        requested = compare_and_swap_overrides(
            tmp_path,
            source_dir.name,
            RunControlRequest(expected_revision=0, owner_stop=True),
        )
        assert requested.owner_stop is True
        assert not future.done()
        release.set()
        source = future.result(timeout=5)

    assert source.status == "owner_stopped"
    assert len(source_invocations) == 1
    source_metadata = json.loads(
        (source.run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert source_metadata["status"] == "owner_stopped"
    if complete_worker:
        assert source_metadata["last_snapshot"]["is_complete"] is True
    assert source_metadata["active_implementation_scope"]["awaiting_review"] is True
    source_turn = json.loads(
        (source.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert source_turn["step_role"] == "worker"
    assert source_turn["chosen_transition"] == "review"
    assert "worker output" in (
        source.run_dir / "turns" / "turn-001" / "stdout.txt"
    ).read_text(encoding="utf-8")

    config_path = tmp_path / "aflow.toml"
    config_path.write_text("# focused resume fixture\n", encoding="utf-8")
    resume_bootstrap = _bootstrap_resume_invocation(
        repo_root=tmp_path,
        config_path=config_path,
        default_config_path=config_path,
        config_path_is_explicit=True,
        workflow_config=config,
        requested_run_id=source.run_dir.name,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
        live_loader=lambda _path: config,
    )
    resume_context = resume_bootstrap.resume_context
    assert resume_context.interrupted_step_name == "review"
    assert resume_context.active_implementation_scope is not None
    resumed_invocations: list[str] = []
    resumed_scope_ids: list[str] = []

    def resumed_provider(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        resumed_invocations.append(str(kwargs.get("input", "")))
        resumed_metadata = json.loads(
            (
                tmp_path
                / ".aflow"
                / "runs"
                / "graceful-stop-continuation"
                / "run.json"
            ).read_text(encoding="utf-8")
        )
        resumed_scope_ids.append(
            resumed_metadata["active_implementation_scope"]["scope_id"]
        )
        plan.write_text(
            "# Plan\n\n"
            "### [x] Checkpoint 1: First\n"
            "- [x] step\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "review output\n", "")

    continuation = run_workflow(
        ControllerConfig(
            repo_root=tmp_path,
            plan_path=plan,
            max_turns=1,
            reserved_run_id="graceful-stop-continuation",
        ),
        config,
        "live",
        config_dir=tmp_path,
        snapshot_config=False,
        runner=resumed_provider,
        resume=resume_context,
    )

    assert continuation.run_dir != source.run_dir
    assert len(resumed_invocations) == 1
    continuation_metadata = json.loads(
        (continuation.run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert continuation_metadata["resumed_from_run_id"] == source.run_dir.name
    assert resumed_scope_ids == [resume_context.active_implementation_scope.scope_id]
    continuation_turn = json.loads(
        (continuation.run_dir / "turns" / "turn-001" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert continuation_turn["step_name"] == "review"
    assert continuation_turn["step_role"] == "reviewer"
    assert source_turn["step_name"] == "implement"


def test_reserved_run_id_collision_fails_before_launch_artifacts(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    existing_run = _run_dir(tmp_path, "occupied-run")
    existing_marker = existing_run / "legacy.txt"
    existing_marker.write_text("keep me\n")
    config = WorkflowUserConfig(
        roles={"worker": "codex.high"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"high": HarnessProfileConfig(model="high-model")}
            )
        },
        workflows={
            "live": WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="END", when="DONE"),),
                    )
                },
                first_step="implement",
            )
        },
        prompts={"p": "Work."},
    )

    with pytest.raises(WorkflowError, match="cannot reserve run identity: .*already has a run directory"):
        run_workflow(
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan,
                max_turns=2,
                reserved_run_id=existing_run.name,
            ),
            config,
            "live",
            config_dir=tmp_path,
            snapshot_config=False,
            runner=lambda *args, **kwargs: pytest.fail("collision must precede child launch"),
        )

    launches = tmp_path / ".aflow" / "launches"
    assert not (launches / f"{existing_run.name}.json").exists()
    assert not (launches / f"{existing_run.name}.state.json").exists()
    assert existing_marker.read_text() == "keep me\n"

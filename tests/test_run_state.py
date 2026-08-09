from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
from aflow.run_state import load_override_request
from aflow.run_state import ControllerConfig
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
        runner=lambda *args, **kwargs: pytest.fail("owner stop must precede child launch"),
    )

    assert result.status == "owner_stopped"
    assert result.end_reason == "owner_stopped"
    metadata = (result.run_dir / "run.json").read_text()
    assert '"status": "owner_stopped"' in metadata
    assert read_events(result.run_dir)[-1].event_type == "owner_stopped"


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
            runner=lambda *args, **kwargs: pytest.fail("collision must precede child launch"),
        )

    launches = tmp_path / ".aflow" / "launches"
    assert not (launches / f"{existing_run.name}.json").exists()
    assert not (launches / f"{existing_run.name}.state.json").exists()
    assert existing_marker.read_text() == "keep me\n"

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from aflow.api.models import PreparedRun, StartupRequest
from aflow.api.startup import PriorWorkStartupError
from aflow.config import (
    AflowSection,
    GoTransition,
    HarnessProfileConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
    load_workflow_config,
)
from aflow.control_plane import (
    InMemoryUnitManager,
    LaunchManifest,
    RunControlRequest,
    compare_and_swap_overrides,
    create_launch_manifest,
    read_events,
    write_launch_phase,
)
from aflow.control_plane.repository import RepositoryError, RunRepository
from aflow.daemon import (
    AflowDaemon,
    DaemonConfig,
    DaemonError,
    DaemonIdempotencyConflict,
    _worker_prepared,
)
from aflow.harnesses.codex import CodexAdapter
from aflow.run_config_snapshot import SnapshotError
from aflow.run_state import ControllerConfig
from aflow.workflow import (
    WorkflowError,
    resolve_profile,
    resolve_role_selector,
    run_workflow,
)
from tests._support import _write_split_config


def _incident_aflow_config(*, current: bool) -> str:
    extra_profile = (
        '\n[harness.codex.profiles.muspark]\nmodel = "model-muspark"\n'
        if current
        else ""
    )
    extra_team = (
        '\n[teams.MusparkGLM]\nworker = "codex.muspark"\n'
        if current
        else ""
    )
    return f'''\
[aflow]
default_workflow = "live"
max_turns = 2

[harness.codex.profiles.base]
model = "model-base"
{extra_profile}
[roles]
worker = "codex.base"

[teams.base]
worker = "codex.base"
{extra_team}
[prompts]
p = "Incident worker prompt {{ACTIVE_PLAN_PATH}}."
'''


_INCIDENT_WORKFLOWS = '''\
[workflow.live]
team = "base"

[workflow.live.steps.work]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }, { to = "work" }]
'''


def _workflow_config() -> WorkflowUserConfig:
    workflow = WorkflowConfig(
        steps={
            "implement": WorkflowStepConfig(
                role="worker",
                prompts=("p",),
                go=(GoTransition(to="END", when="DONE"),),
            )
        },
        first_step="implement",
    )
    return WorkflowUserConfig(
        roles={"worker": "codex.worker"},
        workflows={"managed": workflow},
        prompts={"p": "Work."},
    )


def _pending_review_workflow_config() -> WorkflowUserConfig:
    workflow = WorkflowConfig(
        steps={
            "implement": WorkflowStepConfig(
                role="worker",
                prompts=("p",),
                go=(GoTransition(to="review", when="DONE", preserve_active_plan=True),),
            ),
            "review": WorkflowStepConfig(
                role="reviewer",
                prompts=("p",),
                go=(GoTransition(to="END", when="DONE"),),
            ),
        },
        first_step="implement",
    )
    return WorkflowUserConfig(
        roles={"worker": "codex.worker", "reviewer": "codex.worker"},
        workflows={"managed": workflow},
        prompts={"p": "Work."},
    )


def _daemon_for_config(
    tmp_path: Path,
    monkeypatch,
    units: InMemoryUnitManager,
    workflow_config: WorkflowUserConfig,
) -> tuple[AflowDaemon, StartupRequest]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    config_path = repo_root / "aflow.toml"
    config_path.write_text("")
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _path: workflow_config)
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            environment={"PATH": str(executable.parent)},
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()
    return daemon, StartupRequest(
        repo_root=repo_root,
        plan_path=plan_path,
        config_path=config_path,
        workflow_config=workflow_config,
        workflow_name="managed",
        start_step=None,
        max_turns=None,
        team=None,
    )


def test_daemon_resume_persists_reviewer_start_step_and_replays_once(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    workflow_config = _pending_review_workflow_config()
    daemon, request = _daemon_for_config(tmp_path, monkeypatch, units, workflow_config)
    source_id = "pending-review-source"
    create_launch_manifest(
        request.repo_root,
        LaunchManifest(
            run_id=source_id,
            project_root=str(request.repo_root),
            plan_path=str(request.plan_path),
            workflow_name="managed",
            max_turns=2,
            idempotency_key="source-key",
            caller_scope="project:one",
        ),
    )
    source_dir = request.repo_root / ".aflow" / "runs" / source_id
    source_dir.mkdir()
    source_dir.joinpath("run.json").write_text(
        '{"status":"failed","workflow_name":"managed","team":null,'
        '"selected_start_step":null}'
    )
    write_launch_phase(request.repo_root, source_id, "unit_started")
    before = source_dir.joinpath("run.json").read_bytes()
    sentinel = object()
    monkeypatch.setattr(
        "aflow.cli._bootstrap_resume_invocation",
        lambda **_kwargs: SimpleNamespace(
            workflow_name="managed",
            plan_path=request.plan_path,
            max_turns=2,
            team=None,
            start_step="review",
            extra_instructions=(),
            workflow_config=workflow_config,
            resume_context=sentinel,
        ),
    )

    continuation = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="resume-pending-review",
    )
    replay = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="resume-pending-review",
    )

    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record,
        manifest,
        request.repo_root,
        request.config_path,
        workflow_config,
    )
    assert prepared.start_step == "review"
    assert resume_context is sentinel
    assert source_dir.joinpath("run.json").read_bytes() == before


@pytest.mark.parametrize("remaining_checkpoint", [False, True])
def test_daemon_resume_preview_and_start_reviews_complete_owner_stopped_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remaining_checkpoint: bool,
) -> None:
    """A complete worker snapshot still admits its pending reviewer only."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    remaining = "\n### [ ] Checkpoint 2: Next\n- [ ] next step\n" if remaining_checkpoint else ""
    plan_path.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n" + remaining,
        encoding="utf-8",
    )
    config_path = repo_root / "aflow.toml"
    config_path.write_text("# focused daemon resume fixture\n", encoding="utf-8")
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    workflow_config = WorkflowUserConfig(
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
    monkeypatch.setattr(
        "aflow.daemon.load_workflow_config",
        lambda _path: workflow_config,
    )

    source_id = "complete-owner-stop-source"
    entered = Event()
    release = Event()

    def source_worker(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        entered.set()
        assert release.wait(timeout=5), "fake worker did not receive its release"
        plan_path.write_text(
            "# Plan\n\n"
            "### [x] Checkpoint 1: First\n"
            "- [x] step\n" + remaining,
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "worker output\n", "")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_workflow,
            ControllerConfig(
                repo_root=repo_root,
                plan_path=plan_path,
                max_turns=3,
                reserved_run_id=source_id,
                idempotency_key="source-key",
                caller_scope="project:one",
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=source_worker,
        )
        assert entered.wait(timeout=5), "fake worker did not start"
        requested = compare_and_swap_overrides(
            repo_root,
            source_id,
            RunControlRequest(expected_revision=0, owner_stop=True),
        )
        assert requested.owner_stop is True
        assert not future.done()
        release.set()
        source = future.result(timeout=5)

    assert source.status == "owner_stopped"
    source_run_json = (source.run_dir / "run.json").read_bytes()
    source_turn_result = (
        source.run_dir / "turns" / "turn-001" / "result.json"
    ).read_bytes()
    source_metadata = json.loads(source_run_json)
    assert source_metadata["last_snapshot"]["is_complete"] is (not remaining_checkpoint)
    source_scope = source_metadata["active_implementation_scope"]
    assert isinstance(source_scope, dict)
    scope_id = source_scope["scope_id"]
    envelope_path = source.run_dir / source_scope["envelope_artifact_path"]
    envelope_bytes = envelope_path.read_bytes()

    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status(source_id)
    assert preview.status == "owner_stopped"
    assert preview.evidence["can_resume"] is True

    continuation = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="resume-key",
    )
    assert continuation.run_id != source_id
    assert len(units.start_calls) == 1
    assert units.stop_calls == []
    record = daemon.service._read_record(continuation.run_id)
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    prepared, resume_context = _worker_prepared(
        record,
        manifest,
        repo_root,
        config_path,
        workflow_config,
    )
    assert prepared.start_step == "implement"
    assert resume_context is not None
    assert resume_context.interrupted_step_name == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.scope_id == scope_id
    assert resume_context.scope_envelope_bytes == envelope_bytes
    assert resume_context.scope_evidence_artifact_bytes
    assert all(
        (source.run_dir / relative_path).read_bytes() == artifact_bytes
        for relative_path, artifact_bytes in resume_context.scope_evidence_artifact_bytes.items()
    )

    reviewer_invocations: list[str] = []

    def reviewer(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        reviewer_invocations.append(str(kwargs.get("input", "")))
        return subprocess.CompletedProcess(argv, 0, "review output\n", "")

    try:
        continuation_result = run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=prepared.plan_path,
                max_turns=prepared.max_turns,
                team=prepared.team,
                extra_instructions=prepared.extra_instructions,
                start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=reviewer,
            resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        assert remaining_checkpoint
        assert "workflow selected END while the active plan remains incomplete" in str(exc)
        continuation_run_dir = exc.run_dir
    else:
        assert not remaining_checkpoint
        assert continuation_result.status == "completed"
        continuation_run_dir = continuation_result.run_dir
    assert len(reviewer_invocations) == 1
    continuation_turn = json.loads(
        (
            continuation_run_dir / "turns" / "turn-001" / "result.json"
        ).read_text(encoding="utf-8")
    )
    assert continuation_turn["step_name"] == "review"
    assert continuation_turn["step_role"] == "reviewer"
    assert (source.run_dir / "run.json").read_bytes() == source_run_json
    assert (
        source.run_dir / "turns" / "turn-001" / "result.json"
    ).read_bytes() == source_turn_result


def test_daemon_resume_starts_first_cumulative_review_after_owner_stopped_checkpoint_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalized checkpoint reviewer awaiting its first cumulative review resumes that review only."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    plan_path.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    config_path = repo_root / "aflow.toml"
    config_path.write_text(
        "# focused daemon first-final-review fixture\n", encoding="utf-8"
    )
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    workflow_config = WorkflowUserConfig(
        roles={
            "worker": "codex.high",
            "reviewer": "codex.high",
            "architect": "codex.high",
        },
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
                        go=(GoTransition(to="final_review"),),
                    ),
                    "final_review": WorkflowStepConfig(
                        role="architect",
                        prompts=("final_review",),
                        go=(GoTransition(to="END"),),
                    ),
                },
                first_step="implement",
            )
        },
        prompts={
            "implement": "Implement the checkpoint.",
            "review": "Review the completed worker turn.",
            "final_review": "Run the cumulative final review.",
        },
    )
    monkeypatch.setattr(
        "aflow.daemon.load_workflow_config",
        lambda _path: workflow_config,
    )

    source_id = "first-final-review-source"
    entered_review = Event()
    release_review = Event()
    invocations: list[str] = []

    def source_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if not invocations:
            invocations.append("implement")
            plan_path.write_text(
                "# Plan\n\n"
                "### [x] Checkpoint 1: First\n"
                "- [x] step\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        invocations.append("review")
        entered_review.set()
        assert release_review.wait(timeout=5), "fake reviewer did not receive its release"
        return subprocess.CompletedProcess(argv, 0, "review output\n", "")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_workflow,
            ControllerConfig(
                repo_root=repo_root,
                plan_path=plan_path,
                max_turns=4,
                reserved_run_id=source_id,
                idempotency_key="source-key",
                caller_scope="project:one",
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=source_runner,
        )
        assert entered_review.wait(timeout=5), "fake reviewer did not start"
        requested = compare_and_swap_overrides(
            repo_root,
            source_id,
            RunControlRequest(expected_revision=0, owner_stop=True),
        )
        assert requested.owner_stop is True
        assert not future.done()
        release_review.set()
        source = future.result(timeout=5)

    assert source.status == "owner_stopped"
    source_run_json = (source.run_dir / "run.json").read_bytes()
    review_turn_result = (
        source.run_dir / "turns" / "turn-002" / "result.json"
    ).read_bytes()
    source_metadata = json.loads(source_run_json)
    assert source_metadata["last_snapshot"]["is_complete"] is True
    assert source_metadata["active_implementation_scope"] is None
    assert source_metadata["current_step_name"] == "final_review"
    review_turn = json.loads(review_turn_result)
    assert review_turn["step_name"] == "review"
    assert review_turn["step_role"] == "reviewer"
    assert review_turn["chosen_transition"] == "final_review"
    assert review_turn["returncode"] == 0
    assert review_turn["snapshot_after"]["is_complete"] is True

    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    # The current stop intent is still set: the preview and the shared
    # bootstrap admission must both refuse before any successor is reserved.
    source_overrides_bytes = (source.run_dir / "overrides.toml").read_bytes()
    blocked_preview = daemon.service.run_status(source_id)
    assert blocked_preview.status == "owner_stopped"
    assert blocked_preview.evidence["can_resume"] is False
    with pytest.raises(ValueError, match=r"is not resumable"):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="resume-key",
        )
    assert units.start_calls == []
    assert units.stop_calls == []
    assert list(source.run_dir.parent.iterdir()) == [source.run_dir]
    launches_root = repo_root / ".aflow" / "launches"
    assert sorted(p.name for p in launches_root.iterdir()) == [
        f"{source_id}.json",
        f"{source_id}.state.json",
    ]
    assert (source.run_dir / "run.json").read_bytes() == source_run_json
    assert (
        source.run_dir / "turns" / "turn-002" / "result.json"
    ).read_bytes() == review_turn_result
    assert (
        source.run_dir / "overrides.toml"
    ).read_bytes() == source_overrides_bytes

    cleared = compare_and_swap_overrides(
        repo_root,
        source_id,
        RunControlRequest(expected_revision=1, owner_stop=False),
    )
    assert cleared.owner_stop is False

    preview = daemon.service.run_status(source_id)
    assert preview.status == "owner_stopped"
    assert preview.evidence["can_resume"] is True

    continuation = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="resume-key",
    )
    assert continuation.created is True
    assert continuation.run_id != source_id
    assert len(units.start_calls) == 1
    assert units.stop_calls == []
    record = daemon.service._read_record(continuation.run_id)
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    prepared, resume_context = _worker_prepared(
        record,
        manifest,
        repo_root,
        config_path,
        workflow_config,
        managed_inactivity_check=lambda _run_id: True,
    )
    assert prepared.start_step == "final_review"
    assert resume_context is not None
    assert resume_context.interrupted_step_name == "final_review"
    assert resume_context.active_implementation_scope is None
    assert resume_context.pending_cumulative_review is not None
    assert (
        resume_context.pending_cumulative_review.reviewer_step_name == "final_review"
    )

    replay = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="resume-key",
    )
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1

    final_review_invocations: list[str] = []

    def final_reviewer(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        final_review_invocations.append(str(kwargs.get("input", "")))
        return subprocess.CompletedProcess(argv, 0, "final review output\n", "")

    continuation_result = run_workflow(
        ControllerConfig(
            repo_root=repo_root,
            plan_path=prepared.plan_path,
            max_turns=prepared.max_turns,
            team=prepared.team,
            extra_instructions=prepared.extra_instructions,
            start_step=prepared.start_step,
            reserved_run_id=prepared.reserved_run_id,
            idempotency_key=prepared.idempotency_key,
            caller_scope=prepared.caller_scope,
            team_explicit=prepared.team_explicit,
            max_turns_explicit=prepared.max_turns_explicit,
            start_step_explicit=prepared.start_step_explicit,
        ),
        workflow_config,
        "live",
        config_dir=repo_root,
        snapshot_config=False,
        runner=final_reviewer,
        resume=resume_context,
        allow_existing_launch_manifest=True,
    )
    assert continuation_result.status == "completed"
    assert len(final_review_invocations) == 1
    continuation_turn = json.loads(
        (
            continuation_result.run_dir / "turns" / "turn-001" / "result.json"
        ).read_text(encoding="utf-8")
    )
    assert continuation_turn["step_name"] == "final_review"
    assert continuation_turn["step_role"] == "architect"
    assert (source.run_dir / "run.json").read_bytes() == source_run_json
    assert (
        source.run_dir / "turns" / "turn-002" / "result.json"
    ).read_bytes() == review_turn_result


def _failed_pending_review_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remaining_checkpoint: bool,
    complete_checkpoint: bool = True,
    complete_step: bool = True,
) -> tuple[Path, Path, Path, WorkflowUserConfig, SimpleNamespace]:
    """Produce one genuine failed-reviewer source through the real controller.

    The worker completes checkpoint 1 and the configured reviewer exits
    nonzero, leaving a failed terminal run with a finalized unsuccessful
    reviewer receipt and the original awaiting-review scope.  The plan stays
    incomplete when a checkpoint remains so the fixture is independent of the
    complete-snapshot routes.  With ``complete_checkpoint`` and
    ``complete_step`` both false the genuine worker leaves both the
    checkpoint header and its step unchecked.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    remaining = "\n### [ ] Checkpoint 2: Next\n- [ ] next step\n" if remaining_checkpoint else ""
    plan_path.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n" + remaining,
        encoding="utf-8",
    )
    config_path = repo_root / "aflow.toml"
    config_path.write_text("# failed pending review fixture\n", encoding="utf-8")
    workflow_config = WorkflowUserConfig(
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
    monkeypatch.setattr(
        "aflow.daemon.load_workflow_config",
        lambda _path: workflow_config,
    )

    def source_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "Implement" in str(kwargs.get("input", "")):
            if complete_checkpoint:
                checkpoint_one = "### [x] Checkpoint 1: First"
            else:
                # The worker performs the step but leaves the original
                # checkpoint header unchecked, so approval is never inferred
                # from a completed checkpoint marker.
                checkpoint_one = "### [ ] Checkpoint 1: First"
            step_one = "- [x] step\n" if complete_step else "- [ ] step\n"
            plan_path.write_text(
                "# Plan\n"
                f"{checkpoint_one}\n"
                + step_one
                + remaining,
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        return subprocess.CompletedProcess(argv, 3, "", "reviewer provider failed\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=plan_path,
                max_turns=40,
                reserved_run_id="failed-pending-review-source",
                idempotency_key="source-key",
                caller_scope="project:one",
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=source_runner,
        )
    except WorkflowError as exc:
        source = SimpleNamespace(status="failed", run_dir=exc.run_dir)
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("producer source did not fail at the reviewer")
    return repo_root, plan_path, config_path, workflow_config, source


def _failed_worker_review_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_checkpoint: bool,
    final_review_stage: bool = False,
) -> tuple[Path, Path, Path, WorkflowUserConfig, SimpleNamespace]:
    """Produce one genuine failed-worker source through the real controller.

    The worker advances checkpoint 1 and then the harness transport fails
    (returncode 124) before any transition is chosen; the source stays failed
    with the checkpoint scope still open (``awaiting_review`` not set).  With
    ``final_checkpoint`` the plan has only checkpoint 1 so the source reports
    ``DONE=True``.  With ``final_review_stage`` the checkpoint review routes
    to a distinct final-review step before END, so the managed acceptance can
    assert the final checkpoint reviewer -> final review -> END order.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    remaining = (
        "\n### [ ] Checkpoint 2: Next\n- [ ] next step\n"
        if not final_checkpoint
        else ""
    )
    plan_path.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n" + remaining,
        encoding="utf-8",
    )
    config_path = repo_root / "aflow.toml"
    config_path.write_text("# failed worker review fixture\n", encoding="utf-8")
    if final_review_stage:
        review_go = (GoTransition(to="final_review"),)
        final_review_step = WorkflowStepConfig(
            role="reviewer",
            prompts=("final_review",),
            go=(
                GoTransition(to="END", when="DONE && !NEW_PLAN_EXISTS"),
                GoTransition(to="implement"),
            ),
        )
        final_review_prompt = "Final review of the completed plan."
    else:
        review_go = (
            GoTransition(to="END", when="DONE && !NEW_PLAN_EXISTS"),
            GoTransition(to="implement", when="NEW_PLAN_EXISTS"),
            GoTransition(to="implement"),
        )
        final_review_step = None
        final_review_prompt = "Final review of the completed plan."
    steps: dict[str, WorkflowStepConfig] = {
        "implement": WorkflowStepConfig(
            role="worker",
            prompts=("implement",),
            go=(GoTransition(to="review"),),
        ),
        "review": WorkflowStepConfig(
            role="reviewer",
            prompts=("review",),
            go=review_go,
        ),
    }
    if final_review_step is not None:
        steps["final_review"] = final_review_step
    workflow_config = WorkflowUserConfig(
        roles={"worker": "codex.high", "reviewer": "codex.high"},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={"high": HarnessProfileConfig(model="high-model")}
            )
        },
        workflows={
            "live": WorkflowConfig(
                steps=steps,
                first_step="implement",
            )
        },
        prompts={
            "implement": "Implement the checkpoint.",
            "review": "Review the completed worker turn.",
            "final_review": final_review_prompt,
        },
    )
    monkeypatch.setattr(
        "aflow.daemon.load_workflow_config",
        lambda _path: workflow_config,
    )

    def source_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "Implement" in str(kwargs.get("input", "")):
            plan_path.write_text(
                "# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n" + remaining,
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                argv, 124, "", "harness transport failure\n"
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=plan_path,
                max_turns=40,
                reserved_run_id="failed-worker-review-source",
                idempotency_key="source-key",
                caller_scope="project:one",
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=source_runner,
        )
    except WorkflowError as exc:
        source = SimpleNamespace(status="failed", run_dir=exc.run_dir)
    else:  # pragma: no cover - the producer must fail at the worker
        raise AssertionError("producer source did not fail at the worker")
    return repo_root, plan_path, config_path, workflow_config, source


@pytest.mark.parametrize("final_checkpoint", [False, True])
def test_daemon_resume_failed_worker_advanced_checklist_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_checkpoint: bool
) -> None:
    """Managed resume of a failed-worker source admits and starts the reviewer.

    The read-only preview and admission agree, the same-key resume creates one
    successor with the exact predecessor identity and an explicit budget, the
    checkpoint reviewer runs before any worker, and approval advances to the
    next worker (or the final review for a final checkpoint) while the source
    records stay unchanged.
    """
    repo_root, plan_path, config_path, workflow_config, source = (
        _failed_worker_review_source(
            tmp_path, monkeypatch, final_checkpoint=final_checkpoint
        )
    )
    source_dir = source.run_dir
    source_run_json = source_dir.joinpath("run.json").read_bytes()
    source_turn_results = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.glob("turns/*/result.json"))
    }
    metadata = json.loads(source_run_json)
    assert metadata["status"] == "failed"
    assert metadata["current_step_name"] == "implement"
    failed_receipt = json.loads(
        (source_dir / "turns" / f"turn-{metadata['active_turn']:03d}" / "result.json").read_text()
    )
    assert failed_receipt["status"] == "harness-failed"
    assert failed_receipt["step_role"] == "worker"
    assert failed_receipt["step_name"] == "implement"

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    # Read-only preview and admission agree.
    preview = daemon.service.run_status("failed-worker-review-source")
    assert preview.evidence["can_resume"] is True

    # Same-key single successor with the exact predecessor identity.
    continuation = daemon.service.resume(
        "failed-worker-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-worker-review",
    )
    replay = daemon.service.resume(
        "failed-worker-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-worker-review",
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record.get("resumed_from_run_id") == source_dir.name
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record, manifest, repo_root, config_path, workflow_config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert prepared.reserved_run_id == continuation.run_id
    assert prepared.idempotency_key == "resume-failed-worker-review"
    assert prepared.caller_scope == "project:one"
    assert resume_context is not None
    assert resume_context.failed_worker_review is not None
    assert resume_context.failed_worker_review.reviewer_step_name == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    # Checkpoint reviewer first; approval then next worker (or final review).
    provider_calls: list[str] = []

    def successor_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs.get("input", ""))
        if "Implement" in prompt:
            provider_calls.append("worker")
            if not final_checkpoint:
                plan_path.write_text(
                    "# Plan\n\n### [x] Checkpoint 1: First\n- [x] step\n"
                    "### [x] Checkpoint 2: Next\n- [x] next step\n",
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(
                argv, 1, "", "worker stopped for fixture\n"
            )
        provider_calls.append("reviewer")
        return subprocess.CompletedProcess(argv, 0, "review approved\n", "")

    try:
        continuation_result = run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=prepared.plan_path,
                max_turns=prepared.max_turns,
                team=prepared.team,
                extra_instructions=prepared.extra_instructions,
                start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=successor_runner,
            resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        continuation_run_dir = exc.run_dir
    else:
        continuation_run_dir = continuation_result.run_dir

    # The full provider order: the checkpoint reviewer first, and only after
    # approval the next worker (none for a final checkpoint that ends there).
    assert provider_calls == (
        ["reviewer"] if final_checkpoint else ["reviewer", "worker"]
    )
    continuation_turn = json.loads(
        (continuation_run_dir / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8")
    )
    assert continuation_turn["step_name"] == "review"
    assert continuation_turn["step_role"] == "reviewer"
    # The source records are unchanged.
    assert (source_dir / "run.json").read_bytes() == source_run_json
    for relative_path, artifact_bytes in source_turn_results.items():
        assert (source_dir / relative_path).read_bytes() == artifact_bytes


def test_daemon_resume_failed_worker_advanced_checklist_final_review_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final-checkpoint managed resume runs final review before END.

    The checkpoint review routes to a distinct final-review step: the managed
    resume admits the same single successor, the checkpoint reviewer runs
    first, the distinct final-review step runs second, and only then does the
    workflow reach END/delivery with the source records unchanged.
    """
    repo_root, plan_path, config_path, workflow_config, source = (
        _failed_worker_review_source(
            tmp_path,
            monkeypatch,
            final_checkpoint=True,
            final_review_stage=True,
        )
    )
    source_dir = source.run_dir
    source_run_json = source_dir.joinpath("run.json").read_bytes()
    source_turn_results = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.glob("turns/*/result.json"))
    }

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status("failed-worker-review-source")
    assert preview.evidence["can_resume"] is True
    continuation = daemon.service.resume(
        "failed-worker-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-worker-final-review",
    )
    replay = daemon.service.resume(
        "failed-worker-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-worker-final-review",
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record.get("resumed_from_run_id") == source_dir.name
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record, manifest, repo_root, config_path, workflow_config
    )
    assert prepared.start_step == "review"
    assert prepared.reserved_run_id == continuation.run_id
    assert prepared.idempotency_key == "resume-failed-worker-final-review"
    assert prepared.caller_scope == "project:one"
    assert resume_context is not None
    assert resume_context.failed_worker_review is not None
    assert resume_context.failed_worker_review.reviewer_step_name == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    provider_calls: list[str] = []

    def successor_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs.get("input", ""))
        if "Implement" in prompt:
            provider_calls.append("worker")
            return subprocess.CompletedProcess(
                argv, 1, "", "worker stopped for fixture\n"
            )
        if "Final review" in prompt:
            provider_calls.append("final_reviewer")
        else:
            provider_calls.append("reviewer")
        return subprocess.CompletedProcess(argv, 0, "review approved\n", "")

    continuation_result = run_workflow(
        ControllerConfig(
            repo_root=repo_root,
            plan_path=prepared.plan_path,
            max_turns=prepared.max_turns,
            team=prepared.team,
            extra_instructions=prepared.extra_instructions,
            start_step=prepared.start_step,
            reserved_run_id=prepared.reserved_run_id,
            idempotency_key=prepared.idempotency_key,
            caller_scope=prepared.caller_scope,
            team_explicit=prepared.team_explicit,
            max_turns_explicit=prepared.max_turns_explicit,
            start_step_explicit=prepared.start_step_explicit,
        ),
        workflow_config,
        "live",
        config_dir=repo_root,
        snapshot_config=False,
        runner=successor_runner,
        resume=resume_context,
        allow_existing_launch_manifest=True,
    )
    # Checkpoint reviewer first, then the distinct final-review step, and only
    # then END/delivery; no worker runs in between.
    assert provider_calls == ["reviewer", "final_reviewer"]
    continuation_run_dir = continuation_result.run_dir
    t1 = json.loads(
        (continuation_run_dir / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8")
    )
    assert t1["step_name"] == "review"
    assert t1["step_role"] == "reviewer"
    t2 = json.loads(
        (continuation_run_dir / "turns" / "turn-002" / "result.json").read_text(encoding="utf-8")
    )
    assert t2["step_name"] == "final_review"
    assert t2["step_role"] == "reviewer"
    # The source records are unchanged.
    assert (source_dir / "run.json").read_bytes() == source_run_json
    for relative_path, artifact_bytes in source_turn_results.items():
        assert (source_dir / relative_path).read_bytes() == artifact_bytes


def test_daemon_resume_failed_worker_advanced_checklist_rejection_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed rejection selects the same scope and starts a repair worker.

    The checkpoint reviewer rejects the retained scope (recording a rejection
    that references the recovered attempt), the repair worker starts on the
    same scope, and the source receipts stay byte-for-byte unchanged.
    """
    repo_root, plan_path, config_path, workflow_config, source = (
        _failed_worker_review_source(tmp_path, monkeypatch, final_checkpoint=False)
    )
    source_dir = source.run_dir
    source_run_json = source_dir.joinpath("run.json").read_bytes()

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status("failed-worker-review-source")
    assert preview.evidence["can_resume"] is True
    continuation = daemon.service.resume(
        "failed-worker-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-worker-review-reject",
    )
    assert continuation.created is True
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    prepared, resume_context = _worker_prepared(
        record, manifest, repo_root, config_path, workflow_config
    )
    assert prepared.start_step == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True
    scope_id = resume_context.active_implementation_scope.scope_id

    provider_calls: list[str] = []

    def reject_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs.get("input", ""))
        if "Implement" in prompt:
            provider_calls.append("worker")
            return subprocess.CompletedProcess(argv, 1, "", "worker stopped for fixture\n")
        provider_calls.append("reviewer")
        overlay = plan_path.with_name("plan-cp01-v01.md")
        overlay.write_text(
            "# Follow-up\n\n### [ ] Checkpoint 1: Repair\n- [ ] repair\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "review rejected\n", "")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=prepared.plan_path,
                max_turns=prepared.max_turns,
                team=prepared.team,
                extra_instructions=prepared.extra_instructions,
                start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=reject_runner,
            resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        continuation_run_dir = exc.run_dir
    else:  # pragma: no cover - the repair worker stops for the fixture
        raise AssertionError("repair worker did not stop")

    # Reviewer first, then the repair worker on the same scope.
    assert provider_calls[0] == "reviewer"
    assert provider_calls[1] == "worker"
    review_turn = json.loads(
        (continuation_run_dir / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8")
    )
    assert review_turn["step_name"] == "review"
    assert review_turn["step_role"] == "reviewer"
    rejection = review_turn.get("review_rejection")
    assert rejection is not None
    assert rejection.get("scope_id") == scope_id
    assert rejection.get("reviewed_attempt_ordinal") is not None
    # The source receipts remain byte-for-byte unchanged.
    assert (source_dir / "run.json").read_bytes() == source_run_json


@pytest.mark.parametrize("remaining_checkpoint", [False, True])
def test_daemon_resume_failed_pending_review_resumes_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remaining_checkpoint: bool
) -> None:
    """A proven finalized reviewer failure resumes at the pending review."""
    repo_root, _plan_path, config_path, workflow_config, source = (
        _failed_pending_review_source(
            tmp_path, monkeypatch, remaining_checkpoint=remaining_checkpoint
        )
    )
    source_dir = source.run_dir
    source_run_json = source_dir.joinpath("run.json").read_bytes()
    source_turn_results = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.glob("turns/*/result.json"))
    }
    metadata = json.loads(source_run_json)
    assert metadata["status"] == "failed"
    assert metadata["current_step_name"] == "review"
    assert metadata["active_turn"] == metadata["turns_completed"] + 1
    assert metadata["active_implementation_scope"]["awaiting_review"] is True
    failed_receipt = json.loads(
        (source_dir / "turns" / f"turn-{metadata['active_turn']:03d}" / "result.json").read_text()
    )
    assert failed_receipt["status"] == "harness-failed"
    assert failed_receipt["step_role"] == "reviewer"
    assert failed_receipt["step_name"] == "review"

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status("failed-pending-review-source")
    assert preview.evidence["can_resume"] is True

    continuation = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review",
    )
    replay = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review",
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record, manifest, repo_root, config_path, workflow_config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.interrupted_step_name == "review"
    assert resume_context.failed_pending_review_step == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    provider_calls: list[str] = []

    def successor_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "Implement" in str(kwargs.get("input", "")):
            provider_calls.append("worker")
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        provider_calls.append("reviewer")
        return subprocess.CompletedProcess(argv, 0, "review approved\n", "")

    try:
        continuation_result = run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=prepared.plan_path,
                max_turns=prepared.max_turns,
                team=prepared.team,
                extra_instructions=prepared.extra_instructions,
                start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=successor_runner,
            resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        assert remaining_checkpoint
        assert "workflow selected END while the active plan remains incomplete" in str(exc)
        continuation_run_dir = exc.run_dir
    else:
        assert not remaining_checkpoint
        assert continuation_result.status == "completed"
        continuation_run_dir = continuation_result.run_dir
    assert provider_calls == ["reviewer"]
    continuation_turn = json.loads(
        (continuation_run_dir / "turns" / "turn-001" / "result.json").read_text(encoding="utf-8")
    )
    assert continuation_turn["step_name"] == "review"
    assert continuation_turn["step_role"] == "reviewer"
    continuation_metadata = json.loads(
        (continuation_run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert continuation_metadata["effective_max_turns"] == 40
    assert (source_dir / "run.json").read_bytes() == source_run_json
    for relative_path, artifact_bytes in source_turn_results.items():
        assert (source_dir / relative_path).read_bytes() == artifact_bytes


def test_daemon_resume_failed_pending_review_unchecked_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine failed reviewer with the original checkpoint still unchecked resumes at review.

    The worker completed the step but left the checkpoint header unchecked.
    Preview, prepared manifest, reconstructed context and the first runtime
    provider all agree on the pending review, with budget40, same-key replay
    and byte-identical source evidence retained.
    """
    repo_root, _plan_path, config_path, workflow_config, source = (
        _failed_pending_review_source(
            tmp_path,
            monkeypatch,
            remaining_checkpoint=True,
            complete_checkpoint=False,
        )
    )
    source_dir = source.run_dir
    source_run_json = source_dir.joinpath("run.json").read_bytes()
    source_turn_results = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.glob("turns/*/result.json"))
    }
    metadata = json.loads(source_run_json)
    snapshot = metadata["last_snapshot"]
    assert snapshot["current_checkpoint_index"] == 1
    assert snapshot["is_complete"] is False
    assert snapshot["current_checkpoint_unchecked_step_count"] == 0
    assert metadata["active_implementation_scope"]["awaiting_review"] is True

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status("failed-pending-review-source")
    assert preview.evidence["can_resume"] is True

    continuation = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review-unchecked",
    )
    replay = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review-unchecked",
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record, manifest, repo_root, config_path, workflow_config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.interrupted_step_name == "review"
    assert resume_context.failed_pending_review_step == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    provider_calls: list[str] = []

    def successor_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        is_worker = "Implement" in str(kwargs.get("input", ""))
        provider_calls.append("worker" if is_worker else "reviewer")
        if is_worker:
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        return subprocess.CompletedProcess(argv, 0, "review approved\n", "")

    try:
        continuation_result = run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=prepared.plan_path,
                max_turns=prepared.max_turns,
                team=prepared.team,
                extra_instructions=prepared.extra_instructions,
                start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=successor_runner,
            resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        # The default review step routes to END; with checkpoint 1 still
        # unchecked the plan is incomplete, so the controller fails closed.
        assert (
            "workflow selected END while the active plan remains incomplete"
            in str(exc)
        )
        continuation_run_dir = exc.run_dir
    else:
        assert continuation_result.status == "completed"
        continuation_run_dir = continuation_result.run_dir
    # The first provider is the reviewer, with no preceding worker.
    assert provider_calls == ["reviewer"]
    continuation_metadata = json.loads(
        (continuation_run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert continuation_metadata["effective_max_turns"] == 40
    assert (source_dir / "run.json").read_bytes() == source_run_json
    for relative_path, artifact_bytes in source_turn_results.items():
        assert (source_dir / relative_path).read_bytes() == artifact_bytes


@pytest.mark.parametrize(
    "mutation", ["missing_saved_snapshot", "malformed_saved_snapshot", "foreign_saved_checkpoint_index"]
)
def test_daemon_resume_failed_pending_review_saved_snapshot_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """An invalid saved snapshot of a recognized candidate refuses cleanly.

    Once the failed-reviewer candidate is recognized, a missing, malformed,
    or unsupported noncomplete saved ``last_snapshot`` is a clean managed
    refusal: the read-only preview cannot resume, the resume call raises
    before any successor run, launch manifest, unit start, start request or
    provider dispatch, and the source is preserved byte-identical except the
    append-only audit.  There is no implementation fallback after a
    candidate-shape validation failure.
    """
    repo_root, _plan_path, config_path, workflow_config, source = (
        _failed_pending_review_source(
            tmp_path,
            monkeypatch,
            remaining_checkpoint=True,
            complete_checkpoint=False,
        )
    )
    source_dir = source.run_dir
    source_json = source_dir / "run.json"
    metadata = json.loads(source_json.read_text(encoding="utf-8"))
    assert metadata["last_snapshot"]["current_checkpoint_index"] == 1
    assert metadata["last_snapshot"]["is_complete"] is False
    if mutation == "missing_saved_snapshot":
        metadata.pop("last_snapshot")
    elif mutation == "malformed_saved_snapshot":
        metadata["last_snapshot"] = {"bogus": True}
    elif mutation == "foreign_saved_checkpoint_index":
        metadata["last_snapshot"]["current_checkpoint_index"] = 0
    else:
        raise AssertionError(mutation)
    source_json.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    source_artifacts = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
    }

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    # The read-only admission preview already fails closed.
    preview = daemon.service.run_status("failed-pending-review-source")
    assert preview.evidence["can_resume"] is False
    with pytest.raises(ValueError, match="pending-review evidence"):
        daemon.service.resume(
            "failed-pending-review-source",
            caller_scope="project:one",
            idempotency_key="resume-failed-pending-review-saved-snapshot",
        )
    # No successor unit, start request, run, or provider was allocated.
    assert units.start_calls == []
    runs_root = repo_root / ".aflow" / "runs"
    assert sorted(p.name for p in runs_root.iterdir()) == [
        "failed-pending-review-source"
    ]
    for relative_path, artifact_bytes in source_artifacts.items():
        after = (source_dir / relative_path).read_bytes()
        if relative_path == "events.jsonl":
            assert after.startswith(artifact_bytes)
        else:
            assert after == artifact_bytes


def test_daemon_resume_failed_pending_review_unchecked_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine failed reviewer with the checkpoint and its step unchecked resumes at review.

    The successful worker leaves both the original checkpoint header and its
    step unchecked, then the configured reviewer exits nonzero.  The saved
    snapshot carries a positive unchecked-step count, and the pending review
    is recovered: preview, launch manifest, prepared worker, reconstructed
    context and the actual first provider all select review, with budget40,
    one same-key successor, no preceding worker and byte-identical source
    evidence.  Approval is never inferred from step completion alone.
    """
    repo_root, _plan_path, config_path, workflow_config, source = (
        _failed_pending_review_source(
            tmp_path,
            monkeypatch,
            remaining_checkpoint=True,
            complete_checkpoint=False,
            complete_step=False,
        )
    )
    source_dir = source.run_dir
    source_run_json = source_dir.joinpath("run.json").read_bytes()
    source_turn_results = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.glob("turns/*/result.json"))
    }
    metadata = json.loads(source_run_json)
    snapshot = metadata["last_snapshot"]
    assert snapshot["current_checkpoint_index"] == 1
    assert snapshot["is_complete"] is False
    assert snapshot["current_checkpoint_unchecked_step_count"] > 0
    assert metadata["active_implementation_scope"]["awaiting_review"] is True

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status("failed-pending-review-source")
    assert preview.evidence["can_resume"] is True

    continuation = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review-unchecked-step",
    )
    replay = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review-unchecked-step",
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record, manifest, repo_root, config_path, workflow_config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.interrupted_step_name == "review"
    assert resume_context.failed_pending_review_step == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    provider_calls: list[str] = []

    def successor_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        is_worker = "Implement" in str(kwargs.get("input", ""))
        provider_calls.append("worker" if is_worker else "reviewer")
        if is_worker:
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        return subprocess.CompletedProcess(argv, 0, "review approved\n", "")

    try:
        continuation_result = run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=prepared.plan_path,
                max_turns=prepared.max_turns,
                team=prepared.team,
                extra_instructions=prepared.extra_instructions,
                start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            workflow_config,
            "live",
            config_dir=repo_root,
            snapshot_config=False,
            runner=successor_runner,
            resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        # The default review step routes to END; with the checkpoint and
        # its step still unchecked the plan is incomplete, so the
        # controller fails closed.
        assert (
            "workflow selected END while the active plan remains incomplete"
            in str(exc)
        )
        continuation_run_dir = exc.run_dir
    else:
        assert continuation_result.status == "completed"
        continuation_run_dir = continuation_result.run_dir
    # The first provider is the reviewer, with no preceding worker.
    assert provider_calls == ["reviewer"]
    continuation_metadata = json.loads(
        (continuation_run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert continuation_metadata["effective_max_turns"] == 40
    assert (source_dir / "run.json").read_bytes() == source_run_json
    for relative_path, artifact_bytes in source_turn_results.items():
        assert (source_dir / relative_path).read_bytes() == artifact_bytes


@pytest.mark.parametrize(
    "field", ["original_plan_path", "active_plan_path"]
)
@pytest.mark.parametrize("mutation", ["missing", "blank", "nonstring", "foreign"])
def test_daemon_resume_failed_pending_review_identity_negatives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mutation: str,
) -> None:
    """Each receipt identity refuses independently while the checkpoint is unchecked.

    With the original checkpoint still unchecked, a missing, blank,
    non-string, or foreign receipt plan identity (tested independently for
    the original and active identities) produces a clean managed refusal:
    the preview cannot resume, the resume call raises, no successor unit is
    started, no successor run is allocated, no provider is dispatched, and
    the source is preserved byte-identical.  There is no implementation
    fallback after a candidate-shape validation failure.
    """
    repo_root, _plan_path, config_path, workflow_config, source = (
        _failed_pending_review_source(
            tmp_path,
            monkeypatch,
            remaining_checkpoint=True,
            complete_checkpoint=False,
        )
    )
    source_dir = source.run_dir
    metadata = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    assert metadata["last_snapshot"]["current_checkpoint_index"] == 1
    assert metadata["last_snapshot"]["is_complete"] is False
    receipt_path = source_dir / "turns" / "turn-002" / "result.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        receipt.pop(field)
    elif mutation == "blank":
        receipt[field] = ""
    elif mutation == "nonstring":
        receipt[field] = {"invalid": True}
    else:
        receipt[field] = str(repo_root / "other-plan.md")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    source_artifacts = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
    }

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    # The read-only admission preview already fails closed.
    preview = daemon.service.run_status("failed-pending-review-source")
    assert preview.evidence["can_resume"] is False
    with pytest.raises(ValueError, match="pending-review evidence"):
        daemon.service.resume(
            "failed-pending-review-source",
            caller_scope="project:one",
            idempotency_key="resume-failed-pending-review-identity",
        )
    # No successor unit was started and no provider was allocated.
    assert units.start_calls == []
    # No successor run directory was allocated.
    runs_root = repo_root / ".aflow" / "runs"
    assert sorted(p.name for p in runs_root.iterdir()) == [
        "failed-pending-review-source"
    ]
    # The source is preserved by the refusal; events.jsonl is the
    # documented append-only audit and may only grow.
    for relative_path, artifact_bytes in source_artifacts.items():
        after = (source_dir / relative_path).read_bytes()
        if relative_path == "events.jsonl":
            assert after.startswith(artifact_bytes)
        else:
            assert after == artifact_bytes


def test_daemon_resume_failed_pending_review_conflicting_checkpoint_name_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-index snapshot renamed away from the immutable scope refuses.

    Changing both the saved snapshot and the receipt post-snapshot
    checkpoint name to a foreign name, while the immutable awaiting scope
    retains the original name and the same index, is contradictory
    identity evidence: a clean managed refusal before any successor unit is
    started, a successor is allocated, or a provider is dispatched.
    """
    repo_root, _plan_path, config_path, workflow_config, source = (
        _failed_pending_review_source(
            tmp_path,
            monkeypatch,
            remaining_checkpoint=True,
            complete_checkpoint=False,
        )
    )
    source_dir = source.run_dir
    run_json_path = source_dir / "run.json"
    payload = json.loads(run_json_path.read_text(encoding="utf-8"))
    scope = payload["active_implementation_scope"]
    assert scope["checkpoint_index"] == 1
    assert scope["checkpoint_name"] == "Checkpoint 1: First"
    payload["last_snapshot"]["current_checkpoint_name"] = "Checkpoint 1: Foreign"
    run_json_path.write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )
    receipt_path = source_dir / "turns" / "turn-002" / "result.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["snapshot_after"]["current_checkpoint_name"] = "Checkpoint 1: Foreign"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    source_artifacts = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
    }

    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status("failed-pending-review-source")
    assert preview.evidence["can_resume"] is False
    with pytest.raises(ValueError, match="immutable checkpoint name"):
        daemon.service.resume(
            "failed-pending-review-source",
            caller_scope="project:one",
            idempotency_key="resume-failed-pending-review-conflicting-name",
        )
    assert units.start_calls == []
    runs_root = repo_root / ".aflow" / "runs"
    assert sorted(p.name for p in runs_root.iterdir()) == [
        "failed-pending-review-source"
    ]
    for relative_path, artifact_bytes in source_artifacts.items():
        after = (source_dir / relative_path).read_bytes()
        if relative_path == "events.jsonl":
            assert after.startswith(artifact_bytes)
        else:
            assert after == artifact_bytes


def _progression_source(
    root: Path,
) -> tuple[Path, Path, Path, WorkflowUserConfig, Path]:
    """Produce a failed-reviewer source with progression review transitions.

    The reviewer step uses ordinary transitions prioritizing ``NEW_PLAN_EXISTS``
    (scoped repair), then ``DONE``, then ordinary worker progression, and the
    prompts expose ``ACTIVE_PLAN_PATH``/``NEW_PLAN_PATH``.  Checkpoint 1 is
    complete and checkpoint 2 remains, so an approved review progresses to the
    worker and a scoped rejection routes to a repair worker.
    """
    root.mkdir()
    plan_path = root / "plan.md"
    plan_path.write_text(
        "# Plan\n\n"
        "### [ ] Checkpoint 1: First\n- [ ] step\n"
        "### [ ] Checkpoint 2: Next\n- [ ] next step\n",
        encoding="utf-8",
    )
    config_path = root / "aflow.toml"
    config_path.write_text("# failed pending review fixture\n", encoding="utf-8")
    workflow_config = WorkflowUserConfig(
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
                        go=(
                            GoTransition(to="implement", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement"),
                        ),
                    ),
                },
                first_step="implement",
            )
        },
        prompts={
            "implement": "Implement {ACTIVE_PLAN_PATH}.",
            "review": "Review; repair at {NEW_PLAN_PATH}.",
        },
    )

    def source_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "Implement" in str(kwargs.get("input", "")):
            plan_path.write_text(
                "# Plan\n"
                "### [x] Checkpoint 1: First\n"
                "- [x] step\n"
                "### [ ] Checkpoint 2: Next\n- [ ] next step\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        return subprocess.CompletedProcess(argv, 3, "", "reviewer provider failed\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root,
                plan_path=plan_path,
                max_turns=40,
                reserved_run_id="failed-pending-review-source",
                idempotency_key="source-key",
                caller_scope="project:one",
            ),
            workflow_config,
            "live",
            config_dir=root,
            snapshot_config=False,
            runner=source_runner,
        )
    except WorkflowError as exc:
        source_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("producer source did not fail at the reviewer")
    return root, plan_path, config_path, workflow_config, source_dir


def _inherited_repair_source(
    root: Path,
) -> tuple[Path, Path, Path, WorkflowUserConfig, Path]:
    """Produce a failed-reviewer source that already owns a completed repair.

    The source completes its original worker (turn 1), receives a scoped
    rejection (turn 2), completes that repair (turn 3, scope ordinal 2), and
    then fails at review (turn 4).  Its latest worker is therefore an
    inherited repair whose local turn (3) is unique in the scope's attempt
    sequence, so a genuinely ordinal-absent rejection can use that reviewed
    turn number as its diagnostic predecessor.  No turn counters, result
    statuses, or interrupted finalizations are fabricated; the reviewer
    failures and the scoped rejection are produced by the real controller.
    """
    root.mkdir()
    plan_path = root / "plan.md"
    plan_path.write_text(
        "# Plan\n\n"
        "### [ ] Checkpoint 1: First\n- [ ] step\n"
        "### [ ] Checkpoint 2: Next\n- [ ] next step\n",
        encoding="utf-8",
    )
    config_path = root / "aflow.toml"
    config_path.write_text("# inherited repair fixture\n", encoding="utf-8")
    workflow_config = WorkflowUserConfig(
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
                        go=(
                            GoTransition(to="implement", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement"),
                        ),
                    ),
                },
                first_step="implement",
            )
        },
        prompts={
            "implement": "Implement {ACTIVE_PLAN_PATH}.",
            "review": "Review; repair at {NEW_PLAN_PATH}.",
        },
    )

    worker_calls = 0
    reviewer_calls = 0

    def source_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal worker_calls, reviewer_calls
        prompt = str(kwargs.get("input", ""))
        if "Implement" in prompt:
            worker_calls += 1
            if worker_calls == 1:
                plan_path.write_text(
                    "# Plan\n"
                    "### [x] Checkpoint 1: First\n"
                    "- [x] step\n"
                    "### [ ] Checkpoint 2: Next\n- [ ] next step\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair output\n", "")
        reviewer_calls += 1
        if reviewer_calls == 1:
            overlay = Path(
                prompt.split("repair at ", 1)[1].split()[0].rstrip(".")
            )
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                argv, 0, "scoped rejection\n", ""
            )
        return subprocess.CompletedProcess(
            argv, 3, "", "reviewer provider failed\n"
        )

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root,
                plan_path=plan_path,
                max_turns=40,
                reserved_run_id="inherited-repair-source",
                idempotency_key="source-key",
                caller_scope="project:one",
            ),
            workflow_config,
            "live",
            config_dir=root,
            snapshot_config=False,
            runner=source_runner,
        )
    except WorkflowError as exc:
        source_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("producer source did not fail at the reviewer")
    return root, plan_path, config_path, workflow_config, source_dir


@pytest.mark.parametrize("reject_first", [False, True])
def test_daemon_resume_failed_pending_review_keep_runs_and_progression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject_first: bool
) -> None:
    """Issue #79: daemon admission plus genuine in-process review progression.

    The daemon half verifies the failed-pending-review admission: the preview,
    launch manifest, prepared worker and durable resume context all select the
    review step with budget40, and a same-key replay starts exactly one
    successor unit.  The runtime half runs the real controller in-process over
    a separate source whose review step uses ordinary transitions prioritizing
    ``NEW_PLAN_EXISTS`` (scoped repair), then ``DONE``, then ordinary worker
    progression, with prompts exposing ``ACTIVE_PLAN_PATH``/``NEW_PLAN_PATH``.

    - approve-first: ``reviewer, worker, reviewer`` with zero rejections.
    - reject-first: ``reviewer, worker, reviewer, worker, reviewer`` with one
      scoped rejection, then ordinary progression of the remaining checkpoint.

    Both flows must reach a successful controller completion (an unexpected
    reviewer/controller failure fails the test) while keeping the source
    run/turn/scope artifacts byte-identical under ``keep_runs=1``.
    """
    # --- Daemon admission: preview, manifest, context, budget40, one successor.
    daemon_root, _daemon_plan, daemon_config_path, daemon_config, _daemon_source = (
        _failed_pending_review_source(
            tmp_path, monkeypatch, remaining_checkpoint=True
        )
    )
    environment_file = daemon_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = daemon_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=daemon_root,
            config_path=daemon_config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    preview = daemon.service.run_status("failed-pending-review-source")
    assert preview.evidence["can_resume"] is True
    continuation = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review-keep-runs",
    )
    replay = daemon.service.resume(
        "failed-pending-review-source",
        caller_scope="project:one",
        idempotency_key="resume-failed-pending-review-keep-runs",
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record, manifest, daemon_root, daemon_config_path, daemon_config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.failed_pending_review_step == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    # --- Runtime progression: in-process controller over a separate source.
    import aflow.cli as cli_module

    prog_root, plan_path, prog_config_path, workflow_config, source_dir = (
        _progression_source(tmp_path / "prog")
    )
    bootstrap = cli_module._bootstrap_resume_invocation(
        repo_root=prog_root,
        config_path=str(prog_config_path),
        config_path_is_explicit=True,
        workflow_config=workflow_config,
        requested_run_id="failed-pending-review-source",
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
    )
    assert bootstrap.start_step == "review"
    assert bootstrap.resume_context.failed_pending_review_step == "review"

    # Capture the immutable source run/turn/scope artifact set before resume.
    # The append-only managed audit journal (events.jsonl) and issues.md are
    # excluded: they legitimately grow through documented audit effects.
    artifact_paths = [source_dir / "run.json"]
    for sub in ("turns", "scopes", "evidence"):
        base = source_dir / sub
        if base.is_dir():
            artifact_paths.extend(p for p in base.rglob("*") if p.is_file())
    source_artifacts = {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in artifact_paths
    }

    provider_calls: list[str] = []

    def successor_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        is_worker = "Implement " in prompt
        provider_calls.append("worker" if is_worker else "reviewer")
        if len(provider_calls) == 1:
            # Inside the first resumed reviewer: the source artifacts are
            # intact, the selected worker receipt is readable, and the prompt
            # does not report a missing worker artifact.
            assert all(
                (source_dir / rel).read_bytes() == data
                for rel, data in source_artifacts.items()
            )
            assert (source_dir / "turns" / "turn-001" / "result.json").is_file()
            assert "Worker artifact unavailable" not in prompt
        if reject_first and len(provider_calls) == 1:
            # Genuine scoped rejection: write the supplied NEW_PLAN_PATH overlay
            # with unchecked repair work; the original snapshot stays unchanged.
            # The prompt carries the path as its first token; checkpoint review
            # context follows on later lines.
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "reject: repair needed\n", "")
        if is_worker:
            # The worker operates on the ACTIVE_PLAN_PATH (original plan for
            # ordinary progression, the repair overlay for a scoped repair).
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        # Approve: leave the active plan unchanged.
        return subprocess.CompletedProcess(argv, 0, "approved\n", "")

    continuation_result = run_workflow(
        ControllerConfig(
            repo_root=prog_root,
            plan_path=plan_path,
            max_turns=40,
            start_step=bootstrap.start_step,
            reserved_run_id="progression-successor",
            keep_runs=1,
        ),
        workflow_config,
        "live",
        config_dir=prog_root,
        snapshot_config=False,
        runner=successor_runner,
        resume=bootstrap.resume_context,
    )
    continuation_run_dir = continuation_result.run_dir

    # The first provider is the reviewer (no preceding worker), and the
    # expected sequence holds for each parameterized flow. A controller
    # failure would raise WorkflowError and fail the test (no swallow).
    assert provider_calls[0] == "reviewer"
    assert continuation_result.status == "completed"
    if reject_first:
        assert provider_calls == [
            "reviewer", "worker", "reviewer", "worker", "reviewer",
        ]
    else:
        assert provider_calls == ["reviewer", "worker", "reviewer"]

    continuation_metadata = json.loads(
        (continuation_run_dir / "run.json").read_text(encoding="utf-8")
    )
    # Budget40 is retained.
    assert continuation_metadata["effective_max_turns"] == 40

    # Rejection-history identity: a genuine scoped rejection records exactly
    # one entry bound to checkpoint 1 and its repair overlay; approval records
    # none.
    rejections = continuation_metadata["review_rejection_history"]
    if reject_first:
        assert len(rejections) == 1
        rejection = rejections[0]
        assert rejection["checkpoint_index"] == 1
        assert rejection["checkpoint_name"] == "Checkpoint 1: First"
        assert rejection["repair_plan_path"] is not None
    else:
        assert rejections == []

    # Source artifacts remain byte-identical under keep_runs=1 after the full
    # progression, and the worker receipt referenced by the review prompt is
    # still readable.
    assert all(
        (source_dir / rel).read_bytes() == data
        for rel, data in source_artifacts.items()
    )
    assert (source_dir / "turns" / "turn-001" / "result.json").is_file()


def _bootstrap_cli(
    root: Path, config_path: Path, config: WorkflowUserConfig, source_id: str
):
    import aflow.cli as cli_module

    return cli_module._bootstrap_resume_invocation(
        repo_root=root,
        config_path=str(config_path),
        config_path_is_explicit=True,
        workflow_config=config,
        requested_run_id=source_id,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
    )


def _artifact_set(run_dir: Path) -> dict[str, bytes]:
    """Capture a run's immutable artifact set (run.json plus turn/scope/evidence).

    The append-only managed audit journal (``events.jsonl``) is included so
    callers can explicitly exempt it; it legitimately grows through audit.
    """
    paths = [run_dir / "run.json"]
    for sub in ("turns", "scopes", "evidence"):
        base = run_dir / sub
        if base.is_dir():
            paths.extend(p for p in base.rglob("*") if p.is_file())
    return {p.relative_to(run_dir).as_posix(): p.read_bytes() for p in paths}


def _daemon_for(
    root: Path, config_path: Path, config: WorkflowUserConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AflowDaemon, InMemoryUnitManager]:
    """Start a managed daemon bound to the fixture workflow config."""
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _path: config)
    environment_file = root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()
    return daemon, units


@pytest.mark.parametrize("keep_runs", [20, 1])
def test_daemon_resume_failed_pending_review_local_repair_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_runs: int
) -> None:
    """Issue #79 v06: a local repair worker in the source run wins source-first.

    A produces the checkpoint-1 worker (turn 1) and a failed reviewer.  Resuming
    A into B review-first, B's reviewer rejects with a real non-checkpoint
    repair overlay, B's repair worker completes at local turn 2 (scope-local
    ordinal 2), and B's next reviewer fails at turn 3.  C is admitted through
    the managed preview/resume/manifest/worker-preparation path (same-key
    idempotency, budget40) and runs in-process: review first with zero
    preceding worker calls, selecting B's exact finalized repair-worker receipt.
    After C approves, ordinary remaining-checkpoint progression completes and
    B's source bytes are preserved under both retention settings.
    """
    def local_repair_b_runner(
        argv: list[str], calls: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        calls.append("worker" if "Implement " in prompt else "reviewer")
        if len(calls) == 1:
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "reject: repair needed\n", "")
        if calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair ready\n", "")
        return subprocess.CompletedProcess(argv, 3, "", "reviewer failed after repair\n")

    # --- Managed half: A -> B (reject -> repair worker turn 2 -> reviewer fails),
    # then the managed preview/resume/manifest/worker-preparation path from B.
    m_root, m_plan, m_config_path, m_config, m_a = _progression_source(tmp_path / "managed")
    m_boot = _bootstrap_cli(m_root, m_config_path, m_config, m_a.name)
    assert m_boot.start_step == "review"
    m_calls: list[str] = []
    try:
        run_workflow(
            ControllerConfig(
                repo_root=m_root, plan_path=m_plan, max_turns=40,
                start_step=m_boot.start_step, reserved_run_id="repair-b",
                caller_scope="project:one", keep_runs=keep_runs,
            ),
            m_config, "live", config_dir=m_root, snapshot_config=False,
            runner=lambda argv, **kw: local_repair_b_runner(argv, m_calls, **kw),
            resume=m_boot.resume_context,
        )
    except WorkflowError as exc:
        m_b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B reviewer did not fail")
    assert m_calls == ["reviewer", "worker", "reviewer"]
    m_meta = json.loads((m_b_dir / "run.json").read_text(encoding="utf-8"))
    m_scope = m_meta["active_implementation_scope"]
    m_workers = [
        a for a in m_meta["implementation_attempts"][m_scope["scope_id"]]
        if a["role"] == "worker"
    ]
    assert [a["turn_number"] for a in m_workers] == [1, 2]
    assert [a["attempt_ordinal"] for a in m_workers] == [1, 2]
    m_worker_path = m_b_dir / "turns" / "turn-002" / "result.json"
    m_receipt = json.loads(m_worker_path.read_text(encoding="utf-8"))
    assert m_receipt["step_role"] == "worker"
    assert m_receipt["turn_number"] == 2
    assert m_receipt["status"] == "completed"
    assert m_receipt["original_plan_path"] == str(m_plan)

    daemon, units = _daemon_for(m_root, m_config_path, m_config, monkeypatch)
    preview = daemon.service.run_status("repair-b")
    assert preview.evidence["can_resume"] is True
    continuation = daemon.service.resume(
        "repair-b", caller_scope="project:one", idempotency_key="c-local-repair-key"
    )
    replay = daemon.service.resume(
        "repair-b", caller_scope="project:one", idempotency_key="c-local-repair-key"
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None and manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(record, manifest, m_root, m_config_path, m_config)
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.failed_pending_review_step == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    # --- Runtime half: a separate source, A -> B (local repair) -> C approves and
    # ordinary remaining-checkpoint progression completes.
    r_root, r_plan, r_config_path, r_config, r_a = _progression_source(tmp_path / "runtime")
    r_boot = _bootstrap_cli(r_root, r_config_path, r_config, r_a.name)
    r_calls: list[str] = []
    try:
        run_workflow(
            ControllerConfig(
                repo_root=r_root, plan_path=r_plan, max_turns=40,
                start_step=r_boot.start_step, reserved_run_id="repair-b",
                caller_scope="project:one", keep_runs=keep_runs,
            ),
            r_config, "live", config_dir=r_root, snapshot_config=False,
            runner=lambda argv, **kw: local_repair_b_runner(argv, r_calls, **kw),
            resume=r_boot.resume_context,
        )
    except WorkflowError as exc:
        r_b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B reviewer did not fail")
    assert r_calls == ["reviewer", "worker", "reviewer"]
    r_worker_path = r_b_dir / "turns" / "turn-002" / "result.json"
    r_worker_bytes = r_worker_path.read_bytes()
    r_b_artifacts = _artifact_set(r_b_dir)

    c_boot = _bootstrap_cli(r_root, r_config_path, r_config, r_b_dir.name)
    assert c_boot.start_step == "review"
    c_calls: list[str] = []
    c_selected: list[Path] = []

    def c_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        c_calls.append("worker" if "Implement " in prompt else "reviewer")
        if c_calls[-1] == "reviewer":
            for line in prompt.splitlines():
                if "path (read this exact file):" in line:
                    c_selected.append(
                        Path(line.split(": ", 1)[1].split(". This is the read location", 1)[0])
                    )
        if c_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        return subprocess.CompletedProcess(argv, 0, "approved\n", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=r_root, plan_path=r_plan, max_turns=40,
            start_step=c_boot.start_step, reserved_run_id="c-local-repair",
            keep_runs=keep_runs,
        ),
        r_config, "live", config_dir=r_root, snapshot_config=False,
        runner=c_runner, resume=c_boot.resume_context,
    )
    assert result.status == "completed"
    assert c_calls[0] == "reviewer"
    assert c_calls == ["reviewer", "worker", "reviewer"]
    # C's first reviewer selected B's finalized repair worker receipt (turn 2).
    assert c_selected[0] == r_worker_path
    assert c_selected[0].read_bytes() == r_worker_bytes

    # B source bytes are preserved under keep_runs.
    for rel, data in r_b_artifacts.items():
        if rel == "events.jsonl":
            continue
        assert (r_b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


@pytest.mark.parametrize("keep_runs", [20, 1])
def test_daemon_resume_failed_pending_review_producer_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_runs: int
) -> None:
    """Issue #79 v13: the producing rejection's source run is retained.

    A completes the original worker and fails at review.  B resumes A at
    review, produces a real scoped rejection/repair overlay, then its repair
    worker returns the supported explicit stop marker.  B records the
    interrupted repair receipt with no post-snapshot.  C uses real
    bootstrap's existing ``review_repair_step=implement`` route, completes
    that overlay as its local turn 1 / ordinal 2, then fails at review.

    D is admitted from C through the managed preview/resume/manifest/
    worker-preparation path.  The selected worker owner is C; its producing
    rejection is B's turn 1.  With ``keep_runs=1`` the producer B must be
    preserved so the reviewer's prompt can read the worker receipt and a
    subsequent retry can re-validate the strict binding.

    After an intentional D reviewer failure, another real retry still selects
    the same readable worker and preserves the required lineage and source
    bytes.
    """
    root, plan, cfg_path, cfg, a = _progression_source(tmp_path / "repo")

    # B: resume A at review, reject with repair overlay, then the repair
    # worker returns the supported explicit stop marker.
    b_boot = _bootstrap_cli(root, cfg_path, cfg, a.name)
    assert b_boot.start_step == "review"
    b_calls: list[str] = []

    def b_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        prompt = str(kw["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if b_calls[-1] == "reviewer":
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed\n", "")
        return subprocess.CompletedProcess(
            argv, 0, "AFLOW_STOP: repair worker pauses\n", ""
        )

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="repair-producer",
                keep_runs=20, caller_scope="project:one",
            ),
            cfg, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert "repair worker pauses" in exc.summary, exc.summary
        b = exc.run_dir
    else:
        raise AssertionError("B repair worker did not fail")
    assert b_calls == ["reviewer", "worker"]

    # C: use real bootstrap's review_repair_step=implement route, complete
    # the overlay as local turn 1 / ordinal 2, then fail at review.
    c_boot = _bootstrap_cli(root, cfg_path, cfg, b.name)
    assert c_boot.resume_context.review_repair_step == "implement"
    c_calls: list[str] = []

    def c_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        prompt = str(kw["input"])
        c_calls.append("worker" if "Implement " in prompt else "reviewer")
        if c_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair complete\n", "")
        return subprocess.CompletedProcess(argv, 3, "", "reviewer fails after repair\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=40,
                start_step=c_boot.start_step, reserved_run_id="repair-worker-owner",
                keep_runs=20, caller_scope="project:one",
            ),
            cfg, "live", config_dir=root, snapshot_config=False,
            runner=c_runner, resume=c_boot.resume_context,
        )
    except WorkflowError as exc:
        assert "exited with code 3" in exc.summary, exc.summary
        c = exc.run_dir
    else:
        raise AssertionError("C reviewer did not fail")
    assert c_calls == ["worker", "reviewer"]

    # Verify the producing rejection is B's turn 1.
    c_meta = json.loads((c / "run.json").read_text(encoding="utf-8"))
    assert c_meta["review_rejection_history"][-1]["source_run_id"] == b.name

    # Capture immutable artifacts.
    c_worker_path = c / "turns" / "turn-001" / "result.json"
    worker_bytes = c_worker_path.read_bytes()
    b_artifacts = _artifact_set(b)
    c_artifacts = _artifact_set(c)

    # D: managed preview/resume from C.
    d_boot = _bootstrap_cli(root, cfg_path, cfg, c.name)
    assert d_boot.start_step == "review"

    with pytest.MonkeyPatch.context() as mp:
        daemon, units = _daemon_for(root, cfg_path, cfg, mp)
        preview = daemon.service.run_status(c.name)
        assert preview.evidence["can_resume"] is True
        result = daemon.service.resume(
            c.name, caller_scope="project:one", idempotency_key="producer-retention"
        )
        replay = daemon.service.resume(
            c.name, caller_scope="project:one", idempotency_key="producer-retention"
        )
        assert replay.created is False
        assert replay.run_id == result.run_id
        assert len(units.start_calls) == 1

        record = daemon.service._read_record(result.run_id)
        manifest = daemon.application.repository.get_launch_manifest(result.run_id)
        assert manifest is not None and manifest.start_step == "review"
        prepared, context = _worker_prepared(
            record, manifest, root, cfg_path, cfg
        )
        assert prepared.start_step == "review"
        assert prepared.max_turns == 40

        # D's first reviewer: assert exact worker path, producer retained,
        # no worker before review, and budget40.
        d_calls: list[str] = []
        observations: list[dict[str, object]] = []

        def d_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            prompt = str(kw["input"])
            d_calls.append("worker" if "Implement " in prompt else "reviewer")
            locations = [
                line for line in prompt.splitlines()
                if "path (read this exact file):" in line
            ]
            path = None
            if locations:
                path = Path(
                    locations[0].split(": ", 1)[1]
                    .split(". This is the read location", 1)[0]
                )
            observations.append({
                "has_exact_read_path": path == c_worker_path,
                "path_readable": path is not None and path.is_file(),
                "artifact_unavailable": "unavailable" in prompt,
                "producer_retained": b.is_dir(),
                "worker_retained": c_worker_path.is_file(),
            })
            return subprocess.CompletedProcess(
                argv, 3, "", "probe reviewer fails\n"
            )

        try:
            run_workflow(
                ControllerConfig(
                    repo_root=root, plan_path=prepared.plan_path,
                    max_turns=prepared.max_turns,
                    start_step=prepared.start_step,
                    reserved_run_id=prepared.reserved_run_id,
                    idempotency_key=prepared.idempotency_key,
                    caller_scope=prepared.caller_scope,
                    team_explicit=prepared.team_explicit,
                    max_turns_explicit=prepared.max_turns_explicit,
                    start_step_explicit=prepared.start_step_explicit,
                    keep_runs=keep_runs,
                ),
                cfg, "live", config_dir=root, snapshot_config=False,
                runner=d_runner, resume=context,
                allow_existing_launch_manifest=True,
            )
        except WorkflowError:
            pass

        assert d_calls == ["reviewer"], "no worker before review"
        obs = observations[0]
        assert obs["has_exact_read_path"] is True
        assert obs["path_readable"] is True
        assert obs["artifact_unavailable"] is False
        assert obs["producer_retained"] is True
        assert obs["worker_retained"] is True

        # Worker and producer bytes unchanged.
        assert c_worker_path.read_bytes() == worker_bytes
        for rel, data in b_artifacts.items():
            if rel == "events.jsonl":
                continue
            assert (b / rel).is_file() and (b / rel).read_bytes() == data, (
                f"B artifact {rel} changed"
            )
        for rel, data in c_artifacts.items():
            if rel == "events.jsonl":
                continue
            assert (c / rel).is_file() and (c / rel).read_bytes() == data, (
                f"C artifact {rel} changed"
            )

        # After the D reviewer failure, a further real retry still selects
        # the same readable worker and preserves the required lineage.
        again_boot = _bootstrap_cli(root, cfg_path, cfg, result.run_id)
        assert again_boot.start_step == "review"
        retry_calls: list[str] = []
        retry_observations: list[dict[str, object]] = []

        def retry_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            prompt = str(kw["input"])
            retry_calls.append(
                "worker" if "Implement " in prompt else "reviewer"
            )
            locations = [
                line for line in prompt.splitlines()
                if "path (read this exact file):" in line
            ]
            path = None
            if locations:
                path = Path(
                    locations[0].split(": ", 1)[1]
                    .split(". This is the read location", 1)[0]
                )
            retry_observations.append({
                "has_exact_read_path": path == c_worker_path,
                "path_readable": path is not None and path.is_file(),
                "artifact_unavailable": "unavailable" in prompt,
                "producer_retained": b.is_dir(),
                "worker_retained": c_worker_path.is_file(),
            })
            return subprocess.CompletedProcess(
                argv, 3, "", "retry reviewer fails\n"
            )

        try:
            run_workflow(
                ControllerConfig(
                    repo_root=root, plan_path=plan, max_turns=40,
                    start_step=again_boot.start_step,
                    reserved_run_id="producer-retry",
                    keep_runs=keep_runs, caller_scope="project:one",
                ),
                cfg, "live", config_dir=root, snapshot_config=False,
                runner=retry_runner, resume=again_boot.resume_context,
            )
        except WorkflowError:
            pass

        assert retry_calls == ["reviewer"], "retry: no worker before review"
        rob = retry_observations[0]
        assert rob["has_exact_read_path"] is True
        assert rob["path_readable"] is True
        assert rob["artifact_unavailable"] is False
        assert rob["producer_retained"] is True
        assert rob["worker_retained"] is True
        # Worker bytes still unchanged after the retry.
        assert c_worker_path.read_bytes() == worker_bytes


def test_daemon_resume_failed_pending_review_symlinked_intermediate_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #79 v15: a symlinked intermediate run never supplies owned lineage.

    A completes the original worker and fails at review.  B resumes A at
    review and fails; C resumes B at review and fails.  Moving B's physical
    run directory out and replacing B's recorded location with a directory
    symlink must refuse at CLI bootstrap and managed preview/resume before
    any successor, launch manifest, unit, or provider allocation, leaving the
    A and C artifacts unchanged apart from append-only audit effects.  A
    present link is not absence: the absent-current-run prompt-reference
    seed path is untouched.

    Paired owned control: the same A/B/C chain with B intact admits C at
    review/budget40 through the managed preview/resume/manifest/
    worker-preparation path, selects A's exact readable worker at review
    first, retains the whole chain under ``keep_runs=1``, and a same-key
    replay allocates exactly one successor/unit.
    """
    def failing_review(
        argv: list[str], **kw: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 3, "", "expected reviewer failure"
        )

    def make_chain(
        base: Path,
    ) -> tuple[Path, Path, Path, WorkflowUserConfig, Path, Path]:
        root, plan, cfg_path, cfg, a = _progression_source(base)
        source = a
        for rid in ("lineage-b", "lineage-c"):
            boot = _bootstrap_cli(root, cfg_path, cfg, source.name)
            assert boot.start_step == "review"
            try:
                run_workflow(
                    ControllerConfig(
                        repo_root=root, plan_path=plan, max_turns=40,
                        start_step=boot.start_step, reserved_run_id=rid,
                        keep_runs=20, caller_scope="project:one",
                    ),
                    cfg, "live", config_dir=root, snapshot_config=False,
                    runner=failing_review, resume=boot.resume_context,
                )
            except WorkflowError as exc:
                assert exc.summary.startswith(
                    "harness 'codex' exited with code 3\n"
                ), exc.summary
                source = exc.run_dir
            else:  # pragma: no cover - the producer must fail at the reviewer
                raise AssertionError("expected reviewer failure")
        return root, plan, cfg_path, cfg, a, source

    # --- Symlink half: B's physical dir moved out, directory symlink in place.
    s_root, s_plan, s_cfg_path, s_cfg, s_a, s_c = make_chain(tmp_path / "link")
    s_b = s_root / ".aflow" / "runs" / "lineage-b"
    outside = s_root / "foreign-intermediate"
    s_b.rename(outside)
    s_b.symlink_to(outside, target_is_directory=True)
    assert s_b.is_symlink() and s_b.is_dir()
    before_a = _artifact_set(s_a)
    before_c = _artifact_set(s_c)
    runs_before = set(p.name for p in (s_root / ".aflow" / "runs").iterdir())

    s_repository = RunRepository(s_root)

    def manifest_ids() -> set[str]:
        return {
            rid
            for rid in runs_before
            if s_repository.get_launch_manifest(rid) is not None
        }

    with pytest.raises(ValueError, match="cannot be bound"):
        _bootstrap_cli(s_root, s_cfg_path, s_cfg, s_c.name)

    with pytest.MonkeyPatch.context() as mp:
        daemon, units = _daemon_for(s_root, s_cfg_path, s_cfg, mp)
        preview = daemon.service.run_status(s_c.name)
        assert preview.evidence["can_resume"] is False
        with pytest.raises(ValueError, match="cannot be bound"):
            daemon.service.resume(
                s_c.name, caller_scope="project:one",
                idempotency_key="link-refusal",
            )
        # Zero new run/manifest/unit/provider allocations.
        assert units.start_calls == []
        runs_after = set(p.name for p in (s_root / ".aflow" / "runs").iterdir())
        assert runs_after == runs_before
        assert {
            rid
            for rid in runs_after
            if daemon.application.repository.get_launch_manifest(rid) is not None
        } == manifest_ids()

    # A and C artifacts unchanged apart from append-only audit effects.
    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (s_a / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_c.items():
        if rel == "events.jsonl":
            continue
        assert (s_c / rel).read_bytes() == data, f"C artifact {rel} changed"

    # --- Owned control: same chain with B intact admits C at review/budget40.
    o_root, o_plan, o_cfg_path, o_cfg, o_a, o_c = make_chain(tmp_path / "owned")
    o_b = o_root / ".aflow" / "runs" / "lineage-b"
    assert o_b.is_dir() and not o_b.is_symlink()
    worker_path = o_a / "turns" / "turn-001" / "result.json"
    worker_bytes = worker_path.read_bytes()

    with pytest.MonkeyPatch.context() as mp:
        daemon, units = _daemon_for(o_root, o_cfg_path, o_cfg, mp)
        preview = daemon.service.run_status(o_c.name)
        assert preview.evidence["can_resume"] is True
        result = daemon.service.resume(
            o_c.name, caller_scope="project:one",
            idempotency_key="owned-lineage",
        )
        replay = daemon.service.resume(
            o_c.name, caller_scope="project:one",
            idempotency_key="owned-lineage",
        )
        assert result.created is True
        assert replay.created is False
        assert replay.run_id == result.run_id
        assert len(units.start_calls) == 1
        record = daemon.service._read_record(result.run_id)
        manifest = daemon.application.repository.get_launch_manifest(
            result.run_id
        )
        assert manifest is not None and manifest.start_step == "review"
        prepared, context = _worker_prepared(
            record, manifest, o_root, o_cfg_path, o_cfg
        )
        assert prepared.start_step == "review"
        assert prepared.max_turns == 40

        calls: list[str] = []
        observations: list[dict[str, object]] = []

        def runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            prompt = str(kw["input"])
            calls.append("worker" if "Implement " in prompt else "reviewer")
            locations = [
                line for line in prompt.splitlines()
                if "path (read this exact file):" in line
            ]
            path = None
            if locations:
                path = Path(
                    locations[0].split(": ", 1)[1]
                    .split(". This is the read location", 1)[0]
                )
            observations.append({
                "has_exact_read_path": path == worker_path,
                "path_readable": path is not None and path.is_file(),
                "artifact_unavailable": "unavailable" in prompt,
            })
            return subprocess.CompletedProcess(
                argv, 3, "", "expected reviewer failure"
            )

        try:
            run_workflow(
                ControllerConfig(
                    repo_root=o_root, plan_path=prepared.plan_path,
                    max_turns=prepared.max_turns,
                    start_step=prepared.start_step,
                    reserved_run_id=prepared.reserved_run_id,
                    idempotency_key=prepared.idempotency_key,
                    caller_scope=prepared.caller_scope,
                    keep_runs=1,
                ),
                o_cfg, "live", config_dir=o_root, snapshot_config=False,
                runner=runner, resume=context,
                allow_existing_launch_manifest=True,
            )
        except WorkflowError:
            pass

        # Review first with no preceding worker, reading A's exact worker.
        assert calls == ["reviewer"], "no worker before review"
        obs = observations[0]
        assert obs["has_exact_read_path"] is True
        assert obs["path_readable"] is True
        assert obs["artifact_unavailable"] is False
        # The whole owned chain is retained under keep_runs=1.
        assert o_a.is_dir() and o_b.is_dir() and o_c.is_dir()
        assert worker_path.read_bytes() == worker_bytes


@pytest.mark.parametrize("history", ["ordinal", "legacy"])
@pytest.mark.parametrize("keep_runs", [20, 1])
def test_daemon_resume_failed_pending_review_inherited_producer_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_runs: int, history: str
) -> None:
    """Issue #79 v14: inherited repair producer is retained, ordinal and legacy.

    A completes its original worker (turn 1) and a repair (turn 3 / ordinal 2)
    before failing at review, so its latest worker is an inherited repair whose
    local turn (3) is unique in the scope.  B resumes A at review, produces a
    genuine rejection of that unique predecessor turn, then its repair worker
    returns the supported explicit stop marker.  C completes the repair as its
    local turn 1 / ordinal 3 and fails at review.

    The ``ordinal`` case keeps C's recorded ``reviewed_attempt_ordinal``; the
    ``legacy`` case omits it from C's disposable owner history so the binding
    must use the unique reviewed turn number as the diagnostic predecessor.

    D is admitted from C through the managed preview/resume/manifest/
    worker-preparation path.  The selected worker owner is C; its producing
    rejection is B's turn 1.  With ``keep_runs=1`` the producer B must be
    preserved for both histories so the reviewer's prompt can read the worker
    receipt and a subsequent retry can re-validate the strict binding.
    """
    root, plan, cfg_path, cfg, a = _inherited_repair_source(tmp_path / "repo")

    # B: resume A at review, reject the unique predecessor turn, then the
    # repair worker returns the supported explicit stop marker.
    b_boot = _bootstrap_cli(root, cfg_path, cfg, a.name)
    assert b_boot.start_step == "review"
    b_calls: list[str] = []

    def b_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        prompt = str(kw["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if b_calls[-1] == "reviewer":
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed\n", "")
        return subprocess.CompletedProcess(
            argv, 0, "AFLOW_STOP: repair worker pauses\n", ""
        )

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="repair-producer",
                keep_runs=20, caller_scope="project:one",
            ),
            cfg, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert "repair worker pauses" in exc.summary, exc.summary
        b = exc.run_dir
    else:
        raise AssertionError("B repair worker did not fail")
    assert b_calls == ["reviewer", "worker"]

    # C: use real bootstrap's review_repair_step=implement route, complete the
    # overlay as local turn 1 / ordinal 3, then fail at review.
    c_boot = _bootstrap_cli(root, cfg_path, cfg, b.name)
    assert c_boot.resume_context.review_repair_step == "implement"
    c_calls: list[str] = []

    def c_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        prompt = str(kw["input"])
        c_calls.append("worker" if "Implement " in prompt else "reviewer")
        if c_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair complete\n", "")
        return subprocess.CompletedProcess(
            argv, 3, "", "reviewer fails after repair\n"
        )

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan, max_turns=40,
                start_step=c_boot.start_step, reserved_run_id="repair-worker-owner",
                keep_runs=20, caller_scope="project:one",
            ),
            cfg, "live", config_dir=root, snapshot_config=False,
            runner=c_runner, resume=c_boot.resume_context,
        )
    except WorkflowError as exc:
        assert "exited with code 3" in exc.summary, exc.summary
        c = exc.run_dir
    else:
        raise AssertionError("C reviewer did not fail")
    assert c_calls == ["worker", "reviewer"]

    # The producing rejection is B's turn 1 of the unique predecessor (turn 3,
    # ordinal 2).  The legacy case omits only C's disposable ordinal.
    c_meta = json.loads((c / "run.json").read_text(encoding="utf-8"))
    last_rejection = c_meta["review_rejection_history"][-1]
    assert last_rejection["source_run_id"] == b.name
    assert last_rejection["reviewed_attempt_ordinal"] == 2
    assert last_rejection["reviewed_implementation_turn_number"] == 3
    if history == "legacy":
        last_rejection.pop("reviewed_attempt_ordinal")
        (c / "run.json").write_text(json.dumps(c_meta), encoding="utf-8")

    # Capture immutable artifacts.
    c_worker_path = c / "turns" / "turn-001" / "result.json"
    worker_bytes = c_worker_path.read_bytes()
    b_artifacts = _artifact_set(b)
    c_artifacts = _artifact_set(c)

    # D: managed preview/resume from C.
    d_boot = _bootstrap_cli(root, cfg_path, cfg, c.name)
    assert d_boot.start_step == "review"

    with pytest.MonkeyPatch.context() as mp:
        daemon, units = _daemon_for(root, cfg_path, cfg, mp)
        preview = daemon.service.run_status(c.name)
        assert preview.evidence["can_resume"] is True
        result = daemon.service.resume(
            c.name, caller_scope="project:one", idempotency_key="inherited-retention"
        )
        replay = daemon.service.resume(
            c.name, caller_scope="project:one", idempotency_key="inherited-retention"
        )
        assert replay.created is False
        assert replay.run_id == result.run_id
        assert len(units.start_calls) == 1

        record = daemon.service._read_record(result.run_id)
        manifest = daemon.application.repository.get_launch_manifest(result.run_id)
        assert manifest is not None and manifest.start_step == "review"
        prepared, context = _worker_prepared(record, manifest, root, cfg_path, cfg)
        assert prepared.start_step == "review"
        assert prepared.max_turns == 40

        # D's first reviewer: assert exact worker path, producer retained,
        # no worker before review, and budget40.
        d_calls: list[str] = []
        observations: list[dict[str, object]] = []

        def d_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            prompt = str(kw["input"])
            d_calls.append("worker" if "Implement " in prompt else "reviewer")
            locations = [
                line for line in prompt.splitlines()
                if "path (read this exact file):" in line
            ]
            path = None
            if locations:
                path = Path(
                    locations[0].split(": ", 1)[1]
                    .split(". This is the read location", 1)[0]
                )
            observations.append({
                "has_exact_read_path": path == c_worker_path,
                "path_readable": path is not None and path.is_file(),
                "artifact_unavailable": "unavailable" in prompt,
                "producer_retained": b.is_dir(),
                "worker_retained": c_worker_path.is_file(),
            })
            return subprocess.CompletedProcess(argv, 3, "", "probe reviewer fails\n")

        try:
            run_workflow(
                ControllerConfig(
                    repo_root=root, plan_path=prepared.plan_path,
                    max_turns=prepared.max_turns,
                    start_step=prepared.start_step,
                    reserved_run_id=prepared.reserved_run_id,
                    idempotency_key=prepared.idempotency_key,
                    caller_scope=prepared.caller_scope,
                    team_explicit=prepared.team_explicit,
                    max_turns_explicit=prepared.max_turns_explicit,
                    start_step_explicit=prepared.start_step_explicit,
                    keep_runs=keep_runs,
                ),
                cfg, "live", config_dir=root, snapshot_config=False,
                runner=d_runner, resume=context,
                allow_existing_launch_manifest=True,
            )
        except WorkflowError:
            pass

        assert d_calls == ["reviewer"], "no worker before review"
        obs = observations[0]
        assert obs["has_exact_read_path"] is True
        assert obs["path_readable"] is True
        assert obs["artifact_unavailable"] is False
        assert obs["producer_retained"] is True
        assert obs["worker_retained"] is True

        # Worker and producer bytes unchanged.
        assert c_worker_path.read_bytes() == worker_bytes
        for rel, data in b_artifacts.items():
            if rel == "events.jsonl":
                continue
            assert (b / rel).is_file() and (b / rel).read_bytes() == data, (
                f"B artifact {rel} changed"
            )
        for rel, data in c_artifacts.items():
            if rel == "events.jsonl":
                continue
            assert (c / rel).is_file() and (c / rel).read_bytes() == data, (
                f"C artifact {rel} changed"
            )

        # After the D reviewer failure, a further real retry still selects the
        # same readable worker and preserves the required lineage.
        again_boot = _bootstrap_cli(root, cfg_path, cfg, result.run_id)
        assert again_boot.start_step == "review"
        retry_calls: list[str] = []
        retry_observations: list[dict[str, object]] = []

        def retry_runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            prompt = str(kw["input"])
            retry_calls.append("worker" if "Implement " in prompt else "reviewer")
            locations = [
                line for line in prompt.splitlines()
                if "path (read this exact file):" in line
            ]
            path = None
            if locations:
                path = Path(
                    locations[0].split(": ", 1)[1]
                    .split(". This is the read location", 1)[0]
                )
            retry_observations.append({
                "has_exact_read_path": path == c_worker_path,
                "path_readable": path is not None and path.is_file(),
                "artifact_unavailable": "unavailable" in prompt,
                "producer_retained": b.is_dir(),
                "worker_retained": c_worker_path.is_file(),
            })
            return subprocess.CompletedProcess(argv, 3, "", "retry reviewer fails\n")

        try:
            run_workflow(
                ControllerConfig(
                    repo_root=root, plan_path=plan, max_turns=40,
                    start_step=again_boot.start_step,
                    reserved_run_id="inherited-retry",
                    keep_runs=keep_runs, caller_scope="project:one",
                ),
                cfg, "live", config_dir=root, snapshot_config=False,
                runner=retry_runner, resume=again_boot.resume_context,
            )
        except WorkflowError:
            pass

        assert retry_calls == ["reviewer"], "retry: no worker before review"
        rob = retry_observations[0]
        assert rob["has_exact_read_path"] is True
        assert rob["path_readable"] is True
        assert rob["artifact_unavailable"] is False
        assert rob["producer_retained"] is True
        assert rob["worker_retained"] is True
        # Worker bytes still unchanged after the retry.
        assert c_worker_path.read_bytes() == worker_bytes


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_worker_plan",
        "unfinalized_worker",
        "wrong_worker_turn",
        "symlink_worker_turn",
        "non_object_receipt",
        "malformed_json",
    ],
)
def test_daemon_resume_failed_pending_review_worker_owner_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v06: invalid inherited worker evidence refuses before allocation.

    A produces the checkpoint-1 worker and a failed reviewer; B resumes
    review-first and its reviewer fails.  Mutating only A's task-owned worker
    evidence (foreign original plan, unfinalized status, wrong turn number,
    symlinked turn directory, non-object receipt, or malformed JSON) must make
    the managed preview refuse and the resume raise before any successor run,
    manifest, unit, or provider is allocated, with A and B source bytes
    preserved.  A JSON object or file existence alone never proves ownership.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20,
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=lambda argv, **kw: subprocess.CompletedProcess(
                argv, 3, "", "reviewer failed\n"
            ),
            resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B reviewer did not fail")

    receipt = a_dir / "turns" / "turn-001" / "result.json"
    if mutation == "foreign_worker_plan":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["original_plan_path"] = str(root / "foreign-plan.md")
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "unfinalized_worker":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["status"] = "starting"
        data.pop("snapshot_after", None)
        data["chosen_transition"] = None
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "wrong_worker_turn":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["turn_number"] = 999
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "symlink_worker_turn":
        turn = receipt.parent
        outside = root / "foreign-turn"
        turn.rename(outside)
        turn.symlink_to(outside, target_is_directory=True)
    elif mutation == "non_object_receipt":
        receipt.write_text("[1, 2, 3]\n", encoding="utf-8")
    elif mutation == "malformed_json":
        receipt.write_text("{not valid json\n", encoding="utf-8")

    before_a = _artifact_set(a_dir)
    before_b = _artifact_set(b_dir)

    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is False
    with pytest.raises(ValueError, match="worker receipt cannot be bound"):
        daemon.service.resume(
            "review-b", caller_scope="project:one", idempotency_key="owner-negative"
        )
    assert units.start_calls == []

    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (a_dir / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_b.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_worker_step",
        "missing_post_snapshot",
        "missing_transition",
        "nonzero_completed_returncode",
        "foreign_scope_owner",
        "wrong_attempt_ordinal",
        "symlink_turns_parent",
        "missing_owner_attempt",
        "symlink_metadata",
    ],
)
def test_daemon_resume_failed_pending_review_v07_worker_binding_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v07: shared worker binding rejects incomplete/foreign/symlink evidence.

    A produces the checkpoint-1 worker and a failed reviewer; B resumes
    review-first and its reviewer fails.  Each mutation corrupts a distinct
    dimension of the worker ownership/finalization evidence: the receipt's
    step identity, post snapshot, produced transition, completed return code;
    the owner's recorded scope id or scope-local ordinal; a symlinked ``turns``
    parent; the owner's recorded attempt; or a symlinked metadata file.  The
    managed preview must refuse and the resume raise before any successor run,
    manifest, unit, or provider is allocated, with A and B source bytes
    preserved.  A JSON object or file existence alone never proves ownership.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20,
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=lambda argv, **kw: subprocess.CompletedProcess(
                argv, 3, "", "reviewer failed\n"
            ),
            resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B reviewer did not fail")

    receipt = a_dir / "turns" / "turn-001" / "result.json"
    if mutation == "wrong_worker_step":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["step_name"] = "review"
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "missing_post_snapshot":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data.pop("snapshot_after", None)
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "missing_transition":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["chosen_transition"] = None
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "nonzero_completed_returncode":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["returncode"] = 1
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "foreign_scope_owner":
        meta_path = b_dir / "run.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        attempts = meta["implementation_attempts"]
        scope_id = next(iter(attempts))
        attempts[f"{scope_id}::foreign"] = attempts.pop(scope_id)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    elif mutation == "wrong_attempt_ordinal":
        meta_path = b_dir / "run.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        scope_id = next(iter(meta["implementation_attempts"]))
        meta["implementation_attempts"][scope_id][0]["attempt_ordinal"] = 9
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    elif mutation == "symlink_turns_parent":
        turns = a_dir / "turns"
        outside = root / "foreign-turns"
        turns.rename(outside)
        turns.symlink_to(outside, target_is_directory=True)
    elif mutation == "missing_owner_attempt":
        meta_path = a_dir / "run.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("implementation_attempts", None)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    elif mutation == "symlink_metadata":
        import os as _os

        meta_path = a_dir / "run.json"
        outside_meta = root / "foreign-run.json"
        meta_path.rename(outside_meta)
        _os.symlink(outside_meta, meta_path)
    else:  # pragma: no cover - parametrize covers every case
        raise AssertionError(f"unhandled mutation {mutation!r}")

    before_a = _artifact_set(a_dir)
    before_b = _artifact_set(b_dir)
    before_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}

    # A corrupt worker-binding lineage must refuse before any allocation: a
    # real can_resume=false preview and a raising clean refusal, with no new
    # run, manifest, start request, unit, or provider. Unexpected daemon or
    # preview exceptions fail the test instead of masking a preview regression.
    if mutation == "symlink_metadata":
        # The one explicitly isolated case: the control plane repository
        # refuses a symlinked run metadata file at daemon reconciliation,
        # before any preview or unit exists.
        with pytest.raises(RepositoryError):
            _daemon_for(root, config_path, config, monkeypatch)
    else:
        daemon, units = _daemon_for(root, config_path, config, monkeypatch)
        preview = daemon.service.run_status("review-b")
        assert preview.evidence["can_resume"] is False, (
            f"{mutation}: managed preview admitted a corrupt binding"
        )
        with pytest.raises(ValueError):
            daemon.service.resume(
                "review-b", caller_scope="project:one", idempotency_key="v07-negative"
            )
        assert units.start_calls == [], (
            f"{mutation}: a successor unit started despite a corrupt binding"
        )
    after_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert after_runs == before_runs, (
        f"{mutation}: a new run or launch manifest was allocated despite a "
        "corrupt binding"
    )

    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (a_dir / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_b.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_worker_post_snapshot",
        "malformed_worker_before_snapshot",
        "blank_worker_transition",
        "foreign_worker_active_plan",
        "nonstring_owner_ordinal",
        "boolean_owner_ordinal",
        "foreign_owner_checkpoint",
    ],
)
def test_daemon_resume_failed_pending_review_v08_binding_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v08: worker finalization, active-plan, ordinal, and owner identity.

    A produces the checkpoint-1 worker and a failed reviewer; B resumes
    review-first and its reviewer fails.  Each mutation corrupts a distinct
    dimension that v07 left under-validated: the receipt's decoded before/after
    snapshots, its produced transition, its bound active plan, a malformed
    present owner ordinal, or the owner's checkpoint identity.  The managed
    preview must refuse and the resume raise before any successor run, manifest,
    unit, start request, or provider is allocated, with A and B source bytes
    preserved.  A JSON object or file existence alone never proves ownership.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20,
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=lambda argv, **kw: subprocess.CompletedProcess(
                argv, 3, "", "reviewer failed\n"
            ),
            resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B reviewer did not fail")

    receipt = a_dir / "turns" / "turn-001" / "result.json"
    meta_path = b_dir / "run.json"
    if mutation == "empty_worker_post_snapshot":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["snapshot_after"] = {}
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "malformed_worker_before_snapshot":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["snapshot_before"] = [1]
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "blank_worker_transition":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["chosen_transition"] = "   "
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "foreign_worker_active_plan":
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["active_plan_path"] = str(root / "foreign-plan.md")
        receipt.write_text(json.dumps(data), encoding="utf-8")
    elif mutation in ("nonstring_owner_ordinal", "boolean_owner_ordinal"):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        scope_id = next(iter(meta["implementation_attempts"]))
        meta["implementation_attempts"][scope_id][0]["attempt_ordinal"] = (
            "1" if mutation == "nonstring_owner_ordinal" else True
        )
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    elif mutation == "foreign_owner_checkpoint":
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["active_implementation_scope"]["checkpoint_name"] = (
            "Checkpoint 2: Next"
        )
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    else:  # pragma: no cover - parametrize covers every case
        raise AssertionError(f"unhandled mutation {mutation!r}")

    before_a = _artifact_set(a_dir)
    before_b = _artifact_set(b_dir)
    before_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}

    # A corrupt worker-binding lineage must refuse before any allocation: a
    # real can_resume=false preview and a raising clean refusal, with no new
    # run, manifest, start request, unit, or provider. Unexpected daemon or
    # preview exceptions fail the test instead of masking a preview regression.
    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is False, (
        f"{mutation}: managed preview admitted a corrupt binding"
    )
    with pytest.raises(ValueError):
        daemon.service.resume(
            "review-b", caller_scope="project:one", idempotency_key="v08-negative"
        )
    assert units.start_calls == [], (
        f"{mutation}: a successor unit started despite a corrupt binding"
    )
    after_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert after_runs == before_runs, (
        f"{mutation}: a new run or launch manifest was allocated despite a "
        "corrupt binding"
    )

    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (a_dir / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_b.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


@pytest.mark.parametrize(
    "mutation",
    [
        "owner_checkpoint_index",
        "owner_envelope_corrupt",
        "owner_conflicting_duplicate",
        "owner_legacy_duplicate",
        "local_original_plan",
        "local_twice_old_plan",
        "local_bad_rejection_ordinal",
    ],
)
def test_daemon_resume_failed_pending_review_v09_binding_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v09: producer-bound repair identity, owner scope, and attempt.

    A produces the checkpoint-1 worker and a failed reviewer; B resumes
    review-first. The ``local_*`` mutations perform a real B-side rejection and
    repair worker (``local_twice_*`` performs two real rejection/repair cycles,
    selecting local worker turn 4, ordinal 3) before the failing review; the
    ``owner_*`` mutations fail B's first review. Each mutation corrupts a
    dimension that earlier snapshots left under-bound: the owner's checkpoint
    index, the owner's envelope artifact bytes, the owner's attempt sequence
    (stale/conflicting ordinal or ambiguous legacy duplicate), or the selected
    repair worker's producing-rejection relationship (original plan named by a
    repair worker, a stale same-scope overlay after two repairs, or a
    rejection whose reviewed ordinal no longer matches). The managed preview
    must refuse (real ``can_resume=false``) and the resume raise before any
    successor run, manifest, start request, unit, or provider is allocated,
    with A and B source bytes preserved (only the append-only audit journal
    may grow). A matching reference string, an earlier plausible attempt row,
    or any same-scope overlay never proves the selected binding.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    local = mutation.startswith("local_")
    twice = mutation.startswith("local_twice_")
    b_calls: list[str] = []

    def b_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if local and (len(b_calls) == 1 or (twice and len(b_calls) == 3)):
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed", "")
        if b_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair done", "")
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20, caller_scope="project:one",
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B did not fail at review")
    if local:
        assert b_calls == (
            ["reviewer", "worker", "reviewer", "worker", "reviewer"]
            if twice
            else ["reviewer", "worker", "reviewer"]
        )

    owner_dir = b_dir if local else a_dir
    owner_meta_path = owner_dir / "run.json"
    owner_meta = json.loads(owner_meta_path.read_text(encoding="utf-8"))
    scope_id = owner_meta["active_implementation_scope"]["scope_id"]
    worker_path = (
        b_dir
        / (
            "turns/turn-004/result.json"
            if twice
            else "turns/turn-002/result.json"
        )
        if local
        else a_dir / "turns/turn-001/result.json"
    )
    if mutation == "owner_checkpoint_index":
        # B's awaiting scope and A's saved envelope still identify checkpoint 1;
        # only the owner's recorded index is contradicted.
        owner_meta["active_implementation_scope"]["checkpoint_index"] = 2
        owner_meta_path.write_text(json.dumps(owner_meta), encoding="utf-8")
    elif mutation == "owner_envelope_corrupt":
        # The recorded hashes are unchanged; only the artifact bytes are not.
        envelope_path = owner_meta["active_implementation_scope"][
            "envelope_artifact_path"
        ]
        (owner_dir / envelope_path).write_text("{}", encoding="utf-8")
    elif mutation in {"owner_conflicting_duplicate", "owner_legacy_duplicate"}:
        attempts = owner_meta["implementation_attempts"][scope_id]
        duplicate = dict(attempts[0])
        if mutation == "owner_conflicting_duplicate":
            duplicate["attempt_ordinal"] = 999
        else:
            attempts[0].pop("attempt_ordinal", None)
            duplicate.pop("attempt_ordinal", None)
        attempts.append(duplicate)
        owner_meta_path.write_text(json.dumps(owner_meta), encoding="utf-8")
    elif mutation == "local_original_plan":
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        worker["active_plan_path"] = str(plan_path)
        worker_path.write_text(json.dumps(worker), encoding="utf-8")
    elif mutation == "local_twice_old_plan":
        repair = owner_meta["review_rejection_history"][0]["repair_plan_path"]
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        worker["active_plan_path"] = str(root / repair)
        worker_path.write_text(json.dumps(worker), encoding="utf-8")
    elif mutation == "local_bad_rejection_ordinal":
        owner_meta["review_rejection_history"][-1]["reviewed_attempt_ordinal"] = 999
        owner_meta_path.write_text(json.dumps(owner_meta), encoding="utf-8")
    else:  # pragma: no cover - parametrize covers every case
        raise AssertionError(f"unhandled mutation {mutation!r}")

    before_a = _artifact_set(a_dir)
    before_b = _artifact_set(b_dir)
    before_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}

    # The invalid selected binding must refuse before any allocation: a real
    # can_resume=false preview and a raising clean refusal, with no new run,
    # manifest, start request, unit, or provider. Unexpected daemon or
    # preview exceptions fail the test instead of masking a preview
    # regression.
    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is False, (
        f"{mutation}: managed preview admitted an invalid binding"
    )
    with pytest.raises(ValueError):
        daemon.service.resume(
            "review-b", caller_scope="project:one", idempotency_key="v09-negative"
        )
    assert units.start_calls == [], (
        f"{mutation}: a successor unit started despite an invalid binding"
    )
    after_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert after_runs == before_runs, (
        f"{mutation}: a new run or launch manifest was allocated despite an "
        "invalid binding"
    )

    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (a_dir / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_b.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


@pytest.mark.parametrize(
    "mutation", ["local_twice_control", "owner_legacy_control"],
)
def test_daemon_resume_failed_pending_review_v09_controls_admit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v09: valid two-repair and unique-legacy bindings stay admitted.

    ``local_twice_control`` performs two real rejection/repair cycles in B and
    selects local worker turn 4, ordinal 3, bound to the second consumed
    overlay. ``owner_legacy_control`` removes the owner's sole recorded
    ordinal, leaving exactly one legacy attempt that uniquely identifies the
    worker. Both must admit the managed preview/resume with budget40 and the
    review step, and the selected worker receipt must be the exact intended
    one with its exact intended active plan.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    twice = mutation == "local_twice_control"
    b_calls: list[str] = []

    def b_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if twice and (len(b_calls) == 1 or len(b_calls) == 3):
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed", "")
        if b_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair done", "")
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20, caller_scope="project:one",
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B did not fail at review")
    assert b_calls == (
        ["reviewer", "worker", "reviewer", "worker", "reviewer"]
        if twice
        else ["reviewer"]
    )

    if mutation == "owner_legacy_control":
        meta_path = a_dir / "run.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        scope_id = next(iter(meta["implementation_attempts"]))
        meta["implementation_attempts"][scope_id][0].pop("attempt_ordinal", None)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is True, (
        f"{mutation}: a valid binding was not admitted"
    )
    continuation = daemon.service.resume(
        "review-b", caller_scope="project:one", idempotency_key="v09-control"
    )
    replay = daemon.service.resume(
        "review-b", caller_scope="project:one", idempotency_key="v09-control"
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    assert units.start_calls[0][0] == f"aflow-run-{continuation.run_id}.service"
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    assert manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(
        record, manifest, root, config_path, config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.failed_pending_review_step == "review"

    # The selected worker receipt is the exact intended one with its exact
    # intended active plan: the second consumed overlay after two repairs, or
    # the original plan under the unique legacy owner record.
    if twice:
        worker_receipt = b_dir / "turns" / "turn-004" / "result.json"
        second_overlay = json.loads(
            (b_dir / "run.json").read_text(encoding="utf-8")
        )["review_rejection_history"][1]["repair_plan_path"]
        expected_active = str(root / second_overlay)
    else:
        worker_receipt = a_dir / "turns" / "turn-001" / "result.json"
        expected_active = str(plan_path)
    receipt = json.loads(worker_receipt.read_text(encoding="utf-8"))
    assert receipt["active_plan_path"] == expected_active
    assert receipt["status"] == "completed"
    assert receipt["returncode"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "local_rejection_foreign_source",
        "local_rejection_wrong_review_step",
        "local_rejection_wrong_review_turn",
        "local_rejection_foreign_checkpoint",
        "local_rejection_missing_predecessor",
    ],
)
def test_daemon_resume_failed_pending_review_v10_producing_review_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v10: the selected repair's producing rejection is bound to its actual owned review.

    B performs one real rejection and repair worker before the failing
    review, so the selected local worker (turn 2, ordinal 2) is bound to its
    producing rejection. Each mutation breaks one dimension of that producing
    relationship: the rejection's source run is foreign to the recorded
    lineage, its review step is not the producing reviewer step, its review
    turn has no producing receipt, its checkpoint is not the awaiting
    scope's checkpoint, or the recorded predecessor worker is removed from
    the scope's attempt sequence. The managed preview must refuse (real
    ``can_resume=false``) and the resume raise before any successor run,
    manifest, start request, unit, or provider is allocated, with A and B
    source bytes preserved (only the append-only audit journal may grow).
    Matching ordinal/path metadata alone never proves the producing
    relationship.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    b_calls: list[str] = []

    def b_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if len(b_calls) == 1:
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed", "")
        if b_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair done", "")
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20, caller_scope="project:one",
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B did not fail at review")
    assert b_calls == ["reviewer", "worker", "reviewer"]

    owner_meta_path = b_dir / "run.json"
    owner_meta = json.loads(owner_meta_path.read_text(encoding="utf-8"))
    scope_id = owner_meta["active_implementation_scope"]["scope_id"]
    if mutation == "local_rejection_foreign_source":
        owner_meta["review_rejection_history"][-1]["source_run_id"] = "foreign-run"
    elif mutation == "local_rejection_wrong_review_step":
        owner_meta["review_rejection_history"][-1]["review_step_name"] = "implement"
    elif mutation == "local_rejection_wrong_review_turn":
        owner_meta["review_rejection_history"][-1]["review_turn_number"] = 999
    elif mutation == "local_rejection_foreign_checkpoint":
        owner_meta["review_rejection_history"][-1]["checkpoint_index"] = 2
    elif mutation == "local_rejection_missing_predecessor":
        owner_meta["implementation_attempts"][scope_id] = (
            owner_meta["implementation_attempts"][scope_id][1:]
        )
    else:  # pragma: no cover - parametrize covers every case
        raise AssertionError(f"unhandled mutation {mutation!r}")
    owner_meta_path.write_text(json.dumps(owner_meta), encoding="utf-8")

    before_a = _artifact_set(a_dir)
    before_b = _artifact_set(b_dir)
    before_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}

    # The invalid producing-review binding must refuse before any
    # allocation: a real can_resume=false preview and a raising clean
    # refusal, with no new run, manifest, start request, unit, or provider.
    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is False, (
        f"{mutation}: managed preview admitted an invalid producing-review "
        "binding"
    )
    with pytest.raises(ValueError):
        daemon.service.resume(
            "review-b", caller_scope="project:one", idempotency_key="v10-negative"
        )
    assert units.start_calls == [], (
        f"{mutation}: a successor unit started despite an invalid "
        "producing-review binding"
    )
    after_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert after_runs == before_runs, (
        f"{mutation}: a new run or launch manifest was allocated despite an "
        "invalid producing-review binding"
    )

    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (a_dir / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_b.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


@pytest.mark.parametrize(
    "mutation",
    [
        "local_review_missing_rejection",
        "local_review_foreign_repair",
        "local_review_missing_post_snapshot",
        "local_review_missing_transition",
    ],
)
def test_daemon_resume_failed_pending_review_v11_producing_receipt_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v11: the producing receipt's actual rejection/finalization is bound.

    B performs one real rejection and repair worker before the failing review,
    so the selected local worker (turn 2, ordinal 2) is bound to its producing
    rejection. Each mutation breaks one dimension of the producing review
    receipt's actual finalization evidence: its ``review_rejection`` is
    removed, its recorded ``repair_plan_path`` names a foreign overlay, its
    post-snapshot is removed, or its produced transition is removed. A
    completed reviewer status at the named step/turn alone must not admit
    review. The managed preview must refuse (real ``can_resume=false``) and the
    resume raise before any successor run, manifest, start request, unit, or
    provider is allocated, with A and B source bytes preserved (only the
    append-only audit journal may grow).
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    b_calls: list[str] = []

    def b_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if len(b_calls) == 1:
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed", "")
        if b_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair done", "")
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20, caller_scope="project:one",
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B did not fail at review")
    assert b_calls == ["reviewer", "worker", "reviewer"]

    # Only B's disposable producing-review receipt (turn 1) is mutated; its
    # owner history and selected worker remain unchanged.
    review_path = b_dir / "turns" / "turn-001" / "result.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if mutation == "local_review_missing_rejection":
        review.pop("review_rejection", None)
    elif mutation == "local_review_foreign_repair":
        review["review_rejection"]["repair_plan_path"] = "foreign-overlay.md"
    elif mutation == "local_review_missing_post_snapshot":
        review["snapshot_after"] = None
    elif mutation == "local_review_missing_transition":
        review["chosen_transition"] = None
    else:  # pragma: no cover - parametrize covers every case
        raise AssertionError(f"unhandled mutation {mutation!r}")
    review_path.write_text(json.dumps(review), encoding="utf-8")

    before_a = _artifact_set(a_dir)
    before_b = _artifact_set(b_dir)
    before_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}

    # The invalid producing-receipt binding must refuse before any allocation:
    # a real can_resume=false preview and a raising clean refusal, with no new
    # run, manifest, start request, unit, or provider.
    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is False, (
        f"{mutation}: managed preview admitted a producing receipt whose "
        "actual rejection/finalization evidence is missing or contradictory"
    )
    with pytest.raises(ValueError):
        daemon.service.resume(
            "review-b", caller_scope="project:one", idempotency_key="v11-negative"
        )
    assert units.start_calls == [], (
        f"{mutation}: a successor unit started despite an invalid producing "
        "receipt binding"
    )
    after_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert after_runs == before_runs, (
        f"{mutation}: a new run or launch manifest was allocated despite an "
        "invalid producing-receipt binding"
    )

    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (a_dir / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_b.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


@pytest.mark.parametrize(
    "mutation",
    [
        "local_receipt_foreign_scope",
        "local_receipt_foreign_source",
        "local_receipt_wrong_predecessor",
        "local_receipt_foreign_checkpoint",
        "local_receipt_foreign_selector",
        "local_receipt_foreign_original",
        "local_receipt_missing_before",
        "local_receipt_inconsistent_after",
        "local_receipt_wrong_transition",
        "local_receipt_false_new_plan",
        "local_receipt_boolean_returncode",
    ],
)
def test_daemon_resume_failed_pending_review_v12_producing_receipt_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v12: the producing receipt's recorded rejection and finalization are correlated.

    B performs one real rejection and repair worker before the failing review,
    so the selected local worker (turn 2, ordinal 2) is bound to its producing
    rejection. Each mutation breaks one dimension of the producing review
    receipt's actual relationship to the selected owner-history rejection:
    the recorded rejection's scope, source run, reviewed predecessor ordinal,
    or checkpoint is foreign; the receipt's selector or original plan is
    foreign; its before-snapshot is removed; its after-snapshot contradicts
    the before-snapshot; its produced transition is not the selected repair
    worker's step; its ``NEW_PLAN_EXISTS`` condition is false; or its return
    code is boolean false instead of the producer-supported integer zero. The
    managed preview must refuse (real ``can_resume=false``) and the resume
    raise before any successor run, manifest, start request, unit, or provider
    is allocated, with A and B source bytes preserved (only the append-only
    audit journal may grow). A completed reviewer status at the named
    step/turn with matching ordinal/path metadata alone must not admit review.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    b_calls: list[str] = []

    def b_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if len(b_calls) == 1:
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed", "")
        if b_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair done", "")
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20, caller_scope="project:one",
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B did not fail at review")
    assert b_calls == ["reviewer", "worker", "reviewer"]

    # Only B's disposable producing-review receipt (turn 1) is mutated; its
    # owner history and selected worker remain unchanged.
    review_path = b_dir / "turns" / "turn-001" / "result.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if mutation == "local_receipt_foreign_scope":
        review["review_rejection"]["scope_id"] = "foreign-scope"
    elif mutation == "local_receipt_foreign_source":
        review["review_rejection"]["source_run_id"] = "foreign-source"
    elif mutation == "local_receipt_wrong_predecessor":
        review["review_rejection"]["reviewed_attempt_ordinal"] = 999
    elif mutation == "local_receipt_foreign_checkpoint":
        review["review_rejection"]["checkpoint_name"] = "Foreign checkpoint"
    elif mutation == "local_receipt_foreign_selector":
        review["selector"] = "foreign-reviewer"
    elif mutation == "local_receipt_foreign_original":
        review["original_plan_path"] = "foreign-original.md"
    elif mutation == "local_receipt_missing_before":
        review["snapshot_before"] = None
    elif mutation == "local_receipt_inconsistent_after":
        review["snapshot_after"]["total_checkpoint_count"] += 1
    elif mutation == "local_receipt_wrong_transition":
        review["chosen_transition"] = "review"
    elif mutation == "local_receipt_false_new_plan":
        review["conditions"]["NEW_PLAN_EXISTS"] = False
    elif mutation == "local_receipt_boolean_returncode":
        review["returncode"] = False
    else:  # pragma: no cover - parametrize covers every case
        raise AssertionError(f"unhandled mutation {mutation!r}")
    review_path.write_text(json.dumps(review), encoding="utf-8")

    before_a = _artifact_set(a_dir)
    before_b = _artifact_set(b_dir)
    before_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}

    # The invalid producing-receipt correlation must refuse before any
    # allocation: a real can_resume=false preview and a raising clean refusal,
    # with no new run, manifest, start request, unit, or provider.
    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is False, (
        f"{mutation}: managed preview admitted a producing receipt whose "
        "recorded rejection or finalization contradicts the selected "
        "owner-history relationship"
    )
    with pytest.raises(ValueError):
        daemon.service.resume(
            "review-b", caller_scope="project:one", idempotency_key="v12-negative"
        )
    assert units.start_calls == [], (
        f"{mutation}: a successor unit started despite an invalid producing "
        "receipt correlation"
    )
    after_runs = {p.name for p in (root / ".aflow" / "runs").iterdir()}
    assert after_runs == before_runs, (
        f"{mutation}: a new run or launch manifest was allocated despite an "
        "invalid producing receipt correlation"
    )

    for rel, data in before_a.items():
        if rel == "events.jsonl":
            continue
        assert (a_dir / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in before_b.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"


def test_daemon_resume_failed_pending_review_v12_equivalent_repair_admits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #79 v12: equivalent relative/absolute overlay spelling stays admitted.

    The producing receipt's recorded rejection repair path is converted from
    its relative spelling to the equivalent absolute spelling of the same
    historical overlay. The path identity is resolved against the repository,
    so the same overlay must stay admitted: the managed preview/resume select
    the review step with budget40 and the exact local repair worker (turn 2)
    with its repair overlay active plan, and same-key replay is idempotent.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    b_calls: list[str] = []

    def b_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if len(b_calls) == 1:
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed", "")
        if b_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair done", "")
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20, caller_scope="project:one",
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B did not fail at review")
    assert b_calls == ["reviewer", "worker", "reviewer"]

    # Convert only the receipt rejection's repair path to its equivalent
    # absolute spelling; the selected owner-history record is unchanged.
    review_path = b_dir / "turns" / "turn-001" / "result.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["review_rejection"]["repair_plan_path"] = str(
        root / review["review_rejection"]["repair_plan_path"]
    )
    review_path.write_text(json.dumps(review), encoding="utf-8")

    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is True, (
        "an equivalent relative/absolute overlay spelling was refused"
    )
    continuation = daemon.service.resume(
        "review-b", caller_scope="project:one", idempotency_key="v12-equivalent"
    )
    replay = daemon.service.resume(
        "review-b", caller_scope="project:one", idempotency_key="v12-equivalent"
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    prepared, resume_context = _worker_prepared(
        record, manifest, root, config_path, config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.failed_pending_review_step == "review"

    # The actual successor's first provider is the reviewer naming the exact
    # local worker receipt with its repair overlay active plan.
    calls: list[str] = []
    selected: list[Path] = []

    def c_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        calls.append("worker" if "Implement " in prompt else "reviewer")
        line = next(
            x for x in prompt.splitlines() if "path (read this exact file):" in x
        )
        selected.append(
            Path(line.split(": ", 1)[1].split(". This is the read location", 1)[0])
        )
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=prepared.plan_path,
                max_turns=prepared.max_turns, start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=c_runner, resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
    else:  # pragma: no cover - the successor must fail at the reviewer
        raise AssertionError("the successor did not fail at review")
    assert calls == ["reviewer"], (
        "the successor dispatched a worker before the pending review"
    )
    expected_receipt = b_dir / "turns" / "turn-002" / "result.json"
    assert selected == [expected_receipt]


@pytest.mark.parametrize(
    "mutation", ["local_control", "local_legacy_rejection_control"],
)
def test_daemon_resume_failed_pending_review_v10_local_controls_admit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Issue #79 v10: valid local repair bindings, including an ordinal-absent producing rejection, stay admitted.

    ``local_control`` keeps the real rejection/repair/reviewer-failure
    sequence; ``local_legacy_rejection_control`` removes only
    ``reviewed_attempt_ordinal`` from the unique rejection, whose review and
    worker evidence still establish the same relationship. Both must admit
    the managed preview/resume with budget40 and the review step, and the
    actual successor's first provider must be the reviewer naming the exact
    local worker receipt (turn 2) with its repair overlay active plan.
    """
    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    b_boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert b_boot.start_step == "review"
    b_calls: list[str] = []

    def b_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        b_calls.append("worker" if "Implement " in prompt else "reviewer")
        if len(b_calls) == 1:
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "repair needed", "")
        if b_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "repair done", "")
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=plan_path, max_turns=40,
                start_step=b_boot.start_step, reserved_run_id="review-b",
                keep_runs=20, caller_scope="project:one",
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=b_runner, resume=b_boot.resume_context,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B did not fail at review")
    assert b_calls == ["reviewer", "worker", "reviewer"]

    if mutation == "local_legacy_rejection_control":
        meta_path = b_dir / "run.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["review_rejection_history"][-1].pop("reviewed_attempt_ordinal", None)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    daemon, units = _daemon_for(root, config_path, config, monkeypatch)
    preview = daemon.service.run_status("review-b")
    assert preview.evidence["can_resume"] is True, (
        f"{mutation}: a valid local repair binding was not admitted"
    )
    continuation = daemon.service.resume(
        "review-b", caller_scope="project:one", idempotency_key="v10-control"
    )
    replay = daemon.service.resume(
        "review-b", caller_scope="project:one", idempotency_key="v10-control"
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    prepared, resume_context = _worker_prepared(
        record, manifest, root, config_path, config
    )
    assert prepared.start_step == "review"
    assert prepared.max_turns == 40
    assert resume_context is not None
    assert resume_context.failed_pending_review_step == "review"

    # The actual successor's first provider is the reviewer naming the exact
    # local worker receipt with its repair overlay active plan.
    calls: list[str] = []
    selected: list[Path] = []

    def c_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        calls.append("worker" if "Implement " in prompt else "reviewer")
        line = next(
            x for x in prompt.splitlines() if "path (read this exact file):" in x
        )
        selected.append(
            Path(line.split(": ", 1)[1].split(". This is the read location", 1)[0])
        )
        return subprocess.CompletedProcess(argv, 3, "", "expected reviewer failure\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root, plan_path=prepared.plan_path,
                max_turns=prepared.max_turns, start_step=prepared.start_step,
                reserved_run_id=prepared.reserved_run_id,
                idempotency_key=prepared.idempotency_key,
                caller_scope=prepared.caller_scope,
                team_explicit=prepared.team_explicit,
                max_turns_explicit=prepared.max_turns_explicit,
                start_step_explicit=prepared.start_step_explicit,
            ),
            config, "live", config_dir=root, snapshot_config=False,
            runner=c_runner, resume=resume_context,
            allow_existing_launch_manifest=True,
        )
    except WorkflowError as exc:
        assert exc.summary.startswith("harness 'codex' exited with code 3\n")
    else:  # pragma: no cover - C must fail at the reviewer
        raise AssertionError("C did not fail at review")
    assert calls == ["reviewer"]
    expected_receipt = b_dir / "turns" / "turn-002" / "result.json"
    assert selected == [expected_receipt]
    receipt = json.loads(expected_receipt.read_text(encoding="utf-8"))
    assert receipt["status"] == "completed"
    assert receipt["returncode"] == 0
    overlay = json.loads(
        (b_dir / "run.json").read_text(encoding="utf-8")
    )["review_rejection_history"][-1]["repair_plan_path"]
    assert receipt["active_plan_path"] == str(root / overlay)


def test_daemon_resume_failed_pending_review_debug_file_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #79 v09: a valid resume is independent of /tmp/dbg.txt writability.

    v08 left an unconditional debug-file write in ``_reconstruct_resume_context``
    that raised ``PermissionError`` for every resume route when ``/tmp/dbg.txt``
    was unwritable. With that file's open denied, the same valid review-first
    bootstrap must still succeed at the review step.
    """
    import builtins

    import aflow.cli as cli_module

    root, plan_path, config_path, config, a_dir = _progression_source(tmp_path / "repo")
    real_open = builtins.open
    denied: list[str] = []

    def denying_open(file: object, *args: object, **kwargs: object) -> object:
        if str(file) == "/tmp/dbg.txt":
            denied.append(str(file))
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(cli_module, "open", denying_open, raising=False)
    boot = _bootstrap_cli(root, config_path, config, a_dir.name)
    assert boot.start_step == "review"
    assert boot.resume_context.failed_pending_review_step == "review"
    assert boot.resume_context.active_implementation_scope is not None
    assert boot.resume_context.active_implementation_scope.awaiting_review is True


@pytest.mark.parametrize("keep_runs", [20, 1])
def test_daemon_resume_failed_pending_review_repeated_failure_selects_worker_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_runs: int
) -> None:
    """Issue #79 v06: repeated failed-review resumes select the true worker receipt.

    Source A runs the worker for checkpoint 1; the reviewer exits 3.  Resume A
    into B (review-first); B's reviewer also exits 3.  C is admitted through the
    managed preview/resume/manifest/worker-preparation path from B with same-key
    idempotency, then runs in-process: its first reviewer must name A's worker
    receipt (``step_role == 'worker'``), and on approval ordinary
    remaining-checkpoint progression completes.  A's and B's source artifacts
    must remain available and byte-identical under both retention settings.
    """
    def failing_reviewer_runner(
        argv: list[str], selected: list[Path], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        assert "Implement " not in prompt  # review first, no worker
        for line in prompt.splitlines():
            if "path (read this exact file):" in line:
                selected.append(
                    Path(line.split(": ", 1)[1].split(". This is the read location", 1)[0])
                )
        return subprocess.CompletedProcess(argv, 3, "", "reviewer fails again\n")

    # --- Managed half: A -> B (reviewer fails), then the managed
    # preview/resume/manifest/worker-preparation path from B.
    m_root, m_plan, m_config_path, m_config, m_a = _progression_source(tmp_path / "managed")
    m_boot = _bootstrap_cli(m_root, m_config_path, m_config, m_a.name)
    assert m_boot.start_step == "review"
    m_selected: list[Path] = []
    try:
        run_workflow(
            ControllerConfig(
                repo_root=m_root, plan_path=m_plan, max_turns=40,
                start_step=m_boot.start_step, reserved_run_id="retry-one",
                caller_scope="project:one", keep_runs=keep_runs,
            ),
            m_config, "live", config_dir=m_root, snapshot_config=False,
            runner=lambda argv, **kw: failing_reviewer_runner(argv, m_selected, **kw),
            resume=m_boot.resume_context,
        )
    except WorkflowError:
        pass
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B reviewer did not fail")
    # B's first reviewer selected A's worker receipt, not B's reviewer receipt.
    assert m_selected[0] == (m_a / "turns" / "turn-001" / "result.json")

    daemon, units = _daemon_for(m_root, m_config_path, m_config, monkeypatch)
    preview = daemon.service.run_status("retry-one")
    assert preview.evidence["can_resume"] is True
    continuation = daemon.service.resume(
        "retry-one", caller_scope="project:one", idempotency_key="repeated-worker-key"
    )
    replay = daemon.service.resume(
        "retry-one", caller_scope="project:one", idempotency_key="repeated-worker-key"
    )
    assert continuation.created is True
    assert replay.created is False
    assert replay.run_id == continuation.run_id
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    assert record["prepared"]["start_step"] == "review"
    assert record["prepared"]["max_turns"] == 40
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None and manifest.start_step == "review"
    prepared, resume_context = _worker_prepared(record, manifest, m_root, m_config_path, m_config)
    assert prepared.start_step == "review"
    assert resume_context is not None
    assert resume_context.failed_pending_review_step == "review"
    assert resume_context.active_implementation_scope is not None
    assert resume_context.active_implementation_scope.awaiting_review is True

    # --- Runtime half: a separate source, A -> B (reviewer fails) -> C approves and
    # ordinary remaining-checkpoint progression completes.
    r_root, r_plan, r_config_path, r_config, r_a = _progression_source(tmp_path / "runtime")
    original_worker_bytes = (r_a / "turns" / "turn-001" / "result.json").read_bytes()
    a_artifacts = _artifact_set(r_a)
    r_boot = _bootstrap_cli(r_root, r_config_path, r_config, r_a.name)
    assert r_boot.resume_context.interrupted_step_name == "review"
    b_selected: list[Path] = []
    try:
        run_workflow(
            ControllerConfig(
                repo_root=r_root, plan_path=r_plan, max_turns=40,
                start_step=r_boot.start_step, reserved_run_id="retry-one",
                caller_scope="project:one", keep_runs=keep_runs,
            ),
            r_config, "live", config_dir=r_root, snapshot_config=False,
            runner=lambda argv, **kw: failing_reviewer_runner(argv, b_selected, **kw),
            resume=r_boot.resume_context,
        )
    except WorkflowError as exc:
        b_dir = exc.run_dir
    else:  # pragma: no cover - the producer must fail at the reviewer
        raise AssertionError("B reviewer did not fail")
    assert b_selected[0] == (r_a / "turns" / "turn-001" / "result.json")
    assert b_selected[0].read_bytes() == original_worker_bytes
    b_artifacts = _artifact_set(b_dir)

    c_boot = _bootstrap_cli(r_root, r_config_path, r_config, b_dir.name)
    assert c_boot.start_step == "review"
    c_calls: list[str] = []
    c_selected: list[Path] = []

    def c_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        c_calls.append("worker" if "Implement " in prompt else "reviewer")
        if c_calls[-1] == "reviewer":
            for line in prompt.splitlines():
                if "path (read this exact file):" in line:
                    c_selected.append(
                        Path(line.split(": ", 1)[1].split(". This is the read location", 1)[0])
                    )
        if c_calls[-1] == "worker":
            active = Path(prompt.split("Implement ", 1)[1].rstrip("."))
            active.write_text(
                active.read_text(encoding="utf-8").replace("[ ]", "[x]"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        return subprocess.CompletedProcess(argv, 0, "approved\n", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=r_root, plan_path=r_plan, max_turns=40,
            start_step=c_boot.start_step, reserved_run_id="retry-two",
            keep_runs=keep_runs,
        ),
        r_config, "live", config_dir=r_root, snapshot_config=False,
        runner=c_runner, resume=c_boot.resume_context,
    )
    assert result.status == "completed"
    assert c_calls[0] == "reviewer"
    assert c_calls == ["reviewer", "worker", "reviewer"]

    # Both B's and C's first reviewers named A's worker receipt with exact bytes.
    assert c_selected[0] == (r_a / "turns" / "turn-001" / "result.json")
    assert c_selected[0].read_bytes() == original_worker_bytes

    # A's and B's artifacts remain byte-identical under both retention settings.
    for rel, data in a_artifacts.items():
        if rel == "events.jsonl":
            continue
        assert (r_a / rel).read_bytes() == data, f"A artifact {rel} changed"
    for rel, data in b_artifacts.items():
        if rel == "events.jsonl":
            continue
        assert (b_dir / rel).read_bytes() == data, f"B artifact {rel} changed"
    assert (r_a / "turns" / "turn-001" / "result.json").is_file()


def test_daemon_resume_failed_pending_review_repair_worker_precedence(
    tmp_path: Path
) -> None:
    """Issue #79 v05: a newer same-scope repair worker takes precedence.

    Source A runs the original worker (turn 1), the reviewer rejects, a
    repair worker runs (turn 3), and the reviewer fails again (turn 4).
    Resuming from A must select the repair worker's receipt (turn 3), not
    the original worker's receipt (turn 1).
    """
    import aflow.cli as cli_module

    root = tmp_path / "repo"
    root.mkdir()
    plan_path = root / "plan.md"
    plan_path.write_text(
        "# Plan\n\n"
        "### [ ] Checkpoint 1: First\n- [ ] step\n"
        "### [ ] Checkpoint 2: Next\n- [ ] next step\n",
        encoding="utf-8",
    )
    config_path = root / "aflow.toml"
    config_path.write_text("# repair precedence fixture\n", encoding="utf-8")
    workflow_config = WorkflowUserConfig(
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
                        go=(
                            GoTransition(to="implement", when="NEW_PLAN_EXISTS"),
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement"),
                        ),
                    ),
                },
                first_step="implement",
            )
        },
        prompts={
            "implement": "Implement {ACTIVE_PLAN_PATH}.",
            "review": "Review; repair at {NEW_PLAN_PATH}.",
        },
    )

    call_count = 0

    def source_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        prompt = str(kwargs["input"])
        if "Implement" in prompt:
            # First worker: complete checkpoint 1.
            if call_count == 1:
                plan_path.write_text(
                    "# Plan\n"
                    "### [x] Checkpoint 1: First\n"
                    "- [x] step\n"
                    "### [ ] Checkpoint 2: Next\n- [ ] next step\n",
                    encoding="utf-8",
                )
            # Repair worker: also operates on the active plan.
            return subprocess.CompletedProcess(argv, 0, "worker output\n", "")
        # Reviewer: first call rejects (writes repair plan), second fails.
        if call_count == 2:
            overlay = Path(prompt.split("repair at ", 1)[1].split()[0].rstrip("."))
            overlay.write_text("# Repair\n\n- [ ] fix\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "reject\n", "")
        return subprocess.CompletedProcess(argv, 3, "", "reviewer failed\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root,
                plan_path=plan_path,
                max_turns=40,
                reserved_run_id="repair-source",
                idempotency_key="repair-key",
                caller_scope="project:one",
            ),
            workflow_config,
            "live",
            config_dir=root,
            snapshot_config=False,
            runner=source_runner,
        )
    except WorkflowError as exc:
        source_dir = exc.run_dir
    else:
        raise AssertionError("source did not fail at the reviewer")

    # Verify the source has two worker attempts: turn 1 (original) and
    # turn 3 (repair).
    source_meta = json.loads(
        (source_dir / "run.json").read_text(encoding="utf-8")
    )
    scope_id = source_meta["active_implementation_scope"]["scope_id"]
    attempts = source_meta["implementation_attempts"][scope_id]
    worker_attempts = [a for a in attempts if a["role"] == "worker"]
    assert len(worker_attempts) == 2
    assert worker_attempts[0]["turn_number"] == 1
    assert worker_attempts[1]["turn_number"] == 3

    # Resume from the source; the prompt must reference the repair worker's
    # receipt (turn 3), not the original worker's receipt (turn 1).
    bootstrap = cli_module._bootstrap_resume_invocation(
        repo_root=root,
        config_path=str(config_path),
        config_path_is_explicit=True,
        workflow_config=workflow_config,
        requested_run_id="repair-source",
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
    )
    assert bootstrap.start_step == "review"

    selected_path: list[Path] = []

    def successor_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = str(kwargs["input"])
        for line in prompt.splitlines():
            if "path (read this exact file):" in line:
                selected_path.append(
                    Path(line.split(": ", 1)[1]
                         .split(". This is the read location", 1)[0])
                )
        return subprocess.CompletedProcess(argv, 3, "", "reviewer fails\n")

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root,
                plan_path=plan_path,
                max_turns=40,
                start_step=bootstrap.start_step,
                reserved_run_id="repair-successor",
                keep_runs=20,
            ),
            workflow_config,
            "live",
            config_dir=root,
            snapshot_config=False,
            runner=successor_runner,
            resume=bootstrap.resume_context,
        )
    except WorkflowError:
        pass

    assert len(selected_path) == 1
    selected = selected_path[0]
    assert selected.is_file()
    result = json.loads(selected.read_text(encoding="utf-8"))
    assert result["step_role"] == "worker"
    assert result["turn_number"] == 3, (
        f"expected repair worker turn 3, got {result['turn_number']}"
    )


def test_daemon_resume_failed_pending_review_missing_lineage_refuses(
    tmp_path: Path
) -> None:
    """Issue #79 v05: missing worker lineage refuses at admission.

    Source A runs the worker and the reviewer fails.  Resume A into B
    (reviewer fails again).  Delete A's run directory.  Resuming from B
    must refuse cleanly at bootstrap without allocating a successor or
    calling a provider.
    """
    import aflow.cli as cli_module

    root, plan_path, config_path, config, source_dir = _progression_source(
        tmp_path / "repo"
    )
    source_run_id = source_dir.name

    # Run B: resume from A, reviewer fails.
    bootstrap_b = cli_module._bootstrap_resume_invocation(
        repo_root=root,
        config_path=str(config_path),
        config_path_is_explicit=True,
        workflow_config=config,
        requested_run_id=source_run_id,
        workflow_arg=None,
        plan_file_arg=None,
        team_arg=None,
        start_step_arg=None,
        max_turns_arg=None,
        extra_instructions_arg=(),
        extra_instructions_provided=False,
    )
    assert bootstrap_b.start_step == "review"

    try:
        run_workflow(
            ControllerConfig(
                repo_root=root,
                plan_path=plan_path,
                max_turns=40,
                start_step=bootstrap_b.start_step,
                reserved_run_id="lineage-b",
                keep_runs=20,
            ),
            config,
            "live",
            config_dir=root,
            snapshot_config=False,
            runner=lambda argv, **kw: subprocess.CompletedProcess(
                argv, 3, "", "reviewer fails"
            ),
            resume=bootstrap_b.resume_context,
        )
    except WorkflowError:
        pass

    # Delete A's run directory to simulate keep_runs pruning.
    import shutil
    shutil.rmtree(source_dir)

    # Resuming from B must refuse cleanly: B's resumed_from_run_id points
    # to A, which no longer exists, so the worker receipt cannot be bound.
    with pytest.raises(ValueError, match="worker receipt cannot be bound"):
        cli_module._bootstrap_resume_invocation(
            repo_root=root,
            config_path=str(config_path),
            config_path_is_explicit=True,
            workflow_config=config,
            requested_run_id="lineage-b",
            workflow_arg=None,
            plan_file_arg=None,
            team_arg=None,
            start_step_arg=None,
            max_turns_arg=None,
            extra_instructions_arg=(),
            extra_instructions_provided=False,
        )


@pytest.mark.parametrize("source_phase", ("unit_started", "launch_started"))
@pytest.mark.parametrize("source_status,reconcile", (("running", True), ("failed", True), ("interrupted", True), ("running", False)))
def test_resume_creates_one_new_continuation_and_audits_the_source(tmp_path: Path, monkeypatch, source_phase: str, source_status: str, reconcile: bool) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = repo_root / "aflow.toml"
    config_path.write_text("")
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    plan_path = repo_root / "plan.md"
    plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    workflow_config = _workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: workflow_config)
    source_id = "source-run"
    create_launch_manifest(
        repo_root,
        LaunchManifest(
            run_id=source_id,
            project_root=str(repo_root),
            plan_path=str(plan_path),
            workflow_name="managed",
            max_turns=2,
            idempotency_key="source-key",
            caller_scope="project:one",
        ),
    )
    source_dir = repo_root / ".aflow" / "runs" / source_id
    source_dir.mkdir()
    source_dir.joinpath("run.json").write_text(
        '{"status":"' + source_status + '","workflow_name":"managed","team":null,"selected_start_step":null}'
    )
    write_launch_phase(repo_root, source_id, source_phase)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
        ),
        units=units,
    )
    daemon.start(persist_reconciliation=reconcile)
    bootstrap = SimpleNamespace(
        workflow_name="managed",
        plan_path=plan_path,
        max_turns=2,
        team=None,
        start_step=None,
        extra_instructions=(),
        resume_context=object(),
    )
    monkeypatch.setattr("aflow.cli._bootstrap_resume_invocation", lambda **kwargs: bootstrap)

    before = source_dir.joinpath("run.json").read_bytes()
    if source_phase == "launch_started" and not reconcile:
        # Exercise the admission guard before an activity projection has
        # converted a killed launch into needs_attention.
        repository = daemon.application.repository
        source = repository.get_run_status(source_id)
        assert not source.evidence["controller_terminal"]
        running_source = replace(source, status="running")

        def unreconciled_status(
            run_id: str, *, include_progress: bool = True
        ):
            assert run_id == source_id
            return (
                running_source
                if include_progress
                else replace(running_source, progress=None)
            )

        monkeypatch.setattr(repository, "get_run_status", unreconciled_status)
        assert unreconciled_status(source_id).progress == source.progress
        assert unreconciled_status(source_id, include_progress=False).progress is None
        with pytest.raises(DaemonError, match="must be reconciled"):
            daemon.service.resume(source_id, caller_scope="project:one", idempotency_key="resume-1")
        assert units.start_calls == []
        assert list(source_dir.parent.iterdir()) == [source_dir]
        assert source_dir.joinpath("run.json").read_bytes() == before
        return
    if source_status == "running":
        with pytest.raises(DaemonError, match="predecessor inactivity"):
            daemon.service.resume(
                source_id,
                caller_scope="project:one",
                idempotency_key="resume-1",
            )
        assert units.start_calls == []
        assert list(source_dir.parent.iterdir()) == [source_dir]
        assert source_dir.joinpath("run.json").read_bytes() == before
        return
    assert daemon.service.run_status(source_id).evidence["can_resume"] is True
    assert source_dir.joinpath("run.json").read_bytes() == before
    assert units.start_calls == []
    continuation = daemon.service.resume(source_id, caller_scope="project:one", idempotency_key="resume-1")
    replay = daemon.service.resume(source_id, caller_scope="project:one", idempotency_key="resume-1")

    assert continuation.run_id != source_id
    assert replay.run_id == continuation.run_id
    assert replay.created is False
    assert len(units.start_calls) == 1
    assert units.start_calls[0][0] == f"aflow-run-{continuation.run_id}.service"
    record = daemon.service._read_record(continuation.run_id)
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    worker_prepared, resume_context = _worker_prepared(
        record,
        manifest,
        repo_root,
        config_path,
        workflow_config,
    )
    assert worker_prepared.start_step == manifest.start_step == "implement"
    assert resume_context is bootstrap.resume_context
    source_events = read_events(source_dir)
    assert [event.event_type for event in source_events].count("resume_requested") == 1
    assert source_dir.joinpath("run.json").read_bytes() == before


def _resume_publication_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AflowDaemon, StartupRequest, str]:
    units = InMemoryUnitManager()
    daemon, request = _daemon_for_config(tmp_path, monkeypatch, units, _workflow_config())
    source_id = "resume-publication-source"
    create_launch_manifest(
        request.repo_root,
        LaunchManifest(
            run_id=source_id,
            project_root=str(request.repo_root),
            plan_path=str(request.plan_path),
            workflow_name="managed",
            max_turns=2,
            idempotency_key="source-key",
            caller_scope="project:one",
        ),
    )
    source_dir = request.repo_root / ".aflow" / "runs" / source_id
    source_dir.mkdir()
    source_dir.joinpath("run.json").write_text(
        '{"status":"failed","workflow_name":"managed",'
        '"team":null,"selected_start_step":null}'
    )
    write_launch_phase(request.repo_root, source_id, "failed")
    bootstrap = SimpleNamespace(
        workflow_name="managed",
        plan_path=request.plan_path,
        max_turns=2,
        team=None,
        start_step="implement",
        extra_instructions=(),
        workflow_config=_workflow_config(),
        resume_context=object(),
    )
    monkeypatch.setattr(
        "aflow.cli._bootstrap_resume_invocation",
        lambda **_kwargs: bootstrap,
    )
    return daemon, request, source_id


def test_capacity_rejections_do_not_retain_transient_resume_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aflow.daemon import DaemonStartupError
    from aflow.project_admission import ProjectAdmission
    from aflow.project_settings import ProjectSettings, ProjectSettingsService

    daemon, request, source_id = _resume_publication_fixture(tmp_path, monkeypatch)
    settings = ProjectSettingsService(request.repo_root)
    settings.update(
        ProjectSettings(max_concurrent_implementations=1),
        expected_revision=settings.read().revision,
    )
    admission = ProjectAdmission(request.repo_root)
    occupied = admission.acquire("occupied-resume", idempotency_key="occupied-resume")
    attempted = iter(("rejected-resume-one", "rejected-resume-two", "admitted-resume"))
    monkeypatch.setattr("aflow.daemon.reserve_run_id", lambda _root: next(attempted))

    for key, run_id in (("one", "rejected-resume-one"), ("two", "rejected-resume-two")):
        with pytest.raises(DaemonStartupError) as raised:
            daemon.service.resume(
                source_id,
                caller_scope="project:one",
                idempotency_key=key,
                extra_instructions=("private-resume-guidance",),
            )
        assert raised.value.code == "project_capacity_reached"
        assert run_id not in daemon.service._transient_extra_instructions
        assert admission.reservation(run_id) is None
        assert admission.snapshot().occupied_count == 1

    admission.release(occupied.run_id, occupied.nonce)
    monkeypatch.setattr(
        "aflow.cli._bootstrap_resume_invocation",
        lambda **kwargs: SimpleNamespace(
            workflow_name="managed",
            plan_path=request.plan_path,
            max_turns=2,
            team=None,
            start_step="implement",
            extra_instructions=kwargs["extra_instructions_arg"],
            workflow_config=_workflow_config(),
            resume_context=object(),
        ),
    )
    admitted = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="admitted",
        extra_instructions=("private-resume-guidance",),
    )
    assert admitted.status == "running"
    assert "--extra-instruction=private-resume-guidance" in daemon.application.units.start_calls[0][1]
    assert admitted.run_id not in daemon.service._transient_extra_instructions


def test_distinct_managed_resume_cannot_claim_the_same_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aflow.daemon import DaemonStartupError

    daemon, _request, source_id = _resume_publication_fixture(tmp_path, monkeypatch)
    attempted = iter(("first-successor", "second-successor"))
    monkeypatch.setattr("aflow.daemon.reserve_run_id", lambda _root: next(attempted))
    first = daemon.service.resume(
        source_id, caller_scope="project:one", idempotency_key="first-key"
    )
    assert first.status == "running"

    with pytest.raises(DaemonStartupError, match="unresolved successor") as rejected:
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="second-key",
            extra_instructions=("private-guidance",),
        )
    assert rejected.value.code == "project_admission_error"
    assert "second-successor" not in daemon.service._transient_extra_instructions
    assert daemon.service._admission.reservation("second-successor") is None
    assert daemon.service._admission.snapshot().occupied_count == 1
    assert len(daemon.application.units.start_calls) == 1

    replay = daemon.service.resume(
        source_id, caller_scope="project:one", idempotency_key="first-key"
    )
    assert replay.run_id == first.run_id
    assert len(daemon.application.units.start_calls) == 1


@pytest.mark.parametrize(
    "failure_kind", ["manifest", "successor_directory", "startup_record"]
)
def test_resume_releases_unbound_admission_on_publication_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    daemon, request, source_id = _resume_publication_fixture(tmp_path, monkeypatch)
    target_id = "resume-publication-target"
    monkeypatch.setattr("aflow.daemon.reserve_run_id", lambda _root: target_id)
    unsafe_path: Path | None = None

    if failure_kind == "manifest":
        def fail_manifest(*_args: object, **_kwargs: object) -> None:
            raise ValueError("synthetic manifest failure")

        monkeypatch.setattr("aflow.daemon.create_launch_manifest", fail_manifest)
    elif failure_kind == "successor_directory":
        unsafe_path = request.repo_root / "successor-file"
        unsafe_path.write_text("not a directory", encoding="utf-8")
        original_run_directory = daemon.application.repository.run_directory

        def run_directory(run_id: str) -> Path:
            if run_id == target_id:
                return unsafe_path
            return original_run_directory(run_id)

        monkeypatch.setattr(daemon.application.repository, "run_directory", run_directory)
    else:
        def fail_record(_record: object) -> None:
            raise DaemonError("synthetic startup record failure")

        monkeypatch.setattr(daemon.service, "_create_record", fail_record)

    with pytest.raises(DaemonError):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key=f"resume-{failure_kind}",
        )

    reservation = daemon.service._admission.reservation(target_id)
    assert reservation is not None
    assert reservation.state == "released"
    assert reservation.bound is False
    assert daemon.service._admission.snapshot().occupied_count == 0
    assert not (
        request.repo_root / ".aflow" / "launches" / f"{target_id}.json"
    ).exists()
    assert not (
        request.repo_root / ".aflow" / "launches" / f"{target_id}.state.json"
    ).exists()
    assert not (
        request.repo_root / ".aflow" / "runs" / target_id
    ).exists()
    if failure_kind == "successor_directory":
        assert unsafe_path is not None
        assert unsafe_path.read_text(encoding="utf-8") == "not a directory"


def test_resume_keeps_bound_admission_after_replay_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, request, source_id = _resume_publication_fixture(tmp_path, monkeypatch)
    target_id = "resume-bound-target"
    monkeypatch.setattr("aflow.daemon.reserve_run_id", lambda _root: target_id)

    def fail_replay(*_args: object, **_kwargs: object) -> None:
        raise DaemonError("synthetic post-bind replay failure")

    monkeypatch.setattr(daemon.service, "_recover_resume_record", fail_replay)

    with pytest.raises(DaemonError, match="post-bind replay failure"):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="resume-bound",
        )

    reservation = daemon.service._admission.reservation(target_id)
    assert reservation is not None
    assert reservation.bound is True
    assert reservation.state != "released"
    assert daemon.service._admission.snapshot().occupied_count == 1
    assert daemon.application.repository.get_launch_manifest(target_id) is not None
    assert daemon.service._read_record(target_id)["state"] == "prepared"


def test_daemon_resume_uses_relocated_live_config_after_legacy_snapshot_damage(
    tmp_path: Path,
) -> None:
    """A control-plane continuation resolves its team and profile from live TOML."""
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
        encoding="utf-8",
    )
    old_config, _ = _write_split_config(
        tmp_path / "old-config",
        _incident_aflow_config(current=False),
        _INCIDENT_WORKFLOWS,
    )
    current_config, _ = _write_split_config(
        tmp_path / "relocated-config",
        _incident_aflow_config(current=True),
        _INCIDENT_WORKFLOWS.replace('team = "base"', 'team = "MusparkGLM"'),
    )

    with pytest.raises(WorkflowError) as first_failure:
        run_workflow(
            ControllerConfig(
                repo_root=tmp_path,
                plan_path=plan_path,
                max_turns=2,
                start_step="work",
                idempotency_key="source-key",
                caller_scope="project:daemon-test",
                max_turns_explicit=True,
                start_step_explicit=True,
            ),
            load_workflow_config(old_config),
            "live",
            config_dir=old_config,
            working_dir=tmp_path,
            adapter=CodexAdapter(),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 1, "synthetic old worker failure", ""
            ),
        )

    source_run_dir = first_failure.value.run_dir
    assert source_run_dir is not None
    snapshot_dir = source_run_dir / "config"
    assert (snapshot_dir / "snapshot.json").is_file()
    assert "MusparkGLM" not in (snapshot_dir / "aflow.toml").read_text(
        encoding="utf-8"
    )
    (snapshot_dir / "snapshot.json").write_text("{malformed", encoding="utf-8")
    (source_run_dir / "overrides.toml").write_text(
        'team = "MusparkGLM"\n',
        encoding="utf-8",
    )
    source_metadata_before_resume = (source_run_dir / "run.json").read_bytes()
    source_plan_before_resume = plan_path.read_bytes()

    environment_file = tmp_path / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = tmp_path / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=tmp_path,
            config_path=current_config,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=units,
    )
    daemon.start()

    continuation = daemon.service.resume(
        source_run_dir.name,
        caller_scope="project:daemon-test",
        idempotency_key="resume-current",
    )

    assert continuation.status == "running"
    assert len(units.start_calls) == 1
    record = daemon.service._read_record(continuation.run_id)
    manifest = daemon.application.repository.get_launch_manifest(continuation.run_id)
    assert manifest is not None
    current_workflow_config = load_workflow_config(current_config)
    prepared, resume_context = _worker_prepared(
        record,
        manifest,
        tmp_path,
        current_config,
        current_workflow_config,
    )
    assert resume_context is not None
    assert prepared.config_path == current_config.resolve()
    assert prepared.plan_path == plan_path.resolve()
    assert prepared.team == manifest.team == "MusparkGLM"
    assert prepared.start_step == manifest.start_step == "work"
    selector = resolve_role_selector(
        "worker",
        prepared.team,
        current_workflow_config,
        step_path="workflow.live.steps.work",
        step_name=prepared.start_step,
    )
    assert selector == "codex.muspark"
    assert resolve_profile(
        selector,
        current_workflow_config,
        step_path="workflow.live.steps.work",
    ).model == "model-muspark"
    assert record["prepared"]["config_path"] == str(current_config.resolve())
    assert (source_run_dir / "run.json").read_bytes() == source_metadata_before_resume
    assert plan_path.read_bytes() == source_plan_before_resume


@pytest.mark.parametrize(
    ("submitted", "expected"),
    (
        (None, ("saved guidance",)),
        (("replacement guidance",), ("replacement guidance",)),
        ((), ()),
    ),
)
def test_resume_extra_instructions_inherit_replace_and_clear(
    tmp_path: Path,
    monkeypatch,
    submitted: tuple[str, ...] | None,
    expected: tuple[str, ...],
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon_for_config(
        tmp_path, monkeypatch, units, _workflow_config()
    )
    source_id = "source-run"
    create_launch_manifest(
        request.repo_root,
        LaunchManifest(
            run_id=source_id,
            project_root=str(request.repo_root),
            plan_path=str(request.plan_path),
            workflow_name="managed",
            max_turns=2,
            extra_instructions=("saved guidance",),
            idempotency_key="source-key",
            caller_scope="project:one",
        ),
    )
    source_dir = request.repo_root / ".aflow" / "runs" / source_id
    source_dir.mkdir()
    source_dir.joinpath("run.json").write_text(
        '{"status":"failed","workflow_name":"managed","team":null,'
        '"selected_start_step":null,"extra_instructions":["saved guidance"]}'
    )
    write_launch_phase(request.repo_root, source_id, "failed")
    before = source_dir.joinpath("run.json").read_bytes()
    bootstrap_calls: list[dict[str, object]] = []

    def bootstrap(**kwargs):
        bootstrap_calls.append(kwargs)
        provided = kwargs["extra_instructions_provided"]
        effective = (
            kwargs["extra_instructions_arg"] if provided else ("saved guidance",)
        )
        return SimpleNamespace(
            workflow_name="managed",
            repo_root=request.repo_root,
            plan_path=request.plan_path,
            config_path=request.config_path,
            max_turns=2,
            team=None,
            start_step="implement",
            extra_instructions=effective,
            workflow_config=_workflow_config(),
            resume_context=object(),
        )

    monkeypatch.setattr("aflow.cli._bootstrap_resume_invocation", bootstrap)
    result = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="resume-instructions",
        extra_instructions=submitted,
    )

    assert result.status == "running"
    successor_id = result.run_id
    record = daemon.service._read_record(successor_id)
    manifest = daemon.application.repository.get_launch_manifest(successor_id)
    assert manifest is not None
    worker_prepared, _ = _worker_prepared(
        record,
        manifest,
        request.repo_root,
        request.config_path,
        _workflow_config(),
        extra_instructions=submitted or (),
    )
    assert worker_prepared.extra_instructions == expected
    assert bootstrap_calls[0]["extra_instructions_provided"] == (submitted is not None)
    assert bootstrap_calls[0]["extra_instructions_arg"] == (submitted or ())
    argv = units.start_calls[0][1]
    if expected:
        assert f"--extra-instruction={expected[0]}" in argv
    else:
        assert not any(argument.startswith("--extra-instruction=") for argument in argv)
    assert source_dir.joinpath("run.json").read_bytes() == before
    assert "saved guidance" not in (request.repo_root / ".aflow" / "launches" / f"{successor_id}.json").read_text()


def test_resume_extra_instructions_reuse_conflicts_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    units = InMemoryUnitManager()
    daemon, request = _daemon_for_config(
        tmp_path, monkeypatch, units, _workflow_config()
    )
    source_id = "source-run"
    create_launch_manifest(
        request.repo_root,
        LaunchManifest(
            run_id=source_id,
            project_root=str(request.repo_root),
            plan_path=str(request.plan_path),
            workflow_name="managed",
            max_turns=2,
            idempotency_key="source-key",
            caller_scope="project:one",
        ),
    )
    source_dir = request.repo_root / ".aflow" / "runs" / source_id
    source_dir.mkdir()
    source_dir.joinpath("run.json").write_text(
        '{"status":"failed","workflow_name":"managed","team":null,'
        '"selected_start_step":null,"extra_instructions":[]}'
    )
    write_launch_phase(request.repo_root, source_id, "failed")
    monkeypatch.setattr(
        "aflow.cli._bootstrap_resume_invocation",
        lambda **kwargs: SimpleNamespace(
            workflow_name="managed",
            plan_path=request.plan_path,
            max_turns=2,
            team=None,
            start_step="implement",
            extra_instructions=kwargs["extra_instructions_arg"],
            workflow_config=_workflow_config(),
            resume_context=object(),
        ),
    )

    daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="resume-instructions",
        extra_instructions=("first guidance",),
    )
    before_source = source_dir.joinpath("run.json").read_bytes()
    with pytest.raises(DaemonIdempotencyConflict):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="resume-instructions",
            extra_instructions=("different guidance",),
        )
    assert len(units.start_calls) == 1
    assert source_dir.joinpath("run.json").read_bytes() == before_source


def test_daemon_rejects_legacy_resume_before_reserving_continuation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = repo_root / "aflow.toml"
    config_path.write_text("")
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    plan_path = repo_root / "plan.md"
    plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n")
    workflow_config = _workflow_config()
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda path: workflow_config)
    source_id = "legacy-source"
    create_launch_manifest(
        repo_root,
        LaunchManifest(
            run_id=source_id,
            project_root=str(repo_root),
            plan_path=str(plan_path),
            workflow_name="managed",
            max_turns=2,
            idempotency_key="source-key",
            caller_scope="project:one",
        ),
    )
    source_dir = repo_root / ".aflow" / "runs" / source_id
    source_dir.mkdir()
    source_dir.joinpath("run.json").write_text(
        '{"schema_version":1,"status":"running","workflow_name":"managed",'
        '"team":null,"selected_start_step":null}'
    )
    write_launch_phase(repo_root, source_id, "unit_started")
    units = InMemoryUnitManager()
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
        ),
        units=units,
    )
    daemon.start()
    run_root = repo_root / ".aflow" / "runs"
    before = sorted(path.name for path in run_root.iterdir() if path.is_dir())
    launches_root = repo_root / ".aflow" / "launches"
    launches_before = sorted(path.name for path in launches_root.iterdir())

    # The managed inactivity gate now refuses this unobservable legacy
    # source before any reservation, replacing the later schema failure.
    with pytest.raises(
        ValueError,
        match="current managed inactivity is not proven for the exact source unit",
    ):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="resume-legacy",
        )

    assert units.start_calls == []
    assert sorted(path.name for path in run_root.iterdir() if path.is_dir()) == before
    assert sorted(path.name for path in launches_root.iterdir()) == launches_before


def test_legacy_run_status_remains_read_only_and_historical(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    run_dir = repo_root / ".aflow" / "runs" / "20260101T000000Z-abcdef12"
    run_dir.mkdir(parents=True)
    run_json = run_dir / "run.json"
    run_json.write_text(
        '{"schema_version":1,"status":"running","workflow_name":"old",'
        '"team":null,"turns_completed":3}\n'
    )
    before = run_json.read_bytes()

    status = RunRepository(repo_root).get_run_status("20260101T000000Z-abcdef12")

    assert status.ownership == "legacy"
    assert status.status == "needs_attention"
    assert status.reason == "legacy run has no control-plane launch manifest"
    assert run_json.read_bytes() == before


def test_worker_reloads_edited_defaults_without_a_snapshot_or_stale_step(
    tmp_path: Path,
    monkeypatch,
) -> None:
    units = InMemoryUnitManager()
    base = _workflow_config()
    initial_workflow = replace(
        base.workflows["managed"],
        team="blue",
    )
    initial = replace(
        base,
        aflow=AflowSection(max_turns=2),
        teams={"blue": TeamConfig(), "green": TeamConfig()},
        workflows={"managed": initial_workflow},
    )
    replacement_step = WorkflowStepConfig(
        role="worker",
        prompts=("p",),
        go=(GoTransition(to="END", when="DONE"),),
    )
    edited = replace(
        initial,
        aflow=AflowSection(max_turns=7),
        workflows={
            "managed": WorkflowConfig(
                steps={"replacement": replacement_step},
                first_step="replacement",
                team="green",
            )
        },
    )
    live = [initial]
    daemon, request = _daemon_for_config(tmp_path, monkeypatch, units, initial)
    monkeypatch.setattr("aflow.daemon.load_workflow_config", lambda _path: live[0])
    monkeypatch.setattr(
        "aflow.daemon.create_run_config_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(
            SnapshotError("diagnostic snapshot unavailable")
        ),
    )

    def prepare(current_request: StartupRequest) -> PreparedRun:
        workflow = current_request.workflow_config.workflows["managed"]
        return PreparedRun(
            workflow_name="managed",
            repo_root=current_request.repo_root,
            plan_path=current_request.plan_path,
            config_path=current_request.config_path,
            max_turns=current_request.workflow_config.aflow.max_turns,
            team=workflow.team,
            extra_instructions=(),
            start_step=workflow.first_step or "implement",
            team_explicit=False,
            max_turns_explicit=False,
            start_step_explicit=False,
        )

    monkeypatch.setattr("aflow.daemon.prepare_startup", prepare)
    start_request = replace(
        request,
        workflow_config=initial,
        max_turns=None,
        team=None,
    )
    started = daemon.service.start(
        start_request,
        caller_scope="project:one",
        idempotency_key="live-defaults",
    )

    live[0] = edited
    record = daemon.service._read_record(started.run_id)
    manifest = daemon.application.repository.get_launch_manifest(started.run_id)
    assert manifest is not None
    worker_prepared, resume_context = _worker_prepared(
        record,
        manifest,
        request.repo_root,
        request.config_path,
        edited,
    )

    assert resume_context is None
    assert worker_prepared.max_turns == 7
    assert worker_prepared.team == "green"
    assert worker_prepared.start_step == "replacement"
    assert not (request.repo_root / ".aflow" / "runs" / started.run_id / "config").exists()
    assert units.start_calls[0][1][
        units.start_calls[0][1].index("--config") + 1
    ] == str(request.config_path)

    def reject_prior_work(*_args, **_kwargs):
        raise PriorWorkStartupError(
            "prior_work_requires_recovery", "Previous work needs recovery."
        )

    monkeypatch.setattr("aflow.daemon.require_safe_fresh_worktree", reject_prior_work)
    with pytest.raises(PriorWorkStartupError) as blocked:
        _worker_prepared(
            record, manifest, request.repo_root, request.config_path, edited,
        )
    assert blocked.value.code == "prior_work_requires_recovery"

@pytest.mark.parametrize("budget", [None, 1])
@pytest.mark.parametrize("override_state", ["pending", "accepted"])
def test_managed_successor_budget_caps_rejected_review(tmp_path, monkeypatch, budget, override_state):
    """Exercise 48 real source turns, worker boot, and final-review rejection."""
    config = WorkflowUserConfig(
        aflow=AflowSection(max_turns=49),
        roles={"worker": "codex.worker", "reviewer": "codex.worker"},
        harnesses={"codex": WorkflowHarnessConfig(profiles={
            "worker": HarnessProfileConfig(model="worker"),
            "review": HarnessProfileConfig(model="independent-review"),
        })},
        workflows={"managed": WorkflowConfig(steps={
            "implement": WorkflowStepConfig(role="worker", prompts=("p",), go=(
                GoTransition(to="review", when="DONE"),
                GoTransition(to="implement"),
            )),
            "review": WorkflowStepConfig(role="reviewer", prompts=("p",), go=(
                GoTransition(to="implement", when="NEW_PLAN_EXISTS"),
                GoTransition(to="END"),
            )),
        }, first_step="implement")},
        prompts={"p": "Work on {ACTIVE_PLAN_PATH}. Create review repairs at {NEW_PLAN_PATH}."},
    )
    units = InMemoryUnitManager()
    daemon, request = _daemon_for_config(tmp_path, monkeypatch, units, config)
    root, plan = request.repo_root, request.plan_path
    plan.write_text("# Plan\n\n### [ ] Checkpoint 1: Work\n" + "- [ ] task\n" * 48)
    source_id = "forty-eight-turn-source"
    calls = []

    def source_worker(argv, **kwargs):
        calls.append(argv)
        text = plan.read_text().replace("- [ ]", "- [x]", 1)
        if len(calls) == 48:
            text = text.replace("### [ ]", "### [x]")
            compare_and_swap_overrides(root, source_id,
                RunControlRequest(expected_revision=0, owner_stop=True))
        plan.write_text(text)
        return subprocess.CompletedProcess(argv, 0, "done", "")

    source = run_workflow(
        ControllerConfig(repo_root=root, plan_path=plan, max_turns=49,
            reserved_run_id=source_id, idempotency_key="source",
            caller_scope="project:one"),
        config, "managed", config_dir=root, snapshot_config=False, runner=source_worker,
    )
    assert source.status == "owner_stopped"
    assert len(calls) == 48
    assert json.loads((source.run_dir / "run.json").read_text())["turns_completed"] == 48
    # Existing current-generation CAS still rejects stale revisions and budgets below 48.
    from aflow.control_plane import ControlConflictError, ControlValidationError
    with pytest.raises(ControlConflictError):
        daemon.application.controls.apply(source_id, RunControlRequest(expected_revision=0, max_turns=49))
    with pytest.raises(ControlValidationError):
        daemon.application.controls.apply(source_id, RunControlRequest(expected_revision=1, max_turns=1))
    compare_and_swap_overrides(root, source_id,
        RunControlRequest(expected_revision=1, max_turns=49, owner_stop=False,
            role_selectors={"reviewer": "codex.review"}))
    if override_state == "accepted":
        # Model an already-consumed source override with its original digest and bytes.
        from aflow.run_state import load_override_request
        from dataclasses import asdict
        from aflow.run_state import OverrideResult
        loaded = load_override_request(source.run_dir / "overrides.toml")
        payload = json.loads((source.run_dir / "run.json").read_text())
        payload["override_result"] = asdict(OverrideResult(
            status="accepted", digest=loaded.digest, message="accepted",
            source_text=loaded.source_text, max_turns=49,
            role_selectors={"reviewer": "codex.review"}, applied=True,
        ))
        payload["role_selectors"] = {"reviewer": "codex.review"}
        (source.run_dir / "run.json").write_text(json.dumps(payload))
    source_before = {p: p.read_bytes() for p in source.run_dir.rglob("*")
        if p.is_file() and p.name != "events.jsonl"}
    result = daemon.service.resume(source_id, caller_scope="project:one",
        idempotency_key="one-review", successor_max_turns=budget)
    replay = daemon.service.resume(source_id, caller_scope="project:one",
        idempotency_key="one-review", successor_max_turns=budget)
    assert replay.run_id == result.run_id
    assert len(units.start_calls) == 1
    with pytest.raises(DaemonIdempotencyConflict):
        daemon.service.resume(source_id, caller_scope="project:one",
            idempotency_key="one-review", successor_max_turns=2)
    record = daemon.service._read_record(result.run_id)
    manifest = daemon.application.repository.get_launch_manifest(result.run_id)
    # The daemon worker's managed inactivity authority is supplied by the
    # daemon; this direct preparation call carries an explicit fresh test
    # authority because the stopped source has no portable receipts.
    prepared, resume = _worker_prepared(
        record, manifest, root, request.config_path, config,
        managed_inactivity_check=lambda run_id: True,
    )
    assert prepared.max_turns == (budget or 49)
    assert prepared.max_turns_explicit is True
    assert resume.interrupted_step_name == "review"
    successor_calls = []

    def reject_review(argv, **kwargs):
        successor_calls.append(argv)
        if len(successor_calls) > 1:
            raise RuntimeError("second implementation dispatched")
        assert "independent-review" in argv
        turn = root / ".aflow/runs" / result.run_id / "turns/turn-001"
        prompt = (turn / "effective-prompt.txt").read_text()
        overlay = Path(prompt.split("Create review repairs at ", 1)[1].split(".md", 1)[0] + ".md")
        overlay.write_text("# Repair\n\n- [ ] fix review finding\n")
        return subprocess.CompletedProcess(argv, 0, "rejected", "")

    def execute():
        return run_workflow(
            ControllerConfig(repo_root=root, plan_path=plan,
                max_turns=prepared.max_turns, max_turns_explicit=prepared.max_turns_explicit,
                start_step=prepared.start_step, reserved_run_id=result.run_id,
                idempotency_key=prepared.idempotency_key, caller_scope=prepared.caller_scope),
            config, "managed", config_dir=root, snapshot_config=False,
            runner=reject_review, resume=resume, allow_existing_launch_manifest=True,
        )
    if budget is None:
        with pytest.raises(RuntimeError, match="second implementation dispatched"):
            execute()
        assert len(successor_calls) == 2
    else:
        with pytest.raises(WorkflowError, match="max turns limit of 1") as stopped:
            execute()
        assert len(successor_calls) == 1
        payload = json.loads((stopped.value.run_dir / "run.json").read_text())
        assert payload["turns_completed"] == 1
        assert payload["effective_max_turns"] == 1
        assert payload["role_selectors"]["reviewer"] == "codex.review"
    assert [str(p) for p, contents in source_before.items() if p.read_bytes() != contents] == []


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "1"])
def test_resume_rejects_invalid_successor_budget_before_reservation(tmp_path, monkeypatch, budget):
    daemon, request, source_id = _resume_publication_fixture(tmp_path, monkeypatch)
    before = sorted(str(p) for p in request.repo_root.rglob("*"))
    with pytest.raises(ValueError, match="positive integer"):
        daemon.service.resume(source_id, successor_max_turns=budget, idempotency_key="invalid")
    assert sorted(str(p) for p in request.repo_root.rglob("*")) == before
    assert daemon.application.units.start_calls == []

def test_successor_budget_survives_uncertain_publication(tmp_path, monkeypatch):
    daemon, request, source_id = _resume_publication_fixture(tmp_path, monkeypatch)
    bootstrap_calls = []
    def bootstrap(**kwargs):
        bootstrap_calls.append(kwargs["successor_max_turns"])
        return SimpleNamespace(
            workflow_name="managed", plan_path=request.plan_path,
            max_turns=kwargs["successor_max_turns"], max_turns_explicit=True,
            team=None, start_step="implement", extra_instructions=(),
            workflow_config=_workflow_config(), resume_context=None,
        )
    monkeypatch.setattr("aflow.cli._bootstrap_resume_invocation", bootstrap)
    recover = daemon.service._recover_resume_record
    def interrupted(*args, **kwargs):
        raise RuntimeError("response lost after publication")
    monkeypatch.setattr(daemon.service, "_recover_resume_record", interrupted)
    with pytest.raises(RuntimeError, match="response lost"):
        daemon.service.resume(source_id, caller_scope="project:one",
            idempotency_key="uncertain-budget", successor_max_turns=1)
    monkeypatch.setattr(daemon.service, "_recover_resume_record", recover)
    resumed = daemon.service.resume(source_id, caller_scope="project:one",
        idempotency_key="uncertain-budget", successor_max_turns=1)
    replay = daemon.service.resume(source_id, caller_scope="project:one",
        idempotency_key="uncertain-budget", successor_max_turns=1)
    assert resumed.run_id == replay.run_id
    assert len(daemon.application.units.start_calls) == 1
    record = daemon.service._read_record(resumed.run_id)
    manifest = daemon.application.repository.get_launch_manifest(resumed.run_id)
    prepared, _ = _worker_prepared(record, manifest, request.repo_root, request.config_path, _workflow_config())
    assert prepared.max_turns == 1
    assert bootstrap_calls == [1, 1, 1]

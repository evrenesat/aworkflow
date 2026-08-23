from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aflow.api.models import PreparedRun
from aflow.config import GoTransition, WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig
from aflow.control_plane import InMemoryUnitManager, LaunchManifest, create_launch_manifest, read_events, write_launch_phase
from aflow.control_plane.repository import RunRepository
from aflow.daemon import AflowDaemon, DaemonConfig, _worker_prepared


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


@pytest.mark.parametrize("source_phase", ("unit_started", "launch_started"))
def test_resume_creates_one_new_continuation_and_audits_the_source(tmp_path: Path, monkeypatch, source_phase: str) -> None:
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
        '{"status":"running","workflow_name":"managed","team":null,"selected_start_step":null}'
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
    daemon.start()
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
    assert source_dir.joinpath("run.json").read_text().startswith('{"status":"running"')


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

    with pytest.raises(ValueError, match="unsupported resume state schema"):
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
    assert status.status == "interrupted"
    assert status.reason == "legacy run has no control-plane launch manifest"
    assert run_json.read_bytes() == before

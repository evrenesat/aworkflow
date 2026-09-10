from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aflow.api.models import PreparedRun, StartupRequest
from aflow.config import (
    AflowSection,
    GoTransition,
    TeamConfig,
    WorkflowConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
    load_workflow_config,
)
from aflow.control_plane import InMemoryUnitManager, LaunchManifest, create_launch_manifest, read_events, write_launch_phase
from aflow.control_plane.repository import RunRepository
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
        monkeypatch.setattr(repository, "get_run_status", lambda _: replace(source, status="running"))
        with pytest.raises(DaemonError, match="must be reconciled"):
            daemon.service.resume(source_id, caller_scope="project:one", idempotency_key="resume-1")
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
        '{"status":"running","workflow_name":"managed","team":null,'
        '"selected_start_step":null,"extra_instructions":["saved guidance"]}'
    )
    write_launch_phase(request.repo_root, source_id, "unit_started")
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
        '{"status":"running","workflow_name":"managed","team":null,'
        '"selected_start_step":null,"extra_instructions":[]}'
    )
    write_launch_phase(request.repo_root, source_id, "unit_started")
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

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aflow.api.models import RecoveryRequest
from aflow.config import (
    AflowSection,
    GoTransition,
    HarnessProfileConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.control_plane import (
    InMemoryUnitManager,
    LaunchManifest,
    RunStatus,
    create_launch_manifest,
    read_events,
    read_recovery_intent,
    write_launch_phase,
)
from aflow.daemon import (
    AflowDaemon,
    DaemonConfig,
    DaemonError,
    DaemonIdempotencyConflict,
    DurableRecoveryRejection,
)
from aflow.run_state import ResumeContext


_DEFAULT_WORKER_EVIDENCE = object()


def _workflow_config() -> WorkflowUserConfig:
    return WorkflowUserConfig(
        aflow=AflowSection(max_turns=2),
        harnesses={
            "muse": WorkflowHarnessConfig(
                profiles={
                    "replacement": HarnessProfileConfig(
                        model="replacement-model"
                    )
                }
            )
        },
        roles={"worker": "muse.replacement"},
        teams={"base": TeamConfig(roles={"worker": "muse.replacement"})},
        workflows={
            "managed": WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="END", when="DONE"),),
                    )
                },
                first_step="implement",
                team="base",
            )
        },
        prompts={"p": "Work."},
    )


def _daemon_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    source_status: str = "failed",
    worker_evidence: dict[str, object] | None | object = _DEFAULT_WORKER_EVIDENCE,
    source_selector: str = "dsh.source",
    plan_exists: bool = True,
    context: ResumeContext | None = None,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    if plan_exists:
        plan_path.write_text(
            "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step\n",
            encoding="utf-8",
        )
    config_path = repo_root / "aflow.toml"
    config_path.write_text("", encoding="utf-8")
    environment_file = repo_root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n", encoding="utf-8")
    executable = repo_root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    workflow_config = _workflow_config()
    monkeypatch.setattr(
        "aflow.daemon.load_workflow_config",
        lambda _path: workflow_config,
    )
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=repo_root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="release-test",
            stop_timeout_seconds=0,
        ),
        units=InMemoryUnitManager(),
    )
    daemon.start()

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
    source_run_json = {
        "status": source_status,
        "workflow_name": "managed",
        "role_selectors": {"worker": source_selector},
    }
    (source_dir / "run.json").write_text(
        json.dumps(source_run_json, sort_keys=True),
        encoding="utf-8",
    )
    write_launch_phase(repo_root, source_id, "owner_stopped" if source_status == "owner_stopped" else "failed")
    if source_status == "owner_stopped":
        from aflow.control_plane import append_run_event

        append_run_event(source_dir, "owner_stopped", {"source": "test"})

    status = RunStatus(
        run_id=source_id,
        status=source_status,
        launch_phase="owner_stopped" if source_status == "owner_stopped" else "failed",
        ownership="control_plane",
        evidence={
            "controller_terminal": source_status != "owner_stopped",
            "worker": worker_evidence
            if worker_evidence is not _DEFAULT_WORKER_EVIDENCE
            else {"active": False, "exit_code": 17},
        },
    )
    repository = daemon.application.repository
    original_get_status = repository.get_run_status

    def get_status(
        run_id: str, *, include_progress: bool = True
    ) -> RunStatus:
        if run_id == source_id:
            return status if include_progress else replace(status, progress=None)
        return original_get_status(run_id, include_progress=include_progress)

    monkeypatch.setattr(repository, "get_run_status", get_status)
    assert get_status(source_id, include_progress=False).progress is None
    if context is None:
        context = ResumeContext(
            resumed_from_run_id=source_id,
            feature_branch=None,
            worktree_path=None,
            main_branch=None,
            setup=(),
            teardown=(),
            active_plan_path=plan_path,
        )
    bootstrap = SimpleNamespace(
        workflow_name="managed",
        repo_root=repo_root,
        plan_path=plan_path,
        config_path=config_path,
        max_turns=2,
        team="base",
        start_step="implement",
        extra_instructions=(),
        workflow_config=workflow_config,
        resume_context=context,
        run_json=source_run_json,
    )
    monkeypatch.setattr(
        daemon.service,
        "_resume_bootstrap",
        lambda *_args, **_kwargs: bootstrap,
    )
    return daemon, source_id, source_dir, plan_path, workflow_config, status


def _source_bytes(source_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
    }


def _recovery_request(selector: str = "muse.replacement") -> dict[str, str]:
    return {"mode": "durable_evidence", "worker_selector": selector}


def _runtime_payload(
    target_run_id: str = "source-run",
    *,
    operation_state: str = "in_flight",
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


def test_recovery_request_is_strict_and_canonical() -> None:
    request = RecoveryRequest.from_value(_recovery_request())
    assert request is not None
    assert request.to_dict() == _recovery_request()
    for invalid in (
        {"mode": "automatic", "worker_selector": "muse.replacement"},
        {"mode": "durable_evidence", "worker_selector": ""},
        {
            "mode": "durable_evidence",
            "worker_selector": "muse.replacement",
            "source_run_id": "source-run",
        },
    ):
        with pytest.raises(ValueError):
            RecoveryRequest.from_value(invalid)


@pytest.mark.parametrize("source_status", ("failed", "interrupted", "owner_stopped"))
def test_valid_recovery_persists_successor_provenance_without_source_mutation(
    tmp_path: Path, monkeypatch, source_status: str
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path, monkeypatch, source_status=source_status
    )
    before = _source_bytes(source_dir)
    launch_files_before = sorted(
        path.name for path in (daemon._config.repo_root / ".aflow" / "launches").glob("*.json")
    )
    target_calls: list[str] = []

    def fail_if_provider_is_consulted(*args, **kwargs):
        target_calls.append(f"{args}{kwargs}")
        raise AssertionError("recovery admission must not call a provider")

    monkeypatch.setattr("aflow.harnesses.get_adapter", fail_if_provider_is_consulted)
    result = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="recover-1",
        recovery=_recovery_request(),
    )

    assert result.status == "running"
    assert (
        daemon.application.repository.get_run_status(
            result.run_id, include_progress=False
        ).progress
        is None
    )
    assert target_calls == []
    record = daemon.service._read_record(result.run_id)
    assert record["recovery"] == _recovery_request()
    intent = read_recovery_intent(
        daemon.application.repository.run_directory(result.run_id)
    )
    assert intent.mode == "durable_evidence"
    assert intent.source_run_id == source_id
    assert intent.source_selector == "dsh.source"
    assert intent.target_selector == "muse.replacement"
    assert intent.source_session_context_transferred is False
    assert {item.path for item in intent.evidence} >= {
        f".aflow/runs/{source_id}/run.json",
        f".aflow/launches/{source_id}.json",
        "plan.md",
    }
    events = read_events(daemon.application.repository.run_directory(result.run_id))
    recovery_events = [
        event for event in events if event.event_type == "recovery_requested"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0].data["source_run_id"] == source_id
    assert recovery_events[0].data["target_selector"] == "muse.replacement"
    assert _source_bytes(source_dir) == before


def test_recovery_same_key_replays_and_changed_target_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path, monkeypatch
    )
    first = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="recover-1",
        recovery=_recovery_request(),
    )
    before = _source_bytes(source_dir)
    replay = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="recover-1",
        recovery=_recovery_request(),
    )
    assert replay.run_id == first.run_id
    assert replay.created is False
    assert len(daemon._units.start_calls) == 1
    with pytest.raises(DaemonIdempotencyConflict):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="recover-1",
            recovery=_recovery_request("muse.other"),
        )
    assert _source_bytes(source_dir) == before


@pytest.mark.parametrize(
    ("source_status", "worker_evidence"),
    (
        ("failed", {"active": True, "exit_code": 17}),
        ("failed", None),
        ("completed", {"active": False, "exit_code": 0}),
    ),
)
def test_recovery_rejects_active_unknown_or_completed_sources(
    tmp_path: Path,
    monkeypatch,
    source_status: str,
    worker_evidence: dict[str, object] | None,
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
        source_status=source_status,
        worker_evidence=worker_evidence,
    )
    before = _source_bytes(source_dir)
    with pytest.raises(DaemonError):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="recover-rejected",
            recovery=_recovery_request(),
        )
    assert daemon._units.start_calls == []
    assert _source_bytes(source_dir) == before
    assert not list((daemon._config.repo_root / ".aflow" / "start-requests").glob("*.json"))


def test_recovery_rejects_invalid_target_before_reserving_successor(
    tmp_path: Path, monkeypatch
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path, monkeypatch
    )
    before = _source_bytes(source_dir)
    with pytest.raises(DaemonError, match="target worker selector"):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="recover-invalid-target",
            recovery=_recovery_request("muse.missing"),
        )
    assert daemon._units.start_calls == []
    assert _source_bytes(source_dir) == before
    assert not list((daemon._config.repo_root / ".aflow" / "start-requests").glob("*.json"))


@pytest.mark.parametrize(
    "runtime",
    (
        {
            "schema_version": 1,
            "mode": "durable_evidence",
            "source_run_id": "older-source",
            "target_run_id": "source-run",
            "source_selector": "dsh.source",
            "target_selector": "muse.replacement",
            "intent_digest": "a" * 64,
            "brief_sha256": "b" * 64,
            "source_session_context_transferred": False,
            "consumed": False,
            "operation_state": "in_flight",
        },
        _runtime_payload(operation_state="pending"),
        {"operation_state": "in_flight"},
    ),
    ids=("in-flight", "pending", "malformed"),
)
def test_recovery_rejects_unresolved_source_operation_before_successor(
    tmp_path: Path,
    monkeypatch,
    runtime: dict[str, object],
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
    )
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = runtime
    (source_dir / "run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )
    before = _source_bytes(source_dir)
    launch_files_before = sorted(
        path.name
        for path in (daemon._config.repo_root / ".aflow" / "launches").glob("*.json")
    )

    with pytest.raises(DurableRecoveryRejection) as caught:
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key=f"recover-unresolved-{runtime.get('operation_state', 'malformed')}",
            recovery=_recovery_request(),
        )

    assert caught.value.code == "recovery_operation_unresolved"
    assert daemon._units.start_calls == []
    assert _source_bytes(source_dir) == before
    assert sorted(
        path.name for path in (daemon._config.repo_root / ".aflow" / "launches").glob("*.json")
    ) == launch_files_before
    assert not list((daemon._config.repo_root / ".aflow" / "start-requests").glob("*.json"))


def test_recovery_allows_consumed_source_operation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
    )
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = {
        "schema_version": 1,
        "mode": "durable_evidence",
        "source_run_id": "older-source",
        "target_run_id": source_id,
        "source_selector": "dsh.source",
        "target_selector": "muse.replacement",
        "intent_digest": "a" * 64,
        "brief_sha256": "b" * 64,
        "source_session_context_transferred": False,
        "consumed": True,
        "operation_state": "consumed",
    }
    (source_dir / "run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )

    result = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="recover-consumed-source",
        recovery=_recovery_request(),
    )

    assert result.created is True
    assert result.run_id != source_id
    assert len(daemon._units.start_calls) == 1


@pytest.mark.parametrize(
    "runtime",
    (
        _runtime_payload(),
        _runtime_payload(operation_state="pending"),
        {"operation_state": "in_flight"},
    ),
    ids=("in-flight", "pending", "malformed"),
)
def test_ordinary_resume_rejects_unresolved_operation_before_reservation(
    tmp_path: Path,
    monkeypatch,
    runtime: dict[str, object],
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
    )
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = runtime
    (source_dir / "run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )
    before = _source_bytes(source_dir)
    config_before = daemon._config.config_path.read_bytes()
    runs_root = daemon._config.repo_root / ".aflow" / "runs"
    run_dirs_before = sorted(path.name for path in runs_root.iterdir() if path.is_dir())
    launches_before = sorted(
        path.name for path in (daemon._config.repo_root / ".aflow" / "launches").glob("*.json")
    )
    provider_calls: list[object] = []

    def fail_if_provider_is_consulted(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("ordinary resume admission must not call a provider")

    monkeypatch.setattr("aflow.harnesses.get_adapter", fail_if_provider_is_consulted)
    with pytest.raises(DurableRecoveryRejection) as caught:
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key=f"ordinary-unresolved-{runtime.get('operation_state', 'malformed')}",
        )

    assert caught.value.code == "recovery_operation_unresolved"
    assert provider_calls == []
    assert daemon._units.start_calls == []
    assert _source_bytes(source_dir) == before
    assert daemon._config.config_path.read_bytes() == config_before
    assert sorted(path.name for path in runs_root.iterdir() if path.is_dir()) == run_dirs_before
    assert sorted(
        path.name for path in (daemon._config.repo_root / ".aflow" / "launches").glob("*.json")
    ) == launches_before


@pytest.mark.parametrize(
    "runtime",
    (
        _runtime_payload(),
        _runtime_payload(operation_state="pending"),
    ),
    ids=("in-flight", "pending"),
)
def test_ordinary_resume_replay_returns_started_result_without_rechecking_source(
    tmp_path: Path,
    monkeypatch,
    runtime: dict[str, object],
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
    )
    first = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="ordinary-replay",
    )
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = runtime
    source_dir.joinpath("run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )

    replay = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="ordinary-replay",
    )

    assert replay.run_id == first.run_id
    assert replay.created is False
    assert len(daemon._units.start_calls) == 1


def test_explicit_recovery_replay_keeps_typed_unresolved_admission_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
    )
    first = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="explicit-prelaunch-replay",
        recovery=_recovery_request(),
    )
    record = daemon.service._read_record(first.run_id)
    record["state"] = "prepared"
    daemon.service._write_record(record)
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = _runtime_payload()
    source_dir.joinpath("run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(DurableRecoveryRejection) as caught:
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="explicit-prelaunch-replay",
            recovery=_recovery_request(),
        )

    assert caught.value.code == "recovery_operation_unresolved"
    assert len(daemon._units.start_calls) == 1


def test_ordinary_resume_replay_rejects_pending_source_before_launch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
    )
    first = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="ordinary-pending-prelaunch",
    )
    record = daemon.service._read_record(first.run_id)
    record["state"] = "prepared"
    daemon.service._write_record(record)
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = _runtime_payload(
        operation_state="pending",
    )
    source_dir.joinpath("run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )
    before = _source_bytes(source_dir)
    config_before = daemon._config.config_path.read_bytes()

    with pytest.raises(DurableRecoveryRejection) as caught:
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="ordinary-pending-prelaunch",
        )

    assert caught.value.code == "recovery_operation_unresolved"
    assert len(daemon._units.start_calls) == 1
    assert _source_bytes(source_dir) == before
    assert daemon._config.config_path.read_bytes() == config_before


@pytest.mark.parametrize(
    "operation_state",
    ("in_flight", "pending"),
    ids=("in-flight", "pending"),
)
def test_ordinary_worker_preparation_rechecks_unresolved_source_operation(
    tmp_path: Path,
    monkeypatch,
    operation_state: str,
) -> None:
    daemon, source_id, source_dir, _plan_path, workflow_config, _status = _daemon_fixture(
        tmp_path,
        monkeypatch,
    )
    first = daemon.service.resume(
        source_id,
        caller_scope="project:one",
        idempotency_key="ordinary-prelaunch",
    )
    record = daemon.service._read_record(first.run_id)
    manifest = daemon.application.repository.get_launch_manifest(first.run_id)
    assert manifest is not None
    bootstrap = daemon.service._resume_bootstrap(source_id)
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = _runtime_payload(
        operation_state=operation_state,
    )
    source_dir.joinpath("run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "aflow.cli._bootstrap_resume_invocation",
        lambda **_kwargs: bootstrap,
    )

    with pytest.raises(DurableRecoveryRejection) as caught:
        from aflow.daemon import _worker_prepared

        _worker_prepared(
            record,
            manifest,
            daemon._config.repo_root,
            daemon._config.config_path,
            workflow_config,
        )

    assert caught.value.code == "recovery_operation_unresolved"
    assert len(daemon._units.start_calls) == 1


@pytest.mark.parametrize("missing", ("plan", "worktree"))
def test_recovery_rejects_missing_exact_plan_or_worktree_evidence(
    tmp_path: Path, monkeypatch, missing: str
) -> None:
    if missing == "plan":
        daemon, source_id, source_dir, plan_path, _config, _status = _daemon_fixture(
            tmp_path, monkeypatch, plan_exists=False
        )
        bootstrap_plan = plan_path
    else:
        missing_worktree = tmp_path / "not-a-worktree"
        context = ResumeContext(
            resumed_from_run_id="source-run",
            feature_branch="feature/missing",
            worktree_path=missing_worktree,
            main_branch="main",
            setup=("worktree", "branch"),
            teardown=("merge", "rm_worktree"),
            active_plan_path=tmp_path / "repo" / "plan.md",
        )
        daemon, source_id, source_dir, plan_path, _config, _status = _daemon_fixture(
            tmp_path, monkeypatch, context=context
        )
        bootstrap_plan = plan_path
    before = _source_bytes(source_dir)
    with pytest.raises(DaemonError):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key=f"recover-missing-{missing}",
            recovery=_recovery_request(),
        )
    assert daemon._units.start_calls == []
    assert _source_bytes(source_dir) == before
    assert not list((daemon._config.repo_root / ".aflow" / "start-requests").glob("*.json"))
    if missing == "plan":
        assert not bootstrap_plan.exists()


def test_recovery_does_not_infer_source_selector_from_current_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    daemon, source_id, source_dir, _plan_path, _config, status = _daemon_fixture(
        tmp_path, monkeypatch, source_selector=""
    )
    before = _source_bytes(source_dir)
    with pytest.raises(DaemonError, match="source worker selector"):
        daemon.service.resume(
            source_id,
            caller_scope="project:one",
            idempotency_key="recover-no-inference",
            recovery=_recovery_request(),
        )
    assert daemon._units.start_calls == []
    assert _source_bytes(source_dir) == before
    assert status.evidence["worker"]["active"] is False

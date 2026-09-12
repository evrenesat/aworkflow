"""Contract tests for the authenticated daemon-backed REST control plane."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from threading import Thread
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from aflow.api.models import PreparedRun, StartupQuestion, StartupQuestionKind
from aflow.control_plane import (
    CapabilitySet,
    ContextBundle,
    RunProgressCheckpoint,
    RunProgressChange,
    LaunchManifest,
    RunProgressDetail,
    RunProgressEvent,
    RunProgressSummary,
    RunProgressTruncation,
    RunControlRequest,
    RunStatus,
    StartRunResult,
    create_launch_manifest,
    write_launch_phase,
)
from aflow.control_plane.persistence import append_run_event
from aflow.control_plane.units import InMemoryUnitManager, UnitState
from aflow.daemon import AflowDaemon
from aflow_app_server.config import ServerConfig
from aflow_app_server.control_plane_service import ControlPlaneService
from aflow_app_server.main import app
from aflow_app_server.models import (
    CapabilityResponse,
    ContextResponse,
    RunControlPayload,
    RunProgressDetailResponse,
    RunProgressSummaryResponse,
    RunStatusResponse,
    ResumeRunPayload,
    StartRunResponse,
    WorktreePreflightResponse,
    canonical_contract_payloads,
)
from aflow_app_server.project_registry import (
    ProjectRegistry,
    ProjectRegistryError,
)
from aflow_app_server.global_config_service import GlobalConfigService
from aflow_app_server.plan_service import PlanService
from aflow.run_state import ResumeContext


TOKEN = "control-plane-test-token"
PROJECT_ID = "test-project"


@contextmanager
def live_server():
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            lifespan="off",
            log_level="error",
            access_log=False,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() >= deadline:
            server.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError("Timed out starting test server")
        time.sleep(0.01)

    socket = server.servers[0].sockets[0]
    try:
        yield f"http://127.0.0.1:{socket.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Timed out stopping test server")


def _write_workflow_config(path: Path) -> None:
    path.write_text(
        """
[aflow]
default_workflow = "managed"

[harness.codex.profiles.test]
model = "test"

[roles]
worker = "codex.test"

[prompts]
p = "Work."
""".strip()
        + "\n"
    )
    path.with_name("workflows.toml").write_text(
        """
[workflow.managed.steps.implement]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]
""".strip()
        + "\n"
    )


def _add_live_control_targets(path: Path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[harness.reasonix.profiles.new]\n"
            'model = "new-model"\n'
            "\n[teams.new.roles]\n"
            'worker = "reasonix.new"\n'
        )


@pytest.fixture
def control_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from aflow_app_server import main

    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    plan = root / "plans" / "todo" / "test-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Test\n\n### [ ] Checkpoint 1: Test\n- [ ] step\n")
    second_plan = root / "plans" / "todo" / "second-plan.md"
    second_plan.write_text("# Second\n\n### [ ] Checkpoint 1: Test\n- [ ] step\n")
    config_path = tmp_path / "global" / "aflow.toml"
    config_path.parent.mkdir(parents=True)
    _write_workflow_config(config_path)
    environment_file = root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    executable = root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    units = InMemoryUnitManager()

    registry = ProjectRegistry(tmp_path, tmp_path / "registry.json")
    registry.register(PROJECT_ID, "Test project", "project")
    control_service = ControlPlaneService(
        registry,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
        daemon_factory=lambda config: AflowDaemon(config, units=units),
        workflow_config_path=config_path,
    )
    control_service.start()
    config = ServerConfig(
        bind_host="127.0.0.1",
        bind_port=8765,
        auth_token=TOKEN,
        managed_projects_root=tmp_path,
        project_registry_path=registry.path,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
    )
    main._config = config
    main._project_registry = registry
    main._plan_service = PlanService(registry)
    main._control_plane_service = control_service
    main._global_config_service = GlobalConfigService(
        config_dir=tmp_path / "global",
        audit_path=tmp_path / "config_audit.jsonl",
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        yield client, root, units, monkeypatch
    finally:
        main._config = None
        main._project_registry = None
        main._plan_service = None
        main._control_plane_service = None
        main._global_config_service = None


def _prepared(request) -> PreparedRun:
    return PreparedRun(
        workflow_name="managed",
        repo_root=request.repo_root,
        plan_path=request.plan_path,
        config_path=request.config_path,
        max_turns=request.max_turns or 15,
        team=request.team,
        extra_instructions=request.extra_instructions,
        start_step=("implement" if request.start_step in {None, "1"} else request.start_step),
    )


def _recovery_payload(selector: str = "codex.test") -> dict[str, str]:
    return {"mode": "durable_evidence", "worker_selector": selector}


def _unresolved_recovery_runtime(
    target_run_id: str,
    *,
    malformed: bool = False,
    operation_state: str = "in_flight",
) -> dict[str, object]:
    if malformed:
        return {"operation_state": "in_flight"}
    return {
        "schema_version": 1,
        "mode": "durable_evidence",
        "source_run_id": "older-source",
        "target_run_id": target_run_id,
        "source_selector": "codex.test",
        "target_selector": "codex.test",
        "intent_digest": "a" * 64,
        "brief_sha256": "b" * 64,
        "source_session_context_transferred": False,
        "consumed": operation_state == "consumed",
        "operation_state": operation_state,
    }


_DEFAULT_RECOVERY_WORKER_EVIDENCE = object()


def _seed_recovery_source(
    control_service: ControlPlaneService,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_status: str = "failed",
    worker_evidence: dict[str, object] | None | object = _DEFAULT_RECOVERY_WORKER_EVIDENCE,
    source_id: str = "recovery-source",
) -> tuple[str, Path, object]:
    """Install one no-unit source with the exact evidence used by recovery admission."""
    daemon = control_service._project(PROJECT_ID).daemon
    plan_path = root / "plans" / "todo" / "test-plan.md"
    source_dir = root / ".aflow" / "runs" / source_id
    create_launch_manifest(
        root,
        LaunchManifest(
            run_id=source_id,
            project_root=str(root),
            plan_path=str(plan_path.resolve()),
            workflow_name="managed",
            max_turns=3,
            start_step="implement",
            idempotency_key=f"{source_id}-key",
            caller_scope=f"bearer:{PROJECT_ID}",
        ),
    )
    source_dir.mkdir(parents=True)
    source_payload = {
        "status": source_status,
        "workflow_name": "managed",
        "role_selectors": {"worker": "codex.test"},
    }
    (source_dir / "run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )
    phase = "owner_stopped" if source_status == "owner_stopped" else source_status
    write_launch_phase(root, source_id, phase)
    if source_status == "owner_stopped":
        append_run_event(source_dir, "owner_stopped", {"source": "test"})
    source_worker = (
        {"active": False, "exit_code": 17}
        if worker_evidence is _DEFAULT_RECOVERY_WORKER_EVIDENCE
        else worker_evidence
    )
    source = RunStatus(
        run_id=source_id,
        status=source_status,
        launch_phase=phase,
        ownership="control_plane",
        plan_path=str(plan_path.resolve()),
        workflow_name="managed",
        max_turns=3,
        selected_start_step="implement",
        unit_name=f"aflow-run-{source_id}.service",
        evidence={
            "controller_terminal": source_status != "owner_stopped",
            "worker": source_worker,
        },
    )
    repository = daemon.application.repository
    original_get_status = repository.get_run_status

    def get_status(
        run_id: str, *, include_progress: bool = True
    ) -> RunStatus:
        if run_id == source_id:
            return source if include_progress else replace(source, progress=None)
        return original_get_status(run_id, include_progress=include_progress)

    monkeypatch.setattr(repository, "get_run_status", get_status)
    assert get_status(source_id, include_progress=False).progress is None
    bootstrap = SimpleNamespace(
        workflow_name="managed",
        repo_root=root,
        plan_path=plan_path,
        config_path=daemon._config.config_path,
        max_turns=3,
        team=None,
        start_step="implement",
        extra_instructions=(),
        workflow_config=daemon.service._workflow_config,
        resume_context=ResumeContext(
            resumed_from_run_id=source_id,
            feature_branch=None,
            worktree_path=None,
            main_branch=None,
            setup=(),
            teardown=(),
            active_plan_path=plan_path,
        ),
        run_json=source_payload,
    )

    def resume_bootstrap(*_args, extra_instructions=(), **_kwargs):
        values = vars(bootstrap).copy()
        values["extra_instructions"] = tuple(extra_instructions)
        return SimpleNamespace(**values)

    monkeypatch.setattr(daemon.service, "_resume_bootstrap", resume_bootstrap)
    return source_id, source_dir, daemon


def _source_artifact_bytes(source_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source_dir).as_posix(): path.read_bytes()
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
    }


def test_detached_early_worker_failure_is_visible_without_service_restart(control_client):
    import shutil
    import sys
    from aflow.control_plane.persistent_units import PersistentUnitManager

    client, root, units, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    run_id = started["result"]["run_id"]
    unit = f"aflow-run-{run_id}.service"
    manager = PersistentUnitManager(executable=shutil.which("aflow"))
    manager.start(unit, (sys.executable, "-c", "import sys; print('rest-worker-marker token=PRIVATE-MARKER',file=sys.stderr); sys.exit(1)"), cwd=root)
    monkeypatch.setattr(units, "get", manager.get)
    path = root / ".aflow" / "runs" / run_id / "units" / "exit.json"
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert path.exists()
    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}"
    for _ in range(2):
        response = client.get(endpoint)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "failed"
        assert payload["worker_exit"]["exit_code"] == 1
        assert "rest-worker-marker" in payload["worker_exit"]["reason"]
        assert "PRIVATE-MARKER" not in response.text
        assert payload["evidence"]["can_resume"] is False
        assert payload["started_at"] is None
    assert client.get(endpoint + "/restart-options").json()["eligible"] is True
    detail = client.get(endpoint + "/context?level=full&full_scope=true")
    assert detail.status_code == 200 and "rest-worker-marker" in detail.text
    assert "PRIVATE-MARKER" not in detail.text
    assert not (path.parent.parent / "run.json").exists()


def _start_pending(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    extra_instructions: list[str] | None = None,
) -> dict[str, object]:
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup",
        lambda request: StartupQuestion(
            kind=StartupQuestionKind.PICK_STEP,
            message="Choose a step",
            choices=["implement"],
        ),
    )
    payload: dict[str, object] = {
        "plan_path": "plans/todo/test-plan.md",
        "workflow_name": "managed",
    }
    if extra_instructions is not None:
        payload["extra_instructions"] = extra_instructions
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "start-1"},
        json=payload,
    )
    assert response.status_code == 202
    return response.json()


def _answer_pending(
    client: TestClient, pending: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    monkeypatch.setattr("aflow.daemon.prepare_startup_with_answer", lambda _q, request, _a: _prepared(request))
    question = pending["startup_question"]
    assert isinstance(question, dict)
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/startup-answers/{question['question_id']}",
        headers={"Idempotency-Key": "answer-1"},
        json={"answer": "implement"},
    )
    assert response.status_code == 200
    return response.json()


def _commit_fixture_repository(root: Path) -> None:
    subprocess.run(("git", "add", "-A"), cwd=root, check=True, capture_output=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture baseline",
        ),
        cwd=root,
        check=True,
        capture_output=True,
    )


def _register_peer_project(
    registry: ProjectRegistry,
    managed_root: Path,
    project_id: str = "peer-project",
) -> Path:
    """Create one disposable Git root and add only its exact registry record."""
    root = managed_root / project_id
    root.mkdir()
    subprocess.run(("git", "init", "-q", str(root)), check=True, capture_output=True)
    registry.register(project_id, "Peer project", project_id)
    return root


def _issue35_original_plan(*, current: int = 4) -> str:
    sections: list[str] = []
    for index in range(1, 15):
        mark = "x" if index < current else " "
        sections.append(
            f"### [{mark}] Checkpoint {index}: Stage {index}\n"
            f"- [{mark}] complete stage {index}\n"
        )
    return "# Issue 35 progress fixture\n\n" + "\n".join(sections)


def _seed_issue35_progress_fixture(root: Path) -> dict[str, object]:
    """Create one registered-root run with a linked, unregistered repair worktree."""
    original = root / "plans" / "in-progress" / "issue35-original.md"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text(_issue35_original_plan(), encoding="utf-8")
    _commit_fixture_repository(root)

    execution = root.parent / f"{root.name}-issue35-execution"
    subprocess.run(
        ("git", "-C", str(root), "worktree", "add", "-q", "--detach", str(execution)),
        check=True,
        capture_output=True,
        text=True,
    )
    overlay = execution / "repair-overlay.md"
    overlay.write_text(
        "# Repair overlay\n\nThis fixture deliberately has no checkpoint headings.\n",
        encoding="utf-8",
    )

    run_id = "issue35-progress-repair"
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    scope = {
        "scope_id": "original::checkpoint-4",
        "original_plan_path": str(original),
        "checkpoint_index": 4,
        "checkpoint_name": "Checkpoint 4: Stage 4",
        "opened_turn_number": 3,
        "awaiting_review": True,
    }
    metadata = {
        "status": "running",
        "repo_root": str(root),
        "execution_repo_root": str(execution),
        "worktree_path": str(execution),
        "original_plan_path": str(original),
        "active_plan_path": str(overlay),
        "plan_path": str(original),
        "workflow_name": "managed",
        "current_step_name": "implement",
        "turns_completed": 3,
        "max_turns": 14,
        "run_started_at": "2026-09-10T00:00:00Z",
        "active_implementation_scope": scope,
    }
    run_json = run_dir / "run.json"
    run_json.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    finished_dir = run_dir / "turns" / "turn-003"
    finished_dir.mkdir(parents=True)
    (finished_dir / "result.json").write_text(
        json.dumps({
            "turn_number": 3,
            "step_name": "review",
            "status": "completed",
            "returncode": 0,
            "started_at": "2026-09-10T00:00:03Z",
            "finished_at": "2026-09-10T00:00:05Z",
        })
        + "\n",
        encoding="utf-8",
    )
    (finished_dir / "stdout.txt").write_text("review approved\n", encoding="utf-8")

    starting_dir = run_dir / "turns" / "turn-004"
    starting_dir.mkdir(parents=True)
    (starting_dir / "result.json").write_text(
        json.dumps({
            "turn_number": 4,
            "step_name": "implement",
            "status": "starting",
            "started_at": "2026-09-10T00:00:06Z",
        })
        + "\n",
        encoding="utf-8",
    )
    (starting_dir / "stdout.txt").write_text("", encoding="utf-8")
    append_run_event(run_dir, "scope_opened", {
        "scope_id": scope["scope_id"],
        "checkpoint_index": scope["checkpoint_index"],
        "checkpoint_name": scope["checkpoint_name"],
    })
    append_run_event(run_dir, "turn_started", {
        "turn_number": 4,
        "step_name": "implement",
    })

    missing_run_id = "issue35-progress-missing"
    missing_path = root / "plans" / "in-progress" / "missing-evidence.md"
    missing_run_dir = root / ".aflow" / "runs" / missing_run_id
    missing_run_dir.mkdir(parents=True, exist_ok=True)
    (missing_run_dir / "run.json").write_text(
        json.dumps({
            "status": "completed",
            "repo_root": str(root),
            "original_plan_path": str(missing_path),
            "active_plan_path": str(missing_path),
            "plan_path": str(missing_path),
            "workflow_name": "managed",
        })
        + "\n",
        encoding="utf-8",
    )

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "run_json": run_json,
        "original": original,
        "execution": execution,
        "overlay": overlay,
        "missing_run_id": missing_run_id,
    }


def _close_issue35_repair_scope(fixture: dict[str, object]) -> None:
    """Advance the disposable fixture after its repair scope is closed."""
    original = fixture["original"]
    run_json = fixture["run_json"]
    assert isinstance(original, Path)
    assert isinstance(run_json, Path)
    original.write_text(_issue35_original_plan(current=5), encoding="utf-8")
    metadata = json.loads(run_json.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    metadata.update({
        "active_implementation_scope": None,
        "active_plan_path": str(original),
        "plan_path": str(original),
    })
    run_json.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _preflight_request(
    *,
    limit: int = 200,
    offset: int = 0,
    dirty_worktree_confirmed: bool = False,
) -> dict[str, object]:
    return {
        "plan_path": "plans/todo/test-plan.md",
        "workflow_name": "managed",
        "limit": limit,
        "offset": offset,
        "dirty_worktree_confirmed": dirty_worktree_confirmed,
    }


def test_control_plane_preflight_is_paged_read_only_and_reports_relative_paths(
    control_client,
) -> None:
    client, root, units, _ = control_client
    _commit_fixture_repository(root)
    for name in ("dirty-a.txt", "dirty-b.txt", "dirty-c.txt"):
        (root / name).write_text(name, encoding="utf-8")
    before_runs = sorted(
        path.relative_to(root).as_posix()
        for path in (root / ".aflow" / "runs").rglob("*")
        if path.is_file()
    ) if (root / ".aflow" / "runs").exists() else []

    first = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight",
        json=_preflight_request(limit=2, dirty_worktree_confirmed=True),
    )
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["checkout_path"] == str(root.resolve())
    assert first_payload["dirty"] is True
    assert first_payload["requires_confirmation"] is True
    assert first_payload["total_items"] == 3
    assert first_payload["offset"] == 0
    assert first_payload["limit"] == 2
    assert first_payload["next_offset"] == 2
    assert [item["path"] for item in first_payload["items"]] == [
        "dirty-a.txt",
        "dirty-b.txt",
    ]
    assert all("content" not in item for item in first_payload["items"])
    assert units.start_calls == []

    second = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight",
        json=_preflight_request(limit=2, offset=2),
    )
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["total_items"] == 3
    assert second_payload["next_offset"] is None
    assert [item["path"] for item in second_payload["items"]] == ["dirty-c.txt"]
    after_runs = sorted(
        path.relative_to(root).as_posix()
        for path in (root / ".aflow" / "runs").rglob("*")
        if path.is_file()
    ) if (root / ".aflow" / "runs").exists() else []
    assert after_runs == before_runs

    rejected = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight",
        json=_preflight_request(limit=1_001),
    )
    assert rejected.status_code == 422

    merge_head = root / ".git" / "MERGE_HEAD"
    merge_head.write_text("synthetic-merge-marker\n", encoding="utf-8")
    try:
        conflict = client.post(
            f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight",
            json=_preflight_request(),
        )
        assert conflict.status_code == 200, conflict.text
        assert any(
            blocker == "in-progress Git operation (MERGE_HEAD exists)"
            for blocker in conflict.json()["blockers"]
        )

        refused = client.post(
            f"/api/control-plane/projects/{PROJECT_ID}/runs",
            headers={"Idempotency-Key": "conflict-refusal"},
            json={
                "plan_path": "plans/todo/test-plan.md",
                "workflow_name": "managed",
                "dirty_worktree_confirmed": True,
            },
        )
        assert refused.status_code == 422
        assert units.start_calls == []
    finally:
        merge_head.unlink()


def test_control_plane_start_rechecks_dirt_and_replays_acknowledgment_as_identity(
    control_client,
) -> None:
    client, root, units, _ = control_client
    _commit_fixture_repository(root)
    clean = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight",
        json=_preflight_request(),
    )
    assert clean.status_code == 200, clean.text
    assert clean.json()["dirty"] is False
    (root / "appeared-after-selection.txt").write_text("dirty", encoding="utf-8")

    unacknowledged = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "dirty-unacknowledged"},
        json={"plan_path": "plans/todo/test-plan.md", "workflow_name": "managed"},
    )
    assert unacknowledged.status_code == 202, unacknowledged.text
    question = unacknowledged.json()["startup_question"]
    assert question["kind"] == "confirm_worktree_dirty"
    assert units.start_calls == []

    declined = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/startup-answers/{question['question_id']}",
        headers={"Idempotency-Key": "dirty-declined"},
        json={"answer": False},
    )
    assert declined.status_code == 422
    assert units.start_calls == []

    acknowledged = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "dirty-acknowledged"},
        json={
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
            "dirty_worktree_confirmed": True,
        },
    )
    assert acknowledged.status_code == 201, acknowledged.text
    assert acknowledged.json()["result"]["status"] == "running"
    assert len(units.start_calls) == 1

    changed_replay = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "dirty-acknowledged"},
        json={
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
            "dirty_worktree_confirmed": False,
        },
    )
    assert changed_replay.status_code == 409
    assert changed_replay.json() == {"detail": {"code": "idempotency_conflict"}}
    assert len(units.start_calls) == 1


def test_unregister_refuses_to_invalidate_daemon_with_active_unit(control_client) -> None:
    from aflow_app_server import main

    client, _, _, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    _answer_pending(client, pending, monkeypatch)
    service = main._control_plane_service
    assert service is not None

    with pytest.raises(ProjectRegistryError, match="active workflow unit"):
        service.unregister(PROJECT_ID)

    assert service.projects()[0].project_id == PROJECT_ID


def tmp_path_global_pair(config):
    """The shared global pair location used by the fixture (config dir parent)."""
    return config.managed_projects_root / "global" / "aflow.toml"


def _fresh_control_service(root: Path, units: InMemoryUnitManager) -> ControlPlaneService:
    from aflow_app_server import main

    config = main._config
    assert config is not None
    registry = ProjectRegistry(config.managed_projects_root, config.project_registry_path)
    service = ControlPlaneService(
        registry,
        aflow_executable=config.aflow_executable,
        environment_file=config.environment_file,
        release_identity=config.release_identity,
        daemon_factory=lambda daemon_config: AflowDaemon(daemon_config, units=units),
        workflow_config_path=tmp_path_global_pair(config),
    )
    service.start()
    assert service._projects == {}
    assert registry.resolve(PROJECT_ID)[1] == root.resolve()
    return service


def test_unregister_uncached_project_still_observes_active_exact_unit(control_client) -> None:
    client, root, units, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    result = started["result"]
    assert isinstance(result, dict)
    run_id = str(result["run_id"])
    assert units.get(f"aflow-run-{run_id}.service").is_active  # type: ignore[union-attr]
    fresh = _fresh_control_service(root, units)

    with pytest.raises(ProjectRegistryError, match="active workflow unit"):
        fresh.unregister(PROJECT_ID)

    assert fresh.projects()[0].project_id == PROJECT_ID


def test_unregister_uncached_inactive_project_removes_only_requested_state(control_client) -> None:
    from aflow_app_server import main

    client, root, units, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    result = started["result"]
    assert isinstance(result, dict)
    run_id = str(result["run_id"])
    unit_name = f"aflow-run-{run_id}.service"
    units.units[unit_name] = UnitState(
        name=unit_name,
        active_state="inactive",
        sub_state="dead",
        result="success",
    )
    config = main._config
    assert config is not None
    second = root.parent / "second-project"
    subprocess.run(("git", "init", "-q", str(second)), check=True)
    registry = ProjectRegistry(config.managed_projects_root, config.project_registry_path)
    registry.register("second-project", "Second project", "second-project")
    fresh = _fresh_control_service(root, units)
    assert fresh.capabilities("second-project").workflows == ("managed",)

    assert fresh.unregister(PROJECT_ID) is True
    assert [project.project_id for project in fresh.projects()] == ["second-project"]
    assert set(fresh._projects) == {"second-project"}


def test_registry_project_editor_shape_renames_but_never_moves_root(control_client) -> None:
    from aflow_app_server import main

    client, root, _, _ = control_client
    config = main._config
    assert config is not None
    registry_path = config.project_registry_path
    before = json.loads(registry_path.read_text())

    renamed = client.patch(
        f"/api/projects/{PROJECT_ID}",
        json={"display_name": "Renamed project", "current_path": str(root.resolve())},
    )

    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "Renamed project"
    assert renamed.json()["current_path"] == str(root.resolve())
    after_rename = json.loads(registry_path.read_text())
    assert after_rename["projects"][0]["relative_root"] == "project"
    assert after_rename["projects"][0]["updated_at"] != before["projects"][0]["updated_at"]

    stable_bytes = registry_path.read_bytes()
    rejected = client.patch(
        f"/api/projects/{PROJECT_ID}",
        json={"display_name": "Must not persist", "current_path": str(root.parent / "other")},
    )

    assert rejected.status_code == 422
    assert rejected.json() == {"detail": {"code": "operation_rejected"}}
    assert registry_path.read_bytes() == stable_bytes

    alias_rejected = client.patch(
        f"/api/projects/{PROJECT_ID}",
        json={"alias": str(root)},
    )
    assert alias_rejected.status_code == 422
    assert alias_rejected.json() == {"detail": {"code": "operation_rejected"}}
    assert registry_path.read_bytes() == stable_bytes


def test_transport_models_match_canonical_control_plane_models() -> None:
    payloads = canonical_contract_payloads()
    assert set(payloads["capability"]) == set(CapabilitySet().to_dict())
    assert set(payloads["run"]) == set(RunStatus(run_id="sample", status="manifest_only").to_dict())
    assert set(payloads["start"]) == set(
        StartRunResult(run_id="sample", created=False, status="manifest_only").to_dict()
    )
    assert set(payloads["control"]) == set(RunControlRequest(expected_revision=0).to_dict())
    assert set(payloads["context"]) == set(ContextBundle(run_id="sample", level="lite", data={}).to_dict())
    assert set(CapabilityResponse.model_fields) == set(payloads["capability"])
    assert set(RunStatusResponse.model_fields) == set(payloads["run"])
    assert set(StartRunResponse.model_fields) == set(payloads["start"])
    assert set(RunControlPayload.model_fields) == set(payloads["control"])
    assert set(ResumeRunPayload.model_fields) == {"extra_instructions", "recovery"}
    assert set(ContextResponse.model_fields) == set(payloads["context"])
    assert set(WorktreePreflightResponse.model_fields) == set(payloads["preflight"])


def test_progress_transport_models_keep_optional_status_and_full_detail_shapes() -> None:
    summary_status = RunStatus(run_id="legacy", status="needs_attention")
    response = RunStatusResponse.from_canonical(summary_status)
    assert response.progress is None

    summary_response = RunProgressSummaryResponse.from_canonical(RunProgressSummary())
    detail_response = RunProgressDetailResponse.from_canonical(
        RunProgressDetail(
            checkpoint_states={"2": "approved"},
            checkpoints=(RunProgressCheckpoint("checkpoint-1", awaiting_review=True),),
            events=(RunProgressEvent("whole-plan", association="whole_plan"),),
            applied_changes=(RunProgressChange("change-1", generation_id="generation-1"),),
            truncation=RunProgressTruncation(
                response_limit_records=3,
                response_limit_checkpoints=2,
            ),
        )
    )
    assert summary_response.model_dump(mode="json")["availability"] == "unavailable"
    assert summary_response.model_dump(mode="json")["checkpoint_states"] == {}
    assert detail_response.model_dump(mode="json")["checkpoint_states"] == {"2": "approved"}
    assert detail_response.model_dump(mode="json")["checkpoints"][0]["awaiting_review"] is True
    assert detail_response.model_dump(mode="json")["events"][0]["association"] == "whole_plan"
    assert detail_response.model_dump(mode="json")["applied_changes"][0]["generation_id"] == "generation-1"
    assert detail_response.model_dump(mode="json")["truncation"]["response_limit_records"] == 3
    assert detail_response.model_dump(mode="json")["truncation"]["response_limit_checkpoints"] == 2


def test_openapi_documents_control_plane_operations_and_models() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    assert {
        "/api/executions",
        "/api/executions/{run_id}",
        "/api/executions/{run_id}/events",
    }.isdisjoint(paths)
    assert {
        "/ready",
        "/api/control-plane/capabilities",
        "/api/control-plane/projects",
        "/api/control-plane/projects/{project_id}/runs",
        "/api/control-plane/projects/{project_id}/runs/preflight",
        "/api/control-plane/projects/{project_id}/runs/{run_id}/events/stream",
        "/api/control-plane/projects/{project_id}/runs/{run_id}/control",
        "/api/control-plane/projects/{project_id}/runs/{run_id}/owner-stop",
        "/api/control-plane/projects/{project_id}/runs/{run_id}/resume",
        "/api/control-plane/projects/{project_id}/startup-answers/{question_id}",
    }.issubset(paths)
    assert {
        "CapabilityResponse",
        "RunStatusResponse",
        "StartRunResponse",
        "ResumeRunPayload",
        "RunControlPayload",
        "ContextResponse",
        "WorktreePreflightResponse",
    }.issubset(schema["components"]["schemas"])
    resume_schema = schema["components"]["schemas"]["ResumeRunPayload"]
    assert {"extra_instructions", "recovery"} == set(resume_schema["properties"])
    assert "durable-evidence" in resume_schema["properties"]["recovery"]["description"]


def test_run_list_uses_summary_only_without_context_requests(control_client, monkeypatch):
    from aflow_app_server import main

    client, root, _, _ = control_client
    fixture = _seed_issue35_progress_fixture(root)
    run_id = fixture["run_id"]
    assert isinstance(run_id, str)
    detail = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/context"
    )
    assert detail.status_code == 200, detail.text
    detail_progress = detail.json()["data"]["progress"]
    assert len(detail_progress["checkpoints"]) == 14

    project = main._control_plane_service._project(PROJECT_ID)
    context = project.daemon.application.context

    def unexpected_context_request(*_args, **_kwargs):
        raise AssertionError("run list must not request per-row context")

    monkeypatch.setattr(context, "get", unexpected_context_request)
    listed = client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs?history=all")

    assert listed.status_code == 200, listed.text
    payload = next(item for item in listed.json()["runs"] if item["run_id"] == run_id)
    progress = payload["progress"]
    assert "events" not in progress
    assert "checkpoints" not in progress
    assert progress["total_checkpoints"] == {"value": 14, "coverage": "complete"}
    assert detail_progress["total_checkpoints"] == progress["total_checkpoints"]


def test_run_list_final_projection_once(control_client, monkeypatch) -> None:
    from aflow_app_server import main

    client, root, _, _ = control_client
    legacy_id = "20260809T172123Z-abc12345"
    legacy_dir = root / ".aflow" / "runs" / legacy_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "run.json").write_text('{"status":"running"}\n')

    owned_id = "owned-projection"
    create_launch_manifest(
        root,
        LaunchManifest(
            run_id=owned_id,
            project_root=str(root.resolve()),
            plan_path=str((root / "plans" / "todo" / "test-plan.md").resolve()),
            workflow_name="managed",
            max_turns=5,
            idempotency_key="owned-projection-key",
            caller_scope="bearer:test-project",
        ),
    )
    owned_dir = root / ".aflow" / "runs" / owned_id
    owned_dir.mkdir(parents=True)
    (owned_dir / "run.json").write_text('{"status":"running"}\n')

    service = main._control_plane_service
    assert service is not None
    repository = service._project(PROJECT_ID).daemon.application.repository
    original_projection = repository._with_progress
    projections: list[RunStatus] = []

    def counted_projection(status, run_dir, metadata):
        projections.append(status)
        return original_projection(status, run_dir, metadata)

    monkeypatch.setattr(repository, "_with_progress", counted_projection)

    response = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs?history=all"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["run_id"] for item in payload["runs"]] == [legacy_id, owned_id]
    assert payload["next_cursor"] is None
    assert [item.run_id for item in projections] == [legacy_id, owned_id]
    assert len(projections) == len(payload["runs"]) == 2
    assert all(item["history_state"] == "visible" for item in payload["runs"])
    assert all(item["history_revision"] == 0 for item in payload["runs"])


def test_deprecated_execution_routes_are_not_registered() -> None:
    assert not any(
        getattr(route, "path", "").startswith("/api/executions") for route in app.routes
    )


def test_control_plane_reads_pending_start_and_idempotency(control_client) -> None:
    client, _, units, monkeypatch = control_client
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {
        "ready": True,
        "projects": [PROJECT_ID],
        "project_errors": {},
    }
    assert client.get("/api/control-plane/projects").json()["projects"][0]["project_id"] == PROJECT_ID
    assert client.get("/api/control-plane/capabilities").status_code == 200
    assert client.get(f"/api/control-plane/projects/{PROJECT_ID}/plans").status_code == 200
    assert client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs").json()["runs"] == []

    pending = _start_pending(client, monkeypatch)
    question = pending["startup_question"]
    assert isinstance(question, dict)
    assert question["run_id"]
    assert "unit_name" not in question
    assert units.start_calls == []

    pending_run = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{question['run_id']}"
    )
    assert pending_run.status_code == 200
    assert pending_run.json()["status"] == "awaiting_startup_answer"
    listed_pending = client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs").json()["runs"]
    assert len(listed_pending) == 1
    assert listed_pending[0]["status"] == "awaiting_startup_answer"
    replay = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "start-1"},
        json={"plan_path": "plans/todo/test-plan.md", "workflow_name": "managed"},
    )
    assert replay.status_code == 202
    assert replay.json() == pending
    conflict = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "start-1"},
        json={"plan_path": "plans/todo/second-plan.md", "workflow_name": "managed"},
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "idempotency_conflict"}}


def test_control_events_context_controls_owner_stop_and_resume(control_client) -> None:
    client, root, units, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    result = started["result"]
    assert isinstance(result, dict)
    run_id = result["run_id"]
    assert result["status"] == "running"
    assert len(units.start_calls) == 1

    control = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "control-1"},
        json={"expected_revision": 0, "max_turns": 3},
    )
    assert control.status_code == 200
    assert control.json()["revision"] == 1
    replay = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "control-1"},
        json={"expected_revision": 0, "max_turns": 3},
    )
    assert replay.status_code == 200
    assert replay.json() == control.json()
    mismatch = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "control-1"},
        json={"expected_revision": 1, "max_turns": 4},
    )
    assert mismatch.status_code == 409
    stale = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        json={"expected_revision": 0, "team": "other"},
    )
    assert stale.status_code == 409
    assert stale.json() == {"detail": {"code": "revision_conflict", "current_revision": 1}}

    boundary = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "control-boundary-stop-1"},
        json={"expected_revision": 1, "owner_stop": True},
    )
    assert boundary.status_code == 200, boundary.text
    assert boundary.json()["revision"] == 2
    assert boundary.json()["owner_stop"] is True
    assert boundary.json()["run"]["status"] in {"running", "launch_started"}
    assert units.stop_calls == []
    boundary_replay = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "control-boundary-stop-1"},
        json={"expected_revision": 1, "owner_stop": True},
    )
    assert boundary_replay.status_code == 200
    assert boundary_replay.json() == boundary.json()
    boundary_stale = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "control-boundary-stop-stale"},
        json={"expected_revision": 1, "owner_stop": True},
    )
    assert boundary_stale.status_code == 409
    assert boundary_stale.json() == {"detail": {"code": "revision_conflict", "current_revision": 2}}
    fresh_boundary = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}"
    )
    assert fresh_boundary.status_code == 200
    fresh_boundary_payload = fresh_boundary.json()
    assert fresh_boundary_payload["status"] in {"running", "launch_started"}
    assert fresh_boundary_payload["activity"] == "active"
    assert fresh_boundary_payload["evidence"]["overrides"] == {
        "state": "pending",
        "revision": 2,
        "max_turns": 3,
        "team": None,
        "role_selectors": {},
        "owner_stop": True,
    }
    unsafe = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        json={"expected_revision": 2, "unsafe_changes": {"workflow": "other"}},
    )
    assert unsafe.status_code == 409
    assert unsafe.json()["detail"]["code"] == "restart_required"

    events = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/events?limit=10"
    )
    assert events.status_code == 200
    assert any(event["event_type"] == "control_request" for event in events.json()["events"])
    assert client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/context?level=full"
    ).status_code == 403
    context = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/context?level=full&full_scope=true"
    )
    assert context.status_code == 200
    assert context.json()["level"] == "full"

    units.stop(f"aflow-run-{run_id}.service")
    (root / ".aflow" / "runs" / run_id / "run.json").write_text(
        '{"status":"running","workflow_name":"managed","team":null,'
        '"selected_start_step":"implement","max_turns":3,"extra_instructions":[]}'
    )
    monkeypatch.setattr(
        "aflow.cli._bootstrap_resume_invocation",
        lambda **_kwargs: SimpleNamespace(
            workflow_name="managed",
            plan_path=root / "plans" / "todo" / "test-plan.md",
            max_turns=3,
            team=None,
            start_step="implement",
            extra_instructions=(),
            resume_context=object(),
        ),
    )
    resumed = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/resume",
        headers={"Idempotency-Key": "resume-1"},
    )
    assert resumed.status_code == 201, resumed.text
    resumed_id = resumed.json()["run_id"]
    assert resumed_id != run_id
    assert len(units.start_calls) == 2
    resumed_replay = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/resume",
        headers={"Idempotency-Key": "resume-1"},
    )
    assert resumed_replay.status_code == 200
    assert resumed_replay.json()["run_id"] == resumed_id
    stopped = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{resumed_id}/owner-stop",
        headers={"Idempotency-Key": "stop-1"},
        json={"expected_revision": 0},
    )
    assert stopped.status_code == 200
    assert stopped.json()["launch_phase"] == "owner_stopped"
    assert units.stop_calls[-1] == f"aflow-run-{resumed_id}.service"
    stop_call_count = len(units.stop_calls)
    stopped_replay = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{resumed_id}/owner-stop",
        headers={"Idempotency-Key": "stop-1"},
        json={"expected_revision": 0},
    )
    assert stopped_replay.status_code == 200
    assert stopped_replay.json()["status"] == "owner_stopped"
    assert stopped_replay.json()["launch_phase"] == "owner_stopped"
    assert len(units.stop_calls) == stop_call_count


def test_rest_resume_persists_reviewer_start_step_and_replays_once(control_client) -> None:
    client, root, units, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    run_id = started["result"]["run_id"]
    units.stop(f"aflow-run-{run_id}.service")
    source_path = root / ".aflow" / "runs" / run_id / "run.json"
    source_path.write_text(
        '{"status":"running","workflow_name":"managed","team":null,'
        '"selected_start_step":"implement","max_turns":3,'
        '"extra_instructions":[]}'
    )
    before = source_path.read_bytes()
    monkeypatch.setattr(
        "aflow.cli._bootstrap_resume_invocation",
        lambda **_kwargs: SimpleNamespace(
            workflow_name="managed",
            plan_path=root / "plans" / "todo" / "test-plan.md",
            max_turns=3,
            team=None,
            start_step="review_implementation",
            extra_instructions=(),
            resume_context=object(),
        ),
    )
    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/resume"
    first = client.post(endpoint, headers={"Idempotency-Key": "resume-review"})
    replay = client.post(endpoint, headers={"Idempotency-Key": "resume-review"})

    assert first.status_code == 201, first.text
    assert replay.status_code == 200
    successor_id = first.json()["run_id"]
    assert replay.json()["run_id"] == successor_id
    assert len(units.start_calls) == 2
    record = json.loads(
        (root / ".aflow" / "start-requests" / f"{successor_id}.json").read_text()
    )
    assert record["prepared"]["start_step"] == "review_implementation"
    manifest = json.loads(
        (root / ".aflow" / "launches" / f"{successor_id}.json").read_text()
    )
    assert manifest["start_step"] == "review_implementation"
    assert source_path.read_bytes() == before


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (None, ("saved guidance",)),
        ({"extra_instructions": None}, ("saved guidance",)),
        ({"extra_instructions": ["replacement guidance"]}, ("replacement guidance",)),
        ({"extra_instructions": []}, ()),
    ),
)
def test_resume_extra_instructions_are_optional_and_idempotent(
    control_client,
    payload: dict[str, object] | None,
    expected: tuple[str, ...],
) -> None:
    client, root, units, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch, ["saved guidance"])
    started = _answer_pending(client, pending, monkeypatch)
    run_id = started["result"]["run_id"]
    units.stop(f"aflow-run-{run_id}.service")
    source_path = root / ".aflow" / "runs" / run_id / "run.json"
    source_path.write_text(
        '{"status":"running","workflow_name":"managed","team":null,'
        '"selected_start_step":"implement","max_turns":3,'
        '"extra_instructions":["saved guidance"]}'
    )
    before = source_path.read_bytes()
    bootstrap_calls: list[dict[str, object]] = []

    def bootstrap(**kwargs):
        bootstrap_calls.append(kwargs)
        provided = kwargs["extra_instructions_provided"]
        effective = (
            tuple(kwargs["extra_instructions_arg"])
            if provided
            else ("saved guidance",)
        )
        return SimpleNamespace(
            workflow_name="managed",
            plan_path=root / "plans" / "todo" / "test-plan.md",
            max_turns=3,
            team=None,
            start_step="implement",
            extra_instructions=effective,
            resume_context=object(),
        )

    monkeypatch.setattr("aflow.cli._bootstrap_resume_invocation", bootstrap)
    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/resume"
    headers = {"Idempotency-Key": "resume-instructions"}
    if payload is None:
        resumed = client.post(endpoint, headers=headers)
    else:
        resumed = client.post(endpoint, headers=headers, json=payload)
    assert resumed.status_code == 201, resumed.text
    successor_id = resumed.json()["run_id"]
    assert len(units.start_calls) == 2
    argv = units.start_calls[-1][1]
    if expected:
        assert f"--extra-instruction={expected[0]}" in argv
    else:
        assert not any(argument.startswith("--extra-instruction=") for argument in argv)
    assert bootstrap_calls[0]["extra_instructions_provided"] == (
        payload is not None and payload.get("extra_instructions") is not None
    )
    record_path = root / ".aflow" / "start-requests" / f"{successor_id}.json"
    record_text = record_path.read_text()
    assert "saved guidance" not in record_text
    assert source_path.read_bytes() == before

    replay = (
        client.post(endpoint, headers=headers)
        if payload is None
        else client.post(endpoint, headers=headers, json=payload)
    )
    assert replay.status_code == 200
    assert replay.json()["run_id"] == successor_id
    changed = client.post(
        endpoint,
        headers=headers,
        json={"extra_instructions": ["different guidance"]},
    )
    assert changed.status_code == 409
    assert changed.json() == {"detail": {"code": "idempotency_conflict"}}
    assert len(units.start_calls) == 2
    assert source_path.read_bytes() == before


def test_resume_rejects_invalid_extra_instructions_without_reserving(
    control_client,
) -> None:
    client, root, units, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch, ["saved guidance"])
    started = _answer_pending(client, pending, monkeypatch)
    run_id = started["result"]["run_id"]
    units.stop(f"aflow-run-{run_id}.service")
    source_path = root / ".aflow" / "runs" / run_id / "run.json"
    source_path.write_text(
        '{"status":"running","workflow_name":"managed","team":null,'
        '"selected_start_step":"implement","max_turns":3,'
        '"extra_instructions":["saved guidance"]}'
    )
    before = source_path.read_bytes()
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/resume",
        headers={"Idempotency-Key": "resume-invalid"},
        json={"extra_instructions": [""]},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "operation_rejected"}}
    assert len(units.start_calls) == 1
    assert source_path.read_bytes() == before
    assert len(client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs").json()["runs"]) == 1


def test_rest_durable_recovery_preserves_lineage_and_safe_admission_errors(
    control_client,
) -> None:
    from aflow_app_server import main

    client, root, units, monkeypatch = control_client
    service = main._control_plane_service
    assert service is not None
    source_id, source_dir, daemon = _seed_recovery_source(
        service, root, monkeypatch
    )
    before = _source_artifact_bytes(source_dir)
    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/resume"
    payload = {
        "extra_instructions": ["transport recovery guidance"],
        "recovery": _recovery_payload(),
    }
    headers = {"Idempotency-Key": "rest-recovery-1"}

    resumed = client.post(endpoint, headers=headers, json=payload)
    assert resumed.status_code == 201, resumed.text
    successor = resumed.json()
    successor_id = successor["run_id"]
    assert successor_id != source_id
    assert (
        daemon.application.repository.get_run_status(
            successor_id, include_progress=False
        ).progress
        is None
    )
    assert len(units.start_calls) == 1
    assert _source_artifact_bytes(source_dir) == before

    successor_events = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{successor_id}/events"
    )
    assert successor_events.status_code == 200
    recovery_events = [
        event
        for event in successor_events.json()["events"]
        if event["event_type"] == "recovery_requested"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0]["data"]["source_run_id"] == source_id
    assert recovery_events[0]["data"]["target_selector"] == "codex.test"

    replay = client.post(endpoint, headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json()["run_id"] == successor_id
    assert replay.json()["created"] is False
    assert len(units.start_calls) == 1

    changed_target = client.post(
        endpoint,
        headers=headers,
        json={"recovery": _recovery_payload("codex.other")},
    )
    assert changed_target.status_code == 409
    assert changed_target.json() == {"detail": {"code": "idempotency_conflict"}}
    assert len(units.start_calls) == 1

    for invalid_recovery in (
        {"mode": "automatic", "worker_selector": "codex.test"},
        {
            "mode": "durable_evidence",
            "worker_selector": "codex.test",
            "run_state_path": "/tmp/run.json",
            "provider_session_id": "source-session",
        },
    ):
        rejected = client.post(
            endpoint,
            headers={"Idempotency-Key": f"rest-invalid-{len(invalid_recovery)}"},
            json={"recovery": invalid_recovery},
        )
        assert rejected.status_code == 422
        detail = rejected.json()["detail"]
        assert detail["code"] == "recovery_target_invalid"
        assert "target worker selector" in detail["message"]
    assert len(units.start_calls) == 1
    assert _source_artifact_bytes(source_dir) == before
    assert daemon.service._read_record(successor_id)["recovery"] == _recovery_payload()

    wrong_project = client.post(
        endpoint.replace(PROJECT_ID, "not-allowed"),
        headers={"Idempotency-Key": "rest-wrong-project"},
        json={"recovery": _recovery_payload()},
    )
    assert wrong_project.status_code == 404
    assert wrong_project.json() == {"detail": {"code": "project_not_found"}}
    assert len(units.start_calls) == 1


def test_rest_durable_recovery_rejects_active_source_without_starting_a_unit(
    control_client,
) -> None:
    from aflow_app_server import main

    client, root, units, monkeypatch = control_client
    service = main._control_plane_service
    assert service is not None
    source_id, source_dir, _daemon = _seed_recovery_source(
        service,
        root,
        monkeypatch,
        worker_evidence={"active": True},
        source_id="active-recovery-source",
    )
    before = _source_artifact_bytes(source_dir)
    rejected = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/resume",
        headers={"Idempotency-Key": "rest-active-recovery"},
        json={"recovery": _recovery_payload()},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "recovery_source_activity"
    assert "source activity" in rejected.json()["detail"]["message"]
    assert units.start_calls == []
    assert _source_artifact_bytes(source_dir) == before


@pytest.mark.parametrize(
    ("operation_state", "malformed"),
    (("in_flight", False), ("pending", False), ("in_flight", True)),
    ids=("in-flight", "pending", "malformed"),
)
def test_rest_ordinary_resume_rejects_unresolved_recovery_runtime(
    control_client,
    operation_state: str,
    malformed: bool,
) -> None:
    from aflow_app_server import main

    client, root, units, monkeypatch = control_client
    service = main._control_plane_service
    assert service is not None
    source_id, source_dir, _daemon = _seed_recovery_source(
        service,
        root,
        monkeypatch,
        source_id=(
            "ordinary-inflight-recovery-source"
            if not malformed
            else "ordinary-malformed-recovery-source"
        ),
    )
    source_payload = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
    source_payload["recovery_runtime"] = _unresolved_recovery_runtime(
        source_id,
        malformed=malformed,
        operation_state=operation_state,
    )
    source_dir.joinpath("run.json").write_text(
        json.dumps(source_payload, sort_keys=True),
        encoding="utf-8",
    )
    before = _source_artifact_bytes(source_dir)
    rejected = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/resume",
        headers={"Idempotency-Key": f"ordinary-unresolved-{malformed}"},
        json={},
    )

    assert rejected.status_code == 422
    assert rejected.json() == {
        "detail": {
            "code": "recovery_operation_unresolved",
            "message": (
                "Durable recovery has unknown liveness or prior turn evidence; "
                "reconcile the operation before retrying."
            ),
        }
    }
    assert units.start_calls == []
    assert _source_artifact_bytes(source_dir) == before
    assert len(client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs").json()["runs"]) == 1


def test_control_admission_uses_live_targets_and_preserves_rejected_state(
    control_client,
) -> None:
    client, root, _, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    run_id = started["result"]["run_id"]
    config_path = root.parent / "global" / "aflow.toml"
    _add_live_control_targets(config_path)

    capabilities = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/capabilities"
    )
    assert capabilities.status_code == 200
    assert "new" in capabilities.json()["teams"]
    assert "reasonix.new" in capabilities.json()["admitted_role_selectors"]["worker"]

    accepted = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "live-target-1"},
        json={
            "expected_revision": 0,
            "team": "new",
            "role_selectors": {"worker": "reasonix.new"},
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["revision"] == 1
    override_path = root / ".aflow" / "runs" / run_id / "overrides.toml"
    original_bytes = override_path.read_bytes()
    original_events = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/events?limit=1000"
    ).json()["events"]

    rejected = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "live-target-invalid"},
        json={
            "expected_revision": 1,
            "role_selectors": {"worker": "reasonix.missing"},
        },
    )
    assert rejected.status_code == 422
    assert rejected.json() == {
        "detail": {
            "code": "validation_error",
            "field": "role_selectors.worker",
            "target": "reasonix.missing",
            "message": (
                "roles contains selectors not configured in current configuration: "
                "reasonix.missing"
            ),
        }
    }
    assert override_path.read_bytes() == original_bytes
    assert client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}"
    ).json()["revision"] == 1
    assert client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/events?limit=1000"
    ).json()["events"] == original_events


def test_event_stream_delivers_events_appended_after_connection(control_client) -> None:
    client, root, _, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    result = started["result"]
    assert isinstance(result, dict)
    run_id = result["run_id"]

    initial_events = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/events?limit=1000"
    )
    assert initial_events.status_code == 200
    after_sequence = max(event["sequence"] for event in initial_events.json()["events"])

    with live_server() as base_url, httpx.Client(timeout=5) as streaming_client:
        with streaming_client.stream(
            "GET",
            f"{base_url}/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/events/stream",
            params={"after_sequence": after_sequence, "limit": 10},
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as response:
            assert response.status_code == 200
            appended = append_run_event(
                root / ".aflow" / "runs" / run_id,
                "late_event",
                {"source": "stream-regression-test"},
            )

            payload = next(
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ")
            )

    assert payload["events"] == [
        {
            "sequence": appended.sequence,
            "event_type": "late_event",
            "timestamp": appended.timestamp,
            "data": {"source": "stream-regression-test"},
            "schema_version": appended.schema_version,
        }
    ]


def test_control_plane_rejects_unknown_projects_and_plan_traversal(control_client) -> None:
    client, _, _, _ = control_client
    assert client.get("/api/control-plane/projects/not-allowed/runs").status_code == 404
    rejected = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        json={"plan_path": "../outside.md", "workflow_name": "managed"},
    )
    assert rejected.status_code == 422
    assert rejected.json() == {"detail": {"code": "operation_rejected"}}


def test_two_registered_projects_keep_exact_plan_and_launch_boundaries(
    control_client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Identical documents remain isolated while release identity stays shared."""
    from aflow_app_server import main

    client, root, units, _ = control_client
    registry = main._project_registry
    original_service = main._control_plane_service
    assert registry is not None
    assert original_service is not None
    peer_root = _register_peer_project(registry, root.parent)
    peer_id = "peer-project"
    plan_name = "same-boundary.md"
    markers = {
        PROJECT_ID: "PROJECT_A_ONLY",
        peer_id: "PROJECT_B_ONLY",
    }

    # Both projects intentionally use the same plan basename.  The authoring
    # API must keep content and revisions scoped to the exact registry ID.
    created: dict[str, dict[str, object]] = {}
    for project_id, marker in markers.items():
        response = client.post(
            f"/api/projects/{project_id}/plans",
            json={
                "name": plan_name,
                "content": f"# {project_id}\n\n{marker}\n",
            },
        )
        assert response.status_code == 201, response.text
        created[project_id] = response.json()
        assert created[project_id]["project_id"] == project_id

    for project_id, marker in markers.items():
        read = client.get(
            f"/api/projects/{project_id}/plans/todo/{plan_name}"
        )
        assert read.status_code == 200, read.text
        assert marker in read.json()["content"]
        listed = client.get(f"/api/control-plane/projects/{project_id}/plans")
        assert listed.status_code == 200
        assert any(
            item["path"] == f"plans/todo/{plan_name}"
            for item in listed.json()["plans"]
        )

    updated_a = client.put(
        f"/api/projects/{PROJECT_ID}/plans/todo/{plan_name}",
        json={
            "content": f"# {PROJECT_ID}\n\n{markers[PROJECT_ID]}\nA_UPDATED\n",
            "expected_revision": created[PROJECT_ID]["revision"],
        },
    )
    assert updated_a.status_code == 200, updated_a.text
    assert "A_UPDATED" in updated_a.json()["content"]
    peer_read = client.get(f"/api/projects/{peer_id}/plans/todo/{plan_name}")
    assert peer_read.status_code == 200
    assert peer_read.json()["content"] == (
        f"# {peer_id}\n\n{markers[peer_id]}\n"
    )

    promoted: dict[str, dict[str, object]] = {}
    for project_id, revision in (
        (PROJECT_ID, updated_a.json()["revision"]),
        (peer_id, created[peer_id]["revision"]),
    ):
        response = client.post(
            f"/api/projects/{project_id}/plans/todo/{plan_name}/promote",
            json={"expected_revision": revision},
        )
        assert response.status_code == 200, response.text
        promoted[project_id] = response.json()
        assert promoted[project_id]["path"] == f"plans/in-progress/{plan_name}"

    # A project-scoped launch cannot name an absolute path from its peer, and
    # invalid registry operations must not mutate the persistent allowlist.
    foreign_plan = peer_root / "plans" / "in-progress" / plan_name
    rejected_foreign = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        json={"plan_path": str(foreign_plan), "workflow_name": "managed"},
    )
    assert rejected_foreign.status_code == 422
    assert rejected_foreign.json() == {"detail": {"code": "operation_rejected"}}
    registry_bytes = registry.path.read_bytes()
    rejected_root = client.post(
        "/api/projects",
        json={
            "mode": "register",
            "path": "../outside-project",
            "display_name": "Outside",
        },
    )
    assert rejected_root.status_code == 422
    assert rejected_root.json() == {"detail": {"code": "operation_rejected"}}
    assert registry.path.read_bytes() == registry_bytes
    assert client.get("/api/projects/not-registered/plans").status_code == 404
    assert client.get(
        "/api/control-plane/projects/not-registered/runs"
    ).status_code == 404

    # Replace the fixture service only inside this test so the selected
    # environment contains a synthetic secret.  The service still validates
    # one shared executable, environment file, release identity, and global
    # workflow pair for both dynamically composed project daemons.
    secret = "issue6-synthetic-secret"
    control_service = ControlPlaneService(
        registry,
        aflow_executable=root / "release" / "bin" / "aflow",
        environment_file=root / "aflowd.env",
        release_identity="issue6-release",
        environment={"AFLOW_TEST_SECRET": secret},
        daemon_factory=lambda config: AflowDaemon(config, units=units),
        workflow_config_path=root.parent / "global" / "aflow.toml",
    )
    control_service.start()
    monkeypatch.setattr(main, "_control_plane_service", control_service)
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)

    captured_starts: dict[str, dict[str, object]] = {}
    original_start = units.start

    def capture_start(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment_file: Path | None = None,
        environment: dict[str, str] | None = None,
    ):
        captured_starts[name] = {
            "cwd": cwd,
            "environment_file": environment_file,
            "environment": dict(environment or {}),
        }
        return original_start(
            name,
            argv,
            cwd=cwd,
            environment_file=environment_file,
            environment=environment,
        )

    monkeypatch.setattr(units, "start", capture_start)
    run_ids: dict[str, str] = {}
    public_payloads: list[str] = []
    for project_id, project_root in (
        (PROJECT_ID, root),
        (peer_id, peer_root),
    ):
        response = client.post(
            f"/api/control-plane/projects/{project_id}/runs",
            headers={"Idempotency-Key": f"issue6-{project_id}"},
            json={
                "plan_path": promoted[project_id]["path"],
                "workflow_name": "managed",
            },
        )
        assert response.status_code == 201, response.text
        public_payloads.append(response.text)
        run_id = response.json()["result"]["run_id"]
        run_ids[project_id] = run_id

        manifest_path = project_root / ".aflow" / "launches" / f"{run_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["project_root"] == str(project_root.resolve())
        assert manifest["plan_path"] == str(
            (project_root / "plans" / "in-progress" / plan_name).resolve()
        )
        assert "executable" not in manifest
        assert "environment" not in manifest
        assert "environment_file" not in manifest

        start_record = json.loads(
            (
                project_root
                / ".aflow"
                / "start-requests"
                / f"{run_id}.json"
            ).read_text(encoding="utf-8")
        )
        assert start_record["selected_executable"] == str(
            (root / "release" / "bin" / "aflow").resolve()
        )
        assert start_record["selected_release_identity"] == "issue6-release"
        assert start_record["selected_environment_file"]["path"] == str(
            (root / "aflowd.env").resolve()
        )
        assert captured_starts[f"aflow-run-{run_id}.service"] == {
            "cwd": project_root.resolve(),
            "environment_file": (root / "aflowd.env").resolve(),
            "environment": {"AFLOW_TEST_SECRET": secret},
        }

        events = client.get(
            f"/api/control-plane/projects/{project_id}/runs/{run_id}/events?limit=100"
        )
        assert events.status_code == 200
        public_payloads.append(events.text)
        attempt = next(
            event for event in events.json()["events"]
            if event["event_type"] == "daemon_start_attempt"
        )
        assert attempt["data"]["cwd"] == str(project_root.resolve())
        assert attempt["data"]["executable"] == str(
            (root / "release" / "bin" / "aflow").resolve()
        )
        assert attempt["data"]["release_identity"] == "issue6-release"

    assert len(captured_starts) == 2
    assert {call[2] for call in units.start_calls} == {
        root.resolve(),
        peer_root.resolve(),
    }
    assert {
        control_service._project(PROJECT_ID).config_path,
        control_service._project(peer_id).config_path,
    } == {(root.parent / "global" / "aflow.toml").resolve()}
    reloaded_registry = ProjectRegistry(root.parent, registry.path)
    assert {record.id for record in reloaded_registry.list_records()} == {
        PROJECT_ID,
        peer_id,
    }

    # StartRunPayload is closed: a client cannot select a different release,
    # environment file, or arbitrary environment mapping for either project.
    override = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "issue6-client-override"},
        json={
            "plan_path": promoted[PROJECT_ID]["path"],
            "workflow_name": "managed",
            "aflow_executable": "/tmp/attacker-aflow",
            "environment_file": "/tmp/attacker.env",
            "environment": {"AFLOW_TEST_SECRET": "client-secret"},
        },
    )
    assert override.status_code == 422
    assert len(captured_starts) == 2
    public_payloads.append(override.text)
    assert secret not in "\n".join(public_payloads)
    assert "client-secret" not in "\n".join(public_payloads)
    assert secret not in caplog.text


def test_project_config_read_validate_save_and_stale_revision(control_client, tmp_path: Path) -> None:
    from aflow.config import bootstrap_project_config

    client, root, _, _ = control_client
    config_dir = root.parent / "global"
    aflow_text = (config_dir / "aflow.toml").read_text(encoding="utf-8")
    workflows_text = (config_dir / "workflows.toml").read_text(encoding="utf-8")

    unauthorized = client.get(
        "/api/config",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json() == {"detail": {"code": "unauthorized"}}

    read = client.get("/api/config")
    assert read.status_code == 200
    body = read.json()
    assert body["project_id"] == "global"
    assert body["documents"] == ["aflow.toml", "workflows.toml"]
    assert body["aflow_toml"] == aflow_text
    assert body["workflows_toml"] == workflows_text
    assert body["validation"]["state"] == "ready"
    assert body["validation"]["workflows"] == ["managed"]
    revision = body["revision"]

    starter = tmp_path / "starter-scratch"
    starter.mkdir()
    bootstrap_project_config(starter / "aflow.toml")
    placeholder = client.post(
        "/api/config/validate",
        json={
            "aflow_toml": (starter / "aflow.toml").read_text(encoding="utf-8"),
            "workflows_toml": (starter / "workflows.toml").read_text(encoding="utf-8"),
        },
    )
    assert placeholder.status_code == 200
    assert placeholder.json()["state"] == "configuration_required"
    assert "harness.starter.profiles.default.model" in placeholder.json()["placeholders"]

    updated_aflow = aflow_text.replace('model = "test"', 'model = "test-2"')
    saved = client.put(
        "/api/config",
        json={
            "aflow_toml": updated_aflow,
            "workflows_toml": workflows_text,
            "expected_revision": revision,
        },
    )
    assert saved.status_code == 200
    saved_body = saved.json()
    assert saved_body["revision"] != revision
    assert saved_body["validation"]["state"] == "ready"
    assert (config_dir / "aflow.toml").read_text(encoding="utf-8") == updated_aflow

    stale = client.put(
        "/api/config",
        json={
            "aflow_toml": updated_aflow.replace('model = "test-2"', 'model = "test-3"'),
            "workflows_toml": workflows_text,
            "expected_revision": revision,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    assert stale.json()["detail"]["current_revision"] == saved_body["revision"]
    assert (config_dir / "aflow.toml").read_text(encoding="utf-8") == updated_aflow

    malformed = client.put(
        "/api/config",
        json={
            "aflow_toml": updated_aflow,
            "workflows_toml": workflows_text,
            "expected_revision": "not-a-revision",
        },
    )
    assert malformed.status_code == 422

    broken_workflows = workflows_text.replace('role = "worker"', 'role = "ghost"')
    invalid = client.put(
        "/api/config",
        json={
            "aflow_toml": updated_aflow,
            "workflows_toml": broken_workflows,
            "expected_revision": saved_body["revision"],
        },
    )
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": {"code": "operation_rejected"}}
    assert (config_dir / "workflows.toml").read_text(encoding="utf-8") == workflows_text

    reread = client.get("/api/config").json()
    assert reread["revision"] == saved_body["revision"]
    assert reread["aflow_toml"] == updated_aflow


def test_global_config_save_allowed_while_run_active(
    control_client,
) -> None:
    client, _, _, monkeypatch = control_client
    pending = _start_pending(client, monkeypatch)
    started = _answer_pending(client, pending, monkeypatch)
    result = started["result"]
    assert isinstance(result, dict)
    run_id = result["run_id"]

    # Global saves are never blocked by runs in other states: the running run
    # keeps its frozen snapshot and only future runs observe the new pair.
    before = client.get("/api/config").json()
    updated_aflow = before["aflow_toml"].replace('model = "test"', 'model = "edited"')
    saved = client.put(
        "/api/config",
        json={
            "aflow_toml": updated_aflow,
            "workflows_toml": before["workflows_toml"],
            "expected_revision": before["revision"],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] != before["revision"]
    assert saved.json()["validation"]["state"] == "ready"

    # A second edit during the still-running run is also unblocked.
    again = client.put(
        "/api/config",
        json={
            "aflow_toml": updated_aflow.replace('model = "edited"', 'model = "test"'),
            "workflows_toml": before["workflows_toml"],
            "expected_revision": saved.json()["revision"],
        },
    )
    assert again.status_code == 200

    # The pre-existing run is explicitly stoppable and unaffected by edits.
    stopped = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/owner-stop",
        headers={"Idempotency-Key": "stop-config"},
        json={"expected_revision": 0},
    )
    assert stopped.status_code == 200


def test_rest_typed_successor_start_exposes_lineage_and_keeps_instruction_text_transient(
    control_client,
) -> None:
    client, root, units, monkeypatch = control_client
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)
    source_response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "rest-source"},
        json={
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
            "start_step": "1",
            "max_turns": 2,
        },
    )
    assert source_response.status_code == 201
    source_id = source_response.json()["result"]["run_id"]
    stopped = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/owner-stop",
        headers={"Idempotency-Key": "rest-source-stop"},
        json={"expected_revision": 0},
    )
    assert stopped.status_code == 200

    sentinel = "private-rest-guidance-91ac"
    successor_response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "rest-successor"},
        json={
            "plan_path": "plans/todo/second-plan.md",
            "workflow_name": "managed",
            "start_step": "1",
            "max_turns": 3,
            "extra_instructions": [sentinel],
            "restarted_from_run_id": source_id,
        },
    )

    assert successor_response.status_code == 201
    successor = successor_response.json()["result"]
    assert successor["run_id"] != source_id
    assert successor["restarted_from_run_id"] == source_id
    status = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{successor['run_id']}"
    )
    assert status.status_code == 200
    assert status.json()["selected_start_step"] == "implement"
    assert status.json()["restarted_from_run_id"] == source_id
    assert units.start_calls[-1][1][-1] == f"--extra-instruction={sentinel}"
    durable = [
        root / ".aflow" / "launches" / f"{successor['run_id']}.json",
        root / ".aflow" / "start-requests" / f"{successor['run_id']}.json",
        root / ".aflow" / "runs" / successor["run_id"] / "events.jsonl",
    ]
    assert all(sentinel not in path.read_text() for path in durable)
    source_events = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/events"
    )
    assert source_events.status_code == 200
    assert any(
        event["event_type"] == "restart_successor_requested"
        and event["data"]["successor_run_id"] == successor["run_id"]
        for event in source_events.json()["events"]
    )

    rejected = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        json={
            "plan_path": "plans/todo/test-plan.md",
            "unknown_launch_option": True,
        },
    )
    assert rejected.status_code == 422


def test_project_config_form_is_pure_authenticated_and_action_bounded(
    control_client,
) -> None:

    client, root, _, _ = control_client
    config_dir = root.parent / "global"
    before = (
        (config_dir / "aflow.toml").read_bytes(),
        (config_dir / "workflows.toml").read_bytes(),
    )

    unauthenticated = TestClient(app).post(
        "/api/config/form",
        json={"aflow_toml": "", "workflows_toml": ""},
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": {"code": "unauthorized"}}

    read = client.get("/api/config").json()
    aflow_text = read["aflow_toml"]
    workflows_text = read["workflows_toml"]

    noop = client.post(
        "/api/config/form",
        json={"aflow_toml": aflow_text, "workflows_toml": workflows_text},
    )
    assert noop.status_code == 200
    body = noop.json()
    assert body["changed"] is False
    assert body["aflow_toml"] == aflow_text
    assert body["form"]["default_workflow"] == "managed"
    assert body["starter_defaults"] is None

    updated = client.post(
        "/api/config/form",
        json={
            "aflow_toml": aflow_text,
            "workflows_toml": workflows_text,
            "action": {"type": "set_max_turns", "value": 9},
        },
    )
    assert updated.status_code == 200
    updated_body = updated.json()
    assert updated_body["changed"] is True
    assert "max_turns = 9" in updated_body["aflow_toml"]
    assert updated_body["validation"]["state"] == "ready"

    rejected = client.post(
        "/api/config/form",
        json={
            "aflow_toml": aflow_text,
            "workflows_toml": workflows_text,
            "action": {"type": "set_default_workflow", "value": "ghost"},
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "guided_config_rejected"
    assert rejected.json()["detail"]["reason"] == "unknown_workflow"

    unknown_action = client.post(
        "/api/config/form",
        json={
            "aflow_toml": aflow_text,
            "workflows_toml": workflows_text,
            "action": {"type": "delete_profile", "harness": "codex"},
        },
    )
    assert unknown_action.status_code == 422

    unknown_field = client.post(
        "/api/config/form",
        json={
            "aflow_toml": aflow_text,
            "workflows_toml": workflows_text,
            "action": {"type": "set_max_turns", "value": 5, "force": True},
        },
    )
    assert unknown_field.status_code == 422

    revision_field = client.post(
        "/api/config/form",
        json={
            "aflow_toml": aflow_text,
            "workflows_toml": workflows_text,
            "expected_revision": read["revision"],
        },
    )
    assert revision_field.status_code == 422

    syntax = client.post(
        "/api/config/form",
        json={"aflow_toml": "[aflow\n", "workflows_toml": workflows_text},
    )
    assert syntax.status_code == 200
    assert syntax.json()["changed"] is False
    assert syntax.json()["form"] is None
    assert syntax.json()["syntax_issues"][0]["document"] == "aflow.toml"

    assert (
        (config_dir / "aflow.toml").read_bytes(),
        (config_dir / "workflows.toml").read_bytes(),
    ) == before


def test_project_config_form_build_starter_from_empty_pair(control_client) -> None:

    client, root, _, _ = control_client

    empty = client.post(
        "/api/config/form",
        json={"aflow_toml": "", "workflows_toml": ""},
    )
    assert empty.status_code == 200
    # The global configuration is not tied to one repository, so starter
    # defaults are the labeled fallback rather than a probed project branch.
    assert empty.json()["starter_defaults"] == {
        "workflow": "implement",
        "main_branch": "main",
        "main_branch_source": "fallback",
        "team": None,
    }
    body = empty.json()
    # With no action submitted the form is a pure projection: nothing changes.
    assert body["changed"] is False
    assert body["aflow_toml"] == ""
    assert body["validation"] is not None

    # Rendering with an explicit team keeps the endpoint pure: nothing lands
    # on disk for a form transform.
    with_team = client.post(
        "/api/config/form",
        json={
            "aflow_toml": "",
            "workflows_toml": "",
            "action": {
                "type": "build_starter",
                "workflow": "implement",
                "main_branch": "main",
                "team": "crew",
            },
        },
    )
    assert with_team.status_code == 200
    assert '[teams."crew".roles]' in with_team.json()["aflow_toml"]


def test_project_config_form_rejects_zcode_model_with_explanation(
    control_client,
) -> None:
    client, root, _, _ = control_client
    read = client.get("/api/config").json()

    rejected = client.post(
        "/api/config/form",
        json={
            "aflow_toml": read["aflow_toml"],
            "workflows_toml": read["workflows_toml"],
            "action": {
                "type": "upsert_profile",
                "harness": "zcode",
                "profile": "p",
                "model": "gpt",
            },
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "guided_config_rejected"
    assert rejected.json()["detail"]["reason"] == "zcode_managed_by_zcode"
    assert "ZCode" in rejected.json()["detail"]["message"]


def test_project_config_form_dotted_profiles_and_bounded_rejections(
    control_client,
) -> None:
    client, root, _, _ = control_client
    config_dir = root.parent / "global"
    before = (
        (config_dir / "aflow.toml").read_bytes(),
        (config_dir / "workflows.toml").read_bytes(),
    )
    read = client.get("/api/config").json()
    aflow_text = read["aflow_toml"]
    workflows_text = read["workflows_toml"]

    created = client.post(
        "/api/config/form",
        json={
            "aflow_toml": aflow_text,
            "workflows_toml": workflows_text,
            "action": {
                "type": "upsert_profile",
                "harness": "opencode",
                "profile": "glm-5.3",
                "model": "glm-5.3",
            },
        },
    )
    assert created.status_code == 200
    created_body = created.json()
    assert created_body["form"]["harnesses"]["opencode"]["glm-5.3"]["model"] == (
        "glm-5.3"
    )

    assigned = client.post(
        "/api/config/form",
        json={
            "aflow_toml": created_body["aflow_toml"],
            "workflows_toml": workflows_text,
            "action": {
                "type": "set_global_role",
                "role": "worker",
                "selector": "opencode.glm-5.3",
            },
        },
    )
    assert assigned.status_code == 200
    assert 'worker = "opencode.glm-5.3"' in assigned.json()["aflow_toml"]
    assert "opencode.glm-5.3" in assigned.json()["choices"]["selectors"]

    bounded_cases = [
        # Starter-invalid input must stay a bounded 422, never HTTP 500.
        {
            "type": "set_default_workflow",
            "value": "bad name!",
        },
        # Reserved role and unknown global role are rejected before mutation.
        {
            "type": "set_global_role",
            "role": "prompts",
            "selector": "codex.test",
        },
        {
            "type": "set_team_role",
            "team": "ghost-team",
            "role": "ghost-role",
            "selector": "codex.test",
        },
        # Supplied ZCode model/effort, explicit null included, is rejected.
        {"type": "upsert_profile", "harness": "zcode", "profile": "p", "model": None},
    ]
    expected_reasons = [
        "invalid_action_value",
        "reserved_role",
        "unknown_role",
        "zcode_managed_by_zcode",
    ]
    for action, reason in zip(bounded_cases, expected_reasons, strict=True):
        rejected = client.post(
            "/api/config/form",
            json={
                "aflow_toml": aflow_text,
                "workflows_toml": workflows_text,
                "action": action,
            },
        )
        assert rejected.status_code == 422, action
        assert rejected.json()["detail"]["code"] == "guided_config_rejected"
        assert rejected.json()["detail"]["reason"] == reason

    # Rejections never wrote or reloaded the registered project.
    assert (
        (config_dir / "aflow.toml").read_bytes(),
        (config_dir / "workflows.toml").read_bytes(),
    ) == before


def test_project_config_form_branch_probe_falls_back_for_renderer_incompatible_branch(
    control_client,
) -> None:
    client, root, _, _ = control_client
    subprocess.run(
        ("git", "-C", str(root), "checkout", "-q", "-b", "feature/foo+bar"),
        check=True,
    )

    empty = client.post(
        "/api/config/form",
        json={"aflow_toml": "", "workflows_toml": ""},
    )
    assert empty.status_code == 200
    assert empty.json()["starter_defaults"] == {
        "workflow": "implement",
        "team": None,
        "main_branch": "main",
        "main_branch_source": "fallback",
    }

    built = client.post(
        "/api/config/form",
        json={
            "aflow_toml": "",
            "workflows_toml": "",
            "action": {
                "type": "build_starter",
                "workflow": "implement",
                "main_branch": "main",
            },
        },
    )
    assert built.status_code == 200
    assert built.json()["changed"] is True


def test_real_dirty_startup_failure_survives_api_reload(control_client):
    from aflow_app_server import main
    client, root, units, _ = control_client
    workflow_path = root.parent / "global" / "workflows.toml"
    config_path = workflow_path.with_name("aflow.toml")
    config_path.write_text(config_path.read_text().replace("[aflow]", '[aflow]\nteam_lead = "worker"'))
    workflow_path.write_text(
        '[workflow.managed]\nsetup = ["worktree", "branch"]\nteardown = ["merge", "rm_worktree"]\nmain_branch = "main"\n'
        + workflow_path.read_text()
    )
    subprocess.run(["git", "checkout", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)
    (root / "untracked-blocker.txt").write_text("dirty fixture")
    main._control_plane_service.start()
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "dirty-failure"},
        json={"plan_path": "plans/todo/test-plan.md", "workflow_name": "managed"},
    )
    assert response.status_code == 202, response.text
    question = response.json()["startup_question"]
    assert question["kind"] == "confirm_worktree_dirty"
    assert units.start_calls == []
    declined = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/startup-answers/{question['question_id']}",
        headers={"Idempotency-Key": "dirty-failure-answer"},
        json={"answer": False},
    )
    assert declined.status_code == 422, declined.text
    error = declined.json()["detail"]
    assert error["code"] == "startup_failed", error
    assert error["message"] == "Startup aborted due to dirty worktree"
    assert units.start_calls == []
    main._control_plane_service = ControlPlaneService(
        main._project_registry,
        aflow_executable=root / "release" / "bin" / "aflow",
        environment_file=root / "aflowd.env", release_identity="test-release",
        daemon_factory=lambda config: AflowDaemon(config, units=units),
        workflow_config_path=config_path,
    )
    main._control_plane_service.start()
    status = client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs/{error['run_id']}").json()
    assert status["status"] == "needs_attention"
    assert status["reason"] == error["message"]
    assert status["evidence"]["no_agent_started"] is True
    assert status["started_at"] is None
    assert status["plan_path"].endswith("plans/todo/test-plan.md")


@pytest.mark.parametrize(
    ("plan_text", "expected_message"),
    [
        (
            "# Duplicate\n\n"
            "## Git Tracking\n\n- Plan Branch: `one`\n"
            "- Pre-Handoff Base HEAD: `abc`\n\n"
            "## 2. Git Tracking\n\n- Plan Branch: `two`\n"
            "- Pre-Handoff Base HEAD: `def`\n\n"
            "### [ ] Checkpoint 1: Test\n- [ ] step\n",
            "Plan validation failed. Add or correct exactly one Git Tracking section, then retry.",
        ),
        (
            "# Malformed\n\nThis plan has no checkpoint section.\n",
            "Plan validation failed. Add at least one valid Checkpoint section and checklist, then retry.",
        ),
        (
            "# Tracking without checkpoint\n\n"
            "## Git Tracking\n\n- Plan Branch: ``\n"
            "- Pre-Handoff Base HEAD: ``\n",
            "Plan validation failed. Add at least one valid Checkpoint section and checklist, then retry.",
        ),
    ],
    ids=["duplicate-tracking", "malformed-plan", "tracking-without-checkpoint"],
)
def test_plan_admission_rejection_is_safe_and_preallocation_stays_empty(
    control_client,
    plan_text: str,
    expected_message: str,
) -> None:
    from aflow.api.startup import PLAN_ADMISSION_ERROR_CODE

    client, root, units, monkeypatch = control_client
    (root / "plans" / "todo" / "test-plan.md").write_text(plan_text)
    monkeypatch.setattr("aflow.workflow._workflow_requires_git_tracking", lambda *_: True)

    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "plan-admission-before-reservation"},
        json={
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": {
            "code": PLAN_ADMISSION_ERROR_CODE,
            "message": expected_message,
        }
    }
    assert str(root) not in response.text
    assert "abc" not in response.text
    assert "def" not in response.text
    assert units.start_calls == []
    assert not (root / ".aflow" / "launches").exists()
    assert not (root / ".aflow" / "runs").exists()
    assert not (root / ".aflow" / "last_run_id").exists()


def test_rest_start_normalizes_blank_git_tracking_before_launch(control_client) -> None:
    from aflow.plan import parse_git_tracking_metadata

    client, root, units, monkeypatch = control_client
    subprocess.run(
        ("git", "checkout", "-b", "main"),
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "README.md").write_text("ready\n", encoding="utf-8")
    plan_path = root / "plans" / "todo" / "test-plan.md"
    plan_path.write_text(
        "# Test\n\n"
        "## Git Tracking\n\n"
        "- Plan Branch: ``\n"
        "- Pre-Handoff Base HEAD: ``\n\n"
        "### [ ] Checkpoint 1: Test\n- [ ] step\n",
        encoding="utf-8",
    )
    _commit_fixture_repository(root)
    head_result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_head = head_result.stdout.strip()
    monkeypatch.setattr("aflow.workflow._workflow_requires_git_tracking", lambda *_: True)
    observed: dict[str, object] = {}

    def prepare(request):
        metadata = parse_git_tracking_metadata(
            request.plan_path.read_text(encoding="utf-8")
        )
        assert metadata is not None
        observed["branch"] = metadata.plan_branch
        observed["base"] = metadata.pre_handoff_base_head
        return _prepared(request)

    monkeypatch.setattr("aflow.daemon.prepare_startup", prepare)
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "rest-blank-git-tracking"},
        json={"plan_path": "plans/todo/test-plan.md", "workflow_name": "managed"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["result"]["status"] == "running"
    assert observed == {"branch": "main", "base": expected_head}
    assert len(units.start_calls) == 1


@pytest.mark.parametrize("failure", ["backup", "value"])
def test_unclassified_preallocation_failure_is_generic_and_redacted(
    control_client,
    failure: str,
) -> None:
    client, root, units, monkeypatch = control_client
    monkeypatch.setattr("aflow.workflow._workflow_requires_git_tracking", lambda *_: True)
    sentinel = f"private-preflight-{failure}"
    if failure == "backup":
        def reject_backup(*args, **kwargs):
            raise PermissionError(sentinel)

        monkeypatch.setattr("aflow.workflow._backup_original_plan", reject_backup)
    else:
        monkeypatch.setattr("aflow.workflow._backup_original_plan", lambda *args, **kwargs: None)

        def reject_value(*args, **kwargs):
            raise ValueError(sentinel)

        monkeypatch.setattr(
            "aflow.workflow._prepare_required_git_tracking_before_allocation",
            reject_value,
        )

    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": f"generic-preflight-{failure}"},
        json={
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": {"code": "operation_rejected"}}
    assert sentinel not in response.text
    assert units.start_calls == []
    assert not (root / ".aflow" / "launches").exists()
    assert not (root / ".aflow" / "runs").exists()


def test_reserved_plan_admission_failure_keeps_idempotent_run_identity(control_client) -> None:
    from aflow.api.startup import PLAN_ADMISSION_ERROR_CODE, PLAN_ADMISSION_SAFE_MESSAGE, PlanAdmissionError

    client, root, units, monkeypatch = control_client

    def reject(_request):
        raise PlanAdmissionError

    monkeypatch.setattr("aflow.daemon.prepare_startup", reject)
    request = {
        "plan_path": "plans/todo/test-plan.md",
        "workflow_name": "managed",
    }
    headers = {"Idempotency-Key": "reserved-plan-admission"}
    first = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers=headers,
        json=request,
    )

    assert first.status_code == 422, first.text
    first_detail = first.json()["detail"]
    run_id = first_detail["run_id"]
    assert first_detail == {
        "code": PLAN_ADMISSION_ERROR_CODE,
        "message": PLAN_ADMISSION_SAFE_MESSAGE,
        "run_id": run_id,
    }
    assert units.start_calls == []

    retry = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers=headers,
        json=request,
    )
    assert retry.status_code == 422, retry.text
    assert retry.json() == first.json()
    assert units.start_calls == []
    runs = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs"
    )
    assert runs.status_code == 200, runs.text
    assert [item["run_id"] for item in runs.json()["runs"]] == [run_id]

    status = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}"
    )
    assert status.status_code == 200, status.text
    payload = status.json()
    assert payload["status"] == "needs_attention"
    assert payload["reason"] == PLAN_ADMISSION_SAFE_MESSAGE
    assert payload["evidence"]["startup_failure"]["code"] == PLAN_ADMISSION_ERROR_CODE


def test_history_api_filters_deleted_links_replays_and_auth(control_client):
    client, root, _, _ = control_client
    directory = root / '.aflow' / 'runs' / 'legacy-history'
    directory.mkdir(parents=True)
    source = directory / 'run.json'
    source.write_text('{"status":"running"}')
    endpoint = f'/api/control-plane/projects/{PROJECT_ID}/runs/legacy-history'
    body = {'expected_revision': 0}
    headers = {'Idempotency-Key': 'archive-history'}
    response = client.post(endpoint + '/archive', json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert client.post(endpoint + '/archive', json=body, headers=headers).json() == response.json()
    assert client.get(endpoint).json()['history_state'] == 'archived'
    assert client.get(endpoint.rsplit('/', 1)[0]).json()['runs'] == []
    assert len(client.get(endpoint.rsplit('/', 1)[0] + '?history=archived').json()['runs']) == 1
    assert client.post(endpoint + '/restore', json=body, headers={'Idempotency-Key': 'stale'}).status_code == 409
    deleted = client.request('DELETE', endpoint, json={'expected_revision': 1}, headers={'Idempotency-Key': 'delete-history'})
    assert deleted.status_code == 200
    assert client.request('DELETE', endpoint, json={'expected_revision': 1}, headers={'Idempotency-Key': 'delete-history'}).json() == deleted.json()
    assert client.get(endpoint).status_code == 410
    assert client.get(endpoint + '/context').status_code == 410
    assert client.get(endpoint.rsplit('/', 1)[0] + '?history=all').json()['runs'] == []
    assert client.post(endpoint + '/restore', json={'expected_revision': 2}, headers={'Idempotency-Key': 'restore-deleted'}).status_code == 410
    assert client.post(endpoint.replace(PROJECT_ID, 'unregistered') + '/archive', json=body, headers=headers).status_code == 404
    assert source.read_text() == '{"status":"running"}'
    client.headers.pop('Authorization')
    assert client.post(endpoint + '/archive', json=body, headers=headers).status_code == 401


def test_history_active_acknowledgement_keeps_unit_running(control_client):
    from aflow.control_plane import LaunchManifest, create_launch_manifest
    client, root, units, _ = control_client
    create_launch_manifest(root, LaunchManifest(run_id='active-history', project_root=str(root), plan_path='plans/todo/test-plan.md', workflow_name='managed', max_turns=5))
    unit = 'aflow-run-active-history.service'
    units.start(unit, ('test',), cwd=root)
    endpoint = f'/api/control-plane/projects/{PROJECT_ID}/runs/active-history/archive'
    headers = {'Idempotency-Key': 'active-archive'}
    assert client.post(endpoint, json={'expected_revision': 0}, headers=headers).status_code == 422
    response = client.post(endpoint, json={'expected_revision': 0, 'acknowledge_active': True}, headers=headers)
    assert response.status_code == 200, response.text
    assert units.get(unit).is_active


def test_first_and_cached_history_reads_leave_all_artifact_bytes_unchanged(control_client):
    from aflow.control_plane import LaunchManifest, create_launch_manifest
    client, root, _, _ = control_client
    create_launch_manifest(root, LaunchManifest(run_id='read-only', project_root=str(root), plan_path='plans/todo/test-plan.md', workflow_name='managed', max_turns=5))
    directory = root / '.aflow' / 'runs' / 'read-only'
    directory.mkdir()
    (directory / 'run.json').write_text('{"status":"running"}')
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    for _ in range(2):
        detail = client.get(f'/api/control-plane/projects/{PROJECT_ID}/runs/read-only')
        listing = client.get(f'/api/control-plane/projects/{PROJECT_ID}/runs')
        assert detail.status_code == listing.status_code == 200
        assert detail.json()['status'] == 'needs_attention'
        assert listing.json()['runs'][0]['activity'] == 'unknown'
        assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_deleted_history_retains_original_launch_idempotency(control_client):
    from aflow_app_server import main
    client, root, units, monkeypatch = control_client
    started = _answer_pending(client, _start_pending(client, monkeypatch), monkeypatch)
    run_id = started['result']['run_id']
    before = {p: p.read_bytes() for p in (root / '.aflow').rglob('*') if p.is_file()}
    endpoint = f'/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}'
    deleted = client.request('DELETE', endpoint, json={'expected_revision': 0, 'acknowledge_active': True}, headers={'Idempotency-Key': 'delete-launched'})
    assert deleted.status_code == 200, deleted.text
    replay = client.post(f'/api/control-plane/projects/{PROJECT_ID}/runs', headers={'Idempotency-Key': 'start-1'}, json={'plan_path': 'plans/todo/test-plan.md', 'workflow_name': 'managed'})
    assert replay.status_code == 200, replay.text
    assert replay.json()['result']['run_id'] == run_id
    assert len(units.start_calls) == 1
    assert any(run.run_id == run_id for run in main._control_plane_service._project(PROJECT_ID).daemon.application.repository.list_runs().runs)
    assert all(p.read_bytes() == data for p, data in before.items())

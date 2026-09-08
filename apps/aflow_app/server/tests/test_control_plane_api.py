"""Contract tests for the authenticated daemon-backed REST control plane."""

from __future__ import annotations

from contextlib import contextmanager
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
from aflow.control_plane import CapabilitySet, ContextBundle, RunControlRequest, RunStatus, StartRunResult
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
    RunStatusResponse,
    StartRunResponse,
    canonical_contract_payloads,
)
from aflow_app_server.project_registry import (
    ProjectRegistry,
    ProjectRegistryError,
)
from aflow_app_server.global_config_service import GlobalConfigService
from aflow_app_server.plan_service import PlanService


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


def _start_pending(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup",
        lambda request: StartupQuestion(
            kind=StartupQuestionKind.PICK_STEP,
            message="Choose a step",
            choices=["implement"],
        ),
    )
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "start-1"},
        json={"plan_path": "plans/todo/test-plan.md", "workflow_name": "managed"},
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
    assert set(ContextResponse.model_fields) == set(payloads["context"])


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
        "RunControlPayload",
        "ContextResponse",
    }.issubset(schema["components"]["schemas"])


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
    unsafe = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        json={"expected_revision": 1, "unsafe_changes": {"workflow": "other"}},
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
    assert response.status_code == 422, response.text
    error = response.json()["detail"]
    assert error["code"] == "startup_failed", error
    assert "untracked-blocker.txt" in error["message"]
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

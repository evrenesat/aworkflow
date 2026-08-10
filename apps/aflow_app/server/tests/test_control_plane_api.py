"""Contract tests for the authenticated daemon-backed REST control plane."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
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
from aflow.control_plane.units import InMemoryUnitManager
from aflow.daemon import AflowDaemon
from aflow_app_server.config import ControlPlaneProjectConfig, ServerConfig
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
from aflow_app_server.project_catalog import ProjectCatalog


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
    (root / ".git").mkdir()
    plan = root / "plans" / "todo" / "test-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Test\n\n### [ ] Checkpoint 1: Test\n- [ ] step\n")
    second_plan = root / "plans" / "todo" / "second-plan.md"
    second_plan.write_text("# Second\n\n### [ ] Checkpoint 1: Test\n- [ ] step\n")
    config_path = root / "aflow.toml"
    _write_workflow_config(config_path)
    environment_file = root / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    executable = root / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    units = InMemoryUnitManager()

    project = ControlPlaneProjectConfig(
        id=PROJECT_ID,
        root=root,
        config_path=config_path,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
    )
    control_service = ControlPlaneService(
        (project,),
        daemon_factory=lambda config: AflowDaemon(config, units=units),
    )
    control_service.start()
    config = ServerConfig(
        bind_host="127.0.0.1",
        bind_port=8765,
        auth_token=TOKEN,
        repo_registry_path=tmp_path / "repos.json",
        codex_app_server_url=None,
        codex_app_server_token=None,
        transcription_url=None,
        transcription_token=None,
        projects_home=tmp_path / "code",
        project_overrides_path=tmp_path / "projects.json",
        attachment_root=tmp_path / "attachments",
        control_plane_projects=(project,),
    )
    main._config = config
    main._project_catalog = ProjectCatalog(
        config.projects_home,
        config.project_overrides_path,
        legacy_registry_path=config.repo_registry_path,
    )
    main._control_plane_service = control_service
    main._service = None
    main._planning_service = None
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        yield client, root, units, monkeypatch
    finally:
        main._config = None
        main._project_catalog = None
        main._control_plane_service = None
        main._service = None
        main._planning_service = None


def _prepared(request) -> PreparedRun:
    return PreparedRun(
        workflow_name="managed",
        repo_root=request.repo_root,
        plan_path=request.plan_path,
        config_path=request.config_path,
        max_turns=request.max_turns or 15,
        team=request.team,
        extra_instructions=(),
        start_step=request.start_step or "implement",
    )


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


def test_control_plane_reads_pending_start_and_idempotency(control_client) -> None:
    client, _, units, monkeypatch = control_client
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"ready": True, "projects": [PROJECT_ID]}
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
    assert resumed.status_code == 201
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

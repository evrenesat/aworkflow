"""Contract tests for the authenticated FastMCP control-plane adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from aflow.api.models import StartupQuestion, StartupQuestionKind
from aflow_app_server.main import app
from test_control_plane_api import PROJECT_ID, TOKEN, _prepared, control_client

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
}
EXPECTED_TOOL_NAMES = {
    "get_capabilities",
    "list_projects",
    "get_project_capabilities",
    "list_plans",
    "list_runs",
    "get_run",
    "get_run_events",
    "get_run_context",
    "start_run",
    "answer_startup",
    "control_run",
    "owner_stop",
    "resume_run",
}


def test_shared_and_fastapi_mcp_registries_have_identical_public_contract() -> None:
    from aflow.mcp_control_plane import create_control_plane_mcp as create_shared_mcp
    from aflow_app_server.mcp_adapter import create_control_plane_mcp as create_fastapi_mcp

    shared = create_shared_mcp(lambda: None)
    fastapi = create_fastapi_mcp(lambda: None)
    shared_tools = asyncio.run(shared.list_tools())
    fastapi_tools = asyncio.run(fastapi.list_tools())
    assert {tool.name for tool in shared_tools} == EXPECTED_TOOL_NAMES
    assert {
        tool.name: tool.to_mcp_tool().model_dump(mode="json") for tool in shared_tools
    } == {
        tool.name: tool.to_mcp_tool().model_dump(mode="json") for tool in fastapi_tools
    }
    shared_resources = asyncio.run(shared.list_resource_templates())
    fastapi_resources = asyncio.run(fastapi.list_resource_templates())
    assert {
        resource.uri_template: resource.parameters for resource in shared_resources
    } == {
        resource.uri_template: resource.parameters for resource in fastapi_resources
    }
    assert len(shared_resources) == 3


@pytest.fixture
def mcp_client(control_client, monkeypatch: pytest.MonkeyPatch):
    """Run the mounted MCP transport with the daemon fixture from REST tests."""
    from aflow_app_server import main

    _, root, units, _ = control_client
    config = main._config
    control_service = main._control_plane_service
    assert config is not None
    assert control_service is not None
    test_config = replace(
        config,
        planning_providers=(),
        default_planning_provider_id=None,
    )
    monkeypatch.setattr(main.ServerConfig, "from_env", classmethod(lambda _cls: test_config))
    monkeypatch.setattr(main, "ControlPlaneService", lambda _projects: control_service)

    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client, root, units, monkeypatch


def _mcp_request(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers=headers or MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mcp_tool(
    client: TestClient,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = _mcp_request(
        client,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
    )
    assert "error" not in response
    result = response["result"]
    assert result.get("isError") is not True
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    return json.loads(result["content"][0]["text"])


def test_mcp_stateless_http_auth_metadata_resources_and_rest_parity(mcp_client) -> None:
    client, _, _, _ = mcp_client
    initialization = _mcp_request(
        client,
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aflow-test", "version": "1"},
        },
    )
    assert initialization["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION

    tools = _mcp_request(client, "tools/list")["result"]["tools"]
    tool_by_name = {tool["name"]: tool for tool in tools}
    assert set(tool_by_name) == EXPECTED_TOOL_NAMES
    assert tool_by_name["list_projects"]["annotations"]["readOnlyHint"] is True
    assert tool_by_name["start_run"]["annotations"]["readOnlyHint"] is False
    assert tool_by_name["owner_stop"]["annotations"]["destructiveHint"] is True

    resources = _mcp_request(client, "resources/templates/list")["result"]["resourceTemplates"]
    assert {resource["uriTemplate"] for resource in resources} == {
        "aflow://projects/{project_id}/capabilities",
        "aflow://projects/{project_id}/runs/{run_id}",
        "aflow://projects/{project_id}/runs/{run_id}/context/lite",
    }
    capabilities = _mcp_tool(client, "get_capabilities")
    assert capabilities == client.get("/api/control-plane/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert _mcp_tool(client, "list_projects") == client.get(
        "/api/control-plane/projects",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    assert _mcp_tool(client, "list_plans", {"project_id": PROJECT_ID}) == client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/plans",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()

    missing = client.post(
        "/mcp",
        headers={**MCP_HEADERS, "Authorization": ""},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert missing.status_code == 401
    assert missing.json() == {"detail": {"code": "unauthorized"}}
    rejected = client.post(
        "/mcp?token=super-secret-token",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": {"code": "token_query_rejected"}}
    assert "super-secret-token" not in rejected.text
    disallowed_project = _mcp_request(
        client,
        "tools/call",
        {
            "name": "get_project_capabilities",
            "arguments": {"project_id": "super-secret-token"},
        },
    )
    assert disallowed_project["result"]["isError"] is True
    assert disallowed_project["result"]["content"][0]["text"] == "project_not_found"
    assert "super-secret-token" not in json.dumps(disallowed_project)
    token_argument = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "start_run",
                "arguments": {
                    "project_id": PROJECT_ID,
                    "plan_path": "plans/todo/test-plan.md",
                    "idempotency_key": "Bearer super-secret-token",
                },
            },
        },
    )
    assert token_argument.status_code == 400
    assert token_argument.json() == {"detail": {"code": "token_payload_rejected"}}
    assert "super-secret-token" not in token_argument.text
    token_resource = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {
                "uri": "aflow://projects/test-project/capabilities?token=super-secret-token"
            },
        },
    )
    assert token_resource.status_code == 400
    assert token_resource.json() == {"detail": {"code": "token_payload_rejected"}}
    assert "super-secret-token" not in token_resource.text


def test_mcp_startup_control_and_resume_are_idempotent_and_match_rest(mcp_client) -> None:
    client, root, units, monkeypatch = mcp_client
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup",
        lambda _request: StartupQuestion(
            kind=StartupQuestionKind.PICK_STEP,
            message="Choose a step",
            choices=["implement"],
        ),
    )
    pending = _mcp_tool(
        client,
        "start_run",
        {
            "project_id": PROJECT_ID,
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
            "idempotency_key": "mcp-start-1",
        },
    )
    replayed_pending = _mcp_tool(
        client,
        "start_run",
        {
            "project_id": PROJECT_ID,
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
            "idempotency_key": "mcp-start-1",
        },
    )
    assert pending == replayed_pending
    question = pending["startup_question"]
    assert question["kind"] == "pick_step"
    assert units.start_calls == []

    monkeypatch.setattr(
        "aflow.daemon.prepare_startup_with_answer",
        lambda _question, request, _answer: _prepared(request),
    )
    answered_response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/startup-answers/{question['question_id']}",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "mcp-answer-1"},
        json={"answer": "implement"},
    )
    assert answered_response.status_code == 200
    answered = answered_response.json()
    replayed_answer = _mcp_tool(
        client,
        "answer_startup",
        {
            "project_id": PROJECT_ID,
            "question_id": question["question_id"],
            "answer": "implement",
            "idempotency_key": "mcp-answer-1",
        },
    )
    result = answered["result"]
    assert result["status"] == "running"
    run_id = result["run_id"]
    assert replayed_answer["result"]["run_id"] == run_id
    assert replayed_answer["result"]["status"] == result["status"]
    assert len(units.start_calls) == 1
    assert _mcp_tool(client, "get_run", {"project_id": PROJECT_ID, "run_id": run_id}) == client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    assert _mcp_tool(client, "list_runs", {"project_id": PROJECT_ID}) == client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    assert _mcp_tool(
        client,
        "get_run_context",
        {"project_id": PROJECT_ID, "run_id": run_id},
    ) == client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/context",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()

    controlled = _mcp_tool(
        client,
        "control_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "expected_revision": 0,
            "max_turns": 3,
            "idempotency_key": "mcp-control-1",
        },
    )
    replayed_control = _mcp_tool(
        client,
        "control_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "expected_revision": 0,
            "max_turns": 3,
            "idempotency_key": "mcp-control-1",
        },
    )
    assert controlled == replayed_control
    assert controlled["revision"] == 1
    assert controlled["run"] == client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()

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
    resumed = _mcp_tool(
        client,
        "resume_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "idempotency_key": "mcp-resume-1",
        },
    )
    resumed_replay = _mcp_tool(
        client,
        "resume_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "idempotency_key": "mcp-resume-1",
        },
    )
    resumed_id = resumed["run_id"]
    assert resumed_replay["run_id"] == resumed_id
    assert resumed_replay["status"] == resumed["status"]
    assert resumed_id != run_id
    rest_events = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/events?limit=10",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    mcp_events = _mcp_tool(
        client,
        "get_run_events",
        {"project_id": PROJECT_ID, "run_id": run_id, "limit": 10},
    )
    assert mcp_events == rest_events
    assert any(
        event["event_type"] == "resume_requested"
        and event["data"]["continuation_run_id"] == resumed_id
        for event in mcp_events["events"]
    )
    stopped = _mcp_tool(
        client,
        "owner_stop",
        {
            "project_id": PROJECT_ID,
            "run_id": resumed_id,
            "expected_revision": 0,
            "idempotency_key": "mcp-owner-stop-1",
        },
    )
    assert stopped["launch_phase"] == "owner_stopped"


def test_mcp_client_template_is_secret_free_and_requires_write_approval() -> None:
    import tomllib

    template = Path(__file__).parents[1] / "aflow-control-plane.mcp.example.toml"
    raw = template.read_text()
    config = tomllib.loads(raw)
    server = config["mcp_servers"]["aflow_control_plane"]
    assert server == {
        "url": "http://100.103.69.9:8765/mcp",
        "required": False,
        "bearer_token_env_var": "AFLOW_CONTROL_PLANE_TOKEN",
        "default_tools_approval_mode": "writes",
        "tools": {
            "start_run": {"approval_mode": "approve"},
            "answer_startup": {"approval_mode": "approve"},
            "control_run": {"approval_mode": "approve"},
            "owner_stop": {"approval_mode": "approve"},
            "resume_run": {"approval_mode": "approve"},
        },
    }
    assert "Bearer " not in raw
    assert "super-secret-token" not in raw
    assert _validated_client_url(server["url"]) == server["url"]
    with pytest.raises(ValueError, match="credential"):
        _validated_client_url(f"{server['url']}?token=super-secret-token")


def _validated_client_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.query or parsed.username or parsed.password:
        raise ValueError("MCP URL may not contain a credential")
    return value

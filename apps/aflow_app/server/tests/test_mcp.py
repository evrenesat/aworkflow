"""Contract tests for the authenticated FastMCP control-plane adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from aflow.api.models import StartupQuestion, StartupQuestionKind
from aflow_app_server.main import app
from test_control_plane_api import (
    PROJECT_ID,
    TOKEN,
    _add_live_control_targets,
    _answer_pending as _rest_answer_pending,
    _commit_fixture_repository,
    _prepared,
    _recovery_payload,
    _unresolved_recovery_runtime,
    _seed_recovery_source,
    _seed_issue35_progress_fixture,
    _source_artifact_bytes,
    _start_pending as _rest_start_pending,
    control_client as _control_client_fixture,  # noqa: F401
)

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
}
CORE_TOOL_NAMES = {
    "get_capabilities",
    "list_projects",
    "get_project_capabilities",
    "list_plans",
    "list_runs",
    "get_run",
    "get_run_events",
    "get_run_context",
    "preflight_run",
    "start_run",
    "answer_startup",
    "control_run",
    "owner_stop",
    "resume_run",
}
AUTHORING_TOOL_NAMES = {
    "read_plan",
    "create_plan",
    "update_plan",
    "promote_plan",
    "list_plan_documents",
    "get_global_config",
    "patch_global_config",
}
EXPECTED_TOOL_NAMES = CORE_TOOL_NAMES | AUTHORING_TOOL_NAMES


def test_shared_and_fastapi_mcp_registries_have_identical_public_contract() -> None:
    from aflow.mcp_control_plane import create_control_plane_mcp as create_shared_mcp
    from aflow_app_server.mcp_adapter import create_control_plane_mcp as create_fastapi_mcp

    shared = create_shared_mcp(lambda: None)
    fastapi = create_fastapi_mcp(lambda: None)
    web = create_fastapi_mcp(
        lambda: None,
        get_plan_service=lambda: None,
        get_global_config_service=lambda: None,
    )
    shared_tools = asyncio.run(shared.list_tools())
    fastapi_tools = asyncio.run(fastapi.list_tools())
    web_tools = asyncio.run(web.list_tools())
    assert {tool.name for tool in shared_tools} == CORE_TOOL_NAMES
    assert {tool.name for tool in fastapi_tools} == CORE_TOOL_NAMES
    assert {tool.name for tool in web_tools} == EXPECTED_TOOL_NAMES
    assert {
        tool.name: tool.to_mcp_tool().model_dump(mode="json") for tool in shared_tools
    } == {
        tool.name: tool.to_mcp_tool().model_dump(mode="json") for tool in fastapi_tools
    }
    assert {
        tool.name: tool.to_mcp_tool().model_dump(mode="json")
        for tool in web_tools
        if tool.name in CORE_TOOL_NAMES
    } == {
        tool.name: tool.to_mcp_tool().model_dump(mode="json") for tool in shared_tools
    }
    web_tool_by_name = {tool.name: tool.to_mcp_tool() for tool in web_tools}
    assert web_tool_by_name["read_plan"].annotations.readOnlyHint is True
    assert web_tool_by_name["list_plan_documents"].annotations.idempotentHint is True
    for name in ("create_plan", "update_plan", "promote_plan"):
        assert web_tool_by_name[name].annotations.readOnlyHint is False
        assert web_tool_by_name[name].annotations.idempotentHint is False
    shared_resources = asyncio.run(shared.list_resource_templates())
    fastapi_resources = asyncio.run(fastapi.list_resource_templates())
    assert {
        resource.uri_template: resource.parameters for resource in shared_resources
    } == {
        resource.uri_template: resource.parameters for resource in fastapi_resources
    }
    assert len(shared_resources) == 3


@pytest.fixture
def mcp_client(_control_client_fixture, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    """Run the mounted MCP transport with the daemon fixture from REST tests."""
    from aflow_app_server import config as config_module
    from aflow_app_server import main

    _, root, units, _ = _control_client_fixture
    config = main._config
    control_service = main._control_plane_service
    assert config is not None
    assert control_service is not None
    test_config = replace(
        config,
        config_audit_path=root.parent / "config_audit.jsonl",
    )
    monkeypatch.setattr(main.ServerConfig, "from_env", classmethod(lambda _cls: test_config))
    monkeypatch.setattr(main, "ControlPlaneService", lambda _projects: control_service)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: root.parent / "global")

    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client, root, units, monkeypatch


def _mcp_request(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    path: str = "/mcp",
) -> dict[str, Any]:
    response = client.post(
        path,
        headers=headers or MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mcp_tool(
    client: TestClient,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
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


def _mcp_tool_error(
    client: TestClient,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    response = _mcp_request(
        client,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
    )
    assert "error" not in response
    result = response["result"]
    assert result.get("isError") is True
    return result["content"][0]["text"]


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
    assert tool_by_name["preflight_run"]["annotations"]["readOnlyHint"] is True
    assert tool_by_name["start_run"]["annotations"]["readOnlyHint"] is False
    assert tool_by_name["owner_stop"]["annotations"]["destructiveHint"] is True
    assert "safe boundary" in tool_by_name["control_run"]["description"]
    assert "does" in tool_by_name["control_run"]["description"]
    assert "interrupt" in tool_by_name["control_run"]["description"]
    assert "Immediately" in tool_by_name["owner_stop"]["description"]

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
    preflight_arguments = {
        "project_id": PROJECT_ID,
        "plan_path": "plans/todo/test-plan.md",
        "offset": 0,
        "limit": 1,
    }
    assert _mcp_tool(client, "preflight_run", preflight_arguments) == client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight",
        json={
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": None,
            "offset": 0,
            "limit": 1,
        },
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


def test_mcp_durable_recovery_matches_rest_and_discovery(mcp_client) -> None:
    from aflow_app_server import main

    client, root, units, monkeypatch = mcp_client
    service = main._control_plane_service
    assert service is not None
    source_id, source_dir, _daemon = _seed_recovery_source(
        service, root, monkeypatch
    )
    before = _source_artifact_bytes(source_dir)

    tools = _mcp_request(client, "tools/list")["result"]["tools"]
    resume_tool = next(tool for tool in tools if tool["name"] == "resume_run")
    assert "recovery" in resume_tool["inputSchema"]["properties"]
    assert "fresh provider session" in resume_tool["description"]
    assert "confirmed inactive" in resume_tool["description"]

    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/resume"
    arguments = {
        "project_id": PROJECT_ID,
        "run_id": source_id,
        "idempotency_key": "transport-recovery-1",
        "extra_instructions": ["transport recovery guidance"],
        "recovery": _recovery_payload(),
    }
    rest = client.post(
        endpoint,
        headers={"Idempotency-Key": arguments["idempotency_key"]},
        json={
            "extra_instructions": arguments["extra_instructions"],
            "recovery": arguments["recovery"],
        },
    )
    assert rest.status_code == 201, rest.text
    successor_id = rest.json()["run_id"]
    assert successor_id != source_id
    assert units.start_calls and len(units.start_calls) == 1

    mcp_replay = _mcp_tool(client, "resume_run", arguments)
    assert mcp_replay["run_id"] == successor_id
    assert mcp_replay["status"] == rest.json()["status"]
    assert mcp_replay["created"] is False

    rest_events = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{successor_id}/events"
    )
    assert rest_events.status_code == 200
    mcp_events = _mcp_tool(
        client,
        "get_run_events",
        {"project_id": PROJECT_ID, "run_id": successor_id},
    )
    assert mcp_events == rest_events.json()
    assert any(
        event["event_type"] == "recovery_requested"
        and event["data"]["source_run_id"] == source_id
        and event["data"]["target_selector"] == "codex.test"
        for event in mcp_events["events"]
    )

    changed_target = _mcp_request(
        client,
        "tools/call",
        {
            "name": "resume_run",
            "arguments": {
                **arguments,
                "recovery": _recovery_payload("codex.other"),
            },
        },
    )
    assert changed_target["result"]["isError"] is True
    assert changed_target["result"]["content"][0]["text"] == "idempotency_conflict"

    invalid_recovery = {
        "mode": "durable_evidence",
        "worker_selector": "codex.test",
        "run_state_path": "/tmp/run.json",
        "provider_session_id": "source-session",
    }
    rest_invalid = client.post(
        endpoint,
        headers={"Idempotency-Key": "transport-invalid-1"},
        json={"recovery": invalid_recovery},
    )
    assert rest_invalid.status_code == 422
    rest_detail = rest_invalid.json()["detail"]
    assert rest_detail["code"] == "recovery_target_invalid"
    assert "target worker selector" in rest_detail["message"]
    mcp_invalid = _mcp_request(
        client,
        "tools/call",
        {
            "name": "resume_run",
            "arguments": {
                "project_id": PROJECT_ID,
                "run_id": source_id,
                "idempotency_key": "transport-invalid-2",
                "recovery": invalid_recovery,
            },
        },
    )
    assert mcp_invalid["result"]["isError"] is True
    mcp_detail = json.loads(mcp_invalid["result"]["content"][0]["text"])
    assert mcp_detail == rest_detail

    wrong_rest = client.post(
        endpoint.replace(PROJECT_ID, "not-allowed"),
        headers={"Idempotency-Key": "transport-wrong-project-rest"},
        json={"recovery": _recovery_payload()},
    )
    assert wrong_rest.status_code == 404
    assert wrong_rest.json() == {"detail": {"code": "project_not_found"}}
    wrong_mcp = _mcp_request(
        client,
        "tools/call",
        {
            "name": "resume_run",
            "arguments": {
                "project_id": "not-allowed",
                "run_id": source_id,
                "idempotency_key": "transport-wrong-project-mcp",
                "recovery": _recovery_payload(),
            },
        },
    )
    assert wrong_mcp["result"]["isError"] is True
    assert wrong_mcp["result"]["content"][0]["text"] == "project_not_found"

    unauthenticated = client.post(
        "/mcp",
        headers={**MCP_HEADERS, "Authorization": ""},
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": {"code": "unauthorized"}}
    assert len(units.start_calls) == 1
    assert _source_artifact_bytes(source_dir) == before


@pytest.mark.parametrize(
    "worker_evidence",
    ({"active": True}, None),
    ids=("active-source", "unknown-source"),
)
def test_mcp_and_rest_recovery_reject_active_or_unknown_source(
    mcp_client,
    worker_evidence,
) -> None:
    from aflow_app_server import main

    client, root, units, monkeypatch = mcp_client
    service = main._control_plane_service
    assert service is not None
    source_id, source_dir, _daemon = _seed_recovery_source(
        service,
        root,
        monkeypatch,
        source_id=f"{worker_evidence is None and 'unknown' or 'active'}-recovery-source",
        worker_evidence=worker_evidence,
    )
    before = _source_artifact_bytes(source_dir)
    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/resume"
    rest = client.post(
        endpoint,
        headers={"Idempotency-Key": "recovery-admission-rest"},
        json={"recovery": _recovery_payload()},
    )
    assert rest.status_code == 422
    rest_detail = rest.json()["detail"]
    assert rest_detail["code"] == "recovery_source_activity"
    assert "source activity" in rest_detail["message"]

    mcp = _mcp_request(
        client,
        "tools/call",
        {
            "name": "resume_run",
            "arguments": {
                "project_id": PROJECT_ID,
                "run_id": source_id,
                "idempotency_key": "recovery-admission-mcp",
                "recovery": _recovery_payload(),
            },
        },
    )
    assert mcp["result"]["isError"] is True
    assert json.loads(mcp["result"]["content"][0]["text"]) == rest_detail
    assert units.start_calls == []
    assert _source_artifact_bytes(source_dir) == before


@pytest.mark.parametrize(
    ("operation_state", "malformed"),
    (("in_flight", False), ("pending", False), ("in_flight", True)),
    ids=("in-flight", "pending", "malformed"),
)
def test_mcp_and_rest_ordinary_resume_reject_unresolved_recovery_runtime(
    mcp_client,
    operation_state: str,
    malformed: bool,
) -> None:
    from aflow_app_server import main

    client, root, units, monkeypatch = mcp_client
    service = main._control_plane_service
    assert service is not None
    source_id, source_dir, _daemon = _seed_recovery_source(
        service,
        root,
        monkeypatch,
        source_id=(
            "mcp-ordinary-inflight-recovery-source"
            if not malformed
            else "mcp-ordinary-malformed-recovery-source"
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
    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_id}/resume"
    rest = client.post(
        endpoint,
        headers={"Idempotency-Key": f"mcp-ordinary-rest-{malformed}"},
        json={},
    )
    assert rest.status_code == 422
    rest_detail = rest.json()["detail"]
    assert rest_detail["code"] == "recovery_operation_unresolved"

    mcp = _mcp_request(
        client,
        "tools/call",
        {
            "name": "resume_run",
            "arguments": {
                "project_id": PROJECT_ID,
                "run_id": source_id,
                "idempotency_key": f"mcp-ordinary-mcp-{malformed}",
            },
        },
    )
    assert mcp["result"]["isError"] is True
    assert json.loads(mcp["result"]["content"][0]["text"]) == rest_detail
    assert units.start_calls == []
    assert _source_artifact_bytes(source_dir) == before


def test_mcp_run_context_progress_matches_authenticated_rest(mcp_client) -> None:
    client, root, _, _ = mcp_client
    fixture = _seed_issue35_progress_fixture(root)
    run_id = fixture["run_id"]
    overlay = fixture["overlay"]
    assert isinstance(run_id, str)
    assert isinstance(overlay, Path)
    assert overlay.is_file()
    assert not (root / overlay.name).exists()

    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/context"
    rest_response = client.get(
        endpoint,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert rest_response.status_code == 200, rest_response.text
    rest_context = rest_response.json()
    mcp_context = _mcp_tool(
        client,
        "get_run_context",
        {"project_id": PROJECT_ID, "run_id": run_id},
    )

    assert mcp_context == rest_context
    assert rest_context["data"]["progress"] == {
        "availability": "available",
        "checkpoint": {"index": 4, "name": "Checkpoint 4: Stage 4"},
        "total": 14,
        "complete": False,
        "repairing": True,
        "overlay_path": str(overlay),
        "reason": None,
        "last_finished_turn": {
            "turn_number": 3,
            "step": "review",
            "status": "completed",
            "summary": "exit 0: review approved",
        },
        "current_turn": {
            "turn_number": 4,
            "step": "implement",
            "status": "starting",
            "summary": None,
        },
    }


def test_mcp_and_rest_context_ignore_agent_transcript_stop(mcp_client) -> None:
    client, root, _, _ = mcp_client
    plan = root / "plans" / "in-progress" / "semantic-stop-context.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "# Semantic stop context\n\n"
        "### [x] Checkpoint 1: Completed\n"
        "- [x] completed step\n",
        encoding="utf-8",
    )
    run_id = "semantic-stop-context"
    run_dir = root / ".aflow" / "runs" / run_id
    turn_dir = run_dir / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "repo_root": str(root),
                "plan_path": str(plan),
                "original_plan_path": str(plan),
                "active_plan_path": str(plan),
                "workflow_name": "managed",
                "current_step_name": "review",
                "turns_completed": 1,
                "end_reason": "done",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (turn_dir / "result.json").write_text(
        json.dumps(
            {
                "turn_number": 1,
                "step_name": "review",
                "step_role": "implementer",
                "status": "completed",
                "returncode": 0,
                "output_contract": "agent",
                "chosen_transition": "END",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (turn_dir / "stdout.txt").write_text(
        "approved final response\n", encoding="utf-8"
    )
    stderr = (
        "tool transcript: read an old review artifact\n"
        "AFLOW_STOP: HISTORY: old persisted tool output\n"
    )
    stderr_path = turn_dir / "stderr.txt"
    stderr_path.write_text(stderr, encoding="utf-8")
    stderr_before = stderr_path.read_bytes()

    endpoint = f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/context"
    rest_context = client.get(
        endpoint,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert rest_context.status_code == 200, rest_context.text
    rest_payload = rest_context.json()
    mcp_payload = _mcp_tool(
        client,
        "get_run_context",
        {"project_id": PROJECT_ID, "run_id": run_id},
    )

    assert mcp_payload == rest_payload
    manager_context = rest_payload["data"]["manager_context"]
    assert manager_context["finished_turn"]["detected_stop"] == []
    assert manager_context["finished_turn"]["status"] == "completed"
    assert manager_context["finished_turn"]["raw_artifacts"][1]["byte_size"] == len(stderr_before)
    assert stderr_path.read_bytes() == stderr_before


def test_mcp_trailing_slash_mount_supports_discovery_resource_read_and_header_auth(
    mcp_client,
) -> None:
    client, _, _, _ = mcp_client
    tools = _mcp_request(client, "tools/list", path="/mcp/")["result"]["tools"]
    assert {tool["name"] for tool in tools} == EXPECTED_TOOL_NAMES

    resources = _mcp_request(
        client,
        "resources/templates/list",
        path="/mcp/",
    )["result"]["resourceTemplates"]
    assert len(resources) == 3

    resource_uri = "aflow://projects/test-project/capabilities"
    resource_read = _mcp_request(
        client,
        "resources/read",
        {"uri": resource_uri},
        path="/mcp/",
    )
    content = resource_read["result"]["contents"][0]
    assert content["uri"] == resource_uri
    assert content["text"]

    unauthorized = client.post(
        "/mcp/",
        headers={**MCP_HEADERS, "Authorization": ""},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json() == {"detail": {"code": "unauthorized"}}


def test_mcp_global_config_discovery_and_snapshot_match_rest(mcp_client) -> None:
    client, _, _, _ = mcp_client
    browser = client.get("/api/config")
    assert browser.status_code == 200

    tools = _mcp_request(client, "tools/list")["result"]["tools"]
    tool_by_name = {tool["name"]: tool for tool in tools}
    patch_tool = tool_by_name["patch_global_config"]
    payload_schema = patch_tool["inputSchema"]["properties"]["payload"]
    assert payload_schema["additionalProperties"] is False
    assert set(payload_schema["properties"]) == {
        "expected_revision",
        "actions",
        "documents",
    }
    documents_schema = payload_schema["properties"]["documents"]["anyOf"][0]
    assert documents_schema["propertyNames"]["enum"] == [
        "aflow.toml",
        "workflows.toml",
    ]
    assert patch_tool["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert tool_by_name["get_global_config"]["annotations"]["readOnlyHint"] is True
    assert "all registered projects" in tool_by_name["get_global_config"]["description"]
    assert "safe boundary" in tool_by_name["get_global_config"]["description"]

    snapshot = _mcp_tool(client, "get_global_config")
    assert snapshot == browser.json()
    assert set(snapshot) == {
        "project_id",
        "revision",
        "documents",
        "aflow_toml",
        "workflows_toml",
        "validation",
    }
    assert snapshot["documents"] == ["aflow.toml", "workflows.toml"]
    assert "auth_token" not in json.dumps(snapshot)
    assert "server.toml" not in json.dumps(snapshot)


def test_mcp_global_config_uses_atomic_typed_patch_and_cross_transport_cas(
    mcp_client,
) -> None:
    client, root, _, _ = mcp_client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    before = _mcp_tool(client, "get_global_config")
    typed = _mcp_tool(
        client,
        "patch_global_config",
        {
            "payload": {
                "expected_revision": before["revision"],
                "actions": [
                    {
                        "type": "upsert_profile",
                        "harness": "codex",
                        "profile": "mcp-team",
                        "model": "mcp-model",
                        "effort": "medium",
                    },
                    {"type": "add_team", "team": "mcp-team"},
                    {
                        "type": "set_team_role",
                        "team": "mcp-team",
                        "role": "worker",
                        "selector": "codex.mcp-team",
                    },
                ],
            }
        },
    )
    assert typed["revision"] != before["revision"]
    assert 'model = "mcp-model"' in typed["aflow_toml"]
    assert "mcp-team" in typed["aflow_toml"]
    assert client.get("/api/config", headers=headers).json() == typed

    audit_path = root.parent / "config_audit.jsonl"
    audit_records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert audit_records[-1]["caller_scope"] == "mcp"
    assert audit_records[-1]["outcome"] == "saved"

    exact_aflow = typed["aflow_toml"].replace(
        'model = "mcp-model"', 'model = "mcp-model-exact"'
    )
    exact = _mcp_tool(
        client,
        "patch_global_config",
        {
            "payload": {
                "expected_revision": typed["revision"],
                "documents": {"aflow.toml": exact_aflow},
            }
        },
    )
    assert exact["aflow_toml"] == exact_aflow
    assert exact["workflows_toml"] == typed["workflows_toml"]
    assert client.get("/api/config", headers=headers).json() == exact

    audit_before_noop = audit_path.read_text()
    noop = _mcp_tool(
        client,
        "patch_global_config",
        {
            "payload": {
                "expected_revision": exact["revision"],
                "documents": {"aflow.toml": exact["aflow_toml"]},
            }
        },
    )
    assert noop == exact
    assert audit_path.read_text() == audit_before_noop

    browser_aflow = exact["aflow_toml"].replace(
        'model = "mcp-model-exact"', 'model = "browser-model"'
    )
    browser = client.patch(
        "/api/config",
        headers=headers,
        json={
            "expected_revision": exact["revision"],
            "documents": {"aflow.toml": browser_aflow},
        },
    )
    assert browser.status_code == 200, browser.text
    stale = _mcp_tool_error(
        client,
        "patch_global_config",
        {
            "payload": {
                "expected_revision": exact["revision"],
                "documents": {"aflow.toml": exact["aflow_toml"]},
            }
        },
    )
    assert stale == "revision_conflict"
    assert client.get("/api/config", headers=headers).json() == browser.json()


def test_mcp_global_config_rejects_invalid_pairs_and_unscoped_documents(
    mcp_client,
) -> None:
    client, _, _, _ = mcp_client
    current = _mcp_tool(client, "get_global_config")

    invalid = _mcp_tool_error(
        client,
        "patch_global_config",
        {
            "payload": {
                "expected_revision": current["revision"],
                "documents": {"workflows.toml": "[invalid"},
            }
        },
    )
    assert invalid == "operation_rejected"
    assert _mcp_tool(client, "get_global_config") == current

    mixed = _mcp_request(
        client,
        "tools/call",
        {
            "name": "patch_global_config",
            "arguments": {
                "payload": {
                    "expected_revision": current["revision"],
                    "actions": [
                        {
                            "type": "set_max_turns",
                            "value": 5,
                        }
                    ],
                    "documents": {"aflow.toml": current["aflow_toml"]},
                }
            },
        },
    )
    assert mixed["result"]["isError"] is True

    extra_document = _mcp_request(
        client,
        "tools/call",
        {
            "name": "patch_global_config",
            "arguments": {
                "payload": {
                    "expected_revision": current["revision"],
                    "documents": {
                        "server.toml": "server-secret",
                    },
                }
            },
        },
    )
    assert extra_document["result"]["isError"] is True
    assert _mcp_tool(client, "get_global_config") == current


def test_mcp_authored_journey_reaches_existing_launch_service(mcp_client) -> None:
    client, root, units, monkeypatch = mcp_client
    headers = {"Authorization": f"Bearer {TOKEN}"}

    discovered = _mcp_request(client, "tools/list")["result"]["tools"]
    assert {tool["name"] for tool in discovered} == EXPECTED_TOOL_NAMES

    settings = _mcp_tool(client, "get_global_config")
    team = "mcp-journey"
    edited_settings = _mcp_tool(
        client,
        "patch_global_config",
        {
            "payload": {
                "expected_revision": settings["revision"],
                "actions": [
                    {"type": "add_team", "team": team},
                    {
                        "type": "set_team_role",
                        "team": team,
                        "role": "worker",
                        "selector": "codex.test",
                    },
                ],
            }
        },
    )
    assert client.get("/api/config", headers=headers).json() == edited_settings

    name = "mcp-journey.md"
    draft = "# MCP journey\n\n### [ ] Checkpoint 1: Journey\n- [ ] launch\n"
    created = _mcp_tool(
        client,
        "create_plan",
        {"project_id": PROJECT_ID, "name": name, "content": draft},
    )
    assert client.get(
        f"/api/projects/{PROJECT_ID}/plans/todo/{name}", headers=headers
    ).json() == created
    assert _mcp_tool(
        client,
        "read_plan",
        {"project_id": PROJECT_ID, "plan_status": "todo", "name": name},
    ) == created

    updated_content = draft.replace(
        "- [ ] launch", "- [x] launch\n\nSelected team: mcp-journey"
    )
    updated = _mcp_tool(
        client,
        "update_plan",
        {
            "project_id": PROJECT_ID,
            "plan_status": "todo",
            "name": name,
            "content": updated_content,
            "expected_revision": created["revision"],
        },
    )
    assert client.get(
        f"/api/projects/{PROJECT_ID}/plans/todo/{name}", headers=headers
    ).json() == updated

    promoted = _mcp_tool(
        client,
        "promote_plan",
        {
            "project_id": PROJECT_ID,
            "plan_status": "todo",
            "name": name,
            "expected_revision": updated["revision"],
        },
    )
    assert client.get(
        f"/api/projects/{PROJECT_ID}/plans/in_progress/{name}", headers=headers
    ).json() == promoted

    document_list = _mcp_tool(
        client,
        "list_plan_documents",
        {"project_id": PROJECT_ID, "plan_status": "in_progress"},
    )
    assert document_list["plans"] == [
        {
            key: promoted[key]
            for key in ("project_id", "name", "path", "status", "revision", "size_bytes")
        }
    ]
    lifecycle_list = _mcp_tool(client, "list_plans", {"project_id": PROJECT_ID})
    assert any(
        plan["path"] == promoted["path"] and plan["status"] == "in_progress"
        for plan in lifecycle_list["plans"]
    )

    observed: dict[str, object] = {}

    def prepare(request):
        observed["request"] = request
        return _prepared(request)

    monkeypatch.setattr("aflow.daemon.prepare_startup", prepare)
    started = _mcp_tool(
        client,
        "start_run",
        {
            "project_id": PROJECT_ID,
            "plan_path": promoted["path"],
            "workflow_name": "managed",
            "team": team,
            "start_step": "implement",
            "idempotency_key": "mcp-authored-journey",
        },
    )
    assert started["result"]["status"] == "running"
    request = observed["request"]
    assert request.plan_path == root / promoted["path"]
    assert request.workflow_name == "managed"
    assert request.team == team
    assert units.start_calls
    run = _mcp_tool(
        client,
        "get_run",
        {"project_id": PROJECT_ID, "run_id": started["result"]["run_id"]},
    )
    assert run["plan_path"] == str(root / promoted["path"])
    assert run["team"] == team


def test_mcp_plan_authoring_matches_rest_and_preserves_stale_bytes(mcp_client) -> None:
    client, root, _, _ = mcp_client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    plans_path = f"/api/projects/{PROJECT_ID}/plans"
    name = "mcp-authoring.md"

    created = _mcp_tool(
        client,
        "create_plan",
        {"project_id": PROJECT_ID, "name": name, "content": "# MCP draft\n"},
    )
    browser_read = client.get(f"{plans_path}/todo/{name}", headers=headers)
    assert browser_read.status_code == 200
    assert browser_read.json() == created
    assert _mcp_tool(
        client,
        "read_plan",
        {"project_id": PROJECT_ID, "plan_status": "todo", "name": name},
    ) == browser_read.json()

    browser_update = client.put(
        f"{plans_path}/todo/{name}",
        headers=headers,
        json={
            "content": "# Browser edit\n",
            "expected_revision": created["revision"],
        },
    )
    assert browser_update.status_code == 200
    browser_document = browser_update.json()

    stale = _mcp_tool_error(
        client,
        "update_plan",
        {
            "project_id": PROJECT_ID,
            "plan_status": "todo",
            "name": name,
            "content": "# Stale MCP edit\n",
            "expected_revision": created["revision"],
        },
    )
    assert stale == "revision_conflict"
    assert client.get(f"{plans_path}/todo/{name}", headers=headers).json() == browser_document
    assert (root / "plans" / "todo" / name).read_text(encoding="utf-8") == "# Browser edit\n"

    updated = _mcp_tool(
        client,
        "update_plan",
        {
            "project_id": PROJECT_ID,
            "plan_status": "todo",
            "name": name,
            "content": "# MCP replacement\n",
            "expected_revision": browser_document["revision"],
        },
    )
    assert client.get(f"{plans_path}/todo/{name}", headers=headers).json() == updated

    documents = _mcp_tool(
        client,
        "list_plan_documents",
        {"project_id": PROJECT_ID, "plan_status": "todo"},
    )
    browser_documents = client.get(
        plans_path,
        params={"status": "todo"},
        headers=headers,
    )
    assert browser_documents.status_code == 200
    assert documents == {"plans": browser_documents.json()}

    promoted = _mcp_tool(
        client,
        "promote_plan",
        {
            "project_id": PROJECT_ID,
            "plan_status": "todo",
            "name": name,
            "expected_revision": updated["revision"],
        },
    )
    promoted_from_browser = client.get(
        f"{plans_path}/in_progress/{name}",
        headers=headers,
    )
    assert promoted_from_browser.status_code == 200
    assert promoted_from_browser.json() == promoted
    history = client.get(
        f"{plans_path}/in_progress/{name}/backups",
        headers=headers,
    )
    assert history.status_code == 200
    assert history.json()["backups"][0]["baseline_status"] == "known"
    assert history.json()["backups"][0]["capture_event"] == "ready_promotion"


def test_mcp_plan_authoring_uses_default_template_and_safe_errors(mcp_client) -> None:
    from importlib.resources import files as resource_files

    client, root, _, _ = mcp_client
    template = resource_files("aflow").joinpath("templates/draft-plan.md").read_text(
        encoding="utf-8"
    )
    created = _mcp_tool(
        client,
        "create_plan",
        {"project_id": PROJECT_ID, "name": "template.md", "content": None},
    )
    assert created["content"] == template

    assert _mcp_tool_error(
        client,
        "read_plan",
        {"project_id": PROJECT_ID, "plan_status": "todo", "name": "missing.md"},
    ) == "plan_not_found"
    assert _mcp_tool_error(
        client,
        "read_plan",
        {"project_id": "not-registered", "plan_status": "todo", "name": "x.md"},
    ) == "project_not_found"
    assert _mcp_tool_error(
        client,
        "create_plan",
        {"project_id": PROJECT_ID, "name": "../escape.md", "content": "x"},
    ) == "invalid_plan"
    assert _mcp_tool_error(
        client,
        "create_plan",
        {"project_id": PROJECT_ID, "name": "nul.md", "content": "bad\x00text"},
    ) == "invalid_plan"
    document_body = "document-body-secret"
    invalid_content = _mcp_request(
        client,
        "tools/call",
        {
            "name": "create_plan",
            "arguments": {
                "project_id": PROJECT_ID,
                "name": "invalid-body.md",
                "content": f"{document_body}\x00",
            },
        },
    )
    assert invalid_content["result"]["isError"] is True
    assert invalid_content["result"]["content"][0]["text"] == "invalid_plan"
    assert document_body not in json.dumps(invalid_content)

    duplicate_name = "duplicate.md"
    _mcp_tool(
        client,
        "create_plan",
        {"project_id": PROJECT_ID, "name": duplicate_name, "content": "first"},
    )
    assert _mcp_tool_error(
        client,
        "create_plan",
        {"project_id": PROJECT_ID, "name": duplicate_name, "content": "second"},
    ) == "plan_exists"

    outside = root / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    linked = root / "plans" / "todo" / "linked.md"
    linked.symlink_to(outside)
    assert _mcp_tool_error(
        client,
        "read_plan",
        {"project_id": PROJECT_ID, "plan_status": "todo", "name": "linked.md"},
    ) == "invalid_plan"

    invalid_status = _mcp_request(
        client,
        "tools/call",
        {
            "name": "read_plan",
            "arguments": {
                "project_id": PROJECT_ID,
                "plan_status": "unsafe",
                "name": "document-body-secret.md",
            },
        },
    )
    assert invalid_status["result"]["isError"] is True
    assert "document-body-secret.md" not in invalid_status["result"]["content"][0]["text"]


def test_mcp_start_normalizes_blank_git_tracking_before_launch(mcp_client) -> None:
    from aflow.plan import parse_git_tracking_metadata

    client, root, units, monkeypatch = mcp_client
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
    started = _mcp_tool(
        client,
        "start_run",
        {
            "project_id": PROJECT_ID,
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
            "idempotency_key": "mcp-blank-git-tracking",
        },
    )

    assert started["result"]["status"] == "running"
    assert observed == {"branch": "main", "base": expected_head}
    assert len(units.start_calls) == 1


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

    boundary = _mcp_tool(
        client,
        "control_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "expected_revision": 1,
            "owner_stop": True,
            "idempotency_key": "mcp-boundary-stop-1",
        },
    )
    assert boundary["revision"] == 2
    assert boundary["owner_stop"] is True
    assert boundary["run"]["status"] in {"running", "launch_started"}
    assert units.stop_calls == []
    assert _mcp_tool(
        client,
        "control_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "expected_revision": 1,
            "owner_stop": True,
            "idempotency_key": "mcp-boundary-stop-1",
        },
    ) == boundary
    assert boundary["run"] == client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    assert _mcp_tool_error(
        client,
        "control_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "expected_revision": 1,
            "owner_stop": True,
            "idempotency_key": "mcp-boundary-stop-stale",
        },
    ) == "revision_conflict"

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
    assert units.stop_calls[-1] == f"aflow-run-{resumed_id}.service"

    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)
    successor = _mcp_tool(
        client,
        "start_run",
        {
            "project_id": PROJECT_ID,
            "plan_path": "plans/todo/second-plan.md",
            "workflow_name": "managed",
            "start_step": "1",
            "max_turns": 4,
            "extra_instructions": ["mcp-runtime-guidance"],
            "restarted_from_run_id": resumed_id,
            "idempotency_key": "mcp-successor-1",
        },
    )
    assert successor["result"]["restarted_from_run_id"] == resumed_id
    assert (
        units.start_calls[-1][1][-1]
        == "--extra-instruction=mcp-runtime-guidance"
    )


def test_mcp_reserved_plan_admission_failure_preserves_safe_identity(mcp_client) -> None:
    from aflow.api.startup import (
        PLAN_ADMISSION_ERROR_CODE,
        PLAN_ADMISSION_SAFE_MESSAGE,
        PlanAdmissionError,
    )

    client, _, units, monkeypatch = mcp_client

    def reject(_request):
        raise PlanAdmissionError

    monkeypatch.setattr("aflow.daemon.prepare_startup", reject)
    arguments = {
        "project_id": PROJECT_ID,
        "plan_path": "plans/todo/test-plan.md",
        "workflow_name": "managed",
        "idempotency_key": "mcp-reserved-plan-admission",
    }
    first = _mcp_request(
        client,
        "tools/call",
        {"name": "start_run", "arguments": arguments},
    )
    assert first["result"]["isError"] is True
    first_detail = json.loads(first["result"]["content"][0]["text"])
    run_id = first_detail["run_id"]
    assert first_detail == {
        "code": PLAN_ADMISSION_ERROR_CODE,
        "message": PLAN_ADMISSION_SAFE_MESSAGE,
        "run_id": run_id,
    }

    retry = _mcp_request(
        client,
        "tools/call",
        {"name": "start_run", "arguments": arguments},
    )
    assert retry["result"]["isError"] is True
    assert json.loads(retry["result"]["content"][0]["text"]) == first_detail
    assert units.start_calls == []

    status = client.get(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}"
    )
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "needs_attention"
    assert status.json()["reason"] == PLAN_ADMISSION_SAFE_MESSAGE


@pytest.mark.parametrize(
    ("extra_instructions", "expected"),
    (
        ("omitted", ("saved guidance",)),
        (None, ("saved guidance",)),
        (["replacement guidance"], ("replacement guidance",)),
        ([], ()),
    ),
)
def test_mcp_resume_extra_instructions_inherit_replace_and_clear(
    mcp_client,
    extra_instructions: object,
    expected: tuple[str, ...],
) -> None:
    client, root, units, monkeypatch = mcp_client
    pending = _rest_start_pending(client, monkeypatch, ["saved guidance"])
    started = _rest_answer_pending(client, pending, monkeypatch)
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
    arguments = {
        "project_id": PROJECT_ID,
        "run_id": run_id,
        "idempotency_key": "mcp-resume-instructions",
    }
    if extra_instructions != "omitted":
        arguments["extra_instructions"] = extra_instructions
    resumed = _mcp_tool(client, "resume_run", arguments)
    successor_id = resumed["run_id"]
    assert len(units.start_calls) == 2
    argv = units.start_calls[-1][1]
    if expected:
        assert f"--extra-instruction={expected[0]}" in argv
    else:
        assert not any(argument.startswith("--extra-instruction=") for argument in argv)
    assert bootstrap_calls[0]["extra_instructions_provided"] == (
        extra_instructions != "omitted" and extra_instructions is not None
    )
    assert source_path.read_bytes() == before

    replay = _mcp_tool(client, "resume_run", arguments)
    assert replay["run_id"] == successor_id
    changed = dict(arguments)
    changed["extra_instructions"] = ["different guidance"]
    conflict = _mcp_request(
        client,
        "tools/call",
        {"name": "resume_run", "arguments": changed},
    )
    assert conflict["result"]["isError"] is True
    assert conflict["result"]["content"][0]["text"] == "idempotency_conflict"
    assert len(units.start_calls) == 2
    assert source_path.read_bytes() == before


def test_mcp_resume_rejects_invalid_extra_instructions_without_reserving(
    mcp_client,
) -> None:
    client, root, units, monkeypatch = mcp_client
    pending = _rest_start_pending(client, monkeypatch, ["saved guidance"])
    started = _rest_answer_pending(client, pending, monkeypatch)
    run_id = started["result"]["run_id"]
    units.stop(f"aflow-run-{run_id}.service")
    source_path = root / ".aflow" / "runs" / run_id / "run.json"
    source_path.write_text(
        '{"status":"running","workflow_name":"managed","team":null,'
        '"selected_start_step":"implement","max_turns":3,'
        '"extra_instructions":["saved guidance"]}'
    )
    before = source_path.read_bytes()
    rejected = _mcp_request(
        client,
        "tools/call",
        {
            "name": "resume_run",
            "arguments": {
                "project_id": PROJECT_ID,
                "run_id": run_id,
                "idempotency_key": "mcp-resume-invalid",
                "extra_instructions": [""],
            },
        },
    )
    assert rejected["result"]["isError"] is True
    assert rejected["result"]["content"][0]["text"] == "operation_rejected"
    assert len(units.start_calls) == 1
    assert source_path.read_bytes() == before


def test_mcp_control_uses_live_targets_and_matches_rest_validation(mcp_client) -> None:
    client, root, _, monkeypatch = mcp_client
    config_path = root.parent / "global" / "aflow.toml"
    _add_live_control_targets(config_path)
    capabilities = _mcp_tool(
        client,
        "get_project_capabilities",
        {"project_id": PROJECT_ID},
    )
    assert "new" in capabilities["teams"]
    assert "reasonix.new" in capabilities["admitted_role_selectors"]["worker"]

    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)
    started = _mcp_tool(
        client,
        "start_run",
        {
            "project_id": PROJECT_ID,
            "plan_path": "plans/todo/test-plan.md",
            "workflow_name": "managed",
            "idempotency_key": "mcp-live-start-1",
        },
    )
    run_id = started["result"]["run_id"]
    accepted = _mcp_tool(
        client,
        "control_run",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "expected_revision": 0,
            "team": "new",
            "role_selectors": {"worker": "reasonix.new"},
            "idempotency_key": "mcp-live-control-1",
        },
    )
    assert accepted["revision"] == 1

    rejected_mcp = _mcp_request(
        client,
        "tools/call",
        {
            "name": "control_run",
            "arguments": {
                "project_id": PROJECT_ID,
                "run_id": run_id,
                "expected_revision": 1,
                "role_selectors": {"worker": "reasonix.missing"},
                "idempotency_key": "mcp-live-control-invalid",
            },
        },
    )
    assert rejected_mcp["result"]["isError"] is True
    assert rejected_mcp["result"]["content"][0]["text"] == "validation_error"

    rejected_rest = client.patch(
        f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}/control",
        headers={"Idempotency-Key": "rest-live-control-invalid"},
        json={
            "expected_revision": 1,
            "role_selectors": {"worker": "reasonix.missing"},
        },
    )
    assert rejected_rest.status_code == 422
    assert rejected_rest.json()["detail"]["code"] == "validation_error"
    assert rejected_rest.json()["detail"]["field"] == "role_selectors.worker"
    assert rejected_rest.json()["detail"]["target"] == "reasonix.missing"

    config_path.write_text("[", encoding="utf-8")
    stopped = _mcp_tool(
        client,
        "owner_stop",
        {
            "project_id": PROJECT_ID,
            "run_id": run_id,
            "expected_revision": 1,
            "idempotency_key": "mcp-live-stop-1",
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
        "url": "http://127.0.0.1:8765/mcp",
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

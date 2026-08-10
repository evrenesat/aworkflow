"""Authentication and bearer-handling safety tests for all REST transports."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from aflow_app_server.config import ServerConfig
from aflow_app_server.control_plane_service import ControlPlaneService
from aflow_app_server.main import AccessLogPathFilter, app
from aflow_app_server.project_catalog import ProjectCatalog


@pytest.fixture
def auth_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from aflow_app_server import main

    token_file = tmp_path / "bearer.token"
    token_file.write_text("first-rotating-token\n")
    config = ServerConfig(
        bind_host="127.0.0.1",
        bind_port=8765,
        auth_token="",
        auth_token_file=token_file,
        repo_registry_path=tmp_path / "repos.json",
        codex_app_server_url=None,
        codex_app_server_token=None,
        transcription_url=None,
        transcription_token=None,
        projects_home=tmp_path / "code",
        project_overrides_path=tmp_path / "projects.json",
        attachment_root=tmp_path / "attachments",
    )
    main._config = config
    main._project_catalog = ProjectCatalog(
        config.projects_home,
        config.project_overrides_path,
        legacy_registry_path=config.repo_registry_path,
    )
    main._control_plane_service = ControlPlaneService(())
    main._control_plane_service.start()
    main._service = None
    main._planning_service = None
    client = TestClient(app, follow_redirects=False)
    try:
        yield client, token_file, monkeypatch
    finally:
        main._config = None
        main._project_catalog = None
        main._control_plane_service = None
        main._service = None
        main._planning_service = None


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_protected_routes_require_header_bearer_and_reject_query_tokens(auth_client) -> None:
    client, _, _ = auth_client
    protected = "/api/control-plane/projects"
    assert client.get(protected).status_code == 401
    assert client.get(protected, headers=_bearer("wrong-token")).status_code == 401
    assert client.get(protected, headers=_bearer("first-rotating-token")).status_code == 200
    query = client.get(f"{protected}?token=first-rotating-token")
    assert query.status_code == 400
    assert query.json() == {"detail": {"code": "token_query_rejected"}}
    stream = client.get(
        "/api/control-plane/projects/not-allowed/runs/run/events/stream?access_token=first-rotating-token"
    )
    assert stream.status_code == 400


def test_every_control_plane_operation_rejects_an_unauthenticated_request(auth_client) -> None:
    client, _, _ = auth_client
    requests = (
        ("get", "/ready", None),
        ("get", "/api/control-plane/capabilities", None),
        ("get", "/api/control-plane/projects", None),
        ("get", "/api/control-plane/projects/example/capabilities", None),
        ("get", "/api/control-plane/projects/example/plans", None),
        ("get", "/api/control-plane/projects/example/runs", None),
        ("post", "/api/control-plane/projects/example/runs", {"plan_path": "plans/todo/a.md"}),
        ("get", "/api/control-plane/projects/example/runs/sample", None),
        ("get", "/api/control-plane/projects/example/runs/sample/events", None),
        ("get", "/api/control-plane/projects/example/runs/sample/events/stream", None),
        ("get", "/api/control-plane/projects/example/runs/sample/context", None),
        ("patch", "/api/control-plane/projects/example/runs/sample/control", {"expected_revision": 0}),
        ("post", "/api/control-plane/projects/example/runs/sample/owner-stop", {"expected_revision": 0}),
        ("post", "/api/control-plane/projects/example/runs/sample/resume", {}),
        ("post", "/api/control-plane/projects/example/startup-answers/startup-sample-q1", {"answer": True}),
    )
    for method, path, payload in requests:
        request = getattr(client, method)
        response = request(path) if payload is None else request(path, json=payload)
        assert response.status_code == 401, f"{method.upper()} {path} was not protected"


def test_token_file_rotation_is_immediate_and_never_returned(auth_client) -> None:
    client, token_file, _ = auth_client
    protected = "/api/control-plane/projects"
    assert client.get(protected, headers=_bearer("first-rotating-token")).status_code == 200
    token_file.write_text("second-rotating-token\n")
    stale = client.get(protected, headers=_bearer("first-rotating-token"))
    fresh = client.get(protected, headers=_bearer("second-rotating-token"))
    assert stale.status_code == 401
    assert fresh.status_code == 200
    assert "first-rotating-token" not in stale.text
    assert "second-rotating-token" not in stale.text


def test_redaction_redirect_and_cors_do_not_expose_bearer_material(
    auth_client, caplog: pytest.LogCaptureFixture
) -> None:
    client, _, monkeypatch = auth_client
    secret = "log-only-bearer-token"
    monkeypatch.setenv("AFLOW_APP_LOG_PLUGIN_PROBES", "true")
    with caplog.at_level(logging.WARNING, logger="aflow_app_server.plugin_probe"):
        probe = client.post("/api/plugin/events", content=f"token={secret}".encode())
    assert probe.status_code == 204
    assert secret not in caplog.text
    assert "[redacted]" in caplog.text
    access_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", f"/api/control-plane/projects?token={secret}", "1.1", 400),
        exc_info=None,
    )
    assert AccessLogPathFilter().filter(access_record)
    assert secret not in access_record.getMessage()
    assert "[redacted]" in access_record.getMessage()

    redirect = client.get(
        "/api/control-plane/projects/",
        headers=_bearer("first-rotating-token"),
    )
    assert redirect.status_code == 404
    assert "location" not in redirect.headers
    cors = client.get(
        "/api/control-plane/projects",
        headers={**_bearer("first-rotating-token"), "Origin": "https://untrusted.example"},
    )
    assert cors.status_code == 200
    assert "access-control-allow-origin" not in cors.headers

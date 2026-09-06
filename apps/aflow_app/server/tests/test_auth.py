"""Authentication and bearer-handling safety tests for all REST transports."""

from __future__ import annotations

import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient

from aflow_app_server import browser_session
from aflow_app_server.config import ServerConfig
from aflow_app_server.control_plane_service import ControlPlaneService
from aflow_app_server.project_registry import ProjectRegistry
from aflow_app_server.main import AccessLogPathFilter, app


def _install_auth_server(tmp_path) -> tuple:
    from aflow_app_server import main

    token_file = tmp_path / "bearer.token"
    token_file.write_text("first-rotating-token\n")
    config = ServerConfig(
        bind_host="127.0.0.1",
        bind_port=8765,
        auth_token="",
        auth_token_file=token_file,
    )
    main._config = config
    managed = tmp_path / "code"
    managed.mkdir()
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    main._project_registry = registry
    main._control_plane_service = ControlPlaneService(())
    main._control_plane_service.start()
    return token_file


def _reset_auth_server() -> None:
    from aflow_app_server import main

    main._config = None
    main._project_registry = None
    main._control_plane_service = None


@pytest.fixture
def auth_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    token_file = _install_auth_server(tmp_path)
    client = TestClient(app, follow_redirects=False)
    try:
        yield client, token_file, monkeypatch
    finally:
        _reset_auth_server()


@pytest.fixture
def session_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """HTTPS TestClient so Secure session cookies are stored and replayed."""
    token_file = _install_auth_server(tmp_path)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(browser_session, "_now", lambda: clock["now"])
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    try:
        yield client, token_file, monkeypatch, clock
    finally:
        _reset_auth_server()


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


def _login(client: TestClient, token: str = "first-rotating-token") -> object:
    response = client.post(
        "/api/session",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://testserver"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"authenticated": True}
    return response


def _decode_session_value(value: str) -> dict:
    body = value.partition(".")[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_session_login_sets_strict_secure_httponly_rolling_cookie(session_client) -> None:
    client, _, _, _ = session_client
    response = _login(client)
    cookie_header = response.headers["set-cookie"]
    assert "aflow_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "SameSite=strict" in cookie_header
    assert "Max-Age=2592000" in cookie_header
    assert "Path=/" in cookie_header
    assert "Domain=" not in cookie_header
    assert response.headers["cache-control"] == "no-store"
    assert "first-rotating-token" not in response.text
    assert "first-rotating-token" not in cookie_header


def test_session_login_requires_bearer_credential_and_same_origin(session_client) -> None:
    client, _, _, _ = session_client
    no_auth = client.post("/api/session", headers={"Origin": "https://testserver"})
    assert no_auth.status_code == 401
    bad_token = client.post(
        "/api/session",
        headers={"Authorization": "Bearer wrong-token", "Origin": "https://testserver"},
    )
    assert bad_token.status_code == 401
    missing_origin = client.post("/api/session", headers=_bearer("first-rotating-token"))
    assert missing_origin.status_code == 403
    assert missing_origin.json() == {"detail": {"code": "origin_required"}}
    cross_origin = client.post(
        "/api/session",
        headers={**_bearer("first-rotating-token"), "Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403


def test_session_restores_and_renews_only_on_marked_activity(session_client) -> None:
    client, _, _, clock = session_client
    _login(client)
    clock["now"] += 29 * 24 * 60 * 60

    background = client.get("/api/projects")
    assert background.status_code == 200
    assert "set-cookie" not in background.headers

    renewed = client.get("/api/projects", headers={"X-AFlow-Activity": "1"})
    assert renewed.status_code == 200
    renewed_value = client.cookies.get("aflow_session")
    assert _decode_session_value(renewed_value)["exp"] == int(clock["now"] + 2592000)

    clock["now"] += 20 * 24 * 60 * 60
    assert client.get("/api/session").status_code == 200
    clock["now"] += 31 * 24 * 60 * 60
    expired = client.get("/api/session")
    assert expired.status_code == 401


def test_expired_tampered_malformed_and_overlong_sessions_are_rejected(session_client) -> None:
    client, _, _, clock = session_client
    login_response = _login(client)
    fresh_value = login_response.cookies["aflow_session"]

    client.cookies.set("aflow_session", fresh_value[:-2] + ("aa" if fresh_value[-2:] != "aa" else "bb"))
    assert client.get("/api/projects").status_code == 401
    client.cookies.set("aflow_session", "garbage-cookie-value")
    assert client.get("/api/projects").status_code == 401
    client.cookies.set("aflow_session", fresh_value + "x" * 2000)
    assert client.get("/api/projects").status_code == 401

    client.cookies.set("aflow_session", fresh_value)
    clock["now"] += 31 * 24 * 60 * 60
    assert client.get("/api/projects").status_code == 401


def test_token_rotation_invalidates_issued_browser_sessions(session_client) -> None:
    client, token_file, _, _ = session_client
    _login(client)
    token_file.write_text("second-rotating-token\n")
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/session").status_code == 401


def test_login_cannot_cross_token_epochs_during_rotation(session_client) -> None:
    client, _, monkeypatch, _ = session_client
    from aflow_app_server import main

    config = main._config
    reads = {"count": 0}
    values = iter(("old-token", "new-token"))

    def rotating_auth_token(_: object) -> str:
        reads["count"] += 1
        return next(values, "new-token")

    monkeypatch.setattr(type(config), "current_auth_token", rotating_auth_token)
    login = client.post(
        "/api/session",
        headers={"Authorization": "Bearer old-token", "Origin": "https://testserver"},
    )
    assert login.status_code == 200, login.text
    assert reads["count"] == 1

    cookie = login.cookies["aflow_session"]
    browser_session.verify_session(cookie, "old-token")
    with pytest.raises(ValueError):
        browser_session.verify_session(cookie, "new-token")

    client.cookies.set("aflow_session", cookie)
    assert client.get("/api/session").status_code == 401


def test_logout_expires_cookie_and_is_idempotent(session_client) -> None:
    client, _, _, _ = session_client
    _login(client)
    blocked = client.delete("/api/session", headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403
    logout = client.delete("/api/session", headers={"Origin": "https://testserver"})
    assert logout.status_code == 204
    assert logout.headers["cache-control"] == "no-store"
    cookie_header = logout.headers["set-cookie"]
    assert "Max-Age=0" in cookie_header
    assert client.cookies.get("aflow_session") is None
    assert client.get("/api/session").status_code == 401
    repeat = client.delete("/api/session", headers={"Origin": "https://testserver"})
    assert repeat.status_code == 204


def test_cookie_mutations_require_exact_same_origin(session_client) -> None:
    client, _, _, _ = session_client
    _login(client)
    payload = {"plan_path": "plans/todo/a.md"}
    mutation = "/api/control-plane/projects/example/runs"
    for origin in (None, "https://evil.example", "null", "https://testserver:8443"):
        headers = {"Origin": origin} if origin else {}
        rejected = client.post(mutation, json=payload, headers=headers)
        assert rejected.status_code == 403, f"Origin {origin!r} was not rejected"
    allowed = client.post(
        mutation, json=payload, headers={"Origin": "https://testserver"}
    )
    assert allowed.status_code == 404

    header_client = client.post(mutation, json=payload, headers=_bearer("first-rotating-token"))
    assert header_client.status_code == 404
    invalid_header = client.post(
        mutation, json=payload, headers={**_bearer("wrong-token"), "Origin": "https://testserver"}
    )
    assert invalid_header.status_code == 401


def test_mcp_transport_rejects_cookie_only_sessions(session_client) -> None:
    client, _, _, _ = session_client
    _login(client)
    response = client.post(
        "/mcp",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "unauthorized"}}

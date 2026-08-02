"""Tests for the API endpoints."""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aflow_app_server.config import ServerConfig
from aflow_app_server.main import AccessLogPathFilter, app
from aflow_app_server.project_catalog import ProjectCatalog
from aflow_app_server.transcription import TranscriptionError


@pytest.fixture
def test_token() -> str:
    """Test auth token."""
    return "test-token-12345"


@pytest.fixture
def test_config(tmp_path: Path, test_token: str) -> ServerConfig:
    """Create a test configuration."""
    return ServerConfig(
        bind_host="127.0.0.1",
        bind_port=8765,
        auth_token=test_token,
        repo_registry_path=tmp_path / "repos.json",
        codex_app_server_url=None,
        codex_app_server_token=None,
        transcription_url=None,
        transcription_token=None,
        projects_home=tmp_path / "code",
        project_overrides_path=tmp_path / "project_overrides.json",
        attachment_root=tmp_path / "attachments",
    )


def _set_server_state(
    config: ServerConfig,
    *,
    service: object | None = None,
    planning_service: object | None = None,
) -> None:
    """Install the shared server state used by tests."""
    from aflow_app_server import main as main_module
    from aflow_app_server.aflow_service import AflowService

    main_module._config = config
    main_module._project_catalog = ProjectCatalog(
        config.projects_home,
        config.project_overrides_path,
        legacy_registry_path=config.repo_registry_path,
    )
    main_module._service = service if service is not None else AflowService()
    main_module._planning_service = planning_service


def _create_git_project(projects_home: Path, name: str = "test_repo") -> Path:
    """Create a git repository under the configured projects home."""
    project_path = projects_home / name
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / ".git").mkdir(exist_ok=True)
    return project_path


def _project_id_for_path(client: TestClient, project_path: Path) -> str:
    """Look up the discovered project id for a current path."""
    response = client.get("/api/projects")
    assert response.status_code == 200
    for project in response.json():
        if project["current_path"] == str(project_path):
            return project["id"]
    raise AssertionError(f"Project not found for path: {project_path}")


def test_lifespan_owns_shared_attachment_store_for_codex(
    test_config: ServerConfig,
) -> None:
    from aflow_app_server import main as main_module
    from aflow_app_server.planning import AttachmentKind
    from aflow_app_server.planning.providers import CodexProvider

    async def check() -> None:
        with (
            patch.object(ServerConfig, "from_env", return_value=test_config),
            patch.object(CodexProvider, "start", new=AsyncMock()),
            patch.object(CodexProvider, "close", new=AsyncMock()),
        ):
            async with main_module.lifespan(app):
                service = main_module.get_planning_service()
                provider = service.registry.get("codex")
                store = service.attachment_store

                assert store is not None
                assert store.root == test_config.attachment_root.resolve()
                assert store._max_file_size == test_config.attachment_max_file_size_bytes
                assert store._max_count == test_config.attachment_max_count_per_turn
                assert (
                    store._max_total_size
                    == test_config.attachment_max_total_size_bytes_per_turn
                )
                assert provider._attachment_store is store
                assert provider.capabilities.attachments is True
                assert provider.capabilities.attachment_kinds == (
                    AttachmentKind.FILE,
                    AttachmentKind.IMAGE,
                )
                assert app.state.attachment_store is store

        assert app.state.planning_service is None
        assert app.state.attachment_store is None

    asyncio.run(check())


@pytest.fixture
def client_with_config(test_config: ServerConfig, test_token: str) -> TestClient:
    """Create a test client with proper config injection."""
    from aflow_app_server import main as main_module

    _set_server_state(test_config)

    try:
        client = TestClient(app, raise_server_exceptions=True)
        client.headers["Authorization"] = f"Bearer {test_token}"
        yield client
    finally:
        main_module._config = None
        main_module._project_catalog = None
        main_module._service = None
        main_module._planning_service = None


class TestHealthEndpoint:
    """Tests for the health endpoint."""

    def test_health_check_no_auth(self, test_config: ServerConfig) -> None:
        """Test that health check works without auth."""
        from aflow_app_server import main as main_module

        _set_server_state(test_config)

        try:
            client = TestClient(app)
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None


class TestAuth:
    """Tests for authentication."""

    def test_missing_auth_header(self, test_config: ServerConfig) -> None:
        """Test that requests without auth are rejected."""
        from aflow_app_server import main as main_module

        _set_server_state(test_config)

        try:
            client = TestClient(app)
            response = client.get("/api/projects")
            # FastAPI returns 401 for missing auth, not 403
            assert response.status_code == 401
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None

    def test_invalid_token(self, test_config: ServerConfig, test_token: str) -> None:
        """Test that invalid tokens are rejected."""
        from aflow_app_server import main as main_module

        _set_server_state(test_config)

        try:
            client = TestClient(app)
            client.headers["Authorization"] = "Bearer wrong-token"
            response = client.get("/api/projects")
            assert response.status_code == 401
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None

    def test_query_token_is_accepted(self, test_config: ServerConfig, test_token: str) -> None:
        """Test that token query param works for clients like EventSource."""
        from aflow_app_server import main as main_module

        _set_server_state(test_config)

        try:
            client = TestClient(app)
            response = client.get(f"/api/projects?token={test_token}")
            assert response.status_code == 200
            assert response.json() == []
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None


class TestProjectCatalogBootstrap:
    """Tests for project catalog bootstrap wiring."""

    def test_project_catalog_lists_projects_from_configured_home(
        self,
        test_config: ServerConfig,
        tmp_path: Path,
    ) -> None:
        """Test that the startup catalog can discover projects under the configured home."""
        from aflow_app_server import main as main_module
        from aflow_app_server.aflow_service import AflowService

        projects_home = tmp_path / "code"
        project_path = projects_home / "catalog-repo"
        project_path.mkdir(parents=True)
        (project_path / ".git").mkdir()

        config = ServerConfig(
            bind_host=test_config.bind_host,
            bind_port=test_config.bind_port,
            auth_token=test_config.auth_token,
            repo_registry_path=test_config.repo_registry_path,
            codex_app_server_url=None,
            codex_app_server_token=None,
            transcription_url=None,
            transcription_token=None,
            projects_home=projects_home,
            project_overrides_path=tmp_path / "project_overrides.json",
        )

        _set_server_state(config)

        try:
            projects = main_module.get_project_catalog().list_projects()
            assert len(projects) == 1
            assert projects[0].current_path == project_path
            assert projects[0].detection_source == "local_git_root"
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None


class TestLogging:
    """Tests for access log suppression."""

    def test_plugin_probe_access_log_is_suppressed(self) -> None:
        """Suppress noisy local plugin probe requests."""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:1234", "POST", "/api/plugin/events", "1.1", 405),
            exc_info=None,
        )
        assert AccessLogPathFilter().filter(record) is False

    def test_normal_access_log_is_kept(self) -> None:
        """Keep normal access logs visible."""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:1234", "GET", "/health", "1.1", 200),
            exc_info=None,
        )
        assert AccessLogPathFilter().filter(record) is True

    def test_plugin_probe_is_intercepted_quietly(
        self,
        test_config: ServerConfig,
        tmp_path: Path,
    ) -> None:
        """Short-circuit known localhost plugin probes."""
        from aflow_app_server import main as main_module

        _set_server_state(test_config)
        main_module._seen_plugin_probe_fingerprints.clear()

        try:
            client = TestClient(app)
            response = client.post(
                "/api/plugin/events",
                headers={"User-Agent": "suspicious-local-plugin/1.0"},
                json={"hello": "world"},
            )
            assert response.status_code == 204
            assert response.text == ""
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None
            main_module._seen_plugin_probe_fingerprints.clear()

    def test_plugin_probe_logging_can_be_enabled_once(
        self,
        test_config: ServerConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Log one fingerprint once when debug logging is enabled."""
        from aflow_app_server import main as main_module

        monkeypatch.setenv("AFLOW_APP_LOG_PLUGIN_PROBES", "1")
        _set_server_state(test_config)
        main_module._seen_plugin_probe_fingerprints.clear()

        try:
            client = TestClient(app)
            with caplog.at_level(logging.WARNING, logger="aflow_app_server.plugin_probe"):
                response1 = client.post(
                    "/api/plugin/events",
                    headers={"User-Agent": "suspicious-local-plugin/1.0"},
                    content=b"abc123",
                )
                response2 = client.post(
                    "/api/plugin/events",
                    headers={"User-Agent": "suspicious-local-plugin/1.0"},
                    content=b"abc123",
                )

            assert response1.status_code == 204
            assert response2.status_code == 204
            matching = [
                record.message for record in caplog.records
                if "Blocked localhost probe:" in record.message
            ]
            assert len(matching) == 1
            assert "suspicious-local-plugin/1.0" in matching[0]
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None
            main_module._seen_plugin_probe_fingerprints.clear()


class TestWebAppServing:
    """Tests for serving the built frontend."""

    def test_root_serves_built_index(self, test_config: ServerConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that / serves the built frontend when dist exists."""
        from aflow_app_server import main as main_module

        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "index.html").write_text("<!doctype html><html><body>aflow ui</body></html>")

        monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist_dir))
        _set_server_state(test_config)

        try:
            client = TestClient(app)
            response = client.get("/")
            assert response.status_code == 200
            assert "aflow ui" in response.text
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None

    def test_unknown_frontend_route_falls_back_to_index(
        self,
        test_config: ServerConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that SPA routes fall back to index.html."""
        from aflow_app_server import main as main_module

        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        (dist_dir / "index.html").write_text("<!doctype html><html><body>spa shell</body></html>")

        monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist_dir))
        _set_server_state(test_config)

        try:
            client = TestClient(app)
            response = client.get("/plans/demo")
            assert response.status_code == 200
            assert "spa shell" in response.text
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None


class TestProjectEndpoints:
    """Tests for project endpoints."""

    def test_list_projects_empty(self, client_with_config: TestClient) -> None:
        """Test listing projects when empty."""
        response = client_with_config.get("/api/projects")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_projects_discovers_projects_without_manual_registration(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test that a git project under projects_home is visible before updating overrides."""
        project_path = _create_git_project(test_config.projects_home, "discovered-repo")

        response = client_with_config.get("/api/projects")
        assert response.status_code == 200
        projects = response.json()
        assert any(project["current_path"] == str(project_path) for project in projects)

    def test_get_project(self, client_with_config: TestClient, test_config: ServerConfig) -> None:
        """Test getting a specific project."""
        project_path = _create_git_project(test_config.projects_home, "project-one")
        project_id = _project_id_for_path(client_with_config, project_path)

        response = client_with_config.get(f"/api/projects/{project_id}")
        assert response.status_code == 200
        assert response.json()["id"] == project_id

    def test_get_project_not_found(self, client_with_config: TestClient) -> None:
        """Test getting a non-existent project."""
        response = client_with_config.get("/api/projects/nonexistent")
        assert response.status_code == 404

    def test_update_project(self, client_with_config: TestClient, test_config: ServerConfig) -> None:
        """Test updating a project override."""
        project_path = _create_git_project(test_config.projects_home, "project-two")
        project_id = _project_id_for_path(client_with_config, project_path)
        renamed_path = test_config.projects_home / "renamed-project-two"
        project_path.rename(renamed_path)

        response = client_with_config.patch(
            f"/api/projects/{project_id}",
            json={"display_name": "updated-name", "current_path": str(renamed_path)},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["display_name"] == "updated-name"
        assert payload["current_path"] == str(renamed_path)

    def test_update_project_not_found(self, client_with_config: TestClient) -> None:
        """Test updating a non-existent project."""
        response = client_with_config.patch(
            "/api/projects/nonexistent",
            json={"display_name": "updated-name"},
        )
        assert response.status_code == 404


class TestPlanEndpoints:
    """Tests for plan endpoints."""

    def test_list_plans_empty(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test listing plans when none exist."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        response = client_with_config.get(f"/api/projects/{project_id}/plans")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_plans_with_drafts(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        """Test listing plans with draft plans."""
        project_path = _create_git_project(test_config.projects_home, "test_project")

        # Create a draft plan
        drafts_dir = project_path / "plans" / "drafts"
        drafts_dir.mkdir(parents=True)
        plan_content = """# Test Plan

## Summary
Test plan for testing.

### [ ] Checkpoint 1: First checkpoint

- [ ] Step 1
- [ ] Step 2
"""
        (drafts_dir / "test-plan.md").write_text(plan_content)

        project_id = _project_id_for_path(client_with_config, project_path)

        response = client_with_config.get(f"/api/projects/{project_id}/plans")
        assert response.status_code == 200
        plans = response.json()
        assert len(plans) == 1
        assert plans[0]["name"] == "test-plan"
        assert plans[0]["status"] == "draft"

    def test_list_plans_project_not_found(self, client_with_config: TestClient) -> None:
        """Test listing plans for non-existent project."""
        response = client_with_config.get("/api/projects/nonexistent/plans")
        assert response.status_code == 404


class TestExecutionEndpoints:
    """Tests for execution endpoints."""

    def test_start_execution_project_not_found(self, client_with_config: TestClient) -> None:
        """Test starting execution for non-existent project."""
        response = client_with_config.post(
            "/api/executions",
            json={
                "project_id": "nonexistent",
                "plan_path": "test.md",
            },
        )
        assert response.status_code == 404

    def test_start_execution_plan_not_found(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test starting execution with non-existent plan."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        response = client_with_config.post(
            "/api/executions",
            json={
                "project_id": project_id,
                "plan_path": "nonexistent.md",
            },
        )
        assert response.status_code == 400

    def test_get_execution_not_found(self, client_with_config: TestClient) -> None:
        """Test getting non-existent execution."""
        response = client_with_config.get("/api/executions/nonexistent")
        assert response.status_code == 404



class TestCodexEndpoints:
    """Tests for the temporary thread compatibility routes."""

    @staticmethod
    def _session(path: Path, *, session_id: str = "thread-1"):
        from datetime import datetime, timezone

        from aflow_app_server.planning import Session, SessionKey, SessionStatus, Turn, TurnStatus

        return Session(
            key=SessionKey(provider_id="codex", provider_session_id=session_id),
            cwd=str(path),
            title="Test session",
            preview="preview text",
            status=SessionStatus.IDLE,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            turns=(Turn(turn_id="turn-1", status=TurnStatus.COMPLETED),),
        )

    @staticmethod
    def _provider_error(message: str = "Planning provider is unavailable."):
        from aflow_app_server.planning import (
            PlanningError,
            PlanningErrorCode,
            ProviderOperationError,
        )

        return ProviderOperationError(
            PlanningError(
                code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
                message=message,
                provider_id="codex",
                retryable=True,
            )
        )

    def test_list_threads_degrades_when_provider_is_unavailable(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "thread-project")
        project_id = _project_id_for_path(client_with_config, project_path)
        planning_service = MagicMock()
        planning_service.list_sessions = AsyncMock(side_effect=self._provider_error())
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.get(f"/api/projects/{project_id}/threads")

        assert response.status_code == 200
        payload = response.json()
        assert payload["threads"] == []
        assert payload["backend_status"]["state"] == "error"
        assert payload["backend_status"]["detail"] is None

    def test_list_threads_uses_provider_neutral_service(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "thread-project")
        project_id = _project_id_for_path(client_with_config, project_path)
        session = self._session(project_path)
        planning_service = MagicMock()
        planning_service.list_sessions = AsyncMock(return_value=((session,), ()))
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.get(f"/api/projects/{project_id}/threads")

        assert response.status_code == 200
        payload = response.json()
        assert payload["threads"][0]["id"] == "thread-1"
        assert payload["threads"][0]["cwd"] == str(project_path)
        planning_service.list_sessions.assert_awaited_once_with(
            provider_id="codex", cwd=str(project_path), archived=None
        )

    def test_list_projects_includes_provider_session_only_project(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        session_path = test_config.projects_home.parent / "session-only"
        session_path.mkdir(parents=True)
        session = self._session(session_path)
        planning_service = MagicMock()
        planning_service.list_sessions = AsyncMock(return_value=((session,), ()))
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.get("/api/projects")

        assert response.status_code == 200
        project = next(
            item for item in response.json() if item["current_path"] == str(session_path)
        )
        assert project["linked_thread_count"] == 1
        assert project["detection_source"] == "planning_session_cwd"

    def test_list_threads_keeps_historical_alias_sessions_visible(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        old_path = _create_git_project(test_config.projects_home, "moved-project")
        project_id = _project_id_for_path(client_with_config, old_path)
        new_path = test_config.projects_home / "moved-project-renamed"
        old_path.rename(new_path)
        assert client_with_config.patch(
            f"/api/projects/{project_id}", json={"current_path": str(new_path)}
        ).status_code == 200
        session = self._session(old_path)

        async def list_sessions(*, cwd=None, **kwargs):
            return (((session,) if cwd == str(old_path) else ()), ())

        planning_service = MagicMock()
        planning_service.list_sessions = AsyncMock(side_effect=list_sessions)
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.get(f"/api/projects/{project_id}/threads")

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["threads"]] == ["thread-1"]
        queried_cwds = {
            call.kwargs["cwd"] for call in planning_service.list_sessions.await_args_list
        }
        assert queried_cwds == {str(new_path), str(old_path)}

    def test_read_thread_translates_provider_session(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "thread-project")
        project_id = _project_id_for_path(client_with_config, project_path)
        planning_service = MagicMock()
        planning_service.read_session = AsyncMock(return_value=self._session(project_path))
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.get(
            f"/api/projects/{project_id}/threads/thread-1"
        )

        assert response.status_code == 200
        assert response.json()["turns"][0]["id"] == "turn-1"
        planning_service.read_session.assert_awaited_once()

    def test_start_thread_uses_server_authoritative_project_path(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "thread-project")
        project_id = _project_id_for_path(client_with_config, project_path)
        planning_service = MagicMock()
        planning_service.start_session = AsyncMock(return_value=self._session(project_path))
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.post(
            f"/api/projects/{project_id}/threads", json={"model": None}
        )

        assert response.status_code == 200
        context = planning_service.start_session.await_args.args[0]
        assert context.project_id == project_id
        assert context.cwd == str(project_path)

        rejected = client_with_config.post(
            f"/api/projects/{project_id}/threads", json={"cwd": "/untrusted"}
        )
        assert rejected.status_code == 422

    def test_start_turn_translates_text_and_reasoning_controls(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        from aflow_app_server.planning import Turn, TurnStatus

        project_path = _create_git_project(test_config.projects_home, "thread-project")
        project_id = _project_id_for_path(client_with_config, project_path)
        planning_service = MagicMock()
        planning_service.read_session = AsyncMock(return_value=self._session(project_path))
        planning_service.start_turn = AsyncMock(
            return_value=Turn(turn_id="turn-new", status=TurnStatus.RUNNING)
        )
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.post(
            f"/api/projects/{project_id}/threads/thread-1/turns",
            json={
                "input": [{"type": "text", "text": "hello", "text_elements": []}],
                "approval_policy": "never",
                "model": "gpt-5.6",
                "effort": "high",
                "summary": "concise",
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "inProgress"
        request = planning_service.start_turn.await_args.args[1]
        assert request.text == "hello"
        assert request.model == "gpt-5.6"
        assert request.reasoning_level == "high"
        assert request.reasoning_summary == "concise"

    def test_start_turn_rejects_non_text_legacy_input(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "thread-project")
        project_id = _project_id_for_path(client_with_config, project_path)
        planning_service = MagicMock()
        planning_service.start_turn = AsyncMock()
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.post(
            f"/api/projects/{project_id}/threads/thread-1/turns",
            json={"input": [{"type": "localImage", "path": "/tmp/image.png"}]},
        )

        assert response.status_code == 422
        planning_service.start_turn.assert_not_awaited()

    def test_session_mutation_rejects_cross_project_session(
        self, client_with_config: TestClient, test_config: ServerConfig
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "thread-project")
        other_path = _create_git_project(test_config.projects_home, "other-project")
        project_id = _project_id_for_path(client_with_config, project_path)
        planning_service = MagicMock()
        planning_service.read_session = AsyncMock(return_value=self._session(other_path))
        planning_service.start_turn = AsyncMock()
        _set_server_state(test_config, planning_service=planning_service)

        response = client_with_config.post(
            f"/api/projects/{project_id}/threads/thread-1/turns",
            json={"input": [{"type": "text", "text": "hello"}]},
        )

        assert response.status_code == 404
        planning_service.start_turn.assert_not_awaited()

class TestPlanDraftEndpoints:
    """Tests for plan draft management endpoints."""

    def test_save_draft(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test saving a draft plan."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        content = "# Test Plan\n\nThis is a test plan."
        response = client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "test-plan", "content": content},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "test-plan"
        assert data["status"] == "draft"

        # Verify file was created
        draft_path = project_path / "plans" / "drafts" / "test-plan.md"
        assert draft_path.exists()
        assert draft_path.read_text() == content

    def test_save_draft_ignores_unrelated_payload_fields(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "draft-extra")
        project_id = _project_id_for_path(client_with_config, project_path)

        response = client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "test-plan", "content": "content", "harmless": True},
        )

        assert response.status_code == 201
        assert (project_path / "plans" / "drafts" / "test-plan.md").read_text() == "content"

    def test_list_drafts(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test listing draft plans."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        # Save some drafts
        client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "plan-a", "content": "Content A"},
        )
        client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "plan-b", "content": "Content B"},
        )

        response = client_with_config.get(f"/api/projects/{project_id}/plans/drafts")
        assert response.status_code == 200
        drafts = response.json()
        assert drafts == ["plan-a", "plan-b"]

    def test_load_draft(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test loading a draft plan."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        content = "# Test Plan\n\nContent here."
        client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "test-plan", "content": content},
        )

        response = client_with_config.get(
            f"/api/projects/{project_id}/plans/drafts/test-plan"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-plan"
        assert data["content"] == content

    def test_delete_draft(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test deleting a draft plan."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "test-plan", "content": "Content"},
        )

        response = client_with_config.delete(
            f"/api/projects/{project_id}/plans/drafts/test-plan"
        )
        assert response.status_code == 204

        # Verify it's gone
        list_response = client_with_config.get(
            f"/api/projects/{project_id}/plans/drafts"
        )
        assert "test-plan" not in list_response.json()

    def test_promote_plan(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test promoting a draft to in-progress."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        content = "# Test Plan\n\nThis will be promoted."
        client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "test-plan", "content": content},
        )

        response = client_with_config.post(
            f"/api/projects/{project_id}/plans/promote",
            json={"draft_name": "test-plan"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-plan"
        assert data["status"] == "in_progress"

        # Verify file was created in in-progress
        in_progress_path = project_path / "plans" / "in-progress" / "test-plan.md"
        assert in_progress_path.exists()
        assert in_progress_path.read_text() == content

    def test_promote_plan_ignores_unrelated_payload_fields(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        project_path = _create_git_project(test_config.projects_home, "promote-extra")
        project_id = _project_id_for_path(client_with_config, project_path)
        client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "test-plan", "content": "content"},
        )

        response = client_with_config.post(
            f"/api/projects/{project_id}/plans/promote",
            json={"draft_name": "test-plan", "harmless": True},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "in_progress"

    def test_list_in_progress(
        self,
        client_with_config: TestClient,
        test_config: ServerConfig,
    ) -> None:
        """Test listing in-progress plans."""
        project_path = _create_git_project(test_config.projects_home, "test_project")
        project_id = _project_id_for_path(client_with_config, project_path)

        # Save and promote some plans
        client_with_config.post(
            f"/api/projects/{project_id}/plans/drafts",
            json={"name": "plan-a", "content": "Content A"},
        )
        client_with_config.post(
            f"/api/projects/{project_id}/plans/promote",
            json={"draft_name": "plan-a"},
        )

        response = client_with_config.get(
            f"/api/projects/{project_id}/plans/in-progress"
        )
        assert response.status_code == 200
        plans = response.json()
        assert "plan-a" in plans

    def test_save_draft_project_not_found(self, client_with_config: TestClient) -> None:
        """Test saving draft for non-existent project."""
        response = client_with_config.post(
            "/api/projects/nonexistent/plans/drafts",
            json={"name": "test", "content": "Content"},
        )
        assert response.status_code == 404


class TestTranscriptionEndpoint:
    """Tests for the transcription endpoint."""

    def test_transcribe_without_config(self, client_with_config: TestClient) -> None:
        """Test transcription when service is not configured."""
        audio_data = b"fake audio data"
        files = {"file": ("test.webm", io.BytesIO(audio_data), "audio/webm")}

        response = client_with_config.post("/api/transcribe", files=files)
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"].lower()

    def test_transcribe_success(self, test_config: ServerConfig, test_token: str) -> None:
        """Test successful transcription."""
        from aflow_app_server import main as main_module

        config_with_transcription = ServerConfig(
            bind_host=test_config.bind_host,
            bind_port=test_config.bind_port,
            auth_token=test_config.auth_token,
            repo_registry_path=test_config.repo_registry_path,
            codex_app_server_url=test_config.codex_app_server_url,
            codex_app_server_token=test_config.codex_app_server_token,
            transcription_url="https://api.example.com",
            transcription_token="test-transcription-token",
            projects_home=test_config.projects_home,
            project_overrides_path=test_config.project_overrides_path,
        )

        _set_server_state(config_with_transcription)

        mock_client = AsyncMock()
        mock_client.transcribe = AsyncMock(return_value="Transcribed text")
        main_module._transcription_client = mock_client

        try:
            client = TestClient(app, raise_server_exceptions=True)
            client.headers["Authorization"] = f"Bearer {test_token}"

            audio_data = b"fake audio data"
            files = {"file": ("test.webm", io.BytesIO(audio_data), "audio/webm")}

            response = client.post("/api/transcribe", files=files)
            assert response.status_code == 200
            assert response.json() == {"text": "Transcribed text"}

            mock_client.transcribe.assert_called_once()
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None
            main_module._transcription_client = None

    def test_transcribe_error(self, test_config: ServerConfig, test_token: str) -> None:
        """Test transcription with error."""
        from aflow_app_server import main as main_module

        config_with_transcription = ServerConfig(
            bind_host=test_config.bind_host,
            bind_port=test_config.bind_port,
            auth_token=test_config.auth_token,
            repo_registry_path=test_config.repo_registry_path,
            codex_app_server_url=test_config.codex_app_server_url,
            codex_app_server_token=test_config.codex_app_server_token,
            transcription_url="https://api.example.com",
            transcription_token="test-transcription-token",
            projects_home=test_config.projects_home,
            project_overrides_path=test_config.project_overrides_path,
        )

        _set_server_state(config_with_transcription)

        mock_client = AsyncMock()
        mock_client.transcribe = AsyncMock(side_effect=TranscriptionError("Service unavailable"))
        main_module._transcription_client = mock_client

        try:
            client = TestClient(app, raise_server_exceptions=True)
            client.headers["Authorization"] = f"Bearer {test_token}"

            audio_data = b"fake audio data"
            files = {"file": ("test.webm", io.BytesIO(audio_data), "audio/webm")}

            response = client.post("/api/transcribe", files=files)
            assert response.status_code == 500
            assert "Service unavailable" in response.json()["detail"]
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None
            main_module._transcription_client = None

    def test_transcribe_requires_auth(self, test_config: ServerConfig) -> None:
        """Test that transcription requires authentication."""
        from aflow_app_server import main as main_module

        config_with_transcription = ServerConfig(
            bind_host=test_config.bind_host,
            bind_port=test_config.bind_port,
            auth_token=test_config.auth_token,
            repo_registry_path=test_config.repo_registry_path,
            codex_app_server_url=test_config.codex_app_server_url,
            codex_app_server_token=test_config.codex_app_server_token,
            transcription_url="https://api.example.com",
            transcription_token="test-transcription-token",
            projects_home=test_config.projects_home,
            project_overrides_path=test_config.project_overrides_path,
        )

        _set_server_state(config_with_transcription)

        mock_client = AsyncMock()
        main_module._transcription_client = mock_client

        try:
            client = TestClient(app)

            audio_data = b"fake audio data"
            files = {"file": ("test.webm", io.BytesIO(audio_data), "audio/webm")}

            response = client.post("/api/transcribe", files=files)
            assert response.status_code == 401
        finally:
            main_module._config = None
            main_module._project_catalog = None
            main_module._service = None
            main_module._transcription_client = None

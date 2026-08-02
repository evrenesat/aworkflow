"""Contract tests for provider-neutral planning HTTP routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from aflow_app_server.config import ServerConfig
from aflow_app_server.main import app
from aflow_app_server.planning import (
    AttachmentKind,
    AttachmentStore,
    PendingApproval,
    PlanningError,
    PlanningErrorCode,
    ProviderCapabilities,
    ProviderOperationError,
    ProviderReadiness,
    ProviderState,
    Session,
    SessionKey,
    SessionStatus,
    Turn,
    TurnStatus,
)
from aflow_app_server.project_catalog import ProjectCatalog


TOKEN = "planning-test-token"


def _config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
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
    )


def _session(
    cwd: Path,
    provider_id: str = "codex",
    provider_session_id: str = "shared-id",
    *,
    updated_at: datetime | None = None,
) -> Session:
    return Session(
        key=SessionKey(
            provider_id=provider_id, provider_session_id=provider_session_id
        ),
        cwd=str(cwd),
        title=f"{provider_id} session",
        status=SessionStatus.IDLE,
        updated_at=updated_at,
    )


def _readiness(provider_id: str, *, ready: bool = True) -> ProviderReadiness:
    return ProviderReadiness(
        provider_id=provider_id,
        display_name=provider_id.title(),
        state=ProviderState.READY if ready else ProviderState.UNAVAILABLE,
        capabilities=ProviderCapabilities(
            models=("model-a",),
            reasoning_levels=("low", "high"),
            reasoning_summaries=("auto", "none"),
            attachments=True,
            attachment_kinds=(AttachmentKind.FILE, AttachmentKind.IMAGE),
            fork=True,
            archive=True,
            approvals=True,
            interruption=True,
        ),
        error=None if ready else PlanningError(
            code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
            message="Planning provider is unavailable.",
            provider_id=provider_id,
            retryable=True,
        ),
    )


@pytest.fixture
def planning_client(tmp_path: Path):
    from aflow_app_server import main
    from aflow_app_server.aflow_service import AflowService

    config = _config(tmp_path)
    project_path = config.projects_home / "project"
    project_path.mkdir(parents=True)
    (project_path / ".git").mkdir()
    catalog = ProjectCatalog(
        config.projects_home,
        config.project_overrides_path,
        legacy_registry_path=config.repo_registry_path,
    )
    project = catalog.ensure_current_project(project_path)
    service = MagicMock()
    service.provider_statuses = AsyncMock(return_value=(_readiness("codex"),))
    service.list_models = AsyncMock(return_value=("model-a",))
    service.list_sessions = AsyncMock(return_value=((), (_readiness("codex"),)))
    service.attachment_store = AttachmentStore(
        config.attachment_root,
        max_file_size_bytes=1024,
        max_count_per_turn=3,
        max_total_size_bytes_per_turn=2048,
    )

    main._config = config
    main._project_catalog = catalog
    main._service = AflowService()
    main._planning_service = service
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        yield client, service, project, project_path
    finally:
        main._config = None
        main._project_catalog = None
        main._service = None
        main._planning_service = None


def test_provider_discovery_models_and_reasoning_are_provider_neutral(
    planning_client,
) -> None:
    client, service, _, _ = planning_client

    providers = client.get("/api/planning/providers")
    models = client.get("/api/planning/providers/codex/models")
    reasoning = client.get("/api/planning/providers/codex/reasoning-options")

    assert providers.status_code == models.status_code == reasoning.status_code == 200
    assert providers.json()["providers"][0]["provider_id"] == "codex"
    assert models.json() == {"provider_id": "codex", "models": ["model-a"]}
    assert reasoning.json()["reasoning_levels"] == ["low", "high"]
    service.list_models.assert_awaited_once_with("codex")


def test_unavailable_provider_reasoning_options_return_bounded_error(
    planning_client,
) -> None:
    client, service, _, _ = planning_client
    service.provider_statuses = AsyncMock(
        return_value=(_readiness("codex", ready=False),)
    )

    response = client.get("/api/planning/providers/codex/reasoning-options")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "provider_unavailable",
        "message": "Planning provider is unavailable.",
        "provider_id": "codex",
        "retryable": True,
    }


def test_disabled_provider_reasoning_options_synthesize_bounded_error(
    planning_client,
) -> None:
    client, service, _, _ = planning_client
    disabled = _readiness("codex").model_copy(
        update={"state": ProviderState.DISABLED, "error": None}
    )
    service.provider_statuses = AsyncMock(return_value=(disabled,))

    response = client.get("/api/planning/providers/codex/reasoning-options")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_unavailable"


def test_session_collection_keeps_provider_identity_and_deterministic_order(
    planning_client,
) -> None:
    client, service, project, path = planning_client
    latest = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    sessions = (
        _session(path, "other", "shared-id", updated_at=latest),
        _session(path, "codex", "shared-id", updated_at=latest),
        _session(path, "codex", "older", updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc)),
    )
    service.list_sessions = AsyncMock(
        return_value=(sessions, (_readiness("codex"), _readiness("other", ready=False)))
    )

    response = client.get(f"/api/projects/{project.id}/planning/sessions")

    assert response.status_code == 200
    assert [
        (item["key"]["provider_id"], item["key"]["provider_session_id"])
        for item in response.json()["sessions"]
    ] == [("codex", "shared-id"), ("other", "shared-id"), ("codex", "older")]
    assert {item["provider_id"] for item in response.json()["providers"]} == {
        "codex", "other"
    }
    assert response.json()["next_cursor"] is None


def test_session_collection_preserves_historical_alias_continuity(
    planning_client, tmp_path: Path
) -> None:
    from aflow_app_server import main

    client, service, project, _ = planning_client
    historical_path = tmp_path / "former-project-path"
    historical_path.mkdir()
    main._project_catalog.update_project(project.id, alias=historical_path)
    service.list_sessions = AsyncMock(
        return_value=((_session(historical_path),), (_readiness("codex"),))
    )

    response = client.get(f"/api/projects/{project.id}/planning/sessions")

    assert response.status_code == 200
    assert response.json()["sessions"][0]["cwd"] == str(historical_path)


def test_start_session_uses_catalog_path_and_rejects_client_cwd(planning_client) -> None:
    client, service, project, path = planning_client
    service.start_session = AsyncMock(return_value=_session(path))

    response = client.post(
        f"/api/projects/{project.id}/planning/sessions",
        json={"provider_id": "codex", "model": "model-a", "reasoning_level": "high"},
    )
    rejected = client.post(
        f"/api/projects/{project.id}/planning/sessions",
        json={"provider_id": "codex", "cwd": "/untrusted"},
    )

    assert response.status_code == 201
    context, request = service.start_session.await_args.args
    assert context.project_id == project.id
    assert context.cwd == str(path)
    assert request.reasoning_level == "high"
    assert service.start_session.await_args.kwargs == {"provider_id": "codex"}
    assert rejected.status_code == 422


def test_provider_qualified_detail_and_actions_authorize_project(planning_client) -> None:
    client, service, project, path = planning_client
    session = _session(path, "other", "shared-id")
    service.read_session = AsyncMock(return_value=session)
    service.resume_session = AsyncMock(return_value=session)
    service.fork_session = AsyncMock(return_value=session)
    service.set_session_name = AsyncMock()
    service.set_archived = AsyncMock()
    service.start_turn = AsyncMock(
        return_value=Turn(turn_id="turn-1", status=TurnStatus.RUNNING)
    )
    service.interrupt_turn = AsyncMock()
    service.respond_to_approval = AsyncMock()
    service.pending_approvals.return_value = (
        PendingApproval(
            approval_id="approval-1",
            key=SessionKey(provider_id="other", provider_session_id="shared-id"),
            turn_id="turn-1",
            kind="command",
        ),
    )
    root = (
        f"/api/projects/{project.id}/planning/providers/other/sessions/shared-id"
    )

    assert client.get(root).status_code == 200
    assert client.post(root + "/resume").status_code == 200
    assert client.post(root + "/fork").status_code == 201
    assert client.patch(root, json={"name": "renamed"}).status_code == 200
    assert client.post(root + "/archive").json() == {"archived": True}
    assert client.post(root + "/unarchive").json() == {"archived": False}
    assert client.post(root + "/turns", json={"text": "hello"}).status_code == 201
    assert client.post(root + "/turns/turn-1/interrupt").status_code == 200
    assert client.get(root + "/approvals").json()["approvals"][0]["approval_id"] == "approval-1"
    assert client.post(root + "/approvals/approval-1", json={"decision": "accept"}).status_code == 200

    keys = [call.args[0] for call in service.read_session.await_args_list]
    assert all(key == SessionKey(provider_id="other", provider_session_id="shared-id") for key in keys)
    turn_context = service.start_turn.await_args.kwargs["context"]
    assert turn_context.cwd == str(path)


def test_cross_project_session_is_hidden_and_provider_errors_are_bounded(
    planning_client, tmp_path: Path
) -> None:
    client, service, project, _ = planning_client
    service.read_session = AsyncMock(return_value=_session(tmp_path / "elsewhere"))
    root = f"/api/projects/{project.id}/planning/providers/codex/sessions/shared-id"

    assert client.get(root).status_code == 404

    service.read_session = AsyncMock(return_value=_session(project.current_path, "other"))
    assert client.get(root).status_code == 404

    service.read_session = AsyncMock(
        side_effect=ProviderOperationError(
            PlanningError(
                code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
                message="Planning provider is unavailable.",
                provider_id="codex",
                retryable=True,
            )
        )
    )
    response = client.get(root)
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "provider_unavailable",
        "message": "Planning provider is unavailable.",
        "provider_id": "codex",
        "retryable": True,
    }


def test_invalid_identity_and_unexpected_failures_have_deliberate_envelopes(
    planning_client,
) -> None:
    client, service, project, _ = planning_client
    invalid = client.get(
        f"/api/projects/{project.id}/planning/providers/CODEX/sessions/shared-id"
    )
    assert invalid.status_code == 422

    service.list_models = AsyncMock(side_effect=RuntimeError("secret raw payload"))
    failed = client.get("/api/planning/providers/codex/models")
    assert failed.status_code == 502
    assert failed.json()["detail"] == {
        "code": "internal_error",
        "message": "Planning operation failed unexpectedly.",
        "provider_id": "codex",
        "retryable": False,
    }
    assert "secret raw payload" not in failed.text


def test_multipart_attachment_lifecycle_is_exactly_session_scoped(
    planning_client,
) -> None:
    client, service, project, path = planning_client
    service.read_session = AsyncMock(return_value=_session(path))
    root = f"/api/projects/{project.id}/planning/providers/codex/sessions/shared-id/attachments"

    uploaded = client.post(
        root,
        data={"kind": "file"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert uploaded.status_code == 201
    attachment_id = uploaded.json()["attachment_id"]
    listed = client.get(root)
    assert [item["attachment_id"] for item in listed.json()["attachments"]] == [attachment_id]
    assert client.delete(f"{root}/{attachment_id}").status_code == 204
    assert client.get(root).json() == {"attachments": []}


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("post", "/fork", None),
        ("post", "/archive", None),
        ("post", "/unarchive", None),
        ("post", "/turns/turn-1/interrupt", None),
        ("get", "/approvals", None),
        ("post", "/approvals/approval-1", {"decision": "accept"}),
    ],
)
def test_unsupported_actions_fail_before_session_provider_access(
    planning_client, method: str, suffix: str, payload: dict[str, str] | None
) -> None:
    client, service, project, _ = planning_client
    service.require_capability.side_effect = ProviderOperationError(
        PlanningError(
            code=PlanningErrorCode.CAPABILITY_UNSUPPORTED,
            message="Planning provider does not support this operation.",
            provider_id="codex",
            retryable=False,
        )
    )
    service.read_session = AsyncMock()
    service.start_turn = AsyncMock()
    root = f"/api/projects/{project.id}/planning/providers/codex/sessions/shared-id"

    response = client.request(method, root + suffix, json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "capability_unsupported"
    service.read_session.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "hello", "attachment_ids": ["attachment-1"]},
        {"text": "hello", "output_schema": {"type": "object"}},
    ],
)
def test_unsupported_turn_features_fail_before_session_provider_access(
    planning_client, payload: dict[str, object]
) -> None:
    client, service, project, _ = planning_client
    service.validate_turn_capabilities.side_effect = ProviderOperationError(
        PlanningError(
            code=PlanningErrorCode.CAPABILITY_UNSUPPORTED,
            message="Planning provider does not support this operation.",
            provider_id="codex",
            retryable=False,
        )
    )
    service.read_session = AsyncMock()
    service.start_turn = AsyncMock()
    root = f"/api/projects/{project.id}/planning/providers/codex/sessions/shared-id"

    response = client.post(root + "/turns", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "capability_unsupported"
    service.read_session.assert_not_awaited()
    service.start_turn.assert_not_awaited()


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_unsupported_attachments_fail_before_session_or_store_access(
    planning_client, method: str
) -> None:
    client, service, project, _ = planning_client
    error = ProviderOperationError(
        PlanningError(
            code=PlanningErrorCode.CAPABILITY_UNSUPPORTED,
            message="Planning provider does not support this operation.",
            provider_id="codex",
            retryable=False,
        )
    )
    service.require_capability.side_effect = error
    service.read_session = AsyncMock()
    service.attachment_store = MagicMock()
    root = f"/api/projects/{project.id}/planning/providers/codex/sessions/shared-id/attachments"

    if method == "post":
        response = client.post(
            root,
            data={"kind": "file"},
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
    elif method == "get":
        response = client.get(root)
    else:
        response = client.delete(root + "/attachment-1")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "capability_unsupported"
    service.read_session.assert_not_awaited()
    service.attachment_store.upload.assert_not_called()
    service.attachment_store.list.assert_not_called()
    service.attachment_store.delete.assert_not_called()


def test_project_payload_has_canonical_and_deprecated_equal_counts(
    planning_client,
) -> None:
    client, service, _, path = planning_client
    service.list_sessions = AsyncMock(
        return_value=((_session(path),), (_readiness("codex"),))
    )

    payload = client.get("/api/projects").json()[0]

    assert payload["linked_session_count"] == 1
    assert payload["linked_thread_count"] == payload["linked_session_count"]

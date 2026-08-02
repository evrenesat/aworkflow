"""Tests for the provider-neutral planning domain."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aflow_app_server.planning import (
    AuthorizedProjectContext,
    PlanningError,
    PlanningErrorCode,
    PlanningProvider,
    ProviderCapabilities,
    Session,
    SessionKey,
    StartSessionRequest,
    StartTurnRequest,
)


def test_provider_qualified_keys_do_not_collide() -> None:
    codex = SessionKey(provider_id="codex", provider_session_id="shared-id")
    other = SessionKey(provider_id="other", provider_session_id="shared-id")

    assert codex != other
    assert len({codex, other}) == 2
    assert codex.model_dump() == {
        "provider_id": "codex",
        "provider_session_id": "shared-id",
    }


@pytest.mark.parametrize("provider_id", ["", "Codex", "bad/id", "bad_id", "-codex"])
def test_provider_id_must_be_a_path_safe_slug(provider_id: str) -> None:
    with pytest.raises(ValidationError):
        SessionKey(provider_id=provider_id, provider_session_id="session")


def test_provider_session_id_is_an_opaque_url_safe_segment() -> None:
    encoded_id = "native-id_Zm9vL2Jhcg"
    key = SessionKey(provider_id="codex", provider_session_id=encoded_id)
    assert key.provider_session_id == encoded_id


@pytest.mark.parametrize(
    "provider_session_id",
    ["", "   ", ".", "..", "native/id", "query?value", "fragment#value", "bad%2Fid"],
)
def test_provider_session_id_rejects_unsafe_path_values(
    provider_session_id: str,
) -> None:
    with pytest.raises(ValidationError):
        SessionKey(provider_id="codex", provider_session_id=provider_session_id)


def test_start_session_request_rejects_route_owned_project_fields() -> None:
    with pytest.raises(ValidationError):
        StartSessionRequest.model_validate({"cwd": "/private/path"})
    with pytest.raises(ValidationError):
        StartSessionRequest.model_validate({"project_id": "project-one"})
    with pytest.raises(ValidationError):
        StartSessionRequest.model_validate({"unexpected": True})


def test_turn_controls_are_provider_neutral_and_reject_sandbox_overrides() -> None:
    request = StartTurnRequest(
        text="plan",
        attachment_ids=("att_one",),
        model="model-one",
        reasoning_level="high",
        reasoning_summary="concise",
        output_schema={"type": "object"},
    )

    assert request.reasoning_level == "high"
    assert request.attachment_ids == ("att_one",)
    with pytest.raises(ValidationError):
        StartTurnRequest.model_validate(
            {"text": "plan", "sandbox": "danger-full-access"}
        )


class CapturingProvider(PlanningProvider):
    provider_id = "capture"
    display_name = "Capture"
    capabilities = ProviderCapabilities(reasoning_levels=("standard",))

    def __init__(self) -> None:
        self.received_context: AuthorizedProjectContext | None = None

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def readiness(self):  # pragma: no cover - unused interface method
        raise NotImplementedError

    async def list_sessions(self, **_):  # pragma: no cover - unused interface method
        raise NotImplementedError

    async def start_session(
        self,
        context: AuthorizedProjectContext,
        request: StartSessionRequest,
    ) -> Session:
        self.received_context = context
        return Session(
            key=SessionKey(provider_id=self.provider_id, provider_session_id="session"),
            project_id=context.project_id,
            cwd=context.cwd,
            model=request.model,
            reasoning_level=request.reasoning_level,
        )


@pytest.mark.asyncio
async def test_provider_receives_server_authorized_project_context() -> None:
    provider = CapturingProvider()
    context = AuthorizedProjectContext(project_id="project-one", cwd="/authorized/project")

    session = await provider.start_session(
        context,
        StartSessionRequest(model="model-one", reasoning_level="standard"),
    )

    assert provider.received_context == context
    assert session.project_id == "project-one"
    assert session.cwd == "/authorized/project"
    assert session.reasoning_level == "standard"


def test_safe_error_serialization_is_bounded_and_provider_neutral() -> None:
    error = PlanningError(
        code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
        message="Planning provider is unavailable.",
        provider_id="codex",
        retryable=True,
    )

    assert error.model_dump(mode="json") == {
        "code": "provider_unavailable",
        "message": "Planning provider is unavailable.",
        "provider_id": "codex",
        "retryable": True,
    }
    assert "detail" not in error.model_dump()
    with pytest.raises(ValidationError):
        PlanningError(
            code=PlanningErrorCode.INTERNAL_ERROR,
            message="x" * 501,
        )

from pathlib import Path
from dataclasses import dataclass, field
import json

import pytest

from aflow.harnesses.codex import CodexAdapter, probe_codex_capabilities
from aflow.harnesses.base import HarnessInvocation
import aflow.harnesses.reasonix as reasonix_module
from aflow.harnesses.reasonix import ReasonixAcpDriver, ReasonixAcpProcess
import aflow.workflow as workflow_module
from aflow.harnesses.reasonix import ReasonixAdapter
from aflow.harnesses.session import (
    NO_SESSION_CAPABILITIES,
    NoSessionDriver,
    SessionCapabilities,
    SessionRequest,
    SessionResult,
    parse_jsonl_events,
)


@dataclass
class FakeSessionDriver:
    """Deterministic CP2 contract fixture; no workflow routing is involved."""

    capabilities: SessionCapabilities = field(
        default_factory=lambda: SessionCapabilities(
            session_identity=True, followup_turn=True,
            resume_with_model=True, mid_turn_steer=True,
            idempotent_turn_start=True,
        )
    )
    handover_text: str = "Objective: continue the work."
    crash_point: str | None = None
    calls: list[str] = field(default_factory=list)

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        return HarnessInvocation(
            label="fake-session", argv=("fake-session",), env={},
            prompt_mode="stdin", system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            effective_prompt=f"{request.system_prompt}\n\n{request.user_prompt}",
            stdin_text=f"{request.system_prompt}\n\n{request.user_prompt}",
        )

    def request_mid_turn(self, message: str) -> bool:
        self.calls.append(f"steer:{message}")
        return self.capabilities.mid_turn_steer

    def handover(self, request: SessionRequest) -> str:
        if not self.capabilities.followup_turn:
            raise RuntimeError("fake follow-up unavailable")
        self.calls.append("handover")
        return self.handover_text

    def start(self, request: SessionRequest, *, target: bool = False) -> SessionResult:
        self.calls.append("target-start" if target else "source-start")
        if self.crash_point == "before_provider_operation_id":
            raise RuntimeError("provider operation did not begin")
        operation_id = "fake-operation-1"
        if self.crash_point == "after_provider_operation_id":
            raise RuntimeError(f"ambiguous provider operation {operation_id}")
        return SessionResult(
            session_id=request.session_id or "fake-session-1",
            selector=request.selector, model=request.model, effort=request.effort,
            final_output="fake final output", provider_operation_id=operation_id,
            idempotency_key=request.idempotency_key,
            capabilities=self.capabilities,
        )


def request(
    session_id: str | None = None,
    *,
    model: str | None = "sol",
    effort: str | None = "high",
) -> SessionRequest:
    return SessionRequest(
        repo_root=Path("/repo"), selector="codex.sol", model=model, effort=effort,
        system_prompt="SYSTEM", user_prompt="USER", session_id=session_id,
        idempotency_key="hotplug-1",
    )


CODEX_EXEC_HELP = "codex exec --json resume"
CODEX_RESUME_HELP = "Usage: codex exec resume [SESSION_ID] [PROMPT] -m, --model"


def test_codex_capabilities_require_current_json_and_explicit_resume_help() -> None:
    capabilities = probe_codex_capabilities(CODEX_EXEC_HELP, CODEX_RESUME_HELP)
    assert capabilities.session_identity is True
    assert capabilities.resume_with_model is True
    assert probe_codex_capabilities("codex exec", CODEX_RESUME_HELP).session_identity is False
    assert probe_codex_capabilities(CODEX_EXEC_HELP, "resume --last").resume_with_model is False
    assert probe_codex_capabilities(CODEX_EXEC_HELP, "resume --last").followup_turn is False
    assert probe_codex_capabilities("codex exec --json resume", "resume [SESSION_ID]").followup_turn is True
    assert probe_codex_capabilities("codex exec --json resume", "resume [SESSION_ID]").resume_with_model is False


def test_codex_fresh_and_resume_bind_exact_session_and_target_model() -> None:
    driver = CodexAdapter().session_driver(
        exec_help=CODEX_EXEC_HELP, resume_help=CODEX_RESUME_HELP
    )
    fresh = driver.build_invocation(request())
    assert "--json" in fresh.argv
    assert "USER" not in fresh.argv
    assert fresh.stdin_text.endswith("USER")
    resumed = driver.build_invocation(request("session-123"))
    assert resumed.argv[0:4] == ("codex", "exec", "resume", "session-123")
    assert "--model" in resumed.argv and "sol" in resumed.argv
    assert "model_reasoning_effort='high'" in resumed.argv
    assert "--last" not in resumed.argv


def test_codex_structured_output_extracts_one_session_and_final_response() -> None:
    driver = CodexAdapter().session_driver(
        exec_help=CODEX_EXEC_HELP, resume_help=CODEX_RESUME_HELP
    )
    result = driver.parse_result(
        request(),
        '{"type":"thread.started","thread_id":"session-123"}\n'
        '{"type":"message.completed","thread_id":"session-123","text":"done"}\n',
    )
    assert result.session_id == "session-123"
    assert result.final_output == "done"
    assert result.selector == "codex.sol"
    item_result = driver.parse_result(
        request(),
        '{"type":"thread.started","thread_id":"session-456"}\n'
        '{"type":"item.completed","threadId":"session-456","item":{"type":"agent_message","text":"item done"}}\n',
    )
    assert item_result.final_output == "item done"
    with pytest.raises(ValueError):
        driver.parse_result(
            request("session-123"),
            '{"type":"thread.started","thread_id":"session-999"}\n'
            '{"type":"message.completed","thread_id":"session-999","text":"wrong session"}\n',
        )


@pytest.mark.parametrize("stdout", [
    '{"type":"message.completed","text":"done"}\n',
    '{"type":"thread.started","thread_id":"a"}\n{"type":"thread.started","thread_id":"b"}\n',
    '{"type":"message.completed","thread_id":"a","text":"done"}\nnot-json\n',
])
def test_structured_output_rejects_missing_ambiguous_or_malformed_identity(stdout: str) -> None:
    driver = CodexAdapter().session_driver(
        exec_help=CODEX_EXEC_HELP, resume_help=CODEX_RESUME_HELP
    )
    with pytest.raises(ValueError):
        driver.parse_result(request(), stdout)


def test_reasonix_capabilities_and_steer_are_handshake_gated() -> None:
    driver = ReasonixAcpDriver.from_initialize({
        "capabilities": {"methods": [
            "session/new", "session/prompt", "session/update_config",
            "session/cancel", "session/close", "_reasonix.io/session/steer", "idempotency_key",
        ]}
    })
    assert driver.capabilities.session_identity is True
    assert driver.capabilities.resume_with_model is True
    # The current ACP driver owns no live stdio channel, so it must fail closed
    # instead of advertising an undeliverable mid-turn operation.
    assert driver.capabilities.mid_turn_steer is False
    assert driver.capabilities.idempotent_turn_start is True
    assert driver.capabilities.read_only_teardown is False
    assert '"method": "session/new"' in driver.build_session_open(request())
    assert '"sessionId": "session-123"' in driver.build_prompt(request(), session_id="session-123")
    assert '"method": "_reasonix.io/session/steer"' in driver.build_steer(
        session_id="session-123", message="finish at the next safe boundary"
    )
    update = driver.build_session_update_config(request(), session_id="session-123")
    assert '"method": "session/update_config"' in update
    assert '"model": "sol"' in update
    assert '"effort": "high"' in update
    assert '"method": "session/cancel"' in driver.build_session_cancel(session_id="session-123")
    assert '"method": "session/close"' in driver.build_session_close(session_id="session-123")


def test_reasonix_v1_nested_initialize_negotiates_live_capabilities() -> None:
    driver = ReasonixAcpDriver.from_initialize({
        "jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": {"resume": {}, "close": {}},
                "promptCapabilities": {"embeddedContext": True},
                "_meta": {"reasonix.io": {
                    "sessionSteer": {"method": "_reasonix.io/session/steer"}
                }},
            },
            "agentInfo": {"name": "reasonix", "version": "v1.18.0"},
        },
    })
    assert driver.capabilities.session_identity is True
    assert driver.capabilities.followup_turn is True
    # The nested metadata identifies a possible vendor method, but this
    # driver has no concurrent write channel while prompt() is active.
    assert driver.capabilities.mid_turn_steer is False
    assert driver.capabilities.resume_with_model is False


def test_reasonix_owned_invocation_uses_acp_argv_without_dir_flag(tmp_path: Path) -> None:
    driver = ReasonixAcpDriver.from_initialize({
        "jsonrpc": "2.0", "id": 1, "result": {
            "agentCapabilities": {"loadSession": True,
                "promptCapabilities": {"embeddedContext": True}},
        },
    })
    invocation = driver.build_invocation(request())
    assert invocation.argv == ("reasonix", "acp")
    assert invocation.prompt_mode == "owned-session"
    assert "USER" not in invocation.argv
    limited = ReasonixAcpDriver.from_initialize({"capabilities": {"methods": ["session/new"]}})
    assert limited.capabilities.mid_turn_steer is False
    with pytest.raises(RuntimeError):
        limited.build_steer(session_id="session-123", message="stop")
    with pytest.raises(RuntimeError):
        limited.build_session_update_config(request(), session_id="session-123")


def test_reasonix_acp_parses_open_prompt_streams_and_final_output() -> None:
    driver = ReasonixAcpDriver.from_initialize({
        "capabilities": {"methods": [
            "session/new", "session/prompt", "session/update_config",
            "session/cancel", "session/close",
        ]}
    })
    opened = '{"jsonrpc":"2.0","id":1,"result":{"sessionId":"rx-1"}}\n'
    assert driver.parse_session_open(opened) == "rx-1"
    prompt_output = (
        '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"rx-1","update":{"content":{"text":"hello "}}}}\n'
        '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"rx-1","update":{"content":{"text":"world"}}}}\n'
        '{"jsonrpc":"2.0","id":2,"result":{"sessionId":"rx-1","finalOutput":"hello world"}}\n'
    )
    result = driver.parse_result(
        request("rx-1"), prompt_output
    )
    assert result.session_id == "rx-1"
    assert result.final_output == "hello world"


@pytest.mark.parametrize("stdout", [
    '{"jsonrpc":"2.0","id":2,"result":{"sessionId":"other","finalOutput":"done"}}\n',
    '{"jsonrpc":"2.0","id":999,"result":{"sessionId":"rx-1","finalOutput":"wrong id"}}\n',
    '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"rx-1","update":{"content":{"text":"notification only"}}}}\n',
    '{"jsonrpc":"2.0","id":2,"result":{"sessionId":"rx-1","finalOutput":"a"}}\n{"jsonrpc":"2.0","id":2,"result":{"sessionId":"rx-1","finalOutput":"b"}}\n',
    '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"a","update":{"content":{"text":"a"}}}}\n{"jsonrpc":"2.0","id":2,"result":{"sessionId":"b","finalOutput":"b"}}\n',
    '{"jsonrpc":"2.0","id":2,"error":{"code":-1,"message":"InvalidRequest"}}\n',
    '{"jsonrpc":"2.0","id":2,"result":{"sessionId":"rx-1"}}\nnot-json\n',
])
def test_reasonix_acp_rejects_mismatched_error_or_malformed_wire(stdout: str) -> None:
    driver = ReasonixAcpDriver.from_initialize({
        "capabilities": {"methods": ["session/new", "session/prompt"]}
    })
    with pytest.raises(ValueError):
        driver.parse_result(request("rx-1"), stdout)


def test_reasonix_config_state_retains_complete_records_in_advertised_order() -> None:
    unknown = {"id": "vendor_option", "description": "opaque"}
    approval = {
        "id": "tool_approval", "type": "select", "currentValue": "ask",
        "options": [{"value": "ask"}, {"value": "yolo"}],
    }
    options = reasonix_module._require_config_options(
        {"result": {"configOptions": [unknown, approval]}}, stage="session/new"
    )
    assert options == (unknown, approval)


@pytest.mark.parametrize("response", [
    {},
    {"result": []},
    {"result": {}},
    {"result": {"configOptions": ()}},
    {"result": {"configOptions": ["SECRET"]}},
    {"result": {"configOptions": [{"id": ""}]}},
    {"result": {"configOptions": [{"id": "model"}, {"id": "model"}]}},
])
def test_reasonix_config_state_rejects_malformed_or_ambiguous_payloads_without_echo(
    response,
) -> None:
    with pytest.raises(
        RuntimeError,
        match=r"^Reasonix ACP session/resume response has invalid configOptions state$",
    ) as exc_info:
        reasonix_module._require_config_options(response, stage="session/resume")
    assert "SECRET" not in str(exc_info.value)


def test_reasonix_select_config_option_requires_exact_valid_advertised_value() -> None:
    option = {
        "id": "tool_approval", "type": "select", "currentValue": "ask",
        "options": [{"value": "ask"}, {"value": "yolo"}],
    }
    options = (option, {"id": "opaque"})
    assert reasonix_module._require_select_config_option(
        options, option_id="tool_approval", desired_value="yolo"
    ) is option
    with pytest.raises(RuntimeError, match="does not advertise required option 'model'"):
        reasonix_module._require_select_config_option(
            options, option_id="model", desired_value="sol"
        )
    with pytest.raises(RuntimeError, match="does not advertise required value 'auto'"):
        reasonix_module._require_select_config_option(
            options, option_id="tool_approval", desired_value="auto"
        )


@pytest.mark.parametrize("malformed", [
    {"id": "tool_approval", "currentValue": "ask", "options": [{"value": "yolo"}]},
    {"id": "tool_approval", "type": "boolean", "currentValue": "ask", "options": [{"value": "yolo"}]},
    {"id": "tool_approval", "type": "select", "currentValue": True, "options": [{"value": "yolo"}]},
    {"id": "tool_approval", "type": "select", "currentValue": "ask", "options": {}},
    {"id": "tool_approval", "type": "select", "currentValue": "ask", "options": ["SECRET"]},
    {"id": "tool_approval", "type": "select", "currentValue": "ask", "options": [{"value": ""}]},
])
def test_reasonix_select_config_option_rejects_malformed_records_without_echo(
    malformed,
) -> None:
    with pytest.raises(RuntimeError, match="is not a valid select option") as exc_info:
        reasonix_module._require_select_config_option(
            (malformed,), option_id="tool_approval", desired_value="yolo"
        )
    assert "SECRET" not in str(exc_info.value)


def test_reasonix_applied_config_values_require_exact_current_acknowledgements() -> None:
    options = (
        {"id": "tool_approval", "type": "select", "currentValue": "yolo",
         "options": [{"value": "ask"}, {"value": "yolo"}]},
        {"id": "model", "type": "select", "currentValue": "sol",
         "options": [{"value": "sol"}]},
    )
    reasonix_module._require_applied_config_values(
        options, {"tool_approval": "yolo", "model": "sol"}
    )
    reset_options = ({**options[0], "currentValue": "ask"}, options[1])
    with pytest.raises(RuntimeError, match="did not acknowledge required value 'yolo'"):
        reasonix_module._require_applied_config_values(
            reset_options, {"tool_approval": "yolo", "model": "sol"}
        )


def _reasonix_select_option(
    option_id: str, current_value: str, values: tuple[str, ...]
) -> dict[str, object]:
    return {
        "id": option_id,
        "type": "select",
        "currentValue": current_value,
        "options": [{"value": value} for value in values],
    }


def _reasonix_config_options(
    *, approval: str = "ask", model: str = "other", effort: str = "low"
) -> list[dict[str, object]]:
    return [
        _reasonix_select_option("tool_approval", approval, ("ask", "yolo")),
        _reasonix_select_option("model", model, ("other", "sol")),
        _reasonix_select_option("effort", effort, ("low", "high")),
    ]


class _FakeReasonixAcpProcess:
    def __init__(
        self,
        *,
        session_id: str = "fresh",
        open_response: dict[str, object] | None = None,
        update_responses: tuple[dict[str, object], ...] = (),
        update_error_at: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.open_response = open_response
        self.update_responses = update_responses
        self.update_error_at = update_error_at
        self.current = {
            "tool_approval": "ask",
            "model": "other",
            "effort": "low",
        }
        self.calls: list[tuple[str, dict[str, object], float]] = []
        self.notifications: list[dict[str, object]] = []
        self.last_request_id = 0
        self.update_count = 0
        self.closed = False

    def config_options(self) -> list[dict[str, object]]:
        return _reasonix_config_options(
            approval=self.current["tool_approval"],
            model=self.current["model"],
            effort=self.current["effort"],
        )

    def initialize(self) -> dict[str, object]:
        self.last_request_id = 1
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"embeddedContext": True},
                }
            },
        }

    def request(
        self, method: str, params: dict[str, object], *, timeout_seconds: float = 10.0
    ) -> dict[str, object]:
        self.calls.append((method, dict(params), timeout_seconds))
        self.last_request_id += 1
        if method in {"session/new", "session/resume"}:
            if self.open_response is not None:
                return self.open_response
            return self._response(config_options=self.config_options())
        if method == "session/set_config_option":
            self.update_count += 1
            if self.update_error_at == self.update_count:
                raise RuntimeError("SECRET raw JSON-RPC rejection")
            if self.update_count <= len(self.update_responses):
                return self.update_responses[self.update_count - 1]
            self.current[str(params["configId"])] = str(params["value"])
            return self._response(config_options=self.config_options())
        if method == "session/prompt":
            return self._response(final_output="DONE")
        raise AssertionError(f"unexpected fake ACP method: {method}")

    def _response(
        self,
        *,
        config_options: list[dict[str, object]] | None = None,
        final_output: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {"sessionId": self.session_id}
        if config_options is not None:
            result["configOptions"] = config_options
        if final_output is not None:
            result["finalOutput"] = final_output
        return {
            "jsonrpc": "2.0",
            "id": self.last_request_id,
            "result": result,
        }

    def close(self) -> None:
        self.closed = True


def _execute_fake_reasonix(
    monkeypatch,
    fake: _FakeReasonixAcpProcess,
    session_request: SessionRequest,
    *,
    control_callback=None,
):
    monkeypatch.setattr(ReasonixAcpProcess, "start", lambda **kwargs: fake)
    driver = ReasonixAcpDriver.from_initialize(
        {"capabilities": {"methods": ["session/new", "session/prompt"]}}
    )
    return driver.execute_session(
        session_request,
        driver.build_invocation(session_request),
        control_callback,
    )


def test_reasonix_owned_executor_sets_yolo_before_model_effort_and_prompt_for_new_session(
    monkeypatch,
) -> None:
    fake = _FakeReasonixAcpProcess()
    control_calls: list[str] = []
    execution = _execute_fake_reasonix(
        monkeypatch, fake, request(), control_callback=lambda: control_calls.append("control")
    )

    assert [method for method, _, _ in fake.calls] == [
        "session/new",
        "session/set_config_option",
        "session/set_config_option",
        "session/set_config_option",
        "session/prompt",
    ]
    assert [call[1] for call in fake.calls[1:4]] == [
        {"sessionId": "fresh", "configId": "tool_approval", "value": "yolo"},
        {"sessionId": "fresh", "configId": "model", "value": "sol"},
        {"sessionId": "fresh", "configId": "effort", "value": "high"},
    ]
    assert fake.calls[4][1]["prompt"][0]["type"] == "text"
    assert fake.calls[4][2] > 60
    assert control_calls == ["control"]
    assert execution.result.session_id == "fresh"
    assert execution.result.final_output == "DONE"
    assert execution.result.model == "sol"
    assert execution.result.effort == "high"
    assert "tool_approval" in execution.raw_transport
    assert any("configOptions" in event.get("result", {}) for event in execution.events)
    assert not hasattr(execution.result, "raw_transport")
    assert fake.closed is True


def test_reasonix_owned_executor_sets_yolo_before_model_effort_and_prompt_for_resume(
    monkeypatch,
) -> None:
    fake = _FakeReasonixAcpProcess(session_id="source")
    session_request = request("source", model=None, effort=None)
    execution = _execute_fake_reasonix(monkeypatch, fake, session_request)

    assert [method for method, _, _ in fake.calls] == [
        "session/resume", "session/set_config_option", "session/prompt"
    ]
    assert fake.calls[1][1] == {
        "sessionId": "source",
        "configId": "tool_approval",
        "value": "yolo",
    }
    assert execution.result.session_id == "source"
    assert execution.result.model is None
    assert execution.result.effort is None
    assert execution.result.final_output == "DONE"
    assert fake.closed is True


@pytest.mark.parametrize(
    ("scenario", "expected_error", "expected_updates"),
    [
        ("missing_state", "invalid configOptions state", 0),
        ("non_list_state", "invalid configOptions state", 0),
        ("missing_id", "invalid configOptions state", 0),
        ("duplicate_id", "invalid configOptions state", 0),
        ("missing_approval", "does not advertise required option 'tool_approval'", 0),
        ("non_select_approval", "is not a valid select option", 0),
        ("missing_values", "is not a valid select option", 0),
        ("invalid_values", "is not a valid select option", 0),
        ("missing_yolo", "does not advertise required value 'yolo'", 0),
        ("missing_model", "does not advertise required option 'model'", 0),
        ("invalid_effort", "does not advertise required value 'high'", 0),
        ("update_rejected", "option 'tool_approval' update failed", 1),
        ("update_missing_state", "invalid configOptions state", 1),
        ("update_not_acknowledged", "did not acknowledge required value 'yolo'", 1),
        ("approval_reset_by_model", "did not acknowledge required value 'yolo'", 2),
    ],
)
def test_reasonix_owned_executor_fails_closed_before_prompt(
    monkeypatch, scenario: str, expected_error: str, expected_updates: int
) -> None:
    fake = _FakeReasonixAcpProcess()
    if scenario == "missing_state":
        fake.open_response = {"result": {"sessionId": "PRIVATE SESSION"}}
    elif scenario == "non_list_state":
        fake.open_response = {
            "result": {"sessionId": "PRIVATE SESSION", "configOptions": {}}
        }
    elif scenario == "missing_id":
        fake.open_response = {
            "result": {"sessionId": "PRIVATE SESSION", "configOptions": [{}]}
        }
    elif scenario == "duplicate_id":
        duplicate = _reasonix_select_option("tool_approval", "ask", ("ask", "yolo"))
        fake.open_response = {
            "result": {
                "sessionId": "PRIVATE SESSION",
                "configOptions": [duplicate, duplicate],
            }
        }
    elif scenario == "missing_approval":
        fake.open_response = {
            "result": {
                "sessionId": "PRIVATE SESSION",
                "configOptions": _reasonix_config_options()[1:],
            }
        }
    elif scenario == "non_select_approval":
        options = _reasonix_config_options()
        options[0]["type"] = "boolean"
        fake.open_response = {"result": {"sessionId": "PRIVATE SESSION", "configOptions": options}}
    elif scenario == "missing_values":
        options = _reasonix_config_options()
        options[0].pop("options")
        fake.open_response = {"result": {"sessionId": "PRIVATE SESSION", "configOptions": options}}
    elif scenario == "invalid_values":
        options = _reasonix_config_options()
        options[0]["options"] = [{"value": "yolo"}, {"value": ""}]
        fake.open_response = {"result": {"sessionId": "PRIVATE SESSION", "configOptions": options}}
    elif scenario == "missing_yolo":
        options = _reasonix_config_options()
        options[0] = _reasonix_select_option("tool_approval", "ask", ("ask",))
        fake.open_response = {"result": {"sessionId": "PRIVATE SESSION", "configOptions": options}}
    elif scenario == "missing_model":
        options = [option for option in _reasonix_config_options() if option["id"] != "model"]
        fake.open_response = {"result": {"sessionId": "PRIVATE SESSION", "configOptions": options}}
    elif scenario == "invalid_effort":
        options = _reasonix_config_options()
        options[2] = _reasonix_select_option("effort", "low", ("low",))
        fake.open_response = {"result": {"sessionId": "PRIVATE SESSION", "configOptions": options}}
    elif scenario == "update_rejected":
        fake.update_error_at = 1
    elif scenario == "update_missing_state":
        fake.update_responses = ({"result": {"sessionId": "PRIVATE SESSION"}},)
    elif scenario == "update_not_acknowledged":
        fake.update_responses = (
            {"result": {"sessionId": "PRIVATE SESSION", "configOptions": _reasonix_config_options()}},
        )
    elif scenario == "approval_reset_by_model":
        fake.update_responses = (
            {"result": {"sessionId": "PRIVATE SESSION", "configOptions": _reasonix_config_options(approval="yolo")}},
            {"result": {"sessionId": "PRIVATE SESSION", "configOptions": _reasonix_config_options(model="sol")}},
        )
    else:
        raise AssertionError(f"unknown scenario: {scenario}")

    control_calls: list[str] = []
    with pytest.raises(RuntimeError, match=expected_error) as exc_info:
        _execute_fake_reasonix(
            monkeypatch,
            fake,
            request(),
            control_callback=lambda: control_calls.append("control"),
        )

    methods = [method for method, _, _ in fake.calls]
    assert methods.count("session/set_config_option") == expected_updates
    assert "session/prompt" not in methods
    assert control_calls == []
    assert fake.closed is True
    error = str(exc_info.value)
    assert len(error) < 160
    for secret in ("SECRET", "PRIVATE SESSION", "SYSTEM", "USER"):
        assert secret not in error


def test_reasonix_transport_accepts_long_prompt_timeout_without_fixed_sixty_second_cap(monkeypatch):
    class Stream:
        def write(self, value): pass
        def flush(self): pass
        def readline(self): return '{"jsonrpc":"2.0","id":1,"result":{}}\n'
    class Proc:
        stdin = Stream()
        stdout = Stream()
    seen = []
    monkeypatch.setattr(reasonix_module.select, "select", lambda r, w, e, timeout: (seen.append(timeout) or ([r[0]], [], [])))
    ReasonixAcpProcess(Proc()).request("session/prompt", {}, timeout_seconds=3600.0)
    assert seen == [3600.0]


def test_reasonix_discovery_is_initialize_only_and_does_not_mutate_model(monkeypatch, tmp_path: Path):
    class FakeProcess:
        calls = []
        def initialize(self):
            self.calls.append("initialize")
            return {"result": {"agentCapabilities": {"loadSession": True, "promptCapabilities": {"embeddedContext": True}}}}
        def close(self): self.calls.append("close")
    fake = FakeProcess()
    monkeypatch.setattr(workflow_module.shutil, "which", lambda name: "/usr/local/bin/reasonix")
    monkeypatch.setattr(ReasonixAcpProcess, "start", lambda **kwargs: fake)
    driver = workflow_module._discover_session_driver(ReasonixAdapter(), repo_root=tmp_path)
    assert driver is not None
    assert fake.calls == ["initialize", "close"]
    assert driver.capabilities.resume_with_model is False
    assert driver.capabilities.mid_turn_steer is False


def test_no_session_driver_is_explicitly_one_shot() -> None:
    assert NoSessionDriver.capabilities == NO_SESSION_CAPABILITIES
    with pytest.raises(RuntimeError):
        NoSessionDriver().build_invocation(request())


def test_fake_session_driver_controls_capabilities_midturn_handover_and_target_start() -> None:
    driver = FakeSessionDriver()
    assert driver.request_mid_turn("finish safely") is True
    assert driver.handover(request()) == "Objective: continue the work."
    result = driver.start(request(), target=True)
    assert result.provider_operation_id == "fake-operation-1"
    assert driver.calls == ["steer:finish safely", "handover", "target-start"]
    limited = FakeSessionDriver(
        capabilities=SessionCapabilities(session_identity=True, followup_turn=False)
    )
    assert limited.request_mid_turn("finish") is False
    with pytest.raises(RuntimeError):
        limited.handover(request())


@pytest.mark.parametrize("crash_point", ["before_provider_operation_id", "after_provider_operation_id"])
def test_fake_session_driver_exposes_provider_operation_crash_boundaries(crash_point: str) -> None:
    driver = FakeSessionDriver(crash_point=crash_point)
    with pytest.raises(RuntimeError, match="provider|ambiguous"):
        driver.start(request(), target=True)
    assert driver.calls == ["target-start"]


def test_jsonl_parser_keeps_wire_events_separate() -> None:
    events = parse_jsonl_events('{"type":"progress","value":1}\n')
    assert events == ({"type": "progress", "value": 1},)

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


def request(session_id: str | None = None) -> SessionRequest:
    return SessionRequest(
        repo_root=Path("/repo"), selector="codex.sol", model="sol", effort="high",
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


def test_reasonix_owned_executor_uses_exact_resume_config_and_prompt_wire(monkeypatch, tmp_path: Path) -> None:
    class FakeProcess:
        def __init__(self):
            self.calls = []
            self.last_request_id = 0
            self.notifications = []
        def initialize(self):
            self.last_request_id = 1
            return {"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {"loadSession": True, "promptCapabilities": {"embeddedContext": True}}}}
        def request(self, method, params, *, timeout_seconds=10.0):
            self.calls.append((method, dict(params), timeout_seconds))
            self.last_request_id += 1
            if method == "session/resume":
                return {"jsonrpc": "2.0", "id": self.last_request_id, "result": {"sessionId": "source", "configOptions": [{"id": "model"}, {"id": "effort"}]}}
            if method == "session/prompt":
                return {"jsonrpc": "2.0", "id": self.last_request_id, "result": {"sessionId": "source", "finalOutput": "DONE"}}
            return {"jsonrpc": "2.0", "id": self.last_request_id, "result": {"sessionId": "source"}}
        def close(self):
            self.closed = True
    fake = FakeProcess()
    monkeypatch.setattr(ReasonixAcpProcess, "start", lambda **kwargs: fake)
    driver = ReasonixAcpDriver.from_initialize({"capabilities": {"methods": ["session/new", "session/prompt"]}})
    result = driver.execute_session(request("source"), driver.build_invocation(request("source")))
    assert result.result.final_output == "DONE"
    assert [method for method, _, _ in fake.calls] == ["session/resume", "session/set_config_option", "session/set_config_option", "session/prompt"]
    assert fake.calls[1][1] == {"sessionId": "source", "configId": "model", "value": "sol"}
    assert fake.calls[2][1] == {"sessionId": "source", "configId": "effort", "value": "high"}
    assert fake.calls[3][1]["prompt"][0]["type"] == "text"
    assert fake.calls[3][2] > 60


def test_reasonix_owned_executor_fails_before_prompt_when_config_option_missing(monkeypatch, tmp_path: Path) -> None:
    class FakeProcess:
        def __init__(self): self.calls = []; self.notifications = []
        def initialize(self): return {"result": {"agentCapabilities": {"loadSession": True}}}
        def request(self, method, params, **kwargs):
            self.calls.append(method)
            return {"result": {"sessionId": "fresh", "configOptions": []}}
        def close(self): pass
    fake = FakeProcess()
    monkeypatch.setattr(ReasonixAcpProcess, "start", lambda **kwargs: fake)
    driver = ReasonixAcpDriver.from_initialize({"capabilities": {"methods": ["session/new"]}})
    with pytest.raises(RuntimeError, match="cannot configure"):
        driver.execute_session(request(), driver.build_invocation(request()))
    assert "session/prompt" not in fake.calls


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
    assert driver.config_update_method is None


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

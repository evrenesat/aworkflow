from __future__ import annotations

import json
import subprocess
import select
import time
from pathlib import Path
from typing import Any, Mapping

from .base import HarnessInvocation
from .session import SessionCapabilities, SessionExecutionResult, SessionRequest, SessionResult
from .preflight import (
    HarnessEnvironmentBlocker,
    HarnessPreflightContext,
    HarnessPreflightProbe,
    REASONIX_BWRAP_REMEDIATION,
    diagnostic_fields,
)


def _collect_acp_session_ids(value: object, found: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"sessionId", "session_id"}:
                if not isinstance(child, str) or not child:
                    raise ValueError("Reasonix ACP session id must be a non-empty string")
                found.add(child)
            else:
                _collect_acp_session_ids(child, found)
    elif isinstance(value, list):
        for child in value:
            _collect_acp_session_ids(child, found)


def _collect_acp_text(value: object, streamed: list[str], finals: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"finalOutput", "final_output", "output"} and isinstance(child, str):
                finals.append(child)
            elif key in {"text", "delta"} and isinstance(child, str):
                streamed.append(child)
            elif key in {"content", "message", "update", "result", "params"}:
                _collect_acp_text(child, streamed, finals)
            elif isinstance(child, (Mapping, list)):
                _collect_acp_text(child, streamed, finals)
    elif isinstance(value, list):
        for child in value:
            _collect_acp_text(child, streamed, finals)


def parse_acp_jsonrpc(
    stdout: str,
    *,
    expected_response_id: int | str | None = None,
    expected_session_id: str | None = None,
    require_output: bool = False,
) -> tuple[tuple[Mapping[str, Any], ...], str | None, str | None, Mapping[str, Any] | None]:
    """Parse bounded ACP JSON-RPC responses, notifications, and streamed output."""
    events: list[Mapping[str, Any]] = []
    session_ids: set[str] = set()
    streamed: list[str] = []
    finals: list[str] = []
    matched_result: Mapping[str, Any] | None = None
    matching_response_count = 0
    for line_number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Reasonix ACP line {line_number} is not JSON") from exc
        if not isinstance(event, Mapping) or event.get("jsonrpc") != "2.0":
            raise ValueError(f"Reasonix ACP line {line_number} is not JSON-RPC 2.0")
        error = event.get("error")
        if isinstance(error, Mapping):
            message = error.get("message", "provider error")
            raise ValueError(f"Reasonix ACP error: {message}")
        _collect_acp_session_ids(event, session_ids)
        _collect_acp_text(event, streamed, finals)
        if "id" in event and "result" in event:
            if expected_response_id is None or event.get("id") == expected_response_id:
                matching_response_count += 1
                matched_result = event.get("result") if isinstance(event.get("result"), Mapping) else None
        events.append(event)
    if expected_response_id is not None and matching_response_count != 1:
        raise ValueError(
            f"Reasonix ACP expected exactly one response id {expected_response_id}, "
            f"got {matching_response_count}"
        )
    if len(session_ids) > 1:
        raise ValueError("Reasonix ACP output contains mismatched session ids")
    session_id = next(iter(session_ids), None)
    if expected_session_id is not None and session_id != expected_session_id:
        raise ValueError("Reasonix ACP output session id does not match the requested session")
    final_output = finals[-1] if finals else "".join(streamed)
    if require_output and not final_output:
        raise ValueError("Reasonix ACP output did not contain a final or streamed response")
    return tuple(events), session_id, final_output or None, matched_result


class ReasonixAdapter:
    """Harness adapter for the Reasonix AI coding agent.

    Reasonix does not expose an ``--effort`` flag; reasoning effort is baked
    into each model variant (e.g. ``deepseek-flash`` vs ``deepseek-pro-max``),
    so ``supports_effort`` is ``False``.

    Permission bypass is handled through the Reasonix config file (``reasonix.toml``)
    rather than CLI flags — the user should set ``[permissions] mode = "allow"``
    or appropriate allow rules for non-interactive use.
    """

    name = "reasonix"
    supports_effort = False

    def session_driver(self, initialize_payload: Mapping[str, Any]) -> "ReasonixAcpDriver":
        return ReasonixAcpDriver.from_initialize(initialize_payload)

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ) -> HarnessInvocation:
        effective_prompt = "\n\n".join((system_prompt, user_prompt))
        # Current Reasonix releases expose the directory option only as
        # ``--dir``.  The old single-dash spelling exits during argument
        # parsing before the agent can produce diagnostics.
        argv: list[str] = ["reasonix", "run", "--dir", str(repo_root)]
        if model is not None:
            argv.extend(["--model", model])
        argv.append(effective_prompt)
        final_output_argv = [*argv[:-1], "--print", effective_prompt]
        return HarnessInvocation(
            label=self.name,
            argv=tuple(argv),
            env={},
            prompt_mode="prefix-system-into-user-prompt",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
            final_output_argv=tuple(final_output_argv),
        )

    def preflight_environment(
        self,
        context: HarnessPreflightContext,
        probe: HarnessPreflightProbe,
    ) -> HarnessEnvironmentBlocker | None:
        resolved = probe.resolve_executable(context.invocation.argv[0], env=context.env)
        if resolved is None:
            return None
        try:
            result = probe.run_diagnostic(
                (resolved, "doctor", "--json"),
                cwd=context.cwd,
                env=context.env,
                timeout_seconds=5.0,
            )
        except (OSError, NotImplementedError, TimeoutError, TypeError, ValueError):
            return None
        returncode, stdout, timed_out = diagnostic_fields(result)
        if timed_out or returncode != 0 or not stdout:
            return None
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            return None
        sandbox = payload.get("sandbox") if isinstance(payload, dict) else None
        if not isinstance(sandbox, dict) or sandbox.get("bash") != "enforce":
            return None
        if probe.resolve_executable("bwrap", env=context.env) is not None:
            return None
        return HarnessEnvironmentBlocker(
            "harness_environment_preflight",
            "reasonix_sandbox_bwrap_missing",
            self.name,
            "bwrap",
            ("reasonix", "doctor", "--json"),
            REASONIX_BWRAP_REMEDIATION,
            {"sandbox_bash": "enforce"},
        )


class ReasonixAcpProcess:
    """Owned stdio ACP seam with correlated request/response handling."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._next_id = 1
        self.last_request_id: int | None = None
        self.notifications: list[Mapping[str, Any]] = []

    @classmethod
    def start(cls, *, repo_root: Path, executable: str = "reasonix") -> "ReasonixAcpProcess":
        process = subprocess.Popen(
            [executable, "acp"], cwd=str(repo_root),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        return cls(process)

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout_seconds: float = 10.0
    ) -> Mapping[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("Reasonix ACP stdio transport is unavailable")
        request_id = self._next_id
        self._next_id += 1
        self.last_request_id = request_id
        self.process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method,
            "params": dict(params),
        }) + "\n")
        self.process.stdin.flush()
        while True:
            ready, _, _ = select.select([self.process.stdout], [], [], timeout_seconds)
            if not ready:
                raise TimeoutError("Reasonix ACP response timed out")
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Reasonix ACP exited before correlated response")
            event = json.loads(line)
            if not isinstance(event, Mapping) or event.get("jsonrpc") != "2.0":
                raise ValueError("Reasonix ACP emitted a non-JSON-RPC event")
            if "method" in event and "id" in event:
                raise RuntimeError(
                    "Reasonix ACP agent request requires an owned client handler: "
                    f"{event.get('method')}"
                )
            if "method" in event and "id" not in event:
                self.notifications.append(event)
                continue
            if event.get("id") != request_id:
                raise ValueError("Reasonix ACP response id mismatch")
            if "error" in event:
                raise RuntimeError(f"Reasonix ACP error: {event['error']}")
            return event

    def initialize(self) -> Mapping[str, Any]:
        """Perform the no-prompt ACP handshake and return its JSON-RPC result."""
        return self.request("initialize", {})

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        self.process.terminate()


class ReasonixAcpDriver:
    """Small ACP transport contract; the caller owns the stdio process."""

    def __init__(self, capabilities: SessionCapabilities, methods: frozenset[str]) -> None:
        self.capabilities = capabilities
        self.methods = methods
        self.executable = "reasonix"
        self.config_update_method: str | None = None

    @classmethod
    def from_initialize(cls, payload: Mapping[str, Any]) -> "ReasonixAcpDriver":
        raw_payload = payload.get("result", payload)
        if not isinstance(raw_payload, Mapping):
            raise ValueError("Reasonix ACP initialize result must be an object")
        agent = raw_payload.get("agentCapabilities", raw_payload)
        raw = agent.get("capabilities", agent) if isinstance(agent, Mapping) else agent
        if not isinstance(raw, Mapping):
            raise ValueError("Reasonix ACP initialize capabilities must be an object")
        methods_raw = raw.get("methods", raw.get("supported_methods", ()))
        methods = frozenset(value for value in methods_raw if isinstance(value, str)) if isinstance(methods_raw, list) else frozenset()
        nested_handshake = "agentCapabilities" in raw_payload
        session_identity = (
            "session/new" in methods or "session/open" in methods
            or bool(agent.get("loadSession")) if isinstance(agent, Mapping) else False
        )
        followup = (
            "session/prompt" in methods or "session/send" in methods
            or bool(agent.get("promptCapabilities")) if isinstance(agent, Mapping) else False
        )
        resume = "session/update_config" in methods or "session/set_config" in methods
        steer_advertised = "_reasonix.io/session/steer" in methods
        if isinstance(agent, Mapping):
            meta = agent.get("_meta")
            reasonix_meta = meta.get("reasonix.io") if isinstance(meta, Mapping) else None
            steer_advertised = steer_advertised or (
                isinstance(reasonix_meta, Mapping)
                and isinstance(reasonix_meta.get("sessionSteer"), Mapping)
                and isinstance(reasonix_meta["sessionSteer"].get("method"), str)
            )
        return cls(
            SessionCapabilities(
                session_identity=session_identity,
                followup_turn=followup,
                resume_with_model=resume,
                # Flat fixtures remain fail-closed; the nested v1 handshake is
                # only advertised by the owned ReasonixAcpProcess seam.
                # The current executor has no concurrent request writer while
                # prompt is being read; do not advertise an undeliverable steer.
                mid_turn_steer=False,
                # A method name alone cannot prove provider-enforced
                # read-only teardown. CP4 may advertise this after it adds
                # an enforceable control operation.
                read_only_teardown=False,
                idempotent_turn_start="idempotency_key" in methods,
            ),
            methods,
        )

    def _request(self, method: str, params: Mapping[str, Any], request_id: int = 1) -> str:
        if method not in self.methods:
            raise RuntimeError(f"Reasonix ACP method is not advertised: {method}")
        return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        """Describe the ACP process for secret-safe preflight and turn artifacts."""
        return HarnessInvocation(
            label="reasonix-acp",
            argv=(self.executable, "acp"),
            env={}, prompt_mode="owned-session", system_prompt=request.system_prompt,
            user_prompt=request.user_prompt, effective_prompt="",
        )

    def execute_session(
        self, request: SessionRequest, invocation: HarnessInvocation,
        control_callback: Any | None = None,
    ) -> SessionExecutionResult:
        process = ReasonixAcpProcess.start(repo_root=request.repo_root, executable=invocation.argv[0])
        wire: list[Mapping[str, Any]] = []
        try:
            initialize = process.initialize()
            wire.append(initialize)
            negotiated = ReasonixAcpDriver.from_initialize(initialize)
            self.capabilities = negotiated.capabilities
            session_params: dict[str, Any] = {"cwd": str(request.repo_root)}
            if request.session_id is not None:
                session_params["sessionId"] = request.session_id
                opened = process.request("session/resume", session_params)
            else:
                opened = process.request("session/new", session_params)
            wire.extend(process.notifications)
            process.notifications.clear()
            wire.append(opened)
            opened_result = opened.get("result")
            if not isinstance(opened_result, Mapping) or not isinstance(opened_result.get("sessionId"), str):
                raise ValueError("Reasonix ACP session response has no exact session id")
            session_id = opened_result["sessionId"]
            options = {
                item.get("id") for item in opened_result.get("configOptions", [])
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            }
            if request.model is not None and "model" not in options:
                raise RuntimeError("Reasonix ACP session cannot configure the target model")
            if request.effort is not None and "effort" not in options:
                raise RuntimeError("Reasonix ACP session cannot configure target effort")
            config_method = self.config_update_method
            if request.model is not None or request.effort is not None:
                config = {}
                if request.model is not None:
                    config["model"] = request.model
                if request.effort is not None:
                    config["effort"] = request.effort
                for config_id, value in config.items():
                    updated = process.request(config_method or "session/set_config_option", {
                        "sessionId": session_id, "configId": config_id, "value": value,
                    })
                    wire.extend(process.notifications)
                    process.notifications.clear()
                    wire.append(updated)
            if control_callback is not None:
                control_callback()
            prompt = process.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "\n\n".join((request.system_prompt, request.user_prompt))}],
            }, timeout_seconds=3600.0)
            wire.extend(process.notifications)
            wire.append(prompt)
            raw = "\n".join(json.dumps(item) for item in wire) + "\n"
            _events, parsed_id, final_output, _matched = parse_acp_jsonrpc(
                raw, expected_response_id=process.last_request_id,
                expected_session_id=request.session_id, require_output=True,
            )
            if parsed_id is None or final_output is None:
                raise ValueError("Reasonix ACP prompt did not return exact output")
            return SessionExecutionResult(
                result=SessionResult(
                    session_id=parsed_id, selector=request.selector, model=request.model,
                    effort=request.effort, final_output=final_output,
                    capabilities=self.capabilities,
                ), raw_transport=raw, events=tuple(wire),
            )
        finally:
            process.close()

    def build_initialize(self, *, client_name: str = "aflow") -> str:
        return json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": client_name, "version": "1"}},
        })

    def build_session_open(self, request: SessionRequest) -> str:
        method = "session/open" if "session/open" in self.methods else "session/new"
        return self._request(method, {"cwd": str(request.repo_root), "model": request.model})

    def build_prompt(self, request: SessionRequest, *, session_id: str) -> str:
        method = "session/prompt" if "session/prompt" in self.methods else "session/send"
        params: dict[str, Any] = {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "\n\n".join((request.system_prompt, request.user_prompt))}],
        }
        if request.idempotency_key is not None:
            params["idempotencyKey"] = request.idempotency_key
        return self._request(method, params, request_id=2)

    def build_session_update_config(self, request: SessionRequest, *, session_id: str) -> str:
        method = (
            "session/update_config"
            if "session/update_config" in self.methods
            else "session/set_config"
        )
        if method not in self.methods:
            raise RuntimeError("Reasonix ACP model/config update is not advertised")
        config: dict[str, Any] = {}
        if request.model is not None:
            config["model"] = request.model
        if request.effort is not None:
            config["effort"] = request.effort
        return self._request(
            method, {"sessionId": session_id, "config": config}, request_id=4
        )

    def build_session_cancel(self, *, session_id: str) -> str:
        method = "session/cancel"
        return self._request(method, {"sessionId": session_id}, request_id=5)

    def build_session_close(self, *, session_id: str) -> str:
        method = "session/close"
        return self._request(method, {"sessionId": session_id}, request_id=6)

    def build_steer(self, *, session_id: str, message: str) -> str:
        return self._request(
            "_reasonix.io/session/steer",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": message}]},
            request_id=3,
        )

    def parse_result(
        self, request: SessionRequest, stdout: str, *, returncode: int = 0
    ) -> SessionResult:
        events, session_id, final_output, _result = parse_acp_jsonrpc(
            stdout, expected_response_id=2, expected_session_id=request.session_id,
            require_output=True,
        )
        if session_id is None or final_output is None:
            raise ValueError("Reasonix ACP prompt result has no exact session or final output")
        return SessionResult(
            session_id=session_id, selector=request.selector, model=request.model,
            effort=request.effort, final_output=final_output,
            structured_events=events, idempotency_key=request.idempotency_key,
            capabilities=self.capabilities,
            failure=(None if returncode == 0 else f"session exited with return code {returncode}"),
        )

    def parse_session_open(self, stdout: str) -> str:
        _events, session_id, _output, result = parse_acp_jsonrpc(
            stdout, expected_response_id=1
        )
        if session_id is None or not isinstance(result, Mapping):
            raise ValueError("Reasonix ACP session/open result has no exact session id")
        return session_id

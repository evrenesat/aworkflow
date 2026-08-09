from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .base import HarnessInvocation
from .session import SessionCapabilities, SessionRequest, SessionResult, parse_structured_result
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


class ReasonixAcpDriver:
    """Small ACP transport contract; the caller owns the stdio process."""

    def __init__(self, capabilities: SessionCapabilities, methods: frozenset[str]) -> None:
        self.capabilities = capabilities
        self.methods = methods

    @classmethod
    def from_initialize(cls, payload: Mapping[str, Any]) -> "ReasonixAcpDriver":
        raw = payload.get("capabilities", payload)
        if not isinstance(raw, Mapping):
            raise ValueError("Reasonix ACP initialize capabilities must be an object")
        methods_raw = raw.get("methods", raw.get("supported_methods", ()))
        methods = frozenset(value for value in methods_raw if isinstance(value, str)) if isinstance(methods_raw, list) else frozenset()
        session_identity = "session/new" in methods or "session/open" in methods
        followup = "session/prompt" in methods or "session/send" in methods
        resume = "session/update_config" in methods or "session/set_config" in methods
        return cls(
            SessionCapabilities(
                session_identity=session_identity,
                followup_turn=followup,
                resume_with_model=resume,
                mid_turn_steer="_reasonix.io/session/steer" in methods,
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
            "prompt": "\n\n".join((request.system_prompt, request.user_prompt)),
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
            {"sessionId": session_id, "message": message},
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

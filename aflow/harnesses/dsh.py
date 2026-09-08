from __future__ import annotations

from collections import deque
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .base import HarnessInvocation
from .session import (
    SessionCapabilities,
    SessionExecutionResult,
    SessionRequest,
    SessionResult,
)


ACP_PROTOCOL_VERSION = 1
DSH_CONTROL_TIMEOUT_SECONDS = 60.0
DSH_PROMPT_TIMEOUT_SECONDS = 3600.0


def _result(response: Mapping[str, Any], *, stage: str) -> Mapping[str, Any]:
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise ValueError(f"DSH ACP {stage} response has no result object")
    return result


def _config_options(
    response: Mapping[str, Any],
    *,
    stage: str,
) -> tuple[Mapping[str, Any], ...]:
    result = _result(response, stage=stage)
    raw = result.get("configOptions")
    if not isinstance(raw, list):
        raise ValueError(f"DSH ACP {stage} response has no configOptions array")

    options: list[Mapping[str, Any]] = []
    ids: set[str] = set()

    for option in raw:
        if not isinstance(option, Mapping):
            raise ValueError(f"DSH ACP {stage} contains an invalid config option")

        option_id = option.get("id")
        if not isinstance(option_id, str) or not option_id or option_id in ids:
            raise ValueError(f"DSH ACP {stage} contains an invalid config option id")

        ids.add(option_id)
        options.append(option)

    return tuple(options)


def _option_values(option: Mapping[str, Any]) -> set[str]:
    """Flatten both normal and provider-grouped ACP select options."""

    raw = option.get("options")
    if not isinstance(raw, list):
        raise ValueError(f"DSH ACP option {option.get('id')!r} has no option list")

    values: set[str] = set()

    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("DSH ACP select option contains an invalid entry")

        value = item.get("value")
        if isinstance(value, str):
            values.add(value)
            continue

        # DSH's model selector groups choices by provider:
        #
        # {
        #   "group": "zai",
        #   "options": [
        #       {"value": "[\"zai\",\"glm-5.3-flash\"]", ...}
        #   ]
        # }
        children = item.get("options")
        if not isinstance(children, list):
            raise ValueError("DSH ACP select option contains an invalid option group")

        for child in children:
            if not isinstance(child, Mapping):
                raise ValueError(
                    "DSH ACP select option group contains an invalid entry"
                )

            child_value = child.get("value")
            if not isinstance(child_value, str):
                raise ValueError("DSH ACP select option has an invalid value")

            values.add(child_value)

    return values


def _require_select_option(
    options: tuple[Mapping[str, Any], ...],
    *,
    option_id: str,
    desired_value: str,
) -> Mapping[str, Any]:
    option = next(
        (item for item in options if item.get("id") == option_id),
        None,
    )

    if option is None:
        raise RuntimeError(f"DSH ACP does not advertise config option {option_id!r}")

    if option.get("type") != "select":
        raise RuntimeError(f"DSH ACP option {option_id!r} is not a select option")

    if desired_value not in _option_values(option):
        raise RuntimeError(
            f"DSH ACP option {option_id!r} does not advertise value {desired_value!r}"
        )

    return option


def _require_current_value(
    options: tuple[Mapping[str, Any], ...],
    *,
    option_id: str,
    desired_value: str,
) -> None:
    option = _require_select_option(
        options,
        option_id=option_id,
        desired_value=desired_value,
    )

    if option.get("currentValue") != desired_value:
        raise RuntimeError(f"DSH ACP did not apply {option_id}={desired_value!r}")


def _parse_model_route(model: str) -> tuple[str, str]:
    provider, separator, model_id = model.partition("/")

    if not separator or not provider or not model_id:
        raise ValueError(
            "DSH model profiles must use provider/model syntax, "
            "for example 'zai/glm-5.3-flash'"
        )

    return provider, model_id


def _model_option_value(model: str) -> str:
    provider, model_id = _parse_model_route(model)

    # DSH's AcpModelControl uses JSON.stringify([provider, model]).
    return json.dumps(
        [provider, model_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _assistant_text(
    events: tuple[Mapping[str, Any], ...],
    *,
    session_id: str,
) -> str:
    parts: list[str] = []

    for event in events:
        if event.get("method") != "session/update":
            continue

        params = event.get("params")
        if not isinstance(params, Mapping):
            continue

        if params.get("sessionId") != session_id:
            continue

        update = params.get("update")
        if not isinstance(update, Mapping):
            continue

        if update.get("sessionUpdate") != "agent_message_chunk":
            continue

        content = update.get("content")
        if not isinstance(content, Mapping):
            continue

        if content.get("type") != "text":
            continue

        text = content.get("text")
        if isinstance(text, str):
            parts.append(text)

    return "".join(parts)


class DshAdapter:
    name = "dsh"
    supports_effort = True

    # Manager calls currently go through HarnessAdapter.build_invocation rather
    # than SessionDriver. Keep this false until AFlow routes manager calls
    # through ACP as well.
    manager_workspace_read = False

    def session_driver(
        self,
        initialize_payload: Mapping[str, Any],
    ) -> "DshAcpDriver":
        return DshAcpDriver.from_initialize(initialize_payload)

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ) -> HarnessInvocation:
        # Ordinary AFlow workflow turns should discover DSH ACP and therefore
        # use DshAcpDriver.build_invocation().
        #
        # Falling back to headless with a requested model would silently use
        # DSH's process-wide default model instead, so fail loudly.
        if model is not None or effort is not None:
            raise RuntimeError(
                "DSH model/effort selection requires the ACP session driver"
            )

        effective_prompt = "\n\n".join((system_prompt, user_prompt))

        return HarnessInvocation(
            label=self.name,
            argv=(
                "dsh",
                "--profile",
                "headless",
                effective_prompt,
            ),
            env={"DSH_PERMISSION_MODE": "danger-full-access"},
            prompt_mode="prefix-system-into-user-prompt",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
        )


class DshAcpProcess:
    """One owned DSH ACP stdio process."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._next_id = 1

        self.received: list[Mapping[str, Any]] = []
        self.raw_lines: list[str] = []
        # Read lines in a thread: select()+TextIOWrapper.readline() can stall
        # on already-buffered lines and cannot enforce partial-line timeouts.
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            name="aflow-dsh-acp-stdout",
            daemon=True,
        )
        self._stdout_thread.start()

        # DSH logs belong on stderr. Drain it continuously so a long-running
        # session cannot fill the pipe and deadlock the ACP process.
        self.stderr_tail: deque[str] = deque(maxlen=100)
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="aflow-dsh-acp-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    @classmethod
    def start(
        cls,
        *,
        repo_root: Path,
        executable: str = "dsh",
    ) -> "DshAcpProcess":
        process = subprocess.Popen(
            [executable, "--profile", "acp"],
            cwd=str(repo_root),
            # AFlow owns unattended execution; private sandbox /tmp would hide
            # operator-controlled fixtures and other shared workflow artifacts.
            env={**os.environ, "DSH_PERMISSION_MODE": "danger-full-access"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return cls(process)

    def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return

        try:
            for line in stream:
                self.stderr_tail.append(line.rstrip("\n"))
        except OSError:
            pass

    def _drain_stdout(self) -> None:
        try:
            if self.process.stdout is not None:
                for line in self.process.stdout:
                    self._lines.put(line)
        finally:
            self._lines.put(None)

    def _send(self, payload: Mapping[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("DSH ACP stdin is unavailable")

        self.process.stdin.write(
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )
        self.process.stdin.flush()

    def _answer_server_request(
        self,
        event: Mapping[str, Any],
    ) -> None:
        request_id = event.get("id")
        method = event.get("method")

        if method != "session/request_permission":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "unsupported ACP client method",
                    },
                }
            )
            raise RuntimeError(
                f"DSH ACP requested unsupported client method {method!r}"
            )

        params = event.get("params")
        options = params.get("options") if isinstance(params, Mapping) else None

        selected: str | None = None

        if isinstance(options, list):
            for option in options:
                if not isinstance(option, Mapping):
                    continue

                if option.get("kind") not in {
                    "allow_once",
                    "allow_always",
                }:
                    continue

                option_id = option.get("optionId")
                if isinstance(option_id, str) and option_id:
                    selected = option_id
                    break

        if selected is None:
            outcome: Mapping[str, Any] = {
                "outcome": "cancelled",
            }
        else:
            outcome = {
                "outcome": "selected",
                "optionId": selected,
            }

        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "outcome": outcome,
                },
            }
        )

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("DSH ACP stdout is unavailable")

        request_id = self._next_id
        self._next_id += 1

        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )

        deadline = time.monotonic() + timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"DSH ACP {method} response timed out")

            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"DSH ACP {method} response timed out") from None
            if not line:
                raise RuntimeError(f"DSH ACP exited while waiting for {method}")

            self.raw_lines.append(line)

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("DSH ACP emitted invalid JSON") from exc

            if not isinstance(event, Mapping) or event.get("jsonrpc") != "2.0":
                raise ValueError("DSH ACP emitted a non-JSON-RPC message")

            self.received.append(event)

            # DSH can ask the ACP client for tool escalation permission while
            # our session/prompt request is still outstanding.
            if "method" in event and "id" in event:
                self._answer_server_request(event)
                continue

            # session/update and other notifications.
            if "method" in event:
                continue

            if event.get("id") != request_id:
                raise ValueError("DSH ACP response id does not match request")

            error = event.get("error")
            if isinstance(error, Mapping):
                message = error.get("message")
                raise RuntimeError(
                    "DSH ACP request failed"
                    if not isinstance(message, str)
                    else f"DSH ACP request failed: {message}"
                )

            return event

    def initialize(self) -> Mapping[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {},
            },
            timeout_seconds=DSH_CONTROL_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        # DSH treats stdin EOF as a cooperative shutdown path and flushes
        # persistence while quiescing.
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass

        try:
            self.process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)

        self._stderr_thread.join(timeout=1.0)
        self._stdout_thread.join(timeout=1.0)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class DshAcpDriver:
    def __init__(
        self,
        capabilities: SessionCapabilities,
        *,
        executable: str = "dsh",
    ) -> None:
        self.capabilities = capabilities
        self.executable = executable

    @classmethod
    def from_initialize(
        cls,
        payload: Mapping[str, Any],
    ) -> "DshAcpDriver":
        result = _result(payload, stage="initialize")

        protocol_version = result.get("protocolVersion")
        if protocol_version != ACP_PROTOCOL_VERSION:
            raise ValueError(
                f"unsupported DSH ACP protocol version {protocol_version!r}"
            )

        agent_info = result.get("agentInfo")
        if not isinstance(agent_info, Mapping):
            raise ValueError("DSH ACP initialize response has no agentInfo")

        if agent_info.get("name") != "deepseek-harness-acp":
            raise ValueError("ACP endpoint is not DeepSeek Harness")

        agent_capabilities = result.get("agentCapabilities")
        if not isinstance(agent_capabilities, Mapping):
            raise ValueError("DSH ACP initialize response has no agentCapabilities")

        session_capabilities = agent_capabilities.get("sessionCapabilities")
        resume = (
            isinstance(session_capabilities, Mapping)
            and "resume" in session_capabilities
        )

        return cls(
            SessionCapabilities(
                session_identity=True,
                followup_turn=True,
                # DSH allows session/resume followed by per-session model and
                # reasoning config changes.
                resume_with_model=resume,
                mid_turn_steer=False,
                read_only_teardown=False,
                idempotent_turn_start=False,
            )
        )

    def build_invocation(
        self,
        request: SessionRequest,
    ) -> HarnessInvocation:
        return HarnessInvocation(
            label="dsh-acp",
            argv=(
                self.executable,
                "--profile",
                "acp",
            ),
            env={"DSH_PERMISSION_MODE": "danger-full-access"},
            prompt_mode="owned-session",
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            effective_prompt="",
        )

    def execute_session(
        self,
        request: SessionRequest,
        invocation: HarnessInvocation,
        control_callback: Any | None = None,
    ) -> SessionExecutionResult:
        process = DshAcpProcess.start(
            repo_root=request.repo_root,
            executable=invocation.argv[0],
        )

        session_id: str | None = None
        session_closed = False

        try:
            initialize = process.initialize()
            negotiated = DshAcpDriver.from_initialize(initialize)
            self.capabilities = negotiated.capabilities

            if request.session_id is None:
                opened = process.request(
                    "session/new",
                    {
                        "cwd": str(request.repo_root),
                        "mcpServers": [],
                    },
                    timeout_seconds=DSH_CONTROL_TIMEOUT_SECONDS,
                )

                opened_result = _result(
                    opened,
                    stage="session/new",
                )

                returned_session_id = opened_result.get("sessionId")

                if not isinstance(returned_session_id, str) or not returned_session_id:
                    raise ValueError("DSH ACP session/new returned no session id")

                session_id = returned_session_id
                config_options = _config_options(
                    opened,
                    stage="session/new",
                )

            else:
                if not self.capabilities.resume_with_model:
                    raise RuntimeError("DSH ACP does not advertise session resume")
                session_id = request.session_id

                opened = process.request(
                    "session/resume",
                    {
                        "sessionId": session_id,
                        "cwd": str(request.repo_root),
                        "mcpServers": [],
                    },
                    timeout_seconds=DSH_CONTROL_TIMEOUT_SECONDS,
                )

                config_options = _config_options(
                    opened,
                    stage="session/resume",
                )

            desired_model_value: str | None = None

            # Model must be applied first because DSH's reasoning selector
            # depends on the exact selected model.
            if request.model is not None:
                desired_model_value = _model_option_value(request.model)

                _require_select_option(
                    config_options,
                    option_id="model",
                    desired_value=desired_model_value,
                )

                updated = process.request(
                    "session/set_config_option",
                    {
                        "sessionId": session_id,
                        "configId": "model",
                        "value": desired_model_value,
                    },
                    timeout_seconds=DSH_CONTROL_TIMEOUT_SECONDS,
                )

                config_options = _config_options(
                    updated,
                    stage="session/set_config_option(model)",
                )

                _require_current_value(
                    config_options,
                    option_id="model",
                    desired_value=desired_model_value,
                )

            if request.effort is not None:
                _require_select_option(
                    config_options,
                    option_id="reasoning_effort",
                    desired_value=request.effort,
                )

                updated = process.request(
                    "session/set_config_option",
                    {
                        "sessionId": session_id,
                        "configId": "reasoning_effort",
                        "value": request.effort,
                    },
                    timeout_seconds=DSH_CONTROL_TIMEOUT_SECONDS,
                )

                config_options = _config_options(
                    updated,
                    stage=("session/set_config_option(reasoning_effort)"),
                )

                _require_current_value(
                    config_options,
                    option_id="reasoning_effort",
                    desired_value=request.effort,
                )

                if desired_model_value is not None:
                    _require_current_value(
                        config_options,
                        option_id="model",
                        desired_value=desired_model_value,
                    )

            if control_callback is not None:
                control_callback()

            prompt_event_start = len(process.received)

            prompt_response = process.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [
                        {
                            "type": "text",
                            "text": "\n\n".join(
                                (
                                    request.system_prompt,
                                    request.user_prompt,
                                )
                            ),
                        }
                    ],
                },
                timeout_seconds=DSH_PROMPT_TIMEOUT_SECONDS,
            )

            prompt_result = _result(
                prompt_response,
                stage="session/prompt",
            )

            stop_reason = prompt_result.get("stopReason")
            prompt_events = tuple(process.received[prompt_event_start:])

            final_output = _assistant_text(
                prompt_events,
                session_id=session_id,
            )

            if stop_reason != "end_turn":
                raise RuntimeError(f"DSH ACP prompt stopped with {stop_reason!r}")

            # Explicit close flushes the persisted session. DSH documents
            # closed sessions as resumable, so a later AFlow turn can reopen
            # this exact id.
            process.request(
                "session/close",
                {
                    "sessionId": session_id,
                },
                timeout_seconds=DSH_CONTROL_TIMEOUT_SECONDS,
            )
            session_closed = True

            raw_transport = "".join(process.raw_lines)

            return SessionExecutionResult(
                result=SessionResult(
                    session_id=session_id,
                    selector=request.selector,
                    model=request.model,
                    effort=request.effort,
                    final_output=final_output,
                    structured_events=tuple(process.received),
                    capabilities=self.capabilities,
                ),
                raw_transport=raw_transport,
                events=tuple(process.received),
            )

        finally:
            if session_id is not None and not session_closed:
                try:
                    process.request(
                        "session/close",
                        {
                            "sessionId": session_id,
                        },
                        timeout_seconds=DSH_CONTROL_TIMEOUT_SECONDS,
                    )
                except (
                    OSError,
                    RuntimeError,
                    TimeoutError,
                    ValueError,
                ):
                    pass

            process.close()

    def parse_result(
        self,
        request: SessionRequest,
        stdout: str,
        *,
        returncode: int = 0,
    ) -> SessionResult:
        # DSH final assistant output lives in session/update notifications.
        # execute_session owns the live protocol so it can associate those
        # notifications with the exact prompt interval.
        raise RuntimeError("DSH ACP results require owned execute_session execution")

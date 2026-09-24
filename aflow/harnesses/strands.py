"""Native Strands CLI adapter for fresh, unattended ACP worker turns."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Mapping

from .base import HarnessInvocation
from .session import (
    SessionCapabilities,
    SessionExecutionResult,
    SessionRequest,
    SessionResult,
)


CONTROL_TIMEOUT_SECONDS = 60.0
PROMPT_TIMEOUT_SECONDS = 3600.0


def _provider_env_file(model: str | None) -> Path | None:
    """Select a native CLI env file without putting its secrets in run artifacts."""
    configured = os.environ.get("AFLOW_STRANDS_ENV_FILE")
    if configured:
        path = Path(configured).expanduser()
    elif model is not None and model.startswith("litellm/"):
        # This account's DeepSeek credentials are managed by Reasonix. Strands
        # reads compatible LITELLM_* entries from the same private file.
        path = Path.home() / ".reasonix" / ".env"
    else:
        return None
    return path if path.is_file() else None


def _argv(model: str | None, effort: str | None, *, acp: bool) -> tuple[str, ...]:
    argv = ["strands"]
    if model is not None:
        argv.extend(("--model", model))
    if effort is not None:
        argv.extend(("--effort", effort))
    argv.extend(("--skills", str(Path.home() / ".config" / "aflow" / "skills")))
    env_file = _provider_env_file(model)
    if env_file is not None:
        argv.extend(("--env-file", str(env_file)))
    # One AFlow turn owns one process. Do not depend on user session persistence
    # or interactive permission prompts for unattended work.
    argv.extend(("--session", "off", "--memory", "off", "--set", "interventions=null"))
    argv.append("--acp-server" if acp else "--print")
    return tuple(argv)


def _assistant_text(events: tuple[Mapping[str, Any], ...], session_id: str) -> str:
    parts: list[str] = []
    for event in events:
        if event.get("method") != "session/update":
            continue
        params = event.get("params")
        if not isinstance(params, Mapping) or params.get("sessionId") != session_id:
            continue
        update = params.get("update")
        if not isinstance(update, Mapping) or update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content")
        if isinstance(content, Mapping) and content.get("type") == "text":
            value = content.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


class StrandsAdapter:
    name = "strands"
    supports_effort = True
    manager_workspace_read = True

    def session_driver(self, initialize_payload: Mapping[str, Any]) -> "StrandsAcpDriver":
        return StrandsAcpDriver.from_initialize(initialize_payload)

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
        return HarnessInvocation(
            label=self.name,
            argv=_argv(model, effort, acp=True),
            env={},
            prompt_mode="stdin",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
            stdin_text=effective_prompt,
            final_output_argv=_argv(model, effort, acp=False),
        )


class StrandsAcpProcess:
    """Correlated JSON-RPC stdio with bounded waits and concurrent pipe drains."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._next_id = 1
        self.received: list[Mapping[str, Any]] = []
        self.raw_lines: list[str] = []
        self.stderr_tail: deque[str] = deque(maxlen=100)
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    @classmethod
    def start(cls, *, repo_root: Path, argv: tuple[str, ...]) -> "StrandsAcpProcess":
        return cls(subprocess.Popen(
            argv,
            cwd=str(repo_root),
            env=os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        ))

    def _drain_stdout(self) -> None:
        try:
            if self.process.stdout is not None:
                for line in self.process.stdout:
                    self._lines.put(line)
        finally:
            self._lines.put(None)

    def _drain_stderr(self) -> None:
        if self.process.stderr is None:
            return
        try:
            for line in self.process.stderr:
                self.stderr_tail.append(line.rstrip("\n"))
        except OSError:
            pass

    def _send(self, payload: Mapping[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Strands ACP stdin is unavailable")
        self.process.stdin.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float,
        control_callback: Any | None = None,
    ) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        deadline = time.monotonic() + timeout_seconds
        while True:
            if control_callback is not None:
                control_callback()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Strands ACP {method} response timed out")
            try:
                line = self._lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError(f"Strands ACP exited while waiting for {method}")
            self.raw_lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("Strands ACP emitted invalid JSON") from exc
            if not isinstance(event, Mapping) or event.get("jsonrpc") != "2.0":
                raise ValueError("Strands ACP emitted a non-JSON-RPC message")
            self.received.append(event)
            if "method" in event and "id" in event:
                self._send({"jsonrpc": "2.0", "id": event["id"], "error": {
                    "code": -32601, "message": "unattended client request unsupported"}})
                raise RuntimeError("Strands ACP requested an unsupported client action")
            if "method" in event:
                continue
            if event.get("id") != request_id:
                raise ValueError("Strands ACP response id does not match request")
            if "error" in event:
                raise RuntimeError(f"Strands ACP {method} request failed: {event['error']}")
            if not isinstance(event.get("result"), Mapping):
                raise ValueError(f"Strands ACP {method} response has no result object")
            return event

    def initialize(self) -> Mapping[str, Any]:
        return self.request(
            "initialize", {"protocolVersion": 1, "clientCapabilities": {}},
            timeout_seconds=CONTROL_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class StrandsAcpDriver:
    # Strands v1 is intentionally fresh-turn only. Its native server currently
    # advertises loadSession, but AFlow has not verified model-safe resume.
    capabilities = SessionCapabilities()

    def __init__(self, *, executable: str = "strands") -> None:
        self.executable = executable

    @classmethod
    def from_initialize(cls, payload: Mapping[str, Any]) -> "StrandsAcpDriver":
        result = payload.get("result")
        if not isinstance(result, Mapping) or result.get("protocolVersion") != 1:
            raise ValueError("unsupported Strands ACP initialize response")
        info = result.get("agentInfo")
        if not isinstance(info, Mapping) or info.get("name") != "@strands-agents/cli":
            raise ValueError("ACP endpoint is not Strands CLI")
        return cls()

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        if request.session_id is not None:
            raise RuntimeError("Strands session resume is not supported by this adapter")
        return HarnessInvocation(
            label="strands-acp",
            argv=(self.executable, *_argv(request.model, request.effort, acp=True)[1:]),
            env={},
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
        process = StrandsAcpProcess.start(repo_root=request.repo_root, argv=invocation.argv)
        try:
            self.from_initialize(process.initialize())
            opened = process.request(
                "session/new", {"cwd": str(request.repo_root), "mcpServers": []},
                timeout_seconds=CONTROL_TIMEOUT_SECONDS, control_callback=control_callback,
            )
            session_id = opened["result"].get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("Strands ACP session/new returned no session id")
            prompt = "\n\n".join((request.system_prompt, request.user_prompt))
            response = process.request(
                "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                timeout_seconds=PROMPT_TIMEOUT_SECONDS, control_callback=control_callback,
            )
            stop_reason = response["result"].get("stopReason")
            if stop_reason != "end_turn":
                raise RuntimeError(f"Strands ACP prompt stopped with {stop_reason!r}")
            events = tuple(process.received)
            output = _assistant_text(events, session_id)
            if not output.strip():
                raise ValueError("Strands ACP prompt returned no assistant text")
            return SessionExecutionResult(
                result=SessionResult(
                    session_id=session_id,
                    selector=request.selector,
                    model=request.model,
                    effort=request.effort,
                    final_output=output,
                    structured_events=events,
                    capabilities=self.capabilities,
                ),
                raw_transport="".join(process.raw_lines),
                events=events,
            )
        finally:
            process.close()

    def parse_result(
        self, request: SessionRequest, stdout: str, *, returncode: int = 0,
    ) -> SessionResult:
        raise RuntimeError("Strands ACP results require owned execute_session execution")

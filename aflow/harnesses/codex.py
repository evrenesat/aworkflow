from __future__ import annotations

from pathlib import Path

from .base import HarnessInvocation
from .session import (
    SessionCapabilities,
    SessionRequest,
    SessionResult,
    parse_structured_result,
)


def probe_codex_capabilities(exec_help: str, resume_help: str) -> SessionCapabilities:
    """Gate native resume on the current binary's explicit help surface."""
    has_json = "--json" in exec_help
    has_explicit_resume = "resume" in exec_help and "[SESSION_ID]" in resume_help
    has_resume_model = "-m, --model" in resume_help or "--model" in resume_help
    return SessionCapabilities(
        session_identity=has_json,
        followup_turn=has_json and has_explicit_resume,
        resume_with_model=has_json and has_explicit_resume and has_resume_model,
        idempotent_turn_start=False,
    )


class CodexAdapter:
    name = "codex"
    supports_effort = True
    manager_workspace_read = True

    def session_driver(
        self, *, exec_help: str, resume_help: str
    ) -> "CodexSessionDriver":
        return CodexSessionDriver(probe_codex_capabilities(exec_help, resume_help))

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
        argv: list[str] = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(repo_root),
        ]
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["-c", f'model_reasoning_effort=\'{effort}\''])
        argv.append("-")
        return HarnessInvocation(
            label=self.name,
            argv=tuple(argv),
            env={},
            prompt_mode="stdin",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
            stdin_text=effective_prompt,
        )


class CodexSessionDriver:
    def __init__(self, capabilities: SessionCapabilities) -> None:
        self.capabilities = capabilities

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        if not self.capabilities.session_identity:
            raise RuntimeError("Codex structured session output is unavailable")
        effective_prompt = "\n\n".join((request.system_prompt, request.user_prompt))
        if request.session_id is None:
            argv: list[str] = [
                "codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
                "-C", str(request.repo_root), "--json",
            ]
        else:
            if not self.capabilities.resume_with_model:
                raise RuntimeError("Codex explicit session resume is unavailable")
            argv = [
                "codex", "exec", "resume", request.session_id,
                "--dangerously-bypass-approvals-and-sandbox", "--json",
            ]
        if request.model is not None:
            argv.extend(["--model", request.model])
        if request.effort is not None:
            argv.extend(["-c", f"model_reasoning_effort='{request.effort}'"])
        argv.append("-")
        return HarnessInvocation(
            label="codex-session",
            argv=tuple(argv),
            env={},
            prompt_mode="stdin",
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            effective_prompt=effective_prompt,
            stdin_text=effective_prompt,
        )

    def parse_result(
        self, request: SessionRequest, stdout: str, *, returncode: int = 0
    ) -> SessionResult:
        return parse_structured_result(
            request, stdout, selector=request.selector,
            capabilities=self.capabilities, returncode=returncode,
        )

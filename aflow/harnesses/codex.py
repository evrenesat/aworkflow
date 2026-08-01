from __future__ import annotations

from pathlib import Path

from .base import HarnessInvocation


class CodexAdapter:
    name = "codex"
    supports_effort = True

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
        argv.append(effective_prompt)
        return HarnessInvocation(
            label=self.name,
            argv=tuple(argv),
            env={},
            prompt_mode="stdin",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
        )

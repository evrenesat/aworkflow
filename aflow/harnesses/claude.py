from __future__ import annotations

from pathlib import Path

from .base import HarnessInvocation


class ClaudeAdapter:
    name = "claude"
    supports_effort = True
    manager_workspace_read = True

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ) -> HarnessInvocation:
        argv: list[str] = [
            "claude",
            "-p",
        ]
        if system_prompt:
            argv.extend(["--system-prompt", system_prompt])
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        argv.extend([
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--tools=default",
        ])
        argv.append(user_prompt)
        return HarnessInvocation(
            label=self.name,
            argv=tuple(argv),
            env={},
            prompt_mode="system-prompt-flag",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=user_prompt,
        )

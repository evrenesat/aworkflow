from __future__ import annotations

from pathlib import Path

from .base import HarnessInvocation


class OpencodeAdapter:
    name = "opencode"
    supports_effort = False
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
        effective_prompt = "\n\n".join((system_prompt, user_prompt))
        return HarnessInvocation(
            label=self.name,
            argv=tuple(
                [
                    "opencode",
                    "run",
                    *([] if model is None else ["--model", model]),
                    "--format",
                    "default",
                    "--dir",
                    str(repo_root),
                    effective_prompt,
                ]
            ),
            env={},
            prompt_mode="prefix-system-into-user-prompt",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
        )

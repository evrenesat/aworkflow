from __future__ import annotations

from pathlib import Path

from .base import HarnessInvocation


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
            "reasonix",
            "run",
            "-dir",
            str(repo_root),
        ]
        if model is not None:
            argv.extend(["--model", model])
        argv.append(effective_prompt)
        return HarnessInvocation(
            label=self.name,
            argv=tuple(argv),
            env={},
            prompt_mode="prefix-system-into-user-prompt",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
        )

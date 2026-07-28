from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class HarnessInvocation:
    label: str
    argv: tuple[str, ...]
    env: dict[str, str]
    prompt_mode: str
    system_prompt: str
    user_prompt: str
    effective_prompt: str
    final_output_argv: tuple[str, ...] | None = None

    def for_final_output(self) -> HarnessInvocation:
        if self.final_output_argv is None:
            return self
        return replace(self, argv=self.final_output_argv)


class HarnessAdapter(Protocol):
    name: str
    supports_effort: bool

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ) -> HarnessInvocation:
        ...

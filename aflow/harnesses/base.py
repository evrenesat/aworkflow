from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from ..stop_marker import (
    AGENT_OUTPUT_CONTRACT,
    FINAL_TEXT_OUTPUT_SOURCE,
    OutputContract,
    SemanticOutputSource,
)


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
    stdin_text: str | None = None
    # Built-in adapters return the assistant's final response on stdout.  A
    # plain command adapter can opt into the legacy stdout/stderr result
    # contract explicitly.
    output_contract: OutputContract = AGENT_OUTPUT_CONTRACT
    # The workflow changes structured-session invocations to final_text after
    # accepting and normalizing their assistant result.
    semantic_output_source: SemanticOutputSource = FINAL_TEXT_OUTPUT_SOURCE

    def for_final_output(self) -> HarnessInvocation:
        if self.final_output_argv is None:
            return self
        return replace(self, argv=self.final_output_argv)


class HarnessAdapter(Protocol):
    name: str
    supports_effort: bool
    # True only for adapters whose noninteractive coding mode can read
    # repository files (reference-only manager contexts depend on this).
    # The default is fail-closed false: absent attributes never advertise.
    manager_workspace_read: bool

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


def adapter_manager_workspace_read(adapter: object) -> bool:
    """Fail-closed capability probe for manager workspace reads."""
    return bool(getattr(adapter, "manager_workspace_read", False))

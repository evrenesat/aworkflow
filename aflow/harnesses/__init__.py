from __future__ import annotations

from .base import HarnessAdapter, HarnessInvocation as HarnessInvocation
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .gemini import GeminiAdapter
from .kiro import KiroAdapter
from .opencode import OpencodeAdapter
from .pi import PiAdapter
from .reasonix import ReasonixAdapter


ADAPTERS: dict[str, HarnessAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "copilot": CopilotAdapter(),
    "gemini": GeminiAdapter(),
    "kiro": KiroAdapter(),
    "opencode": OpencodeAdapter(),
    "pi": PiAdapter(),
    "reasonix": ReasonixAdapter(),
}

__all__ = ["ADAPTERS", "HarnessAdapter", "HarnessInvocation", "get_adapter"]


def get_adapter(name: str) -> HarnessAdapter:
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise KeyError(f"unsupported harness '{name}'") from exc

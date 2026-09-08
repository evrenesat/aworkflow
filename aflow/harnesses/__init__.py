from __future__ import annotations

from .base import HarnessAdapter, HarnessInvocation as HarnessInvocation
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .dsh import DshAdapter
from .gemini import GeminiAdapter
from .kiro import KiroAdapter
from .muse import MuseAdapter
from .opencode import OpencodeAdapter
from .pi import PiAdapter
from .reasonix import ReasonixAdapter
from .zcode import ZcodeAdapter


ADAPTERS: dict[str, HarnessAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "copilot": CopilotAdapter(),
    "dsh": DshAdapter(),
    "gemini": GeminiAdapter(),
    "kiro": KiroAdapter(),
    "muse": MuseAdapter(),
    "opencode": OpencodeAdapter(),
    "pi": PiAdapter(),
    "reasonix": ReasonixAdapter(),
    "zcode": ZcodeAdapter(),
}

__all__ = ["ADAPTERS", "HarnessAdapter", "HarnessInvocation", "get_adapter"]


def get_adapter(name: str) -> HarnessAdapter:
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise KeyError(f"unsupported harness '{name}'") from exc

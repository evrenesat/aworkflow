"""Shared parsing for an explicit workflow-agent stop request."""

from __future__ import annotations

from collections.abc import Iterator

from .plan import FENCE_RE


STOP_SENTINEL_PREFIX = "AFLOW_STOP:"
STOP_SENTINEL_FALLBACK_REASON = "implementer requested stop without a reason"
STOP_SENTINEL_PLACEHOLDER_REASON = "<reason>"


def iter_non_fenced_lines(text: str) -> Iterator[str]:
    """Yield lines outside Markdown fences, preserving the runtime's stop semantics."""
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    for line in text.splitlines():
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
                fence_char = None
                fence_len = 0
            continue
        if not in_fence:
            yield line


def extract_stop_markers(text: str) -> list[str]:
    """Return real stop reasons, ignoring fenced examples and ``<reason>`` placeholders."""
    messages: list[str] = []
    for line in iter_non_fenced_lines(text):
        if not line.startswith(STOP_SENTINEL_PREFIX):
            continue
        reason = line[len(STOP_SENTINEL_PREFIX):].strip()
        if reason == STOP_SENTINEL_PLACEHOLDER_REASON:
            continue
        messages.append(reason or STOP_SENTINEL_FALLBACK_REASON)
    return messages


def detect_stop_marker(stdout: str, stderr: str) -> str | None:
    """Return the first stop reason, preserving stdout-before-stderr priority."""
    for text in (stdout, stderr):
        messages = extract_stop_markers(text)
        if messages:
            return messages[0]
    return None

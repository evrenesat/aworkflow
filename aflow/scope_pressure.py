"""Parse AFLOW_SCOPE_PRESSURE signals with the same fence/placeholder and
stop-marker precedence as AFLOW_STOP.

Scope pressure is a nonterminal signal: it forces Full evaluation but never
mandates a split or stop.  AFLOW_STOP remains terminal and defeats a
simultaneous scope-pressure marker.
"""

from __future__ import annotations

from dataclasses import dataclass

from .stop_marker import iter_non_fenced_lines

SCOPE_PRESSURE_SENTINEL_PREFIX = "AFLOW_SCOPE_PRESSURE:"
SCOPE_PRESSURE_FALLBACK_REASON = "scope pressure without a reason"
SCOPE_PRESSURE_PLACEHOLDER_REASON = "<reason>"


@dataclass(frozen=True)
class ScopePressureResult:
    """Canonical scope-pressure parse carrying the bounded reason."""

    detected: bool
    reason: str | None


def extract_scope_pressure_markers(text: str) -> list[str]:
    """Return real scope-pressure reasons, ignoring fenced examples and ``<reason>`` placeholders."""
    messages: list[str] = []
    for line in iter_non_fenced_lines(text):
        if not line.startswith(SCOPE_PRESSURE_SENTINEL_PREFIX):
            continue
        reason = line[len(SCOPE_PRESSURE_SENTINEL_PREFIX):].strip()
        if reason == SCOPE_PRESSURE_PLACEHOLDER_REASON:
            continue
        messages.append(reason or SCOPE_PRESSURE_FALLBACK_REASON)
    return messages


def parse_scope_pressure(stdout: str, stderr: str) -> ScopePressureResult:
    """Return the canonical scope-pressure result with bounded reason.

    Preserves stdout-before-stderr ordering and ignores fenced examples
    and ``<reason>`` placeholders.
    """
    for text in (stdout, stderr):
        messages = extract_scope_pressure_markers(text)
        if messages:
            return ScopePressureResult(detected=True, reason=messages[0])
    return ScopePressureResult(detected=False, reason=None)


def detect_scope_pressure(stdout: str, stderr: str) -> str | None:
    """Return the first scope-pressure reason, preserving stdout-before-stderr priority."""
    return parse_scope_pressure(stdout, stderr).reason


def has_scope_pressure(stdout: str, stderr: str) -> bool:
    """True when at least one real scope-pressure marker is present."""
    return parse_scope_pressure(stdout, stderr).detected

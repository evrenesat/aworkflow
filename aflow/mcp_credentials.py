"""Shared syntactic detector for credential-shaped MCP values.

This module is transport-neutral: the HTTP prevalidation middleware and the
shared MCP tool/resource guard both call :func:`contains_mcp_credential` so the
two guards apply one identical policy. It performs no I/O and has no dependency
on the web server, configuration, or plan services.

The detector is a conservative syntactic safeguard, not a universal secret
scanner. Unmarked alphabetic words are indistinguishable from ordinary prose,
so the policy deliberately rejects only credential-shaped syntax:

1. the existing case-insensitive ``token=`` / ``access_token=`` /
   ``authorization=`` assignment and query pattern, preserved unchanged;
2. an explicit ``Authorization`` label followed by a ``Bearer`` scheme and a
   nonempty value, recognized anywhere (including code fences and prose), with
   optional surrounding quotes on the label and scheme;
3. a whole value (after trimming surrounding whitespace) that consists only of
   a case-insensitive ``Bearer`` scheme, horizontal whitespace, and one
   non-whitespace candidate, regardless of the candidate's length or shape;
4. a ``Bearer`` word inside a longer string whose following candidate, after
   removing surrounding prose/code delimiters and trailing sentence
   punctuation, is token-shaped: it consists of ASCII token characters
   ``[A-Za-z0-9._~+/-]`` with optional terminal ``=`` padding, and contains a
   digit, an internal token marker from ``._~+/-``, or a terminal ``=``.

Ordinary alphabetic words carrying only sentence punctuation or brackets are
accepted. Token length and entropy are never used as criteria.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping

__all__ = ["contains_mcp_credential"]

# Horizontal whitespace: whitespace excluding CR/LF, including ordinary space,
# tab and nonbreaking space.
_HWS = "[ \\t\\x0b\\x0c\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"

# Rule 1: existing assignment/query pattern, preserved unchanged.
_ASSIGNMENT = re.compile(r"(?:token|access_token|authorization)=[^&\s]+", re.IGNORECASE)

# Rule 2: an explicit Authorization header carrying a Bearer scheme and value.
# Accepts an optional quote around the field label and before the scheme so an
# embedded serialized header object cannot bypass the rejection syntax.
_AUTH_HEADER = re.compile(
    r"""["']?\s*authorization["']?\s*[:=]\s*["']?\s*bearer\s+\S""",
    re.IGNORECASE,
)

# Rule 3: the whole (whitespace-trimmed) value is ``Bearer <candidate>``.
_WHOLE_BEARER = re.compile(rf"^bearer{_HWS}+\S+$", re.IGNORECASE)

# Rule 4: a Bearer word inside a longer string with a following candidate.
_BEARER_CANDIDATE = re.compile(rf"\bbearer{_HWS}+([^\s]+)", re.IGNORECASE)

_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "._~+/-")
_MARKER_CHARS = frozenset("._~+/-")
# Surrounding prose/code delimiters and trailing sentence punctuation.
_TRIM_CHARS = "\"'`.,;:!?"
_OPEN_DELIMITERS = "([{"
_CLOSE_DELIMITERS = ")]}"


def _clean_candidate(candidate: str) -> str:
    """Remove surrounding delimiters and trailing punctuation before classifying."""
    value = candidate
    for _ in range(16):
        previous = value
        value = value.strip(_TRIM_CHARS)
        value = value.lstrip(_OPEN_DELIMITERS)
        value = value.rstrip(_CLOSE_DELIMITERS)
        if value == previous:
            break
    return value


def _is_token_shaped(candidate: str) -> bool:
    """Return True when a cleaned candidate reads like a credential token."""
    if not candidate:
        return False
    core = candidate
    padded = core.endswith("=")
    if padded:
        core = core.rstrip("=")
        if not core:
            return False
    if any(ch not in _TOKEN_CHARS for ch in core):
        return False
    return (
        padded
        or any(ch in _MARKER_CHARS for ch in core)
        or any(ch.isdigit() for ch in core)
    )


def _string_contains_credential(value: str) -> bool:
    if _ASSIGNMENT.search(value):
        return True
    if _AUTH_HEADER.search(value):
        return True
    if _WHOLE_BEARER.match(value.strip()):
        return True
    for match in _BEARER_CANDIDATE.finditer(value):
        if _is_token_shaped(_clean_candidate(match.group(1))):
            return True
    return False


def contains_mcp_credential(value: object) -> bool:
    """Return True when a JSON-decoded MCP value contains credential-shaped text.

    Recurses over mapping values and list/tuple/set/frozenset elements; scalars
    other than strings are never treated as credentials.
    """
    if isinstance(value, str):
        return _string_contains_credential(value)
    if isinstance(value, Mapping):
        return any(contains_mcp_credential(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_mcp_credential(item) for item in value)
    return False

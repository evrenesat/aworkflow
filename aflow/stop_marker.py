"""Shared parsing for an explicit workflow-agent stop request."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Final, Literal

from .plan import FENCE_RE


STOP_SENTINEL_PREFIX = "AFLOW_STOP:"
STOP_SENTINEL_FALLBACK_REASON = "implementer requested stop without a reason"
STOP_SENTINEL_PLACEHOLDER_REASON = "<reason>"
OutputContract = Literal["agent", "command"]
AGENT_OUTPUT_CONTRACT: Final[OutputContract] = "agent"
COMMAND_OUTPUT_CONTRACT: Final[OutputContract] = "command"
SemanticOutputSource = Literal["final_text", "structured_transport"]
FINAL_TEXT_OUTPUT_SOURCE: Final[SemanticOutputSource] = "final_text"
STRUCTURED_TRANSPORT_OUTPUT_SOURCE: Final[SemanticOutputSource] = (
    "structured_transport"
)


def resolve_semantic_output_source(
    value: object,
    *,
    artifact_dir: Path | None = None,
) -> SemanticOutputSource:
    """Resolve how a turn's persisted stdout should be interpreted.

    ``stdout.txt`` is normally the final assistant response, including after
    a structured session has been normalized.  Only an explicit structured
    transport record may be reparsed as events.  Missing metadata therefore
    keeps the legacy-safe final-text interpretation; an optional artifact
    lookup supports callers that only retain a turn directory.
    """
    if value in (FINAL_TEXT_OUTPUT_SOURCE, STRUCTURED_TRANSPORT_OUTPUT_SOURCE):
        return value  # type: ignore[return-value]

    if artifact_dir is not None:
        for filename in ("result.json", "argv.json"):
            try:
                payload = json.loads(
                    (Path(artifact_dir) / filename).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                source = payload.get("semantic_output_source")
                if source in (
                    FINAL_TEXT_OUTPUT_SOURCE,
                    STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
                ):
                    return source  # type: ignore[return-value]

    return FINAL_TEXT_OUTPUT_SOURCE


def resolve_output_contract(
    value: object,
    *,
    artifact_dir: Path | None = None,
) -> OutputContract:
    """Resolve a recorded semantic-stream contract with legacy compatibility.

    New turn records persist the contract directly.  Older records can recover
    it from ``argv.json`` when available; otherwise they retain the historical
    command-result behavior where both captured streams are semantic output.
    """
    if value == AGENT_OUTPUT_CONTRACT:
        return AGENT_OUTPUT_CONTRACT
    if value == COMMAND_OUTPUT_CONTRACT:
        return COMMAND_OUTPUT_CONTRACT

    if artifact_dir is not None:
        try:
            argv_payload = json.loads(
                (Path(artifact_dir) / "argv.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            argv_payload = None
        if isinstance(argv_payload, dict):
            value = argv_payload.get("output_contract")
            if value == AGENT_OUTPUT_CONTRACT:
                return AGENT_OUTPUT_CONTRACT
            if value == COMMAND_OUTPUT_CONTRACT:
                return COMMAND_OUTPUT_CONTRACT

    return COMMAND_OUTPUT_CONTRACT


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


def semantic_output_streams(
    stdout: str,
    stderr: str,
    *,
    output_contract: OutputContract = COMMAND_OUTPUT_CONTRACT,
) -> tuple[str, ...]:
    """Select streams that are allowed to carry current semantic output.

    Agent adapters expose the final assistant response on stdout; their stderr
    is diagnostic transport and must not control workflow state.  Command
    invocations retain the historical contract that both captured streams are
    command results.
    """
    if output_contract == AGENT_OUTPUT_CONTRACT:
        return (stdout,)
    if output_contract == COMMAND_OUTPUT_CONTRACT:
        return (stdout, stderr)
    raise ValueError(f"unknown harness output contract: {output_contract!r}")


def extract_trusted_stop_markers(
    stdout: str,
    stderr: str,
    *,
    output_contract: OutputContract = COMMAND_OUTPUT_CONTRACT,
) -> list[str]:
    """Return stop markers from the invocation's trusted semantic streams."""
    messages: list[str] = []
    for text in semantic_output_streams(
        stdout, stderr, output_contract=output_contract
    ):
        messages.extend(extract_stop_markers(text))
    return messages


def detect_stop_marker(
    stdout: str,
    stderr: str,
    *,
    output_contract: OutputContract = COMMAND_OUTPUT_CONTRACT,
) -> str | None:
    """Return the first stop reason from the invocation's trusted streams."""
    for text in semantic_output_streams(
        stdout, stderr, output_contract=output_contract
    ):
        messages = extract_stop_markers(text)
        if messages:
            return messages[0]
    return None

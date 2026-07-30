"""Pure, deterministic primitives for scope-envelope capture, repartition proposal
parsing, candidate rendering, and mechanical validation.

Checkpoint 1 scope: no harness calls, no live-plan mutation, no workflow routing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Collection, Mapping
from uuid import uuid4

from .plan import PlanParseError, parse_plan_text


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
BOLD_LABEL_RE = re.compile(r"^\*\*[^*]+\*\*")
# A source unit starts at every non-indented Markdown list item.  The optional
# checkbox deliberately makes ordinary bullets/ordered lists first-class units
# rather than folding them into surrounding prose.
TOP_LEVEL_LIST_ITEM_RE = re.compile(r"^(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
SECTION_RE = re.compile(r"^###\s+\[([ xX])\]\s+(Checkpoint\b.*)$")
NON_CHECKPOINT_HEADING_RE = re.compile(r"^#{1,3}\s+")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_b64(data: bytes) -> str:
    """RFC 4648 padded base64 encoding of a SHA-256 digest."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _text_index_at_utf8_boundary(text: str, byte_offset: int) -> int:
    """Return the text index for a verified UTF-8 byte boundary.

    All persisted spans remain byte spans.  This conversion is intentionally
    narrow: decoding the prefix both rejects an offset in the middle of a code
    point and avoids treating a byte offset as a Python string index.
    """
    data = text.encode("utf-8")
    if not isinstance(byte_offset, int) or isinstance(byte_offset, bool):
        raise ValueError("UTF-8 byte offset must be an integer")
    if byte_offset < 0 or byte_offset > len(data):
        raise ValueError("UTF-8 byte offset is outside the text")
    try:
        return len(data[:byte_offset].decode("utf-8", "strict"))
    except UnicodeDecodeError as exc:
        raise ValueError("UTF-8 byte offset is not a code-point boundary") from exc


def _is_nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_identifier(value: object, field_name: str) -> str:
    if not _is_nonblank_string(value):
        raise ValueError(f"{field_name} must be a nonblank string")
    result = str(value)
    if any(ch.isspace() for ch in result) or "-->" in result:
        raise ValueError(f"{field_name} contains unsafe whitespace or comment terminator")
    return result


def _decode_canonical_base64(value: object, field_name: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a nonempty base64 string")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} is not strict base64") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field_name} is not canonical padded base64")
    return decoded


def _validate_relative_logical_path(value: object) -> str:
    if not _is_nonblank_string(value):
        raise ValueError("original_plan_path is empty")
    path = str(value)
    logical = PurePosixPath(path)
    if logical.is_absolute() or ".." in logical.parts or path.startswith("/") or "\\" in path:
        raise ValueError("original_plan_path must be repository-relative without parent traversal")
    return path


def _resolve_envelope_original_plan_path(
    value: str | Path,
    *,
    repo_root: Path | None,
) -> str:
    """Normalize an in-repository plan path to envelope logical form.

    Direct library callers may provide an already repository-relative path.
    Runtime callers also pass the absolute plan path and repository root; both
    forms produce one strict logical path while outside and traversing paths
    remain invalid.
    """
    raw = str(value)
    if repo_root is None:
        return _validate_relative_logical_path(raw)
    logical_input = PurePosixPath(raw)
    if "\\" in raw or (not Path(raw).is_absolute() and ".." in logical_input.parts):
        raise ValueError("original_plan_path must be repository-relative without parent traversal")
    root = repo_root.resolve()
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        logical = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("original_plan_path must be inside repository root") from exc
    return _validate_relative_logical_path(logical)


# ---------------------------------------------------------------------------
# 1. Fence-aware checkpoint source-slice helper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckpointSourceSlice:
    """Exact byte/line spans and text for one checkpoint within a plan."""

    checkpoint_index: int
    checkpoint_name: str
    heading_line: int  # 1-based
    heading_prefix: str  # exact heading line + immediately following blank bytes
    heading_prefix_byte_start: int
    heading_prefix_byte_end: int
    body_text: str  # checkpoint body after heading_prefix, without the heading line
    body_byte_start: int
    body_byte_end: int
    checkpoint_byte_start: int  # start of ### heading
    checkpoint_byte_end: int  # exclusive end of checkpoint body
    full_text: str  # heading_prefix + body_text


def slice_checkpoint_source(
    plan_text: str,
    *,
    checkpoint_index: int | None = None,
    checkpoint_name: str | None = None,
) -> CheckpointSourceSlice | None:
    """Return exact checkpoint offsets and text from the full plan.

    At least one of *checkpoint_index* (1-based) or *checkpoint_name* must be
    provided.  Content inside fenced blocks is treated as opaque body text; the
    slice itself is determined from live checkpoint headings only.

    Returns None when no matching checkpoint is found.
    """
    plan_bytes = plan_text.encode("utf-8")
    lines = plan_text.splitlines(keepends=True)

    # Find all checkpoint headings (outside fences)
    checkpoints: list[dict[str, Any]] = []
    in_fence = False
    fence_char: str | None = None
    fence_len = 0

    for line_idx, line in enumerate(lines):
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
        if in_fence:
            continue

        section_match = SECTION_RE.match(line)
        if section_match:
            checkpoints.append({
                "line_number": line_idx + 1,
                "name": section_match.group(2).strip(),
                "heading_checked": section_match.group(1).lower() == "x",
                "line": line,
            })

    if not checkpoints:
        return None

    # Resolve target
    target_idx: int | None = None
    if checkpoint_name is not None:
        for i, cp in enumerate(checkpoints):
            if cp["name"] == checkpoint_name:
                target_idx = i
                break
        if target_idx is None:
            return None
    elif checkpoint_index is not None:
        if checkpoint_index < 1 or checkpoint_index > len(checkpoints):
            return None
        target_idx = checkpoint_index - 1
    else:
        return None

    target = checkpoints[target_idx]

    # Compute byte offset of the heading line
    heading_byte_start = _line_byte_offset(lines, target["line_number"] - 1)

    # Find end of this checkpoint: next checkpoint heading (outside fences), or
    # end of file.
    in_fence = False
    fence_char = None
    fence_len = 0
    end_line_idx = len(lines)  # exclusive

    for line_idx in range(target["line_number"], len(lines)):
        line = lines[line_idx]
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
        if in_fence:
            continue
        if SECTION_RE.match(line):
            end_line_idx = line_idx
            break
        # Non-checkpoint headings (##, #) end the checkpoint scope
        if NON_CHECKPOINT_HEADING_RE.match(line):
            end_line_idx = line_idx
            break

    # Gather bytes
    heading_prefix_lines: list[str] = [lines[target["line_number"] - 1]]  # heading line
    # Collect immediately following blank lines
    body_start_line = target["line_number"]
    while body_start_line < end_line_idx and lines[body_start_line].strip() == "":
        heading_prefix_lines.append(lines[body_start_line])
        body_start_line += 1

    heading_prefix = "".join(heading_prefix_lines)
    body_lines = lines[body_start_line:end_line_idx]
    body_text = "".join(body_lines)

    heading_prefix_byte_start = heading_byte_start
    heading_prefix_byte_end = heading_prefix_byte_start + len(heading_prefix.encode("utf-8"))
    body_byte_start = heading_prefix_byte_end
    body_byte_end = _line_byte_offset(lines, end_line_idx)
    checkpoint_byte_end = body_byte_end

    full_text = heading_prefix + body_text

    return CheckpointSourceSlice(
        checkpoint_index=target_idx + 1,
        checkpoint_name=target["name"],
        heading_line=target["line_number"],
        heading_prefix=heading_prefix,
        heading_prefix_byte_start=heading_prefix_byte_start,
        heading_prefix_byte_end=heading_prefix_byte_end,
        body_text=body_text,
        body_byte_start=body_byte_start,
        body_byte_end=body_byte_end,
        checkpoint_byte_start=heading_byte_start,
        checkpoint_byte_end=checkpoint_byte_end,
        full_text=full_text,
    )


def _line_byte_offset(lines: list[str], line_idx: int) -> int:
    """Byte offset to the start of line at *line_idx* (0-based)."""
    return sum(len(line.encode("utf-8")) for line in lines[:line_idx])


# ---------------------------------------------------------------------------
# 2. Deterministic source-block extraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceBlock:
    """One deterministically extracted block from a checkpoint body."""

    block_id: str
    text: str
    byte_start: int  # absolute within the *full plan* bytes
    byte_end: int    # exclusive
    line_start: int  # 1-based, within the full plan
    line_end: int    # exclusive, within the full plan
    section_label: str | None  # bold section label if discovered, e.g. "**Goal:**"
    content_sha256: str  # hex SHA-256 of block text as UTF-8 bytes


def extract_source_blocks(
    source_slice: CheckpointSourceSlice,
    *,
    envelope_checkpoint_sha256: str,
    plan_text: str,
) -> tuple[SourceBlock, ...]:
    """Deterministically split a checkpoint body into ordered source blocks.

    Algorithm (from plan):
    1. Outside fences, start a unit at a bold section label, a top-level
       list/task item, a fence opener, or the first nonblank line of a
       paragraph/unknown construct.
    2. Keep an item's indented continuation lines and nested/fenced content.
    3. End a unit immediately before the next recognized unit; trailing blank
       bytes stay with the preceding unit.
    4. If no recognized construct applies, consume conservatively through the
       next recognized boundary.
    5. Assert that the concatenation of all block texts equals the body text.
    """
    body_text = source_slice.body_text
    body_bytes = body_text.encode("utf-8")
    body_byte_offset = source_slice.body_byte_start
    body_line_offset = source_slice.heading_line + source_slice.heading_prefix.count("\n")

    lines = body_text.splitlines(keepends=True)
    if not lines:
        return ()

    blocks: list[SourceBlock] = []
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    fence_start_idx: int | None = None

    i = 0
    while i < len(lines):
        line = lines[i]

        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                # Opening a fence
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                fence_start_idx = i
                # Continue scanning until we close this fence
                j = i + 1
                while j < len(lines):
                    close_match = FENCE_RE.match(lines[j])
                    if close_match:
                        close_marker = close_match.group(1)
                        if close_marker[0] == fence_char and len(close_marker) >= fence_len:
                            j += 1  # include closing fence line
                            break
                    j += 1
                # j is now exclusive end of fence block
                # Consume trailing blank lines (they stay with the fence block)
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                block_lines = lines[i:j]
                block_text = "".join(block_lines)
                blocks.append(_make_source_block(
                    block_text, body_byte_offset, body_line_offset,
                    lines, i, j, envelope_checkpoint_sha256, len(blocks),
                ))
                i = j
                in_fence = False
                fence_char = None
                fence_len = 0
                fence_start_idx = None
                continue
            # else: closing fence inside a fence — handled by the open-fence scan above
            i += 1
            continue

        if in_fence:
            i += 1
            continue

        stripped = line.strip()

        # Skip pure blank lines between blocks
        if stripped == "":
            i += 1
            continue

        # Check for a recognized unit start
        is_bold_label = bool(BOLD_LABEL_RE.match(stripped))
        is_list_item = bool(TOP_LEVEL_LIST_ITEM_RE.match(line))
        is_heading = bool(NON_CHECKPOINT_HEADING_RE.match(line))

        if is_bold_label or is_list_item or is_heading:
            # This is a recognized unit start — consume it and its continuations
            j = _consume_unit(lines, i)
            block_lines = lines[i:j]
            block_text = "".join(block_lines)
            blocks.append(_make_source_block(
                block_text, body_byte_offset, body_line_offset,
                lines, i, j, envelope_checkpoint_sha256, len(blocks),
            ))
            i = j
            continue

        # Unknown/paragraph construct: consume through the next recognized boundary
        j = i + 1
        while j < len(lines):
            next_line = lines[j]
            next_stripped = next_line.strip()
            if next_stripped == "":
                # Blank line — include it but check what follows
                k = j + 1
                while k < len(lines) and lines[k].strip() == "":
                    k += 1
                if k < len(lines):
                    look = lines[k]
                    look_stripped = look.strip()
                    fm = FENCE_RE.match(look)
                    if fm or BOLD_LABEL_RE.match(look_stripped) or TOP_LEVEL_LIST_ITEM_RE.match(look) or NON_CHECKPOINT_HEADING_RE.match(look):
                        break
                j = k
                continue
            fm = FENCE_RE.match(next_line)
            if fm or BOLD_LABEL_RE.match(next_stripped) or TOP_LEVEL_LIST_ITEM_RE.match(next_line) or NON_CHECKPOINT_HEADING_RE.match(next_line):
                break
            j += 1

        # Include trailing blanks
        while j < len(lines) and lines[j].strip() == "":
            j += 1

        block_lines = lines[i:j]
        block_text = "".join(block_lines)
        if block_text.strip():
            blocks.append(_make_source_block(
                block_text, body_byte_offset, body_line_offset,
                lines, i, j, envelope_checkpoint_sha256, len(blocks),
            ))
        i = j

    # Verify concatenation
    reconstructed = "".join(b.text for b in blocks)
    if reconstructed != body_text:
        raise ValueError(
            f"Source-block extraction produced coverage gap: "
            f"reconstructed {len(reconstructed)} bytes != body {len(body_bytes)} bytes"
        )

    return tuple(blocks)


def _consume_unit(lines: list[str], start_idx: int) -> int:
    """Consume a recognized unit and its continuations, returning exclusive end index."""
    first_line = lines[start_idx]
    # Determine if this is indented continuation context
    first_stripped = first_line.strip()

    is_bold = bool(BOLD_LABEL_RE.match(first_stripped))
    is_list = bool(TOP_LEVEL_LIST_ITEM_RE.match(first_line))

    j = start_idx + 1
    while j < len(lines):
        line = lines[j]
        stripped = line.strip()

        # Blank line — keep as continuation unless followed by a new unit
        if stripped == "":
            # Peek ahead
            k = j + 1
            while k < len(lines) and lines[k].strip() == "":
                k += 1
            if k < len(lines):
                look = lines[k]
                look_stripped = look.strip()
                fm = FENCE_RE.match(look)
                if fm:
                    break
                if BOLD_LABEL_RE.match(look_stripped):
                    break
                # A top-level list item ends a bold-label unit or another list item
                if TOP_LEVEL_LIST_ITEM_RE.match(look):
                    if is_list:
                        break
                if NON_CHECKPOINT_HEADING_RE.match(look):
                    break
                # For list items: a new list item starts a new unit
                if is_list and TOP_LEVEL_LIST_ITEM_RE.match(look):
                    break
                # For bold labels: continue consuming (content paragraphs belong to this section)
            j = k
            continue

        # Fence opener starts a new unit
        if FENCE_RE.match(line):
            break

        # Bold label starts a new unit
        if BOLD_LABEL_RE.match(stripped):
            break

        # Top-level list item starts a new unit (ends a bold-label unit)
        if TOP_LEVEL_LIST_ITEM_RE.match(line):
            if is_bold:
                break
            if is_list:
                break

        # Non-checkpoint heading ends a unit
        if NON_CHECKPOINT_HEADING_RE.match(line):
            break

        j += 1

    # Include trailing blanks
    while j < len(lines) and lines[j].strip() == "":
        j += 1

    return j


def _make_source_block(
    text: str,
    body_byte_offset: int,
    body_line_offset: int,
    all_body_lines: list[str],
    start_idx: int,
    end_idx: int,
    envelope_checkpoint_sha256: str,
    ordinal: int,
) -> SourceBlock:
    """Build a SourceBlock with correct absolute positions."""
    preceding_text = "".join(all_body_lines[:start_idx])
    byte_start = body_byte_offset + len(preceding_text.encode("utf-8"))
    byte_end = byte_start + len(text.encode("utf-8"))

    line_start = body_line_offset + start_idx
    line_end = body_line_offset + end_idx

    # Discover section label from bold text at start
    section_label: str | None = None
    first_line = all_body_lines[start_idx].strip() if start_idx < len(all_body_lines) else ""
    bold_match = BOLD_LABEL_RE.match(first_line)
    if bold_match:
        section_label = bold_match.group(0)

    block_id = f"{envelope_checkpoint_sha256[:16]}-b{ordinal:03d}"

    return SourceBlock(
        block_id=block_id,
        text=text,
        byte_start=byte_start,
        byte_end=byte_end,
        line_start=line_start,
        line_end=line_end,
        section_label=section_label,
        content_sha256=_sha256_hex(text.encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# 3. Scope envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScopeEnvelopeV1:
    """Immutable authoritative capture of a checkpoint scope at opening time."""

    schema_version: int = 1
    scope_id: str = ""
    scope_digest: str = ""  # SHA-256(scope_id UTF-8)
    original_plan_path: str = ""  # repository-relative
    plan_sha256: str = ""
    plan_bytes_b64: str = ""
    plan_text: str = ""
    checkpoint_index: int = 0
    checkpoint_name: str = ""
    checkpoint_line_start: int = 0
    checkpoint_line_end: int = 0
    checkpoint_byte_start: int = 0
    checkpoint_byte_end: int = 0
    checkpoint_sha256: str = ""
    checkpoint_bytes_b64: str = ""
    checkpoint_text: str = ""
    heading_prefix: str = ""
    source_blocks: tuple[SourceBlock, ...] = ()
    canonical_envelope_sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict, excluding canonical_envelope_sha256."""
        return {
            "schema_version": self.schema_version,
            "scope_id": self.scope_id,
            "scope_digest": self.scope_digest,
            "original_plan_path": self.original_plan_path,
            "plan_sha256": self.plan_sha256,
            "plan_bytes_b64": self.plan_bytes_b64,
            "plan_text": self.plan_text,
            "checkpoint_index": self.checkpoint_index,
            "checkpoint_name": self.checkpoint_name,
            "checkpoint_line_start": self.checkpoint_line_start,
            "checkpoint_line_end": self.checkpoint_line_end,
            "checkpoint_byte_start": self.checkpoint_byte_start,
            "checkpoint_byte_end": self.checkpoint_byte_end,
            "checkpoint_sha256": self.checkpoint_sha256,
            "checkpoint_bytes_b64": self.checkpoint_bytes_b64,
            "checkpoint_text": self.checkpoint_text,
            "heading_prefix": self.heading_prefix,
            "source_blocks": [_source_block_to_dict(b) for b in self.source_blocks],
        }

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> ScopeEnvelopeV1:
        """Reconstruct an envelope without coercing untrusted JSON values."""
        if not isinstance(data, Mapping):
            raise ValueError("Envelope JSON must be an object")
        expected_keys = {
            "schema_version", "scope_id", "scope_digest", "original_plan_path",
            "plan_sha256", "plan_bytes_b64", "plan_text", "checkpoint_index",
            "checkpoint_name", "checkpoint_line_start", "checkpoint_line_end",
            "checkpoint_byte_start", "checkpoint_byte_end", "checkpoint_sha256",
            "checkpoint_bytes_b64", "checkpoint_text", "heading_prefix",
            "source_blocks", "canonical_envelope_sha256",
        }
        actual_keys = set(data)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            unknown = sorted(actual_keys - expected_keys)
            raise ValueError(f"Envelope fields do not match schema (missing={missing}, unknown={unknown})")

        def required_string(key: str) -> str:
            value = data[key]
            if not isinstance(value, str):
                raise ValueError(f"Envelope field '{key}' must be a string")
            return value

        def required_int(key: str) -> int:
            value = data[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"Envelope field '{key}' must be an integer")
            return value

        raw_blocks = data["source_blocks"]
        if not isinstance(raw_blocks, list):
            raise ValueError("Envelope field 'source_blocks' must be a list")
        blocks: list[SourceBlock] = []
        block_keys = {
            "block_id", "text", "byte_start", "byte_end", "line_start",
            "line_end", "section_label", "content_sha256",
        }
        for index, raw_block in enumerate(raw_blocks):
            if not isinstance(raw_block, Mapping) or set(raw_block) != block_keys:
                raise ValueError(f"Envelope source_blocks[{index}] does not match schema")
            section_label = raw_block["section_label"]
            if section_label is not None and not isinstance(section_label, str):
                raise ValueError(f"Envelope source_blocks[{index}].section_label must be string or null")
            ints: list[int] = []
            for key in ("byte_start", "byte_end", "line_start", "line_end"):
                value = raw_block[key]
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"Envelope source_blocks[{index}].{key} must be an integer")
                ints.append(value)
            strings: list[str] = []
            for key in ("block_id", "text", "content_sha256"):
                value = raw_block[key]
                if not isinstance(value, str):
                    raise ValueError(f"Envelope source_blocks[{index}].{key} must be a string")
                strings.append(value)
            blocks.append(SourceBlock(
                block_id=strings[0], text=strings[1], byte_start=ints[0], byte_end=ints[1],
                line_start=ints[2], line_end=ints[3], section_label=section_label,
                content_sha256=strings[2],
            ))

        return ScopeEnvelopeV1(
            schema_version=required_int("schema_version"),
            scope_id=required_string("scope_id"),
            scope_digest=required_string("scope_digest"),
            original_plan_path=required_string("original_plan_path"),
            plan_sha256=required_string("plan_sha256"),
            plan_bytes_b64=required_string("plan_bytes_b64"),
            plan_text=required_string("plan_text"),
            checkpoint_index=required_int("checkpoint_index"),
            checkpoint_name=required_string("checkpoint_name"),
            checkpoint_line_start=required_int("checkpoint_line_start"),
            checkpoint_line_end=required_int("checkpoint_line_end"),
            checkpoint_byte_start=required_int("checkpoint_byte_start"),
            checkpoint_byte_end=required_int("checkpoint_byte_end"),
            checkpoint_sha256=required_string("checkpoint_sha256"),
            checkpoint_bytes_b64=required_string("checkpoint_bytes_b64"),
            checkpoint_text=required_string("checkpoint_text"),
            heading_prefix=required_string("heading_prefix"),
            source_blocks=tuple(blocks),
            canonical_envelope_sha256=required_string("canonical_envelope_sha256"),
        )


def _source_block_to_dict(block: SourceBlock) -> dict[str, object]:
    return {
        "block_id": block.block_id,
        "text": block.text,
        "byte_start": block.byte_start,
        "byte_end": block.byte_end,
        "line_start": block.line_start,
        "line_end": block.line_end,
        "section_label": block.section_label,
        "content_sha256": block.content_sha256,
    }


def create_envelope(
    *,
    scope_id: str,
    original_plan_path: str | Path,
    plan_text: str,
    checkpoint_index: int,
    repo_root: Path | None = None,
) -> ScopeEnvelopeV1:
    """Build a ScopeEnvelopeV1 from the original plan at scope opening.

    This is the single factory function for creating envelopes.  It:
    1. Hashes the full plan
    2. Slices the target checkpoint
    3. Extracts deterministic source blocks
    4. Computes the canonical envelope hash
    """
    resolved_original_plan_path = _resolve_envelope_original_plan_path(
        original_plan_path,
        repo_root=repo_root,
    )
    plan_bytes = plan_text.encode("utf-8")
    plan_sha256 = _sha256_hex(plan_bytes)
    plan_bytes_b64 = base64.b64encode(plan_bytes).decode("ascii")

    scope_digest = _sha256_hex(scope_id.encode("utf-8"))

    source_slice = slice_checkpoint_source(plan_text, checkpoint_index=checkpoint_index)
    if source_slice is None:
        raise ValueError(
            f"Checkpoint index {checkpoint_index} not found in plan text"
        )

    checkpoint_bytes = source_slice.full_text.encode("utf-8")
    checkpoint_sha256 = _sha256_hex(checkpoint_bytes)
    checkpoint_bytes_b64 = base64.b64encode(checkpoint_bytes).decode("ascii")

    source_blocks = extract_source_blocks(
        source_slice,
        envelope_checkpoint_sha256=checkpoint_sha256,
        plan_text=plan_text,
    )

    checkpoint_line_end = _compute_checkpoint_line_end(plan_text, source_slice)

    envelope = ScopeEnvelopeV1(
        schema_version=1,
        scope_id=scope_id,
        scope_digest=scope_digest,
        original_plan_path=resolved_original_plan_path,
        plan_sha256=plan_sha256,
        plan_bytes_b64=plan_bytes_b64,
        plan_text=plan_text,
        checkpoint_index=source_slice.checkpoint_index,
        checkpoint_name=source_slice.checkpoint_name,
        checkpoint_line_start=source_slice.heading_line,
        checkpoint_line_end=checkpoint_line_end,
        checkpoint_byte_start=source_slice.checkpoint_byte_start,
        checkpoint_byte_end=source_slice.checkpoint_byte_end,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_bytes_b64=checkpoint_bytes_b64,
        checkpoint_text=source_slice.full_text,
        heading_prefix=source_slice.heading_prefix,
        source_blocks=source_blocks,
        canonical_envelope_sha256="",
    )

    # Compute canonical hash
    canonical = _compute_canonical_envelope_hash(envelope)
    completed = ScopeEnvelopeV1(
        schema_version=1,
        scope_id=scope_id,
        scope_digest=scope_digest,
        original_plan_path=resolved_original_plan_path,
        plan_sha256=plan_sha256,
        plan_bytes_b64=plan_bytes_b64,
        plan_text=plan_text,
        checkpoint_index=source_slice.checkpoint_index,
        checkpoint_name=source_slice.checkpoint_name,
        checkpoint_line_start=source_slice.heading_line,
        checkpoint_line_end=checkpoint_line_end,
        checkpoint_byte_start=source_slice.checkpoint_byte_start,
        checkpoint_byte_end=source_slice.checkpoint_byte_end,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_bytes_b64=checkpoint_bytes_b64,
        checkpoint_text=source_slice.full_text,
        heading_prefix=source_slice.heading_prefix,
        source_blocks=source_blocks,
        canonical_envelope_sha256=canonical,
    )
    issues = validate_envelope(completed)
    if issues:
        raise ValueError(f"Refusing to create invalid scope envelope: {'; '.join(issues)}")
    return completed


def _compute_checkpoint_line_end(plan_text: str, source_slice: CheckpointSourceSlice) -> int:
    """Compute 1-based exclusive line end of the checkpoint within the plan."""
    plan_bytes = plan_text.encode("utf-8")
    preceding = plan_bytes[:source_slice.checkpoint_byte_end].decode("utf-8", "strict")
    return preceding.count("\n") + 1


def _envelope_canonical_dict(envelope: ScopeEnvelopeV1) -> dict[str, object]:
    """Build the canonical dict for hashing (excludes canonical_envelope_sha256)."""
    return {
        "schema_version": envelope.schema_version,
        "scope_id": envelope.scope_id,
        "scope_digest": envelope.scope_digest,
        "original_plan_path": envelope.original_plan_path,
        "plan_sha256": envelope.plan_sha256,
        "plan_bytes_b64": envelope.plan_bytes_b64,
        "plan_text": envelope.plan_text,
        "checkpoint_index": envelope.checkpoint_index,
        "checkpoint_name": envelope.checkpoint_name,
        "checkpoint_line_start": envelope.checkpoint_line_start,
        "checkpoint_line_end": envelope.checkpoint_line_end,
        "checkpoint_byte_start": envelope.checkpoint_byte_start,
        "checkpoint_byte_end": envelope.checkpoint_byte_end,
        "checkpoint_sha256": envelope.checkpoint_sha256,
        "checkpoint_bytes_b64": envelope.checkpoint_bytes_b64,
        "checkpoint_text": envelope.checkpoint_text,
        "heading_prefix": envelope.heading_prefix,
        "source_blocks": [_source_block_to_dict(b) for b in envelope.source_blocks],
    }


def _compute_canonical_envelope_hash(envelope: ScopeEnvelopeV1) -> str:
    """SHA-256 of UTF-8 JSON with sorted keys, compact separators, no trailing newline.

    Excludes the canonical_envelope_sha256 field itself.
    """
    d = _envelope_canonical_dict(envelope)
    json_bytes = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _sha256_hex(json_bytes)


def validate_envelope(envelope: ScopeEnvelopeV1) -> list[str]:
    """Validate every persisted envelope field without normalizing authority."""
    issues: list[str] = []

    if not isinstance(envelope.schema_version, int) or isinstance(envelope.schema_version, bool) or envelope.schema_version != 1:
        issues.append("schema_version must be integer 1")
    if not _is_nonblank_string(envelope.scope_id):
        issues.append("scope_id is empty")
    if not _is_valid_sha256_hex(envelope.scope_digest):
        issues.append("scope_digest is not a lowercase SHA-256 hex digest")
    elif _is_nonblank_string(envelope.scope_id) and envelope.scope_digest != _sha256_hex(envelope.scope_id.encode("utf-8")):
        issues.append("scope_digest does not match scope_id")
    try:
        _validate_relative_logical_path(envelope.original_plan_path)
    except ValueError as exc:
        issues.append(str(exc))

    for field_name in ("plan_sha256", "checkpoint_sha256", "canonical_envelope_sha256"):
        if not _is_valid_sha256_hex(getattr(envelope, field_name)):
            issues.append(f"{field_name} is not a lowercase SHA-256 hex digest")

    if not isinstance(envelope.plan_text, str) or not isinstance(envelope.checkpoint_text, str) or not isinstance(envelope.heading_prefix, str):
        issues.append("plan, checkpoint, and heading text fields must be strings")
        return issues
    plan_bytes = envelope.plan_text.encode("utf-8")
    checkpoint_bytes = envelope.checkpoint_text.encode("utf-8")
    try:
        decoded_plan = _decode_canonical_base64(envelope.plan_bytes_b64, "plan_bytes_b64")
        if decoded_plan != plan_bytes or decoded_plan.decode("utf-8", "strict") != envelope.plan_text:
            issues.append("plan_bytes_b64 does not exactly round-trip to plan_text")
    except (ValueError, UnicodeDecodeError) as exc:
        issues.append(str(exc))
    try:
        decoded_checkpoint = _decode_canonical_base64(envelope.checkpoint_bytes_b64, "checkpoint_bytes_b64")
        if decoded_checkpoint != checkpoint_bytes or decoded_checkpoint.decode("utf-8", "strict") != envelope.checkpoint_text:
            issues.append("checkpoint_bytes_b64 does not exactly round-trip to checkpoint_text")
    except (ValueError, UnicodeDecodeError) as exc:
        issues.append(str(exc))
    if envelope.plan_sha256 != _sha256_hex(plan_bytes):
        issues.append("plan_sha256 does not match plan_text bytes")
    if envelope.checkpoint_sha256 != _sha256_hex(checkpoint_bytes):
        issues.append("checkpoint_sha256 does not match checkpoint_text bytes")

    integer_fields = (
        "checkpoint_index", "checkpoint_line_start", "checkpoint_line_end",
        "checkpoint_byte_start", "checkpoint_byte_end",
    )
    if any(not isinstance(getattr(envelope, field), int) or isinstance(getattr(envelope, field), bool) for field in integer_fields):
        issues.append("checkpoint spans and index must be integers")
        return issues
    if envelope.checkpoint_index < 1:
        issues.append("checkpoint_index must be positive")
    if not (1 <= envelope.checkpoint_line_start < envelope.checkpoint_line_end):
        issues.append("checkpoint line span is invalid")
    if not (0 <= envelope.checkpoint_byte_start < envelope.checkpoint_byte_end <= len(plan_bytes)):
        issues.append("checkpoint byte span is invalid")
    elif plan_bytes[envelope.checkpoint_byte_start:envelope.checkpoint_byte_end] != checkpoint_bytes:
        issues.append("checkpoint byte span does not select checkpoint bytes")

    try:
        source_slice = slice_checkpoint_source(envelope.plan_text, checkpoint_index=envelope.checkpoint_index)
        if source_slice is None:
            issues.append("checkpoint index is absent from plan_text")
        else:
            expected_fields = (
                ("checkpoint_name", source_slice.checkpoint_name),
                ("checkpoint_line_start", source_slice.heading_line),
                ("checkpoint_line_end", _compute_checkpoint_line_end(envelope.plan_text, source_slice)),
                ("checkpoint_byte_start", source_slice.checkpoint_byte_start),
                ("checkpoint_byte_end", source_slice.checkpoint_byte_end),
                ("checkpoint_text", source_slice.full_text),
                ("heading_prefix", source_slice.heading_prefix),
            )
            for field_name, expected in expected_fields:
                if getattr(envelope, field_name) != expected:
                    issues.append(f"{field_name} does not match the exact checkpoint slice")
            expected_blocks = extract_source_blocks(
                source_slice, envelope_checkpoint_sha256=envelope.checkpoint_sha256,
                plan_text=envelope.plan_text,
            )
            if tuple(envelope.source_blocks) != expected_blocks:
                issues.append("source_blocks do not exactly reconstruct the checkpoint body")
    except (ValueError, UnicodeDecodeError) as exc:
        issues.append(f"checkpoint reconstruction failed: {exc}")

    try:
        computed = _compute_canonical_envelope_hash(envelope)
        if envelope.canonical_envelope_sha256 != computed:
            issues.append("canonical_envelope_sha256 mismatch")
    except (TypeError, ValueError) as exc:
        issues.append(f"canonical envelope encoding failed: {exc}")
    return issues


def envelope_artifact_dir(run_dir: Path, scope_digest: str) -> Path:
    """Return the artifact directory path for an envelope."""
    return run_dir / "scopes" / scope_digest


def write_envelope_atomic(envelope: ScopeEnvelopeV1, artifact_dir: Path) -> Path:
    """Create an immutable envelope artifact without clobbering an existing one."""
    issues = validate_envelope(envelope)
    if issues:
        raise ValueError(f"Refusing to write invalid envelope: {'; '.join(issues)}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    envelope_path = artifact_dir / "envelope.json"
    payload = envelope.to_dict()
    payload["canonical_envelope_sha256"] = envelope.canonical_envelope_sha256
    payload_bytes = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b"\n"
    )
    if envelope_path.exists():
        try:
            existing_bytes = envelope_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Cannot read existing envelope artifact: {exc}") from exc
        if existing_bytes == payload_bytes:
            # Validate even exact replays: a corrupt artifact must never be
            # treated as an idempotent success.
            read_envelope(envelope_path)
            return envelope_path
        try:
            read_envelope(envelope_path)
        except ValueError as exc:
            raise ValueError("Envelope artifact already exists with different invalid bytes") from exc
        raise ValueError("Envelope artifact already exists with different bytes")

    temporary = envelope_path.with_name(f".envelope.json.{uuid4().hex}.tmp")
    try:
        temporary_fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(temporary_fd, "wb") as handle:
            handle.write(payload_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard link is an atomic no-clobber publication when both paths
            # are in this same directory.  Unlike os.replace, it cannot erase
            # an immutable artifact published by another writer.
            os.link(temporary, envelope_path)
        except FileExistsError:
            existing_bytes = envelope_path.read_bytes()
            if existing_bytes == payload_bytes:
                read_envelope(envelope_path)
                return envelope_path
            raise ValueError("Envelope artifact was concurrently created with different bytes")
        try:
            directory_fd = os.open(str(artifact_dir), os.O_RDONLY)
        except OSError:
            return envelope_path
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    return envelope_path


def read_envelope(envelope_path: Path) -> ScopeEnvelopeV1 | None:
    """Read a validated envelope; only an absent artifact returns ``None``."""
    if not envelope_path.exists():
        return None
    try:
        return parse_envelope_bytes(envelope_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid envelope artifact {envelope_path}: {exc}") from exc


def parse_envelope_bytes(raw: bytes) -> ScopeEnvelopeV1:
    """Parse validated immutable envelope bytes without newline conversion."""
    try:
        text = raw.decode("utf-8", "strict")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Envelope JSON must be an object")
        envelope = ScopeEnvelopeV1.from_dict(data)
        issues = validate_envelope(envelope)
        if issues:
            raise ValueError("Envelope validation failed: " + "; ".join(issues))
        return envelope
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid envelope bytes: {exc}") from exc


# ---------------------------------------------------------------------------
# 4. Proposal and verdict dataclasses with strict parsers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PartitionSpecV1:
    """One child checkpoint in a repartition proposal."""

    title: str
    narrow_goal: str
    source_block_ids: tuple[str, ...]
    implementation_steps: tuple[str, ...]
    verification_commands: tuple[str, ...]
    done_criteria: tuple[str, ...]
    repair_evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "narrow_goal": self.narrow_goal,
            "source_block_ids": list(self.source_block_ids),
            "implementation_steps": list(self.implementation_steps),
            "verification_commands": list(self.verification_commands),
            "done_criteria": list(self.done_criteria),
            "repair_evidence_ids": list(self.repair_evidence_ids),
        }

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> PartitionSpecV1:
        return PartitionSpecV1(
            title=str(data.get("title", "")),
            narrow_goal=str(data.get("narrow_goal", "")),
            source_block_ids=tuple(_require_string_list(data, "source_block_ids")),
            implementation_steps=tuple(_require_string_list(data, "implementation_steps")),
            verification_commands=tuple(_require_string_list(data, "verification_commands")),
            done_criteria=tuple(_require_string_list(data, "done_criteria")),
            repair_evidence_ids=tuple(_require_string_list(data, "repair_evidence_ids", required=False)),
        )


VALID_DISPOSITIONS = frozenset({"review_current_partition", "implement_current_partition"})


@dataclass(frozen=True)
class RepartitionProposalV1:
    """A Full manager's repartition proposal."""

    schema_version: int = 1
    envelope_sha256: str = ""
    source_plan_sha256: str = ""
    rationale: str = ""
    children: tuple[PartitionSpecV1, ...] = ()
    current_disposition: str = ""
    cross_cutting_source_reasons: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "envelope_sha256": self.envelope_sha256,
            "source_plan_sha256": self.source_plan_sha256,
            "rationale": self.rationale,
            "children": [c.to_dict() for c in self.children],
            "current_disposition": self.current_disposition,
            "cross_cutting_source_reasons": dict(self.cross_cutting_source_reasons),
        }

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> RepartitionProposalV1:
        children_data = data.get("children", [])
        if not isinstance(children_data, list):
            children_data = []
        children = tuple(
            PartitionSpecV1.from_dict(c) if isinstance(c, dict) else PartitionSpecV1(
                title="", narrow_goal="",
                source_block_ids=(), implementation_steps=(),
                verification_commands=(), done_criteria=(),
            )
            for c in children_data
        )
        return RepartitionProposalV1(
            schema_version=int(data.get("schema_version", 1)),
            envelope_sha256=str(data.get("envelope_sha256", "")),
            source_plan_sha256=str(data.get("source_plan_sha256", "")),
            rationale=str(data.get("rationale", "")),
            children=children,
            current_disposition=str(data.get("current_disposition", "")),
            cross_cutting_source_reasons=(
                dict(data.get("cross_cutting_source_reasons", {}))
                if isinstance(data.get("cross_cutting_source_reasons", {}), Mapping)
                else {}
            ),
        )


@dataclass(frozen=True)
class RepartitionVerdictV1:
    """Semantic validation verdict."""

    schema_version: int = 1
    proposal_sha256: str = ""
    candidate_sha256: str = ""
    verdict: str = ""  # "accept" or "reject"
    reason: str = ""
    findings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "proposal_sha256": self.proposal_sha256,
            "candidate_sha256": self.candidate_sha256,
            "verdict": self.verdict,
            "reason": self.reason,
            "findings": list(self.findings),
        }

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> RepartitionVerdictV1:
        findings_data = data.get("findings", [])
        findings: tuple[str, ...] = ()
        if isinstance(findings_data, list):
            findings = tuple(str(f) for f in findings_data if isinstance(f, str))
        return RepartitionVerdictV1(
            schema_version=int(data.get("schema_version", 1)),
            proposal_sha256=str(data.get("proposal_sha256", "")),
            candidate_sha256=str(data.get("candidate_sha256", "")),
            verdict=str(data.get("verdict", "")),
            reason=str(data.get("reason", "")),
            findings=findings,
        )


def _require_string_list(data: Mapping[str, object], key: str, *, required: bool = True) -> list[str]:
    raw = data.get(key)
    if raw is None:
        if required:
            raise ValueError(f"Missing required field: {key}")
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Field '{key}' must be a list, got {type(raw).__name__}")
    if required and not raw:
        raise ValueError(f"Field '{key}' must not be empty")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"Field '{key}' items must be strings, got {type(item).__name__}")
        if not item.strip():
            raise ValueError(f"Field '{key}' items must not be blank")
        result.append(item)
    return result


def _require_exact_keys(data: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(data)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        if unknown:
            raise ValueError(f"Unknown field in {label}: {unknown[0]}")
        raise ValueError(f"Missing required field in {label}: {missing[0]}")


def _require_nonblank_string(data: Mapping[str, object], key: str, label: str) -> str:
    value = data.get(key)
    if not _is_nonblank_string(value):
        raise ValueError(f"{label}.{key} must not be empty")
    return str(value)


def parse_proposal_json(
    json_text: str,
    *,
    expected_envelope_sha256: str,
    expected_source_plan_sha256: str,
    valid_source_block_ids: Collection[str],
    valid_repair_evidence_ids: Collection[str],
) -> RepartitionProposalV1:
    """Parse only a complete proposal bound to explicit controller context."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in proposal: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Proposal JSON must be a JSON object")

    top_keys = {
        "schema_version", "envelope_sha256", "source_plan_sha256", "rationale",
        "children", "current_disposition", "cross_cutting_source_reasons",
    }
    _require_exact_keys(data, top_keys, "proposal")
    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError("schema_version must be 1")
    envelope_sha256 = _require_nonblank_string(data, "envelope_sha256", "proposal")
    source_plan_sha256 = _require_nonblank_string(data, "source_plan_sha256", "proposal")
    if not _is_valid_sha256_hex(envelope_sha256):
        raise ValueError("envelope_sha256 is not a valid SHA-256 hex string")
    if not _is_valid_sha256_hex(source_plan_sha256):
        raise ValueError("source_plan_sha256 is not a valid SHA-256 hex string")
    if envelope_sha256 != expected_envelope_sha256:
        raise ValueError("envelope_sha256 does not match the expected envelope")
    if source_plan_sha256 != expected_source_plan_sha256:
        raise ValueError("source_plan_sha256 does not match the expected source plan")
    rationale = _require_nonblank_string(data, "rationale", "proposal")

    children_data = data["children"]
    if not isinstance(children_data, list):
        raise ValueError("children must be a list")
    if len(children_data) < 2:
        raise ValueError(f"At least 2 children required, got {len(children_data)}")

    children: list[PartitionSpecV1] = []
    child_keys = {
        "title", "narrow_goal", "source_block_ids", "repair_evidence_ids",
        "implementation_steps", "verification_commands", "done_criteria",
    }
    for i, child_data in enumerate(children_data):
        if not isinstance(child_data, dict):
            raise ValueError(f"children[{i}] must be a JSON object")
        _require_exact_keys(child_data, child_keys, f"children[{i}]")
        source_ids = _require_string_list(child_data, "source_block_ids")
        repair_ids = _require_string_list(child_data, "repair_evidence_ids", required=False)
        for field_name, values in (("source_block_ids", source_ids), ("repair_evidence_ids", repair_ids)):
            if len(values) != len(set(values)):
                raise ValueError(f"children[{i}].{field_name} contains duplicate references")
        child = PartitionSpecV1(
            title=_require_nonblank_string(child_data, "title", f"children[{i}]"),
            narrow_goal=_require_nonblank_string(child_data, "narrow_goal", f"children[{i}]"),
            source_block_ids=tuple(source_ids),
            repair_evidence_ids=tuple(repair_ids),
            implementation_steps=tuple(_require_string_list(child_data, "implementation_steps")),
            verification_commands=tuple(_require_string_list(child_data, "verification_commands")),
            done_criteria=tuple(_require_string_list(child_data, "done_criteria")),
        )
        children.append(child)

    disposition = _require_nonblank_string(data, "current_disposition", "proposal")
    if disposition not in VALID_DISPOSITIONS:
        raise ValueError(
            f"current_disposition must be one of {sorted(VALID_DISPOSITIONS)}, got {disposition!r}"
        )

    raw_cross_reasons = data["cross_cutting_source_reasons"]
    if not isinstance(raw_cross_reasons, dict):
        raise ValueError("cross_cutting_source_reasons must be an object")
    cross_reasons: dict[str, str] = {}
    for block_id, reason in raw_cross_reasons.items():
        if not _is_nonblank_string(block_id) or not _is_nonblank_string(reason):
            raise ValueError("cross_cutting_source_reasons requires nonblank string IDs and reasons")
        cross_reasons[str(block_id)] = str(reason)

    source_occurrences: dict[str, int] = {}
    repair_occurrences: dict[str, int] = {}
    for child in children:
        for block_id in child.source_block_ids:
            source_occurrences[block_id] = source_occurrences.get(block_id, 0) + 1
        for block_id in child.repair_evidence_ids:
            repair_occurrences[block_id] = repair_occurrences.get(block_id, 0) + 1
    valid_sources = set(valid_source_block_ids)
    valid_repairs = set(valid_repair_evidence_ids)
    unknown = sorted(set(source_occurrences) - valid_sources)
    missing = sorted(valid_sources - set(source_occurrences))
    if unknown:
        raise ValueError(f"Proposal references unknown source blocks: {unknown}")
    if missing:
        raise ValueError(f"Proposal does not cover source blocks: {missing}")
    unknown = sorted(set(repair_occurrences) - valid_repairs)
    missing = sorted(valid_repairs - set(repair_occurrences))
    if unknown:
        raise ValueError(f"Proposal references unknown repair evidence: {unknown}")
    if missing:
        raise ValueError(f"Proposal does not cover repair evidence: {missing}")
    duplicated = {block_id for block_id, count in source_occurrences.items() if count > 1}
    if duplicated - set(cross_reasons):
        raise ValueError(f"Cross-child source reuse lacks an explicit reason: {sorted(duplicated - set(cross_reasons))}")
    unused_reasons = set(cross_reasons) - duplicated
    if unused_reasons or set(cross_reasons) - valid_sources:
        raise ValueError(f"Cross-cutting reasons are unused or unknown: {sorted(unused_reasons | (set(cross_reasons) - valid_sources))}")

    return RepartitionProposalV1(
        schema_version=1,
        envelope_sha256=envelope_sha256,
        source_plan_sha256=source_plan_sha256,
        rationale=rationale,
        children=tuple(children),
        current_disposition=disposition,
        cross_cutting_source_reasons=cross_reasons,
    )


def parse_verdict_json(
    json_text: str,
    *,
    expected_proposal_sha256: str,
    expected_candidate_sha256: str,
) -> RepartitionVerdictV1:
    """Strictly parse a RepartitionVerdictV1 from JSON.

    Raises ValueError on structural violations.
    """
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in verdict: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Verdict JSON must be a JSON object")

    _require_exact_keys(
        data,
        {"schema_version", "proposal_sha256", "candidate_sha256", "verdict", "reason", "findings"},
        "verdict",
    )
    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError("schema_version must be 1")
    proposal_sha256 = _require_nonblank_string(data, "proposal_sha256", "verdict")
    candidate_sha256 = _require_nonblank_string(data, "candidate_sha256", "verdict")
    if not _is_valid_sha256_hex(proposal_sha256) or not _is_valid_sha256_hex(candidate_sha256):
        raise ValueError("verdict hashes must be lowercase SHA-256 hex strings")
    if proposal_sha256 != expected_proposal_sha256:
        raise ValueError("proposal_sha256 does not match expected proposal")
    if candidate_sha256 != expected_candidate_sha256:
        raise ValueError("candidate_sha256 does not match expected candidate")
    verdict = data["verdict"]
    if not isinstance(verdict, str) or verdict not in ("accept", "reject"):
        raise ValueError("verdict must be 'accept' or 'reject'")
    reason = _require_nonblank_string(data, "reason", "verdict")
    findings = _require_string_list(data, "findings", required=False)
    return RepartitionVerdictV1(
        schema_version=1, proposal_sha256=proposal_sha256, candidate_sha256=candidate_sha256,
        verdict=verdict, reason=reason, findings=tuple(findings),
    )


def _is_valid_sha256_hex(s: object) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s)


# ---------------------------------------------------------------------------
# 5. Pure candidate renderer
# ---------------------------------------------------------------------------

# Strict byte-preserving renderer.
_METADATA_JSON_PREFIX = "<!-- aflow-repartition-metadata "
_METADATA_JSON_SUFFIX = " -->"
_CHILD_METADATA_KEYS = {
    "schema_version", "envelope_sha256", "generation_id", "partition_id",
    "ordinal", "total_children", "assigned_source_block_ids",
    "shared_parent_source_block_ids", "source_blocks", "repair_evidence",
}
_BLOCK_METADATA_KEYS = {
    "id", "sha256", "byte_start", "byte_end", "line_start", "line_end",
}


def render_candidate_plan(
    *,
    envelope: ScopeEnvelopeV1,
    proposal: RepartitionProposalV1,
    source_plan_text: str,
    generation_id: str,
    partition_ids: tuple[str, ...],
    repair_evidence_blocks: tuple[SourceBlock, ...] = (),
    repair_evidence_artifact_references: Mapping[str, str] | None = None,
) -> str:
    """Render a candidate from verified bytes; never use a byte span as text index."""
    issues = validate_envelope(envelope)
    if issues:
        raise ValueError("Cannot render with invalid envelope: " + "; ".join(issues))
    if proposal.schema_version != 1 or proposal.envelope_sha256 != envelope.canonical_envelope_sha256:
        raise ValueError("proposal does not match the envelope")
    if proposal.source_plan_sha256 != envelope.plan_sha256 or len(proposal.children) < 2:
        raise ValueError("proposal does not match the source plan or lacks children")
    generation_id = _require_identifier(generation_id, "generation_id")
    if len(partition_ids) != len(proposal.children):
        raise ValueError("partition_ids must contain exactly one ID per child")
    partition_ids = tuple(_require_identifier(value, "partition_id") for value in partition_ids)
    if len(set(partition_ids)) != len(partition_ids):
        raise ValueError("partition_ids must be unique")
    plan_bytes = source_plan_text.encode("utf-8")
    if _sha256_hex(plan_bytes) != envelope.plan_sha256:
        raise ValueError("source plan hash does not match envelope")
    if plan_bytes[envelope.checkpoint_byte_start:envelope.checkpoint_byte_end] != envelope.checkpoint_text.encode("utf-8"):
        raise ValueError("source plan checkpoint bytes do not match envelope")
    source_by_id = {block.block_id: block for block in envelope.source_blocks}
    repair_by_id = {block.block_id: block for block in repair_evidence_blocks}
    if len(source_by_id) != len(envelope.source_blocks) or len(repair_by_id) != len(repair_evidence_blocks):
        raise ValueError("source or repair evidence block IDs are not unique")
    artifact_references = dict(repair_evidence_artifact_references or {})
    if set(artifact_references) - set(repair_by_id):
        raise ValueError("repair evidence artifact mapping includes an unknown ID")
    for block_id, block in repair_by_id.items():
        if block.content_sha256 != _sha256_hex(block.text.encode("utf-8")):
            raise ValueError(f"repair evidence {block_id} has an invalid content hash")
    for block_id, artifact_reference in artifact_references.items():
        if not _is_nonblank_string(artifact_reference) or "-->" in str(artifact_reference) or "\r" in str(artifact_reference) or "\n" in str(artifact_reference):
            raise ValueError(f"repair evidence {block_id} has an unsafe artifact reference")
    newline = "\r\n" if b"\r\n" in plan_bytes else "\n"
    children: list[bytes] = []
    for index, child in enumerate(proposal.children, start=1):
        if not _is_nonblank_string(child.title) or not _is_nonblank_string(child.narrow_goal):
            raise ValueError(f"child {index} has blank guidance")
        if not child.source_block_ids or any(block_id not in source_by_id for block_id in child.source_block_ids):
            raise ValueError(f"child {index} references an unknown source block")
        if len(child.source_block_ids) != len(set(child.source_block_ids)):
            raise ValueError(f"child {index} repeats a source block")
        if any(block_id not in repair_by_id for block_id in child.repair_evidence_ids):
            raise ValueError(f"child {index} references unknown repair evidence")
        if len(child.repair_evidence_ids) != len(set(child.repair_evidence_ids)):
            raise ValueError(f"child {index} repeats repair evidence")
        missing_refs = [block_id for block_id in child.repair_evidence_ids if not _is_nonblank_string(artifact_references.get(block_id))]
        if missing_refs:
            raise ValueError(f"child {index} lacks repair artifact references: {missing_refs}")
        children.append(_render_child_checkpoint(
            child=child, child_ordinal=index, total_children=len(proposal.children),
            partition_id=partition_ids[index - 1], generation_id=generation_id,
            envelope=envelope, source_by_id=source_by_id, repair_by_id=repair_by_id,
            artifact_references=artifact_references, newline=newline,
        ))
    candidate_bytes = (
        plan_bytes[:envelope.checkpoint_byte_start]
        + newline.encode("ascii").join(children)
        + plan_bytes[envelope.checkpoint_byte_end:]
    )
    return candidate_bytes.decode("utf-8", "strict")


def _render_child_checkpoint(
    *, child: PartitionSpecV1, child_ordinal: int, total_children: int,
    partition_id: str, generation_id: str, envelope: ScopeEnvelopeV1,
    source_by_id: dict[str, SourceBlock], repair_by_id: dict[str, SourceBlock],
    artifact_references: Mapping[str, str], newline: str,
) -> bytes:
    shared_ids = tuple(
        block.block_id for block in source_by_id.values()
        if block.section_label is not None
        and block.section_label.casefold().startswith(("**goal", "**context"))
    )
    embedded_ids = tuple(dict.fromkeys((*shared_ids, *child.source_block_ids)))
    embedded_sources = tuple(source_by_id[block_id] for block_id in embedded_ids)
    metadata = {
        "schema_version": 1, "envelope_sha256": envelope.canonical_envelope_sha256,
        "generation_id": generation_id, "partition_id": partition_id,
        "ordinal": child_ordinal, "total_children": total_children,
        "assigned_source_block_ids": list(child.source_block_ids),
        "shared_parent_source_block_ids": list(shared_ids),
        "source_blocks": [_block_metadata(block) for block in embedded_sources],
        "repair_evidence": [
            {**_block_metadata(repair_by_id[block_id]), "artifact_reference": artifact_references[block_id]}
            for block_id in child.repair_evidence_ids
        ],
    }
    authoritative_payload = _authoritative_payload(embedded_sources, newline)
    fence = _safe_fence(authoritative_payload)
    parts = [
        f"### [ ] {envelope.checkpoint_name} / Partition {child_ordinal}/{total_children}: {child.title}", newline, newline,
        _METADATA_JSON_PREFIX + json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + _METADATA_JSON_SUFFIX,
        newline, newline, fence + "aflow-authoritative-source", newline,
        authoritative_payload, fence, newline, newline,
    ]
    if child.repair_evidence_ids:
        evidence_payload = _repair_evidence_payload(
            tuple(repair_by_id[block_id] for block_id in child.repair_evidence_ids),
            artifact_references,
            newline,
        )
        evidence_fence = _safe_fence(evidence_payload)
        parts.extend([evidence_fence + "aflow-repair-evidence", newline,
                      "# Non-authoritative corrective evidence; authoritative scope is above.", newline,
                      evidence_payload, evidence_fence, newline, newline])
    parts.extend([f"**Narrow Goal:** {child.narrow_goal}", newline, newline, "**Steps:**", newline, newline])
    for step in child.implementation_steps:
        parts.extend([f"- [ ] {step}", newline])
    parts.extend([newline, "**Done When:**", newline, newline])
    for criterion in child.done_criteria:
        parts.extend([f"- {criterion}", newline])
    parts.extend([newline, "**Verification:**", newline, newline])
    for command in child.verification_commands:
        parts.extend([f"- Run: `{command}`", newline])
    parts.append(newline)
    return "".join(parts).encode("utf-8")


def _block_metadata(block: SourceBlock) -> dict[str, object]:
    return {"id": block.block_id, "sha256": block.content_sha256, "byte_start": block.byte_start,
            "byte_end": block.byte_end, "line_start": block.line_start, "line_end": block.line_end}


def _source_marker(block: SourceBlock) -> str:
    return (f"<!-- aflow-source-block id={block.block_id} sha256={block.content_sha256} "
            f"byte_start={block.byte_start} byte_end={block.byte_end} -->")


def _repair_marker(block: SourceBlock, artifact_reference: str) -> str:
    return (f"<!-- aflow-repair-evidence id={block.block_id} sha256={block.content_sha256} "
            f"artifact_reference={json.dumps(artifact_reference, ensure_ascii=False)} -->")


def _authoritative_payload(blocks: tuple[SourceBlock, ...], newline: str) -> str:
    return "".join(
        _source_marker(block) + newline + block.text
        + ("" if block.text.endswith(("\n", "\r")) else newline) + newline
        for block in blocks
    )


def _repair_evidence_payload(
    blocks: tuple[SourceBlock, ...],
    artifact_references: Mapping[str, str],
    newline: str,
) -> str:
    return "".join(
        _repair_marker(block, artifact_references[block.block_id]) + newline
        + block.text + ("" if block.text.endswith(("\n", "\r")) else newline)
        + newline
        for block in blocks
    )


def _safe_fence(payload: str) -> str:
    longest = max((len(run) for run in re.findall(r"[`~]+", payload)), default=2)
    return "~" * max(3, longest + 1)


# ---------------------------------------------------------------------------
# 6. Mechanical validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MechanicalValidationResult:
    """Machine-readable result of candidate mechanical validation."""

    valid: bool
    source_plan_sha256: str
    candidate_plan_sha256: str
    envelope_sha256: str
    unchanged_prefix: bool  # bytes before checkpoint unchanged
    unchanged_suffix: bool  # bytes after checkpoint unchanged
    parse_success: bool
    child_count: int
    all_children_unchecked: bool
    source_block_coverage: dict[str, bool]  # block_id -> covered
    repair_evidence_coverage: dict[str, bool]
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "source_plan_sha256": self.source_plan_sha256,
            "candidate_plan_sha256": self.candidate_plan_sha256,
            "envelope_sha256": self.envelope_sha256,
            "unchanged_prefix": self.unchanged_prefix,
            "unchanged_suffix": self.unchanged_suffix,
            "parse_success": self.parse_success,
            "child_count": self.child_count,
            "all_children_unchecked": self.all_children_unchecked,
            "source_block_coverage": self.source_block_coverage,
            "repair_evidence_coverage": self.repair_evidence_coverage,
            "issues": list(self.issues),
        }


def validate_candidate_mechanically(
    *,
    source_plan_text: str,
    candidate_plan_text: str,
    envelope: ScopeEnvelopeV1,
    proposal: RepartitionProposalV1,
    repair_evidence_blocks: tuple[SourceBlock, ...] = (),
    repair_evidence_artifact_references: Mapping[str, str],
    expected_generation_id: str,
    expected_partition_ids: tuple[str, ...],
) -> MechanicalValidationResult:
    """Conservatively validate one candidate from its bytes, not model claims."""
    issues: list[str] = []
    try:
        expected_generation_id = _require_identifier(
            expected_generation_id, "expected_generation_id",
        )
    except ValueError as exc:
        issues.append(f"invalid_expected_generation_id:{exc}")
    if (
        len(expected_partition_ids) != len(proposal.children)
        or len(set(expected_partition_ids)) != len(expected_partition_ids)
        or any(not _is_nonblank_string(value) for value in expected_partition_ids)
    ):
        issues.append("invalid_expected_partition_ids")
    # Controller-owned artifact references must always be provided.
    controller_artifact_refs: Mapping[str, str] = dict(repair_evidence_artifact_references)
    known_evidence_ids = {block.block_id for block in repair_evidence_blocks}
    if set(controller_artifact_refs) != known_evidence_ids:
        raise ValueError(
            "repair_evidence_artifact_references must contain exactly one entry "
            "per repair evidence block ID"
        )
    for block_id, ref in controller_artifact_refs.items():
        if not _is_nonblank_string(ref) or "-->" in ref or "\r" in ref or "\n" in ref:
            raise ValueError(
                f"repair evidence {block_id} has an unsafe artifact reference"
            )
    source_bytes = source_plan_text.encode("utf-8")
    candidate_bytes = candidate_plan_text.encode("utf-8")
    source_hash = _sha256_hex(source_bytes)
    candidate_hash = _sha256_hex(candidate_bytes)
    envelope_issues = validate_envelope(envelope)
    if envelope_issues:
        issues.extend(f"invalid_envelope:{issue}" for issue in envelope_issues)
    if source_hash != envelope.plan_sha256:
        issues.append("source_plan_hash_does_not_match_envelope")
    if source_hash != proposal.source_plan_sha256:
        issues.append("source_plan_hash_does_not_match_proposal")
    if proposal.envelope_sha256 != envelope.canonical_envelope_sha256:
        issues.append("proposal_envelope_hash_mismatch")
    if len(proposal.children) < 2:
        issues.append("proposal_requires_at_least_two_children")
    if source_bytes[envelope.checkpoint_byte_start:envelope.checkpoint_byte_end] != envelope.checkpoint_text.encode("utf-8"):
        issues.append("source_checkpoint_slice_mismatch")

    prefix_unchanged = candidate_bytes[:envelope.checkpoint_byte_start] == source_bytes[:envelope.checkpoint_byte_start]
    if not prefix_unchanged:
        issues.append("prefix_bytes_changed")
    source_suffix = source_bytes[envelope.checkpoint_byte_end:]
    candidate_suffix = candidate_bytes[-len(source_suffix):] if source_suffix else b""
    suffix_unchanged = candidate_suffix == source_suffix
    if not suffix_unchanged:
        issues.append("suffix_bytes_changed")

    parse_success = True
    source_sections = ()
    candidate_sections = ()
    try:
        source_sections = parse_plan_text(source_plan_text, source_path=Path("<source-plan>")).sections
    except (PlanParseError, ValueError) as exc:
        issues.append(f"source_parse_failed:{exc}")
    try:
        candidate_sections = parse_plan_text(candidate_plan_text, source_path=Path("<candidate-plan>")).sections
    except (PlanParseError, ValueError) as exc:
        parse_success = False
        issues.append(f"candidate_parse_failed:{exc}")

    expected_child_count = len(proposal.children)
    checkpoint_offset = envelope.checkpoint_index - 1
    child_sections = candidate_sections[checkpoint_offset:checkpoint_offset + expected_child_count]
    if source_sections and len(candidate_sections) != len(source_sections) - 1 + expected_child_count:
        issues.append("candidate_checkpoint_count_mismatch")
    if len(child_sections) != expected_child_count:
        issues.append("candidate_missing_or_extra_children")
    child_slices: list[CheckpointSourceSlice] = []
    for child_index in range(expected_child_count):
        child_slice = slice_checkpoint_source(candidate_plan_text, checkpoint_index=envelope.checkpoint_index + child_index)
        if child_slice is None:
            issues.append(f"child_slice_missing:{child_index + 1}")
        else:
            child_slices.append(child_slice)

    all_unchecked = True
    if len(child_sections) != expected_child_count:
        all_unchecked = False
    for index, section in enumerate(child_sections, start=1):
        expected_name = f"{envelope.checkpoint_name} / Partition {index}/{expected_child_count}: {proposal.children[index - 1].title}"
        if section.name != expected_name:
            issues.append(f"child_heading_mismatch:{index}")
        if section.heading_checked or section.checked_step_count:
            all_unchecked = False
            issues.append(f"child_is_checked:{index}")
    if not all_unchecked and not any(issue.startswith("child_is_checked:") for issue in issues):
        issues.append("children_not_all_unchecked")

    source_by_id = {block.block_id: block for block in envelope.source_blocks}
    repair_by_id = {block.block_id: block for block in repair_evidence_blocks}
    source_coverage = {block.block_id: False for block in envelope.source_blocks}
    evidence_coverage = {block.block_id: False for block in repair_evidence_blocks}
    shared_ids = tuple(
        block.block_id for block in envelope.source_blocks
        if block.section_label is not None and block.section_label.casefold().startswith(("**goal", "**context"))
    )
    child_newline = "\r\n" if b"\r\n" in candidate_bytes else "\n"
    parsed_partition_ids: list[str] = []
    for ordinal, child_slice in enumerate(child_slices, start=1):
        child = proposal.children[ordinal - 1]
        metadata = _read_child_metadata(child_slice.full_text)
        if metadata is None:
            issues.append(f"child_metadata_missing_or_invalid:{ordinal}")
            continue
        metadata_issues = _validate_child_metadata_schema(
            metadata, child=child, ordinal=ordinal,
            total_children=expected_child_count,
            expected_shared_parent_ids=shared_ids,
        )
        issues.extend(metadata_issues)
        if metadata.get("envelope_sha256") != envelope.canonical_envelope_sha256:
            issues.append(f"child_metadata_envelope_mismatch:{ordinal}")
        if metadata.get("ordinal") != ordinal or metadata.get("total_children") != expected_child_count:
            issues.append(f"child_metadata_order_mismatch:{ordinal}")
        partition_id = metadata.get("partition_id")
        if not _is_nonblank_string(partition_id):
            issues.append(f"child_partition_id_missing:{ordinal}")
        else:
            parsed_partition_ids.append(str(partition_id))
        if metadata.get("generation_id") != expected_generation_id:
            issues.append(f"child_generation_id_mismatch:{ordinal}")
        if metadata.get("assigned_source_block_ids") != list(child.source_block_ids):
            issues.append(f"child_source_assignment_mismatch:{ordinal}")
        expected_ids = tuple(dict.fromkeys((*shared_ids, *child.source_block_ids)))
        metadata_blocks = metadata.get("source_blocks")
        if not isinstance(metadata_blocks, list):
            metadata_blocks = []
        expected_source_metadata = [
            _block_metadata(source_by_id[block_id])
            for block_id in expected_ids if block_id in source_by_id
        ]
        if metadata_blocks != expected_source_metadata:
            issues.append(f"child_source_metadata_mismatch:{ordinal}")
        for block_id in expected_ids:
            block = source_by_id.get(block_id)
            if block is None:
                issues.append(f"unknown_source_block_in_proposal:{ordinal}:{block_id}")
                continue
        authoritative_payloads, authoritative_issue = _fence_payloads(
            child_slice.full_text, "aflow-authoritative-source",
        )
        expected_authoritative_payload = _authoritative_payload(
            tuple(source_by_id[block_id] for block_id in expected_ids if block_id in source_by_id),
            child_newline,
        )
        if authoritative_issue is not None or len(authoritative_payloads) != 1:
            issues.append(f"authoritative_fence_invalid:{ordinal}")
        elif authoritative_payloads[0] != expected_authoritative_payload:
            issues.append(f"authoritative_payload_mismatch:{ordinal}")
        else:
            for block_id in child.source_block_ids:
                if block_id in source_coverage:
                    source_coverage[block_id] = True
        metadata_evidence = metadata.get("repair_evidence")
        if not isinstance(metadata_evidence, list):
            metadata_evidence = []
        expected_evidence_metadata: list[dict[str, object]] = []
        for block_id in child.repair_evidence_ids:
            block = repair_by_id.get(block_id)
            if block is None:
                issues.append(f"unknown_repair_evidence_in_proposal:{ordinal}:{block_id}")
                continue
            controller_ref = controller_artifact_refs.get(block_id)
            if not controller_ref:
                issues.append(f"repair_evidence_missing_controller_reference:{ordinal}:{block_id}")
                continue
            matching = [
                entry for entry in metadata_evidence
                if isinstance(entry, dict) and entry.get("id") == block_id
            ]
            entry = matching[0] if len(matching) == 1 else None
            if not isinstance(entry, dict) or entry.get("artifact_reference") != controller_ref:
                issues.append(f"repair_metadata_mismatch:{ordinal}:{block_id}")
                continue
            expected_entry = {**_block_metadata(block), "artifact_reference": controller_ref}
            expected_evidence_metadata.append(expected_entry)
        if metadata_evidence != expected_evidence_metadata:
            issues.append(f"child_repair_metadata_mismatch:{ordinal}")
        evidence_payloads, evidence_issue = _fence_payloads(
            child_slice.full_text, "aflow-repair-evidence",
        )
        expected_evidence_payload: str | None = None
        if child.repair_evidence_ids:
            expected_evidence_payload = (
                "# Non-authoritative corrective evidence; authoritative scope is above."
                + child_newline
                + _repair_evidence_payload(
                    tuple(
                        repair_by_id[block_id] for block_id in child.repair_evidence_ids
                        if block_id in repair_by_id and block_id in controller_artifact_refs
                    ),
                    controller_artifact_refs,
                    child_newline,
                )
            )
            if evidence_issue is not None or len(evidence_payloads) != 1:
                issues.append(f"repair_evidence_fence_invalid:{ordinal}")
            elif evidence_payloads[0] != expected_evidence_payload:
                issues.append(f"repair_evidence_payload_mismatch:{ordinal}")
            elif len(expected_evidence_metadata) == len(child.repair_evidence_ids):
                for block_id in child.repair_evidence_ids:
                    evidence_coverage[block_id] = True
        elif evidence_issue is not None or evidence_payloads:
            issues.append(f"unexpected_repair_evidence_fence:{ordinal}")

        placement_issues = _validate_fence_placement(
            child_slice.full_text,
            ordinal=ordinal,
            expects_repair_evidence=bool(child.repair_evidence_ids),
            expected_auth_fence=_safe_fence(expected_authoritative_payload),
            expected_evidence_fence=(
                _safe_fence(expected_evidence_payload)
                if expected_evidence_payload is not None
                else None
            ),
        )
        issues.extend(placement_issues)
    if len(parsed_partition_ids) != len(set(parsed_partition_ids)):
        issues.append("partition_ids_are_not_unique")
    if tuple(parsed_partition_ids) != expected_partition_ids:
        issues.append("partition_ids_do_not_match_expected_order")
    for block_id, covered in source_coverage.items():
        if not covered:
            issues.append(f"source_block_not_covered:{block_id}")
    for block_id, covered in evidence_coverage.items():
        if not covered:
            issues.append(f"repair_evidence_not_covered:{block_id}")
    missing_sources = [block_id for block_id, covered in source_coverage.items() if not covered]
    if missing_sources:
        issues.append(f"Source blocks not covered by candidate payload: {missing_sources}")
    missing_evidence = [block_id for block_id, covered in evidence_coverage.items() if not covered]
    if missing_evidence:
        issues.append(f"Repair evidence not covered by candidate payload: {missing_evidence}")

    return MechanicalValidationResult(
        valid=not issues, source_plan_sha256=source_hash, candidate_plan_sha256=candidate_hash,
        envelope_sha256=envelope.canonical_envelope_sha256, unchanged_prefix=prefix_unchanged,
        unchanged_suffix=suffix_unchanged, parse_success=parse_success, child_count=len(child_sections),
        all_children_unchecked=all_unchecked, source_block_coverage=source_coverage,
        repair_evidence_coverage=evidence_coverage, issues=tuple(issues),
    )


def _read_child_metadata(child_text: str) -> dict[str, object] | None:
    values = []
    for line in child_text.splitlines():
        if line.startswith(_METADATA_JSON_PREFIX) and line.endswith(_METADATA_JSON_SUFFIX):
            try:
                parsed = json.loads(line[len(_METADATA_JSON_PREFIX):-len(_METADATA_JSON_SUFFIX)])
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, dict):
                return None
            values.append(parsed)
    return values[0] if len(values) == 1 else None


def _validate_child_metadata_schema(
    metadata: Mapping[str, object],
    *,
    child: PartitionSpecV1,
    ordinal: int,
    total_children: int,
    expected_shared_parent_ids: tuple[str, ...] | None = None,
) -> list[str]:
    issues: list[str] = []
    if set(metadata) != _CHILD_METADATA_KEYS:
        issues.append(f"child_metadata_keys_mismatch:{ordinal}")
    if metadata.get("schema_version") != 1 or isinstance(metadata.get("schema_version"), bool):
        issues.append(f"child_metadata_schema_mismatch:{ordinal}")
    for key in ("envelope_sha256", "generation_id", "partition_id"):
        if not _is_nonblank_string(metadata.get(key)):
            issues.append(f"child_metadata_type_mismatch:{ordinal}:{key}")
    for key, expected in (("ordinal", ordinal), ("total_children", total_children)):
        value = metadata.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value != expected:
            issues.append(f"child_metadata_type_mismatch:{ordinal}:{key}")
    for key in ("assigned_source_block_ids", "shared_parent_source_block_ids"):
        value = metadata.get(key)
        if (
            not isinstance(value, list)
            or any(not _is_nonblank_string(item) for item in value)
            or len(value) != len(set(value))
        ):
            issues.append(f"child_metadata_type_mismatch:{ordinal}:{key}")
    if metadata.get("assigned_source_block_ids") != list(child.source_block_ids):
        issues.append(f"child_source_assignment_mismatch:{ordinal}")
    if expected_shared_parent_ids is not None and metadata.get("shared_parent_source_block_ids") != list(expected_shared_parent_ids):
        issues.append(f"child_shared_parent_mismatch:{ordinal}")
    for key, extra_keys in (
        ("source_blocks", set()),
        ("repair_evidence", {"artifact_reference"}),
    ):
        value = metadata.get(key)
        expected_keys = _BLOCK_METADATA_KEYS | extra_keys
        if not isinstance(value, list):
            issues.append(f"child_metadata_type_mismatch:{ordinal}:{key}")
            continue
        ids: list[str] = []
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != expected_keys:
                issues.append(f"child_metadata_entry_keys_mismatch:{ordinal}:{key}")
                continue
            block_id = entry.get("id")
            if not _is_nonblank_string(block_id) or not _is_valid_sha256_hex(entry.get("sha256")):
                issues.append(f"child_metadata_entry_type_mismatch:{ordinal}:{key}")
                continue
            ids.append(str(block_id))
            for field in ("byte_start", "byte_end", "line_start", "line_end"):
                field_value = entry.get(field)
                if not isinstance(field_value, int) or isinstance(field_value, bool):
                    issues.append(f"child_metadata_entry_type_mismatch:{ordinal}:{key}")
            if extra_keys and not _is_nonblank_string(entry.get("artifact_reference")):
                issues.append(f"child_metadata_entry_type_mismatch:{ordinal}:{key}")
        if len(ids) != len(set(ids)):
            issues.append(f"child_metadata_duplicate_ids:{ordinal}:{key}")
    return issues


def _fence_payloads(child_text: str, expected_info: str) -> tuple[list[str], str | None]:
    """Return exact interiors of top-level matching fences, preserving newlines."""
    lines = child_text.splitlines(keepends=True)
    payloads: list[str] = []
    index = 0
    while index < len(lines):
        opener = re.match(r"^(`{3,}|~{3,})([^\r\n]*)(?:\r?\n|$)", lines[index])
        if opener is None:
            index += 1
            continue
        marker = opener.group(1)
        info = opener.group(2)
        closing = index + 1
        while closing < len(lines):
            close = re.match(r"^(`{3,}|~{3,})[ \t]*(?:\r?\n|$)", lines[closing])
            if (
                close is not None
                and close.group(1)[0] == marker[0]
                and len(close.group(1)) >= len(marker)
            ):
                break
            closing += 1
        if closing == len(lines):
            return payloads, "unclosed_fence"
        if info == expected_info:
            payloads.append("".join(lines[index + 1:closing]))
        index = closing + 1
    return payloads, None


def _fence_line_indexes(child_text: str, fence_info: str) -> tuple[tuple[int, int], ...]:
    """Return (opener_line, closer_line) pairs for matching fences in child_text.

    Line numbers are 0-based indices into child_text.splitlines(keepends=True).
    """
    lines = child_text.splitlines(keepends=True)
    pairs: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        opener = re.match(r"^(`{3,}|~{3,})([^\r\n]*)(?:\r?\n|$)", lines[index])
        if opener is None:
            index += 1
            continue
        marker = opener.group(1)
        info = opener.group(2)
        closing = index + 1
        while closing < len(lines):
            close = re.match(r"^(`{3,}|~{3,})[ \t]*(?:\r?\n|$)", lines[closing])
            if (
                close is not None
                and close.group(1)[0] == marker[0]
                and len(close.group(1)) >= len(marker)
            ):
                break
            closing += 1
        if closing == len(lines):
            return tuple(pairs)
        if info == fence_info:
            pairs.append((index, closing))
        index = closing + 1
    return tuple(pairs)


def _validate_fence_placement(
    child_text: str,
    *,
    ordinal: int,
    expects_repair_evidence: bool,
    expected_auth_fence: str,
    expected_evidence_fence: str | None = None,
) -> list[str]:
    """Validate exact canonical fence markers and positions.

    The authoritative fence must use exactly *expected_auth_fence* for both
    opener and closer, appear immediately after the JSON metadata line, and
    precede ``**Narrow Goal:**``.

    When repair evidence is expected, its fence must use exactly
    *expected_evidence_fence*, appear immediately after the authoritative
    fence closer, and precede ``**Narrow Goal:**``.  When no evidence is
    assigned, any evidence fence is rejected.
    """
    issues: list[str] = []
    lines = child_text.splitlines(keepends=True)

    # Locate the JSON metadata line.
    metadata_line: int | None = None
    for index, line in enumerate(lines):
        if line.strip().startswith(_METADATA_JSON_PREFIX) and line.rstrip().endswith(_METADATA_JSON_SUFFIX):
            metadata_line = index
            break

    # Locate ``**Narrow Goal:**`` line.
    narrow_goal_line: int | None = None
    for index, line in enumerate(lines):
        if line.strip().startswith("**Narrow Goal:**"):
            narrow_goal_line = index
            break

    auth_pairs = _fence_line_indexes(child_text, "aflow-authoritative-source")
    if len(auth_pairs) != 1:
        # Already caught by payload validation; skip placement check.
        return issues
    auth_opener, auth_closer = auth_pairs[0]

    # Exact marker check.
    auth_opener_stripped = lines[auth_opener].rstrip("\r\n")
    auth_closer_stripped = lines[auth_closer].rstrip("\r\n")
    if auth_opener_stripped != expected_auth_fence + "aflow-authoritative-source":
        issues.append(f"authoritative_fence_opener_mismatch:{ordinal}")
    if auth_closer_stripped != expected_auth_fence:
        issues.append(f"authoritative_fence_closer_mismatch:{ordinal}")

    # Authoritative fence must appear immediately after the metadata line
    # (with at most one blank line between, which the renderer emits).
    if metadata_line is not None:
        expected_auth_opener_line = metadata_line + 2  # metadata + blank + fence
        if auth_opener != expected_auth_opener_line:
            issues.append(f"authoritative_fence_not_immediately_after_metadata:{ordinal}")
    else:
        issues.append(f"metadata_line_not_found:{ordinal}")

    if narrow_goal_line is not None and auth_opener >= narrow_goal_line:
        issues.append(f"authoritative_fence_after_narrow_goal:{ordinal}")

    repair_pairs = _fence_line_indexes(child_text, "aflow-repair-evidence")
    if expects_repair_evidence and len(repair_pairs) != 1:
        # Already caught by payload validation; skip placement check.
        pass
    elif expects_repair_evidence and len(repair_pairs) == 1:
        repair_opener, repair_closer = repair_pairs[0]
        if expected_evidence_fence is not None:
            repair_opener_stripped = lines[repair_opener].rstrip("\r\n")
            repair_closer_stripped = lines[repair_closer].rstrip("\r\n")
            if repair_opener_stripped != expected_evidence_fence + "aflow-repair-evidence":
                issues.append(f"repair_evidence_fence_opener_mismatch:{ordinal}")
            if repair_closer_stripped != expected_evidence_fence:
                issues.append(f"repair_evidence_fence_closer_mismatch:{ordinal}")
        # Repair fence must appear immediately after the authoritative fence closing
        # block (one blank line separates them; the renderer emits newline + newline).
        if repair_opener != auth_closer + 2:
            issues.append(f"repair_evidence_fence_not_immediately_after_authoritative:{ordinal}")
        if narrow_goal_line is not None and repair_closer >= narrow_goal_line:
            issues.append(f"repair_evidence_fence_after_narrow_goal:{ordinal}")
    elif not expects_repair_evidence and repair_pairs:
        issues.append(f"unexpected_repair_evidence_fence_placement:{ordinal}")

    return issues


# ---------------------------------------------------------------------------
# 7. Envelope-to-boundary drift validator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DriftValidationResult:
    """Result of envelope-to-boundary drift validation."""

    allowed: bool
    issues: tuple[str, ...]
    boundary_plan_sha256: str


_CONTROLLER_GIT_FIELDS = (
    "Plan Branch", "Pre-Handoff Base HEAD", "Last Reviewed HEAD",
)
_CONTROLLER_GIT_FIELD_RE = re.compile(
    r"^(\s*-\s+(?:Plan Branch|Pre-Handoff Base HEAD|Last Reviewed HEAD):\s+`)([^`\r\n]*)(`\r?\n?)$"
)
_LIVE_TASK_MARKER_RE = re.compile(r"^[-*]\s+\[ \]\s+")


def validate_envelope_boundary_drift(
    *,
    envelope: ScopeEnvelopeV1,
    boundary_plan_text: str,
    git_tracking_allowed: bool = True,
) -> DriftValidationResult:
    """Allow only controller fields and forward live markers, despite byte shifts."""
    boundary_hash = _sha256_hex(boundary_plan_text.encode("utf-8"))
    envelope_issues = validate_envelope(envelope)
    if envelope_issues:
        return DriftValidationResult(
            allowed=False,
            issues=tuple(f"invalid_envelope:{issue}" for issue in envelope_issues),
            boundary_plan_sha256=boundary_hash,
        )
    if boundary_plan_text == envelope.plan_text:
        return DriftValidationResult(allowed=True, issues=(), boundary_plan_sha256=boundary_hash)

    source_lines = envelope.plan_text.splitlines(keepends=True)
    boundary_lines = boundary_plan_text.splitlines(keepends=True)
    issues: list[str] = []
    if len(source_lines) != len(boundary_lines):
        return DriftValidationResult(
            allowed=False, issues=("structural_line_change_not_allowed",), boundary_plan_sha256=boundary_hash,
        )

    git_lines = _live_git_tracking_line_indexes(source_lines)
    current_start = envelope.checkpoint_line_start - 1
    current_end = envelope.checkpoint_line_end - 1
    fenced_lines = _fenced_line_indexes(source_lines)
    for index, (source_line, boundary_line) in enumerate(zip(source_lines, boundary_lines), start=0):
        if source_line == boundary_line:
            continue
        if index in git_lines:
            if not git_tracking_allowed:
                issues.append(f"git_tracking_change_not_allowed:line_{index + 1}")
            elif _normalize_controller_git_field(source_line) == _normalize_controller_git_field(boundary_line) and _normalize_controller_git_field(source_line) is not None:
                continue
            else:
                issues.append(f"git_tracking_structure_changed:line_{index + 1}")
            continue
        if current_start <= index < current_end and index not in fenced_lines:
            if _is_forward_live_marker_transition(source_line, boundary_line):
                continue
            issues.append(f"checkpoint_change_not_allowed:line_{index + 1}")
            continue
        issues.append(f"external_change_not_allowed:line_{index + 1}")
    return DriftValidationResult(
        allowed=not issues, issues=tuple(issues), boundary_plan_sha256=boundary_hash,
    )


def _normalize_controller_git_field(line: str) -> str | None:
    match = _CONTROLLER_GIT_FIELD_RE.match(line)
    if match is None:
        return None
    return match.group(1) + "<AFLOW_CONTROLLER_VALUE>" + match.group(3)


def _is_forward_live_marker_transition(source_line: str, boundary_line: str) -> bool:
    if SECTION_RE.match(source_line) is None and _LIVE_TASK_MARKER_RE.match(source_line) is None:
        return False
    return (
        source_line.replace("[ ]", "[x]", 1) == boundary_line
        or source_line.replace("[ ]", "[X]", 1) == boundary_line
    )


def _live_git_tracking_line_indexes(lines: list[str]) -> set[int]:
    indexes: set[int] = set()
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    in_git = False
    for index, line in enumerate(lines):
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence, fence_char, fence_len = True, marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence, fence_char, fence_len = False, None, 0
            continue
        if in_fence:
            continue
        if line.strip() == "## Git Tracking":
            in_git = True
            continue
        if in_git and re.match(r"^#{1,3}\s", line):
            in_git = False
        if in_git:
            indexes.add(index)
    return indexes


def _fenced_line_indexes(lines: list[str]) -> set[int]:
    indexes: set[int] = set()
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    for index, line in enumerate(lines):
        if in_fence:
            indexes.add(index)
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                indexes.add(index)
                in_fence, fence_char, fence_len = True, marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                indexes.add(index)
                in_fence, fence_char, fence_len = False, None, 0
    return indexes

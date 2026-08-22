from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from pathlib import Path


SECTION_RE = re.compile(r"^###\s+\[([ xX])\]\s+(Checkpoint\b.*)$")
STEP_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
GIT_TRACKING_RE = re.compile(r"^##\s+Git Tracking\b")
NON_CHECKPOINT_HEADING_RE = re.compile(r"^#{1,3}\s+")
GIT_TRACKING_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:Plan Branch|Pre-Handoff Base HEAD|Last Reviewed HEAD|Review Log):"
)


@dataclass(frozen=True)
class CheckpointSection:
    line_number: int
    name: str
    heading_checked: bool
    unchecked_step_count: int
    checked_step_count: int = 0


@dataclass(frozen=True)
class PlanSnapshot:
    current_checkpoint_name: str | None
    unchecked_checkpoint_count: int
    current_checkpoint_unchecked_step_count: int
    is_complete: bool
    total_checkpoint_count: int = 0
    current_checkpoint_index: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ParsedPlan:
    path: Path
    sections: tuple[CheckpointSection, ...]
    snapshot: PlanSnapshot


@dataclass(frozen=True)
class TolerantPlanLoadResult:
    parsed_plan: ParsedPlan
    parse_error: PlanParseError | None


@dataclass(frozen=True)
class GitTrackingMetadata:
    """Parsed Git Tracking section metadata."""
    plan_branch: str | None
    pre_handoff_base_head: str | None
    last_reviewed_head: str | None
    review_log_entries: tuple[str, ...]


class PlanParseError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        checkpoint_name: str | None = None,
        checkpoint_line: int | None = None,
        unchecked_step_count: int | None = None,
        checkpoint_index: int | None = None,
        total_checkpoint_count: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.checkpoint_name = checkpoint_name
        self.checkpoint_line = checkpoint_line
        self.unchecked_step_count = unchecked_step_count
        self.checkpoint_index = checkpoint_index
        self.total_checkpoint_count = total_checkpoint_count
        self.error_kind = error_kind


def _build_error(path: Path, message: str) -> PlanParseError:
    return PlanParseError(f"{path}: {message}")


def plan_has_git_tracking(text: str) -> bool:
    """Return True when the text contains a ## Git Tracking heading outside fenced blocks."""
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
        if in_fence:
            continue
        if GIT_TRACKING_RE.match(line):
            return True
    return False


def plan_step_checklist_is_complete(path: Path) -> bool:
    """Return whether a non-checkpoint plan has only checked live step items."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    states: list[str] = []
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
        if in_fence:
            continue
        step_match = STEP_RE.match(line)
        if step_match:
            states.append(step_match.group(1))

    return bool(states) and all(state.casefold() == "x" for state in states)


def _live_git_tracking_heading_line_numbers(text: str) -> tuple[int, ...]:
    line_numbers: list[int] = []
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
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
        if GIT_TRACKING_RE.match(line):
            line_numbers.append(line_number)
    return tuple(line_numbers)


def parse_git_tracking_metadata(text: str) -> GitTrackingMetadata | None:
    """Parse Git Tracking metadata from plan text, ignoring content inside fenced blocks.
    
    Returns None if no ## Git Tracking section is found outside fences.
    """
    heading_line_numbers = _live_git_tracking_heading_line_numbers(text)
    if not heading_line_numbers:
        return None
    if len(heading_line_numbers) > 1:
        raise ValueError("AFLOW_STOP: git tracking metadata is ambiguous across multiple live sections")

    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    lines_after_heading: list[str] = []
    found_heading = False

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
        
        if in_fence:
            continue

        if not found_heading:
            if GIT_TRACKING_RE.match(line):
                found_heading = True
            continue

        if NON_CHECKPOINT_HEADING_RE.match(line):
            break

        lines_after_heading.append(line)

    plan_branch: str | None = None
    pre_handoff_base_head: str | None = None
    last_reviewed_head: str | None = None
    review_log_entries: list[str] = []
    in_review_log = False

    for line in lines_after_heading:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith('- Plan Branch:'):
            match = re.search(r'`([^`]*)`', stripped)
            if match:
                plan_branch = match.group(1)
            continue

        if stripped.startswith('- Pre-Handoff Base HEAD:'):
            match = re.search(r'`([^`]*)`', stripped)
            if match:
                pre_handoff_base_head = match.group(1)
            continue

        if stripped.startswith('- Last Reviewed HEAD:'):
            match = re.search(r'`([^`]*)`', stripped)
            if match:
                last_reviewed_head = match.group(1)
            continue

        if stripped.startswith('- Review Log:'):
            in_review_log = True
            continue

        if in_review_log:
            if stripped.startswith('- '):
                review_log_entries.append(stripped[2:])
            elif stripped.startswith('  '):
                review_log_entries.append(stripped)
            else:
                break
    
    return GitTrackingMetadata(
        plan_branch=plan_branch,
        pre_handoff_base_head=pre_handoff_base_head,
        last_reviewed_head=last_reviewed_head,
        review_log_entries=tuple(review_log_entries),
    )


def is_handoff_pristine_for_base_refresh(metadata: GitTrackingMetadata, sections: tuple[CheckpointSection, ...]) -> bool:
    """Determine if a handoff is pristine enough for a startup base-HEAD refresh.
    
    A handoff is considered pristine if:
    - No checkpoint headings are checked
    - No checkpoint steps are checked
    - Last Reviewed HEAD is absent/empty or exactly 'none'
    - Review Log is absent/empty or contains only the single sentinel 'None yet.' entry
    """
    for section in sections:
        if section.heading_checked:
            return False
        if section.checked_step_count > 0:
            return False

    if metadata.last_reviewed_head not in (None, '', 'none'):
        return False

    if not metadata.review_log_entries:
        return True

    if len(metadata.review_log_entries) != 1:
        return False
    if metadata.review_log_entries[0].strip() != 'None yet.':
        return False

    return True


def is_plan_pristine_for_git_tracking_bootstrap(
    text: str,
    sections: tuple[CheckpointSection, ...],
) -> bool:
    """Return whether a section-less plan is safe for Git Tracking insertion."""
    if any(section.heading_checked or section.checked_step_count for section in sections):
        return False

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
        if in_fence:
            continue
        if GIT_TRACKING_RE.match(line) or GIT_TRACKING_FIELD_RE.match(line):
            return False

    return True


def insert_git_tracking_section(text: str, *, pre_handoff_base_head: str) -> str:
    """Insert the minimal Git Tracking section before the first live checkpoint."""
    if "`" in pre_handoff_base_head or "\r" in pre_handoff_base_head or "\n" in pre_handoff_base_head:
        raise ValueError("Pre-Handoff Base HEAD must be a single value without backticks")

    heading_line_numbers = _live_git_tracking_heading_line_numbers(text)
    if heading_line_numbers:
        raise ValueError("Git Tracking section already exists")

    has_crlf = "\r\n" in text
    has_bare_lf = "\n" in text.replace("\r\n", "")
    if has_crlf and has_bare_lf:
        raise ValueError("plan uses mixed newline conventions")
    newline = "\r\n" if has_crlf else "\n"

    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    insertion_offset: int | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
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
            offset += len(line)
            continue
        if not in_fence and SECTION_RE.match(line):
            insertion_offset = offset
            break
        offset += len(line)

    if insertion_offset is None:
        raise ValueError("plan has no live checkpoint for Git Tracking insertion")

    section = (
        f"## Git Tracking{newline}{newline}"
        f"- Plan Branch: ``{newline}"
        f"- Pre-Handoff Base HEAD: `{pre_handoff_base_head}`{newline}{newline}"
    )
    return text[:insertion_offset] + section + text[insertion_offset:]


def rewrite_git_tracking_field(text: str, field: str, new_value: str) -> str:
    """Rewrite only the specified Git Tracking field outside fenced blocks.
    
    Args:
        text: The full plan text
        field: The field to rewrite ('Pre-Handoff Base HEAD', 'Plan Branch', etc.)
        new_value: The new value (without backticks)
    
    Returns:
        The updated text with only the specified field modified
    """
    heading_line_numbers = _live_git_tracking_heading_line_numbers(text)
    if len(heading_line_numbers) > 1:
        raise ValueError("AFLOW_STOP: git tracking metadata is ambiguous across multiple live sections")
    if not heading_line_numbers:
        return text

    lines = text.splitlines(keepends=True)
    result_lines: list[str] = []
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    found_heading = False
    field_rewritten = False
    field_prefix = f'- {field}:'

    for line in lines:
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
            result_lines.append(line)
            continue
        
        if in_fence:
            result_lines.append(line)
            continue

        if not found_heading:
            if GIT_TRACKING_RE.match(line):
                found_heading = True
            result_lines.append(line)
            continue

        if NON_CHECKPOINT_HEADING_RE.match(line):
            result_lines.append(line)
            continue

        if not field_rewritten and line.strip().startswith(field_prefix):
            match = re.match(r'^(\s*-\s+' + re.escape(field) + r':\s+`)([^`]*)(`\r?\n?)$', line)
            if match:
                new_line = f"{match.group(1)}{new_value}{match.group(3)}"
                result_lines.append(new_line)
                field_rewritten = True
                continue
        
        result_lines.append(line)
    
    return ''.join(result_lines)


def _collect_sections(text: str, *, source_path: Path) -> tuple[CheckpointSection, ...]:
    sections: list[CheckpointSection] = []
    current_section: dict[str, object] | None = None
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    in_checkpoint_scope = False

    for line_number, line in enumerate(text.splitlines(), start=1):
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
            if current_section is not None:
                sections.append(
                    CheckpointSection(
                        line_number=int(current_section["line_number"]),
                        name=str(current_section["name"]),
                        heading_checked=bool(current_section["heading_checked"]),
                        unchecked_step_count=int(current_section["unchecked_step_count"]),
                        checked_step_count=int(current_section["checked_step_count"]),
                    )
                )
            current_section = {
                "line_number": line_number,
                "name": section_match.group(2).strip(),
                "heading_checked": section_match.group(1).lower() == "x",
                "unchecked_step_count": 0,
                "checked_step_count": 0,
            }
            in_checkpoint_scope = True
            continue

        if NON_CHECKPOINT_HEADING_RE.match(line):
            in_checkpoint_scope = False
            continue

        if current_section is None or not in_checkpoint_scope:
            continue

        step_match = STEP_RE.match(line)
        if not step_match:
            continue
        if step_match.group(1).lower() == " ":
            current_section["unchecked_step_count"] = int(current_section["unchecked_step_count"]) + 1
        else:
            current_section["checked_step_count"] = int(current_section["checked_step_count"]) + 1

    if current_section is not None:
        sections.append(
            CheckpointSection(
                line_number=int(current_section["line_number"]),
                name=str(current_section["name"]),
                heading_checked=bool(current_section["heading_checked"]),
                unchecked_step_count=int(current_section["unchecked_step_count"]),
                checked_step_count=int(current_section["checked_step_count"]),
            )
        )

    if not sections:
        raise _build_error(source_path, "no checkpoint sections were found")

    return tuple(sections)


def _validate_sections(sections: tuple[CheckpointSection, ...], *, source_path: Path) -> None:
    for i, section in enumerate(sections):
        if section.heading_checked and section.unchecked_step_count > 0:
            raise PlanParseError(
                f"{source_path}: inconsistent checkpoint state at line {section.line_number}: "
                f"'{section.name}' is marked complete but still has "
                f"{section.unchecked_step_count} unchecked step(s)",
                checkpoint_name=section.name,
                checkpoint_line=section.line_number,
                unchecked_step_count=section.unchecked_step_count,
                checkpoint_index=i + 1,
                total_checkpoint_count=len(sections),
                error_kind="inconsistent_checkpoint_state",
            )


def _build_snapshot_from_sections(sections: tuple[CheckpointSection, ...]) -> PlanSnapshot:
    unchecked_checkpoint_count = sum(not section.heading_checked for section in sections)
    total_checkpoint_count = len(sections)
    current_checkpoint = next((section for section in sections if not section.heading_checked), None)

    if current_checkpoint is None:
        return PlanSnapshot(
            current_checkpoint_name=None,
            unchecked_checkpoint_count=0,
            current_checkpoint_unchecked_step_count=0,
            is_complete=True,
            total_checkpoint_count=total_checkpoint_count,
            current_checkpoint_index=None,
        )

    current_checkpoint_index = sections.index(current_checkpoint) + 1
    return PlanSnapshot(
        current_checkpoint_name=current_checkpoint.name,
        unchecked_checkpoint_count=unchecked_checkpoint_count,
        current_checkpoint_unchecked_step_count=current_checkpoint.unchecked_step_count,
        is_complete=False,
        total_checkpoint_count=total_checkpoint_count,
        current_checkpoint_index=current_checkpoint_index,
    )


def _build_recovery_snapshot(error: PlanParseError) -> PlanSnapshot:
    if (
        error.checkpoint_name is None
        or error.checkpoint_index is None
        or error.unchecked_step_count is None
        or error.total_checkpoint_count is None
    ):
        raise ValueError("inconsistent checkpoint parse error is missing recovery metadata")

    return PlanSnapshot(
        current_checkpoint_name=error.checkpoint_name,
        unchecked_checkpoint_count=error.total_checkpoint_count - (error.checkpoint_index - 1),
        current_checkpoint_unchecked_step_count=error.unchecked_step_count,
        is_complete=False,
        total_checkpoint_count=error.total_checkpoint_count,
        current_checkpoint_index=error.checkpoint_index,
    )


def parse_plan_text(text: str, *, source_path: Path) -> ParsedPlan:
    sections = _collect_sections(text, source_path=source_path)
    _validate_sections(sections, source_path=source_path)
    snapshot = _build_snapshot_from_sections(sections)
    return ParsedPlan(path=source_path, sections=sections, snapshot=snapshot)


def load_plan(path: Path) -> ParsedPlan:
    if not path.is_file():
        raise _build_error(path, "plan file does not exist")
    return parse_plan_text(path.read_text(encoding="utf-8"), source_path=path)


def load_plan_tolerant(path: Path) -> TolerantPlanLoadResult:
    if not path.is_file():
        raise _build_error(path, "plan file does not exist")

    text = path.read_text(encoding="utf-8")
    sections = _collect_sections(text, source_path=path)
    try:
        _validate_sections(sections, source_path=path)
    except PlanParseError as exc:
        if exc.error_kind != "inconsistent_checkpoint_state":
            raise
        snapshot = _build_recovery_snapshot(exc)
        return TolerantPlanLoadResult(
            parsed_plan=ParsedPlan(path=path, sections=sections, snapshot=snapshot),
            parse_error=exc,
        )

    return TolerantPlanLoadResult(
        parsed_plan=ParsedPlan(path=path, sections=sections, snapshot=_build_snapshot_from_sections(sections)),
        parse_error=None,
    )

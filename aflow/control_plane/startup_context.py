"""Read-only, bounded plan facts for a run that has not executed yet."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat

from aflow.plan import (
    MISSING_CHECKPOINT_SECTIONS,
    PlanParseError,
    parse_plan_text,
    unchecked_step_texts,
)
from aflow.plan_backups import plan_identity_for_path

from .models import StartupCheckpoint, StartupContextSummary, utc_now


MAX_PLAN_BYTES = 256 * 1024
MAX_OUTLINE_ROWS = 100
MAX_PENDING_TASKS = 5
MAX_TEXT_BYTES = 256
MAX_CONTEXT_BYTES = 48 * 1024
MAX_PLAN_PATH_BYTES = 1024
_REASONS = {
    "invalid_repository": "The project directory is unavailable. Reopen the project and retry.",
    "unsafe_plan_path": "The plan path is outside the project or crosses a symbolic link. Select a project plan.",
    "missing_plan": "The plan file is missing. Open Plans to select an existing plan.",
    "unsafe_or_unreadable_plan": "The plan cannot be read safely. Check its path and permissions in Plans.",
    "plan_too_large": "The plan exceeds the 256 KiB preview limit. Shorten the plan before starting.",
    "invalid_plan_encoding": "The plan is not valid UTF-8. Correct its encoding before starting.",
    "non_checkpoint_plan": "This plan has no checkpoint headings. Open the plan to review its tasks.",
    "inconsistent_checkpoint_state": "A completed checkpoint has unchecked tasks. Correct the plan before starting.",
    "invalid_plan": "The plan cannot be parsed. Open it in Plans and correct its structure.",
    "plan_changed_during_read": "The plan changed during observation. Refresh before starting.",
    "pending_task_text_unavailable": "Task text does not match the checklist counts. Open the plan to inspect it.",
    "response_truncated": "The checkpoint outline was shortened. Open the plan for the full list.",
    "response_too_large": "The plan summary is too large to display. Open the plan directly.",
}


class _PlanObservationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def _plan_parts(repo_root: Path, plan_path: Path) -> tuple[str, ...]:
    if repo_root.is_symlink() or not repo_root.is_dir():
        raise _PlanObservationError("invalid_repository")
    try:
        requested = Path(plan_path)
        relative = requested.relative_to(repo_root) if requested.is_absolute() else requested
    except (TypeError, ValueError):
        raise _PlanObservationError("unsafe_plan_path") from None
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _PlanObservationError("unsafe_plan_path")
    if len(str(relative).encode("utf-8")) > MAX_PLAN_PATH_BYTES:
        raise _PlanObservationError("unsafe_plan_path")
    return parts


def _read_plan(repo_root: Path, parts: tuple[str, ...], *, body: bool) -> tuple[bytes, tuple[int, ...]]:
    """Walk beneath the root with no-follow descriptors, including every parent."""
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(repo_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in parts[:-1]:
            descriptors.append(
                os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1])
            )
        descriptors.append(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
        info = os.fstat(descriptors[-1])
        if not stat.S_ISREG(info.st_mode):
            raise _PlanObservationError("unsafe_plan_path")
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if info.st_size > MAX_PLAN_BYTES:
            raise _PlanObservationError("plan_too_large")
        if not body:
            return b"", signature
        chunks: list[bytes] = []
        remaining = MAX_PLAN_BYTES
        while remaining:
            chunk = os.read(descriptors[-1], min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptors[-1])
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != signature:
            raise _PlanObservationError("plan_changed_during_read")
        return raw, signature
    except FileNotFoundError:
        raise _PlanObservationError("missing_plan") from None
    except OSError:
        raise _PlanObservationError("unsafe_or_unreadable_plan") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _bounded_text(value: str) -> str:
    value = " ".join(value.split())
    if len(value.encode("utf-8")) <= MAX_TEXT_BYTES:
        return value
    shortened = value.encode("utf-8")[: MAX_TEXT_BYTES - len("…".encode("utf-8"))]
    return shortened.decode("utf-8", "ignore") + "…"


def _text_is_truncated(value: str) -> bool:
    return len(" ".join(value.split()).encode("utf-8")) > MAX_TEXT_BYTES


def _serialized_size(summary: StartupContextSummary) -> int:
    return len(json.dumps(summary.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def project_plan_startup_context(repo_root: Path, plan_path: Path) -> StartupContextSummary:
    """Observe one exact project plan without creating run or identity artifacts."""
    observed_at = utc_now()
    root = Path(repo_root)
    try:
        parts = _plan_parts(root, Path(plan_path))
    except (TypeError, ValueError, _PlanObservationError) as exc:
        code = exc.code if isinstance(exc, _PlanObservationError) else "unsafe_plan_path"
        return StartupContextSummary(observed_at=observed_at, reason_codes=(code,), reason=_REASONS[code])
    relative_path = Path(*parts).as_posix()
    try:
        raw, signature = _read_plan(root, parts, body=True)
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise _PlanObservationError("invalid_plan_encoding") from None
        try:
            parsed = parse_plan_text(text, source_path=root / relative_path)
        except PlanParseError as exc:
            code = (
                "non_checkpoint_plan"
                if exc.admission_kind == MISSING_CHECKPOINT_SECTIONS
                else exc.error_kind or "invalid_plan"
            )
            availability = "not_applicable" if code == "non_checkpoint_plan" else "unavailable"
            return StartupContextSummary(
                availability=availability,
                observed_at=observed_at,
                plan_path=relative_path,
                reason_codes=(code,),
                reason=_REASONS.get(code, _REASONS["invalid_plan"]),
            )
        next_index = parsed.snapshot.current_checkpoint_index
        next_section = parsed.sections[next_index - 1] if next_index is not None else None
        texts: tuple[str, ...] = ()
        text_count = 0
        if next_section is not None:
            texts, text_count = unchecked_step_texts(text, next_section, limit=MAX_PENDING_TASKS)
        reliable_text = next_section is None or text_count == next_section.unchecked_step_count
        reasons = () if reliable_text else ("pending_task_text_unavailable",)
        pending_tasks = tuple(_bounded_text(item) for item in texts) if reliable_text else ()

        def checkpoint(index: int) -> StartupCheckpoint:
            section = parsed.sections[index - 1]
            return StartupCheckpoint(
                ordinal=index,
                title=_bounded_text(section.name),
                heading_checked=section.heading_checked,
                checked_tasks=section.checked_step_count,
                total_tasks=section.checked_step_count + section.unchecked_step_count,
            )

        outline = tuple(checkpoint(index) for index in range(1, min(len(parsed.sections), MAX_OUTLINE_ROWS) + 1))
        text_truncated = any(
            _text_is_truncated(section.name) for section in parsed.sections[:len(outline)]
        ) or bool(next_section and _text_is_truncated(next_section.name)) or any(
            _text_is_truncated(item) for item in texts
        )
        identity = plan_identity_for_path(root, root / relative_path)
        try:
            current_signature = _read_plan(root, parts, body=False)[1]
        except _PlanObservationError:
            raise _PlanObservationError("plan_changed_during_read") from None
        if current_signature != signature:
            raise _PlanObservationError("plan_changed_during_read")
        summary = StartupContextSummary(
            availability="available" if reliable_text else "partial",
            reason_codes=reasons,
            reason=_REASONS[reasons[0]] if reasons else None,
            observed_at=observed_at,
            plan_path=relative_path,
            plan_identity=identity,
            plan_revision=hashlib.sha256(raw).hexdigest(),
            total_checkpoints=len(parsed.sections),
            recorded_complete_checkpoints=sum(section.heading_checked for section in parsed.sections),
            next_checkpoint=checkpoint(next_index) if next_index is not None else None,
            checkpoints=outline,
            pending_tasks=pending_tasks,
            checkpoint_outline_truncated=len(parsed.sections) > len(outline),
            pending_tasks_truncated=bool(next_section and next_section.unchecked_step_count > MAX_PENDING_TASKS),
            text_truncated=text_truncated,
        )
        while _serialized_size(summary) > MAX_CONTEXT_BYTES and summary.checkpoints:
            summary = replace(
                summary,
                availability="partial",
                reason_codes=tuple(dict.fromkeys((*summary.reason_codes, "response_truncated"))),
                reason=_REASONS["response_truncated"],
                checkpoints=summary.checkpoints[:-1],
                checkpoint_outline_truncated=True,
            )
        if _serialized_size(summary) > MAX_CONTEXT_BYTES:
            raise _PlanObservationError("response_too_large")
        return summary
    except _PlanObservationError as exc:
        return StartupContextSummary(
            observed_at=observed_at,
            plan_path=relative_path,
            reason_codes=(exc.code,),
            reason=_REASONS[exc.code],
        )

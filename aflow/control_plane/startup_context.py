"""Read-only, bounded plan facts for a run that has not executed yet."""

from __future__ import annotations

from dataclasses import replace
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import TYPE_CHECKING, Literal, Mapping

from aflow.plan import (
    MISSING_CHECKPOINT_SECTIONS,
    PlanParseError,
    parse_git_tracking_metadata,
    parse_plan_text_tolerant,
    unchecked_step_texts,
)
from aflow.plan_backups import (
    BackupProvenanceError,
    plan_identity_alias_owners,
    plan_identity_for_path,
    plan_identity_for_path_strict,
)
from aflow.git_status import classify_status_items_by_prefix, parse_porcelain_status
from aflow.project_admission import ProjectAdmission, ProjectAdmissionSafetyError
from aflow.project_settings import ProjectSettingsError

from .models import StartupCheckpoint, StartupContextSummary, StartupRelatedRun, utc_now, startup_failure
from .repository import RepositoryError, RunRepository
from .run_history import RunHistory
from .persistence import PersistenceError

if TYPE_CHECKING:
    from aflow.config import WorkflowConfig
    from aflow.daemon import DaemonService
    from .models import RunStatus


MAX_PLAN_BYTES = 256 * 1024
MAX_OUTLINE_ROWS = 100
MAX_PENDING_TASKS = 5
MAX_TEXT_BYTES = 256
MAX_CONTEXT_BYTES = 48 * 1024
MAX_PLAN_PATH_BYTES = 1024
MAX_RUN_IDENTITIES = 1_000
MAX_MATCHING_WORKTREES = 10
MAX_RELATED_RUNS = 5
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
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


def with_startup_selection(
    summary: StartupContextSummary,
    *,
    workflow_name: str | None,
    selected_step: str | None,
    step_source: Literal["workflow_default", "explicit", "resume"] | None,
) -> StartupContextSummary:
    """Attach one validated selection without exceeding the context budget."""
    selected = replace(
        summary,
        workflow_name=workflow_name,
        selected_step=selected_step,
        step_source=step_source,
    )
    if _serialized_size(selected) <= MAX_CONTEXT_BYTES:
        return selected
    reasons = tuple(dict.fromkeys((*selected.reason_codes, "response_truncated")))
    while _serialized_size(selected) > MAX_CONTEXT_BYTES and selected.checkpoints:
        selected = replace(
            selected, checkpoints=selected.checkpoints[:-1],
            checkpoint_outline_truncated=True, availability="partial",
            reason_codes=reasons,
        )
    while _serialized_size(selected) > MAX_CONTEXT_BYTES and selected.related_runs:
        selected = replace(
            selected, related_runs=selected.related_runs[:-1],
            related_runs_truncated=True, related_runs_complete=False,
            availability="partial", reason_codes=reasons,
            recommendation="inspect_previous_runs",
            recommendation_reason="The related-run summary was shortened. Inspect run history before starting.",
        )
    if _serialized_size(selected) > MAX_CONTEXT_BYTES:
        return replace(
            selected, availability="unavailable", recommendation="blocked",
            recommendation_reason="The startup summary exceeds its display limit.",
            checkpoints=(), related_runs=(), pending_tasks=(),
            reason_codes=tuple(dict.fromkeys((*reasons, "response_too_large"))),
        )
    return selected


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
            tolerant = parse_plan_text_tolerant(text, source_path=root / relative_path)
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
        # An inconsistent checkpoint state is a display warning, not a
        # prior-work safety fact. The preview carries the validated tolerant
        # recovery snapshot forward so the existing explicit recovery
        # question remains reachable; admission, daemon handoff, and worker
        # revalidation still perform the strict parse and the prior-work
        # scan below decides the recommendation.
        parsed = tolerant.parsed_plan
        next_index = parsed.snapshot.current_checkpoint_index
        next_section = parsed.sections[next_index - 1] if next_index is not None else None
        texts: tuple[str, ...] = ()
        text_count = 0
        if next_section is not None:
            texts, text_count = unchecked_step_texts(text, next_section, limit=MAX_PENDING_TASKS)
        reliable_text = next_section is None or text_count == next_section.unchecked_step_count
        inconsistent = tolerant.parse_error is not None
        reasons = (
            ("inconsistent_checkpoint_state",)
            if inconsistent
            else ()
        ) + (() if reliable_text else ("pending_task_text_unavailable",))
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
            availability=(
                "partial"
                if inconsistent or not reliable_text
                else "available"
            ),
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


@dataclass(frozen=True)
class _RelatedCandidate:
    repository: RunRepository
    status: RunStatus
    metadata: Mapping[str, object]
    history_state: str
    parent_id: str | None
    identity_uncertain: bool = False


def _git(root: Path, *args: str) -> tuple[int, bytes]:
    """Run only fixed Git operations at an already verified checkout."""
    try:
        result = subprocess.run(
            ("git", "-C", str(root), "--no-optional-locks", *args),
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, b""
    return result.returncode, result.stdout[:MAX_PLAN_BYTES]


def _commit(root: Path, ref: str) -> str | None:
    code, output = _git(root, "rev-parse", "--verify", ref)
    value = output.strip().decode("ascii", "ignore")
    return value if code == 0 and _SHA_RE.fullmatch(value) else None


def _ancestor(root: Path, ancestor: str, descendant: str) -> bool | None:
    if not _SHA_RE.fullmatch(ancestor) or not _SHA_RE.fullmatch(descendant):
        return None
    code, _ = _git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    return True if code == 0 else False if code == 1 else None


def _original_path(
    value: object, *, roots: tuple[Path, ...]
) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_PLAN_PATH_BYTES:
        return None
    path = Path(value)
    if path.is_absolute():
        for root in roots:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if relative.parts and ".." not in relative.parts:
                return relative.as_posix()
        return None
    if path.parts and ".." not in path.parts and path.parts[0] != ".":
        return path.as_posix()
    return None


def _candidate_match(
    metadata: Mapping[str, object], manifest: object, *,
    plan: StartupContextSummary, roots: tuple[Path, ...], primary_root: Path,
    tracking: object, base_ref: str | None,
) -> tuple[bool, bool]:
    """Return (related, identity uncertain); names never establish a match.

    Identity authority rule: an explicit valid run identity equal to the
    current plan identity is an exact match. An explicit conflicting (or
    malformed) identity is related but uncertain and can never become a
    certain match through path or Git Tracking. For ordinary run metadata
    without identity fields, the recorded path is a certain match only when
    its durable ownership is unique to the current plan identity, whether
    that path is equal to or different from the current plan path; exactly
    one durable owner different from the current plan identity makes the
    record unrelated. Shared aliases, missing ownership evidence, and paths
    outside the current identity's history are reported ambiguous (related
    but uncertain). Genuinely identity-free plans keep the exact-path plus
    Git Tracking fallback.
    """
    raw_ids = tuple(
        value for value in (metadata.get("original_plan_identity"), metadata.get("plan_identity"))
        if value is not None
    )
    invalid_identity = any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None
        for value in raw_ids
    ) or len(set(str(value) for value in raw_ids)) > 1
    explicit = raw_ids[0] if raw_ids and not invalid_identity else None
    if plan.plan_identity and explicit == plan.plan_identity:
        return True, False
    source = metadata.get("original_plan_path")
    if source is None:
        source = getattr(manifest, "plan_path", None)
    path = _original_path(source, roots=roots)
    if path is None:
        return False, False
    if plan.plan_identity:
        # Explicit conflicting or malformed identity fields block a certain
        # match before any provenance lookup can run.
        if explicit is not None or invalid_identity:
            return True, True
        # Ordinary records: the recorded path is a certain match only when
        # exactly one durable owner exists and it is the current identity;
        # exactly one different durable owner is unrelated. Path equality
        # and Git Tracking never resolve reused or shared ownership.
        recorded = Path(path)
        if not recorded.is_absolute():
            recorded = primary_root / recorded
        owners = plan_identity_alias_owners(primary_root, recorded)
        if owners is None or len(owners) != 1:
            return True, True
        if owners != {plan.plan_identity}:
            return False, False
        return True, False
    if path != plan.plan_path:
        return False, False
    if tracking is None or invalid_identity:
        return True, True
    branch = getattr(tracking, "plan_branch", None)
    base = getattr(tracking, "pre_handoff_base_head", None)
    candidate_branch = metadata.get("feature_branch")
    candidate_base = metadata.get("pre_handoff_base_head")
    if not isinstance(branch, str) or not branch or candidate_branch != branch:
        return True, True
    if not isinstance(base, str) or not _SHA_RE.fullmatch(base):
        return True, True
    if candidate_base is not None and candidate_base != base:
        return True, True
    if base_ref is None or _ancestor(primary_root, base, base_ref) is not True:
        return True, True
    return True, False


def _candidate_parent(
    metadata: Mapping[str, object], manifest: object, startup: Mapping[str, object]
) -> tuple[str | None, bool]:
    values = (
        metadata.get("resumed_from_run_id"),
        getattr(manifest, "restarted_from_run_id", None),
        startup.get("resumed_from_run_id"),
    )
    parents = {item for item in values if isinstance(item, str) and item}
    invalid = any(item is not None and not isinstance(item, str) for item in values)
    if invalid or len(parents) > 1:
        return None, True
    return (next(iter(parents)) if parents else None), False


def _read_publication_commit(candidate: _RelatedCandidate, starting_branch: str) -> tuple[str, str] | None:
    try:
        raw = candidate.repository.read_run_artifact(
            candidate.status.run_id, "publication.json", max_bytes=256 * 1024
        )
        receipt = json.loads(raw)
    except (OSError, ValueError, RepositoryError):
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != "published"
        or receipt.get("branch") != starting_branch
        or not isinstance(receipt.get("remote"), str)
        or not receipt["remote"]
    ):
        return None
    source = receipt.get("source_commit")
    commit = receipt.get("commit")
    if not isinstance(source, str) or not _SHA_RE.fullmatch(source):
        return None
    return (source, commit) if isinstance(commit, str) and _SHA_RE.fullmatch(commit) else None


def _preserved_work(
    candidate: _RelatedCandidate, *, roots: tuple[Path, ...],
    primary_root: Path, starting_branch: str, starting_commit: str | None,
) -> tuple[str | None, bool, str | None, bool, bool | None, bool | None]:
    metadata = candidate.metadata
    raw_worktree = metadata.get("worktree_path") or metadata.get("execution_repo_root")
    raw_branch = metadata.get("feature_branch")
    worktree = next(
        (root for root in roots if isinstance(raw_worktree, str) and str(root) == raw_worktree),
        None,
    )
    branch = raw_branch if isinstance(raw_branch, str) and len(raw_branch) <= 256 else None
    if worktree is None:
        if branch is None:
            return None, False, None, False, None, None
        code, _ = _git(primary_root, "check-ref-format", "--branch", branch)
        if code != 0:
            return None, False, branch, False, None, None
        branch_commit = _commit(primary_root, f"refs/heads/{branch}^{{commit}}")
        if branch_commit is None or starting_commit is None:
            return None, False, branch, False, None, None
        merged = _ancestor(primary_root, branch_commit, starting_commit)
        if merged is None:
            return None, False, branch, True, None, None
        if not merged:
            return None, False, branch, True, True, None
        receipt = _read_publication_commit(candidate, starting_branch)
        if (
            receipt
            and _ancestor(primary_root, receipt[0], receipt[1]) is True
            and _ancestor(primary_root, receipt[1], starting_commit) is True
        ):
            return None, False, branch, True, False, False
        return None, False, branch, True, False, None
    if branch is None:
        return str(worktree), True, None, False, None, None
    code, _ = _git(primary_root, "check-ref-format", "--branch", branch)
    if code != 0:
        return str(worktree), True, branch, False, None, None
    code, observed_branch = _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    if code != 0 or observed_branch.strip().decode("utf-8", "replace") != branch:
        return str(worktree), True, branch, False, None, None
    head = _commit(worktree, "HEAD^{commit}")
    branch_commit = _commit(primary_root, f"refs/heads/{branch}^{{commit}}")
    if head is None or branch_commit != head:
        return str(worktree), True, branch, False, None, None
    unmerged = None if starting_commit is None else (
        None if (merged := _ancestor(primary_root, head, starting_commit)) is None else not merged
    )
    code, output = _git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if code != 0 or len(output) == MAX_PLAN_BYTES:
        return str(worktree), True, branch, True, unmerged, None
    try:
        items = parse_porcelain_status(output)
    except (TypeError, ValueError):
        return str(worktree), True, branch, True, unmerged, None
    _, implementation_paths = classify_status_items_by_prefix(
        items, ignore_lifecycle_owned=True, ignore_untracked_lifecycle_backups=True
    )
    return str(worktree), True, branch, True, unmerged, bool(implementation_paths)


def project_startup_context(
    repo_root: Path, plan_path: Path, *, workflow: WorkflowConfig | None,
    admission: ProjectAdmission | None = None,
    daemon: DaemonService | None = None,
    pending_run_id: str | None = None,
    allow_unverified_starting_ref: bool = False,
) -> StartupContextSummary:
    """Add bounded related-run evidence to the plan observation; never reconcile.

    With ``allow_unverified_starting_ref`` the missing configured starting
    branch of a bootstrap-eligible (non-Git or unborn) checkout no longer
    short-circuits the related-run scan. The scan then runs with an unresolved
    starting commit, so every Git-dependent work classification stays unknown
    and any retained candidate keeps the result blocked; only a scan with no
    retained candidates at all can recommend starting.
    """
    summary = project_plan_startup_context(repo_root, plan_path)
    if summary.availability in {"unavailable", "not_applicable"}:
        return replace(summary, recommendation="blocked", recommendation_reason=summary.reason)
    if workflow is None:
        return replace(summary, recommendation="blocked", recommendation_reason="The current workflow is unavailable.")
    if tuple(workflow.setup or ()) != ("worktree", "branch"):
        return replace(summary, recommendation="start",
                       recommendation_reason="This workflow uses its existing checkout; normal startup checks still apply.")
    if not workflow.main_branch:
        return replace(summary, recommendation="blocked", recommendation_reason="A worktree starting branch is not configured.")
    try:
        owner = admission or ProjectAdmission(Path(repo_root), unit_manager=(daemon._application.units if daemon else None))
        primary_root = owner.primary_root
        roots = owner.project_roots()
        if Path(repo_root).resolve() not in roots or primary_root not in roots:
            raise ProjectAdmissionSafetyError("project identity differs from the selected checkout")
        if plan_identity_for_path_strict(Path(repo_root), Path(repo_root) / summary.plan_path) != summary.plan_identity:
            raise _PlanObservationError("plan_changed_during_read")
        repositories = {root: RunRepository(root) for root in roots}
        current_raw, _ = _read_plan(Path(repo_root), _plan_parts(Path(repo_root), Path(plan_path)), body=True)
        if hashlib.sha256(current_raw).hexdigest() != summary.plan_revision:
            raise _PlanObservationError("plan_changed_during_read")
        tracking = parse_git_tracking_metadata(current_raw.decode("utf-8"))
    except (ProjectAdmissionSafetyError, ProjectSettingsError, BackupProvenanceError, PersistenceError, OSError, ValueError, _PlanObservationError):
        return replace(summary, availability="partial", related_runs_complete=False,
                       recommendation="blocked", recommendation_reason="Project or plan evidence changed; refresh before starting.",
                       reason_codes=(*summary.reason_codes, "related_evidence_unavailable"))
    code, _ = _git(primary_root, "check-ref-format", "--branch", workflow.main_branch)
    starting_commit = _commit(primary_root, f"refs/heads/{workflow.main_branch}^{{commit}}") if code == 0 else None
    if starting_commit is None and not allow_unverified_starting_ref:
        return replace(summary, availability="partial", related_runs_complete=False,
                       recommendation="blocked", recommendation_reason="The configured starting branch cannot be verified.",
                       reason_codes=(*summary.reason_codes, "starting_ref_unavailable"))
    base_ref = _commit(primary_root, f"refs/heads/{getattr(tracking, 'plan_branch', '')}^{{commit}}") if tracking else None
    candidates: dict[str, _RelatedCandidate] = {}
    complete = True
    uncertain_lineage = False
    seen = 0
    for root, repository in repositories.items():
        if seen >= MAX_RUN_IDENTITIES:
            complete = False
            break
        try:
            page = repository.list_runs(limit=MAX_RUN_IDENTITIES - seen, include_progress=False)
            seen += len(page.runs)
            if page.next_cursor is not None:
                complete = False
            history = RunHistory(repository)
            for status in page.runs:
                if status.run_id == pending_run_id:
                    continue
                manifest = repository.get_launch_manifest(status.run_id) if status.ownership == "control_plane" else None
                if not status.evidence.get("has_run_metadata") and manifest is not None:
                    continue
                metadata = repository.read_run_metadata(status.run_id)
                if not metadata:
                    continue
                recorded_root = getattr(manifest, "project_root", None) if manifest else metadata.get("repo_root")
                if isinstance(recorded_root, str) and recorded_root not in {str(item) for item in roots}:
                    continue
                related, uncertain = _candidate_match(
                    metadata, manifest, plan=summary, roots=roots,
                    primary_root=primary_root, tracking=tracking, base_ref=base_ref,
                )
                if not related:
                    continue
                state = history.read(status.run_id)["state"]
                parent, invalid_parent = _candidate_parent(metadata, manifest, repository._startup_record(status.run_id) if manifest else {})
                uncertain_lineage |= invalid_parent
                candidate = _RelatedCandidate(repository, status, metadata, state, parent, uncertain)
                if status.run_id in candidates:
                    uncertain_lineage = True
                candidates[status.run_id] = candidate
        except (PersistenceError, ProjectAdmissionSafetyError, OSError, ValueError, TypeError):
            complete = False
            break
    children: set[str] = set()
    for candidate in candidates.values():
        if candidate.parent_id is None:
            continue
        parent = candidates.get(candidate.parent_id)
        if parent is None:
            uncertain_lineage = True
            continue
        if (
            parent.metadata.get("feature_branch") != candidate.metadata.get("feature_branch")
            or parent.metadata.get("worktree_path") != candidate.metadata.get("worktree_path")
        ):
            uncertain_lineage = True
            continue
        try:
            parent_activity, _ = owner.classify_run_for_preview(parent.status)
        except ProjectAdmissionSafetyError:
            parent_activity = "uncertain"
        if parent_activity != "inactive":
            uncertain_lineage = True
            continue
        children.add(parent.status.run_id)
    leaves = sorted(
        (item for item in candidates.values() if item.status.run_id not in children),
        key=lambda item: item.status.run_id,
    )
    if not leaves and candidates:
        uncertain_lineage = True
        leaves = sorted(candidates.values(), key=lambda item: item.status.run_id)
    related_truncated = len(leaves) > MAX_RELATED_RUNS
    selected = leaves[:MAX_RELATED_RUNS]
    entries: list[StartupRelatedRun] = []
    has_active = False
    has_unknown_activity = False
    has_preserved = False
    has_unknown_work = False
    inspected_worktrees = 0
    for candidate in selected:
        status = candidate.status
        if daemon is not None and candidate.repository.repo_root == daemon._application.repository.repo_root:
            try:
                status = daemon.run_status(status.run_id, include_progress=False, include_resume_preview=False)
            except Exception:
                has_unknown_activity = True
        try:
            activity, _ = owner.classify_run_for_preview(status)
        except ProjectAdmissionSafetyError:
            activity = "uncertain"
        has_active |= activity == "active"
        has_unknown_activity |= activity not in {"active", "inactive"}
        worktree_path = None
        worktree_verified = False
        branch = None
        branch_verified = False
        unmerged = None
        uncommitted = None
        if activity == "inactive":
            if inspected_worktrees >= MAX_MATCHING_WORKTREES:
                complete = False
            else:
                inspected_worktrees += 1
                worktree_path, worktree_verified, branch, branch_verified, unmerged, uncommitted = _preserved_work(
                    candidate, roots=roots, primary_root=primary_root,
                    starting_branch=workflow.main_branch, starting_commit=starting_commit
                )
                has_preserved |= unmerged is True or uncommitted is True
                has_unknown_work |= unmerged is None or uncommitted is None
        reason = status.reason
        failure = status.evidence.get("startup_failure")
        if isinstance(failure, Mapping) and isinstance(failure.get("message"), str):
            reason = failure["message"]
        bounded_reason = startup_failure("previous", reason)["message"][:512] if isinstance(reason, str) else None
        can_resume = None
        if daemon is not None and activity == "inactive" and len(selected) == 1:
            can_resume = daemon._can_resume(status)
        entries.append(StartupRelatedRun(
            run_id=status.run_id, status=status.status,
            step=_bounded_text(status.current_step) if isinstance(status.current_step, str) else None,
            activity="active" if activity == "active" else "inactive" if activity == "inactive" else "unknown",
            history_state="archived" if candidate.history_state == "archived" else "visible",
            failure_reason=bounded_reason,
            worktree_path=worktree_path, worktree_verified=worktree_verified,
            branch=branch, branch_verified=branch_verified,
            unmerged_work=unmerged, uncommitted_work=uncommitted, can_resume=can_resume,
        ))
    deleted = any(candidate.history_state == "deleted" for candidate in candidates.values())
    entries = [entry for entry in entries if candidates[entry.run_id].history_state != "deleted"]
    uncertain_identity = any(candidate.identity_uncertain for candidate in candidates.values())
    if has_active and not (uncertain_identity or related_truncated):
        recommendation, reason = "open_existing_run", "This plan is already running. Open its current run."
    elif has_unknown_activity:
        recommendation, reason = "blocked", "Current run activity could not be verified. Inspect the related runs."
    elif not complete or deleted or uncertain_lineage or uncertain_identity or related_truncated:
        recommendation, reason = "inspect_previous_runs", "Earlier run evidence is incomplete or ambiguous. Inspect the related runs."
    elif len(leaves) > 1:
        recommendation, reason = "inspect_previous_runs", "Several independent runs match this plan. Inspect each before starting."
    elif has_preserved:
        recommendation, reason = "review_previous_run", "Previous work needs recovery. Review the recorded run and preserved work."
    elif has_unknown_work:
        recommendation, reason = "inspect_previous_runs", "Previous worktree, branch, or Git evidence could not be verified. Inspect the earlier run."
    else:
        recommendation, reason = "start", "No unresolved earlier work was found in the complete scan."
    result = replace(
        summary, recommendation=recommendation, recommendation_reason=reason[:512],
        related_runs=tuple(entries), related_runs_complete=complete and not uncertain_lineage and not uncertain_identity,
        related_runs_truncated=related_truncated,
        availability="partial" if not complete or deleted or uncertain_lineage or uncertain_identity or has_unknown_activity or has_unknown_work else summary.availability,
        reason_codes=(
            *summary.reason_codes,
            *(("related_evidence_incomplete",) if not complete else ()),
            *(("prior_work_unverified",) if has_unknown_work else ()),
        ),
    )
    while _serialized_size(result) > MAX_CONTEXT_BYTES and result.checkpoints:
        result = replace(result, checkpoints=result.checkpoints[:-1], checkpoint_outline_truncated=True,
                         availability="partial", reason_codes=tuple(dict.fromkeys((*result.reason_codes, "response_truncated"))))
    while _serialized_size(result) > MAX_CONTEXT_BYTES and result.related_runs:
        result = replace(result, related_runs=result.related_runs[:-1], related_runs_truncated=True,
                         related_runs_complete=False, availability="partial",
                         recommendation="inspect_previous_runs",
                         recommendation_reason="The related-run summary was shortened. Inspect run history before starting.",
                         reason_codes=tuple(dict.fromkeys((*result.reason_codes, "response_truncated"))))
    if _serialized_size(result) > MAX_CONTEXT_BYTES:
        return replace(summary, availability="unavailable", related_runs_complete=False,
                       recommendation="blocked", recommendation_reason="The startup summary exceeds its display limit.",
                       reason_codes=(*summary.reason_codes, "response_too_large"))
    return result

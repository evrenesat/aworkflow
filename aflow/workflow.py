from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

if TYPE_CHECKING:
    from aflow.api.events import ExecutionEvent, ExecutionObserver

from .config import (
    AflowSection,
    GoTransition,
    VALID_CONDITION_SYMBOLS,
    WorkflowConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from .manager import (
    ManagerDecisionError,
    ManagerDecisionV1,
    build_manager_prompts,
    build_repartition_prompts,
    eligible_implementation_upgrade,
    parse_manager_decision,
    render_manager_stop_report,
    resolve_manager_role,
    validate_manager_decision,
    validate_manager_note_authority,
)
from .manager_context import (
    build_manager_context,
    build_manager_note_scope,
    summarize_repair_plan,
    summarize_review_rejection,
)
from .git_status import classify_dirtiness_by_prefix, RepoState, probe_repo_state
from .harnesses import get_adapter
from .harnesses.base import HarnessAdapter, HarnessInvocation
from .plan import (
    ParsedPlan,
    PlanParseError,
    PlanSnapshot,
    is_handoff_pristine_for_base_refresh,
    load_plan,
    load_plan_tolerant,
    parse_git_tracking_metadata,
    plan_has_git_tracking,
    plan_step_checklist_is_complete,
    rewrite_git_tracking_field,
)
from .recovery import (
    build_recovery_evidence,
    build_recovery_context,
    build_team_lead_recovery_prompt,
    find_first_matching_rule,
    recovery_made_progress,
    parse_team_lead_recovery_decision,
    resolve_backup_team,
    TeamLeadRecoveryDecisionError,
)
from .run_state import ActiveImplementationScope, CheckpointRepartitionRecord, ControllerConfig, ControllerRunResult, ControllerState, ExecutionContext, FinalizedTurnBoundary, FrozenRunIdentity, HarnessRecoveryAction, HarnessRecoveryContext, ImplementationAttempt, IssueRecord, ManagerDecisionSummary, OverrideResult, PendingBoundaryDecision, PendingManagerNotes, PendingRepartitionV1, PendingTeamOverride, RetryContext, ResumeContext, ReviewRejectionRecord, TurnRecord, WorkflowEndReason, format_harness_model_display, load_override_request
from .runlog import create_repartition_attempt_paths, create_run_paths, finalize_turn_artifacts, prune_old_runs, write_issue_summary, write_manager_artifacts, write_repartition_artifact, write_run_metadata, write_turn_artifacts_start
from .stop_marker import detect_stop_marker
from .scope_pressure import parse_scope_pressure
from .status import BannerRenderer, WorkflowGraphSource
from aflow.api.events import (
    CheckpointRepartitionedEvent,
    ManagerDecidedEvent,
    ManagerStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    StatusChangedEvent,
    TurnFinishedEvent,
    TurnStartedEvent,
)


PROCESS_POLL_INTERVAL_SECONDS = 0.05
BANNER_REFRESH_INTERVAL_SECONDS = 1.0

_REVIEW_SKILL_NAMES = frozenset({
    "aflow-review-squash",
    "aflow-review-checkpoint",
    "aflow-review-final",
})
_PLAN_BRANCH_LINE_RE = re.compile(r"^(\s*-\s+Plan Branch:\s+`)([^`]*)(`.*)$", re.MULTILINE)


def _freeze_run_identity(
    workflow_name: str,
    workflow_config: WorkflowUserConfig,
    *,
    config_dir: Path,
) -> FrozenRunIdentity:
    """Fingerprint the resolved in-memory inputs used to execute one workflow."""
    selected = {
        "workflow_name": workflow_name,
        "workflow": asdict(workflow_config.workflows[workflow_name]),
        "roles": workflow_config.roles,
        "teams": {
            name: asdict(team)
            for name, team in workflow_config.teams.items()
        },
        "harnesses": {
            name: asdict(harness)
            for name, harness in workflow_config.harnesses.items()
        },
        "manager": asdict(workflow_config.manager),
        "error_handling": asdict(workflow_config.error_handling),
    }
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    return FrozenRunIdentity(
        workflow_name=workflow_name,
        config_path=str(config_dir.resolve()),
        config_fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _target_plan_identity(plan_path: Path, snapshot: PlanSnapshot | None = None) -> str:
    """Identify the exact active-plan checkpoint targeted by one-hop state."""
    resolved_snapshot = snapshot
    if resolved_snapshot is None:
        try:
            resolved_snapshot = load_plan_tolerant(plan_path).parsed_plan.snapshot
        except (OSError, PlanParseError, ValueError):
            resolved_snapshot = None
    index = resolved_snapshot.current_checkpoint_index if resolved_snapshot is not None else None
    suffix = f"checkpoint-{index}" if index is not None else "checkpoint-complete"
    return f"{plan_path}::{suffix}"


def _normalized_checkpoint_name(name: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (name or "unnamed").casefold()).strip("-")
    return normalized or "unnamed"


def _open_implementation_scope(
    state: ControllerState,
    *,
    original_plan_path: Path,
    original_snapshot: PlanSnapshot,
    turn_number: int,
) -> tuple[ActiveImplementationScope, bool]:
    """Open once for an original checkpoint and report whether it is new."""
    if state.active_implementation_scope is not None:
        return state.active_implementation_scope, False
    index = original_snapshot.current_checkpoint_index
    name = original_snapshot.current_checkpoint_name
    scope = ActiveImplementationScope(
        scope_id=(
            f"{original_plan_path}::checkpoint-{index}::"
            f"{_normalized_checkpoint_name(name)}"
        ),
        original_plan_path=str(original_plan_path),
        checkpoint_index=index,
        checkpoint_name=name,
        opened_turn_number=turn_number,
    )
    state.active_implementation_scope = scope
    return scope, True


def _original_checkpoint_advanced(
    scope: ActiveImplementationScope,
    snapshot: PlanSnapshot,
) -> bool:
    if snapshot.is_complete:
        return True
    if scope.checkpoint_index is None or snapshot.current_checkpoint_index is None:
        return False
    return snapshot.current_checkpoint_index > scope.checkpoint_index

def _resume_completed_worker_can_use_original_plan(
    *,
    pending_turn: PendingFinalizedTurn | None,
    active_plan_path: Path,
    original_plan_path: Path,
    active_scope: ActiveImplementationScope | None,
    exec_ctx: ExecutionContext | None,
) -> bool:
    """Recognize a completed repair whose worker removed its finished overlay."""
    if (
        pending_turn is None
        or pending_turn.step_role != "worker"
        or pending_turn.active_plan_path != active_plan_path
        or active_plan_path == original_plan_path
        or active_scope is None
        or active_scope.awaiting_review
        or pending_turn.conditions.get("DONE", False)
        or pending_turn.conditions.get("NEW_PLAN_EXISTS", False)
        or pending_turn.conditions.get("MAX_TURNS_REACHED", False)
        or pending_turn.chosen_transition == "END"
        or not _original_checkpoint_advanced(
            active_scope,
            pending_turn.snapshot_after,
        )
    ):
        return False
    return _exec_plan_path(original_plan_path, exec_ctx).is_file()


def _capture_scope_envelope(
    state: ControllerState,
    *,
    plan_text: str | None,
    primary_plan_path: Path,
    run_dir: Path,
    exec_ctx: ExecutionContext | None,
    repo_root: Path | None = None,
) -> None:
    """Capture and persist an immutable scope envelope at new-scope opening.

    Must be called after ``_open_implementation_scope`` but before the first
    worker harness invocation.  Idempotent: returns immediately when the
    active scope already has an envelope artifact.

    Raises ``WorkflowError`` when primary and execution plan copies disagree.
    """
    scope = state.active_implementation_scope
    if scope is None:
        return
    reference_values = (
        scope.envelope_artifact_path,
        scope.envelope_artifact_sha256,
        scope.envelope_canonical_sha256,
    )
    if any(value is not None for value in reference_values):
        if scope.has_envelope:
            # Envelope already captured — reuse across repair overlays,
            # retries, one-edge upgrades, reviewer turns, and manager boundaries;
            # a complete modern reference still fails closed if its immutable
            # artifact was removed or altered.
            load_scope_envelope_for_resume(run_dir, scope)
            return
        raise WorkflowError("cannot capture scope envelope: partial envelope reference")

    # Require primary and execution original-plan copies to agree when both exist.
    _validate_plan_copies_agree(
        primary_plan_path=primary_plan_path,
        exec_ctx=exec_ctx,
    )

    try:
        primary_bytes = primary_plan_path.read_bytes()
        decoded_plan_text = primary_bytes.decode("utf-8", "strict")
        if plan_text is not None and decoded_plan_text != plan_text:
            raise ValueError("provided plan text does not match primary plan bytes")
        resolved_repo_root = (repo_root or primary_plan_path.parent).resolve()
        from .repartition import (
            create_envelope,
            parse_envelope_bytes,
            write_envelope_atomic,
        )

        checkpoint_index = scope.checkpoint_index if scope.checkpoint_index is not None else 0
        envelope = create_envelope(
            scope_id=scope.scope_id,
            original_plan_path=primary_plan_path,
            plan_text=decoded_plan_text,
            checkpoint_index=checkpoint_index,
            repo_root=resolved_repo_root,
        )
        envelope_path = write_envelope_atomic(
            envelope,
            run_dir / "scopes" / envelope.scope_digest,
        )
        artifact_bytes = envelope_path.read_bytes()
        parsed = parse_envelope_bytes(artifact_bytes)
        reference = replace(
            scope,
            envelope_artifact_path=(
                envelope_path.resolve().relative_to(run_dir.resolve()).as_posix()
            ),
            envelope_artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
            envelope_canonical_sha256=parsed.canonical_envelope_sha256,
        )
        _validate_scope_envelope_bytes(reference, artifact_bytes)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise WorkflowError(f"cannot capture scope envelope: {exc}") from exc

    state.active_implementation_scope = reference


def _scope_envelope_reference(
    scope: ActiveImplementationScope,
) -> tuple[str, str, str] | None:
    """Return a complete, layout-bound modern reference or fail closed."""
    values = (
        scope.envelope_artifact_path,
        scope.envelope_artifact_sha256,
        scope.envelope_canonical_sha256,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise WorkflowError("invalid scope envelope reference: partial reference")
    if not all(isinstance(value, str) and value for value in values):
        raise WorkflowError("invalid scope envelope reference: fields must be nonempty strings")
    artifact_path, artifact_sha256, canonical_sha256 = values
    if not isinstance(scope.scope_id, str) or not scope.scope_id:
        raise WorkflowError("invalid scope envelope reference: scope_id is invalid")
    expected_digest = hashlib.sha256(scope.scope_id.encode("utf-8")).hexdigest()
    expected_path = f"scopes/{expected_digest}/envelope.json"
    if artifact_path != expected_path:
        raise WorkflowError(
            "invalid scope envelope reference: artifact path must be "
            f"{expected_path}"
        )
    for field_name, value in (
        ("artifact SHA-256", artifact_sha256),
        ("canonical SHA-256", canonical_sha256),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise WorkflowError(
                f"invalid scope envelope reference: {field_name} is invalid"
            )
    return artifact_path, artifact_sha256, canonical_sha256


def _validate_scope_envelope_bytes(
    scope: ActiveImplementationScope,
    artifact_bytes: bytes,
) -> None:
    """Bind exact artifact bytes and parsed envelope authority to one scope."""
    reference = _scope_envelope_reference(scope)
    if reference is None:
        raise WorkflowError("invalid scope envelope reference: legacy scope has no artifact")
    _artifact_path, artifact_sha256, canonical_sha256 = reference
    actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_sha256 != artifact_sha256:
        raise WorkflowError("invalid scope envelope reference: artifact bytes hash mismatch")
    try:
        from .repartition import parse_envelope_bytes

        envelope = parse_envelope_bytes(artifact_bytes)
    except ValueError as exc:
        raise WorkflowError(f"invalid scope envelope reference: corrupt envelope: {exc}") from exc
    if envelope.scope_id != scope.scope_id:
        raise WorkflowError("invalid scope envelope reference: scope_id mismatch")
    if envelope.checkpoint_index != scope.checkpoint_index:
        raise WorkflowError("invalid scope envelope reference: checkpoint index mismatch")
    if envelope.checkpoint_name != scope.checkpoint_name:
        raise WorkflowError("invalid scope envelope reference: checkpoint name mismatch")
    expected_digest = hashlib.sha256(scope.scope_id.encode("utf-8")).hexdigest()
    if envelope.scope_digest != expected_digest:
        raise WorkflowError("invalid scope envelope reference: scope digest mismatch")
    if envelope.canonical_envelope_sha256 != canonical_sha256:
        raise WorkflowError("invalid scope envelope reference: canonical hash mismatch")


def load_scope_envelope_for_resume(
    source_run_dir: Path,
    scope: ActiveImplementationScope,
) -> bytes | None:
    """Read and bind a source artifact before resume pruning can remove it."""
    reference = _scope_envelope_reference(scope)
    if reference is None:
        return None
    artifact_path, _artifact_sha256, _canonical_sha256 = reference
    source_root = source_run_dir.resolve()
    candidate = source_run_dir / artifact_path
    if not candidate.exists():
        raise WorkflowError("cannot resume: scope envelope artifact is missing from source run")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(source_root)
    except (OSError, ValueError) as exc:
        raise WorkflowError(
            "cannot resume: scope envelope artifact escapes source run"
        ) from exc
    if not resolved.is_file():
        raise WorkflowError("cannot resume: scope envelope artifact is missing from source run")
    try:
        artifact_bytes = resolved.read_bytes()
    except OSError as exc:
        raise WorkflowError(f"cannot resume: cannot read scope envelope artifact: {exc}") from exc
    _validate_scope_envelope_bytes(scope, artifact_bytes)
    return artifact_bytes


def _validate_existing_scope_envelope(
    run_dir: Path,
    scope: ActiveImplementationScope,
) -> None:
    """Fail closed for a modern active scope while retaining legacy scopes."""
    if _scope_envelope_reference(scope) is not None:
        load_scope_envelope_for_resume(run_dir, scope)


def _require_valid_pressure_scope(
    run_dir: Path,
    scope: ActiveImplementationScope | None,
) -> None:
    """Require immutable scope authority before pressure may reach a manager.

    ``AFLOW_SCOPE_PRESSURE`` is meaningful only for an actively supervised
    original-checkpoint scope.  Reuse the resume validator here so a pressure
    boundary gets the same path, hash, parser, and scope-identity checks as
    capture and resume instead of a weaker parallel interpretation.
    """
    if scope is None:
        raise WorkflowError(
            "AFLOW_SCOPE_PRESSURE requires an active implementation scope with "
            "a validated immutable envelope; no active implementation scope is open"
        )
    try:
        reference = _scope_envelope_reference(scope)
        if reference is None:
            raise WorkflowError(
                "invalid scope envelope reference: legacy scope has no artifact"
            )
        load_scope_envelope_for_resume(run_dir, scope)
    except WorkflowError as exc:
        raise WorkflowError(
            "AFLOW_SCOPE_PRESSURE requires an active implementation scope with "
            f"a validated immutable envelope: {exc.summary}"
        ) from exc


def _validate_plan_copies_agree(
    *,
    primary_plan_path: Path,
    exec_ctx: ExecutionContext | None,
) -> None:
    """Ensure primary and execution original-plan copies are byte-identical.

    When a worktree is active and both copies exist, any divergence prevents
    envelope capture.  The primary copy is always authoritative.
    """
    if exec_ctx is None or exec_ctx.worktree_path is None:
        return
    execution_path = _exec_plan_path(primary_plan_path, exec_ctx)
    if not execution_path.is_file():
        return
    if not primary_plan_path.is_file():
        raise WorkflowError(
            "cannot capture scope envelope: primary plan is missing while "
            f"execution copy exists at {execution_path}"
        )
    primary_bytes = primary_plan_path.read_bytes()
    execution_bytes = execution_path.read_bytes()
    if primary_bytes != execution_bytes:
        raise WorkflowError(
            "primary and execution original-plan copies must be identical "
            "before scope-envelope capture: "
            f"primary={primary_plan_path} "
            f"execution={execution_path}"
        )


def _close_implementation_scope(state: ControllerState) -> None:
    """Close routing state while retaining historical attempt records."""
    state.active_implementation_scope = None
    state.pending_manager_notes = None
    state.pending_step_team_override = None
    state.pending_boundary_decision = None
    state.reviewer_rejection_count = 0


def _pending_matches_scope_and_plan(
    pending: object,
    state: ControllerState,
    target_plan_identity: str,
) -> bool:
    pending_scope_id = getattr(pending, "scope_id", None)
    active_scope_id = (
        state.active_implementation_scope.scope_id
        if state.active_implementation_scope is not None else None
    )
    pending_target = (
        getattr(pending, "target_plan_identity", None)
        or getattr(pending, "checkpoint_identity", None)
    )
    pending_partition_identity = (
        getattr(pending, "repartition_generation_id", None),
        getattr(pending, "repartition_candidate_sha256", None),
        getattr(pending, "repartition_partition_id", None),
    )
    scope = state.active_implementation_scope
    active_partition_identity = (
        (
            scope.current_partition_generation_id,
            scope.current_partition_candidate_sha256,
            scope.current_partition_id,
        )
        if scope is not None
        else (None, None, None)
    )
    partition_matches = not any(
        value is not None
        for value in (*active_partition_identity, *pending_partition_identity)
    ) or (
        all(value is not None for value in pending_partition_identity)
        and pending_partition_identity == active_partition_identity
    )
    return (
        (pending_scope_id is None or pending_scope_id == active_scope_id)
        and (pending_target is None or pending_target == target_plan_identity)
        and partition_matches
    )


def _mutable_implementation_attempts(
    attempts: Mapping[str, tuple[ImplementationAttempt, ...] | list[ImplementationAttempt]],
) -> dict[str, list[ImplementationAttempt]]:
    """Normalize durable resume histories before any live append."""
    return {key: list(history) for key, history in attempts.items()}


def _implementation_upgrade_depth(
    config: WorkflowUserConfig,
    *,
    baseline_team: str | None,
    most_recent_team: str | None,
) -> int | None:
    """Count configured upgrade edges, independent of retry attempt count."""
    if baseline_team is None or most_recent_team is None:
        return 0 if baseline_team == most_recent_team else None
    current = baseline_team
    depth = 0
    seen: set[str] = set()
    while current != most_recent_team:
        if current in seen:
            return None
        seen.add(current)
        team = config.teams.get(current)
        if team is None or team.upgrade_to is None:
            return None
        current = team.upgrade_to
        depth += 1
    return depth


class StartupBaseHeadRefreshStatus(str, Enum):
    NO_GIT_TRACKING = "no_git_tracking"
    NO_RESOLVABLE_HEAD = "no_resolvable_head"
    MATCH = "match"
    MALFORMED = "malformed"
    EMPTY_BASE_STARTED = "empty_base_started"
    EMPTY_BASE_PRISTINE = "empty_base_pristine"
    MISMATCH_STARTED = "mismatch_started"
    MISMATCH_PRISTINE = "mismatch_pristine"


@dataclass(frozen=True)
class StartupBaseHeadRefreshResult:
    status: StartupBaseHeadRefreshStatus
    current_head: str | None = None
    recorded_base_head: str | None = None
    is_pristine: bool | None = None


class WorkflowError(RuntimeError):
    def __init__(self, summary: str, *, run_dir: Path | None = None) -> None:
        super().__init__(summary)
        self.summary = summary
        self.run_dir = run_dir


@dataclass(frozen=True)
class ResolvedProfile:
    harness_name: str
    profile_name: str
    model: str | None
    effort: str | None


@dataclass(frozen=True)
class _PreparedPrimaryPlanForMerge:
    plan_path: Path
    original_text: str | None


def _turn_artifact_display_path(
    repo_root: Path,
    turn_dir: Path,
    filename: str,
    *,
    content: str | None = None,
) -> str | None:
    artifact_path = turn_dir / filename
    if content is not None:
        return str(artifact_path.relative_to(repo_root)) if content.strip() else None
    if not artifact_path.is_file() or not artifact_path.read_text(encoding="utf-8").strip():
        return None
    return str(artifact_path.relative_to(repo_root))


def resolve_profile(
    selector: str,
    config: WorkflowUserConfig,
    *, step_path: str,
) -> ResolvedProfile:
    return _resolve_selector(selector, config, step_path=step_path)


def _resolve_selector(
    selector: str,
    config: WorkflowUserConfig,
    *,
    step_path: str,
) -> ResolvedProfile:
    if "." not in selector:
        raise WorkflowError(
            f"step profile must be fully qualified (harness.profile) "
            f"in {step_path}, got '{selector}'"
        )
    harness_name, _, profile_name = selector.partition(".")
    if not harness_name or not profile_name:
        raise WorkflowError(
            f"invalid profile selector '{selector}' in {step_path}"
        )
    harness_config = config.harnesses.get(harness_name)
    if harness_config is None:
        raise WorkflowError(
            f"workflow step references unknown harness '{harness_name}' "
            f"in {step_path}"
        )
    profile_config = harness_config.profiles.get(profile_name)
    if profile_config is None:
        raise WorkflowError(
            f"workflow step references unknown profile '{profile_name}' "
            f"for harness '{harness_name}' in {step_path}"
        )
    return ResolvedProfile(
        harness_name=harness_name,
        profile_name=profile_name,
        model=profile_config.model,
        effort=profile_config.effort,
    )


def resolve_role_selector(
    role: str,
    team_name: str | None,
    config: WorkflowUserConfig,
    *,
    step_path: str = "<unknown>",
) -> str:
    selector = config.roles.get(role)
    if selector is None:
        if "." in role:
            return role
        raise WorkflowError(
            f"workflow step references unknown role '{role}' in {step_path}"
        )
    if team_name is None:
        return selector
    team_config = config.teams.get(team_name)
    if team_config is None:
        raise WorkflowError(
            f"workflow step references unknown team '{team_name}' in {step_path}"
        )
    return team_config.roles.get(role, selector)


def _resolve_step_runtime(
    step: WorkflowStepConfig,
    config: WorkflowUserConfig,
    *,
    team_name: str | None,
    step_path: str,
) -> tuple[str, ResolvedProfile]:
    selector = resolve_role_selector(
        step.role,
        team_name,
        config,
        step_path=step_path,
    )
    return selector, resolve_profile(selector, config, step_path=step_path)


def _resolve_prompt_file_path(
    prompt_text: str,
    *,
    config_dir: Path,
    working_dir: Path,
) -> Path | None:
    if not prompt_text.startswith("file://"):
        return None

    location = prompt_text[len("file://") :]
    if prompt_text.startswith("file:///"):
        file_path = Path(location)
        if not file_path.is_absolute():
            raise WorkflowError(
                f"prompt file path must be absolute: {file_path}"
            )
        return file_path

    if prompt_text.startswith("file://./"):
        return working_dir / location

    return config_dir / location


def render_prompt(
    prompt_text: str,
    *,
    config_dir: Path,
    working_dir: Path,
    original_plan_path: Path,
    new_plan_path: Path,
    active_plan_path: Path,
) -> str:
    file_path = _resolve_prompt_file_path(
        prompt_text,
        config_dir=config_dir,
        working_dir=working_dir,
    )
    if file_path is not None:
        if not file_path.is_file():
            raise WorkflowError(f"prompt file not found: {file_path}")
        prompt_text = file_path.read_text(encoding="utf-8")

    next_checkpoint = "-"
    work_on_next_checkpoint_cmd = ""
    if (
        "{NEXT_CP}" in prompt_text
        or "{WORK_ON_NEXT_CHECKPOINT_CMD}" in prompt_text
    ):
        try:
            active_plan = load_plan_tolerant(active_plan_path)
        except PlanParseError as exc:
            if "no checkpoint sections were found" not in str(exc):
                raise WorkflowError(str(exc)) from exc
        else:
            checkpoint_index = active_plan.parsed_plan.snapshot.current_checkpoint_index
            if checkpoint_index is not None:
                next_checkpoint = str(checkpoint_index)
                work_on_next_checkpoint_cmd = (
                    f"Work only on Checkpoint #{checkpoint_index}. "
                    "Do not repeat earlier checkpoints, and do not skip ahead."
                )
    prompt_text = prompt_text.replace("{ORIGINAL_PLAN_PATH}", str(original_plan_path))
    prompt_text = prompt_text.replace("{NEW_PLAN_PATH}", str(new_plan_path))
    prompt_text = prompt_text.replace("{ACTIVE_PLAN_PATH}", str(active_plan_path))
    prompt_text = prompt_text.replace("{NEXT_CP}", next_checkpoint)
    prompt_text = prompt_text.replace("{WORK_ON_NEXT_CHECKPOINT_CMD}", work_on_next_checkpoint_cmd)
    return prompt_text


def render_step_prompts(
    step: WorkflowStepConfig,
    config: WorkflowUserConfig,
    *,
    config_dir: Path,
    working_dir: Path,
    original_plan_path: Path,
    new_plan_path: Path,
    active_plan_path: Path,
) -> str:
    parts: list[str] = []
    for prompt_key in step.prompts:
        if prompt_key not in config.prompts:
            raise WorkflowError(
                f"step references unknown prompt '{prompt_key}'"
            )
        raw = config.prompts[prompt_key]
        rendered = render_prompt(
            raw,
            config_dir=config_dir,
            working_dir=working_dir,
            original_plan_path=original_plan_path,
            new_plan_path=new_plan_path,
            active_plan_path=active_plan_path,
        )
        parts.append(rendered)
    return "\n\n".join(parts)


def _rewrite_plan_branch_text(text: str, branch_name: str) -> str:
    return _PLAN_BRANCH_LINE_RE.sub(
        lambda match: f"{match.group(1)}{branch_name}{match.group(3)}",
        text,
        count=1,
    )


def _update_plan_branch(path: Path, branch_name: str) -> bool:
    try:
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        updated = _rewrite_plan_branch_text(text, branch_name)
        if updated == text:
            return False
        path.write_text(updated, encoding="utf-8")
        return True
    except OSError as exc:
        raise WorkflowError(
            f"failed to update Plan Branch in original plan '{path}' to '{branch_name}': {exc}"
        ) from exc


def _sync_plan_branch_for_execution(
    original_plan_path: Path,
    exec_ctx: ExecutionContext | None,
) -> None:
    if exec_ctx is None:
        return
    _update_plan_branch(original_plan_path, exec_ctx.feature_branch)


def _sync_startup_plan_metadata_for_execution(
    original_plan_path: Path,
    exec_ctx: ExecutionContext | None,
    *,
    startup_base_head_refresh_sha: str | None,
) -> None:
    if exec_ctx is None and startup_base_head_refresh_sha is None:
        return
    if not original_plan_path.is_file():
        raise WorkflowError(f"startup metadata sync: original plan file does not exist: {original_plan_path}")

    text = original_plan_path.read_text(encoding="utf-8")
    updated = text
    try:
        if exec_ctx is not None:
            updated = _rewrite_plan_branch_text(updated, exec_ctx.feature_branch)
        if startup_base_head_refresh_sha is not None:
            before_refresh = updated
            updated = rewrite_git_tracking_field(
                updated,
                "Pre-Handoff Base HEAD",
                startup_base_head_refresh_sha,
            )
            if updated == before_refresh:
                raise WorkflowError(
                    "startup metadata sync did not update Pre-Handoff Base HEAD in the original plan"
                )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc

    if updated != text:
        original_plan_path.write_text(updated, encoding="utf-8")


def preflight_pre_handoff_base_head_refresh(
    repo_root: Path,
    plan_text: str,
    parsed_plan: ParsedPlan,
) -> StartupBaseHeadRefreshResult:
    metadata = parse_git_tracking_metadata(plan_text)
    if metadata is None:
        return StartupBaseHeadRefreshResult(
            status=StartupBaseHeadRefreshStatus.NO_GIT_TRACKING,
        )

    rc, current_head, _ = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if rc != 0 or not current_head.strip():
        return StartupBaseHeadRefreshResult(
            status=StartupBaseHeadRefreshStatus.NO_RESOLVABLE_HEAD,
        )

    if metadata.pre_handoff_base_head is None:
        return StartupBaseHeadRefreshResult(
            status=StartupBaseHeadRefreshStatus.MALFORMED,
            current_head=current_head,
        )

    is_pristine = is_handoff_pristine_for_base_refresh(metadata, parsed_plan.sections)
    recorded_base_head = metadata.pre_handoff_base_head

    if recorded_base_head == "":
        return StartupBaseHeadRefreshResult(
            status=(
                StartupBaseHeadRefreshStatus.EMPTY_BASE_PRISTINE
                if is_pristine
                else StartupBaseHeadRefreshStatus.EMPTY_BASE_STARTED
            ),
            current_head=current_head,
            recorded_base_head=recorded_base_head,
            is_pristine=is_pristine,
        )

    if recorded_base_head == current_head:
        return StartupBaseHeadRefreshResult(
            status=StartupBaseHeadRefreshStatus.MATCH,
            current_head=current_head,
            recorded_base_head=recorded_base_head,
            is_pristine=is_pristine,
        )

    return StartupBaseHeadRefreshResult(
        status=(
            StartupBaseHeadRefreshStatus.MISMATCH_PRISTINE
            if is_pristine
            else StartupBaseHeadRefreshStatus.MISMATCH_STARTED
        ),
        current_head=current_head,
        recorded_base_head=recorded_base_head,
        is_pristine=is_pristine,
    )


def generate_new_plan_path(
    original_plan_path: Path,
    checkpoint_index: int | None,
) -> Path:
    stem = original_plan_path.stem
    parent = original_plan_path.parent
    suffix = original_plan_path.suffix or ".md"
    cp = 1 if checkpoint_index is None else checkpoint_index
    pattern = re.compile(
        re.escape(f"{stem}-cp{cp:02d}-v") + r"(\d+)" + re.escape(suffix)
    )
    existing_versions: set[int] = set()
    if parent.is_dir():
        for child in parent.iterdir():
            m = pattern.match(child.name)
            if m:
                existing_versions.add(int(m.group(1)))
    next_version = max(existing_versions, default=0) + 1
    return parent / f"{stem}-cp{cp:02d}-v{next_version:02d}{suffix}"


def _plan_backup_base_name(original_plan_path: Path) -> tuple[str, str]:
    suffix = original_plan_path.suffix
    if suffix:
        return original_plan_path.name[:-len(suffix)], suffix
    return original_plan_path.name, ""


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _same_file_contents(
    source_path: Path,
    candidate_path: Path,
    *,
    source_identity: tuple[int, str] | None = None,
) -> bool:
    if source_identity is None:
        source_identity = _file_identity(source_path)
    source_size, source_hash = source_identity
    if candidate_path.stat().st_size != source_size:
        return False
    candidate_identity = _file_identity(candidate_path)
    return candidate_identity[1] == source_hash


def _backup_original_plan(repo_root: Path, original_plan_path: Path) -> Path:
    if not original_plan_path.is_file():
        raise WorkflowError(f"original plan file does not exist: {original_plan_path}")

    backup_dir = repo_root / "plans" / "backups"
    base_name, suffix = _plan_backup_base_name(original_plan_path)
    base_backup_path = backup_dir / f"{base_name}{suffix}"
    version_pattern = re.compile(
        rf"^{re.escape(base_name)}_v(\d+){re.escape(suffix)}$"
    )
    source_identity = _file_identity(original_plan_path)
    highest_version = 1

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)

        if base_backup_path.is_file():
            if _same_file_contents(
                original_plan_path,
                base_backup_path,
                source_identity=source_identity,
            ):
                return base_backup_path

        for child in backup_dir.iterdir():
            if not child.is_file() or child == base_backup_path:
                continue
            match = version_pattern.match(child.name)
            if match is None:
                continue
            highest_version = max(highest_version, int(match.group(1)))
            if _same_file_contents(
                original_plan_path,
                child,
                source_identity=source_identity,
            ):
                return child

        if not base_backup_path.exists():
            target_path = base_backup_path
        else:
            version = max(highest_version, 1) + 1
            target_path = backup_dir / f"{base_name}_v{version:02d}{suffix}"
            while target_path.exists():
                version += 1
                target_path = backup_dir / f"{base_name}_v{version:02d}{suffix}"

        shutil.copyfile(original_plan_path, target_path)
        return target_path
    except OSError as exc:
        raise WorkflowError(
            f"failed to back up original plan {original_plan_path} into {backup_dir}: {exc}"
        ) from exc


def _done_plan_path(repo_root: Path, plan_path: Path) -> Path | None:
    plans_root = (repo_root / "plans").resolve()
    in_progress_root = plans_root / "in-progress"
    try:
        relative_plan_path = plan_path.resolve().relative_to(in_progress_root)
    except ValueError:
        return None
    return plans_root / "done" / relative_plan_path


def move_completed_plan_to_done(repo_root: Path, plan_path: Path) -> Path:
    done_plan_path = _done_plan_path(repo_root, plan_path)
    if done_plan_path is None:
        raise WorkflowError(
            f"completed plan is not under '{repo_root / 'plans' / 'in-progress'}': {plan_path}"
        )
    if not plan_path.is_file():
        raise WorkflowError(f"completed plan file does not exist: {plan_path}")

    done_plan_path.parent.mkdir(parents=True, exist_ok=True)
    if done_plan_path.exists():
        if done_plan_path.is_file() and _same_file_contents(plan_path, done_plan_path):
            plan_path.unlink()
            return done_plan_path
        raise WorkflowError(
            f"done plan path already exists: {done_plan_path}"
        )

    try:
        shutil.move(str(plan_path), str(done_plan_path))
    except OSError as exc:
        raise WorkflowError(
            f"failed to move completed plan {plan_path} to {done_plan_path}: {exc}"
        ) from exc
    return done_plan_path


def _finalize_original_plan_if_complete(
    repo_root: Path,
    original_plan_path: Path,
    *,
    snapshot: PlanSnapshot,
) -> Path:
    if not snapshot.is_complete:
        return original_plan_path
    done_plan_path = _done_plan_path(repo_root, original_plan_path)
    if done_plan_path is None:
        return original_plan_path
    return move_completed_plan_to_done(repo_root, original_plan_path)


def _resolve_post_turn_original_plan_path(
    repo_root: Path,
    original_plan_path: Path,
    *,
    completed_returncode: int,
) -> Path:
    if original_plan_path.is_file():
        return original_plan_path
    if completed_returncode != 0:
        raise FileNotFoundError(
            f"{original_plan_path}: plan file does not exist"
        )
    raise FileNotFoundError(
        f"{original_plan_path}: original plan file is missing after the turn; "
        "workflow-owned finalization requires agents to keep the original plan "
        "under plans/in-progress until terminal success"
    )


def _evaluate_condition_token(
    token: str,
    *,
    done: bool,
    new_plan_exists: bool,
    max_turns_reached: bool,
) -> bool:
    if token == "DONE":
        return done
    if token == "NEW_PLAN_EXISTS":
        return new_plan_exists
    if token == "MAX_TURNS_REACHED":
        return max_turns_reached
    raise WorkflowError(f"unknown condition symbol: {token}")


def evaluate_condition(
    expression: str,
    *,
    done: bool,
    new_plan_exists: bool,
    max_turns_reached: bool,
) -> bool:
    tokens = _tokenize_condition(expression)
    pos = [0]
    result = _parse_or(tokens, pos, done=done, new_plan_exists=new_plan_exists, max_turns_reached=max_turns_reached)
    if pos[0] < len(tokens):
        raise WorkflowError(
            f"unexpected token '{tokens[pos[0]]}' in condition expression"
        )
    return result


def _tokenize_condition(expression: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(expression):
        ch = expression[i]
        if ch.isspace():
            i += 1
            continue
        if expression[i:i+2] == "&&":
            tokens.append("&&")
            i += 2
        elif expression[i:i+2] == "||":
            tokens.append("||")
            i += 2
        elif ch == "!":
            tokens.append("!")
            i += 1
        elif ch == "(":
            tokens.append("(")
            i += 1
        elif ch == ")":
            tokens.append(")")
            i += 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < len(expression) and (expression[j].isalnum() or expression[j] == "_"):
                j += 1
            tokens.append(expression[i:j])
            i = j
        else:
            raise WorkflowError(
                f"unexpected character '{ch}' in condition expression"
            )
    return tokens


def _parse_or(
    tokens: list[str], pos: list[int], **kwargs: bool,
) -> bool:
    result = _parse_and(tokens, pos, **kwargs)
    while pos[0] < len(tokens) and tokens[pos[0]] == "||":
        pos[0] += 1
        right = _parse_and(tokens, pos, **kwargs)
        result = result or right
    return result


def _parse_and(
    tokens: list[str], pos: list[int], **kwargs: bool,
) -> bool:
    result = _parse_not(tokens, pos, **kwargs)
    while pos[0] < len(tokens) and tokens[pos[0]] == "&&":
        pos[0] += 1
        right = _parse_not(tokens, pos, **kwargs)
        result = result and right
    return result


def _parse_not(
    tokens: list[str], pos: list[int], **kwargs: bool,
) -> bool:
    if pos[0] < len(tokens) and tokens[pos[0]] == "!":
        pos[0] += 1
        return not _parse_not(tokens, pos, **kwargs)
    return _parse_primary(tokens, pos, **kwargs)


def _parse_primary(
    tokens: list[str], pos: list[int], **kwargs: bool,
) -> bool:
    if pos[0] >= len(tokens):
        raise WorkflowError("unexpected end of condition expression")
    token = tokens[pos[0]]
    if token == "(":
        pos[0] += 1
        result = _parse_or(tokens, pos, **kwargs)
        if pos[0] >= len(tokens) or tokens[pos[0]] != ")":
            raise WorkflowError("missing closing parenthesis in condition expression")
        pos[0] += 1
        return result
    if token in VALID_CONDITION_SYMBOLS:
        pos[0] += 1
        return _evaluate_condition_token(token, **kwargs)
    raise WorkflowError(f"unexpected token '{token}' in condition expression")


def pick_transition(
    transitions: tuple[GoTransition, ...],
    *,
    step_path: str,
    done: bool,
    new_plan_exists: bool,
    max_turns_reached: bool,
) -> str:
    return _select_transition(
        transitions,
        step_path=step_path,
        done=done,
        new_plan_exists=new_plan_exists,
        max_turns_reached=max_turns_reached,
    ).to


def _select_transition(
    transitions: tuple[GoTransition, ...],
    *,
    step_path: str,
    done: bool,
    new_plan_exists: bool,
    max_turns_reached: bool,
) -> GoTransition:
    for transition in transitions:
        if transition.when is None:
            return transition
        if evaluate_condition(
            transition.when,
            done=done,
            new_plan_exists=new_plan_exists,
            max_turns_reached=max_turns_reached,
        ):
            return transition
    raise WorkflowError(
        f"no transition matched for {step_path} "
        f"with conditions: DONE={done}, NEW_PLAN_EXISTS={new_plan_exists}, "
        f"MAX_TURNS_REACHED={max_turns_reached}"
    )


def _normalize_end_reason(
    *,
    already_complete: bool = False,
    selected_transition: GoTransition | None = None,
    done: bool = False,
    max_turns_reached: bool = False,
) -> WorkflowEndReason:
    if already_complete:
        return "already_complete"
    if selected_transition is not None and selected_transition.when is None:
        return "transition_end"
    if done:
        return "done"
    if max_turns_reached:
        return "max_turns_reached"
    return "transition_end"


def _format_failure(
    *,
    reason: str,
    run_dir: Path,
    snapshot: PlanSnapshot,
    parse_error: PlanParseError | None = None,
) -> str:
    if parse_error is not None and parse_error.checkpoint_name is not None:
        current = parse_error.checkpoint_name
        unchecked_steps = parse_error.unchecked_step_count or 0
    else:
        current = snapshot.current_checkpoint_name or "none"
        unchecked_steps = snapshot.current_checkpoint_unchecked_step_count
    return (
        f"{reason}\n"
        f"run log directory: {run_dir}\n"
        f"current checkpoint: {current}\n"
        f"unchecked checkpoint count: {snapshot.unchecked_checkpoint_count}\n"
        f"current checkpoint unchecked step count: {unchecked_steps}"
    )


def _normalize_process_launch_error(
    invocation: HarnessInvocation,
    error: OSError,
) -> subprocess.CompletedProcess[str]:
    """Represent a harness process-creation error as a normal failed result."""
    returncode = 127 if (
        isinstance(error, FileNotFoundError) or error.errno == errno.ENOENT
    ) else 126
    errno_text = str(error.errno) if error.errno is not None else "unknown"
    message = error.strerror or "process creation failed"
    return subprocess.CompletedProcess(
        list(invocation.argv),
        returncode,
        "",
        f"harness '{invocation.label}' failed to start (errno {errno_text}): {message}",
    )


def _run_process(
    invocation: HarnessInvocation,
    repo_root: Path,
    banner: BannerRenderer,
    state: ControllerState,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.Popen(
            list(invocation.argv),
            cwd=str(repo_root),
            env={**os.environ, **invocation.env},
            stdin=subprocess.PIPE if invocation.stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return _normalize_process_launch_error(invocation, exc)

    banner.update(state)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _drain(stream, chunks: list[str]) -> None:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            chunks.append(chunk)

    assert proc.stdout is not None
    assert proc.stderr is not None
    t_out = threading.Thread(
        target=_drain,
        args=(proc.stdout, stdout_chunks),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_drain,
        args=(proc.stderr, stderr_chunks),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    stdin_errors: list[BrokenPipeError] = []
    t_in: threading.Thread | None = None
    stdin_text = invocation.stdin_text
    if stdin_text is not None:
        assert proc.stdin is not None

        def _record_broken_pipe(exc: BrokenPipeError) -> None:
            try:
                proc.wait(timeout=PROCESS_POLL_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                stdin_errors.append(exc)

        def _write_stdin() -> None:
            try:
                proc.stdin.write(stdin_text)
            except BrokenPipeError as exc:
                # A child which exits while its input is being written has
                # already closed the pipe.  If it is still alive, preserve the
                # error rather than hiding an unexpected transport failure.
                _record_broken_pipe(exc)
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError as exc:
                    _record_broken_pipe(exc)

        t_in = threading.Thread(target=_write_stdin, daemon=True)
        t_in.start()

    while True:
        try:
            proc.wait(timeout=PROCESS_POLL_INTERVAL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass

    if t_in is not None:
        t_in.join()
    t_out.join()
    t_err.join()

    if stdin_errors:
        raise stdin_errors[0]

    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode or 0,
        "".join(stdout_chunks),
        "".join(stderr_chunks),
    )


def _run_injected_runner(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    invocation: HarnessInvocation,
    repo_root: Path,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "cwd": str(repo_root),
        "env": {**os.environ, **invocation.env},
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if invocation.stdin_text is not None:
        kwargs["input"] = invocation.stdin_text
    try:
        return runner(list(invocation.argv), **kwargs)
    except OSError as exc:
        return _normalize_process_launch_error(invocation, exc)


def _workflow_requires_git_tracking(
    wf: WorkflowConfig,
    config: WorkflowUserConfig,
) -> bool:
    for step in wf.steps.values():
        for prompt_key in step.prompts:
            prompt_text = config.prompts.get(prompt_key, "")
            for skill_name in _REVIEW_SKILL_NAMES:
                if skill_name in prompt_text:
                    return True
    return False


def _make_banner(
    config: ControllerConfig,
    *,
    workflow_steps: dict[str, WorkflowStepConfig] | None = None,
    workflow_graph_source: WorkflowGraphSource | None = None,
    workflow_name: str | None = None,
    original_plan_path: Path | None = None,
    banner_files_limit: int = 10,
) -> BannerRenderer:
    return BannerRenderer(
        config_max_turns=config.max_turns,
        config_plan_path=config.plan_path,
        workflow_steps=workflow_steps,
        workflow_graph_source=workflow_graph_source,
        config_banner_files_limit=banner_files_limit,
        workflow_name=workflow_name,
        original_plan_path=original_plan_path,
        repo_root=config.repo_root,
    )


_RETRY_APPENDIX_INTRO = (
    "The previous attempt left the plan in an invalid checkpoint state: "
    "a checkpoint heading was marked complete while one or more checkpoint-local "
    "steps remained unchecked. Repair the plan file so that any checkpoint "
    "marked complete has all its checkpoint-local steps also checked.\n\n"
    "Parse error from the previous attempt:\n"
)


def _effective_retry_limit(
    wf: WorkflowConfig,
    global_section: object,
) -> int:
    if wf.retry_inconsistent_checkpoint_state is not None:
        return wf.retry_inconsistent_checkpoint_state
    return getattr(global_section, "retry_inconsistent_checkpoint_state", 0)


def _build_retry_appendix(parse_error_str: str) -> str:
    return f"{_RETRY_APPENDIX_INTRO}{parse_error_str}"


def _detect_stop_marker(stdout: str, stderr: str) -> str | None:
    return detect_stop_marker(stdout, stderr)


_BRANCH_STEM_MAX_LEN = 50


def _sanitize_plan_stem(stem: str) -> str:
    stem = stem.lower()
    stem = re.sub(r"[^a-z0-9-]", "-", stem)
    stem = re.sub(r"-+", "-", stem)
    stem = stem.strip("-")
    return stem[:_BRANCH_STEM_MAX_LEN] or "plan"


def _run_git(args: list[str], *, cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        result.returncode,
        result.stdout.rstrip("\r\n"),
        result.stderr.rstrip("\r\n"),
    )


def _is_git_tracked(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    rc, _, _ = _run_git(
        ["ls-files", "--error-unmatch", "--", rel.as_posix()],
        cwd=repo_root,
    )
    return rc == 0


@dataclass(frozen=True)
class _LifecyclePlan:
    main_branch: str
    feature_branch: str
    worktree_path: Path | None
    setup: tuple[str, ...]
    teardown: tuple[str, ...]


def _lifecycle_preflight_git(
    primary_root: Path,
    main_branch: str,
    feature_branch: str,
    uses_worktree: bool,
    worktree_path: Path | None,
    *,
    allow_untracked: bool = False,
) -> None:
    """Phase B: git-dependent lifecycle preflight checks.

    Runs only after bootstrap has ensured commits exist at primary_root.
    When allow_untracked is True, untracked files (porcelain '??') are not treated as
    dirtiness — used after bootstrap where pre-existing files may remain untracked.
    """
    rc, current_branch, err = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=primary_root)
    if rc != 0:
        raise WorkflowError(
            f"lifecycle preflight: cannot determine current branch in '{primary_root}': {err}"
        )

    rc, current_branch, err = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=primary_root)
    if rc != 0:
        raise WorkflowError(
            f"lifecycle preflight: cannot determine current branch in '{primary_root}': {err}"
        )

    rc, _, _ = _run_git(["rev-parse", "--verify", "HEAD"], cwd=primary_root)
    if rc != 0 and current_branch == main_branch:
        raise WorkflowError(
            f"lifecycle preflight: branch '{main_branch}' in '{primary_root}' has no commits yet; "
            "create an initial commit before running lifecycle workflows"
        )

    rc, _, _ = _run_git(["show-ref", "--verify", f"refs/heads/{main_branch}"], cwd=primary_root)
    if rc != 0:
        raise WorkflowError(
            f"lifecycle preflight: branch '{main_branch}' does not exist locally in '{primary_root}'"
        )

    if current_branch != main_branch:
        raise WorkflowError(
            f"lifecycle preflight: current branch is '{current_branch}' "
            f"but workflow requires starting from '{main_branch}'"
        )

    rc, status_out, _ = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=primary_root
    )
    if rc != 0:
        raise WorkflowError(
            f"lifecycle preflight: cannot check working tree state in '{primary_root}'"
        )

    effective_status = status_out
    if allow_untracked:
        tracked_lines = [
            line for line in status_out.splitlines()
            if len(line) >= 2 and line[:2] != "??"
        ]
        effective_status = "\n".join(tracked_lines)

    if effective_status.strip():
        if uses_worktree:
            _, non_plan_paths = classify_dirtiness_by_prefix(effective_status)
            if non_plan_paths:
                raise WorkflowError(
                    f"lifecycle preflight: primary checkout at '{primary_root}' has non-plan dirtiness: "
                    f"{', '.join(non_plan_paths[:3])}{'...' if len(non_plan_paths) > 3 else ''}"
                )
        else:
            raise WorkflowError(
                f"lifecycle preflight: primary checkout at '{primary_root}' has uncommitted changes"
            )

    rc, _, _ = _run_git(["show-ref", "--verify", f"refs/heads/{feature_branch}"], cwd=primary_root)
    if rc == 0:
        raise WorkflowError(
            f"lifecycle preflight: branch '{feature_branch}' already exists"
        )

    if uses_worktree and worktree_path is not None:
        rc, wt_list, _ = _run_git(["worktree", "list", "--porcelain"], cwd=primary_root)
        if rc == 0:
            for line in wt_list.splitlines():
                if line.startswith("worktree "):
                    registered = line[len("worktree "):]
                    if Path(registered).resolve() == worktree_path.resolve():
                        raise WorkflowError(
                            f"lifecycle preflight: path '{worktree_path}' is already "
                            f"registered as a git worktree"
                        )


def _lifecycle_preflight(
    primary_root: Path,
    plan_path: Path,
    wf: WorkflowConfig,
    aflow_section: AflowSection,
    repo_state: RepoState,
    *,
    skip_phase_b: bool = False,
) -> _LifecyclePlan | None:
    setup = wf.setup or ()
    teardown = wf.teardown or ()

    if not setup:
        return None

    if repo_state == RepoState.NO_GIT_BINARY:
        raise WorkflowError(
            "lifecycle bootstrap requires git to be installed locally; "
            "git was not found on PATH"
        )

    # --- Phase A: git-independent validation ---
    main_branch = wf.main_branch
    if not main_branch:
        raise WorkflowError(
            "workflow uses lifecycle setup but main_branch is not configured"
        )

    uses_worktree = "worktree" in setup
    worktree_path: Path | None = None

    if uses_worktree:
        try:
            plan_path.resolve().relative_to(primary_root.resolve())
        except ValueError:
            raise WorkflowError(
                f"lifecycle preflight: plan file '{plan_path}' must be under "
                f"the primary repo root '{primary_root}' for worktree workflows"
            )
        if not plan_path.is_file():
            raise WorkflowError(
                f"lifecycle preflight: plan file '{plan_path}' must exist "
                "for worktree workflows"
            )

        worktree_root_str = aflow_section.worktree_root
        if not worktree_root_str:
            raise WorkflowError(
                "lifecycle preflight: worktree workflow requires [aflow].worktree_root to be set"
            )

        worktree_root = Path(worktree_root_str).expanduser().resolve()

        try:
            worktree_root.relative_to(primary_root.resolve())
            raise WorkflowError(
                f"lifecycle preflight: worktree_root '{worktree_root}' "
                f"must not be inside the primary repo root '{primary_root}'"
            )
        except ValueError:
            pass

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stem = _sanitize_plan_stem(plan_path.stem)
        branch_prefix = (aflow_section.branch_prefix or "aflow").rstrip("-")
        feature_branch = f"{branch_prefix}-{stem}-{ts}"

        worktree_dir_prefix = (aflow_section.worktree_prefix or "aflow").rstrip("-")
        worktree_dir_name = f"{worktree_dir_prefix}-{stem}-{ts}"
        worktree_path = worktree_root / worktree_dir_name

        if worktree_path.exists():
            raise WorkflowError(
                f"lifecycle preflight: worktree path '{worktree_path}' already exists on disk"
            )
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stem = _sanitize_plan_stem(plan_path.stem)
        branch_prefix = (aflow_section.branch_prefix or "aflow").rstrip("-")
        feature_branch = f"{branch_prefix}-{stem}-{ts}"

    # --- Phase B: git-dependent validation ---
    # Runs after bootstrap has ensured commits exist.
    # skip_phase_b=True defers this call to after the bootstrap handoff in run_workflow.
    if not skip_phase_b:
        _lifecycle_preflight_git(primary_root, main_branch, feature_branch, uses_worktree, worktree_path)

    return _LifecyclePlan(
        main_branch=main_branch,
        feature_branch=feature_branch,
        worktree_path=worktree_path,
        setup=setup,
        teardown=teardown,
    )


def _setup_branch_only(
    primary_root: Path,
    main_branch: str,
    feature_branch: str,
) -> None:
    rc, _, err = _run_git(
        ["checkout", "-b", feature_branch, main_branch], cwd=primary_root
    )
    if rc != 0:
        raise WorkflowError(
            f"lifecycle setup: cannot create branch '{feature_branch}' "
            f"from '{main_branch}': {err}"
        )


def _setup_worktree(
    primary_root: Path,
    main_branch: str,
    feature_branch: str,
    worktree_path: Path,
) -> None:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = _run_git(
        ["worktree", "add", "-b", feature_branch, str(worktree_path), main_branch],
        cwd=primary_root,
    )
    if rc != 0:
        raise WorkflowError(
            f"lifecycle setup: cannot create worktree at '{worktree_path}' "
            f"with branch '{feature_branch}' from '{main_branch}': {err}"
        )


def _do_lifecycle_setup(
    primary_root: Path,
    plan: _LifecyclePlan,
) -> ExecutionContext:
    if "worktree" in plan.setup:
        assert plan.worktree_path is not None
        _setup_worktree(primary_root, plan.main_branch, plan.feature_branch, plan.worktree_path)
        execution_root = plan.worktree_path
    else:
        _setup_branch_only(primary_root, plan.main_branch, plan.feature_branch)
        execution_root = primary_root
    return ExecutionContext(
        primary_repo_root=primary_root,
        execution_repo_root=execution_root,
        main_branch=plan.main_branch,
        feature_branch=plan.feature_branch,
        worktree_path=plan.worktree_path,
        setup=plan.setup,
        teardown=plan.teardown,
    )


def _exec_plan_path(path: Path, exec_ctx: ExecutionContext | None) -> Path:
    if exec_ctx is None or exec_ctx.worktree_path is None:
        return path
    try:
        rel = path.resolve().relative_to(exec_ctx.primary_repo_root.resolve())
        return exec_ctx.execution_repo_root / rel
    except ValueError:
        return path


def _primary_plan_path(path: Path, exec_ctx: ExecutionContext | None) -> Path:
    if exec_ctx is None or exec_ctx.worktree_path is None:
        return path
    try:
        rel = path.resolve().relative_to(exec_ctx.execution_repo_root.resolve())
        return exec_ctx.primary_repo_root / rel
    except ValueError:
        return path


def _select_next_active_plan_path(
    *,
    original_plan_path: Path,
    active_plan_path: Path,
    new_plan_path: Path,
    new_plan_exists: bool,
    selected_transition: GoTransition,
    exec_ctx: ExecutionContext | None,
) -> Path:
    if new_plan_exists:
        return new_plan_path
    if selected_transition.preserve_active_plan:
        execution_path = _exec_plan_path(active_plan_path, exec_ctx)
        if not execution_path.is_file():
            raise WorkflowError(
                "cannot preserve active plan for transition to "
                f"'{selected_transition.to}': active plan does not exist in the "
                f"execution checkout: {execution_path}"
            )
        return active_plan_path
    return original_plan_path


def _list_followup_plan_candidates(original_plan_path: Path) -> set[Path]:
    parent = original_plan_path.parent
    suffix = original_plan_path.suffix
    prefix = f"{original_plan_path.stem}-"
    if not parent.is_dir():
        return set()

    candidates: set[Path] = set()
    for child in parent.iterdir():
        if not child.is_file() or child == original_plan_path:
            continue
        if suffix and child.suffix != suffix:
            continue
        if not child.name.startswith(prefix):
            continue
        candidates.add(child.resolve())
    return candidates


def _resolve_post_turn_new_plan_path(
    *,
    original_plan_path: Path,
    expected_new_plan_path: Path,
    candidates_before: set[Path],
) -> Path | None:
    if (
        expected_new_plan_path.is_file()
        and expected_new_plan_path.resolve() not in candidates_before
    ):
        return expected_new_plan_path

    candidates_after = _list_followup_plan_candidates(original_plan_path)
    created_candidates = candidates_after - candidates_before
    if len(created_candidates) == 1:
        return next(iter(created_candidates))

    return None


def _sync_plan_to_worktree(primary_plan_path: Path, exec_ctx: ExecutionContext | None) -> None:
    """Copy the original plan from primary checkout to worktree if needed.

    Creates parent directories in the worktree if they don't exist.
    Raises WorkflowError if the source is unreadable or the copy fails.
    """
    if exec_ctx is None or exec_ctx.worktree_path is None:
        return

    exec_plan_path = _exec_plan_path(primary_plan_path, exec_ctx)

    try:
        if not primary_plan_path.is_file():
            raise WorkflowError(
                f"_sync_plan_to_worktree: original plan file not found: {primary_plan_path}"
            )

        exec_plan_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(primary_plan_path, exec_plan_path)
    except (OSError, IOError) as exc:
        raise WorkflowError(
            f"_sync_plan_to_worktree: failed to copy original plan from "
            f"{primary_plan_path} to {exec_plan_path}: {exc}"
        ) from exc


def _sync_plan_from_worktree(primary_plan_path: Path, exec_ctx: ExecutionContext | None) -> None:
    """Copy the original plan from worktree back to primary checkout if it was edited.

    Raises WorkflowError if the copy fails.
    Sync happens regardless of harness success/failure — if the plan was edited,
    the primary copy must reflect those edits for restart correctness.
    """
    if exec_ctx is None or exec_ctx.worktree_path is None:
        return

    exec_plan_path = _exec_plan_path(primary_plan_path, exec_ctx)

    try:
        if not exec_plan_path.is_file():
            raise WorkflowError(
                f"_sync_plan_from_worktree: worktree plan file not found: {exec_plan_path}"
            )

        shutil.copyfile(exec_plan_path, primary_plan_path)
    except (OSError, IOError) as exc:
        raise WorkflowError(
            f"_sync_plan_from_worktree: failed to copy original plan from "
            f"{exec_plan_path} back to {primary_plan_path}: {exc}"
        ) from exc


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    """Durably replace one file without exposing a partial plan copy."""
    from uuid import uuid4

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise WorkflowError(
            f"cannot atomically replace repartitioned plan copy '{path}': {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _reconcile_repartition_plan_copies(
    pending: PendingRepartitionV1,
    *,
    candidate_bytes: bytes,
    execution_path: Path,
    primary_path: Path,
    persist: Callable[[PendingRepartitionV1], None],
) -> PendingRepartitionV1:
    """Apply source/candidate copies idempotently and persist each boundary."""
    copies = (
        ("execution_plan_applied", execution_path),
        ("primary_plan_applied", primary_path),
    )
    stage_rank = {
        "semantically_validated": 0,
        "execution_plan_applied": 1,
        "primary_plan_applied": 2,
        "applied": 3,
    }
    observed: dict[Path, str] = {}
    for _stage, plan_copy in copies:
        if plan_copy in observed:
            continue
        try:
            observed[plan_copy] = hashlib.sha256(plan_copy.read_bytes()).hexdigest()
        except OSError as exc:
            raise WorkflowError(
                f"cannot inspect repartition plan copy '{plan_copy}': {exc}"
            ) from exc
    unknown = {
        path: digest
        for path, digest in observed.items()
        if digest not in {
            pending.source_plan_sha256,
            pending.candidate_plan_sha256,
        }
    }
    if unknown:
        details = "; ".join(
            f"{path}: expected source={pending.source_plan_sha256} or "
            f"candidate={pending.candidate_plan_sha256}, observed={digest}"
            for path, digest in unknown.items()
        )
        raise WorkflowError(
            "repartition plan-copy divergence; no copy was overwritten: " + details
        )

    for applied_stage, plan_copy in copies:
        current_hash = hashlib.sha256(plan_copy.read_bytes()).hexdigest()
        if current_hash == pending.source_plan_sha256:
            _atomic_replace_bytes(plan_copy, candidate_bytes)
            observed_hash = hashlib.sha256(plan_copy.read_bytes()).hexdigest()
            if observed_hash != pending.candidate_plan_sha256:
                raise WorkflowError(
                    "repartition post-write hash mismatch for "
                    f"'{plan_copy}': expected={pending.candidate_plan_sha256} "
                    f"observed={observed_hash}"
                )
        elif current_hash != pending.candidate_plan_sha256:
            raise WorkflowError(
                "repartition plan copy changed during application: "
                f"'{plan_copy}' observed={current_hash}"
            )
        if stage_rank.get(pending.stage, -1) < stage_rank[applied_stage]:
            pending = replace(pending, stage=applied_stage)
            persist(pending)
    return pending


def _prepare_primary_plan_for_merge(
    primary_root: Path,
    original_plan_path: Path,
) -> _PreparedPrimaryPlanForMerge | None:
    if not original_plan_path.exists():
        return None

    try:
        original_text = original_plan_path.read_text(encoding="utf-8")
        tracked_in_git = _is_git_tracked(primary_root, original_plan_path)
        if tracked_in_git:
            try:
                rel = original_plan_path.resolve().relative_to(primary_root.resolve())
            except ValueError:
                return _PreparedPrimaryPlanForMerge(
                    plan_path=original_plan_path,
                    original_text=original_text,
                )
            rc, _, err = _run_git(["checkout", "--", rel.as_posix()], cwd=primary_root)
            if rc != 0:
                raise WorkflowError(
                    f"lifecycle teardown: failed to reset tracked original plan "
                    f"'{original_plan_path}' before merge: {err}"
                )
        else:
            original_plan_path.unlink()
    except OSError as exc:
        raise WorkflowError(
            f"lifecycle teardown: failed to prepare original plan '{original_plan_path}' "
            f"for merge: {exc}"
        ) from exc

    return _PreparedPrimaryPlanForMerge(
        plan_path=original_plan_path,
        original_text=original_text,
    )


def _restore_primary_plan_after_merge(
    prepared: _PreparedPrimaryPlanForMerge | None,
) -> None:
    if prepared is None:
        return
    if prepared.original_text is None:
        return
    try:
        prepared.plan_path.parent.mkdir(parents=True, exist_ok=True)
        prepared.plan_path.write_text(prepared.original_text, encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(
            f"lifecycle teardown: failed to restore original plan "
            f"'{prepared.plan_path}' after merge: {exc}"
            ) from exc


def _collect_merge_dirty_paths(
    repo_root: Path,
    *,
    original_plan_path: Path | None,
) -> list[str]:
    rc, out, _ = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
    )
    if rc != 0:
        raise WorkflowError(
            f"lifecycle teardown: cannot check working tree state in '{repo_root}' before merge"
        )

    dirty_paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        if _is_ignored_merge_status_line(
            line,
            primary_root=repo_root,
            original_plan_path=original_plan_path,
        ):
            continue
        path = line[3:] if len(line) >= 4 and line[2] == " " else line[2:]
        path = path.strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dirty_paths.append(path)
    return dirty_paths


def _ensure_merge_handoff_clean(
    exec_ctx: ExecutionContext,
    *,
    original_plan_path: Path,
) -> None:
    primary_dirty = _collect_merge_dirty_paths(
        exec_ctx.primary_repo_root,
        original_plan_path=original_plan_path,
    )
    worktree_dirty: list[str] = []
    if exec_ctx.worktree_path is not None:
        worktree_dirty = _collect_merge_dirty_paths(
            exec_ctx.worktree_path,
            original_plan_path=_exec_plan_path(original_plan_path, exec_ctx),
        )

    if not primary_dirty and not worktree_dirty:
        return

    reasons: list[str] = []
    if primary_dirty:
        sample = ", ".join(primary_dirty[:3])
        suffix = "..." if len(primary_dirty) > 3 else ""
        reasons.append(
            f"primary checkout at '{exec_ctx.primary_repo_root}' is dirty: {sample}{suffix}"
        )
    if worktree_dirty:
        sample = ", ".join(worktree_dirty[:3])
        suffix = "..." if len(worktree_dirty) > 3 else ""
        reasons.append(
            f"feature worktree at '{exec_ctx.worktree_path}' is dirty and those changes are not represented by branch '{exec_ctx.feature_branch}': {sample}{suffix}"
        )

    raise WorkflowError(
        "lifecycle teardown: merge handoff requires clean git state, but "
        + "; ".join(reasons)
    )


def _lifecycle_is_bootstrap_eligible(wf: WorkflowConfig, repo_state: RepoState) -> bool:
    """True when the lifecycle workflow needs a bootstrap before git-dependent preflight."""
    return bool(wf.setup) and repo_state in (RepoState.NOT_A_REPO, RepoState.UNBORN)


_SKIP_SECTION_HEADING_RE = re.compile(
    r"^## (Git Tracking|Done Means|Critical Invariants|Forbidden)"
)
_CHECKPOINT_HEADING_RE = re.compile(r"^###\s+\[")
_SUMMARY_SECTION_RE = re.compile(
    r"^## Summary\s*\n(.*?)(?=\n## |\Z)", re.MULTILINE | re.DOTALL
)


def derive_readme_content(plan_text: str, plan_stem: str) -> tuple[str, str]:
    """Extract (title, body) for README from plan text.

    Pure function — does not call git or read files. Accept plan text as a string.
    """
    title = _derive_readme_title(plan_text, plan_stem)
    body = _derive_readme_body(plan_text, title)
    return title, body


def _derive_readme_title(plan_text: str, plan_stem: str) -> str:
    for line in plan_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return plan_stem.replace("-", " ").title()


def _derive_readme_body(plan_text: str, title: str) -> str:
    summary_match = _SUMMARY_SECTION_RE.search(plan_text)
    if summary_match:
        body = summary_match.group(1).strip()
        if body:
            return body

    lines = plan_text.splitlines()
    past_title = False
    in_fenced = False
    skip_section = False
    prose_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        if not past_title:
            if stripped.startswith("# ") and not stripped.startswith("## "):
                past_title = True
            continue

        if _CHECKPOINT_HEADING_RE.match(stripped):
            break

        if stripped.startswith("## "):
            if _SKIP_SECTION_HEADING_RE.match(stripped):
                skip_section = True
            else:
                skip_section = False
                if prose_lines:
                    return " ".join(prose_lines)
            continue

        if skip_section:
            continue

        if stripped.startswith("#"):
            if prose_lines:
                return " ".join(prose_lines)
            continue

        if stripped.startswith("```"):
            in_fenced = not in_fenced
            if not in_fenced and prose_lines:
                return " ".join(prose_lines)
            if in_fenced and prose_lines:
                return " ".join(prose_lines)
            continue

        if in_fenced:
            continue

        if not stripped:
            if prose_lines:
                return " ".join(prose_lines)
            continue

        if stripped.startswith("- ") or stripped.startswith("* "):
            if prose_lines:
                return " ".join(prose_lines)
            continue

        prose_lines.append(stripped)

    if prose_lines:
        return " ".join(prose_lines)

    return f'This repository is being initialized from the aflow plan "{title}".'


_INIT_REPO_BUILTIN_INSTRUCTION = (
    "Use the `aflow-init-repo` skill to initialize the repository and create the initial commit."
)


def _build_init_repo_user_prompt(
    repo_root: Path,
    main_branch: str,
    readme_title: str,
    readme_body: str,
) -> str:
    return "\n\n".join([
        _INIT_REPO_BUILTIN_INSTRUCTION,
        f"Repo root: `{repo_root}`",
        f"Main branch: `{main_branch}`",
        f"README title: {readme_title}",
        f"README body:\n{readme_body}",
    ])


def _verify_init_repo_success(repo_root: Path, main_branch: str) -> str | None:
    """Returns None on success, or a description of which check failed."""
    rc, _, _ = _run_git(["rev-parse", "--verify", "HEAD"], cwd=repo_root)
    if rc != 0:
        return "HEAD does not resolve to a commit after bootstrap"

    rc, head_ref, _ = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=repo_root)
    if rc != 0 or head_ref.strip() != main_branch:
        return f"HEAD is not on '{main_branch}' after bootstrap (got '{head_ref.strip()}')"

    readme_path = repo_root / "README.md"
    if not readme_path.exists() or readme_path.stat().st_size == 0:
        return "README.md does not exist or is empty after bootstrap"
    rc, ls_out, _ = _run_git(["ls-files", "README.md"], cwd=repo_root)
    if rc != 0 or "README.md" not in ls_out:
        return "README.md is not tracked by git after bootstrap"

    rc, status_out, _ = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=repo_root
    )
    if rc != 0:
        return "cannot check working tree state after bootstrap"
    for line in status_out.splitlines():
        if len(line) < 2:
            continue
        xy = line[:2]
        if xy != "??":
            return f"working tree has tracked-file dirtiness after bootstrap: {line.strip()}"

    return None


def _execute_init_repo_handoff(
    primary_root: Path,
    workflow_config: WorkflowUserConfig,
    *,
    team_name: str | None,
    adapter: HarnessAdapter | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    main_branch: str,
    readme_title: str,
    readme_body: str,
    banner: BannerRenderer,
    state: ControllerState,
) -> subprocess.CompletedProcess[str]:
    team_lead_role = workflow_config.aflow.team_lead
    if not team_lead_role:
        raise WorkflowError("lifecycle bootstrap requires [aflow].team_lead to be configured")

    team_lead_selector = resolve_role_selector(
        team_lead_role, team_name, workflow_config, step_path="lifecycle bootstrap"
    )
    resolved = resolve_profile(team_lead_selector, workflow_config, step_path="lifecycle bootstrap")

    user_prompt = _build_init_repo_user_prompt(primary_root, main_branch, readme_title, readme_body)

    init_adapter = adapter or get_adapter(resolved.harness_name)
    invocation = init_adapter.build_invocation(
        repo_root=primary_root,
        model=resolved.model,
        system_prompt="",
        user_prompt=user_prompt,
        effort=resolved.effort,
    )

    if runner is None:
        return _run_process(invocation, primary_root, banner, state)
    return _run_injected_runner(runner, invocation, primary_root)


def _resolve_team_lead_profile(
    workflow_config: WorkflowUserConfig,
    *,
    team_name: str | None,
    step_path: str,
) -> ResolvedProfile:
    team_lead_role = workflow_config.aflow.team_lead
    if not team_lead_role:
        raise WorkflowError(f"{step_path} requires [aflow].team_lead to be configured")
    team_lead_selector = resolve_role_selector(
        team_lead_role,
        team_name,
        workflow_config,
        step_path=step_path,
    )
    return resolve_profile(team_lead_selector, workflow_config, step_path=step_path)


def _run_team_lead_recovery_handoff(
    repo_root: Path,
    workflow_config: WorkflowUserConfig,
    *,
    team_name: str | None,
    adapter: HarnessAdapter | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    banner: BannerRenderer,
    state: ControllerState,
    step_path: str,
    current_team: str | None,
    active_selector: str,
    harness_name: str,
    model: str | None,
    snapshot_before: PlanSnapshot,
    snapshot_after: PlanSnapshot | None,
    stdout: str | None,
    stderr: str | None,
    returncode: int,
    recovery_reason: str,
    recovery_cap: int,
    consecutive_count: int,
    matched_rule_action: str | None,
    matched_terms: tuple[str, ...],
    backup_team: str | None,
) -> TeamLeadRecoveryDecision:
    resolved = _resolve_team_lead_profile(workflow_config, team_name=team_name, step_path=step_path)
    user_prompt = build_team_lead_recovery_prompt(
        step_path=step_path,
        current_team=current_team,
        active_selector=active_selector,
        harness_name=harness_name,
        model=model,
        returncode=returncode,
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
        stdout=stdout,
        stderr=stderr,
        recovery_reason=recovery_reason,
        recovery_cap=recovery_cap,
        consecutive_count=consecutive_count,
        matched_rule_action=matched_rule_action,
        matched_terms=matched_terms,
        backup_team=backup_team,
    )
    lead_adapter = adapter or get_adapter(resolved.harness_name)
    invocation = lead_adapter.build_invocation(
        repo_root=repo_root,
        model=resolved.model,
        system_prompt="",
        user_prompt=user_prompt,
        effort=resolved.effort,
    )
    if runner is None:
        completed = _run_process(invocation, repo_root, banner, state)
    else:
        completed = _run_injected_runner(runner, invocation, repo_root)
    if completed.returncode != 0:
        evidence = build_recovery_evidence(
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=None,
        )
        detail = f": {evidence}" if evidence else ""
        raise TeamLeadRecoveryDecisionError(
            f"team lead recovery handoff failed with exit code {completed.returncode}{detail}"
        )
    return parse_team_lead_recovery_decision(completed.stdout)


_MERGE_BUILTIN_INSTRUCTION = "Use the `aflow-merge` skill to merge the feature branch into the target branch."


def render_merge_prompt(
    prompt_text: str,
    *,
    config_dir: Path,
    working_dir: Path,
    exec_ctx: ExecutionContext,
    original_plan_path: Path,
    new_plan_path: Path,
    active_plan_path: Path,
) -> str:
    rendered = render_prompt(
        prompt_text,
        config_dir=config_dir,
        working_dir=working_dir,
        original_plan_path=original_plan_path,
        new_plan_path=new_plan_path,
        active_plan_path=active_plan_path,
    )
    worktree_path_str = str(exec_ctx.worktree_path) if exec_ctx.worktree_path else ""
    rendered = rendered.replace("{MAIN_BRANCH}", exec_ctx.main_branch)
    rendered = rendered.replace("{FEATURE_BRANCH}", exec_ctx.feature_branch)
    rendered = rendered.replace("{PRIMARY_REPO_ROOT}", str(exec_ctx.primary_repo_root))
    rendered = rendered.replace("{EXECUTION_REPO_ROOT}", str(exec_ctx.execution_repo_root))
    rendered = rendered.replace("{FEATURE_WORKTREE_PATH}", worktree_path_str)
    return rendered


def _build_merge_user_prompt(
    wf: WorkflowConfig,
    workflow_config: WorkflowUserConfig,
    *,
    exec_ctx: ExecutionContext,
    config_dir: Path,
    working_dir: Path,
    original_plan_path: Path,
    active_plan_path: Path,
    new_plan_path: Path,
) -> str:
    parts = [_MERGE_BUILTIN_INSTRUCTION]
    for prompt_key in (wf.merge_prompt or ()):
        if prompt_key not in workflow_config.prompts:
            raise WorkflowError(f"merge_prompt references unknown prompt '{prompt_key}'")
        raw = workflow_config.prompts[prompt_key]
        rendered = render_merge_prompt(
            raw,
            config_dir=config_dir,
            working_dir=working_dir,
            exec_ctx=exec_ctx,
            original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
        )
        parts.append(rendered)
    return "\n\n".join(parts)


def _verify_merge_success(
    primary_root: Path,
    main_branch: str,
    feature_branch: str,
    *,
    original_plan_path: Path | None = None,
) -> str | None:
    """Returns None on success, or a description of which check failed."""
    rc, out, _ = _run_git(["ls-files", "--unmerged"], cwd=primary_root)
    if rc != 0 or out.strip():
        return "unmerged index entries remain after merge"

    rc, out, _ = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=primary_root,
    )
    if rc != 0:
        return "working tree is not clean after merge"
    dirty_lines = [
        line for line in out.splitlines()
        if line.strip()
        and not _is_ignored_merge_status_line(
            line,
            primary_root=primary_root,
            original_plan_path=original_plan_path,
        )
    ]
    if dirty_lines:
        return "working tree is not clean after merge"

    rc, head_ref, _ = _run_git(["symbolic-ref", "HEAD"], cwd=primary_root)
    if rc != 0 or head_ref.strip() != f"refs/heads/{main_branch}":
        return f"HEAD is not on '{main_branch}' after merge (got '{head_ref.strip()}')"

    rc, _, _ = _run_git(
        ["merge-base", "--is-ancestor", feature_branch, main_branch],
        cwd=primary_root,
    )
    if rc != 0:
        _, main_head, _ = _run_git(["rev-parse", main_branch], cwd=primary_root)
        _, feature_head, _ = _run_git(["rev-parse", feature_branch], cwd=primary_root)
        return (
            f"feature branch '{feature_branch}' is not an ancestor of '{main_branch}' after merge "
            f"(main={main_head or 'unknown'}, feature={feature_head or 'unknown'})"
        )

    return None


def _try_fast_forward_merge(
    exec_ctx: ExecutionContext,
) -> subprocess.CompletedProcess[str] | None:
    primary_root = exec_ctx.primary_repo_root

    rc, head_ref, err = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=primary_root)
    if rc != 0:
        raise WorkflowError(
            f"merge teardown requires the primary checkout to be on '{exec_ctx.main_branch}': "
            f"{err or head_ref or 'detached HEAD'}"
        )
    if head_ref.strip() != exec_ctx.main_branch:
        raise WorkflowError(
            f"merge teardown requires the primary checkout to be on '{exec_ctx.main_branch}' "
            f"(got '{head_ref.strip()}')"
        )

    rc, _, _ = _run_git(
        ["merge-base", "--is-ancestor", exec_ctx.main_branch, exec_ctx.feature_branch],
        cwd=primary_root,
    )
    if rc != 0:
        return None

    merge_args = ["merge", "--ff-only", exec_ctx.feature_branch]
    merge_rc, merge_out, merge_err = _run_git(merge_args, cwd=primary_root)
    if merge_rc != 0:
        raise WorkflowError(
            f"lifecycle teardown: fast-forward merge of '{exec_ctx.feature_branch}' into "
            f"'{exec_ctx.main_branch}' failed: {merge_err or merge_out or 'unknown git error'}"
        )

    return subprocess.CompletedProcess(
        ["git", *merge_args],
        merge_rc,
        merge_out,
        merge_err,
    )


def _is_ignored_merge_status_line(
    line: str,
    *,
    primary_root: Path,
    original_plan_path: Path | None,
) -> bool:
    if len(line) < 3:
        return False
    xy = line[:2]
    path = line[3:] if len(line) >= 4 and line[2] == " " else line[2:]
    path = path.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    path = path.strip('"')
    if xy == "??" and (
        path == ".aflow"
        or path.startswith(".aflow/")
        or path == "plans/backups"
        or path.startswith("plans/backups/")
    ):
        return True
    if original_plan_path is None:
        return False
    try:
        rel = original_plan_path.resolve().relative_to(primary_root.resolve()).as_posix()
    except ValueError:
        return False
    return path == rel


def _rm_worktree_safe(primary_root: Path, worktree_path: Path) -> None:
    rc, _, err = _run_git(
        ["worktree", "remove", "--force", str(worktree_path)],
        cwd=primary_root,
    )
    if rc != 0:
        raise WorkflowError(
            f"lifecycle teardown: failed to remove worktree '{worktree_path}': {err}"
        )


def _validate_worktree_resume_context(
    primary_root: Path,
    resume_ctx: ResumeContext,
) -> None:
    """Validate that a recorded worktree execution context is safe to resume.

    Verifies:
    - The recorded feature_branch exists locally
    - The recorded worktree_path exists and is a directory
    - The worktree_path is still registered in git worktree list
    - The recorded main_branch still exists locally
    - No in-progress git operation is active in the worktree

    Raises WorkflowError if any validation fails.
    """
    # Verify feature branch exists locally
    rc, _, err = _run_git(
        ["rev-parse", "--verify", f"refs/heads/{resume_ctx.feature_branch}"],
        cwd=primary_root,
    )
    if rc != 0:
        raise WorkflowError(
            f"resume validation: feature branch '{resume_ctx.feature_branch}' does not exist locally"
        )

    # Verify worktree path exists and is a directory
    if not resume_ctx.worktree_path.exists():
        raise WorkflowError(
            f"resume validation: worktree path '{resume_ctx.worktree_path}' does not exist on disk"
        )
    if not resume_ctx.worktree_path.is_dir():
        raise WorkflowError(
            f"resume validation: worktree path '{resume_ctx.worktree_path}' is not a directory"
        )

    # Verify worktree is still registered in git worktree list
    rc, wt_list, _ = _run_git(["worktree", "list", "--porcelain"], cwd=primary_root)
    if rc == 0:
        worktree_registered = False
        for line in wt_list.splitlines():
            if line.startswith("worktree "):
                registered_path = line[len("worktree "):]
                if Path(registered_path).resolve() == resume_ctx.worktree_path.resolve():
                    worktree_registered = True
                    break
        if not worktree_registered:
            raise WorkflowError(
                f"resume validation: worktree path '{resume_ctx.worktree_path}' is not registered in git worktree list"
            )

    # Verify main branch exists locally
    rc, _, err = _run_git(
        ["show-ref", "--verify", f"refs/heads/{resume_ctx.main_branch}"],
        cwd=primary_root,
    )
    if rc != 0:
        raise WorkflowError(
            f"resume validation: main branch '{resume_ctx.main_branch}' does not exist locally"
        )

    # Check for in-progress git operations in the worktree
    # Need to find the .git directory for the worktree
    rc, git_dir, _ = _run_git(["rev-parse", "--git-dir"], cwd=resume_ctx.worktree_path)
    if rc == 0:
        worktree_git_dir = Path(git_dir)
        if not worktree_git_dir.is_absolute():
            worktree_git_dir = resume_ctx.worktree_path / worktree_git_dir

        # Check for merge conflicts
        if (worktree_git_dir / "MERGE_HEAD").exists():
            raise WorkflowError(
                f"resume validation: worktree '{resume_ctx.worktree_path}' has an in-progress merge (MERGE_HEAD exists)"
            )
        # Check for rebase in progress
        if (worktree_git_dir / "REBASE_HEAD").exists():
            raise WorkflowError(
                f"resume validation: worktree '{resume_ctx.worktree_path}' has an in-progress rebase (REBASE_HEAD exists)"
            )
        # Check for rebase-merge directory
        if (worktree_git_dir / "rebase-merge").exists():
            raise WorkflowError(
                f"resume validation: worktree '{resume_ctx.worktree_path}' has an in-progress rebase (rebase-merge exists)"
            )


def _execute_merge_handoff(
    exec_ctx: ExecutionContext,
    wf: WorkflowConfig,
    workflow_config: WorkflowUserConfig,
    *,
    team_name: str | None,
    adapter: HarnessAdapter | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    config_dir: Path,
    working_dir: Path,
    original_plan_path: Path,
    active_plan_path: Path,
    new_plan_path: Path,
    banner: BannerRenderer,
    state: ControllerState,
) -> subprocess.CompletedProcess[str]:
    primary_root = exec_ctx.primary_repo_root
    team_lead_role = workflow_config.aflow.team_lead
    if not team_lead_role:
        raise WorkflowError("merge teardown requires [aflow].team_lead to be configured")

    fast_forward_merge = _try_fast_forward_merge(exec_ctx)
    if fast_forward_merge is not None:
        return fast_forward_merge

    team_lead_selector = resolve_role_selector(
        team_lead_role, team_name, workflow_config, step_path="merge teardown"
    )
    resolved = resolve_profile(team_lead_selector, workflow_config, step_path="merge teardown")

    user_prompt = _build_merge_user_prompt(
        wf, workflow_config,
        exec_ctx=exec_ctx,
        config_dir=config_dir,
        working_dir=working_dir,
        original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
    )

    merge_adapter = adapter or get_adapter(resolved.harness_name)
    invocation = merge_adapter.build_invocation(
        repo_root=primary_root,
        model=resolved.model,
        system_prompt="",
        user_prompt=user_prompt,
        effort=resolved.effort,
    )

    if runner is None:
        return _run_process(invocation, primary_root, banner, state)
    return _run_injected_runner(runner, invocation, primary_root)


def _perform_merge_teardown(
    exec_ctx: ExecutionContext,
    wf: WorkflowConfig,
    workflow_config: WorkflowUserConfig,
    *,
    repo_root: Path,
    team_name: str | None,
    adapter: HarnessAdapter | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    config_dir: Path,
    working_dir: Path,
    original_plan_path: Path,
    active_plan_path: Path,
    new_plan_path: Path | None,
    banner: BannerRenderer,
    state: ControllerState,
) -> tuple[str, str | None]:
    prepared_primary_plan: _PreparedPrimaryPlanForMerge | None = None
    try:
        prepared_primary_plan = _prepare_primary_plan_for_merge(
            repo_root,
            original_plan_path,
        )
        _ensure_merge_handoff_clean(
            exec_ctx,
            original_plan_path=original_plan_path,
        )
        merge_completed = _execute_merge_handoff(
            exec_ctx,
            wf,
            workflow_config,
            team_name=team_name,
            adapter=adapter,
            runner=runner,
            config_dir=config_dir,
            working_dir=working_dir,
            original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path or original_plan_path,
            banner=banner,
            state=state,
        )
    except WorkflowError as exc:
        _restore_primary_plan_after_merge(prepared_primary_plan)
        return "failed", exc.summary

    stop_reason = _detect_stop_marker(
        merge_completed.stdout,
        merge_completed.stderr,
    )
    if stop_reason is not None:
        _restore_primary_plan_after_merge(prepared_primary_plan)
        return "failed", f"AFLOW_STOP: {stop_reason}"
    if merge_completed.returncode != 0:
        _restore_primary_plan_after_merge(prepared_primary_plan)
        return (
            "failed",
            f"merge agent exited with code {merge_completed.returncode}",
        )

    _restore_primary_plan_after_merge(prepared_primary_plan)
    check_failure = _verify_merge_success(
        repo_root,
        exec_ctx.main_branch,
        exec_ctx.feature_branch,
        original_plan_path=original_plan_path,
    )
    if check_failure is not None:
        return "failed", f"merge verification failed: {check_failure}"

    if "rm_worktree" in exec_ctx.teardown and exec_ctx.worktree_path is not None:
        try:
            _rm_worktree_safe(repo_root, exec_ctx.worktree_path)
        except WorkflowError as exc:
            return "failed", exc.summary
    return "success", None


def _emit_event(observer: ExecutionObserver | None, event: ExecutionEvent) -> None:
    """Emit an event to the observer if one is provided."""
    if observer is not None:
        observer.on_event(event)


def run_workflow(
    config: ControllerConfig,
    workflow_config: WorkflowUserConfig,
    workflow_name: str,
    *,
    parsed_plan: ParsedPlan | None = None,
    startup_retry: RetryContext | None = None,
    startup_base_head_refresh_sha: str | None = None,
    config_dir: Path,
    working_dir: Path | None = None,
    adapter: HarnessAdapter | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    banner: BannerRenderer | None = None,
    resume: ResumeContext | None = None,
    observer: ExecutionObserver | None = None,
) -> ControllerRunResult:
    if workflow_name not in workflow_config.workflows:
        raise WorkflowError(f"workflow '{workflow_name}' not found in config")

    wf = workflow_config.workflows[workflow_name]
    if wf.first_step is None:
        raise WorkflowError(f"workflow '{workflow_name}' has no steps")

    _emit_event(observer, RunStartedEvent.create(
        workflow_name=workflow_name,
        repo_root=config.repo_root,
        plan_path=config.plan_path,
        max_turns=config.max_turns,
        team=config.team,
        start_step=config.start_step,
    ))

    repo_state = probe_repo_state(config.repo_root)
    needs_bootstrap = _lifecycle_is_bootstrap_eligible(wf, repo_state)
    lifecycle_plan = None
    if resume is None:
        lifecycle_plan = _lifecycle_preflight(
            config.repo_root, config.plan_path, wf, workflow_config.aflow, repo_state,
            skip_phase_b=needs_bootstrap,
        )
    exec_ctx: ExecutionContext | None = None
    resumed_from_run_id = resume.resumed_from_run_id if resume else None

    original_plan_path = config.plan_path
    active_plan_path = original_plan_path
    current_step_name = (
        resume.interrupted_step_name
        if resume is not None and resume.interrupted_step_name is not None
        else config.start_step or wf.first_step
    )
    working_dir = working_dir or Path.cwd()

    # Resume discovery carries already validated bytes so pruning the source
    # run cannot turn a checked authority record into a late file read.
    resumed_envelope_bytes = resume.scope_envelope_bytes if resume is not None else None
    if resume is not None and resume.active_implementation_scope is not None:
        reference = _scope_envelope_reference(resume.active_implementation_scope)
        if reference is None:
            if resumed_envelope_bytes is not None:
                raise WorkflowError(
                    "cannot resume: legacy scope unexpectedly carries envelope bytes"
                )
        elif resumed_envelope_bytes is None:
            raise WorkflowError(
                "cannot resume: modern scope envelope bytes were not validated before startup"
            )
        else:
            _validate_scope_envelope_bytes(
                resume.active_implementation_scope,
                resumed_envelope_bytes,
            )

    preserve_resume_override_source = (
        resume is not None
        and resume.override_source_run_dir is not None
        and resume.override_source_run_dir.parent.resolve()
        == (config.repo_root / ".aflow" / "runs").resolve()
    )
    run_paths = create_run_paths(
        replace(config, keep_runs=config.keep_runs + 1)
        if preserve_resume_override_source
        else config
    )
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.run_id = run_paths.run_dir.name
    state.resumed_from_run_id = resumed_from_run_id
    state.frozen_run_identity = _freeze_run_identity(
        workflow_name,
        workflow_config,
        config_dir=config_dir,
    )
    state.effective_max_turns = (
        resume.effective_max_turns
        if resume is not None and resume.effective_max_turns is not None
        else config.max_turns
    )
    if resume is not None:
        state.override_result = resume.override_result
        state.pending_override_notes = resume.pending_override_notes
        state.override_source_run_dir = resume.override_source_run_dir
        state.override_file_present = resume.override_file_present
    state.status_message = "initializing"
    state.selected_start_step = config.start_step
    state.startup_recovery_used = startup_retry is not None
    state.startup_recovery_reason = startup_retry.parse_error_str if startup_retry is not None else None
    print(f"Run ID: {run_paths.run_dir.name}", file=sys.stderr)
    if resumed_from_run_id is not None:
        print(f"Resuming from: {resumed_from_run_id}", file=sys.stderr)
    write_run_metadata(
        run_paths, config, state, status="initializing",
        workflow_name=workflow_name, original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        resumed_from_run_id=resumed_from_run_id,
    )

    if banner is None:
        workflow_graph_source = WorkflowGraphSource(
            declared_steps=dict(wf.declared_steps),
            executable_steps=dict(wf.steps),
            excluded_step_names=wf.excluded_steps,
        )
        banner = _make_banner(
            config,
            workflow_steps=wf.steps,
            workflow_graph_source=workflow_graph_source,
            workflow_name=workflow_name,
            original_plan_path=original_plan_path,
            banner_files_limit=workflow_config.aflow.banner_files_limit,
        )
    banner.start(state)

    try:
        _backup_original_plan(config.repo_root, original_plan_path)
        if parsed_plan is None:
            parsed_plan = load_plan(original_plan_path)
    except WorkflowError as exc:
        state.status_message = "failed"
        banner.stop(state)
        summary = _format_failure(
            reason=exc.summary,
            run_dir=run_paths.run_dir,
            snapshot=PlanSnapshot(None, 0, 0, False),
        )
        write_run_metadata(
            run_paths, config, state, status="failed", failure_reason=summary,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )
        _emit_event(observer, RunFailedEvent.create(
            run_dir=run_paths.run_dir,
            turns_completed=0,
            failure_reason=summary,
            final_snapshot=PlanSnapshot(None, 0, 0, False),
            issues_accumulated=state.issues_accumulated,
            recovery_summary=state.current_harness_recovery,
            recovery_history=tuple(state.harness_recovery_history),
        ))
        raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc
    except (PlanParseError, FileNotFoundError) as exc:
        state.status_message = "failed"
        banner.stop(state)
        summary = _format_failure(
            reason=str(exc),
            run_dir=run_paths.run_dir,
            snapshot=PlanSnapshot(None, 0, 0, False),
        )
        write_run_metadata(
            run_paths, config, state, status="failed", failure_reason=summary,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )
        _emit_event(observer, RunFailedEvent.create(
            run_dir=run_paths.run_dir,
            turns_completed=0,
            failure_reason=summary,
            final_snapshot=PlanSnapshot(None, 0, 0, False),
            issues_accumulated=state.issues_accumulated,
            recovery_summary=state.current_harness_recovery,
            recovery_history=tuple(state.harness_recovery_history),
        ))
        raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc

    if _workflow_requires_git_tracking(wf, workflow_config):
        plan_text = original_plan_path.read_text(encoding="utf-8")
        if not plan_has_git_tracking(plan_text):
            state.status_message = "failed"
            banner.stop(state)
            summary = (
                f"workflow '{workflow_name}' requires a '## Git Tracking' section "
                f"in the original plan at '{original_plan_path}'"
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            resumed_from_run_id=resumed_from_run_id,
            )
            raise WorkflowError(summary, run_dir=run_paths.run_dir)

    original_snapshot = parsed_plan.snapshot

    def _abort_startup_base_head_refresh(reason: str) -> None:
        state.status_message = "failed"
        banner.stop(state)
        summary = _format_failure(
            reason=reason,
            run_dir=run_paths.run_dir,
            snapshot=original_snapshot,
        )
        write_run_metadata(
            run_paths, config, state, status="failed", failure_reason=summary,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )
        raise WorkflowError(summary, run_dir=run_paths.run_dir)

    try:
        startup_base_head_refresh_check = preflight_pre_handoff_base_head_refresh(
            config.repo_root,
            original_plan_path.read_text(encoding="utf-8"),
            parsed_plan,
        )
    except ValueError as exc:
        _abort_startup_base_head_refresh(str(exc))

    # When resuming a previously-started workflow, the target branch may have
    # advanced (e.g. a parallel workflow merged while this one was paused).
    # That is expected — the base head recorded at plan creation no longer
    # matches current HEAD, but the merge step handles divergence via rebase.
    # Only enforce base-head consistency for fresh starts, not resumes.
    effective_startup_base_head_refresh_sha = startup_base_head_refresh_sha

    if resume is None:
        if startup_base_head_refresh_check.status in {
            StartupBaseHeadRefreshStatus.NO_GIT_TRACKING,
            StartupBaseHeadRefreshStatus.NO_RESOLVABLE_HEAD,
            StartupBaseHeadRefreshStatus.MATCH,
        }:
            pass
        elif startup_base_head_refresh_check.status in {
            StartupBaseHeadRefreshStatus.MALFORMED,
            StartupBaseHeadRefreshStatus.EMPTY_BASE_STARTED,
            StartupBaseHeadRefreshStatus.MISMATCH_STARTED,
        }:
            _abort_startup_base_head_refresh(
                f"startup preflight rejected Pre-Handoff Base HEAD state: "
                f"{startup_base_head_refresh_check.status.value}"
            )
        else:
            if effective_startup_base_head_refresh_sha is None:
                effective_startup_base_head_refresh_sha = startup_base_head_refresh_check.current_head
            if startup_base_head_refresh_check.current_head != effective_startup_base_head_refresh_sha:
                _abort_startup_base_head_refresh(
                    "startup preflight refresh target does not match current HEAD for "
                    "Pre-Handoff Base HEAD refresh"
                )

        should_refresh_pre_handoff_base_head = (
            startup_base_head_refresh_check.status
            in {
                StartupBaseHeadRefreshStatus.EMPTY_BASE_PRISTINE,
                StartupBaseHeadRefreshStatus.MISMATCH_PRISTINE,
            }
            and effective_startup_base_head_refresh_sha is not None
        )
    else:
        should_refresh_pre_handoff_base_head = False

    state.last_snapshot = original_snapshot
    if startup_retry is not None:
        state.pending_retry = startup_retry
    write_run_metadata(
        run_paths, config, state, status="running", last_snapshot=original_snapshot,
        workflow_name=workflow_name, original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        resumed_from_run_id=resumed_from_run_id,
    )
    banner.update(state)

    done = original_snapshot.is_complete
    terminal_integration_only = bool(
        resume is not None and resume.terminal_integration_only
    )
    if done and not terminal_integration_only:
        prior_original_plan_path = original_plan_path
        finalized_original_plan_path = _finalize_original_plan_if_complete(
            config.repo_root,
            original_plan_path,
            snapshot=original_snapshot,
        )
        if finalized_original_plan_path != prior_original_plan_path:
            original_plan_path = finalized_original_plan_path
            if active_plan_path == prior_original_plan_path:
                active_plan_path = original_plan_path
        end_reason = _normalize_end_reason(already_complete=True)
        state.end_reason = end_reason
        state.status_message = "completed"
        banner.stop(state)
        result = ControllerRunResult(
            run_dir=run_paths.run_dir,
            turns_completed=0,
            final_snapshot=original_snapshot,
            issues_accumulated=state.issues_accumulated,
            end_reason=end_reason,
            recovery_summary=state.current_harness_recovery,
            recovery_history=tuple(state.harness_recovery_history),
        )
        write_run_metadata(
            run_paths, config, state, status="completed", last_snapshot=original_snapshot,
            end_reason=end_reason,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )

        _emit_event(observer, RunCompletedEvent.create(
            run_dir=run_paths.run_dir,
            turns_completed=0,
            final_snapshot=original_snapshot,
            end_reason=end_reason,
            issues_accumulated=state.issues_accumulated,
            recovery_summary=state.current_harness_recovery,
            recovery_history=tuple(state.harness_recovery_history),
        ))

        return result

    use_popen = runner is None
    new_plan_path: Path | None = None
    retry_limit = _effective_retry_limit(wf, workflow_config.aflow)
    active_team_name = config.team if config.team is not None else wf.team
    if active_team_name is not None and active_team_name not in workflow_config.teams:
        raise WorkflowError(
            f"workflow '{workflow_name}' references unknown team '{active_team_name}'"
        )
    baseline_team_name = active_team_name
    state.current_team = active_team_name
    state.current_team_override = None

    if resume is not None:
        state.manager_decision_number = resume.manager_decision_number
        state.manager_history = list(resume.manager_history)
        state.semantic_stall_count = resume.semantic_stall_count
        state.reviewer_rejection_count = resume.reviewer_rejection_count
        state.implementation_attempts = _mutable_implementation_attempts(
            resume.implementation_attempts
        )
        state.review_rejection_history = list(resume.review_rejection_history)
        state.active_implementation_scope = (
            replace(
                resume.active_implementation_scope,
                opened_turn_number=1,
                carried_reviewer_rejection_count=resume.reviewer_rejection_count,
            )
            if resume.active_implementation_scope is not None
            else None
        )
        state.pending_manager_notes = resume.pending_manager_notes
        state.pending_step_team_override = resume.pending_step_team_override
        state.pending_boundary_decision = resume.pending_boundary_decision
        state.pending_repartition = resume.pending_repartition
        state.repartition_history = list(resume.repartition_history)
        state.scope_pressure_reason = resume.scope_pressure_reason
        state.last_manager_report_path = resume.last_manager_report_path

        # create_run_paths may already have pruned the source run. Restore the
        # controller-owned transaction artifacts carried by ResumeContext
        # before attempting any reconciliation or harness launch.
        for relative_path, artifact_bytes in resume.repartition_artifact_bytes.items():
            destination = (run_paths.run_dir / relative_path).resolve()
            try:
                destination.relative_to(run_paths.run_dir.resolve())
            except ValueError as exc:
                raise WorkflowError(
                    "cannot resume: pending repartition artifact path escapes "
                    f"the new run directory: {relative_path}"
                ) from exc
            if destination.exists():
                if destination.read_bytes() != artifact_bytes:
                    raise WorkflowError(
                        "cannot resume: pending repartition artifact already "
                        f"exists with different bytes: {relative_path}"
                    )
            else:
                _atomic_replace_bytes(destination, artifact_bytes)

        # Carry the fully validated source artifact into the new run before
        # any harness or manager call.  Do not consult the old source path.
        if resumed_envelope_bytes is not None and state.active_implementation_scope is not None:
            scope = state.active_implementation_scope
            reference = _scope_envelope_reference(scope)
            if reference is None:
                raise WorkflowError(
                    "cannot resume: legacy scope unexpectedly carries envelope bytes"
                )
            artifact_path, _artifact_sha256, _canonical_sha256 = reference
            envelope_path = run_paths.run_dir / artifact_path
            envelope_path.parent.mkdir(parents=True, exist_ok=True)
            if not envelope_path.exists():
                import os as _os
                from uuid import uuid4 as _uuid4
                tmp = envelope_path.with_name(f".envelope.json.{_uuid4().hex}.tmp")
                try:
                    tmp_fd = _os.open(str(tmp), _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
                    with _os.fdopen(tmp_fd, "wb") as handle:
                        handle.write(resumed_envelope_bytes)
                        handle.flush()
                        _os.fsync(handle.fileno())
                    _os.link(tmp, envelope_path)
                except FileExistsError:
                    existing = envelope_path.read_bytes()
                    if existing != resumed_envelope_bytes:
                        raise WorkflowError(
                            "cannot resume: envelope artifact already exists with "
                            "different bytes in new run"
                        )
                finally:
                    try:
                        tmp.unlink()
                    except (FileNotFoundError, OSError):
                        pass
            else:
                existing = envelope_path.read_bytes()
                _validate_scope_envelope_bytes(scope, existing)
                if existing != resumed_envelope_bytes:
                    raise WorkflowError(
                        "cannot resume: envelope artifact already exists with "
                        "different bytes in new run"
                    )

            _validate_scope_envelope_bytes(scope, envelope_path.read_bytes())
        try:
            _validate_worktree_resume_context(config.repo_root, resume)
            exec_ctx = ExecutionContext(
                primary_repo_root=config.repo_root,
                execution_repo_root=resume.worktree_path,
                main_branch=resume.main_branch,
                feature_branch=resume.feature_branch,
                worktree_path=resume.worktree_path,
                setup=resume.setup,
                teardown=resume.teardown,
            )
            _sync_startup_plan_metadata_for_execution(
                original_plan_path,
                exec_ctx,
                startup_base_head_refresh_sha=(
                    effective_startup_base_head_refresh_sha if should_refresh_pre_handoff_base_head else None
                ),
            )
            if resume.active_plan_path is not None:
                active_plan_path = resume.active_plan_path
                active_execution_path = _exec_plan_path(active_plan_path, exec_ctx)
                if not active_execution_path.is_file():
                    if _resume_completed_worker_can_use_original_plan(
                        pending_turn=resume.pending_finalized_turn,
                        active_plan_path=active_plan_path,
                        original_plan_path=original_plan_path,
                        active_scope=state.active_implementation_scope,
                        exec_ctx=exec_ctx,
                    ):
                        active_plan_path = original_plan_path
                    else:
                        raise WorkflowError(
                            "cannot resume with the saved active plan because it does "
                            "not exist in the reused execution checkout: "
                            f"{active_execution_path}"
                        )
                resumed_scope = state.active_implementation_scope
                if (
                    active_execution_path.is_file()
                    and active_plan_path != original_plan_path
                    and resumed_scope is not None
                    and not resumed_scope.awaiting_review
                    and _original_checkpoint_advanced(
                        resumed_scope,
                        original_snapshot,
                    )
                ):
                    try:
                        resumed_active_snapshot = load_plan_tolerant(
                            active_execution_path
                        ).parsed_plan.snapshot
                    except (OSError, PlanParseError, ValueError):
                        resumed_active_snapshot = None
                    resumed_active_complete = (
                        resumed_active_snapshot.is_complete
                        if resumed_active_snapshot is not None
                        else plan_step_checklist_is_complete(
                            active_execution_path
                        )
                    )
                    if resumed_active_complete:
                        active_plan_path = original_plan_path
                        _close_implementation_scope(state)
        except WorkflowError as exc:
            state.status_message = "failed"
            banner.stop(state)
            summary = _format_failure(
                reason=exc.summary,
                run_dir=run_paths.run_dir,
                snapshot=original_snapshot,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                active_plan_path=active_plan_path,
                execution_context=exec_ctx,
                resumed_from_run_id=resumed_from_run_id,
            )
            raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc
    elif lifecycle_plan is not None:
        try:
            if needs_bootstrap:
                plan_text = original_plan_path.read_text(encoding="utf-8")
                readme_title, readme_body = derive_readme_content(
                    plan_text, original_plan_path.stem
                )
                bootstrap_result = _execute_init_repo_handoff(
                    config.repo_root,
                    workflow_config,
                    team_name=active_team_name,
                    adapter=adapter,
                    runner=runner,
                    main_branch=lifecycle_plan.main_branch,
                    readme_title=readme_title,
                    readme_body=readme_body,
                    banner=banner,
                    state=state,
                )
                stop_reason = _detect_stop_marker(
                    bootstrap_result.stdout, bootstrap_result.stderr
                )
                if stop_reason is not None:
                    raise WorkflowError(
                        f"lifecycle bootstrap: init-repo agent emitted AFLOW_STOP: {stop_reason}"
                    )
                if bootstrap_result.returncode != 0:
                    raise WorkflowError(
                        "lifecycle bootstrap: init-repo agent failed with exit code "
                        f"{bootstrap_result.returncode}"
                    )
                verify_failure = _verify_init_repo_success(
                    config.repo_root, lifecycle_plan.main_branch
                )
                if verify_failure:
                    raise WorkflowError(
                        f"lifecycle bootstrap verification failed: {verify_failure}"
                    )
                print(
                    f"aflow: lifecycle bootstrap succeeded at '{config.repo_root}' "
                    f"on branch '{lifecycle_plan.main_branch}'",
                    file=sys.stderr,
                )
                _lifecycle_preflight_git(
                    config.repo_root,
                    lifecycle_plan.main_branch,
                    lifecycle_plan.feature_branch,
                    "worktree" in (wf.setup or ()),
                    lifecycle_plan.worktree_path,
                    allow_untracked=True,
                )
            exec_ctx = _do_lifecycle_setup(config.repo_root, lifecycle_plan)
            _sync_startup_plan_metadata_for_execution(
                original_plan_path,
                exec_ctx,
                startup_base_head_refresh_sha=(
                    effective_startup_base_head_refresh_sha if should_refresh_pre_handoff_base_head else None
                ),
            )
        except WorkflowError as exc:
            state.status_message = "failed"
            banner.stop(state)
            summary = _format_failure(
                reason=exc.summary,
                run_dir=run_paths.run_dir,
                snapshot=original_snapshot,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                active_plan_path=active_plan_path,
                execution_context=exec_ctx,
                resumed_from_run_id=resumed_from_run_id,
            )
            raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc

    if exec_ctx is None:
        _sync_startup_plan_metadata_for_execution(
            original_plan_path,
            None,
            startup_base_head_refresh_sha=(
                effective_startup_base_head_refresh_sha if should_refresh_pre_handoff_base_head else None
            ),
        )

    pending_boundary = state.pending_boundary_decision
    if pending_boundary is not None and not pending_boundary.consumed:
        if pending_boundary.resolved_next_step is not None:
            current_step_name = pending_boundary.resolved_next_step
        if pending_boundary.post_transition_active_plan_path is not None:
            restored_active_plan = Path(pending_boundary.post_transition_active_plan_path)
            if restored_active_plan.is_file():
                active_plan_path = restored_active_plan

    execution_repo_root = exec_ctx.execution_repo_root if exec_ctx else config.repo_root
    write_run_metadata(
        run_paths,
        config,
        state,
        status="running",
        execution_context=exec_ctx,
        last_snapshot=state.last_snapshot,
        workflow_name=workflow_name,
        original_plan_path=original_plan_path,
        current_step_name=current_step_name,
        active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
        resumed_from_run_id=resumed_from_run_id,
    )

    def _record_issue(
        kind: str,
        message: str,
        *,
        turn_dir: Path | None = None,
    ) -> str | None:
        state.issues_accumulated += 1
        current_turn = state.turn_history[-1] if state.turn_history else None
        resolved_turn_dir = turn_dir
        if resolved_turn_dir is None and current_turn is not None:
            resolved_turn_dir = current_turn.turn_dir
        turn_number = state.active_turn or (current_turn.turn_number if current_turn is not None else None)
        issue_record = IssueRecord(
            issue_number=state.issues_accumulated,
            kind=kind,
            message=message,
            turn_number=turn_number,
            turn_dir=(
                str(resolved_turn_dir.relative_to(run_paths.repo_root))
                if resolved_turn_dir is not None
                else None
            ),
            result_artifact_path=(
                str((resolved_turn_dir / "result.json").relative_to(run_paths.repo_root))
                if resolved_turn_dir is not None
                else None
            ),
            stdout_artifact_path=(
                str((resolved_turn_dir / "stdout.txt").relative_to(run_paths.repo_root))
                if resolved_turn_dir is not None
                else None
            ),
            stderr_artifact_path=(
                str((resolved_turn_dir / "stderr.txt").relative_to(run_paths.repo_root))
                if resolved_turn_dir is not None
                else None
            ),
        )
        state.issue_history.append(issue_record)
        issue_summary_path = write_issue_summary(run_paths, state)
        if current_turn is not None:
            current_turn.issues_summary_path = issue_summary_path
        return issue_summary_path

    def _raise_pre_turn_failure(
        *,
        reason: str,
        snapshot: PlanSnapshot,
        active_path: Path,
        new_path: Path | None,
    ) -> None:
        state.status_message = "failed"
        banner.stop(state)
        summary = _format_failure(
            reason=reason,
            run_dir=run_paths.run_dir,
            snapshot=snapshot,
        )
        write_run_metadata(
            run_paths, config, state, status="failed", failure_reason=summary,
            execution_context=exec_ctx,
            last_snapshot=state.last_snapshot,
            turns_completed=state.turns_completed,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            current_step_name=current_step_name, active_plan_path=active_path,
            new_plan_path=new_path,
            resumed_from_run_id=resumed_from_run_id,
        )
        raise WorkflowError(summary, run_dir=run_paths.run_dir)

    def _start_turn(
        *,
        turn_number: int,
        step_name: str,
        step: WorkflowStepConfig,
        step_role: str,
        resolved_selector: str,
        resolved: ResolvedProfile,
        active_path: Path,
        new_path: Path,
        invocation: HarnessInvocation,
        snapshot_before: PlanSnapshot,
    ) -> tuple[Path, datetime]:
        started_at = datetime.now(timezone.utc)
        state.active_turn = turn_number
        state.current_turn_started_at = started_at
        scope = state.active_implementation_scope
        triggering_rejection_number = None
        if step_role == "worker" and scope is not None:
            matching_rejections = [
                item for item in state.review_rejection_history
                if item.scope_id == scope.scope_id
            ]
            if matching_rejections:
                triggering_rejection_number = max(
                    item.rejection_number for item in matching_rejections
                )
        state.turn_history.append(
            TurnRecord(
                turn_number=turn_number,
                step_name=step_name,
                step_role=step_role,
                resolved_selector=resolved_selector,
                resolved_harness_name=resolved.harness_name,
                resolved_model_display=format_harness_model_display(
                    resolved.harness_name,
                    resolved.model,
                    resolved.effort,
                ),
                active_plan_path=str(active_path),
                triggering_rejection_number=triggering_rejection_number,
                started_at=started_at,
            )
        )

        _emit_event(observer, TurnStartedEvent.create(
            turn_number=turn_number,
            step_name=step_name,
            step_role=step_role,
            resolved_harness_name=resolved.harness_name,
            resolved_model_display=format_harness_model_display(
                resolved.harness_name,
                resolved.model,
                resolved.effort,
            ),
        ))

        banner.set_context(
            current_step_name=step_name,
            active_plan_path=active_path,
            new_plan_path=new_path if new_path.is_file() else None,
            config_harness=resolved.harness_name,
            config_model=resolved.model,
            config_effort=resolved.effort,
        )
        _emit_event(observer, StatusChangedEvent.create(
            status_message=f"running turn {turn_number}: {step_name}",
            turns_completed=turn_number - 1,
            active_turn=turn_number,
            current_step_name=step_name,
        ))
        turn_dir = write_turn_artifacts_start(
            run_paths,
            turn_number=turn_number,
            invocation=invocation,
            snapshot_before=snapshot_before,
            started_at=started_at,
            status="starting",
            step_name=step_name,
            step_role=step_role,
            selector=resolved_selector,
            original_plan_path=original_plan_path,
            active_plan_path=active_path,
            new_plan_path=new_path if new_path.is_file() else None,
        )
        state.turn_history[-1].turn_dir = turn_dir
        banner.update(state)
        return turn_dir, started_at

    def _finalize_turn_record(
        *,
        status: str,
        started_at: datetime,
        snapshot_before: PlanSnapshot,
        snapshot_after: PlanSnapshot | None,
        invocation: HarnessInvocation,
        turn_dir: Path,
        stdout: str,
        stderr: str,
        returncode: int,
        error: str | None = None,
        step_name: str | None = None,
        step_role: str | None = None,
        selector: str | None = None,
        active_path: Path | None = None,
        new_path: Path | None = None,
        conditions: dict[str, bool] | None = None,
        chosen_transition: str | None = None,
        chosen_transition_condition: str | None = None,
        end_reason: WorkflowEndReason | None = None,
        retry_attempt: int | None = None,
        retry_limit_value: int | None = None,
        retry_reason: str | None = None,
        retry_next_turn: bool | None = None,
        was_retry: bool | None = None,
        recovery: HarnessRecoveryContext | None = None,
        review_rejection: ReviewRejectionRecord | None = None,
    ) -> None:
        record = state.turn_history[-1]
        normalized_status = (
            "completed"
            if status in {"running", "completed"} and returncode == 0
            else status
        )
        finished_at = datetime.now(timezone.utc)
        duration_seconds = (finished_at - started_at).total_seconds()
        if record.active_plan_path is None and active_path is not None:
            record.active_plan_path = str(active_path)
        if chosen_transition is not None:
            record.chosen_transition = chosen_transition
        if chosen_transition_condition is not None:
            record.chosen_transition_condition = chosen_transition_condition
        finalize_turn_artifacts(
            turn_dir,
            turn_number=state.active_turn,
            invocation=invocation,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            status=normalized_status,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            error=error,
            step_name=step_name,
            step_role=step_role,
            selector=selector,
            original_plan_path=original_plan_path,
            active_plan_path=Path(record.active_plan_path) if record.active_plan_path is not None else active_path,
            new_plan_path=new_path,
            conditions=conditions,
            chosen_transition=chosen_transition,
            chosen_transition_condition=chosen_transition_condition,
            issues_summary_path=record.issues_summary_path,
            end_reason=end_reason,
            retry_attempt=retry_attempt,
            retry_limit=retry_limit_value,
            retry_reason=retry_reason,
            retry_next_turn=retry_next_turn,
            was_retry=was_retry,
            recovery=recovery,
            review_rejection=(asdict(review_rejection) if review_rejection is not None else None),
        )
        record.turn_dir = turn_dir
        record.stdout_artifact_path = _turn_artifact_display_path(run_paths.repo_root, turn_dir, "stdout.txt")
        record.stderr_artifact_path = _turn_artifact_display_path(run_paths.repo_root, turn_dir, "stderr.txt")
        record.outcome = normalized_status
        record.finished_at = finished_at
        record.duration_seconds = duration_seconds

        _emit_event(observer, TurnFinishedEvent.create(
            turn_number=state.active_turn,
            step_name=step_name or record.step_name,
            outcome=record.outcome,
            duration_seconds=record.duration_seconds,
            stdout_artifact_path=record.stdout_artifact_path,
            stderr_artifact_path=record.stderr_artifact_path,
            returncode=returncode,
            error=error,
            recovery=recovery,
        ))

    def _handle_harness_recovery(
        *,
        turn_number: int,
        step_name: str,
        step: WorkflowStepConfig,
        step_path: str,
        active_team_name: str | None,
        selector: str,
        resolved: ResolvedProfile,
        invocation: HarnessInvocation,
        turn_dir: Path,
        started_at: datetime,
        snapshot_before: PlanSnapshot,
        snapshot_after: PlanSnapshot,
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> bool:
        recovery_config = workflow_config.error_handling.harness_error_recovery
        team_lead_role = workflow_config.aflow.team_lead

        def _supervise_scheduled_recovery() -> None:
            """Offer the already-finalized operational retry to the manager."""
            if not workflow_config.manager.enabled:
                return
            backup_team, _ = resolve_backup_team(active_team_name, workflow_config.teams)
            backup_selector: str | None = None
            if backup_team is not None:
                try:
                    backup_selector, _ = _resolve_step_runtime(
                        step, workflow_config, team_name=backup_team,
                        step_path=step_path,
                    )
                except WorkflowError:
                    backup_selector = None
            _manager_gate(
                proposed_transition=step_name, current_step=step_name,
                current_role=step.role, active_team=active_team_name,
                active_selector=selector, post_transition_active_path=active_plan_path,
                trigger="harness_recovery", proposed_action="recovery",
                safely_retryable=True, operational_failure=True,
                backup_team=backup_team, backup_selector=backup_selector,
            )

        def _finalize_team_lead_failure(
            *,
            action: HarnessRecoveryAction,
            reason: str,
            delay_seconds: int | None,
            to_team: str | None,
            suggested_keywords: tuple[str, ...],
            suggested_action: HarnessRecoveryAction | None,
            rejection_reason: str | None,
            executed: bool,
        ) -> None:
            recovery = build_recovery_context(
                source="team_lead",
                action=action,
                reason=reason,
                match_terms=(),
                matched_terms=(),
                delay_seconds=delay_seconds,
                from_team=active_team_name,
                to_team=to_team,
                consecutive_count=state.consecutive_harness_recoveries + 1,
                suggested_keywords=suggested_keywords,
                suggested_action=suggested_action,
                executed=executed,
                rejection_reason=rejection_reason,
            )
            state.current_harness_recovery = recovery
            state.harness_recovery_history.append(recovery)
            state.consecutive_harness_recoveries = recovery.consecutive_count
            _record_issue("recovery-failed", reason, turn_dir=turn_dir)
            state.status_message = "failed"
            _finalize_turn_record(
                status="recovery-failed",
                started_at=started_at,
                snapshot_before=snapshot_before,
                snapshot_after=snapshot_after,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                error=reason,
                step_name=step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions={
                    "DONE": snapshot_after.is_complete,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                },
                recovery=recovery,
            )
            summary = _format_failure(
                reason=reason,
                run_dir=run_paths.run_dir,
                snapshot=snapshot_after,
            )
            write_run_metadata(
                run_paths,
                config,
                state,
                status="failed",
                failure_reason=summary,
                turns_completed=state.turns_completed,
                last_snapshot=snapshot_after,
                execution_context=exec_ctx,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir)

        def _schedule_team_lead_recovery(
            *,
            decision: TeamLeadRecoveryDecision,
            to_team: str | None,
            reason: str,
            rejection_reason: str | None = None,
        ) -> bool:
            delay = decision.delay_seconds
            recovery = build_recovery_context(
                source="team_lead",
                action=decision.action,
                reason=reason,
                match_terms=matched_rule.match if matched_rule is not None else (),
                matched_terms=matched_terms,
                delay_seconds=delay,
                from_team=active_team_name,
                to_team=to_team,
                consecutive_count=state.consecutive_harness_recoveries + 1,
                suggested_keywords=decision.suggested_keywords,
                suggested_action=decision.suggested_action,
                executed=True,
                rejection_reason=rejection_reason,
            )
            state.current_harness_recovery = recovery
            state.harness_recovery_history.append(recovery)
            state.consecutive_harness_recoveries = recovery.consecutive_count
            _record_issue("recovery-scheduled", reason, turn_dir=turn_dir)
            state.turns_completed += 1
            state.last_snapshot = snapshot_after
            if delay is not None and delay > 0:
                time.sleep(delay)
            _finalize_turn_record(
                status="recovery-scheduled",
                started_at=started_at,
                snapshot_before=snapshot_before,
                snapshot_after=snapshot_after,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                error=reason,
                step_name=step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions={
                    "DONE": snapshot_after.is_complete,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                },
                recovery=recovery,
            )
            _supervise_scheduled_recovery()
            write_run_metadata(
                run_paths,
                config,
                state,
                status="running",
                turns_completed=state.turns_completed,
                last_snapshot=state.last_snapshot,
                execution_context=exec_ctx,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.update(state)
            return True

        matched_rule, matched_terms = find_first_matching_rule(
            recovery_config,
            stdout=stdout,
            stderr=stderr,
            error=None,
        )
        if recovery_made_progress(snapshot_before, snapshot_after):
            return False
        if matched_rule is None:
            if returncode == 0:
                return False
            # Manager supervision owns unmatched operational incidents.  Do
            # not invoke the older team-lead handoff first: the finalized turn
            # below is routed as one Full, stop-only boundary instead.
            if workflow_config.manager.enabled:
                return False
            if team_lead_role is None:
                return False
            fallback_reason = (
                f"no deterministic harness recovery rule matched in {step_path}; "
                "escalating to the team lead"
            )
            backup_team, _ = resolve_backup_team(active_team_name, workflow_config.teams)
            try:
                recovery_repo_root = exec_ctx.primary_repo_root if exec_ctx is not None else run_paths.repo_root
                decision = _run_team_lead_recovery_handoff(
                    recovery_repo_root,
                    workflow_config,
                    team_name=active_team_name,
                    adapter=adapter,
                    runner=runner,
                    banner=banner,
                    state=state,
                    step_path=f"harness recovery for {step_path}",
                    current_team=active_team_name,
                    active_selector=selector,
                    harness_name=resolved.harness_name,
                    model=resolved.model,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    recovery_reason=fallback_reason,
                    recovery_cap=recovery_config.max_consecutive_recoveries,
                    consecutive_count=state.consecutive_harness_recoveries,
                    matched_rule_action=None,
                    matched_terms=(),
                    backup_team=backup_team,
                )
            except TeamLeadRecoveryDecisionError as exc:
                state.status_message = "failed"
                _record_issue("recovery-failed", str(exc), turn_dir=turn_dir)
                _finalize_turn_record(
                    status="recovery-failed",
                    started_at=started_at,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    invocation=invocation,
                    turn_dir=turn_dir,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    error=str(exc),
                    step_name=step_name,
                    step_role=step.role,
                    selector=selector,
                    active_path=active_plan_path,
                    new_path=new_plan_path,
                    conditions={
                        "DONE": snapshot_after.is_complete,
                        "NEW_PLAN_EXISTS": False,
                        "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                    },
                )
                summary = _format_failure(
                    reason=str(exc),
                    run_dir=run_paths.run_dir,
                    snapshot=snapshot_after,
                )
                write_run_metadata(
                    run_paths,
                    config,
                    state,
                    status="failed",
                    failure_reason=summary,
                    turns_completed=state.turns_completed,
                    last_snapshot=snapshot_after,
                    execution_context=exec_ctx,
                    workflow_name=workflow_name,
                    original_plan_path=original_plan_path,
                    current_step_name=current_step_name,
                    active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                banner.stop(state)
                raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc
            if decision.action == "retry_same_team_after_delay":
                return _schedule_team_lead_recovery(
                    decision=decision,
                    to_team=active_team_name,
                    reason=decision.reason,
                )
            if decision.action == "switch_to_backup_team_and_retry":
                backup_team, backup_reason = resolve_backup_team(active_team_name, workflow_config.teams)
                if backup_team is None:
                    return _finalize_team_lead_failure(
                        action=decision.action,
                        reason=f"{decision.reason}; {backup_reason or 'team lead requested a backup team that is not configured'}",
                        delay_seconds=decision.delay_seconds,
                        to_team=None,
                        suggested_keywords=decision.suggested_keywords,
                        suggested_action=decision.suggested_action,
                        rejection_reason=backup_reason,
                        executed=False,
                    )
                state.current_team_override = backup_team
                return _schedule_team_lead_recovery(
                    decision=decision,
                    to_team=backup_team,
                    reason=(
                        f"{decision.reason}; switching from team '{active_team_name}' to '{backup_team}'"
                    ),
                )
            if decision.action == "fail_immediately":
                return _finalize_team_lead_failure(
                    action=decision.action,
                    reason=decision.reason,
                    delay_seconds=decision.delay_seconds,
                    to_team=None,
                    suggested_keywords=decision.suggested_keywords,
                    suggested_action=decision.suggested_action,
                    rejection_reason=None,
                    executed=True,
                )

        if state.consecutive_harness_recoveries >= recovery_config.max_consecutive_recoveries:
            if team_lead_role is None:
                cap_reason = (
                    f"matched harness recovery rule in {step_path}: "
                    f"{matched_rule.action} on {', '.join(matched_terms)}; "
                    f"maximum consecutive recoveries "
                    f"({recovery_config.max_consecutive_recoveries}) reached"
                )
                recovery = build_recovery_context(
                    source="deterministic",
                    action=matched_rule.action,
                    reason=cap_reason,
                    match_terms=matched_rule.match,
                    matched_terms=matched_terms,
                    delay_seconds=matched_rule.delay_seconds,
                    from_team=active_team_name,
                    to_team=None,
                    consecutive_count=state.consecutive_harness_recoveries,
                )
                state.current_harness_recovery = recovery
                state.harness_recovery_history.append(recovery)
                _record_issue("recovery-failed", recovery.reason, turn_dir=turn_dir)
                state.status_message = "failed"
                _finalize_turn_record(
                    status="recovery-failed",
                    started_at=started_at,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    invocation=invocation,
                    turn_dir=turn_dir,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    error=recovery.reason,
                    step_name=step_name,
                    step_role=step.role,
                    selector=selector,
                    active_path=active_plan_path,
                    new_path=new_plan_path,
                    conditions={
                        "DONE": snapshot_after.is_complete,
                        "NEW_PLAN_EXISTS": False,
                        "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                    },
                    recovery=recovery,
                )
                summary = _format_failure(
                    reason=recovery.reason,
                    run_dir=run_paths.run_dir,
                    snapshot=snapshot_after,
                )
                write_run_metadata(
                    run_paths,
                    config,
                    state,
                    status="failed",
                    failure_reason=summary,
                    turns_completed=state.turns_completed,
                    last_snapshot=snapshot_after,
                    execution_context=exec_ctx,
                    workflow_name=workflow_name,
                    original_plan_path=original_plan_path,
                    current_step_name=current_step_name,
                    active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                banner.stop(state)
                raise WorkflowError(summary, run_dir=run_paths.run_dir)

            cap_reason = (
                f"matched harness recovery rule in {step_path}: "
                f"{matched_rule.action} on {', '.join(matched_terms)}; "
                f"maximum consecutive recoveries "
                f"({recovery_config.max_consecutive_recoveries}) reached"
            )
            backup_team, _ = resolve_backup_team(active_team_name, workflow_config.teams)
            try:
                recovery_repo_root = exec_ctx.primary_repo_root if exec_ctx is not None else run_paths.repo_root
                decision = _run_team_lead_recovery_handoff(
                    recovery_repo_root,
                    workflow_config,
                    team_name=active_team_name,
                    adapter=adapter,
                    runner=runner,
                    banner=banner,
                    state=state,
                    step_path=f"harness recovery for {step_path}",
                    current_team=active_team_name,
                    active_selector=selector,
                    harness_name=resolved.harness_name,
                    model=resolved.model,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    recovery_reason=cap_reason,
                    recovery_cap=recovery_config.max_consecutive_recoveries,
                    consecutive_count=state.consecutive_harness_recoveries,
                    matched_rule_action=matched_rule.action,
                    matched_terms=matched_terms,
                    backup_team=backup_team,
                )
            except TeamLeadRecoveryDecisionError as exc:
                state.status_message = "failed"
                _record_issue("recovery-failed", str(exc), turn_dir=turn_dir)
                _finalize_turn_record(
                    status="recovery-failed",
                    started_at=started_at,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    invocation=invocation,
                    turn_dir=turn_dir,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    error=str(exc),
                    step_name=step_name,
                    step_role=step.role,
                    selector=selector,
                    active_path=active_plan_path,
                    new_path=new_plan_path,
                    conditions={
                        "DONE": snapshot_after.is_complete,
                        "NEW_PLAN_EXISTS": False,
                        "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                    },
                )
                summary = _format_failure(
                    reason=str(exc),
                    run_dir=run_paths.run_dir,
                    snapshot=snapshot_after,
                )
                write_run_metadata(
                    run_paths,
                    config,
                    state,
                    status="failed",
                    failure_reason=summary,
                    turns_completed=state.turns_completed,
                    last_snapshot=snapshot_after,
                    execution_context=exec_ctx,
                    workflow_name=workflow_name,
                    original_plan_path=original_plan_path,
                    current_step_name=current_step_name,
                    active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                banner.stop(state)
                raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc
            if decision.action == "retry_same_team_after_delay":
                return _schedule_team_lead_recovery(
                    decision=decision,
                    to_team=active_team_name,
                    reason=decision.reason,
                )
            if decision.action == "switch_to_backup_team_and_retry":
                backup_team, backup_reason = resolve_backup_team(active_team_name, workflow_config.teams)
                if backup_team is None:
                    return _finalize_team_lead_failure(
                        action=decision.action,
                        reason=f"{decision.reason}; {backup_reason or 'team lead requested a backup team that is not configured'}",
                        delay_seconds=decision.delay_seconds,
                        to_team=None,
                        suggested_keywords=decision.suggested_keywords,
                        suggested_action=decision.suggested_action,
                        rejection_reason=backup_reason,
                        executed=False,
                    )
                state.current_team_override = backup_team
                return _schedule_team_lead_recovery(
                    decision=decision,
                    to_team=backup_team,
                    reason=(
                        f"{decision.reason}; switching from team '{active_team_name}' to '{backup_team}'"
                    ),
                )
            if decision.action == "fail_immediately":
                return _finalize_team_lead_failure(
                    action=decision.action,
                    reason=decision.reason,
                    delay_seconds=decision.delay_seconds,
                    to_team=None,
                    suggested_keywords=decision.suggested_keywords,
                    suggested_action=decision.suggested_action,
                    rejection_reason=None,
                    executed=True,
                )

        reason = (
            f"matched harness recovery rule in {step_path}: "
            f"{matched_rule.action} on {', '.join(matched_terms)}"
        )
        base_count = state.consecutive_harness_recoveries + 1
        recovery_source_team = active_team_name

        if matched_rule.action == "retry_same_team_after_delay":
            recovery = build_recovery_context(
                source="deterministic",
                action=matched_rule.action,
                reason=reason,
                match_terms=matched_rule.match,
                matched_terms=matched_terms,
                delay_seconds=matched_rule.delay_seconds,
                from_team=active_team_name,
                to_team=active_team_name,
                consecutive_count=base_count,
            )
            state.current_harness_recovery = recovery
            state.harness_recovery_history.append(recovery)
            state.consecutive_harness_recoveries = base_count
            _record_issue("recovery-scheduled", reason, turn_dir=turn_dir)
            state.turns_completed += 1
            state.last_snapshot = snapshot_after
            if matched_rule.delay_seconds > 0:
                time.sleep(matched_rule.delay_seconds)
            _finalize_turn_record(
                status="recovery-scheduled",
                started_at=started_at,
                snapshot_before=snapshot_before,
                snapshot_after=snapshot_after,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                error=reason,
                step_name=step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions={
                    "DONE": snapshot_after.is_complete,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                },
                recovery=recovery,
            )
            _supervise_scheduled_recovery()
            write_run_metadata(
                run_paths,
                config,
                state,
                status="running",
                turns_completed=state.turns_completed,
                last_snapshot=state.last_snapshot,
                execution_context=exec_ctx,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.update(state)
            return True

        if matched_rule.action == "switch_to_backup_team_and_retry":
            backup_team, backup_reason = resolve_backup_team(active_team_name, workflow_config.teams)
            if backup_team is None:
                failure_reason = backup_reason or (
                    f"team '{active_team_name}' does not configure a backup_team"
                )
                recovery = build_recovery_context(
                    source="deterministic",
                    action=matched_rule.action,
                    reason=failure_reason,
                    match_terms=matched_rule.match,
                    matched_terms=matched_terms,
                    delay_seconds=matched_rule.delay_seconds,
                    from_team=active_team_name,
                    to_team=None,
                    consecutive_count=base_count,
                )
                state.current_harness_recovery = recovery
                state.harness_recovery_history.append(recovery)
                state.consecutive_harness_recoveries = base_count
                _record_issue("recovery-failed", failure_reason, turn_dir=turn_dir)
                state.status_message = "failed"
                _finalize_turn_record(
                    status="recovery-failed",
                    started_at=started_at,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    invocation=invocation,
                    turn_dir=turn_dir,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    error=failure_reason,
                    step_name=step_name,
                    step_role=step.role,
                    selector=selector,
                    active_path=active_plan_path,
                    new_path=new_plan_path,
                    conditions={
                        "DONE": snapshot_after.is_complete,
                        "NEW_PLAN_EXISTS": False,
                        "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                    },
                    recovery=recovery,
                )
                summary = _format_failure(
                    reason=failure_reason,
                    run_dir=run_paths.run_dir,
                    snapshot=snapshot_after,
                )
                write_run_metadata(
                    run_paths,
                    config,
                    state,
                    status="failed",
                    failure_reason=summary,
                    turns_completed=state.turns_completed,
                    last_snapshot=snapshot_after,
                    execution_context=exec_ctx,
                    workflow_name=workflow_name,
                    original_plan_path=original_plan_path,
                    current_step_name=current_step_name,
                    active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                banner.stop(state)
                raise WorkflowError(summary, run_dir=run_paths.run_dir)

            state.current_team_override = backup_team
            if matched_rule.delay_seconds > 0:
                time.sleep(matched_rule.delay_seconds)
            recovery = build_recovery_context(
                source="deterministic",
                action=matched_rule.action,
                reason=(
                    f"{reason}; switching from team '{recovery_source_team}' to '{backup_team}'"
                ),
                match_terms=matched_rule.match,
                matched_terms=matched_terms,
                delay_seconds=matched_rule.delay_seconds,
                from_team=recovery_source_team,
                to_team=backup_team,
                consecutive_count=base_count,
            )
            state.current_harness_recovery = recovery
            state.harness_recovery_history.append(recovery)
            state.consecutive_harness_recoveries = base_count
            _record_issue("recovery-scheduled", recovery.reason, turn_dir=turn_dir)
            state.turns_completed += 1
            state.last_snapshot = snapshot_after
            _finalize_turn_record(
                status="recovery-scheduled",
                started_at=started_at,
                snapshot_before=snapshot_before,
                snapshot_after=snapshot_after,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                error=recovery.reason,
                step_name=step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions={
                    "DONE": snapshot_after.is_complete,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                },
                recovery=recovery,
            )
            _supervise_scheduled_recovery()
            write_run_metadata(
                run_paths,
                config,
                state,
                status="running",
                turns_completed=state.turns_completed,
                last_snapshot=state.last_snapshot,
                execution_context=exec_ctx,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.update(state)
            return True

        if matched_rule.action == "fail_immediately":
            recovery = build_recovery_context(
                source="deterministic",
                action=matched_rule.action,
                reason=reason,
                match_terms=matched_rule.match,
                matched_terms=matched_terms,
                delay_seconds=matched_rule.delay_seconds,
                from_team=active_team_name,
                to_team=None,
                consecutive_count=base_count,
            )
            state.current_harness_recovery = recovery
            state.harness_recovery_history.append(recovery)
            state.consecutive_harness_recoveries = base_count
            _record_issue("recovery-failed", recovery.reason, turn_dir=turn_dir)
            state.status_message = "failed"
            _finalize_turn_record(
                status="recovery-failed",
                started_at=started_at,
                snapshot_before=snapshot_before,
                snapshot_after=snapshot_after,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                error=recovery.reason,
                step_name=step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions={
                    "DONE": snapshot_after.is_complete,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": turn_number >= config.max_turns,
                },
                recovery=recovery,
            )
            summary = _format_failure(
                reason=recovery.reason,
                run_dir=run_paths.run_dir,
                snapshot=snapshot_after,
            )
            write_run_metadata(
                run_paths,
                config,
                state,
                status="failed",
                failure_reason=summary,
                turns_completed=state.turns_completed,
                last_snapshot=snapshot_after,
                execution_context=exec_ctx,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir)

        return False

    def _manager_repo_fingerprint() -> tuple[str, str, tuple[tuple[str, str], ...]]:
        """Capture only repository state that a read-only manager may not alter."""
        _, head, _ = _run_git(["rev-parse", "HEAD"], cwd=execution_repo_root)
        _, status, _ = _run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"], cwd=execution_repo_root
        )
        plan_hashes: list[tuple[str, str]] = []
        for plan_path in (original_plan_path, active_plan_path):
            try:
                plan_hashes.append((str(plan_path), hashlib.sha256(plan_path.read_bytes()).hexdigest()))
            except OSError:
                plan_hashes.append((str(plan_path), "<missing>"))
        return head.strip(), status, tuple(plan_hashes)

    def _protected_repartition_fingerprint() -> tuple[
        tuple[str, str, tuple[tuple[str, str], ...]],
        tuple[tuple[str, str], ...],
    ]:
        """Capture repository state plus every protected current-run artifact."""
        artifact_hashes: list[tuple[str, str]] = []
        run_dir = run_paths.run_dir
        if not run_dir.is_dir():
            return _manager_repo_fingerprint(), ((".", "<missing-run-directory>"),)

        def _record_walk_error(exc: OSError) -> None:
            error_path = Path(exc.filename) if exc.filename else run_dir
            try:
                relative = error_path.relative_to(run_dir).as_posix()
            except ValueError:
                relative = "."
            artifact_hashes.append(
                (relative or ".", f"<unreadable:{type(exc).__name__}>")
            )

        for root, directory_names, file_names in os.walk(
            run_dir, onerror=_record_walk_error, followlinks=False,
        ):
            directory_names.sort()
            file_names.sort()
            root_path = Path(root)
            for file_name in file_names:
                artifact_path = root_path / file_name
                relative = artifact_path.relative_to(run_dir).as_posix()
                try:
                    if not artifact_path.is_file():
                        if not artifact_path.exists():
                            artifact_hashes.append((relative, "<missing>"))
                        continue
                    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                except FileNotFoundError:
                    digest = "<missing>"
                except OSError as exc:
                    digest = f"<unreadable:{type(exc).__name__}>"
                artifact_hashes.append((relative, digest))

        return _manager_repo_fingerprint(), tuple(sorted(artifact_hashes))

    def _write_manager_report(
        context: dict[str, object], *, reason: str, decision: ManagerDecisionV1 | None = None
    ) -> str:
        report = render_manager_stop_report(
            context=context,
            stop_report=decision.stop_report if decision is not None else None,
            failure_reason=reason,
        )
        pending = state.pending_repartition
        if pending is not None:
            stage_actions = {
                "decided": (
                    "Resume the run to retry the bounded proposal cycle, or reset "
                    "the active scope if its authoritative envelope is no longer valid."
                ),
                "proposed": (
                    "Inspect the proposal and attempt result artifacts, correct the "
                    "reported proposal failure, then resume from durable state."
                ),
                "mechanically_validated": (
                    "Inspect the candidate and mechanical-validation artifacts, then "
                    "resume semantic validation from the preserved transaction."
                ),
                "semantically_validated": (
                    "Restore any missing plan copy or resolve the reported plan-copy "
                    "divergence, then resume application without rerunning Full."
                ),
                "execution_plan_applied": (
                    "Reconcile the primary plan copy to the verified candidate, then "
                    "resume the persisted multi-copy application."
                ),
                "primary_plan_applied": (
                    "Resume to verify both plan copies and finish post-split routing."
                ),
                "applied": (
                    "Resume to launch the recorded target step for the current partition."
                ),
                "failed": (
                    "Inspect the named failed-stage artifacts; resume only after "
                    "correcting the reported cause or explicitly reset the active scope."
                ),
            }
            artifact_references = {
                "latest attempt": pending.latest_attempt_path,
                "proposal": pending.proposal_artifact_path,
                "candidate": pending.candidate_artifact_path,
                "mechanical validation": (
                    pending.mechanical_validation_artifact_path
                ),
                "semantic verdict": pending.semantic_verdict_artifact_path,
            }
            report += "\n".join((
                "",
                "## Repartition recovery evidence",
                f"- Pending stage: {pending.stage}",
                f"- Failed stage: {pending.failed_stage or 'none'}",
                f"- Failure reason: {pending.failure_reason or reason}",
                f"- Scope pressure: {state.scope_pressure_reason or 'not recorded'}",
                f"- Scope ID: {pending.scope_id}",
                f"- Generation: {pending.generation_id or 'not assigned'}",
                f"- Envelope SHA-256: {pending.envelope_sha256}",
                f"- Source plan SHA-256: {pending.source_plan_sha256}",
                f"- Proposal SHA-256: {pending.proposal_sha256 or 'not assigned'}",
                (
                    "- Candidate plan SHA-256: "
                    f"{pending.candidate_plan_sha256 or 'not assigned'}"
                ),
                (
                    "- Children: "
                    + (
                        "; ".join(pending.child_summaries)
                        if pending.child_summaries else "not assigned"
                    )
                ),
                (
                    "- Exact next action: "
                    + stage_actions.get(
                        pending.stage,
                        "Inspect run.json and the referenced artifacts before resuming.",
                    )
                ),
                *(
                    f"- {label.title()} artifact: {path}"
                    for label, path in artifact_references.items()
                    if path is not None
                ),
                "",
            ))
        path = run_paths.run_dir / "manager-report.md"
        path.write_text(report, encoding="utf-8")
        state.last_manager_report_path = "manager-report.md"
        return report

    def _fail_manager_gate(
        context: dict[str, object],
        *,
        reason: str,
        decision: ManagerDecisionV1 | None = None,
    ) -> None:
        report = _write_manager_report(context, reason=reason, decision=decision)
        state.status_message = "failed"
        write_run_metadata(
            run_paths, config, state, status="failed", failure_reason=report,
            last_snapshot=state.last_snapshot, turns_completed=state.turns_completed,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            current_step_name=current_step_name, active_plan_path=active_plan_path,
            new_plan_path=new_plan_path, resumed_from_run_id=resumed_from_run_id,
        )
        _emit_event(observer, RunFailedEvent.create(
            run_dir=run_paths.run_dir,
            turns_completed=state.turns_completed,
            failure_reason=report,
            final_snapshot=state.last_snapshot,
            issues_accumulated=state.issues_accumulated,
            recovery_summary=state.current_harness_recovery,
            recovery_history=tuple(state.harness_recovery_history),
        ))
        banner.stop(state)
        raise WorkflowError(report, run_dir=run_paths.run_dir)

    def _manager_level_for_boundary(context: dict[str, object]) -> str:
        controller = context.get("controller_state")
        if not isinstance(controller, dict):
            return "lite"
        stalled = int(controller.get("semantic_stall_count", 0) or 0)
        reviewer_failures = int(controller.get("reviewer_rejection_count", 0) or 0)
        trigger = context.get("trigger")
        scope_pressure = bool(controller.get("scope_pressure_detected"))
        if (
            stalled >= workflow_config.manager.full_after_stalled_turns
            or reviewer_failures >= 2
            or scope_pressure
            or trigger in {"explicit_stop", "invalid_plan", "ambiguous_failure", "same_step_cap", "max_turns", "merge_failure", "illegal_transition"}
        ):
            return "full"
        return "lite"

    def _run_manager_call(
        *,
        level: str,
        boundary: FinalizedTurnBoundary,
        proposed_target_plan: Path | None,
        retry_target_plan: Path | None,
        context_run_dir: Path | None = None,
    ) -> tuple[ManagerDecisionV1 | None, dict[str, object], str | None]:
        """Run and durably record one manager attempt without altering turn accounting."""
        decision_number = state.manager_decision_number + 1
        manager_plan_path = (
            Path(boundary.active_plan_path)
            if isinstance(boundary.active_plan_path, str)
            else active_plan_path
        )
        metadata = {
            "run_id": state.run_id,
            "team": baseline_team_name,
            "max_turns": config.max_turns,
            "turns_completed": state.turns_completed,
            "original_plan_path": str(original_plan_path),
            "active_plan_path": str(manager_plan_path),
            "current_step_name": current_step_name,
        }
        captured_active_plan = None
        try:
            captured_active_plan = _exec_plan_path(manager_plan_path, exec_ctx).read_text(encoding="utf-8")
        except OSError:
            pass

        def note_scope_for(plan_path: Path | None) -> dict[str, object] | None:
            if plan_path is None:
                return None
            content: str | None = None
            try:
                content = _exec_plan_path(plan_path, exec_ctx).read_text(
                    encoding="utf-8"
                )
            except OSError:
                pass
            return build_manager_note_scope(
                active_plan_identity=_target_plan_identity(plan_path),
                active_plan_content=content,
            )

        proposed_note_scope = note_scope_for(proposed_target_plan)
        retry_note_scope = note_scope_for(retry_target_plan)
        if retry_target_plan == proposed_target_plan:
            retry_note_scope = None
        fingerprint_before = _manager_repo_fingerprint()
        boundary_payload = {
            **boundary.__dict__,
            "active_plan_content": captured_active_plan,
            "manager_note_scope": proposed_note_scope,
            "retry_manager_note_scope": retry_note_scope,
            "workspace_state": {
                "branch": exec_ctx.feature_branch if exec_ctx is not None else None,
                "head": fingerprint_before[0],
                "dirty_worktree": fingerprint_before[1],
                "merge_state": "managed" if exec_ctx is not None and "merge" in exec_ctx.teardown else "none",
            },
        }
        context = build_manager_context(
            context_run_dir or run_paths.run_dir,
            level=level,  # type: ignore[arg-type]
            trigger=boundary.trigger,
            decision_number=decision_number,
            run_metadata=metadata,
            boundary=boundary_payload,
            active_plan_content=captured_active_plan,
        )
        if boundary.context_schema_version >= 3:
            controller_state = context.get("controller_state")
            if isinstance(controller_state, dict):
                controller_state["checkpoint_repartitions"] = list(
                    boundary.repartition_history
                )
        boundary_payload["captured_plan_state"] = context["plan_state"]
        eligible = set(boundary.__dict__.get("eligible_actions", ()))
        if level == "lite":
            eligible.add("escalate_to_full")

        system_prompt, user_prompt = build_manager_prompts(
            context,
            skill_name=workflow_config.manager.skill,
        )
        artifact_dir = run_paths.manager_dir / f"decision-{decision_number:03d}"
        artifact_paths = {
            name: str((artifact_dir / filename).relative_to(run_paths.run_dir))
            for name, filename in {
                "context": "context.json", "system_prompt": "system-prompt.txt",
                "user_prompt": "user-prompt.txt", "stdout": "stdout.txt",
                "stderr": "stderr.txt", "result": "result.json",
            }.items()
        }
        target_team = boundary.implementation_upgrade.get("target_team") if boundary.implementation_upgrade else boundary.actual_team
        _emit_event(observer, ManagerStartedEvent.create(
            decision_number=decision_number, level=level, trigger=boundary.trigger,
            target_step=boundary.proposed_transition, target_team=target_team, artifact_paths=artifact_paths,
        ))
        stdout = ""
        stderr = ""
        result_payload: dict[str, object] = {
            "decision_number": decision_number, "finalized_turn_number": boundary.finalized_turn_number,
            "level": level, "trigger": boundary.trigger, "status": "invalid",
        }
        parsed: ManagerDecisionV1 | None = None
        error: str | None = None
        try:
            role_resolution = resolve_manager_role(
                workflow_config, level=level, baseline_team=baseline_team_name  # type: ignore[arg-type]
            )
            manager_profile = resolve_profile(role_resolution.selector, workflow_config, step_path="manager")
            manager_adapter = adapter or get_adapter(manager_profile.harness_name)
            manager_invocation = manager_adapter.build_invocation(
                repo_root=execution_repo_root,
                model=manager_profile.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                effort=manager_profile.effort,
            ).for_final_output()
            result_payload["invocation"] = {
                "label": manager_invocation.label,
                "argv": list(manager_invocation.argv),
            }
            if runner is None:
                completed = _run_process(manager_invocation, execution_repo_root, banner, state)
            else:
                completed = _run_injected_runner(runner, manager_invocation, execution_repo_root)
            stdout, stderr = completed.stdout, completed.stderr
            if completed.returncode != 0:
                raise ManagerDecisionError(f"manager harness exited with code {completed.returncode}")
            candidate = validate_manager_decision(
                parse_manager_decision(stdout),
                level=level,  # type: ignore[arg-type]
                eligible_actions=eligible,
                proposed_transition=boundary.proposed_transition,
            )
            if candidate.next_step_notes:
                note_scope = (
                    (
                        retry_note_scope
                        if retry_note_scope is not None
                        else proposed_note_scope
                    )
                    if candidate.action in {
                        "retry_current_step",
                        "switch_to_backup_and_retry",
                    }
                    else proposed_note_scope
                )
                validate_manager_note_authority(
                    candidate.next_step_notes,
                    scope=note_scope,
                )
            parsed = candidate
            result_payload.update({"status": "accepted", **parsed.to_dict()})
        except (ManagerDecisionError, ValueError, WorkflowError) as exc:
            parsed = None
            error = str(exc)
            result_payload["error"] = error

        fingerprint_after = _manager_repo_fingerprint()
        if fingerprint_after != fingerprint_before:
            parsed = None
            error = "manager mutated repository or plan state"
            result_payload.update({"status": "mutation-detected", "error": error})
        artifacts = write_manager_artifacts(
            run_paths, decision_number=decision_number, context=context,
            system_prompt=system_prompt, user_prompt=user_prompt, stdout=stdout, stderr=stderr,
            result=result_payload,
            boundary={
                "decision_number": decision_number,
                "trigger": boundary.trigger,
                "run_metadata": metadata,
                "boundary": boundary_payload,
                "active_plan_content": captured_active_plan,
            },
        )
        state.manager_decision_number = decision_number
        state.manager_history.append(ManagerDecisionSummary(
            decision_number=decision_number, level=level, trigger=boundary.trigger,
            action=parsed.action if parsed is not None else "invalid",
            reason=parsed.reason if parsed is not None else (error or "invalid manager result"),
            artifact_path=str(artifacts.directory.relative_to(run_paths.run_dir)),
        ))
        action = parsed.action if parsed is not None else "invalid"
        _emit_event(observer, ManagerDecidedEvent.create(
            decision_number=decision_number, level=level, trigger=boundary.trigger, action=action,
            target_step=boundary.proposed_transition, target_team=target_team,
            report_path="manager-report.md" if action == "stop" else None,
            artifact_paths=artifact_paths,
        ))
        return parsed, context, error

    def _persist_repartition(pending: PendingRepartitionV1) -> None:
        state.pending_repartition = pending
        write_run_metadata(
            run_paths, config, state, status="running",
            last_snapshot=state.last_snapshot,
            workflow_name=workflow_name,
            original_plan_path=original_plan_path,
            current_step_name=current_step_name,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )

    def _apply_pending_repartition() -> None:
        """Reconcile and route one semantically validated transaction."""
        nonlocal active_plan_path, current_step_name

        pending = state.pending_repartition
        if pending is None:
            return
        if pending.stage in {"decided", "proposed", "mechanically_validated"}:
            raise WorkflowError(
                "pending repartition proposal/validation transaction must be "
                f"reconciled before a harness can start (stage={pending.stage})",
                run_dir=run_paths.run_dir,
            )
        if pending.stage == "failed":
            raise WorkflowError(
                "cannot resume a failed repartition transaction without "
                "explicit scope reset",
                run_dir=run_paths.run_dir,
            )
        if pending.stage not in {
            "semantically_validated",
            "execution_plan_applied",
            "primary_plan_applied",
            "applied",
        }:
            raise WorkflowError(
                f"cannot apply repartition transaction at unknown stage '{pending.stage}'",
                run_dir=run_paths.run_dir,
            )
        if (
            (pending.proposal_sha256 is None or not pending.child_summaries)
            and pending.proposal_artifact_path is not None
        ):
            proposal_path = (
                run_paths.run_dir / pending.proposal_artifact_path
            ).resolve()
            try:
                proposal_path.relative_to(run_paths.run_dir.resolve())
                proposal_bytes = proposal_path.read_bytes()
                proposal_payload = json.loads(proposal_bytes)
                raw_children = proposal_payload.get("children")
                if not isinstance(raw_children, list):
                    raise ValueError("proposal children are unavailable")
                child_summaries = tuple(
                    f"{child['title']}: {child['narrow_goal']}"
                    for child in raw_children
                    if isinstance(child, dict)
                    and isinstance(child.get("title"), str)
                    and isinstance(child.get("narrow_goal"), str)
                )
                if len(child_summaries) != len(raw_children):
                    raise ValueError("proposal child summaries are incomplete")
                pending = replace(
                    pending,
                    proposal_sha256=hashlib.sha256(proposal_bytes).hexdigest(),
                    child_summaries=child_summaries,
                )
                _persist_repartition(pending)
            except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
                raise WorkflowError(
                    f"cannot restore compact repartition proposal evidence: {exc}",
                    run_dir=run_paths.run_dir,
                ) from exc
        if (
            pending.candidate_artifact_path is None
            or pending.candidate_plan_sha256 is None
            or pending.generation_id is None
            or not pending.partition_ids
            or len(pending.child_summaries) != len(pending.partition_ids)
            or pending.proposal_sha256 is None
            or pending.current_disposition is None
            or pending.resolved_target_step is None
            or pending.resolved_target_role is None
        ):
            raise WorkflowError(
                "cannot apply repartition transaction: validated identity or "
                "routing fields are incomplete",
                run_dir=run_paths.run_dir,
            )
        artifact_path = (run_paths.run_dir / pending.candidate_artifact_path).resolve()
        try:
            artifact_path.relative_to(run_paths.run_dir.resolve())
            candidate_bytes = artifact_path.read_bytes()
        except (ValueError, OSError) as exc:
            raise WorkflowError(
                f"cannot read validated repartition candidate: {exc}",
                run_dir=run_paths.run_dir,
            ) from exc
        candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
        if candidate_hash != pending.candidate_plan_sha256:
            raise WorkflowError(
                "validated repartition candidate hash mismatch: "
                f"expected={pending.candidate_plan_sha256} observed={candidate_hash}",
                run_dir=run_paths.run_dir,
            )

        execution_path = _exec_plan_path(original_plan_path, exec_ctx)
        primary_path = _primary_plan_path(original_plan_path, exec_ctx)
        pending = _reconcile_repartition_plan_copies(
            pending,
            candidate_bytes=candidate_bytes,
            execution_path=execution_path,
            primary_path=primary_path,
            persist=_persist_repartition,
        )

        try:
            candidate_snapshot = load_plan_tolerant(
                execution_path
            ).parsed_plan.snapshot
        except (OSError, PlanParseError, ValueError) as exc:
            raise WorkflowError(
                f"applied repartition candidate cannot be parsed: {exc}",
                run_dir=run_paths.run_dir,
            ) from exc
        scope = state.active_implementation_scope
        if scope is None or scope.scope_id != pending.scope_id:
            raise WorkflowError(
                "cannot apply repartition transaction without its active parent scope",
                run_dir=run_paths.run_dir,
            )
        target_step = wf.steps.get(pending.resolved_target_step)
        if target_step is None or target_step.role != pending.resolved_target_role:
            raise WorkflowError(
                "repartition target no longer resolves to the persisted workflow role",
                run_dir=run_paths.run_dir,
            )

        # The split supersedes only one-hop state and overlay selection. Attempts,
        # rejections, dirty code, and the immutable parent envelope remain.
        state.pending_manager_notes = None
        state.pending_step_team_override = None
        state.active_implementation_scope = replace(
            scope,
            awaiting_review=target_step.role == "reviewer",
            current_partition_generation_id=pending.generation_id,
            current_partition_candidate_sha256=pending.candidate_plan_sha256,
            current_partition_id=pending.partition_ids[0],
        )
        state.last_snapshot = candidate_snapshot
        active_plan_path = original_plan_path
        current_step_name = pending.resolved_target_step
        state.pending_retry = None

        target_team: str | None = None
        if target_step.role == "worker":
            attempts = state.implementation_attempts.get(scope.scope_id, [])
            target_team = attempts[-1].team if attempts else baseline_team_name
        selector, _resolved = _resolve_step_runtime(
            target_step,
            workflow_config,
            team_name=target_team or baseline_team_name,
            step_path=(
                f"workflow.{workflow_name}.steps.{pending.resolved_target_step}"
            ),
        )
        target_identity = _target_plan_identity(
            original_plan_path, candidate_snapshot,
        )
        state.pending_boundary_decision = PendingBoundaryDecision(
            finalized_turn_number=(
                state.pending_boundary_decision.finalized_turn_number
                if state.pending_boundary_decision is not None
                else state.active_turn
            ),
            decision_number=pending.decision_number,
            action="repartition_current_checkpoint",
            proposed_action="transition",
            proposed_transition=None,
            resolved_next_step=pending.resolved_target_step,
            target_role=target_step.role,
            target_team=target_team,
            target_selector=selector,
            checkpoint_identity=target_identity,
            post_transition_active_plan_path=str(original_plan_path),
            post_transition_checkpoint_identity=target_identity,
            scope_id=scope.scope_id,
            target_plan_identity=target_identity,
            repartition_generation_id=pending.generation_id,
            repartition_candidate_sha256=pending.candidate_plan_sha256,
            repartition_partition_id=pending.partition_ids[0],
        )
        if target_step.role == "worker" and target_team is not None:
            state.pending_step_team_override = PendingTeamOverride(
                target_step=pending.resolved_target_step,
                role=target_step.role,
                source_team=target_team,
                target_team=target_team,
                selector=selector,
                checkpoint_identity=target_identity,
                decision_number=pending.decision_number,
                scope_id=scope.scope_id,
                target_plan_identity=target_identity,
                repartition_generation_id=pending.generation_id,
                repartition_candidate_sha256=pending.candidate_plan_sha256,
                repartition_partition_id=pending.partition_ids[0],
            )
        required_artifact_paths = (
            scope.envelope_artifact_path,
            pending.proposal_artifact_path,
            pending.candidate_artifact_path,
            pending.mechanical_validation_artifact_path,
            pending.semantic_verdict_artifact_path,
        )
        if not all(
            isinstance(path, str) and path for path in required_artifact_paths
        ) or not isinstance(scope.envelope_artifact_sha256, str):
            raise WorkflowError(
                "cannot publish applied repartition evidence with incomplete "
                "artifact references",
                run_dir=run_paths.run_dir,
            )
        record = CheckpointRepartitionRecord(
            schema_version=1,
            decision_number=pending.decision_number,
            scope_id=pending.scope_id,
            generation_id=pending.generation_id,
            envelope_sha256=pending.envelope_sha256,
            envelope_artifact_sha256=str(scope.envelope_artifact_sha256),
            source_plan_sha256=pending.source_plan_sha256,
            proposal_sha256=pending.proposal_sha256,
            candidate_plan_sha256=pending.candidate_plan_sha256,
            partition_ids=pending.partition_ids,
            child_summaries=pending.child_summaries,
            current_disposition=pending.current_disposition or "",
            resolved_target_step=pending.resolved_target_step,
            resolved_target_role=pending.resolved_target_role,
            current_partition_id=pending.partition_ids[0],
            scope_pressure_reason=state.scope_pressure_reason,
            envelope_artifact_path=str(scope.envelope_artifact_path),
            proposal_artifact_path=str(pending.proposal_artifact_path),
            candidate_artifact_path=str(pending.candidate_artifact_path),
            mechanical_validation_artifact_path=str(
                pending.mechanical_validation_artifact_path
            ),
            semantic_verdict_artifact_path=str(
                pending.semantic_verdict_artifact_path
            ),
        )
        is_new_record = not any(
            item.generation_id == record.generation_id
            for item in state.repartition_history
        )
        if is_new_record:
            state.repartition_history.append(record)
        pending = replace(pending, stage="applied")
        _persist_repartition(pending)
        if is_new_record:
            _emit_event(observer, CheckpointRepartitionedEvent.create(
                decision_number=record.decision_number,
                scope_id=record.scope_id,
                generation_id=record.generation_id,
                envelope_sha256=record.envelope_sha256,
                envelope_artifact_sha256=record.envelope_artifact_sha256,
                source_plan_sha256=record.source_plan_sha256,
                proposal_sha256=record.proposal_sha256,
                candidate_plan_sha256=record.candidate_plan_sha256,
                partition_ids=record.partition_ids,
                child_summaries=record.child_summaries,
                current_disposition=record.current_disposition,
                resolved_target_step=record.resolved_target_step,
                resolved_target_role=record.resolved_target_role,
                current_partition_id=record.current_partition_id,
                scope_pressure_reason=record.scope_pressure_reason,
                artifact_paths={
                    "envelope": record.envelope_artifact_path,
                    "proposal": record.proposal_artifact_path,
                    "candidate": record.candidate_artifact_path,
                    "mechanical_validation": (
                        record.mechanical_validation_artifact_path
                    ),
                    "semantic_verdict": record.semantic_verdict_artifact_path,
                },
            ))

    def _invoke_repartition_full(
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, str, str | None]:
        """Invoke the configured Full role without manager/turn accounting."""
        stdout = ""
        stderr = ""
        error: str | None = None
        fingerprint_before = _protected_repartition_fingerprint()
        try:
            role_resolution = resolve_manager_role(
                workflow_config, level="full", baseline_team=baseline_team_name,
            )
            profile = resolve_profile(
                role_resolution.selector, workflow_config,
                step_path="manager.repartition",
            )
            call_adapter = adapter or get_adapter(profile.harness_name)
            invocation = call_adapter.build_invocation(
                repo_root=execution_repo_root,
                model=profile.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                effort=profile.effort,
            ).for_final_output()
            if runner is None:
                completed = _run_process(invocation, execution_repo_root, banner, state)
            else:
                completed = _run_injected_runner(runner, invocation, execution_repo_root)
            stdout, stderr = completed.stdout, completed.stderr
            if completed.returncode != 0:
                error = f"repartition Full harness exited with code {completed.returncode}"
        except (ValueError, WorkflowError) as exc:
            error = str(exc)
        if _protected_repartition_fingerprint() != fingerprint_before:
            error = (
                "repartition Full call mutated repository, plan, "
                "or protected run-artifact state"
            )
        return stdout, stderr, error

    def _run_repartition_cycle(
        *,
        decision_context: dict[str, object],
        disposition_targets: Mapping[str, tuple[str, str]],
    ) -> None:
        """Produce and semantically validate at most two candidates."""
        from .repartition import (
            canonical_json_bytes,
            derive_generation_id,
            derive_partition_ids,
            extract_source_blocks,
            make_repair_evidence_block,
            parse_envelope_bytes,
            parse_proposal_json,
            parse_verdict_json,
            render_candidate_plan,
            repartition_proposal_sha256,
            slice_checkpoint_source,
            validate_candidate_mechanically,
            validate_envelope_boundary_drift,
        )

        scope = state.active_implementation_scope
        if scope is None or state.pending_repartition is not None:
            _fail_manager_gate(
                decision_context,
                reason="accepted repartition decision has no unique active scope transaction",
            )
        envelope_bytes = load_scope_envelope_for_resume(run_paths.run_dir, scope)
        if envelope_bytes is None:
            _fail_manager_gate(
                decision_context,
                reason="accepted repartition decision lacks an immutable scope envelope",
            )
        try:
            envelope = parse_envelope_bytes(envelope_bytes)
            source_path = _exec_plan_path(original_plan_path, exec_ctx)
            source_plan_text = source_path.read_bytes().decode("utf-8", "strict")
            drift = validate_envelope_boundary_drift(
                envelope=envelope, boundary_plan_text=source_plan_text,
            )
            if not drift.allowed:
                raise ValueError(
                    "boundary source plan drift is not allowed: " + "; ".join(drift.issues)
                )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            _fail_manager_gate(
                decision_context,
                reason=f"cannot capture repartition boundary artifacts: {exc}",
            )
        source_sha256 = hashlib.sha256(source_plan_text.encode("utf-8")).hexdigest()
        generation_id = derive_generation_id(
            scope_id=scope.scope_id,
            decision_number=state.manager_decision_number,
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=source_sha256,
        )
        pending = PendingRepartitionV1(
            schema_version=1,
            decision_number=state.manager_decision_number,
            scope_id=scope.scope_id,
            stage="decided",
            envelope_sha256=envelope.canonical_envelope_sha256,
            source_plan_sha256=source_sha256,
            generation_id=generation_id,
        )
        _persist_repartition(pending)

        evidence_blocks = []
        evidence_references: dict[str, str] = {}
        active_source_path = _exec_plan_path(active_plan_path, exec_ctx)
        if active_source_path.resolve() != source_path.resolve():
            try:
                repair_text = active_source_path.read_bytes().decode("utf-8", "strict")
                repair_slice = slice_checkpoint_source(
                    repair_text,
                    checkpoint_index=scope.checkpoint_index,
                )
                if repair_slice is None:
                    raise ValueError("active repair checkpoint is missing")
                repair_units = extract_source_blocks(
                    repair_slice,
                    envelope_checkpoint_sha256=hashlib.sha256(
                        repair_slice.full_text.encode("utf-8")
                    ).hexdigest(),
                    plan_text=repair_text,
                )
                for ordinal, unit in enumerate(repair_units, start=1):
                    block = make_repair_evidence_block(
                        evidence_kind="active-repair",
                        ordinal=ordinal,
                        text=unit.text,
                    )
                    evidence_blocks.append(block)
                    evidence_references[block.block_id] = (
                        f"{active_plan_path}#checkpoint={scope.checkpoint_index}&block={ordinal}"
                    )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                pending = replace(
                    pending, stage="failed", failed_stage="evidence",
                    failure_reason=str(exc),
                )
                _persist_repartition(pending)
                _fail_manager_gate(
                    decision_context,
                    reason=f"cannot capture active corrective evidence: {exc}",
                )
        controller = decision_context.get("controller_state")
        latest_rejection = (
            controller.get("latest_full_rejection")
            if isinstance(controller, Mapping) else None
        )
        if isinstance(latest_rejection, Mapping):
            reviewer_output = latest_rejection.get("exact_reviewer_output")
            reviewer_artifact = latest_rejection.get("review_stdout_artifact_path")
            if isinstance(reviewer_output, str) and reviewer_output.strip():
                block = make_repair_evidence_block(
                    evidence_kind="latest-reviewer-output",
                    ordinal=1,
                    text=reviewer_output,
                )
                evidence_blocks.append(block)
                evidence_references[block.block_id] = (
                    str(reviewer_artifact)
                    if isinstance(reviewer_artifact, str) and reviewer_artifact
                    else "latest-reviewer-output"
                )
        repair_evidence = tuple(evidence_blocks)
        evidence_payload = {
            "blocks": [
                {**asdict(block), "artifact_reference": evidence_references[block.block_id]}
                for block in repair_evidence
            ],
        }
        base_payload: dict[str, object] = {
            "schema_version": 1,
            "decision_number": state.manager_decision_number,
            "scope_id": scope.scope_id,
            "envelope": {
                **envelope.to_dict(),
                "canonical_envelope_sha256": (
                    envelope.canonical_envelope_sha256
                ),
            },
            "source_plan_sha256": source_sha256,
            "source_plan_text": source_plan_text,
            "manager_context": decision_context,
            "repair_evidence_blocks": evidence_payload["blocks"],
            "allowed_current_dispositions": sorted(disposition_targets),
        }

        correction_findings: tuple[str, ...] = ()
        rejected_proposal_sha256: str | None = None
        for attempt_number in (1, 2):
            attempt_paths = create_repartition_attempt_paths(
                run_paths,
                decision_number=state.manager_decision_number,
                attempt_number=attempt_number,
            )
            attempt_rel = attempt_paths.directory.relative_to(run_paths.run_dir).as_posix()
            write_repartition_artifact(attempt_paths.source_plan, source_plan_text)
            write_repartition_artifact(attempt_paths.envelope, envelope_bytes)
            write_repartition_artifact(attempt_paths.evidence, evidence_payload)
            propose_system, propose_user = build_repartition_prompts(
                base_payload,
                mode="propose",
                skill_name=workflow_config.manager.repartition_skill,
                correction_findings=correction_findings,
            )
            write_repartition_artifact(attempt_paths.propose_system_prompt, propose_system)
            write_repartition_artifact(attempt_paths.propose_user_prompt, propose_user)
            propose_stdout, propose_stderr, call_error = _invoke_repartition_full(
                system_prompt=propose_system, user_prompt=propose_user,
            )
            write_repartition_artifact(attempt_paths.propose_stdout, propose_stdout)
            write_repartition_artifact(attempt_paths.propose_stderr, propose_stderr)
            if call_error is not None:
                write_repartition_artifact(
                    attempt_paths.result,
                    {"status": "failed", "stage": "propose", "reason": call_error},
                )
                pending = replace(
                    pending, stage="failed", attempt_count=attempt_number,
                    latest_attempt_path=attempt_rel, failed_stage="propose",
                    failure_reason=call_error,
                )
                _persist_repartition(pending)
                _fail_manager_gate(decision_context, reason=call_error)
            try:
                proposal = parse_proposal_json(
                    propose_stdout,
                    expected_envelope_sha256=envelope.canonical_envelope_sha256,
                    expected_source_plan_sha256=source_sha256,
                    valid_source_block_ids=tuple(
                        block.block_id for block in envelope.source_blocks
                    ),
                    valid_repair_evidence_ids=tuple(
                        block.block_id for block in repair_evidence
                    ),
                )
                if proposal.current_disposition not in disposition_targets:
                    raise ValueError(
                        "proposal current_disposition has no controller-resolvable "
                        "worker or reviewer continuation"
                    )
            except ValueError as exc:
                reason = f"invalid repartition proposal: {exc}"
                write_repartition_artifact(
                    attempt_paths.result,
                    {"status": "failed", "stage": "proposal-schema", "reason": reason},
                )
                pending = replace(
                    pending, stage="failed", attempt_count=attempt_number,
                    latest_attempt_path=attempt_rel, failed_stage="proposal-schema",
                    failure_reason=reason,
                )
                _persist_repartition(pending)
                _fail_manager_gate(decision_context, reason=reason)

            proposal_bytes = canonical_json_bytes(proposal.to_dict())
            proposal_sha256 = repartition_proposal_sha256(proposal)
            if (
                attempt_number == 2
                and rejected_proposal_sha256 == proposal_sha256
            ):
                reason = (
                    "corrected repartition proposal is byte-identical to the "
                    "rejected proposal"
                )
                write_repartition_artifact(
                    attempt_paths.result,
                    {
                        "status": "failed",
                        "stage": "proposal-correction",
                        "reason": reason,
                    },
                )
                pending = replace(
                    pending, stage="failed", attempt_count=attempt_number,
                    latest_attempt_path=attempt_rel,
                    failed_stage="proposal-correction",
                    failure_reason=reason,
                )
                _persist_repartition(pending)
                _fail_manager_gate(decision_context, reason=reason)
            write_repartition_artifact(attempt_paths.proposal, proposal_bytes)
            pending = replace(
                pending, stage="proposed", attempt_count=attempt_number,
                latest_attempt_path=attempt_rel,
                child_summaries=tuple(
                    f"{child.title}: {child.narrow_goal}"
                    for child in proposal.children
                ),
                proposal_sha256=proposal_sha256,
                proposal_artifact_path=attempt_paths.proposal.relative_to(
                    run_paths.run_dir
                ).as_posix(),
                current_disposition=proposal.current_disposition,
                resolved_target_step=disposition_targets[
                    proposal.current_disposition
                ][0],
                resolved_target_role=disposition_targets[
                    proposal.current_disposition
                ][1],
                failed_stage=None, failure_reason=None,
            )
            _persist_repartition(pending)

            mechanical_findings: tuple[str, ...] = ()
            try:
                partition_ids = derive_partition_ids(
                    generation_id=generation_id, proposal=proposal,
                )
                candidate_text = render_candidate_plan(
                    envelope=envelope,
                    proposal=proposal,
                    source_plan_text=source_plan_text,
                    generation_id=generation_id,
                    partition_ids=partition_ids,
                    repair_evidence_blocks=repair_evidence,
                    repair_evidence_artifact_references=evidence_references,
                )
                mechanical = validate_candidate_mechanically(
                    source_plan_text=source_plan_text,
                    candidate_plan_text=candidate_text,
                    envelope=envelope,
                    proposal=proposal,
                    repair_evidence_blocks=repair_evidence,
                    repair_evidence_artifact_references=evidence_references,
                    expected_generation_id=generation_id,
                    expected_partition_ids=partition_ids,
                )
                mechanical_payload = mechanical.to_dict()
                mechanical_findings = mechanical.issues
            except ValueError as exc:
                candidate_text = ""
                mechanical_payload = {
                    "valid": False,
                    "issues": [f"candidate_render_failed:{exc}"],
                }
                mechanical_findings = tuple(mechanical_payload["issues"])
            write_repartition_artifact(attempt_paths.candidate_plan, candidate_text)
            write_repartition_artifact(
                attempt_paths.mechanical_validation, mechanical_payload,
            )
            if not bool(mechanical_payload.get("valid")):
                write_repartition_artifact(
                    attempt_paths.result,
                    {
                        "status": "rejected", "stage": "mechanical",
                        "findings": list(mechanical_findings),
                    },
                )
                if attempt_number == 1:
                    rejected_proposal_sha256 = proposal_sha256
                    correction_findings = tuple(
                        finding[:1_000] for finding in mechanical_findings[:16]
                    )
                    continue
                reason = (
                    "second repartition candidate failed mechanical validation: "
                    + "; ".join(mechanical_findings)
                )
                pending = replace(
                    pending, stage="failed", failed_stage="mechanical",
                    failure_reason=reason,
                )
                _persist_repartition(pending)
                _fail_manager_gate(decision_context, reason=reason)

            candidate_sha256 = hashlib.sha256(
                candidate_text.encode("utf-8")
            ).hexdigest()
            pending = replace(
                pending, stage="mechanically_validated",
                candidate_plan_sha256=candidate_sha256,
                partition_ids=partition_ids,
                candidate_artifact_path=attempt_paths.candidate_plan.relative_to(
                    run_paths.run_dir
                ).as_posix(),
                mechanical_validation_artifact_path=(
                    attempt_paths.mechanical_validation.relative_to(
                        run_paths.run_dir
                    ).as_posix()
                ),
            )
            _persist_repartition(pending)
            validate_payload = {
                **base_payload,
                "proposal": proposal.to_dict(),
                "proposal_sha256": proposal_sha256,
                "candidate_plan_text": candidate_text,
                "candidate_plan_sha256": candidate_sha256,
                "mechanical_validation": mechanical_payload,
            }
            validate_system, validate_user = build_repartition_prompts(
                validate_payload,
                mode="validate",
                skill_name=workflow_config.manager.repartition_skill,
            )
            write_repartition_artifact(
                attempt_paths.validate_system_prompt, validate_system,
            )
            write_repartition_artifact(
                attempt_paths.validate_user_prompt, validate_user,
            )
            validate_stdout, validate_stderr, call_error = _invoke_repartition_full(
                system_prompt=validate_system, user_prompt=validate_user,
            )
            write_repartition_artifact(
                attempt_paths.validate_stdout, validate_stdout,
            )
            write_repartition_artifact(
                attempt_paths.validate_stderr, validate_stderr,
            )
            if call_error is not None:
                write_repartition_artifact(
                    attempt_paths.result,
                    {"status": "failed", "stage": "validate", "reason": call_error},
                )
                pending = replace(
                    pending, stage="failed", failed_stage="validate",
                    failure_reason=call_error,
                )
                _persist_repartition(pending)
                _fail_manager_gate(decision_context, reason=call_error)
            try:
                verdict = parse_verdict_json(
                    validate_stdout,
                    expected_proposal_sha256=proposal_sha256,
                    expected_candidate_sha256=candidate_sha256,
                )
            except ValueError as exc:
                reason = f"invalid repartition semantic verdict: {exc}"
                write_repartition_artifact(
                    attempt_paths.result,
                    {"status": "failed", "stage": "verdict-schema", "reason": reason},
                )
                pending = replace(
                    pending, stage="failed", failed_stage="verdict-schema",
                    failure_reason=reason,
                )
                _persist_repartition(pending)
                _fail_manager_gate(decision_context, reason=reason)
            verdict_bytes = canonical_json_bytes(verdict.to_dict())
            write_repartition_artifact(
                attempt_paths.semantic_verdict, verdict_bytes,
            )
            if verdict.verdict == "reject":
                write_repartition_artifact(
                    attempt_paths.result,
                    {
                        "status": "rejected", "stage": "semantic",
                        "reason": verdict.reason,
                        "findings": list(verdict.findings),
                    },
                )
                if attempt_number == 1:
                    rejected_proposal_sha256 = proposal_sha256
                    correction_findings = tuple(
                        finding[:1_000]
                        for finding in (verdict.findings or (verdict.reason,))[:16]
                    )
                    continue
                reason = (
                    "second repartition candidate failed semantic validation: "
                    + verdict.reason
                )
                pending = replace(
                    pending, stage="failed", failed_stage="semantic",
                    semantic_verdict_artifact_path=(
                        attempt_paths.semantic_verdict.relative_to(
                            run_paths.run_dir
                        ).as_posix()
                    ),
                    failure_reason=reason,
                )
                _persist_repartition(pending)
                _fail_manager_gate(decision_context, reason=reason)

            write_repartition_artifact(
                attempt_paths.result,
                {
                    "status": "accepted",
                    "stage": "semantically_validated",
                    "proposal_sha256": proposal_sha256,
                    "candidate_plan_sha256": candidate_sha256,
                },
            )
            pending = replace(
                pending, stage="semantically_validated",
                semantic_verdict_artifact_path=(
                    attempt_paths.semantic_verdict.relative_to(
                        run_paths.run_dir
                    ).as_posix()
                ),
                failed_stage=None, failure_reason=None,
            )
            _persist_repartition(pending)
            return
        raise AssertionError("bounded repartition cycle exhausted without a result")

    def _manager_gate(
        *,
        proposed_transition: str,
        current_step: str,
        current_role: str,
        active_team: str | None,
        active_selector: str,
        post_transition_active_path: Path,
        trigger: str = "post_turn",
        proposed_action: str = "transition",
        safely_retryable: bool = False,
        operational_failure: bool = False,
        backup_team: str | None = None,
        backup_selector: str | None = None,
        context_run_dir: Path | None = None,
        finalized_turn_number: int | None = None,
        artifact_path: str | None = None,
        scope_pressure_reason: str | None = None,
    ) -> str:
        """Accept or replace the controller transition after a durable turn."""
        if scope_pressure_reason is not None and not workflow_config.manager.enabled:
            raise WorkflowError(
                f"AFLOW_SCOPE_PRESSURE detected ('{scope_pressure_reason}') but manager supervision is disabled; "
                f"scope-pressure rerouting requires an enabled manager",
                run_dir=run_paths.run_dir,
            )
        if not workflow_config.manager.enabled:
            return proposed_transition
        if scope_pressure_reason is not None:
            state.scope_pressure_reason = scope_pressure_reason
            _require_valid_pressure_scope(
                run_paths.run_dir,
                state.active_implementation_scope,
            )
        next_step = None if proposed_transition == "END" else proposed_transition
        candidate_step = wf.steps.get(next_step) if next_step is not None else None
        proposed_target_plan = (
            active_plan_path
            if proposed_transition == current_step
            else post_transition_active_path
        )
        target_plan_identity = _target_plan_identity(proposed_target_plan)
        scope = state.active_implementation_scope
        attempts = (
            state.implementation_attempts.get(scope.scope_id, [])
            if scope is not None else []
        )
        recent_team = attempts[-1].team if attempts else None
        scope_context = None
        if scope is not None:
            upgrade_depth = _implementation_upgrade_depth(
                workflow_config,
                baseline_team=baseline_team_name,
                most_recent_team=recent_team,
            )
            scope_context = {
                "scope_id": scope.scope_id,
                "opened_turn_number": scope.opened_turn_number,
                "carried_reviewer_rejection_count": (
                    scope.carried_reviewer_rejection_count
                ),
                "checkpoint_index": scope.checkpoint_index,
                "checkpoint_name": scope.checkpoint_name,
                "awaiting_review": scope.awaiting_review,
                "attempt_count": len(attempts),
                "attempt_teams": [attempt.team for attempt in attempts],
                "attempt_selectors": [attempt.selector for attempt in attempts],
                "most_recent_team": recent_team,
                "upgrade_depth": upgrade_depth,
            }
        retrying_scoped_implementation = (
            scope is not None
            and bool(attempts)
            and candidate_step is not None
            and candidate_step.role == "worker"
        )
        upgrade = eligible_implementation_upgrade(
            workflow_config,
            role=candidate_step.role if candidate_step is not None else current_role,
            baseline_team=baseline_team_name,
            most_recent_implementation_team=recent_team,
            is_implementation_attempt=retrying_scoped_implementation,
        )
        if (
            candidate_step is not None
            and candidate_step.role == "worker"
            and not retrying_scoped_implementation
        ):
            upgrade = replace(
                upgrade,
                reason="next worker has no prior attempt in an active implementation scope",
            )
        eligible: set[str] = {"continue", "stop"}
        if safely_retryable:
            eligible.add("retry_current_step")
        if operational_failure and backup_team is not None and backup_team != active_team:
            eligible.add("switch_to_backup_and_retry")
        if upgrade.available:
            eligible.add("upgrade_next_implementation")
        repartition_disposition_targets: dict[str, tuple[str, str]] = {}
        for routed_step_name, routed_step in (
            (next_step, candidate_step),
            (current_step, wf.steps.get(current_step)),
        ):
            if routed_step is None:
                continue
            if routed_step.role == "worker":
                repartition_disposition_targets.setdefault(
                    "implement_current_partition",
                    (str(routed_step_name), routed_step.role),
                )
            elif routed_step.role == "reviewer":
                repartition_disposition_targets.setdefault(
                    "review_current_partition",
                    (str(routed_step_name), routed_step.role),
                )
        if (
            scope is not None
            and scope.checkpoint_index is not None
            and scope.checkpoint_name is not None
            and state.pending_repartition is None
            and repartition_disposition_targets
        ):
            try:
                from .repartition import (
                    parse_envelope_bytes,
                    validate_envelope_boundary_drift,
                )
                envelope_bytes = load_scope_envelope_for_resume(
                    run_paths.run_dir, scope,
                )
                if envelope_bytes is not None:
                    eligible_envelope = parse_envelope_bytes(envelope_bytes)
                    eligible_source = _exec_plan_path(
                        original_plan_path, exec_ctx,
                    ).read_bytes().decode("utf-8", "strict")
                    eligible_drift = validate_envelope_boundary_drift(
                        envelope=eligible_envelope,
                        boundary_plan_text=eligible_source,
                    )
                    if eligible_drift.allowed:
                        eligible.add("repartition_current_checkpoint")
            except (OSError, UnicodeDecodeError, ValueError, WorkflowError):
                # Invalid authority makes repartition unavailable. Full still
                # receives continue/upgrade/stop and may report the boundary.
                pass
        boundary = FinalizedTurnBoundary(
            finalized_turn_number=(
                state.active_turn
                if finalized_turn_number is None
                else finalized_turn_number
            ),
            artifact_path=(
                f"turns/turn-{state.active_turn:03d}"
                if artifact_path is None
                else artifact_path
            ),
            trigger=trigger, terminal=False,
            proposed_action=proposed_action, proposed_transition=proposed_transition,
            current_step=current_step, current_role=current_role, baseline_team=baseline_team_name,
            actual_team=active_team, actual_selector=active_selector,
            original_plan_path=str(original_plan_path), active_plan_path=str(proposed_target_plan),
            checkpoint_identity=target_plan_identity, safely_retryable=safely_retryable,
            operational_failure=operational_failure, backup_team=backup_team,
            backup_selector=backup_selector, implementation_upgrade=upgrade.__dict__,
            active_implementation_scope=scope_context,
            eligible_actions=sorted(eligible),
            scope_pressure_reason=scope_pressure_reason,
            # Immutable controller-owned copies for deterministic v2 reconstruction.
            review_rejection_history=[asdict(r) for r in state.review_rejection_history],
            implementation_attempts={
                sid: [asdict(a) for a in atts]
                for sid, atts in state.implementation_attempts.items()
            },
            envelope_artifact_path=(
                str(scope.envelope_artifact_path)
                if scope is not None and scope.envelope_artifact_path is not None
                else None
            ),
            envelope_artifact_sha256=(
                str(scope.envelope_artifact_sha256)
                if scope is not None and scope.envelope_artifact_sha256 is not None
                else None
            ),
            envelope_canonical_sha256=(
                str(scope.envelope_canonical_sha256)
                if scope is not None and scope.envelope_canonical_sha256 is not None
                else None
            ),
            repartition_history=[
                asdict(record) for record in state.repartition_history
            ],
        )
        # Build once to select Lite or Full without exposing plan text to Lite.
        selection_context = build_manager_context(
            context_run_dir or run_paths.run_dir,
            level="lite", trigger=trigger,
            run_metadata={"team": baseline_team_name, "max_turns": config.max_turns, "turns_completed": state.turns_completed,
                          "original_plan_path": str(original_plan_path), "active_plan_path": str(active_plan_path)},
            boundary=boundary.__dict__,
        )
        signals = selection_context.get("controller_state")
        if isinstance(signals, dict):
            state.semantic_stall_count = int(signals.get("semantic_stall_count", 0) or 0)
            state.reviewer_rejection_count = int(signals.get("reviewer_rejection_count", 0) or 0)
        level = _manager_level_for_boundary(selection_context)
        decision, context, error = _run_manager_call(
            level=level,
            boundary=boundary,
            proposed_target_plan=proposed_target_plan,
            retry_target_plan=(
                active_plan_path
                if {"retry_current_step", "switch_to_backup_and_retry"} & eligible
                else None
            ),
            context_run_dir=context_run_dir,
        )
        if decision is None and level == "lite" and error != "manager mutated repository or plan state":
            boundary = FinalizedTurnBoundary(**{**boundary.__dict__, "trigger": "lite_invalid", "evidence": error})
            decision, context, error = _run_manager_call(
                level="full",
                boundary=boundary,
                proposed_target_plan=proposed_target_plan,
                retry_target_plan=(
                    active_plan_path
                    if {"retry_current_step", "switch_to_backup_and_retry"} & eligible
                    else None
                ),
                context_run_dir=context_run_dir,
            )
        elif decision is not None and decision.action == "escalate_to_full":
            boundary = FinalizedTurnBoundary(**{**boundary.__dict__, "trigger": "lite_escalation", "evidence": decision.reason})
            decision, context, error = _run_manager_call(
                level="full",
                boundary=boundary,
                proposed_target_plan=proposed_target_plan,
                retry_target_plan=(
                    active_plan_path
                    if {"retry_current_step", "switch_to_backup_and_retry"} & eligible
                    else None
                ),
                context_run_dir=context_run_dir,
            )
        if decision is None:
            _fail_manager_gate(
                context,
                reason=error or "manager did not return a valid decision",
            )
        if decision.action == "stop":
            _fail_manager_gate(context, reason=decision.reason, decision=decision)
        if decision.action == "repartition_current_checkpoint":
            _run_repartition_cycle(
                decision_context=context,
                disposition_targets=repartition_disposition_targets,
            )
            _apply_pending_repartition()
            pending = state.pending_repartition
            if pending is None or pending.resolved_target_step is None:
                raise WorkflowError(
                    "repartition application did not resolve a post-split target",
                    run_dir=run_paths.run_dir,
                )
            return pending.resolved_target_step

        target_step = current_step if decision.action in {"retry_current_step", "switch_to_backup_and_retry"} else proposed_transition
        target_config = wf.steps.get(target_step) if target_step not in {None, "END"} else None
        target_plan = active_plan_path if target_step == current_step else post_transition_active_path
        target_identity = _target_plan_identity(target_plan)
        scope_id = (
            state.active_implementation_scope.scope_id
            if state.active_implementation_scope is not None else None
        )
        active_partition_identity = (
            (
                state.active_implementation_scope.current_partition_generation_id,
                state.active_implementation_scope.current_partition_candidate_sha256,
                state.active_implementation_scope.current_partition_id,
            )
            if state.active_implementation_scope is not None
            else (None, None, None)
        )
        retain_scoped_team = (
            decision.action == "continue"
            and retrying_scoped_implementation
            and recent_team is not None
        )
        target_team = (
            upgrade.target_team
            if decision.action == "upgrade_next_implementation"
            else recent_team
            if retain_scoped_team
            else None
        )
        target_selector = upgrade.target_selector if decision.action == "upgrade_next_implementation" else None
        if target_config is not None and target_selector is None:
            resolution_team = (
                backup_team
                if decision.action == "switch_to_backup_and_retry"
                else active_team
                if decision.action == "retry_current_step"
                else target_team
                if target_team is not None
                else baseline_team_name
            )
            try:
                target_selector, _ = _resolve_step_runtime(
                    target_config, workflow_config,
                    team_name=resolution_team,
                    step_path=f"workflow.{workflow_name}.steps.{target_step}",
                )
            except WorkflowError:
                target_selector = None
        state.pending_boundary_decision = PendingBoundaryDecision(
            finalized_turn_number=boundary.finalized_turn_number,
            decision_number=state.manager_decision_number,
            action=decision.action, proposed_action=boundary.proposed_action,
            proposed_transition=proposed_transition, resolved_next_step=target_step,
            target_role=target_config.role if target_config is not None else None,
            target_team=target_team,
            target_selector=target_selector,
            checkpoint_identity=target_identity,
            scope_id=scope_id,
            target_plan_identity=target_identity,
            post_transition_active_plan_path=str(target_plan),
            post_transition_checkpoint_identity=target_identity,
            notes_reference=(f"manager/decision-{state.manager_decision_number:03d}" if decision.next_step_notes else None),
            repartition_generation_id=active_partition_identity[0],
            repartition_candidate_sha256=active_partition_identity[1],
            repartition_partition_id=active_partition_identity[2],
        )
        if decision.next_step_notes and target_step != "END":
            state.pending_manager_notes = PendingManagerNotes(
                target_step, decision.next_step_notes, state.manager_decision_number,
                target_role=target_config.role if target_config is not None else None,
                target_selector=target_selector,
                checkpoint_identity=target_identity,
                scope_id=scope_id,
                target_plan_identity=target_identity,
                repartition_generation_id=active_partition_identity[0],
                repartition_candidate_sha256=active_partition_identity[1],
                repartition_partition_id=active_partition_identity[2],
            )
        # This is intentionally before changing any controller routing state.
        write_run_metadata(run_paths, config, state, status="running", last_snapshot=state.last_snapshot,
                           workflow_name=workflow_name, original_plan_path=original_plan_path,
                           current_step_name=current_step_name, active_plan_path=active_plan_path,
                           new_plan_path=new_plan_path, resumed_from_run_id=resumed_from_run_id)
        if decision.action == "retry_current_step":
            return current_step
        if decision.action == "switch_to_backup_and_retry":
            backup_team, _ = resolve_backup_team(active_team, workflow_config.teams)
            if backup_team is None:
                raise WorkflowError("manager selected unavailable backup-team retry", run_dir=run_paths.run_dir)
            state.current_team_override = backup_team
            return current_step
        if decision.action == "upgrade_next_implementation":
            assert next_step is not None
            if not upgrade.available or upgrade.target_team is None or upgrade.target_selector is None:
                raise WorkflowError("manager selected unavailable implementation upgrade", run_dir=run_paths.run_dir)
            state.pending_step_team_override = PendingTeamOverride(
                target_step=next_step, role=wf.steps[next_step].role, source_team=upgrade.source_team,
                target_team=upgrade.target_team, selector=upgrade.target_selector,
                checkpoint_identity=target_identity, decision_number=state.manager_decision_number,
                scope_id=scope_id, target_plan_identity=target_identity,
                repartition_generation_id=active_partition_identity[0],
                repartition_candidate_sha256=active_partition_identity[1],
                repartition_partition_id=active_partition_identity[2],
            )
        elif (
            retain_scoped_team
            and next_step is not None
            and target_team is not None
            and target_selector is not None
        ):
            # A plain Full/Lite continuation inside the same rejected scope
            # retains the most recently reviewed worker. Baseline is restored
            # only when review closes the scope and checkpoint progress opens
            # a fresh one.
            state.pending_step_team_override = PendingTeamOverride(
                target_step=next_step,
                role=wf.steps[next_step].role,
                source_team=target_team,
                target_team=target_team,
                selector=target_selector,
                checkpoint_identity=target_identity,
                decision_number=state.manager_decision_number,
                scope_id=scope_id,
                target_plan_identity=target_identity,
                repartition_generation_id=active_partition_identity[0],
                repartition_candidate_sha256=active_partition_identity[1],
                repartition_partition_id=active_partition_identity[2],
            )
        return proposed_transition

    def _manager_terminal_incident(
        *,
        trigger: str,
        reason: str,
        current_step: str | None,
        current_role: str | None,
        active_team: str | None,
        active_selector: str | None,
    ) -> str | None:
        """Enrich a finalized controller incident without permitting recovery.

        Full receives a single closed protocol choice: ``stop``.  Any invalid
        output therefore falls through to the same deterministic report rather
        than accidentally opening a second manager loop.
        """
        if not workflow_config.manager.enabled:
            return None
        boundary = FinalizedTurnBoundary(
            finalized_turn_number=state.active_turn,
            artifact_path=f"turns/turn-{state.active_turn:03d}", trigger=trigger, terminal=True,
            proposed_action="stop", proposed_transition=None, current_step=current_step,
            current_role=current_role, baseline_team=baseline_team_name, actual_team=active_team,
            actual_selector=active_selector, original_plan_path=str(original_plan_path),
            active_plan_path=str(active_plan_path), checkpoint_identity=_target_plan_identity(active_plan_path),
            eligible_actions=["stop"], evidence=reason,
            scope_pressure_reason=state.scope_pressure_reason,
            repartition_history=[
                asdict(record) for record in state.repartition_history
            ],
        )
        decision, context, error = _run_manager_call(
            level="full",
            boundary=boundary,
            proposed_target_plan=None,
            retry_target_plan=None,
        )
        report = _write_manager_report(
            context,
            reason=reason if decision is not None else (error or reason),
            decision=decision if decision is not None and decision.action == "stop" else None,
        )
        state.last_manager_report_path = "manager-report.md"
        # Keep report state durable even though the caller owns the final
        # failure metadata and exception.
        write_run_metadata(run_paths, config, state, status="failed", failure_reason=report,
                           last_snapshot=state.last_snapshot, turns_completed=state.turns_completed,
                           workflow_name=workflow_name, original_plan_path=original_plan_path,
                           current_step_name=current_step_name, active_plan_path=active_plan_path,
                           new_plan_path=new_plan_path, resumed_from_run_id=resumed_from_run_id)
        return report

    def _prepare_pending_manager_notes(
        *,
        step_name: str,
        step_role: str,
        selector: str,
        target_plan_path: Path,
        active_team: str | None,
    ) -> tuple[tuple[str, ...], bool]:
        """Validate or correct one matching persisted manager note before launch.

        The correction marker is written before invoking Full, so a resume can
        never re-run a correction for the same durable pending record.
        """
        pending = state.pending_manager_notes
        if not (
            pending is not None
            and not pending.consumed
            and pending.target_step == step_name
            and (pending.target_role is None or pending.target_role == step_role)
            and (pending.target_selector is None or pending.target_selector == selector)
            and _pending_matches_scope_and_plan(
                pending,
                state,
                _target_plan_identity(target_plan_path),
            )
        ):
            return (), False

        scope = build_manager_note_scope(
            active_plan_identity=_target_plan_identity(target_plan_path),
            active_plan_content=_exec_plan_path(
                target_plan_path, exec_ctx
            ).read_text(encoding="utf-8"),
        )
        try:
            validate_manager_note_authority(pending.notes, scope=scope)
        except ManagerDecisionError as validation_error:
            if pending.correction_attempted:
                raise WorkflowError(
                    "pending manager notes already received their one Full "
                    "correction attempt and remain invalid",
                    run_dir=run_paths.run_dir,
                ) from validation_error

            state.pending_manager_notes = replace(
                pending,
                correction_attempted=True,
            )
            write_run_metadata(
                run_paths,
                config,
                state,
                status="running",
                last_snapshot=state.last_snapshot,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            boundary = FinalizedTurnBoundary(
                finalized_turn_number=state.active_turn,
                artifact_path=f"turns/turn-{state.active_turn:03d}",
                trigger="pending_notes_invalid",
                terminal=False,
                proposed_action="pending_note_correction",
                proposed_transition=step_name,
                current_step=step_name,
                current_role=step_role,
                baseline_team=baseline_team_name,
                actual_team=active_team,
                actual_selector=selector,
                original_plan_path=str(original_plan_path),
                active_plan_path=str(target_plan_path),
                checkpoint_identity=_target_plan_identity(target_plan_path),
                eligible_actions=["continue", "stop"],
                evidence=(
                    f"pending manager decision {pending.decision_number} is "
                    f"incompatible with current controller scope: {validation_error}"
                ),
                scope_pressure_reason=state.scope_pressure_reason,
                review_rejection_history=[
                    asdict(record) for record in state.review_rejection_history
                ],
                implementation_attempts={
                    scope_id: [asdict(attempt) for attempt in attempts]
                    for scope_id, attempts in state.implementation_attempts.items()
                },
                repartition_history=[
                    asdict(record) for record in state.repartition_history
                ],
            )
            decision, context, error = _run_manager_call(
                level="full",
                boundary=boundary,
                proposed_target_plan=target_plan_path,
                retry_target_plan=None,
            )
            if decision is None:
                _fail_manager_gate(
                    context,
                    reason=error or "pending-note correction returned invalid output",
                )
            if decision.action == "stop":
                _fail_manager_gate(context, reason=decision.reason, decision=decision)
            if decision.action != "continue":
                _fail_manager_gate(
                    context,
                    reason="pending-note correction selected an illegal action",
                )
            if decision.next_step_notes:
                state.pending_manager_notes = replace(
                    pending,
                    notes=decision.next_step_notes,
                    decision_number=state.manager_decision_number,
                    correction_attempted=True,
                )
            else:
                state.pending_manager_notes = None
            write_run_metadata(
                run_paths,
                config,
                state,
                status="running",
                last_snapshot=state.last_snapshot,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            corrected = state.pending_manager_notes
            return (
                corrected.notes if corrected is not None else (),
                corrected is not None,
            )
        return pending.notes, True

    if terminal_integration_only:
        if not done:
            raise WorkflowError(
                "terminal integration resume requires a complete saved plan",
                run_dir=run_paths.run_dir,
            )
        if exec_ctx is None or "merge" not in exec_ctx.teardown:
            raise WorkflowError(
                "terminal integration resume requires recorded merge teardown",
                run_dir=run_paths.run_dir,
            )

        merge_status, merge_failure_reason = _perform_merge_teardown(
            exec_ctx,
            wf,
            workflow_config,
            repo_root=config.repo_root,
            team_name=baseline_team_name,
            adapter=adapter,
            runner=runner,
            config_dir=config_dir,
            working_dir=working_dir,
            original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            banner=banner,
            state=state,
        )
        if merge_status == "failed":
            state.status_message = "failed"
            current_step = wf.steps.get(current_step_name)
            report = _manager_terminal_incident(
                trigger="merge_failure",
                reason=merge_failure_reason or "merge teardown failed",
                current_step=current_step_name,
                current_role=current_step.role if current_step is not None else None,
                active_team=baseline_team_name,
                active_selector=None,
            )
            summary = report or _format_failure(
                reason=merge_failure_reason or "merge teardown failed",
                run_dir=run_paths.run_dir,
                snapshot=original_snapshot,
            )
            write_run_metadata(
                run_paths,
                config,
                state,
                status="failed",
                merge_status=merge_status,
                merge_failure_reason=merge_failure_reason,
                execution_context=exec_ctx,
                last_snapshot=original_snapshot,
                turns_completed=0,
                workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            prune_old_runs(run_paths.runs_root, config.keep_runs)
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir)

        prior_original_plan_path = original_plan_path
        finalized_original_plan_path = _finalize_original_plan_if_complete(
            config.repo_root,
            original_plan_path,
            snapshot=original_snapshot,
        )
        if finalized_original_plan_path != prior_original_plan_path:
            original_plan_path = finalized_original_plan_path
            if active_plan_path == prior_original_plan_path:
                active_plan_path = original_plan_path

        end_reason: WorkflowEndReason = "transition_end"
        state.end_reason = end_reason
        state.status_message = "completed"
        _emit_event(
            observer,
            StatusChangedEvent.create(
                status_message="completed",
                turns_completed=0,
                active_turn=None,
                current_step_name=current_step_name,
            ),
        )
        result = ControllerRunResult(
            run_dir=run_paths.run_dir,
            turns_completed=0,
            final_snapshot=original_snapshot,
            issues_accumulated=state.issues_accumulated,
            end_reason=end_reason,
            recovery_summary=state.current_harness_recovery,
            recovery_history=tuple(state.harness_recovery_history),
        )
        write_run_metadata(
            run_paths,
            config,
            state,
            status="completed",
            merge_status=merge_status,
            execution_context=exec_ctx,
            last_snapshot=original_snapshot,
            turns_completed=0,
            end_reason=end_reason,
            workflow_name=workflow_name,
            original_plan_path=original_plan_path,
            current_step_name=current_step_name,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )
        prune_old_runs(run_paths.runs_root, config.keep_runs)
        banner.stop(state)
        _emit_event(
            observer,
            RunCompletedEvent.create(
                run_dir=run_paths.run_dir,
                turns_completed=0,
                final_snapshot=original_snapshot,
                end_reason=end_reason,
                issues_accumulated=state.issues_accumulated,
                recovery_summary=state.current_harness_recovery,
                recovery_history=tuple(state.harness_recovery_history),
            ),
        )
        return result

    replayed_boundary = (
        resume.pending_finalized_turn
        if resume is not None
        else None
    )
    if replayed_boundary is not None:
        replayed_step = wf.steps.get(replayed_boundary.step_name)
        if replayed_step is None or replayed_step.role != replayed_boundary.step_role:
            raise WorkflowError(
                "cannot resume finalized turn boundary because its workflow "
                "step or role no longer matches configuration",
                run_dir=run_paths.run_dir,
            )
        matching_transitions = [
            transition
            for transition in replayed_step.go
            if (
                transition.to == replayed_boundary.chosen_transition
                and transition.when
                == replayed_boundary.chosen_transition_condition
            )
        ]
        if len(matching_transitions) != 1:
            raise WorkflowError(
                "cannot resume finalized turn boundary because its selected "
                "transition no longer matches configuration",
                run_dir=run_paths.run_dir,
            )
        if replayed_boundary.chosen_transition == "END":
            raise WorkflowError(
                "cannot yet resume a finalized terminal turn before its "
                "manager boundary",
                run_dir=run_paths.run_dir,
            )

        state.last_snapshot = replayed_boundary.snapshot_after
        state.turns_completed = 0
        replayed_active_plan_path = replayed_boundary.active_plan_path
        if (
            not _exec_plan_path(replayed_active_plan_path, exec_ctx).is_file()
            and _resume_completed_worker_can_use_original_plan(
                pending_turn=replayed_boundary,
                active_plan_path=replayed_active_plan_path,
                original_plan_path=original_plan_path,
                active_scope=state.active_implementation_scope,
                exec_ctx=exec_ctx,
            )
        ):
            replayed_active_plan_path = original_plan_path
        active_plan_path = (
            replayed_boundary.new_plan_path
            if replayed_boundary.conditions["NEW_PLAN_EXISTS"]
            else replayed_active_plan_path
        )
        new_plan_path = replayed_boundary.new_plan_path
        scope = state.active_implementation_scope
        source_scope = (
            resume.active_implementation_scope
            if resume is not None
            else None
        )
        if scope is not None and source_scope is not None:
            state.active_implementation_scope = replace(
                scope,
                awaiting_review=(
                    False
                    if replayed_boundary.step_role != "worker"
                    else scope.awaiting_review
                ),
                # Evaluate the missing boundary against the source run's own
                # scope window and carry, not the rebased new-run values.
                opened_turn_number=source_scope.opened_turn_number,
                carried_reviewer_rejection_count=(
                    source_scope.carried_reviewer_rejection_count
                ),
            )
        post_transition_active_path = _select_next_active_plan_path(
            original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            new_plan_exists=replayed_boundary.conditions["NEW_PLAN_EXISTS"],
            selected_transition=matching_transitions[0],
            exec_ctx=exec_ctx,
        )
        current_step_name = _manager_gate(
            proposed_transition=replayed_boundary.chosen_transition,
            current_step=replayed_boundary.step_name,
            current_role=replayed_boundary.step_role,
            active_team=baseline_team_name,
            active_selector=replayed_boundary.selector,
            post_transition_active_path=post_transition_active_path,
            context_run_dir=replayed_boundary.source_run_dir,
            finalized_turn_number=replayed_boundary.turn_number,
            artifact_path=(
                f"resumed-from/{replayed_boundary.source_run_dir.name}/"
                f"turns/turn-{replayed_boundary.turn_number:03d}"
            ),
        )
        scope = state.active_implementation_scope
        if scope is not None:
            state.active_implementation_scope = replace(
                scope,
                opened_turn_number=1,
                carried_reviewer_rejection_count=(
                    state.reviewer_rejection_count
                ),
            )
        state.current_team_override = None
        active_plan_path = post_transition_active_path
        banner.set_context(
            active_plan_path=active_plan_path,
            new_plan_path=(
                new_plan_path
                if replayed_boundary.conditions["NEW_PLAN_EXISTS"]
                else None
            ),
        )
        write_run_metadata(
            run_paths,
            config,
            state,
            status="running",
            execution_context=exec_ctx,
            last_snapshot=state.last_snapshot,
            turns_completed=state.turns_completed,
            workflow_name=workflow_name,
            original_plan_path=original_plan_path,
            current_step_name=current_step_name,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )

    def _write_override_boundary(*, status: str) -> None:
        write_run_metadata(
            run_paths,
            config,
            state,
            status=status,
            execution_context=exec_ctx,
            last_snapshot=state.last_snapshot,
            turns_completed=state.turns_completed,
            workflow_name=workflow_name,
            original_plan_path=original_plan_path,
            current_step_name=current_step_name,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )

    def _apply_boundary_override() -> tuple[str, str | None]:
        nonlocal current_step_name, baseline_team_name
        source_run_dir = state.override_source_run_dir or run_paths.run_dir
        override_path = source_run_dir / "overrides.toml"
        prior = state.override_result
        required_predecessor_override = (
            state.override_source_run_dir is not None
            and source_run_dir.resolve() != run_paths.run_dir.resolve()
        )
        if (
            prior is not None
            and prior.status == "accepted"
            and not prior.applied
        ):
            if prior.next_step is not None:
                current_step_name = prior.next_step
            if prior.team is not None:
                state.current_team = prior.team
                state.current_team_override = None
                baseline_team_name = prior.team
            if prior.max_turns is not None:
                state.effective_max_turns = prior.max_turns
            state.override_result = replace(prior, applied=True)
            state.override_source_run_dir = None
            _write_override_boundary(status="running")
            if preserve_resume_override_source:
                prune_old_runs(run_paths.runs_root, config.keep_runs)
            return current_step_name, baseline_team_name
        consumed_digest = (
            prior.digest
            if prior is not None and prior.status == "accepted"
            else None
        )
        loaded = load_override_request(
            override_path,
            consumed_digest=consumed_digest,
        )
        state.override_file_present = loaded.status != "absent"
        if loaded.status == "absent" and not required_predecessor_override:
            return current_step_name, baseline_team_name
        if (
            loaded.status == "already_consumed"
            and prior is not None
            and prior.status == "accepted"
            and prior.applied
        ):
            return current_step_name, baseline_team_name
        request = loaded.request
        validation_error = loaded.message
        if loaded.status == "absent" and required_predecessor_override:
            validation_error = (
                "required predecessor override file is missing; restore or "
                f"correct '{override_path}' before resuming"
            )
        if loaded.status == "valid" and request is not None:
            target_step = request.next_step or current_step_name
            if target_step not in wf.steps:
                validation_error = (
                    f"next_step '{target_step}' is not an executable step in "
                    f"workflow '{workflow_name}'"
                )
            target_team = request.team or state.current_team
            if validation_error is None and request.team is not None:
                if request.team not in workflow_config.teams:
                    validation_error = f"team '{request.team}' is not configured"
                else:
                    try:
                        _resolve_step_runtime(
                            wf.steps[target_step],
                            workflow_config,
                            team_name=target_team,
                            step_path=(
                                f"workflow.{workflow_name}.steps.{target_step}"
                            ),
                        )
                    except Exception as exc:
                        validation_error = (
                            f"team '{request.team}' is incompatible with step "
                            f"'{target_step}': {exc}"
                        )
            if (
                validation_error is None
                and request.max_turns is not None
                and request.max_turns < state.turns_completed
            ):
                validation_error = (
                    f"max_turns ({request.max_turns}) cannot be below completed "
                    f"turns ({state.turns_completed})"
                )

        if validation_error is not None or request is None:
            digest = (
                loaded.digest
                or (prior.digest if prior is not None else None)
                or hashlib.sha256(
                    (validation_error or "invalid override").encode("utf-8")
                ).hexdigest()
            )
            state.override_result = OverrideResult(
                status="rejected",
                digest=digest,
                message=validation_error or "invalid override request",
                source_text=loaded.source_text,
            )
            state.status_message = (
                "waiting_for_valid_override: "
                f"{state.override_result.message}"
            )
            state.override_source_run_dir = source_run_dir
            _write_override_boundary(status="waiting_for_valid_override")
            if preserve_resume_override_source:
                prune_old_runs(run_paths.runs_root, config.keep_runs)
            banner.stop(state)
            raise WorkflowError(
                state.status_message,
                run_dir=run_paths.run_dir,
            )

        state.pending_override_notes = request.notes
        state.override_result = OverrideResult(
            status="accepted",
            digest=request.digest,
            message="override accepted at pre-turn boundary",
            source_text=request.source_text,
            next_step=request.next_step,
            team=request.team,
            max_turns=request.max_turns,
            has_notes=bool(request.notes),
            applied=False,
        )
        _write_override_boundary(status="running")

        if request.next_step is not None:
            current_step_name = request.next_step
        if request.team is not None:
            state.current_team = request.team
            state.current_team_override = None
            baseline_team_name = request.team
        if request.max_turns is not None:
            state.effective_max_turns = request.max_turns
        state.override_result = replace(state.override_result, applied=True)
        state.override_source_run_dir = None
        _write_override_boundary(status="running")
        if preserve_resume_override_source:
            prune_old_runs(run_paths.runs_root, config.keep_runs)
        return current_step_name, baseline_team_name

    # A resumed transaction is reconciled before the first harness. Accepted
    # proposal/validation artifacts are reused; no Full subcall is replayed.
    if state.pending_repartition is not None:
        _apply_pending_repartition()

    turn_number = 1
    while turn_number <= (state.effective_max_turns or config.max_turns):
        current_step_name, baseline_team_name = _apply_boundary_override()
        effective_max_turns = state.effective_max_turns or config.max_turns
        if turn_number > effective_max_turns:
            break
        retry_ctx = state.pending_retry
        active_team_name = (
            state.current_team_override
            if state.current_team_override is not None
            else state.current_team
        )
        followup_candidates_before: set[Path] = set()
        consume_manager_notes = False
        consume_team_override = False

        if retry_ctx is not None:
            state.status_message = (
                f"running turn {turn_number}: step {current_step_name} "
                f"(retry {retry_ctx.attempt}/{retry_ctx.retry_limit})"
            )
            write_run_metadata(
                run_paths, config, state, status="running", last_snapshot=state.last_snapshot,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=retry_ctx.active_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            done = retry_ctx.snapshot_before.is_complete
            active_plan_path = retry_ctx.active_plan_path
            new_plan_path = retry_ctx.new_plan_path
            followup_candidates_before = _list_followup_plan_candidates(
                _exec_plan_path(original_plan_path, exec_ctx)
            )
            step = wf.steps[current_step_name]
            step_path = f"workflow.{workflow_name}.steps.{current_step_name}"
            if step.role == "worker":
                try:
                    _scope, scope_was_opened = _open_implementation_scope(
                        state,
                        original_plan_path=original_plan_path,
                        original_snapshot=state.last_snapshot,
                        turn_number=turn_number,
                    )
                    if scope_was_opened and not state.last_snapshot.is_complete:
                        _capture_scope_envelope(
                            state,
                            plan_text=None,
                            primary_plan_path=original_plan_path,
                            run_dir=run_paths.run_dir,
                            exec_ctx=exec_ctx,
                            repo_root=config.repo_root,
                        )
                        write_run_metadata(
                            run_paths, config, state, status="running",
                            execution_context=exec_ctx,
                            last_snapshot=state.last_snapshot,
                            turns_completed=state.turns_completed,
                            workflow_name=workflow_name,
                            original_plan_path=original_plan_path,
                            current_step_name=current_step_name,
                            active_plan_path=active_plan_path,
                            new_plan_path=new_plan_path,
                            resumed_from_run_id=resumed_from_run_id,
                        )
                    elif not scope_was_opened:
                        _validate_existing_scope_envelope(
                            run_paths.run_dir,
                            _scope,
                        )
                except WorkflowError as exc:
                    _raise_pre_turn_failure(
                        reason=exc.summary,
                        snapshot=retry_ctx.snapshot_before,
                        active_path=active_plan_path,
                        new_path=new_plan_path,
                    )
            pending_override = state.pending_step_team_override
            if (
                pending_override is not None
                and not pending_override.consumed
                and pending_override.target_step == current_step_name
                and pending_override.role == step.role
                and _pending_matches_scope_and_plan(
                    pending_override,
                    state,
                    _target_plan_identity(active_plan_path),
                )
            ):
                active_team_name = pending_override.target_team
                consume_team_override = True
            selector, resolved = _resolve_step_runtime(
                step,
                workflow_config,
                team_name=active_team_name,
                step_path=step_path,
            )
            step_adapter = adapter or get_adapter(resolved.harness_name)
            snapshot_before = retry_ctx.snapshot_before
            manager_notes, consume_manager_notes = _prepare_pending_manager_notes(
                step_name=current_step_name,
                step_role=step.role,
                selector=selector,
                target_plan_path=active_plan_path,
                active_team=active_team_name,
            )
            try:
                user_prompt = retry_ctx.base_user_prompt + "\n\n" + _build_retry_appendix(retry_ctx.parse_error_str)
                if manager_notes:
                    user_prompt += "\n\n## Manager notes for this turn\n" + "\n".join(
                        f"- {note}" for note in manager_notes
                    )
                if step.role == "worker" and state.pending_override_notes:
                    user_prompt += (
                        "\n\n## User override notes for this turn\n"
                        + "\n".join(
                            f"- {note}" for note in state.pending_override_notes
                        )
                    )
                invocation = step_adapter.build_invocation(
                    repo_root=execution_repo_root,
                    model=resolved.model,
                    system_prompt="",
                    user_prompt=user_prompt,
                    effort=resolved.effort,
                )
            except Exception as exc:
                _raise_pre_turn_failure(
                    reason=str(exc),
                    snapshot=snapshot_before,
                    active_path=active_plan_path,
                    new_path=new_plan_path,
                )
        else:
            state.status_message = f"running turn {turn_number}: step {current_step_name}"
            write_run_metadata(
                run_paths, config, state, status="running", last_snapshot=state.last_snapshot,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            current_step_name=current_step_name, active_plan_path=active_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )

            _sync_plan_to_worktree(original_plan_path, exec_ctx)

            try:
                current_plan = load_plan(original_plan_path)
            except (PlanParseError, FileNotFoundError) as exc:
                state.status_message = "failed"
                banner.stop(state)
                summary = _format_failure(
                    reason=str(exc),
                    run_dir=run_paths.run_dir,
                    snapshot=state.last_snapshot,
                )
                write_run_metadata(
                    run_paths, config, state, status="failed", failure_reason=summary,
                    workflow_name=workflow_name, original_plan_path=original_plan_path,
                    current_step_name=current_step_name, active_plan_path=active_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc

            done = current_plan.snapshot.is_complete
            checkpoint_index = current_plan.snapshot.current_checkpoint_index

            execution_original_plan_path = _exec_plan_path(
                original_plan_path,
                exec_ctx,
            )
            execution_new_plan_path = generate_new_plan_path(
                execution_original_plan_path,
                checkpoint_index=checkpoint_index,
            )
            new_plan_path = _primary_plan_path(
                execution_new_plan_path,
                exec_ctx,
            )

            step = wf.steps[current_step_name]
            step_path = f"workflow.{workflow_name}.steps.{current_step_name}"
            if step.role == "worker":
                try:
                    _scope, scope_was_opened = _open_implementation_scope(
                        state,
                        original_plan_path=original_plan_path,
                        original_snapshot=current_plan.snapshot,
                        turn_number=turn_number,
                    )
                    if scope_was_opened and not current_plan.snapshot.is_complete:
                        _capture_scope_envelope(
                            state,
                            plan_text=None,
                            primary_plan_path=original_plan_path,
                            run_dir=run_paths.run_dir,
                            exec_ctx=exec_ctx,
                            repo_root=config.repo_root,
                        )
                        write_run_metadata(
                            run_paths, config, state, status="running",
                            execution_context=exec_ctx,
                            last_snapshot=current_plan.snapshot,
                            turns_completed=state.turns_completed,
                            workflow_name=workflow_name,
                            original_plan_path=original_plan_path,
                            current_step_name=current_step_name,
                            active_plan_path=active_plan_path,
                            new_plan_path=new_plan_path,
                            resumed_from_run_id=resumed_from_run_id,
                        )
                    elif not scope_was_opened:
                        _validate_existing_scope_envelope(
                            run_paths.run_dir,
                            _scope,
                        )
                except WorkflowError as exc:
                    _raise_pre_turn_failure(
                        reason=exc.summary,
                        snapshot=current_plan.snapshot,
                        active_path=active_plan_path,
                        new_path=new_plan_path,
                    )
            pending_override = state.pending_step_team_override
            if (
                pending_override is not None
                and not pending_override.consumed
                and pending_override.target_step == current_step_name
                and pending_override.role == step.role
                and _pending_matches_scope_and_plan(
                    pending_override,
                    state,
                    _target_plan_identity(active_plan_path),
                )
            ):
                active_team_name = pending_override.target_team
                consume_team_override = True
            selector, resolved = _resolve_step_runtime(
                step,
                workflow_config,
                team_name=active_team_name,
                step_path=step_path,
            )

            step_adapter = adapter or get_adapter(resolved.harness_name)
            snapshot_before = state.last_snapshot

            _sync_plan_to_worktree(original_plan_path, exec_ctx)
            followup_candidates_before = _list_followup_plan_candidates(
                _exec_plan_path(original_plan_path, exec_ctx)
            )
            manager_notes, consume_manager_notes = _prepare_pending_manager_notes(
                step_name=current_step_name,
                step_role=step.role,
                selector=selector,
                target_plan_path=active_plan_path,
                active_team=active_team_name,
            )

            try:
                user_prompt = render_step_prompts(
                    step,
                    workflow_config,
                    config_dir=config_dir,
                    working_dir=working_dir,
                    original_plan_path=_exec_plan_path(original_plan_path, exec_ctx),
                    new_plan_path=_exec_plan_path(new_plan_path, exec_ctx),
                    active_plan_path=_exec_plan_path(active_plan_path, exec_ctx),
                )

                if config.extra_instructions:
                    extra_text = " ".join(config.extra_instructions).strip()
                    user_prompt = "\n\n".join((user_prompt, extra_text))

                if manager_notes:
                    user_prompt += "\n\n## Manager notes for this turn\n" + "\n".join(
                        f"- {note}" for note in manager_notes
                    )
                if step.role == "worker" and state.pending_override_notes:
                    user_prompt += (
                        "\n\n## User override notes for this turn\n"
                        + "\n".join(
                            f"- {note}" for note in state.pending_override_notes
                        )
                    )

                invocation = step_adapter.build_invocation(
                    repo_root=execution_repo_root,
                    model=resolved.model,
                    system_prompt="",
                    user_prompt=user_prompt,
                    effort=resolved.effort,
                )
            except Exception as exc:
                _raise_pre_turn_failure(
                    reason=str(exc),
                    snapshot=snapshot_before,
                    active_path=active_plan_path,
                    new_path=new_plan_path,
                )

        if step.role == "worker":
            state.pending_override_notes = ()
        if step.role == "worker":
            write_run_metadata(
                run_paths, config, state, status="running",
                last_snapshot=state.last_snapshot, workflow_name=workflow_name,
                original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path, resumed_from_run_id=resumed_from_run_id,
            )

        turn_dir, turn_started_at = _start_turn(
            turn_number=turn_number,
            step_name=current_step_name,
            step=step,
            step_role=step.role,
            resolved_selector=selector,
            resolved=resolved,
            active_path=active_plan_path,
            new_path=new_plan_path,
            invocation=invocation,
            snapshot_before=snapshot_before,
        )
        if consume_manager_notes:
            state.pending_manager_notes = None
        if consume_team_override:
            state.pending_step_team_override = None
        boundary_target_started = (
            state.pending_boundary_decision is not None
            and not state.pending_boundary_decision.consumed
            and state.pending_boundary_decision.resolved_next_step == current_step_name
            and (state.pending_boundary_decision.target_role is None or state.pending_boundary_decision.target_role == step.role)
            and _pending_matches_scope_and_plan(
                state.pending_boundary_decision,
                state,
                _target_plan_identity(active_plan_path),
            )
            and (state.pending_boundary_decision.target_selector is None or state.pending_boundary_decision.target_selector == selector)
        )
        if consume_manager_notes or consume_team_override or boundary_target_started:
            if boundary_target_started:
                completed_repartition_boundary = (
                    state.pending_boundary_decision.action
                    == "repartition_current_checkpoint"
                )
                state.pending_boundary_decision = replace(
                    state.pending_boundary_decision, applied=True, consumed=True
                )
                if completed_repartition_boundary:
                    state.pending_repartition = None
            write_run_metadata(
                run_paths, config, state, status="running", last_snapshot=state.last_snapshot,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path, resumed_from_run_id=resumed_from_run_id,
            )

        if use_popen:
            completed = _run_process(invocation, execution_repo_root, banner, state)
        else:
            assert runner is not None
            completed = _run_injected_runner(runner, invocation, execution_repo_root)

        stop_reason = _detect_stop_marker(completed.stdout, completed.stderr)
        if stop_reason is not None:
            state.status_message = "failed"
            _record_issue("aflow-stop", f"AFLOW_STOP: {stop_reason}", turn_dir=turn_dir)
            _finalize_turn_record(
                status="harness-failed",
                started_at=turn_started_at,
                snapshot_before=snapshot_before,
                snapshot_after=None,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                error=f"AFLOW_STOP: {stop_reason}",
                step_name=current_step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
            )
            report = _manager_terminal_incident(
                trigger="explicit_stop", reason=f"AFLOW_STOP: {stop_reason}",
                current_step=current_step_name, current_role=step.role,
                active_team=active_team_name, active_selector=selector,
            )
            summary = report or _format_failure(
                reason=f"workflow stopped by explicit AFLOW_STOP marker: {stop_reason}",
                run_dir=run_paths.run_dir, snapshot=snapshot_before,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                turns_completed=state.turns_completed,
                execution_context=exec_ctx,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir)

        try:
            exec_original = _exec_plan_path(original_plan_path, exec_ctx)
            resolved_exec_plan_path = _resolve_post_turn_original_plan_path(
                execution_repo_root,
                exec_original,
                completed_returncode=completed.returncode,
            )
            parsed_after = load_plan(resolved_exec_plan_path)

            # Sync the original plan back after every worktree turn so the
            # primary checkout remains the durable source of truth between turns.
            if exec_ctx is not None and exec_ctx.worktree_path is not None:
                _sync_plan_from_worktree(original_plan_path, exec_ctx)

            if resolved_exec_plan_path != exec_original:
                if exec_ctx is not None and exec_ctx.worktree_path is not None:
                    try:
                        rel = resolved_exec_plan_path.relative_to(execution_repo_root)
                        original_plan_path = config.repo_root / rel
                    except ValueError:
                        original_plan_path = resolved_exec_plan_path
                else:
                    original_plan_path = resolved_exec_plan_path
                if active_plan_path == config.plan_path:
                    active_plan_path = original_plan_path
            resolved_exec_new_plan_path = _resolve_post_turn_new_plan_path(
                original_plan_path=resolved_exec_plan_path,
                expected_new_plan_path=_exec_plan_path(new_plan_path, exec_ctx),
                candidates_before=followup_candidates_before,
            )
            if resolved_exec_new_plan_path is not None:
                new_plan_path = _primary_plan_path(resolved_exec_new_plan_path, exec_ctx)
            post_snapshot = parsed_after.snapshot
        except (PlanParseError, FileNotFoundError) as exc:
            is_retryable = (
                isinstance(exc, PlanParseError)
                and exc.error_kind == "inconsistent_checkpoint_state"
                and completed.returncode == 0
            )
            current_attempt = (retry_ctx.attempt if retry_ctx is not None else 0) + 1
            base_prompt = retry_ctx.base_user_prompt if retry_ctx is not None else user_prompt

            if is_retryable and current_attempt <= retry_limit and turn_number < effective_max_turns:
                _record_issue("retry-scheduled", str(exc), turn_dir=turn_dir)
                state.turns_completed += 1
                new_retry_ctx = RetryContext(
                    step_name=current_step_name,
                    step_role=step.role,
                    resolved_selector=selector,
                    resolved_harness_name=resolved.harness_name,
                    resolved_model=resolved.model,
                    resolved_effort=resolved.effort,
                    snapshot_before=snapshot_before,
                    active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    base_user_prompt=base_prompt,
                    parse_error_str=str(exc),
                    attempt=current_attempt,
                    retry_limit=retry_limit,
                )
                state.pending_retry = new_retry_ctx
                _finalize_turn_record(
                    status="retry-scheduled",
                    started_at=turn_started_at,
                    snapshot_before=snapshot_before,
                    snapshot_after=None,
                    invocation=invocation,
                    turn_dir=turn_dir,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    returncode=completed.returncode,
                    error=str(exc),
                    step_name=current_step_name,
                    step_role=step.role,
                    selector=selector,
                    active_path=active_plan_path,
                    new_path=new_plan_path,
                    conditions={"DONE": done, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": turn_number >= effective_max_turns},
                    retry_attempt=current_attempt,
                    retry_limit_value=retry_limit,
                    retry_reason="inconsistent_checkpoint_state",
                    retry_next_turn=True,
                )
                write_run_metadata(
                    run_paths, config, state, status="running",
                    turns_completed=state.turns_completed,
                    last_snapshot=state.last_snapshot,
                    workflow_name=workflow_name, original_plan_path=original_plan_path,
                    current_step_name=current_step_name, active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                banner.update(state)
                turn_number += 1
                continue

            state.pending_retry = None
            state.status_message = "failed"
            _record_issue("plan-invalid", str(exc), turn_dir=turn_dir)
            _finalize_turn_record(
                status="plan-invalid",
                started_at=turn_started_at,
                snapshot_before=snapshot_before,
                snapshot_after=None,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                error=str(exc),
                step_name=current_step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions={"DONE": done, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": turn_number >= effective_max_turns},
            )
            report = _manager_terminal_incident(
                trigger="invalid_plan", reason=str(exc), current_step=current_step_name,
                current_role=step.role, active_team=active_team_name, active_selector=selector,
            )
            summary = report or _format_failure(
                reason=str(exc), run_dir=run_paths.run_dir, snapshot=snapshot_before,
                parse_error=exc if isinstance(exc, PlanParseError) else None,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                turns_completed=state.turns_completed,
                execution_context=exec_ctx,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc

        state.pending_retry = None

        if _handle_harness_recovery(
            turn_number=turn_number,
            step_name=current_step_name,
            step=step,
            step_path=step_path,
            active_team_name=active_team_name,
            selector=selector,
            resolved=resolved,
            invocation=invocation,
            turn_dir=turn_dir,
            started_at=turn_started_at,
            snapshot_before=snapshot_before,
            snapshot_after=post_snapshot,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        ):
            turn_number += 1
            continue

        state.consecutive_harness_recoveries = 0

        if completed.returncode != 0:
            state.status_message = "failed"
            _record_issue(
                "harness-failed",
                f"harness '{invocation.label}' exited with code {completed.returncode}",
                turn_dir=turn_dir,
            )
            _finalize_turn_record(
                status="harness-failed",
                started_at=turn_started_at,
                snapshot_before=snapshot_before,
                snapshot_after=post_snapshot,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                step_name=current_step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions={"DONE": post_snapshot.is_complete, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": turn_number >= effective_max_turns},
            )
            report = _manager_terminal_incident(
                trigger="ambiguous_failure",
                reason=f"harness '{invocation.label}' exited with code {completed.returncode}",
                current_step=current_step_name, current_role=step.role,
                active_team=active_team_name, active_selector=selector,
            )
            summary = report or _format_failure(
                reason=f"harness '{invocation.label}' exited with code {completed.returncode}",
                run_dir=run_paths.run_dir, snapshot=post_snapshot,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                turns_completed=state.turns_completed,
                last_snapshot=post_snapshot,
                execution_context=exec_ctx,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir)

        state.last_snapshot = post_snapshot
        state.turns_completed += 1

        done = post_snapshot.is_complete
        new_plan_exists = _exec_plan_path(new_plan_path, exec_ctx).is_file()

        if new_plan_exists:
            active_plan_path = new_plan_path

        max_turns_reached = turn_number >= effective_max_turns

        conditions = {
            "DONE": done,
            "NEW_PLAN_EXISTS": new_plan_exists,
            "MAX_TURNS_REACHED": max_turns_reached,
        }

        selected_transition: GoTransition | None = None
        transition_target: str | None = None
        try:
            selected_transition = _select_transition(
                step.go,
                step_path=step_path,
                done=done,
                new_plan_exists=new_plan_exists,
                max_turns_reached=max_turns_reached,
            )
            transition_target = selected_transition.to
        except WorkflowError as exc:
            state.status_message = "failed"
            _record_issue("transition-failed", exc.summary, turn_dir=turn_dir)
            _finalize_turn_record(
                status="transition-failed",
                started_at=turn_started_at,
                snapshot_before=snapshot_before,
                snapshot_after=post_snapshot,
                invocation=invocation,
                turn_dir=turn_dir,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                step_name=current_step_name,
                step_role=step.role,
                selector=selector,
                active_path=active_plan_path,
                new_path=new_plan_path,
                conditions=conditions,
            )
            report = _manager_terminal_incident(
                trigger="illegal_transition", reason=exc.summary, current_step=current_step_name,
                current_role=step.role, active_team=active_team_name, active_selector=selector,
            )
            summary = report or _format_failure(
                reason=exc.summary, run_dir=run_paths.run_dir, snapshot=state.last_snapshot,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                turns_completed=state.turns_completed,
                last_snapshot=state.last_snapshot,
                execution_context=exec_ctx,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc

        review_rejection: ReviewRejectionRecord | None = None
        scope_before_finalize = state.active_implementation_scope
        controller_next_step = (
            wf.steps.get(transition_target)
            if transition_target != "END" else None
        )
        is_scoped_rejection = (
            scope_before_finalize is not None
            and scope_before_finalize.awaiting_review
            and step.role != "worker"
            and not done
            and snapshot_before == post_snapshot
            and controller_next_step is not None
            and controller_next_step.role == "worker"
        )
        if is_scoped_rejection:
            attempts = state.implementation_attempts.get(scope_before_finalize.scope_id, [])
            if not attempts:
                raise WorkflowError(
                    "internal error: review rejection has no implementation attempt",
                    run_dir=run_paths.run_dir,
                )
            reviewed_attempt = attempts[-1]
            repair_path = new_plan_path if new_plan_exists else None
            try:
                repair_path_text = str(repair_path.relative_to(run_paths.repo_root)) if repair_path else None
            except ValueError:
                repair_path_text = str(repair_path) if repair_path else None
            matching = [
                item.rejection_number for item in state.review_rejection_history
                if item.scope_id == scope_before_finalize.scope_id
            ]
            review_rejection = ReviewRejectionRecord(
                scope_id=scope_before_finalize.scope_id,
                rejection_number=max(matching, default=0) + 1,
                source_run_id=state.run_id or run_paths.run_dir.name,
                review_turn_number=turn_number,
                review_step_name=current_step_name,
                reviewer_selector=selector,
                checkpoint_index=scope_before_finalize.checkpoint_index,
                checkpoint_name=scope_before_finalize.checkpoint_name,
                reviewed_implementation_turn_number=reviewed_attempt.turn_number,
                reviewed_worker_team=reviewed_attempt.team,
                reviewed_worker_selector=reviewed_attempt.selector,
                review_summary=summarize_review_rejection(completed.stdout),
                repair_plan_summary=summarize_repair_plan(repair_path),
                review_stdout_artifact_path=_turn_artifact_display_path(
                    run_paths.repo_root, turn_dir, "stdout.txt",
                    content=completed.stdout,
                ),
                repair_plan_path=repair_path_text,
            )

        _finalize_turn_record(
            status="completed" if done else "running",
            started_at=turn_started_at,
            snapshot_before=snapshot_before,
            snapshot_after=post_snapshot,
            invocation=invocation,
            turn_dir=turn_dir,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            step_name=current_step_name,
            step_role=step.role,
            selector=selector,
            active_path=active_plan_path,
            new_path=new_plan_path,
            conditions=conditions,
            chosen_transition=transition_target,
            chosen_transition_condition=selected_transition.when,
            end_reason=(
                _normalize_end_reason(
                    selected_transition=selected_transition,
                    done=done,
                    max_turns_reached=max_turns_reached,
                )
                if transition_target == "END" and selected_transition is not None
                else None
            ),
            was_retry=True if retry_ctx is not None else None,
            retry_attempt=retry_ctx.attempt if retry_ctx is not None else None,
            review_rejection=review_rejection,
        )
        if review_rejection is not None:
            state.review_rejection_history.append(review_rejection)

        if step.role == "worker":
            scope = state.active_implementation_scope
            if scope is None:
                raise WorkflowError(
                    "internal error: worker turn finalized without an implementation scope",
                    run_dir=run_paths.run_dir,
                )
            state.implementation_attempts.setdefault(scope.scope_id, []).append(ImplementationAttempt(
                turn_number=turn_number, step_name=current_step_name, role=step.role,
                team=active_team_name, selector=selector,
                outcome="accepted" if done else "progress",
                manager_decision_number=(state.pending_boundary_decision.decision_number
                    if state.pending_boundary_decision is not None else None),
            ))
            next_config = (
                wf.steps.get(transition_target)
                if transition_target != "END" else None
            )
            state.active_implementation_scope = replace(
                scope,
                awaiting_review=(
                    next_config is not None and next_config.role != "worker"
                ),
            )

        post_transition_active_path = _select_next_active_plan_path(
            original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            new_plan_exists=new_plan_exists,
            selected_transition=selected_transition,
            exec_ctx=exec_ctx,
        )

        scope = state.active_implementation_scope
        next_config = (
            wf.steps.get(transition_target)
            if transition_target != "END" else None
        )
        if scope is not None and step.role != "worker" and scope.awaiting_review:
            scope = replace(scope, awaiting_review=False)
            state.active_implementation_scope = scope
        if (
            scope is not None
            and not new_plan_exists
            and _original_checkpoint_advanced(scope, post_snapshot)
            and (
                step.role != "worker"
                or next_config is None
                or next_config.role == "worker"
            )
        ):
            _close_implementation_scope(state)

        # Preserve the legacy MAX_TURNS transition when supervision is off.
        # With supervision enabled, exhaustion is itself the finalized
        # terminal boundary and must go directly to Full rather than through a
        # normal Lite transition gate.
        if workflow_config.manager.enabled and max_turns_reached and not done:
            reason = f"reached max turns limit of {effective_max_turns} without completing the active plan"
            state.status_message = "failed"
            report = _manager_terminal_incident(
                trigger="max_turns", reason=reason, current_step=current_step_name,
                current_role=step.role, active_team=active_team_name, active_selector=selector,
            )
            summary = report or _format_failure(
                reason=reason, run_dir=run_paths.run_dir, snapshot=post_snapshot,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                turns_completed=state.turns_completed, last_snapshot=post_snapshot,
                execution_context=exec_ctx, workflow_name=workflow_name,
                original_plan_path=original_plan_path, current_step_name=current_step_name,
                active_plan_path=active_plan_path, new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir)

        # A same-step cap is a terminal controller boundary, not a normal
        # transition followed by a second manager call.  Decide it before the
        # ordinary gate so Full is invoked exactly once when supervision is on.
        if len(wf.steps) > 1 and transition_target == current_step_name:
            max_cap = workflow_config.aflow.max_same_step_turns
            new_streak = (
                state.consec_step_count + 1
                if state.consec_step_name == current_step_name
                else 1
            )
            if max_cap > 0 and new_streak >= max_cap:
                reason = (
                    f"same-step cap reached: step '{current_step_name}' "
                    f"selected {new_streak} consecutive times (limit: {max_cap})"
                )
                state.status_message = "failed"
                report = _manager_terminal_incident(
                    trigger="same_step_cap", reason=reason,
                    current_step=current_step_name, current_role=step.role,
                    active_team=active_team_name, active_selector=selector,
                )
                _record_issue("same-step-cap", reason, turn_dir=turn_dir)
                summary = report or _format_failure(
                    reason=reason, run_dir=run_paths.run_dir, snapshot=post_snapshot,
                )
                write_run_metadata(
                    run_paths, config, state, status="failed", failure_reason=summary,
                    turns_completed=state.turns_completed, last_snapshot=post_snapshot,
                    execution_context=exec_ctx, workflow_name=workflow_name,
                    original_plan_path=original_plan_path, current_step_name=current_step_name,
                    active_plan_path=active_plan_path, new_plan_path=new_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                banner.stop(state)
                raise WorkflowError(summary, run_dir=run_paths.run_dir)

        # Detect scope pressure from the finalized turn before the manager gate.
        # Stop already won at this point (checked at line 5257).  When pressure
        # is present, _manager_gate forces Full or fails clearly for disabled
        # supervision; it must not reach the stop path with a simultaneous
        # real AFLOW_STOP marker.
        scope_pressure = parse_scope_pressure(
            (turn_dir / "stdout.txt").read_text(encoding="utf-8")
            if (turn_dir / "stdout.txt").is_file() else "",
            (turn_dir / "stderr.txt").read_text(encoding="utf-8")
            if (turn_dir / "stderr.txt").is_file() else "",
        )
        scope_pressure_reason = scope_pressure.reason if scope_pressure.detected else None

        transition_target = _manager_gate(
            proposed_transition=transition_target,
            current_step=current_step_name,
            current_role=step.role,
            active_team=active_team_name,
            active_selector=selector,
            post_transition_active_path=post_transition_active_path,
            scope_pressure_reason=scope_pressure_reason,
        )

        if transition_target != "END":
            state.current_team_override = None
        if selected_transition is None:
            raise WorkflowError("internal error: transition selection produced no result")
        try:
            active_plan_path = _select_next_active_plan_path(
                original_plan_path=original_plan_path,
                active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                new_plan_exists=new_plan_exists,
                selected_transition=selected_transition,
                exec_ctx=exec_ctx,
            )
        except WorkflowError as exc:
            state.status_message = "failed"
            summary = _format_failure(
                reason=exc.summary,
                run_dir=run_paths.run_dir,
                snapshot=state.last_snapshot,
            )
            write_run_metadata(
                run_paths, config, state, status="failed", failure_reason=summary,
                turns_completed=state.turns_completed,
                last_snapshot=state.last_snapshot,
                execution_context=exec_ctx,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            banner.stop(state)
            raise WorkflowError(summary, run_dir=run_paths.run_dir) from exc

        banner.set_context(
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path if new_plan_exists else None,
        )
        banner.update(state)

        write_run_metadata(
            run_paths, config, state, status="running",
            execution_context=exec_ctx,
            last_snapshot=state.last_snapshot,
            turns_completed=state.turns_completed,
            workflow_name=workflow_name, original_plan_path=original_plan_path,
            current_step_name=current_step_name, active_plan_path=active_plan_path,
            new_plan_path=new_plan_path,
            resumed_from_run_id=resumed_from_run_id,
        )

        if transition_target == "END":
            end_reason = _normalize_end_reason(
                selected_transition=selected_transition,
                done=done,
                max_turns_reached=max_turns_reached,
            )
            state.end_reason = end_reason
            recovered_turn = state.current_team_override is not None
            if recovered_turn:
                state.current_team_override = None
            merge_team_name = baseline_team_name if recovered_turn else active_team_name

            merge_status: str | None = None
            merge_failure_reason: str | None = None

            if exec_ctx is not None and "merge" in exec_ctx.teardown:
                merge_status, merge_failure_reason = _perform_merge_teardown(
                    exec_ctx,
                    wf,
                    workflow_config,
                    repo_root=config.repo_root,
                    team_name=merge_team_name,
                    adapter=adapter,
                    runner=runner,
                    config_dir=config_dir,
                    working_dir=working_dir,
                    original_plan_path=original_plan_path,
                    active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    banner=banner,
                    state=state,
                )

            if merge_status == "failed":
                state.status_message = "failed"
                report = _manager_terminal_incident(
                    trigger="merge_failure", reason=merge_failure_reason or "merge teardown failed",
                    current_step=current_step_name, current_role=step.role,
                    active_team=merge_team_name, active_selector=selector,
                )
                summary = report or _format_failure(
                    reason=merge_failure_reason or "merge teardown failed",
                    run_dir=run_paths.run_dir, snapshot=post_snapshot,
                )
                write_run_metadata(
                    run_paths, config, state, status="failed",
                    merge_status=merge_status,
                    merge_failure_reason=merge_failure_reason,
                    execution_context=exec_ctx,
                    last_snapshot=post_snapshot,
                    turns_completed=state.turns_completed,
                    workflow_name=workflow_name, original_plan_path=original_plan_path,
                    current_step_name=current_step_name, active_plan_path=active_plan_path,
                    new_plan_path=new_plan_path,
                    resumed_from_run_id=resumed_from_run_id,
                )
                prune_old_runs(run_paths.runs_root, config.keep_runs)
                banner.stop(state)
                raise WorkflowError(summary, run_dir=run_paths.run_dir)

            prior_original_plan_path = original_plan_path
            finalized_original_plan_path = _finalize_original_plan_if_complete(
                config.repo_root,
                original_plan_path,
                snapshot=post_snapshot,
            )
            if finalized_original_plan_path != prior_original_plan_path:
                original_plan_path = finalized_original_plan_path
                if active_plan_path == prior_original_plan_path:
                    active_plan_path = original_plan_path

            state.status_message = "completed"
            _emit_event(observer, StatusChangedEvent.create(
                status_message="completed",
                turns_completed=state.turns_completed,
                active_turn=None,
                current_step_name=current_step_name,
            ))
            result = ControllerRunResult(
                run_dir=run_paths.run_dir,
                turns_completed=state.turns_completed,
                final_snapshot=post_snapshot,
                issues_accumulated=state.issues_accumulated,
                end_reason=end_reason,
                recovery_summary=state.current_harness_recovery,
                recovery_history=tuple(state.harness_recovery_history),
            )
            write_run_metadata(
                run_paths, config, state, status="completed",
                merge_status=merge_status,
                execution_context=exec_ctx,
                last_snapshot=post_snapshot,
                turns_completed=state.turns_completed,
                end_reason=end_reason,
                workflow_name=workflow_name, original_plan_path=original_plan_path,
                current_step_name=current_step_name, active_plan_path=active_plan_path,
                new_plan_path=new_plan_path,
                resumed_from_run_id=resumed_from_run_id,
            )
            prune_old_runs(run_paths.runs_root, config.keep_runs)
            banner.stop(state)

            _emit_event(observer, RunCompletedEvent.create(
                run_dir=run_paths.run_dir,
                turns_completed=state.turns_completed,
                final_snapshot=post_snapshot,
                end_reason=end_reason,
                issues_accumulated=state.issues_accumulated,
                recovery_summary=state.current_harness_recovery,
                recovery_history=tuple(state.harness_recovery_history),
            ))

            return result

        if len(wf.steps) > 1:
            max_cap = workflow_config.aflow.max_same_step_turns
            if transition_target == current_step_name:
                new_streak = (
                    state.consec_step_count + 1
                    if state.consec_step_name == current_step_name
                    else 1
                )
                if max_cap > 0 and new_streak >= max_cap:
                    state.status_message = "failed"
                    _manager_terminal_incident(
                        trigger="same_step_cap",
                        reason=(f"same-step cap reached: step '{current_step_name}' "
                                f"selected {new_streak} consecutive times (limit: {max_cap})"),
                        current_step=current_step_name, current_role=step.role,
                        active_team=active_team_name, active_selector=selector,
                    )
                    _record_issue(
                        "same-step-cap",
                        (
                            f"same-step cap reached: step '{current_step_name}' "
                            f"selected {new_streak} consecutive times (limit: {max_cap})"
                        ),
                        turn_dir=turn_dir,
                    )
                    summary = _format_failure(
                        reason=(
                            f"same-step cap reached: step '{current_step_name}' "
                            f"selected {new_streak} consecutive times (limit: {max_cap})"
                        ),
                        run_dir=run_paths.run_dir,
                        snapshot=post_snapshot,
                    )
                    write_run_metadata(
                        run_paths, config, state, status="failed", failure_reason=summary,
                        turns_completed=state.turns_completed,
                        last_snapshot=post_snapshot,
                        execution_context=exec_ctx,
                        workflow_name=workflow_name, original_plan_path=original_plan_path,
                        current_step_name=current_step_name, active_plan_path=active_plan_path,
                        new_plan_path=new_plan_path,
                        resumed_from_run_id=resumed_from_run_id,
                    )
                    banner.stop(state)
                    raise WorkflowError(summary, run_dir=run_paths.run_dir)
                state.consec_step_name = current_step_name
                state.consec_step_count = new_streak
            else:
                state.consec_step_name = None
                state.consec_step_count = 0

        current_step_name = transition_target
        turn_number += 1

    state.status_message = "failed"
    effective_max_turns = state.effective_max_turns or config.max_turns
    report = _manager_terminal_incident(
        trigger="max_turns", reason=f"reached max turns limit of {effective_max_turns} without a transition to END",
        current_step=current_step_name, current_role=None, active_team=state.current_team,
        active_selector=None,
    )
    summary = report or _format_failure(
        reason=f"reached max turns limit of {effective_max_turns} without a transition to END",
        run_dir=run_paths.run_dir, snapshot=state.last_snapshot,
    )
    write_run_metadata(
        run_paths, config, state, status="failed", failure_reason=summary,
        last_snapshot=state.last_snapshot,
        turns_completed=state.turns_completed,
        execution_context=exec_ctx,
        workflow_name=workflow_name, original_plan_path=original_plan_path,
        current_step_name=current_step_name, active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
        resumed_from_run_id=resumed_from_run_id,
    )
    _emit_event(observer, RunFailedEvent.create(
        run_dir=run_paths.run_dir,
        turns_completed=state.turns_completed,
        failure_reason=summary,
        final_snapshot=state.last_snapshot,
        issues_accumulated=state.issues_accumulated,
        recovery_summary=state.current_harness_recovery,
        recovery_history=tuple(state.harness_recovery_history),
    ))
    prune_old_runs(run_paths.runs_root, config.keep_runs)
    banner.stop(state)
    raise WorkflowError(summary, run_dir=run_paths.run_dir)

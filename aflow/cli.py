from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any, Callable, Mapping

from .api import (
    AnalyzeRequest,
    ExecutionEvent,
    ExecutionObserver,
    PreparedRun,
    StartupQuestion,
    StartupQuestionKind,
    StartupRequest,
    analyze_runs,
    execute_workflow,
    prepare_startup,
    prepare_startup_with_answer,
)
from .config import (
    ConfigError,
    bootstrap_config,
    _bootstrap_config_files,
    find_placeholders,
    load_workflow_config,
    validate_workflow_config,
    WorkflowConfig,
    WorkflowStepConfig,
)
from .manager_context import scoped_reviewer_rejection_count
from .live_config import load_live_config, load_live_config_for_run
from .resume_relocation import ResumeRelocation, prepare_resume_relocation
from .plan import PlanParseError, PlanSnapshot, load_plan, parse_plan_text
from .skill_installer import InstallerError, install_skills
from .skill_installer import DEFAULT_BUNDLED_SKILL_NAMES
from .run_state import (
    ActiveImplementationScope,
    FrozenRunIdentity,
    PendingFinalizedTurn,
    PendingCumulativeReview,
    PendingRepartitionV1,
    RUN_STATE_SCHEMA_VERSION,
    ResumeContext,
    WorkflowEndReason,
    describe_end_reason,
    hotplug_resume_fields,
    manager_resume_fields,
    manager_resume_fields_strict,
    resolve_resume_override,
)


_MISSING_RESUME_FIELD = object()


class ResumeScopeReconciliationError(ValueError):
    """A durable progression claim was not safe to bind to a successor scope."""

    error_kind = "resume_scope_reconciliation"

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(
            f"error: run '{run_id}' cannot reconcile completed implementation "
            f"scope: {reason}."
        )


_PENDING_REPARTITION_STAGES = frozenset({
    "decided",
    "proposed",
    "mechanically_validated",
    "semantically_validated",
    "execution_plan_applied",
    "primary_plan_applied",
    "applied",
    "failed",
})
_PENDING_REPARTITION_PROPOSED_STAGES = frozenset({
    "proposed",
    "mechanically_validated",
    "semantically_validated",
    "execution_plan_applied",
    "primary_plan_applied",
    "applied",
})
_PENDING_REPARTITION_MECHANICAL_STAGES = frozenset({
    "mechanically_validated",
    "semantically_validated",
    "execution_plan_applied",
    "primary_plan_applied",
    "applied",
})
_PENDING_REPARTITION_SEMANTIC_STAGES = frozenset({
    "semantically_validated",
    "execution_plan_applied",
    "primary_plan_applied",
    "applied",
})
_PENDING_REPARTITION_BLOCKED_STAGES = frozenset({
    "decided",
    "proposed",
    "mechanically_validated",
})
_PENDING_REPARTITION_ARTIFACT_FIELDS = (
    ("proposal_artifact_path", "file"),
    ("candidate_artifact_path", "file"),
    ("mechanical_validation_artifact_path", "file"),
    ("semantic_verdict_artifact_path", "file"),
)
from .workflow import (
    WorkflowError,
    _latest_approved_checkpoint_index,
    _rebase_scope_envelope_evidence,
    _scope_envelope_reference,
    _validate_scope_envelope_bytes,
    evaluate_condition,
    load_scope_envelope_for_resume,
    load_scope_evidence_for_resume,
    move_completed_plan_to_done,
)
from .repartition import derive_generation_id, parse_envelope_bytes, slice_checkpoint_source
from .runlog import RunPaths, load_run_json, resolve_envelope_texts
from .analyzer import resolve_run_id
from .recovery_runtime import (
    RecoveryRuntimeValidationError,
    validate_recovery_runtime,
)
from .status import BannerRenderer, WorkflowGraphSource, build_workflow_show

RUN_HELP = """\
Flags:
  --plan/-p PLAN_FILE       Path to the plan Markdown file.
  --workflow/-w WORKFLOW    Name of the workflow to run (default from config).
  --config CONFIG_FILE      Current workflow configuration for launch/resume.
  --start-step/-ss STEP     Start from this step name or 1-based index (default: first).
  --team/-t TEAM_NAME       Override workflow team.
  --max-turns/-mt N         Maximum turns (default from config).
  --run-id RUN_ID           Canonical pre-reserved run identity (advanced use).
  --resume [RUN_ID]         Resume a saved run; plan and identity are optional when omitted.
  --continue-from-current   Start a nested run from the accepted current branch recorded in the plan.
  --resume-rehome-worktree PATH
                            Rehome an explicitly named resume onto a registered worktree.

Positional arguments:
  [workflow_name] [plan_file]   Either form works:
                                  - One positional: treated as plan_file
                                  - Two positionals: first is workflow (if it matches a config name),
                                    second is plan_file
                                  If only one token matches a workflow name, the other is the plan.

Extra instructions:
  Append -- followed by free-form text to pass extra instructions to each step prompt.
  This text is run-wide guidance. For one-turn recovery directions, write
  notes = ["..."] to the run-owned overrides.toml; pair it with
  next_step = "review_checkpoint" to target one review, or omit next_step for
  the next worker.

Examples:
  aflow run path/to/plan.md
  aflow run ralph path/to/plan.md
  aflow run --workflow ralph --plan path/to/plan.md
  aflow run --plan path/to/plan.md --start-step my_step
  aflow run --continue-from-current path/to/plan.md
  aflow run -mt 10 -ss 2 ralph plan.md
  aflow run plan.md -- keep edits small and update docs if behavior changes
"""


@dataclass(frozen=True)
class ResumeBootstrap:
    """Validated durable identity used to prepare one exact resume request."""

    resolved_run_id: Path
    run_dir: Path
    run_json: dict[str, object]
    plan_path: Path
    workflow_name: str
    team: str | None
    start_step: str | None
    max_turns: int
    extra_instructions: tuple[str, ...]
    resume_context: ResumeContext
    frozen_run_identity: FrozenRunIdentity
    config_path: Path
    workflow_config: Any
    team_explicit: bool
    max_turns_explicit: bool
    start_step_override: bool = False
    parsed_plan: object | None = None
    relocation: ResumeRelocation | None = None


INSTALL_SKILLS_HELP = """\
Auto mode: omit DESTINATION to refresh the default bundled skills in the canonical
store and link them into each supported harness skill directory for the harness
CLIs found on PATH.

Manual mode: provide DESTINATION to link the default bundled skills into that
root, one subdirectory per skill. Destinations may not overlap the canonical
skill store.

Selection flags:
  --include-optional    Include optional bundled skills in the installation.
  --only SKILL          Install only the named skill(s). Can be repeated. Cannot be combined
                        with --include-optional.

Installation links each skill as an absolute directory symlink to its saved copy
in ~/.config/aflow/skills/<name>, so editing a saved skill updates every linked
harness without reinstalling.

Supported auto targets:
  claude -> ~/.claude/skills
  kiro -> ~/.kiro/skills
  zcode -> ~/.zcode/skills
  codex -> ~/.agents/skills
  copilot -> ~/.agents/skills
  dsh -> ~/.agents/skills
  gemini -> ~/.agents/skills
  muse -> ~/.agents/skills
  opencode -> ~/.agents/skills
  pi -> ~/.agents/skills
  reasonix -> ~/.agents/skills
"""


def _is_valid_resume_candidate(
    prev_run: dict[str, object],
    current_workflow_config: Any,
    current_repo_root: Path,
    current_workflow_name: str,
    current_plan_path: Path,
    current_team: str | None,
    current_selected_start_step: str | None,
    current_max_turns: int | None,
    current_extra_instructions: tuple[str, ...],
) -> bool:
    return _resume_candidate_mismatch_reason(
        prev_run,
        current_workflow_config,
        current_repo_root,
        current_workflow_name,
        current_plan_path,
        current_team,
        current_selected_start_step,
        current_max_turns,
        current_extra_instructions,
    ) is None


def _is_terminal_integration_resume(
    prev_run: Mapping[str, object],
) -> bool:
    last_snapshot = prev_run.get("last_snapshot")
    lifecycle_teardown = prev_run.get("lifecycle_teardown")
    merge_failure_reason = prev_run.get("merge_failure_reason")
    return (
        prev_run.get("status") == "failed"
        and isinstance(last_snapshot, Mapping)
        and last_snapshot.get("is_complete") is True
        and prev_run.get("end_reason") == "transition_end"
        and prev_run.get("merge_status") == "failed"
        and isinstance(merge_failure_reason, str)
        and bool(merge_failure_reason.strip())
        and isinstance(lifecycle_teardown, list)
        and "merge" in lifecycle_teardown
    )


def _completion_resume_phase(prev_run: Mapping[str, object]) -> str | None:
    """Return the durable terminal-delivery phase that still needs work."""
    last_snapshot = prev_run.get("last_snapshot")
    phase = prev_run.get("completion_phase")
    if (
        prev_run.get("status") == "failed"
        and isinstance(last_snapshot, Mapping)
        and last_snapshot.get("is_complete") is True
        and prev_run.get("failure_kind") == "completion_publication"
        and phase in {"approved", "lifecycle"}
    ):
        return phase
    return None


def _is_terminal_completion_resume(prev_run: Mapping[str, object]) -> bool:
    return _completion_resume_phase(prev_run) is not None


def _resume_done_plan_path(repo_root: Path, plan_path: Path) -> Path | None:
    plans_root = (repo_root / "plans").resolve()
    try:
        relative = plan_path.resolve().relative_to(plans_root / "in-progress")
    except ValueError:
        return None
    return plans_root / "done" / relative


def _resume_plan_path(
    prev_run: Mapping[str, object],
    repo_root: Path,
) -> Path | None:
    """Return the saved current original plan path."""
    value = prev_run.get("original_plan_path")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = repo_root / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _resume_max_turns(prev_run: Mapping[str, object]) -> int | None:
    """Return the saved current invocation max-turns value."""
    value = prev_run.get("max_turns")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _resume_metadata_error(run_id: Path, field: str, detail: str) -> ValueError:
    return ValueError(f"error: run '{run_id.name}' has invalid {field}: {detail}.")


_CURRENT_MANAGER_RESUME_FIELDS = frozenset({
    "manager_decision_number",
    "manager_history",
    "semantic_stall_count",
    "reviewer_rejection_count",
    "implementation_attempts",
    "active_implementation_scope",
    "review_rejection_history",
    "pending_manager_notes",
    "pending_step_team_override",
    "pending_boundary_decision",
    "pending_repartition",
    "repartition_history",
    "scope_pressure_reason",
    "last_manager_report_path",
})


def _validate_current_resume_metadata(
    prev_run: Mapping[str, object],
    run_id: Path,
    *,
    reset_scope: bool = False,
) -> None:
    """Validate the complete schema-v2 controller snapshot before resume work."""
    required_fields = {
        "repo_root",
        "workflow_name",
        "original_plan_path",
        "plan_path",
        "team",
        "selected_start_step",
        "max_turns",
        "effective_max_turns",
        "extra_instructions",
        "lifecycle_setup",
        "lifecycle_teardown",
        *_CURRENT_MANAGER_RESUME_FIELDS,
        "hotplug_schema_version",
        "role_selectors",
        "current_hotplug_transaction",
        "pending_hotplug_transaction",
        "active_role_sessions",
        "hotplug_transaction_number",
        "hotplug_history",
    }
    missing_fields = sorted(field for field in required_fields if field not in prev_run)
    if missing_fields:
        raise _resume_metadata_error(
            run_id,
            "run.json",
            "missing required current fields: " + ", ".join(missing_fields),
        )

    for field in ("repo_root", "workflow_name", "original_plan_path", "plan_path"):
        value = prev_run.get(field)
        if not isinstance(value, str) or not value.strip():
            raise _resume_metadata_error(run_id, field, "expected a non-empty string")
    team = prev_run.get("team")
    if team is not None and (not isinstance(team, str) or not team.strip()):
        raise _resume_metadata_error(
            run_id, "team", "expected null or a non-empty string"
        )
    start_step = prev_run.get("selected_start_step")
    if start_step is not None and (
        not isinstance(start_step, str) or not start_step.strip()
    ):
        raise _resume_metadata_error(
            run_id,
            "selected_start_step",
            "expected null or a non-empty string",
        )
    for field in ("max_turns", "effective_max_turns"):
        value = prev_run.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise _resume_metadata_error(
                run_id, field, "expected a positive integer"
            )
    extra = prev_run.get("extra_instructions")
    if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
        raise _resume_metadata_error(run_id, "extra_instructions", "expected a list of strings")
    for field in ("lifecycle_setup", "lifecycle_teardown"):
        value = prev_run.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise _resume_metadata_error(run_id, field, "expected a list of strings")
    pending_override_target_step = prev_run.get("pending_override_target_step")
    if pending_override_target_step is not None and (
        not isinstance(pending_override_target_step, str)
        or not pending_override_target_step.strip()
    ):
        raise _resume_metadata_error(
            run_id,
            "pending_override_target_step",
            "expected a non-empty string or null",
        )

    for field in ("manager_history", "review_rejection_history", "repartition_history"):
        value = prev_run.get(field)
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise _resume_metadata_error(run_id, field, "expected a list of mappings")
    for field in (
        "manager_decision_number",
        "semantic_stall_count",
        "reviewer_rejection_count",
    ):
        value = prev_run.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _resume_metadata_error(run_id, field, "expected a non-negative integer")
    for field in (
        "pending_manager_notes",
        "pending_step_team_override",
        "pending_boundary_decision",
        "pending_repartition",
    ):
        value = prev_run.get(field)
        if value is not None and not isinstance(value, Mapping):
            raise _resume_metadata_error(run_id, field, "expected a mapping or null")
    for field in ("scope_pressure_reason", "last_manager_report_path"):
        value = prev_run.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _resume_metadata_error(
                run_id, field, "expected a non-empty string or null"
            )
    attempts = prev_run.get("implementation_attempts")
    if not isinstance(attempts, Mapping):
        raise _resume_metadata_error(run_id, "implementation_attempts", "expected a mapping")
    for scope_id, records in attempts.items():
        if not isinstance(scope_id, str) or not scope_id.strip() or not isinstance(records, list):
            raise _resume_metadata_error(
                run_id,
                "implementation_attempts",
                "expected non-empty scope ids mapped to lists",
            )
        for record in records:
            if not isinstance(record, Mapping) or not {
                "turn_number", "step_name", "role", "outcome"
            } <= set(record):
                raise _resume_metadata_error(
                    run_id,
                    "implementation_attempts",
                    "expected serialized ImplementationAttempt records",
                )

    active_scope = prev_run.get("active_implementation_scope")
    if active_scope is not None:
        if not isinstance(active_scope, Mapping):
            raise _resume_metadata_error(
                run_id, "active_implementation_scope", "expected a mapping or null"
            )
        required_scope_fields = {
            "scope_id",
            "original_plan_path",
            "opened_turn_number",
            "carried_reviewer_rejection_count",
            "envelope_artifact_path",
            "envelope_artifact_sha256",
            "envelope_canonical_sha256",
        }
        missing_scope_fields = sorted(
            field for field in required_scope_fields if field not in active_scope
        )
        if missing_scope_fields:
            detail = "missing required fields: " + ", ".join(missing_scope_fields)
            if any(field.startswith("envelope_") for field in missing_scope_fields):
                detail = "invalid scope envelope reference: " + detail
            raise _resume_metadata_error(
                run_id,
                "active_implementation_scope",
                detail,
            )
        for field in (
            "scope_id",
            "original_plan_path",
            "envelope_artifact_path",
            "envelope_artifact_sha256",
            "envelope_canonical_sha256",
        ):
            value = active_scope.get(field)
            if not isinstance(value, str) or not value.strip():
                detail = "expected a non-empty string"
                if field.startswith("envelope_"):
                    detail = "invalid scope envelope reference: " + detail
                raise _resume_metadata_error(
                    run_id,
                    f"active_implementation_scope.{field}",
                    detail,
                )
        for field in ("opened_turn_number", "carried_reviewer_rejection_count"):
            value = active_scope.get(field)
            minimum = 1 if field == "opened_turn_number" else 0
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise _resume_metadata_error(
                    run_id,
                    f"active_implementation_scope.{field}",
                    f"expected an integer >= {minimum}",
                )

    try:
        hotplug_resume_fields(prev_run)
    except (TypeError, ValueError, KeyError) as exc:
        raise _resume_metadata_error(run_id, "hotplug state", str(exc)) from exc
    try:
        manager_resume_fields_strict(
            prev_run,
            ignore_pending_repartition=reset_scope,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise _resume_metadata_error(run_id, "manager state", str(exc)) from exc


def _pending_repartition_error(
    run_id: str | Path,
    field: str,
    detail: str,
) -> ValueError:
    run_id_path = run_id if isinstance(run_id, Path) else Path(str(run_id))
    return _resume_metadata_error(
        run_id_path,
        f"pending_repartition.{field}",
        detail,
    )


def _validate_pending_repartition_resume_state(
    *,
    raw_pending_repartition: object,
    pending_repartition: PendingRepartitionV1 | None,
    run_dir: Path,
    run_id: str | Path,
    reset_scope: bool,
    active_scope: ActiveImplementationScope | None = None,
    manager_decision_number: int | None = None,
    workflow_steps: Mapping[str, object] | None = None,
    scope_envelope_bytes: bytes | None = None,
) -> tuple[PendingRepartitionV1 | None, dict[str, bytes]]:
    """Strictly validate and bind one pending repartition transaction.

    ``manager_resume_fields`` intentionally remains tolerant for status and
    analysis consumers.  Resume bootstrap is the mandatory boundary where a
    present transaction must either be complete enough to use or fail before
    startup and before the source run can be pruned.
    """
    if reset_scope:
        # The checkpoint-scoped transaction is deliberately opaque to reset.
        # In particular, do not inspect its shape or open any referenced path.
        return None, {}

    if raw_pending_repartition is _MISSING_RESUME_FIELD or raw_pending_repartition is None:
        return None, {}

    if not isinstance(raw_pending_repartition, Mapping):
        raise _pending_repartition_error(
            run_id,
            "pending_repartition",
            "expected a mapping or null",
        )
    allowed_fields = {
        "schema_version",
        "decision_number",
        "scope_id",
        "stage",
        "envelope_sha256",
        "source_plan_sha256",
        "attempt_count",
        "generation_id",
        "partition_ids",
        "child_summaries",
        "proposal_sha256",
        "candidate_plan_sha256",
        "current_disposition",
        "resolved_target_step",
        "resolved_target_role",
        "latest_attempt_path",
        "proposal_artifact_path",
        "candidate_artifact_path",
        "mechanical_validation_artifact_path",
        "semantic_verdict_artifact_path",
        "failed_stage",
        "failure_reason",
    }
    unknown_fields = sorted(
        str(field)
        for field in raw_pending_repartition
        if field not in allowed_fields
    )
    if unknown_fields:
        raise _pending_repartition_error(
            run_id,
            "pending_repartition",
            f"unexpected field(s): {', '.join(unknown_fields)}",
        )

    def required_int(field: str, *, minimum: int | None = None) -> int:
        value = raw_pending_repartition.get(field, _MISSING_RESUME_FIELD)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or (minimum is not None and value < minimum)
        ):
            expectation = "an integer"
            if minimum is not None:
                expectation += f" >= {minimum}"
            raise _pending_repartition_error(run_id, field, f"expected {expectation}")
        return value

    def optional_int(field: str, *, default: int) -> int:
        if field not in raw_pending_repartition:
            return default
        return required_int(field, minimum=0)

    def required_text(field: str) -> str:
        value = raw_pending_repartition.get(field, _MISSING_RESUME_FIELD)
        if not isinstance(value, str) or not value.strip():
            raise _pending_repartition_error(
                run_id,
                field,
                "expected a non-empty string",
            )
        return value

    def optional_text(field: str) -> str | None:
        if field not in raw_pending_repartition:
            return None
        value = raw_pending_repartition[field]
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise _pending_repartition_error(
                run_id,
                field,
                "expected null or a non-empty string",
            )
        return value

    def text_sequence(field: str) -> tuple[str, ...]:
        if field not in raw_pending_repartition:
            return ()
        value = raw_pending_repartition[field]
        if not isinstance(value, (list, tuple)):
            raise _pending_repartition_error(
                run_id,
                field,
                "expected a list of non-empty strings",
            )
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise _pending_repartition_error(
                run_id,
                field,
                "expected a list of non-empty strings",
            )
        return tuple(value)

    schema_version = required_int("schema_version")
    if schema_version != 1:
        raise _pending_repartition_error(
            run_id,
            "schema_version",
            "expected integer 1",
        )
    decision_number = required_int("decision_number", minimum=1)
    scope_id = required_text("scope_id")
    stage = required_text("stage")
    if stage not in _PENDING_REPARTITION_STAGES:
        raise _pending_repartition_error(
            run_id,
            "stage",
            f"unknown stage '{stage}'",
        )
    envelope_sha256 = required_text("envelope_sha256")
    source_plan_sha256 = required_text("source_plan_sha256")
    generation_id = required_text("generation_id")
    attempt_count = optional_int("attempt_count", default=0)
    partition_ids = text_sequence("partition_ids")
    child_summaries = text_sequence("child_summaries")
    proposal_sha256 = optional_text("proposal_sha256")
    candidate_plan_sha256 = optional_text("candidate_plan_sha256")
    current_disposition = optional_text("current_disposition")
    resolved_target_step = optional_text("resolved_target_step")
    resolved_target_role = optional_text("resolved_target_role")
    latest_attempt_path = optional_text("latest_attempt_path")
    failed_stage = optional_text("failed_stage")
    failure_reason = optional_text("failure_reason")
    artifact_values = {
        field: optional_text(field)
        for field, _kind in _PENDING_REPARTITION_ARTIFACT_FIELDS
    }

    if not isinstance(pending_repartition, PendingRepartitionV1):
        raise _pending_repartition_error(
            run_id,
            "pending_repartition",
            "present metadata did not decode as PendingRepartitionV1",
        )

    expected_decoded_values = {
        "schema_version": schema_version,
        "decision_number": decision_number,
        "scope_id": scope_id,
        "stage": stage,
        "envelope_sha256": envelope_sha256,
        "source_plan_sha256": source_plan_sha256,
        "attempt_count": attempt_count,
        "generation_id": generation_id,
        "partition_ids": partition_ids,
        "child_summaries": child_summaries,
        "proposal_sha256": proposal_sha256,
        "candidate_plan_sha256": candidate_plan_sha256,
        "current_disposition": current_disposition,
        "resolved_target_step": resolved_target_step,
        "resolved_target_role": resolved_target_role,
        "latest_attempt_path": latest_attempt_path,
        "proposal_artifact_path": artifact_values["proposal_artifact_path"],
        "candidate_artifact_path": artifact_values["candidate_artifact_path"],
        "mechanical_validation_artifact_path": artifact_values[
            "mechanical_validation_artifact_path"
        ],
        "semantic_verdict_artifact_path": artifact_values[
            "semantic_verdict_artifact_path"
        ],
        "failed_stage": failed_stage,
        "failure_reason": failure_reason,
    }
    for field, expected in expected_decoded_values.items():
        if getattr(pending_repartition, field) != expected:
            raise _pending_repartition_error(
                run_id,
                field,
                "tolerant decoder did not preserve the raw value",
            )

    if stage in _PENDING_REPARTITION_PROPOSED_STAGES:
        if attempt_count < 1:
            raise _pending_repartition_error(
                run_id,
                "attempt_count",
                "is required to be a positive integer from the proposed stage onward",
            )
        if not child_summaries:
            raise _pending_repartition_error(
                run_id,
                "child_summaries",
                "must be non-empty from the proposed stage onward",
            )
        for field in (
            "proposal_sha256",
            "current_disposition",
            "resolved_target_step",
            "resolved_target_role",
            "proposal_artifact_path",
            "latest_attempt_path",
        ):
            if getattr(pending_repartition, field) is None:
                raise _pending_repartition_error(
                    run_id,
                    field,
                    "is required from the proposed stage onward",
                )

    if stage in _PENDING_REPARTITION_MECHANICAL_STAGES:
        if not partition_ids:
            raise _pending_repartition_error(
                run_id,
                "partition_ids",
                "must be non-empty from the mechanically_validated stage onward",
            )
        if len(child_summaries) != len(partition_ids):
            raise _pending_repartition_error(
                run_id,
                "partition_ids",
                "must have the same cardinality as child_summaries",
            )
        for field in (
            "candidate_plan_sha256",
            "candidate_artifact_path",
            "mechanical_validation_artifact_path",
        ):
            if getattr(pending_repartition, field) is None:
                raise _pending_repartition_error(
                    run_id,
                    field,
                    "is required from the mechanically_validated stage onward",
                )

    if stage in _PENDING_REPARTITION_SEMANTIC_STAGES:
        if pending_repartition.semantic_verdict_artifact_path is None:
            raise _pending_repartition_error(
                run_id,
                "semantic_verdict_artifact_path",
                "is required from the semantically_validated stage onward",
            )

    if stage == "failed":
        if failed_stage is None:
            raise _pending_repartition_error(
                run_id,
                "failed_stage",
                "is required for a failed transaction",
            )
        if failure_reason is None:
            raise _pending_repartition_error(
                run_id,
                "failure_reason",
                "is required for a failed transaction",
            )
        if latest_attempt_path is not None and attempt_count < 1:
            raise _pending_repartition_error(
                run_id,
                "attempt_count",
                "must be positive when failed metadata carries an attempt path",
            )
    elif failed_stage is not None or failure_reason is not None:
        raise _pending_repartition_error(
            run_id,
            "failed_stage",
            "and failure_reason must be null outside the failed stage",
        )

    if not isinstance(active_scope, ActiveImplementationScope):
        raise _pending_repartition_error(
            run_id,
            "active_implementation_scope",
            "a modern active parent scope is required for a pending transaction",
        )
    try:
        scope_reference = _scope_envelope_reference(active_scope)
    except WorkflowError as exc:
        raise _pending_repartition_error(
            run_id,
            "active_implementation_scope",
            exc.summary,
        ) from exc
    if scope_reference is None:
        raise _pending_repartition_error(
            run_id,
            "active_implementation_scope",
            "a complete scope envelope reference is required for a pending transaction",
        )
    if scope_envelope_bytes is None:
        raise _pending_repartition_error(
            run_id,
            "active_implementation_scope",
            "the scope envelope must be validated before pending transaction use",
        )
    try:
        _validate_scope_envelope_bytes(active_scope, scope_envelope_bytes)
    except WorkflowError as exc:
        raise _pending_repartition_error(
            run_id,
            "active_implementation_scope",
            exc.summary,
        ) from exc

    if pending_repartition.scope_id != active_scope.scope_id:
        raise _pending_repartition_error(
            run_id,
            "scope_id",
            "does not match the restored active implementation scope",
        )

    def required_sha256(field: str, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise _pending_repartition_error(
                run_id,
                field,
                "expected a lowercase 64-character SHA-256 hex string",
            )
        return value

    envelope_sha256 = required_sha256("envelope_sha256", envelope_sha256)
    source_plan_sha256 = required_sha256("source_plan_sha256", source_plan_sha256)
    for field, value in (
        ("proposal_sha256", proposal_sha256),
        ("candidate_plan_sha256", candidate_plan_sha256),
    ):
        if value is not None:
            required_sha256(field, value)

    if envelope_sha256 != active_scope.envelope_canonical_sha256:
        raise _pending_repartition_error(
            run_id,
            "envelope_sha256",
            "does not match the restored active scope envelope identity",
        )
    if (
        not isinstance(manager_decision_number, int)
        or isinstance(manager_decision_number, bool)
        or manager_decision_number < 1
    ):
        raise _pending_repartition_error(
            run_id,
            "decision_number",
            "does not have a positive restored manager decision boundary",
        )
    if pending_repartition.decision_number != manager_decision_number:
        raise _pending_repartition_error(
            run_id,
            "decision_number",
            "does not match the restored manager decision boundary",
        )
    try:
        expected_generation_id = derive_generation_id(
            scope_id=pending_repartition.scope_id,
            decision_number=pending_repartition.decision_number,
            envelope_sha256=envelope_sha256,
            source_plan_sha256=source_plan_sha256,
        )
    except ValueError as exc:
        raise _pending_repartition_error(
            run_id,
            "generation_id",
            f"cannot derive producer identity: {exc}",
        ) from exc
    if pending_repartition.generation_id != expected_generation_id:
        raise _pending_repartition_error(
            run_id,
            "generation_id",
            "does not match the producer-derived repartition identity",
        )

    if stage in _PENDING_REPARTITION_PROPOSED_STAGES:
        steps = workflow_steps if isinstance(workflow_steps, Mapping) else {}
        target_step = steps.get(pending_repartition.resolved_target_step)
        if target_step is None:
            raise _pending_repartition_error(
                run_id,
                "resolved_target_step",
                "does not identify an executable step in the restored workflow",
            )
        target_role = getattr(target_step, "role", None)
        if target_role != pending_repartition.resolved_target_role:
            raise _pending_repartition_error(
                run_id,
                "resolved_target_role",
                "does not match the executable step role in the restored workflow",
            )

    run_root = run_dir.resolve()
    artifact_bytes: dict[str, bytes] = {}
    resolved_artifacts: dict[str, Path] = {}
    artifact_owners: dict[Path, str] = {}

    def resolve_path(field: str, raw_path: str, *, kind: str) -> tuple[Path, str]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise _pending_repartition_error(
                run_id,
                field,
                "expected a non-empty repository-relative POSIX path",
            )
        if (
            raw_path != raw_path.strip()
            or "\\" in raw_path
            or (len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha())
        ):
            raise _pending_repartition_error(
                run_id,
                field,
                "must be a canonical repository-relative POSIX path",
            )
        try:
            posix_candidate = PurePosixPath(raw_path)
            if (
                posix_candidate.is_absolute()
                or ".." in posix_candidate.parts
                or posix_candidate.as_posix() != raw_path
            ):
                raise ValueError("absolute or traversal path")
            candidate = Path(*posix_candidate.parts)
            resolved = (run_root / candidate).resolve()
            relative = resolved.relative_to(run_root).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise _pending_repartition_error(
                run_id,
                field,
                "must resolve beneath the source run directory",
            ) from exc
        if not relative or relative == ".":
            raise _pending_repartition_error(
                run_id,
                field,
                "must name a path below the source run directory",
            )
        if relative != raw_path:
            raise _pending_repartition_error(
                run_id,
                field,
                "must resolve to its own canonical path; symlink aliases are not allowed",
            )
        if kind == "directory":
            if not resolved.is_dir():
                raise _pending_repartition_error(
                    run_id,
                    field,
                    f"expected directory '{raw_path}'",
                )
        elif not resolved.is_file():
            raise _pending_repartition_error(
                run_id,
                field,
                f"expected file '{raw_path}'",
            )
        return resolved, relative

    def read_file(field: str, path: Path, relative: str) -> bytes:
        try:
            data = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise _pending_repartition_error(
                run_id,
                field,
                f"cannot read '{relative}': {exc}",
            ) from exc
        existing = artifact_bytes.get(relative, _MISSING_RESUME_FIELD)
        if existing is not _MISSING_RESUME_FIELD and existing != data:
            raise _pending_repartition_error(
                run_id,
                field,
                f"changed while binding artifact '{relative}'",
            )
        artifact_bytes[relative] = data
        return data

    attempt_root: Path | None = None
    if latest_attempt_path is not None:
        attempt_root, _attempt_relative = resolve_path(
            "latest_attempt_path",
            latest_attempt_path,
            kind="directory",
        )

    for field, _kind in _PENDING_REPARTITION_ARTIFACT_FIELDS:
        raw_path = getattr(pending_repartition, field)
        if raw_path is None:
            continue
        resolved, relative = resolve_path(field, raw_path, kind="file")
        previous_owner = artifact_owners.get(resolved)
        if previous_owner is not None and previous_owner != field:
            raise _pending_repartition_error(
                run_id,
                field,
                f"resolves to the same file as {previous_owner}",
            )
        artifact_owners[resolved] = field
        resolved_artifacts[field] = resolved
        read_file(field, resolved, relative)

    for field, _kind in _PENDING_REPARTITION_ARTIFACT_FIELDS:
        raw_path = getattr(pending_repartition, field)
        if raw_path is None:
            continue
        if raw_path not in artifact_bytes:
            raise _pending_repartition_error(
                run_id,
                field,
                "must be bound under its identical canonical path key",
            )

    if attempt_root is not None:
        try:
            attempt_entries = sorted(attempt_root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise _pending_repartition_error(
                run_id,
                "latest_attempt_path",
                f"cannot enumerate '{latest_attempt_path}': {exc}",
            ) from exc
        # Repartition attempts have a fixed, flat artifact layout.  Carry the
        # direct files for evidence retention; individual references below are
        # still authoritative and are read even when they are nested.
        for attempt_entry in attempt_entries:
            try:
                canonical_entry = attempt_entry.resolve()
                entry_raw_relative = attempt_entry.relative_to(run_root).as_posix()
                entry_relative = canonical_entry.relative_to(run_root).as_posix()
            except (OSError, RuntimeError, ValueError) as exc:
                raise _pending_repartition_error(
                    run_id,
                    "latest_attempt_path",
                    f"contains an unsafe artifact path '{attempt_entry.name}'",
                ) from exc
            if attempt_entry.is_symlink() or entry_relative != entry_raw_relative:
                raise _pending_repartition_error(
                    run_id,
                    "latest_attempt_path",
                    f"contains a non-canonical symlink alias '{entry_raw_relative}'",
                )
            if attempt_entry.is_dir():
                continue
            if not attempt_entry.is_file():
                raise _pending_repartition_error(
                    run_id,
                    "latest_attempt_path",
                    f"contains non-file artifact '{attempt_entry.name}'",
                )
            read_file("latest_attempt_path", canonical_entry, entry_relative)

    for path_field, digest_field in (
        ("proposal_artifact_path", "proposal_sha256"),
        ("candidate_artifact_path", "candidate_plan_sha256"),
    ):
        digest = getattr(pending_repartition, digest_field)
        artifact_path = resolved_artifacts.get(path_field)
        if digest is not None and artifact_path is None:
            raise _pending_repartition_error(
                run_id,
                digest_field,
                f"requires {path_field} to be present",
            )
        if digest is not None and artifact_path is not None:
            relative = artifact_path.relative_to(run_root).as_posix()
            observed = hashlib.sha256(artifact_bytes[relative]).hexdigest()
            if observed != digest:
                raise _pending_repartition_error(
                    run_id,
                    digest_field,
                    f"does not match {path_field} bytes (expected {digest}, observed {observed})",
                )

    if stage in _PENDING_REPARTITION_BLOCKED_STAGES:
        raise ValueError(
            f"error: run '{Path(str(run_id)).name}' has pending repartition "
            "proposal/validation transaction that must be reconciled before "
            f"a harness can start (stage={stage})."
        )
    if stage == "failed":
        raise ValueError(
            f"error: run '{Path(str(run_id)).name}' cannot resume a failed "
            "repartition transaction without explicit scope reset."
        )

    return pending_repartition, artifact_bytes


def _decode_frozen_run_identity(
    prev_run: Mapping[str, object],
    run_id: Path,
) -> FrozenRunIdentity:
    """Decode diagnostic identity while leaving configuration admission live.

    Schema-v2 run metadata remains required for the controller-owned resume
    envelope, but ``frozen_config`` itself is optional.  Older fields are
    retained when present for diagnostics and continuation facts; none of them
    are required to select or admit the current configuration.
    """
    schema_version = prev_run.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != RUN_STATE_SCHEMA_VERSION
    ):
        observed = "missing" if "schema_version" not in prev_run else repr(schema_version)
        raise ValueError(
            f"error: run '{run_id.name}' uses unsupported resume state schema; "
            f"expected integer {RUN_STATE_SCHEMA_VERSION} (schema={observed})."
        )

    frozen_value = prev_run.get("frozen_config")
    if not isinstance(frozen_value, Mapping):
        frozen_value = None

    def metadata_value(field_name: str) -> object:
        value = prev_run.get(field_name)
        if value is None and isinstance(frozen_value, Mapping):
            value = frozen_value.get(field_name)
        return value

    workflow_name = metadata_value("workflow_name")
    if not isinstance(workflow_name, str) or not workflow_name.strip():
        raise _resume_metadata_error(
            run_id,
            "workflow_name",
            "expected a non-empty string",
        )
    values: dict[str, object] = {"workflow_name": workflow_name}
    for field_name in (
        "config_path",
        "config_fingerprint",
        "live_config_path",
    ):
        value = metadata_value(field_name)
        values[field_name] = (
            value
            if isinstance(value, str) and value.strip()
            else None
        )
    for field_name in ("team_explicit", "max_turns_explicit"):
        value = metadata_value(field_name)
        values[field_name] = value if isinstance(value, bool) else None
    for field_name in (
        "continuation_from_branch",
        "continuation_from_head",
        "continuation_mode",
    ):
        value = metadata_value(field_name)
        values[field_name] = (
            value
            if isinstance(value, str) and value.strip()
            else None
        )

    return FrozenRunIdentity(**values)


def _validate_resume_run_id(resolved_run_id: Path) -> str:
    """Return one safe run-ID component, rejecting path-shaped identifiers."""
    run_id = str(resolved_run_id)
    if (
        not run_id
        or resolved_run_id.is_absolute()
        or run_id in {".", ".."}
        or resolved_run_id.name != run_id
    ):
        raise ValueError(f"error: invalid resume run id '{run_id}'.")
    return run_id


def _bootstrap_resume_invocation(
    *,
    repo_root: Path,
    config_path: Path | None = None,
    default_config_path: Path | None = None,
    config_path_is_explicit: bool = False,
    workflow_config: Any,
    requested_run_id: str | None,
    workflow_arg: str | None,
    plan_file_arg: str | None,
    team_arg: str | None,
    start_step_arg: str | None,
    max_turns_arg: int | None,
    extra_instructions_arg: tuple[str, ...],
    extra_instructions_provided: bool,
    reset_scope: bool = False,
    rehome_worktree: str | Path | None = None,
    live_loader: Callable[[Path], Any] | None = None,
) -> ResumeBootstrap:
    """Resolve one durable run and reconstruct omitted resume identity read-only."""
    resolved_run_id, _source = resolve_run_id(requested_run_id, repo_root)
    if resolved_run_id is None:
        raise ValueError(
            "error: no previous run could be resolved for resume from the current shell context. "
            "Pass --resume RUN_ID to select a specific run."
        )

    run_id = _validate_resume_run_id(resolved_run_id)
    runs_root = (repo_root / ".aflow" / "runs").resolve()
    run_dir = runs_root / run_id
    prev_run = load_run_json(run_dir)
    if not isinstance(prev_run, dict):
        raise ValueError(
            f"error: run '{resolved_run_id.name}' does not contain readable or valid run metadata."
        )

    frozen_run_identity = _decode_frozen_run_identity(prev_run, resolved_run_id)
    _validate_current_resume_metadata(
        prev_run,
        resolved_run_id,
        reset_scope=reset_scope,
    )
    relocation = None
    if rehome_worktree is not None:
        if not requested_run_id or reset_scope:
            raise ValueError("resume relocation requires explicit --resume RUN_ID without --resume-reset-scope")
        relocation = prepare_resume_relocation(
            prev_run, source_run_id=run_id, current_repo_root=repo_root,
            replacement_worktree=rehome_worktree,
        )
        prev_run = relocation.payload(prev_run)
    plan_path = _resume_plan_path(prev_run, repo_root)
    plan_field = "original_plan_path"
    if plan_path is None:
        raise _resume_metadata_error(
            resolved_run_id,
            plan_field,
            "expected a non-empty string",
        )
    completion_phase = _completion_resume_phase(prev_run)
    parsed_plan_for_startup = None
    plan_to_validate = plan_path
    if not plan_path.is_file() and completion_phase == "lifecycle":
        done_plan_path = _resume_done_plan_path(repo_root, plan_path)
        if done_plan_path is not None and done_plan_path.is_file():
            plan_to_validate = done_plan_path
    try:
        if not plan_to_validate.is_file():
            raise OSError("file does not exist")
        plan_to_validate.read_text(encoding="utf-8")
        if plan_to_validate != plan_path:
            parsed_plan_for_startup = load_plan(plan_to_validate)
    except (OSError, UnicodeError, PlanParseError) as exc:
        raise _resume_metadata_error(
            resolved_run_id,
            plan_field,
            f"saved plan '{plan_path}' is not readable ({exc})",
        ) from exc

    try:
        loaded_live = load_live_config_for_run(
            repo_root,
            run_id,
            config_path=(config_path if config_path_is_explicit else None),
            run_metadata=prev_run,
            default_config_path=(
                default_config_path
                or config_path
                or (repo_root / "aflow.toml")
            ),
            loader=live_loader or (lambda _path: workflow_config),
        )
    except ConfigError as exc:
        raise ValueError(
            f"error: run '{resolved_run_id.name}' current configuration is unusable: {exc}"
        ) from None
    workflow_config = loaded_live.workflow_config
    effective_config_path = loaded_live.source.config_path

    workflow_name = prev_run.get("workflow_name")
    if not isinstance(workflow_name, str) or not workflow_name.strip():
        raise _resume_metadata_error(
            resolved_run_id,
            "workflow_name",
            "expected a non-empty string",
        )
    if workflow_name not in workflow_config.workflows:
        raise ValueError(
            f"error: run '{resolved_run_id.name}' references unknown saved workflow "
            f"'{workflow_name}'."
        )
    workflow_spec = workflow_config.workflows[workflow_name]

    team_value = prev_run.get("team")
    if team_value is not None and (
        not isinstance(team_value, str) or not team_value.strip()
    ):
        raise _resume_metadata_error(
            resolved_run_id,
            "team",
            "expected null or a non-empty string",
        )
    saved_team = team_value if isinstance(team_value, str) else None
    saved_team_explicit = frozen_run_identity.team_explicit
    if saved_team_explicit is None:
        saved_team_explicit = saved_team is not None

    start_step_value = prev_run.get("selected_start_step")
    if start_step_value is not None and (
        not isinstance(start_step_value, str) or not start_step_value.strip()
    ):
        raise _resume_metadata_error(
            resolved_run_id,
            "selected_start_step",
            "expected null or a non-empty string",
        )
    saved_start_step = (
        start_step_value if isinstance(start_step_value, str) else None
    )
    saved_max_turns = _resume_max_turns(prev_run)
    if saved_max_turns is None:
        raise _resume_metadata_error(
            resolved_run_id,
            "effective_max_turns",
            "expected a positive integer",
        )
    saved_max_turns_explicit = frozen_run_identity.max_turns_explicit
    if saved_max_turns_explicit is None:
        saved_max_turns_explicit = True
    extra_value = prev_run.get("extra_instructions")
    if not isinstance(extra_value, list) or not all(
        isinstance(item, str) for item in extra_value
    ):
        raise _resume_metadata_error(
            resolved_run_id,
            "extra_instructions",
            "expected a list of strings",
        )
    saved_extra = tuple(extra_value)

    if workflow_arg is not None and workflow_arg != workflow_name:
        raise ValueError(
            f"error: resume workflow mismatch: requested '{workflow_arg}', "
            f"but run '{resolved_run_id.name}' saved '{workflow_name}'."
        )
    if plan_file_arg is not None:
        requested_plan = Path(plan_file_arg).expanduser()
        if not requested_plan.is_absolute():
            requested_plan = Path.cwd() / requested_plan
        if requested_plan.resolve() != plan_path:
            raise ValueError(
                f"error: resume plan mismatch: requested '{requested_plan.resolve()}', "
                f"but run '{resolved_run_id.name}' saved '{plan_path}'."
            )

    effective_saved_team = (
        saved_team
        if saved_team_explicit
        else workflow_spec.team
    )
    resume_team_override: str | None = None
    resumed_from_team: str | None = None
    team_override_requested = team_arg is not None
    if team_arg is not None:
        if requested_run_id is None:
            raise ValueError(
                "error: resume team override requires explicit --resume RUN_ID"
            )
        configured_teams = getattr(workflow_config, "teams", {})
        if not isinstance(configured_teams, Mapping) or team_arg not in configured_teams:
            raise ValueError(
                f"error: resume team mismatch: requested '{team_arg}' is an "
                "unknown team; the target team must be configured."
            )
        blocker = _resume_team_override_blocker(run_dir, prev_run)
        if blocker is not None:
            raise ValueError(
                f"error: resume team override blocked by {blocker}."
            )
        resumed_from_team = saved_team
        resume_team_override = team_arg
        effective_saved_team = team_arg
        saved_team_explicit = True

    effective_start_step = saved_start_step
    start_step_override = False
    if start_step_arg is not None:
        resolved_start_step, start_step_error = _resolve_numeric_start_step(
            start_step_arg,
            workflow_spec,
        )
        if start_step_error is not None:
            raise ValueError(start_step_error)
        effective_start_step = resolved_start_step
        # Explicit resume intent must survive even when the requested step is
        # equal to the saved starting step. The saved run may have failed
        # before a different, now-removed step, and equality alone cannot
        # distinguish that correction from an omitted argument.
        start_step_override = True
    elif saved_start_step is not None and saved_start_step not in workflow_spec.steps:
        raise _resume_metadata_error(
            resolved_run_id,
            "selected_start_step",
            f"'{saved_start_step}' is not a configured step of current workflow "
            f"'{workflow_name}'; pass --start-step with a current step",
        )

    effective_max_turns = saved_max_turns
    max_turns_override = False
    if max_turns_arg is not None:
        effective_max_turns = max_turns_arg
        max_turns_override = max_turns_arg != saved_max_turns
        saved_max_turns_explicit = True
    elif not saved_max_turns_explicit:
        current_default_max_turns = getattr(
            getattr(workflow_config, "aflow", None),
            "max_turns",
            None,
        )
        if (
            not isinstance(current_default_max_turns, int)
            or isinstance(current_default_max_turns, bool)
            or current_default_max_turns < 1
        ):
            raise ValueError(
                f"error: run '{resolved_run_id.name}' current configuration has "
                "an invalid [aflow].max_turns default."
            )
        effective_max_turns = current_default_max_turns

    effective_extra = extra_instructions_arg if extra_instructions_provided else saved_extra

    has_owner_stopped_pending_review = (
        _owner_stopped_review_step(
            run_dir,
            prev_run,
            workflow_steps=workflow_spec.steps,
        )
        is not None
    )
    mismatch_reason = _resume_candidate_mismatch_reason(
        prev_run,
        workflow_spec,
        repo_root,
        workflow_name,
        plan_path,
        effective_saved_team,
        effective_start_step,
        effective_max_turns,
        saved_extra,
        allow_team_override=team_override_requested,
        allow_start_step_override=start_step_override,
        allow_max_turns_override=max_turns_override,
        allow_extra_instructions_override=extra_instructions_provided,
        allow_owner_stopped=has_owner_stopped_pending_review,
        allow_owner_stopped_pending_review=has_owner_stopped_pending_review,
        team_explicit=saved_team_explicit,
        max_turns_explicit=saved_max_turns_explicit,
        run_dir=run_dir,
        relocation=relocation,
    )
    if mismatch_reason is not None:
        raise ValueError(
            f"error: run '{resolved_run_id.name}' is not resumable: "
            f"{mismatch_reason}."
        )

    resume_context = _reconstruct_resume_context(
        resolved_run_id=resolved_run_id,
        run_dir=run_dir,
        prev_run=prev_run,
        plan_path=plan_path,
        frozen_run_identity=frozen_run_identity,
        reset_scope=reset_scope,
        require_resume=True,
        workflow_steps=workflow_spec.steps,
        relocation=relocation,
        resumed_from_team=resumed_from_team,
        resume_team_override=resume_team_override,
        effective_start_step=effective_start_step,
        start_step_override=start_step_override,
        team_explicit=saved_team_explicit,
        max_turns_explicit=saved_max_turns_explicit,
        start_step_explicit=True,
        effective_max_turns=effective_max_turns,
    )
    assert resume_context is not None

    return ResumeBootstrap(
        resolved_run_id=resolved_run_id,
        run_dir=run_dir,
        run_json=prev_run,
        plan_path=plan_path,
        workflow_name=workflow_name,
        team=effective_saved_team,
        start_step=(
            resume_context.pending_cumulative_review.reviewer_step_name
            if resume_context.pending_cumulative_review is not None
            else effective_start_step
        ),
        max_turns=effective_max_turns,
        extra_instructions=effective_extra,
        resume_context=resume_context,
        frozen_run_identity=frozen_run_identity,
        config_path=effective_config_path,
        workflow_config=workflow_config,
        team_explicit=saved_team_explicit,
        max_turns_explicit=saved_max_turns_explicit,
        start_step_override=(
            start_step_override
            or resume_context.pending_cumulative_review is not None
        ),
        parsed_plan=parsed_plan_for_startup,
        relocation=relocation,
    )


def _resume_candidate_mismatch_reason(
    prev_run: dict[str, object],
    current_workflow_config: Any,
    current_repo_root: Path,
    current_workflow_name: str,
    current_plan_path: Path,
    current_team: str | None,
    current_selected_start_step: str | None,
    current_max_turns: int | None,
    current_extra_instructions: tuple[str, ...],
    *,
    allow_team_override: bool = False,
    allow_extra_instructions_override: bool = False,
    allow_start_step_override: bool = False,
    allow_max_turns_override: bool = False,
    allow_owner_stopped: bool = False,
    allow_owner_stopped_pending_review: bool = False,
    team_explicit: bool | None = None,
    max_turns_explicit: bool | None = None,
    run_dir: Path | None = None,
    relocation: ResumeRelocation | None = None,
) -> str | None:
    """Check if the previous run is a valid resume candidate for the current invocation.

    A valid candidate must:
    - Have lifecycle metadata compatible with the current workflow
    - Have branch/worktree identity required by that lifecycle mode
    - Have status of "failed" or "running" (not "completed")
    - Have last_snapshot.is_complete == false, unless terminal merge failed
    - Not have merge_status, unless it records a failed terminal merge
    - Have lifecycle_setup that matches the current workflow's effective setup tuple
    - Match on all resolved invocation fields
    """
    lifecycle_setup = prev_run.get("lifecycle_setup", [])
    lifecycle_teardown = prev_run.get("lifecycle_teardown", [])
    if not isinstance(lifecycle_setup, list) or not all(
        isinstance(item, str) for item in lifecycle_setup
    ):
        return "it has invalid lifecycle resume metadata (setup)"
    if not isinstance(lifecycle_teardown, list) or not all(
        isinstance(item, str) for item in lifecycle_teardown
    ):
        return "it has invalid lifecycle resume metadata (teardown)"

    feature_branch = prev_run.get("feature_branch")
    worktree_path = prev_run.get("worktree_path")
    main_branch = prev_run.get("main_branch")
    terminal_completion_only = _is_terminal_completion_resume(prev_run)
    if "branch" in lifecycle_setup or (terminal_completion_only and lifecycle_setup):
        if not isinstance(feature_branch, str) or not feature_branch:
            return "it has no recorded feature branch"
        if not isinstance(main_branch, str) or not main_branch:
            return "it has no recorded main branch"
    if "worktree" in lifecycle_setup and not terminal_completion_only:
        if not isinstance(worktree_path, str) or not worktree_path:
            return "it has no recorded worktree path"

    status = prev_run.get("status")
    allowed_statuses = ("failed", "running", "waiting_for_valid_override")
    if allow_owner_stopped:
        allowed_statuses = (*allowed_statuses, "owner_stopped")
    if status not in allowed_statuses:
        return (
            f"its status is '{status}', not 'failed', 'running', or "
            "'waiting_for_valid_override'"
            + (" or 'owner_stopped'" if allow_owner_stopped else "")
        )

    last_snapshot = prev_run.get("last_snapshot")
    terminal_integration_only = _is_terminal_integration_resume(prev_run)
    pending_cumulative_review = None
    if (
        isinstance(last_snapshot, Mapping)
        and last_snapshot.get("is_complete") is True
        and not terminal_integration_only
        and not terminal_completion_only
        and not allow_owner_stopped_pending_review
    ):
        if run_dir is not None:
            try:
                pending_cumulative_review = _resume_pending_cumulative_review_resume(
                    run_id=run_dir.name,
                    run_dir=run_dir,
                    prev_run=prev_run,
                    plan_path=current_plan_path,
                    current_repo_root=current_repo_root,
                    workflow_steps=_resume_workflow_steps(
                        current_workflow_config,
                        current_workflow_name,
                    ),
                    relocation=relocation,
                )
            except ResumeScopeReconciliationError as exc:
                return f"pending cumulative review evidence is invalid: {exc.reason}"
    if (
        isinstance(last_snapshot, dict)
        and last_snapshot.get("is_complete") is True
        and not terminal_integration_only
        and not terminal_completion_only
        and pending_cumulative_review is None
        and not allow_owner_stopped_pending_review
        and not _completed_manager_budget_boundary_pending(prev_run, current_repo_root)
    ):
        return "its last saved plan snapshot was already complete"

    if (
        "merge_status" in prev_run
        and not terminal_integration_only
        and not terminal_completion_only
    ):
        return "it already entered merge teardown"

    prev_repo_root = prev_run.get("repo_root")
    if not isinstance(prev_repo_root, str) or Path(prev_repo_root).resolve() != current_repo_root:
        return "it belongs to a different repo root"

    prev_workflow_name = prev_run.get("workflow_name")
    if not isinstance(prev_workflow_name, str) or prev_workflow_name != current_workflow_name:
        return "its workflow name does not match this invocation"

    prev_plan_path = _resume_plan_path(prev_run, current_repo_root)
    if prev_plan_path is None or prev_plan_path != current_plan_path.resolve():
        return "its plan path does not match this invocation"

    prev_team = prev_run.get("team")
    prev_team_explicit = (
        team_explicit
        if team_explicit is not None
        else isinstance(prev_team, str) and bool(prev_team.strip())
    )
    if prev_team_explicit and not allow_team_override and prev_team != current_team:
        return "its effective team does not match this invocation"

    prev_selected_start_step = prev_run.get("selected_start_step")
    if (
        prev_selected_start_step != current_selected_start_step
        and not allow_start_step_override
    ):
        return "its selected start step does not match this invocation"

    prev_max_turns = _resume_max_turns(prev_run)
    prev_max_turns_explicit = (
        max_turns_explicit if max_turns_explicit is not None else True
    )
    if (
        prev_max_turns is None
        or (
            prev_max_turns_explicit
            and prev_max_turns != current_max_turns
            and not allow_max_turns_override
        )
    ):
        return "its max-turns value does not match this invocation"

    prev_extra_instructions = prev_run.get("extra_instructions")
    if not isinstance(prev_extra_instructions, list) or (
        not allow_extra_instructions_override
        and tuple(prev_extra_instructions) != current_extra_instructions
    ):
        return "its extra instructions do not match this invocation"

    if terminal_integration_only or terminal_completion_only:
        # Teardown belongs to the saved execution context.  A live workflow
        # edit must not migrate or repeat lifecycle ownership during resume.
        if terminal_integration_only and not lifecycle_teardown:
            return "it has no recorded lifecycle teardown"

    return None


def _prompt_resume(
    prev_run_id: str,
    feature_branch: str | None,
    worktree_path: str | None,
) -> bool:
    """Prompt the user whether to resume from the previous run.

    Returns True if the user accepts resume, False otherwise.
    """
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not is_tty:
        return False

    try:
        if worktree_path is not None:
            location = f"on branch '{feature_branch}' in worktree '{worktree_path}'"
        elif feature_branch is not None:
            location = f"on branch '{feature_branch}' in the primary checkout"
        else:
            location = "in the primary checkout"
        response = input(
            f"Resume from previous run '{prev_run_id}' {location}? [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    return response in ("", "y", "yes")


def _owner_stopped_review_step(
    run_dir: Path,
    prev_run: Mapping[str, object],
    *,
    workflow_steps: Mapping[str, object] | None = None,
) -> str | None:
    """Recover the configured reviewer after a finalized worker boundary stop."""
    if (
        prev_run.get("status") != "owner_stopped"
        or prev_run.get("end_reason") != "owner_stopped"
        or prev_run.get("pending_boundary_decision") is not None
    ):
        return None
    current_step_name = prev_run.get("current_step_name")
    if not isinstance(current_step_name, str) or not current_step_name.strip():
        return None
    scope = prev_run.get("active_implementation_scope")
    if not isinstance(scope, Mapping) or scope.get("awaiting_review") is not True:
        return None
    if workflow_steps is not None:
        step = workflow_steps.get(current_step_name)
        if step is None or getattr(step, "role", None) != "reviewer":
            return None

    turns_completed = prev_run.get("turns_completed")
    active_turn = prev_run.get("active_turn")
    if (
        not isinstance(turns_completed, int)
        or isinstance(turns_completed, bool)
        or turns_completed < 1
        or active_turn != turns_completed
    ):
        return None
    result_path = run_dir / "turns" / f"turn-{turns_completed:03d}" / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(result, Mapping)
        or result.get("status") != "completed"
        or result.get("turn_number") != turns_completed
        or result.get("step_role") != "worker"
        or not isinstance(result.get("step_name"), str)
        or not result.get("step_name")
        or result.get("chosen_transition") != current_step_name
        or result.get("returncode") != 0
        or not isinstance(result.get("snapshot_after"), Mapping)
    ):
        return None
    return current_step_name


def _interrupted_resume_step(
    run_dir: Path,
    prev_run: Mapping[str, object],
    *,
    workflow_steps: Mapping[str, object] | None = None,
) -> str | None:
    """Return the workflow step whose durable turn never finalized."""
    status = prev_run.get("status")
    current_step_name = prev_run.get("current_step_name")
    if status == "owner_stopped":
        return _owner_stopped_review_step(
            run_dir,
            prev_run,
            workflow_steps=workflow_steps,
        )
    if status == "failed" and prev_run.get("failure_kind") == "environment_preflight":
        preflight = prev_run.get("environment_preflight")
        if not isinstance(preflight, Mapping):
            return None
        invocation_kind = preflight.get("invocation_kind")
        payload_step_name = preflight.get("step_name")
        if (
            invocation_kind == "workflow_turn"
            and isinstance(current_step_name, str)
            and current_step_name.strip()
            and (
                payload_step_name is None
                or payload_step_name == current_step_name
            )
        ):
            return current_step_name
        return None
    if status != "running":
        return None
    active_turn = prev_run.get("active_turn")
    if (
        not isinstance(active_turn, int)
        or isinstance(active_turn, bool)
        or active_turn < 1
        or not isinstance(current_step_name, str)
        or not current_step_name.strip()
    ):
        return None
    result_path = run_dir / "turns" / f"turn-{active_turn:03d}" / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, Mapping) or result.get("status") != "starting":
        return None
    result_step_name = result.get("step_name")
    if (
        isinstance(result_step_name, str)
        and result_step_name.strip()
        and result_step_name != current_step_name
    ):
        return None
    return current_step_name


def _manager_budget_prelaunch_failed(run_dir: Path, prev_run: Mapping[str, object]) -> bool:
    """Recognize one durable, unlaunched manager context-budget failure."""
    number = prev_run.get("manager_decision_number")
    turn = prev_run.get("active_turn")
    if (
        prev_run.get("status") != "failed"
        or not isinstance(number, int) or isinstance(number, bool) or number < 1
        or not isinstance(turn, int) or isinstance(turn, bool) or turn < 1
    ):
        return False
    try:
        result = json.loads((run_dir / "manager" / f"decision-{number:03d}" / "result.json").read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(result, Mapping)
        and result.get("decision_number") == number
        and result.get("finalized_turn_number") == turn
        and result.get("failure_stage") == "prelaunch"
        and result.get("status") == "invalid"
        and result.get("trigger") == "post_turn"
        and isinstance(result.get("error"), str)
        and result["error"].startswith("manager inline context exceeds the ")
    )


def _resume_plan_snapshot(value: object) -> PlanSnapshot | None:
    """Decode the bounded checkpoint metadata needed by resume routing."""
    if not isinstance(value, Mapping):
        return None
    checkpoint_name = value.get("current_checkpoint_name")
    checkpoint_index = value.get("current_checkpoint_index")
    snapshot_values = (
        value.get("unchecked_checkpoint_count"),
        value.get("current_checkpoint_unchecked_step_count"),
        value.get("total_checkpoint_count", 0),
    )
    if (
        checkpoint_name is not None
        and not isinstance(checkpoint_name, str)
    ) or (
        checkpoint_index is not None
        and (
            not isinstance(checkpoint_index, int)
            or isinstance(checkpoint_index, bool)
        )
    ) or not all(
        isinstance(item, int) and not isinstance(item, bool)
        for item in snapshot_values
    ) or not isinstance(value.get("is_complete"), bool):
        return None
    return PlanSnapshot(
        current_checkpoint_name=checkpoint_name,
        unchecked_checkpoint_count=snapshot_values[0],
        current_checkpoint_unchecked_step_count=snapshot_values[1],
        is_complete=value["is_complete"],
        total_checkpoint_count=snapshot_values[2],
        current_checkpoint_index=checkpoint_index,
    )


def _completed_manager_budget_boundary_pending(prev_run: Mapping[str, object], repo_root: Path) -> bool:
    run_dir_value = prev_run.get("run_dir")
    if not isinstance(run_dir_value, str):
        return False
    run_dir = Path(run_dir_value)
    if not run_dir.is_absolute():
        run_dir = repo_root / run_dir
    if run_dir.resolve().parent != (repo_root / ".aflow" / "runs").resolve():
        return False
    if not _manager_budget_prelaunch_failed(run_dir, prev_run):
        return False
    pending = _pending_finalized_resume_turn(run_dir, prev_run)
    return pending is not None and pending.snapshot_after.is_complete


def _pending_finalized_resume_turn(
    run_dir: Path,
    prev_run: Mapping[str, object],
) -> PendingFinalizedTurn | None:
    """Recover a completed harness turn whose manager boundary never ran."""
    preflight = prev_run.get("environment_preflight")
    blocked_manager_boundary = _manager_budget_prelaunch_failed(run_dir, prev_run) or (
        prev_run.get("status") == "failed"
        and prev_run.get("failure_kind") == "environment_preflight"
        and isinstance(preflight, Mapping)
        and preflight.get("invocation_kind") in {
            "manager",
            "manager_note_correction",
            "checkpoint_repartition",
        }
    )
    if prev_run.get("status") != "running" and not blocked_manager_boundary:
        return None
    active_turn = prev_run.get("active_turn")
    turns_completed = prev_run.get("turns_completed")
    if (
        not isinstance(active_turn, int)
        or isinstance(active_turn, bool)
        or active_turn < 1
        or not isinstance(turns_completed, int)
        or isinstance(turns_completed, bool)
        or (
            active_turn != turns_completed
            if blocked_manager_boundary
            else active_turn <= turns_completed
        )
    ):
        return None
    boundary = prev_run.get("pending_boundary_decision")
    if (
        isinstance(boundary, Mapping)
        and boundary.get("finalized_turn_number") == active_turn
    ):
        return None
    result_path = run_dir / "turns" / f"turn-{active_turn:03d}" / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(result, Mapping)
        or result.get("turn_number") != active_turn
        or not isinstance(result.get("returncode"), int)
        or isinstance(result.get("returncode"), bool)
        or result.get("snapshot_after") is None
    ):
        return None
    step_name = result.get("step_name")
    step_role = result.get("step_role")
    selector = result.get("selector")
    active_plan_path = result.get("active_plan_path")
    new_plan_path = result.get("new_plan_path")
    chosen_transition = result.get("chosen_transition")
    chosen_condition = result.get("chosen_transition_condition")
    conditions = result.get("conditions")
    snapshot = result.get("snapshot_after")
    if (
        not all(
            isinstance(value, str) and value
            for value in (
                step_name,
                step_role,
                selector,
                active_plan_path,
                new_plan_path,
                chosen_transition,
            )
        )
        or (chosen_condition is not None and not isinstance(chosen_condition, str))
        or not isinstance(conditions, Mapping)
        or not isinstance(snapshot, Mapping)
    ):
        return None
    condition_values = {
        key: conditions.get(key)
        for key in ("DONE", "NEW_PLAN_EXISTS", "MAX_TURNS_REACHED")
    }
    if not all(isinstance(value, bool) for value in condition_values.values()):
        return None
    snapshot_after = _resume_plan_snapshot(snapshot)
    if snapshot_after is None:
        return None
    return PendingFinalizedTurn(
        source_run_dir=run_dir,
        turn_number=active_turn,
        step_name=step_name,
        step_role=step_role,
        selector=selector,
        active_plan_path=Path(active_plan_path),
        new_plan_path=Path(new_plan_path),
        snapshot_after=snapshot_after,
        snapshot_before=_resume_plan_snapshot(result.get("snapshot_before")),
        conditions={key: bool(value) for key, value in condition_values.items()},
        chosen_transition=chosen_transition,
        chosen_transition_condition=chosen_condition,
    )


def _resume_team_override_blocker(
    run_dir: Path,
    prev_run: Mapping[str, object],
) -> str | None:
    """Return the first durable state field that makes team rebinding unsafe."""
    for field in (
        "pending_manager_notes",
        "pending_step_team_override",
        "pending_boundary_decision",
        "pending_repartition",
        "current_hotplug_transaction",
        "pending_hotplug_transaction",
    ):
        if prev_run.get(field) is not None:
            return field
    if _pending_finalized_resume_turn(run_dir, prev_run) is not None:
        return "pending_finalized_turn"
    pending_override_notes = prev_run.get("pending_override_notes")
    if isinstance(pending_override_notes, list) and pending_override_notes:
        return "pending_override_notes"
    persisted = prev_run.get("override_result")
    resolution = resolve_resume_override(
        run_dir,
        persisted if isinstance(persisted, Mapping) else None,
    )
    if resolution.override_result is not None:
        if (
            resolution.override_result.status != "accepted"
            or not resolution.override_result.applied
        ):
            return "override_result"
    if resolution.source_run_dir is not None:
        return "overrides.toml"
    return None


def _manager_resume_fields_for_scope(
    prev_run: Mapping[str, object],
    *,
    reset_scope: bool,
    run_dir: Path | None = None,
) -> dict[str, object]:
    """Restore manager history while optionally discarding one checkpoint scope."""
    decoder_payload = prev_run
    if reset_scope and "pending_repartition" in prev_run:
        # Reset scope must not interpret the checkpoint-scoped transaction.
        decoder_payload = dict(prev_run)
        decoder_payload.pop("pending_repartition", None)
    fields = manager_resume_fields(decoder_payload)
    if reset_scope:
        fields.update({
            "semantic_stall_count": 0,
            "reviewer_rejection_count": 0,
            "implementation_attempts": {},
            "active_implementation_scope": None,
            "pending_manager_notes": None,
            "pending_step_team_override": None,
            "pending_boundary_decision": None,
            "pending_repartition": None,
            "last_manager_report_path": None,
        })
        return fields
    scope = prev_run.get("active_implementation_scope")
    if run_dir is not None and isinstance(scope, Mapping):
        recomputed = scoped_reviewer_rejection_count(run_dir, scope)
        if recomputed is not None:
            fields["reviewer_rejection_count"] = recomputed
    return fields


def _resume_owned_plan_path(
    plan_path: Path,
    prev_run: Mapping[str, object],
    *,
    run_id: str,
) -> Path:
    """Resolve the plan copy owned by the saved execution lifecycle."""
    lifecycle_setup = prev_run.get("lifecycle_setup", ())
    if not isinstance(lifecycle_setup, list) or "worktree" not in lifecycle_setup:
        return plan_path

    repo_value = prev_run.get("repo_root")
    worktree_value = prev_run.get("worktree_path")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (repo_value, worktree_value)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "worktree lifecycle metadata is incomplete while locating the owned plan",
        )
    try:
        repo_root = Path(repo_value).resolve()
        worktree_root = Path(worktree_value).resolve()
        relative = plan_path.resolve().relative_to(repo_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            "saved original plan is outside the recorded repository/worktree roots",
        ) from exc
    return worktree_root / relative


def _resume_scope_envelope_texts(
    run_dir: Path,
    envelope_bytes: bytes,
    *,
    run_id: str,
    plan_path: Path,
) -> tuple[object, str]:
    """Resolve the already-validated immutable envelope without source writes."""
    try:
        source_root = run_dir.resolve(strict=True)
        paths = RunPaths(
            repo_root=source_root.parent.parent.parent,
            runs_root=source_root.parent,
            run_dir=source_root,
            turns_dir=source_root / "turns",
            manager_dir=source_root / "manager",
            run_json=source_root / "run.json",
        )
        envelope = _rebase_scope_envelope_evidence(
            paths,
            parse_envelope_bytes(envelope_bytes),
        )
        plan_text, _checkpoint_text = resolve_envelope_texts(paths, envelope)
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"immutable scope envelope evidence cannot be resolved for '{plan_path}': {exc}",
        ) from exc
    return envelope, plan_text


def _resume_path_matches(value: object, expected: Path) -> bool:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return False
    try:
        return Path(value).expanduser().resolve() == expected.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _resume_result_path(
    value: object,
    *,
    relocation: ResumeRelocation | None,
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value).expanduser()
        if relocation is not None:
            path = relocation.map_path(path, required=False)
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def _resume_normalized_plan_bytes(
    text: str,
    *,
    checkpoint_index: int,
) -> bytes:
    """Normalize only checklist markers in one completed checkpoint."""
    source_slice = slice_checkpoint_source(text, checkpoint_index=checkpoint_index)
    if source_slice is None:
        raise ValueError(
            f"checkpoint {checkpoint_index} is absent from the owned plan"
        )
    checkpoint = re.sub(
        rb"\[([ xX])\]",
        b"[ ]",
        source_slice.full_text.encode("utf-8"),
    )
    plan_bytes = text.encode("utf-8")
    return (
        plan_bytes[: source_slice.checkpoint_byte_start]
        + checkpoint
        + plan_bytes[source_slice.checkpoint_byte_end :]
    )


def _resume_normalized_checkpoint_bytes(
    text: str,
    *,
    checkpoint_index: int,
) -> bytes:
    """Return one checkpoint body with only its checklist markers normalized."""
    source_slice = slice_checkpoint_source(text, checkpoint_index=checkpoint_index)
    if source_slice is None:
        raise ValueError(
            f"checkpoint {checkpoint_index} is absent from the owned plan"
        )
    return re.sub(
        rb"\[([ xX])\]",
        b"[ ]",
        source_slice.full_text.encode("utf-8"),
    )


def _resume_workflow_steps(
    workflow_config: object,
    workflow_name: str,
) -> Mapping[str, object] | None:
    """Resolve executable workflow steps from either config shape used by callers."""
    direct_steps = getattr(workflow_config, "steps", None)
    if isinstance(direct_steps, Mapping):
        return direct_steps
    workflows = getattr(workflow_config, "workflows", None)
    workflow = workflows.get(workflow_name) if isinstance(workflows, Mapping) else None
    steps = getattr(workflow, "steps", None)
    return steps if isinstance(steps, Mapping) else None


def _resume_read_json_object(path: Path, *, label: str, run_id: str) -> Mapping[str, object]:
    """Read one controller-owned JSON object without following artifact links."""
    if path.is_symlink() or not path.is_file():
        raise ResumeScopeReconciliationError(
            run_id,
            f"{label} is missing or is not an owned regular artifact",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"{label} is missing or unreadable: {exc}",
        ) from exc
    if not isinstance(value, Mapping):
        raise ResumeScopeReconciliationError(
            run_id,
            f"{label} must be a JSON object",
        )
    return value


def _resume_workspace_state(
    run_dir: Path,
    prev_run: Mapping[str, object],
    *,
    run_id: str,
) -> Mapping[str, object]:
    """Load the latest immutable branch/HEAD boundary for a strict resume."""
    inline = prev_run.get("workspace_state")
    if inline is not None:
        if not isinstance(inline, Mapping):
            raise ResumeScopeReconciliationError(
                run_id,
                "source workspace_state is not a JSON object",
            )
        return inline

    manager_dir = run_dir / "manager"
    if manager_dir.is_symlink() or not manager_dir.is_dir():
        raise ResumeScopeReconciliationError(
            run_id,
            "source has no owned manager boundary for branch/HEAD evidence",
        )
    decision_number = prev_run.get("manager_decision_number")
    if (
        isinstance(decision_number, int)
        and not isinstance(decision_number, bool)
        and decision_number > 0
    ):
        boundary_path = manager_dir / f"decision-{decision_number:03d}" / "boundary.json"
        boundary = _resume_read_json_object(
            boundary_path,
            label="latest manager boundary",
            run_id=run_id,
        )
        boundary_value = boundary.get("boundary")
        if not isinstance(boundary_value, Mapping):
            raise ResumeScopeReconciliationError(
                run_id,
                "latest manager boundary has no structured boundary payload",
            )
        workspace = boundary_value.get("workspace_state")
        if not isinstance(workspace, Mapping):
            raise ResumeScopeReconciliationError(
                run_id,
                "latest manager boundary has no workspace_state evidence",
            )
        return workspace

    try:
        decisions = sorted(
            (
                path
                for path in manager_dir.glob("decision-*/boundary.json")
                if not path.is_symlink() and path.is_file()
            ),
            reverse=True,
        )
    except OSError as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"source manager boundaries cannot be enumerated: {exc}",
        ) from exc
    if not decisions:
        raise ResumeScopeReconciliationError(
            run_id,
            "source has no owned manager boundary for branch/HEAD evidence",
        )
    boundary = _resume_read_json_object(
        decisions[0],
        label="latest manager boundary",
        run_id=run_id,
    )
    boundary_value = boundary.get("boundary")
    workspace = boundary_value.get("workspace_state") if isinstance(boundary_value, Mapping) else None
    if not isinstance(workspace, Mapping):
        raise ResumeScopeReconciliationError(
            run_id,
            "latest manager boundary has no workspace_state evidence",
        )
    return workspace


def _resume_git_output(
    root: Path,
    args: tuple[str, ...],
    *,
    run_id: str,
    label: str,
) -> str:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"{label} Git identity could not be verified: {exc}",
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise ResumeScopeReconciliationError(
            run_id,
            f"{label} Git identity could not be verified: {detail}",
        )
    return result.stdout.strip()


def _resume_validate_workspace_identity(
    *,
    run_dir: Path,
    prev_run: Mapping[str, object],
    current_repo_root: Path,
    run_id: str,
) -> None:
    """Require the source's recorded clean branch and exact commit still exist."""
    workspace = _resume_workspace_state(run_dir, prev_run, run_id=run_id)
    expected_head = None
    for key in ("head", "execution_head", "worktree_head", "current_head"):
        value = workspace.get(key)
        if value is None:
            value = prev_run.get(key)
        if value is not None:
            expected_head = value
            break
    if not isinstance(expected_head, str) or re.fullmatch(
        r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", expected_head
    ) is None:
        raise ResumeScopeReconciliationError(
            run_id,
            "source workspace boundary has no full repository HEAD",
        )

    expected_branch = workspace.get("branch")
    if expected_branch is None:
        expected_branch = prev_run.get("feature_branch")
    if not isinstance(expected_branch, str) or not expected_branch.strip():
        raise ResumeScopeReconciliationError(
            run_id,
            "source workspace boundary has no branch",
        )
    feature_branch = prev_run.get("feature_branch")
    if isinstance(feature_branch, str) and feature_branch and expected_branch != feature_branch:
        raise ResumeScopeReconciliationError(
            run_id,
            "source workspace boundary branch differs from feature_branch",
        )
    dirty = workspace.get("dirty_worktree")
    if not isinstance(dirty, str) or dirty.strip():
        raise ResumeScopeReconciliationError(
            run_id,
            "source workspace boundary records a dirty worktree",
        )
    merge_state = workspace.get("merge_state")
    if merge_state is not None and merge_state != "managed":
        raise ResumeScopeReconciliationError(
            run_id,
            "source workspace boundary is no longer managed",
        )

    lifecycle_setup = prev_run.get("lifecycle_setup", ())
    execution_root = current_repo_root
    if isinstance(lifecycle_setup, list) and "worktree" in lifecycle_setup:
        worktree_value = prev_run.get("worktree_path")
        if not isinstance(worktree_value, str) or not worktree_value.strip():
            raise ResumeScopeReconciliationError(
                run_id,
                "worktree lifecycle metadata has no execution worktree",
            )
        execution_root = Path(worktree_value)
    if execution_root.is_symlink() or not execution_root.is_dir():
        raise ResumeScopeReconciliationError(
            run_id,
            "recorded execution worktree is missing or is a symlink",
        )
    try:
        resolved_execution_root = execution_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"recorded execution root cannot be resolved: {exc}",
        ) from exc

    actual_top_level = Path(
        _resume_git_output(
            resolved_execution_root,
            ("rev-parse", "--show-toplevel"),
            run_id=run_id,
            label="source execution worktree",
        )
    ).resolve()
    if actual_top_level != resolved_execution_root:
        raise ResumeScopeReconciliationError(
            run_id,
            "source execution worktree is not the recorded Git root",
        )
    actual_branch = _resume_git_output(
        resolved_execution_root,
        ("symbolic-ref", "--short", "HEAD"),
        run_id=run_id,
        label="source execution worktree",
    )
    if actual_branch != expected_branch:
        raise ResumeScopeReconciliationError(
            run_id,
            f"source branch changed from '{expected_branch}' to '{actual_branch}'",
        )
    actual_head = _resume_git_output(
        resolved_execution_root,
        ("rev-parse", "HEAD"),
        run_id=run_id,
        label="source execution worktree",
    )
    if actual_head.lower() != expected_head.lower():
        raise ResumeScopeReconciliationError(
            run_id,
            f"source HEAD changed from '{expected_head}' to '{actual_head}'",
        )
    status = _resume_git_output(
        resolved_execution_root,
        ("status", "--porcelain", "--untracked-files=all"),
        run_id=run_id,
        label="source execution worktree",
    )
    unexpected_status = tuple(
        line
        for line in status.splitlines()
        if not (
            line.startswith("?? ")
            and line[3:].strip() == ".aflow-config-pair.lock"
        )
    )
    if unexpected_status:
        raise ResumeScopeReconciliationError(
            run_id,
            "source execution worktree changed after the finalized worker",
        )


def _resume_validate_inactive_unit(
    run_dir: Path,
    prev_run: Mapping[str, object],
    *,
    run_id: str,
) -> None:
    """Bind active-session metadata to the source unit's nonce-bound exit receipt."""
    try:
        hotplug_resume_fields(prev_run)
    except (TypeError, ValueError, KeyError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"source owner/session state is invalid: {exc}",
        ) from exc
    units_dir = run_dir / "units"
    if units_dir.is_symlink() or not units_dir.is_dir():
        raise ResumeScopeReconciliationError(
            run_id,
            "source has no owned unit receipt directory",
        )
    start = _resume_read_json_object(
        units_dir / "start.json",
        label="source unit start receipt",
        run_id=run_id,
    )
    exit_receipt = _resume_read_json_object(
        units_dir / "exit.json",
        label="source unit exit receipt",
        run_id=run_id,
    )
    nonce = start.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise ResumeScopeReconciliationError(
            run_id,
            "source unit start receipt has no nonce",
        )
    if (
        start.get("schema") != 1
        or start.get("run_id") != run_id
        or start.get("unit") != f"aflow-run-{run_id}.service"
        or exit_receipt.get("schema") != 1
        or exit_receipt.get("nonce") != nonce
        or not isinstance(exit_receipt.get("returncode"), int)
        or isinstance(exit_receipt.get("returncode"), bool)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source unit exit receipt does not prove owned inactivity",
        )


def _resume_scan_turn_results(
    run_dir: Path,
    *,
    active_turn: int,
    run_id: str,
) -> Mapping[str, object]:
    """Scan bounded finalized turn receipts and reject any prior reviewer."""
    turns_dir = run_dir / "turns"
    if turns_dir.is_symlink() or not turns_dir.is_dir():
        raise ResumeScopeReconciliationError(
            run_id,
            "source has no owned turns directory",
        )
    try:
        children = list(turns_dir.iterdir())
    except OSError as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"source turns cannot be enumerated: {exc}",
        ) from exc
    for child in children:
        match = re.fullmatch(r"turn-(\d+)", child.name)
        if match is None:
            continue
        if child.is_symlink() or not child.is_dir():
            raise ResumeScopeReconciliationError(
                run_id,
                f"source turn artifact '{child.name}' is not an owned directory",
            )
        if int(match.group(1)) > active_turn:
            raise ResumeScopeReconciliationError(
                run_id,
                "source contains a turn artifact after its finalized active turn",
            )
    if active_turn > 256:
        raise ResumeScopeReconciliationError(
            run_id,
            "source finalized turn history exceeds the bounded resume scan",
        )
    current: Mapping[str, object] | None = None
    for turn_number in range(1, active_turn + 1):
        turn_dir = turns_dir / f"turn-{turn_number:03d}"
        if turn_dir.is_symlink() or not turn_dir.is_dir():
            raise ResumeScopeReconciliationError(
                run_id,
                f"source turn-{turn_number:03d} is missing or is not owned",
            )
        result = _resume_read_json_object(
            turn_dir / "result.json",
            label=f"source turn-{turn_number:03d} result",
            run_id=run_id,
        )
        role = result.get("step_role")
        status = result.get("status")
        if not isinstance(role, str) or not role.strip() or not isinstance(status, str):
            raise ResumeScopeReconciliationError(
                run_id,
                f"source turn-{turn_number:03d} result has incomplete finalized metadata",
            )
        if status in {"starting", "running"}:
            raise ResumeScopeReconciliationError(
                run_id,
                f"source turn-{turn_number:03d} result is not finalized",
            )
        if role == "reviewer":
            raise ResumeScopeReconciliationError(
                run_id,
                "source contains a reviewer result before the pending full review",
            )
        if result.get("review_rejection") is not None:
            raise ResumeScopeReconciliationError(
                run_id,
                "source contains a recorded reviewer rejection",
            )
        if turn_number == active_turn:
            current = result
    if current is None:
        raise ResumeScopeReconciliationError(
            run_id,
            "source has no finalized active worker result",
        )
    return current


def _resume_pending_cumulative_review_resume(
    *,
    run_id: str,
    run_dir: Path,
    prev_run: Mapping[str, object],
    plan_path: Path,
    current_repo_root: Path,
    workflow_steps: Mapping[str, object] | None,
    relocation: ResumeRelocation | None,
) -> PendingCumulativeReview | None:
    """Classify one complete, failed run whose worker-to-review edge is pending."""
    last_snapshot = _resume_plan_snapshot(prev_run.get("last_snapshot"))
    raw_scope = prev_run.get("active_implementation_scope")
    if last_snapshot is None or not last_snapshot.is_complete:
        return None
    if not isinstance(raw_scope, Mapping) or raw_scope.get("awaiting_review") is not True:
        return None

    if prev_run.get("status") != "failed":
        raise ResumeScopeReconciliationError(
            run_id,
            "source is active or does not have a terminal controller failure",
        )
    for field_name in (
        "end_reason",
        "completion_phase",
        "merge_status",
        "merge_failure_reason",
        "approved_commit",
        "publication_receipt",
        "completion_receipt",
    ):
        value = prev_run.get(field_name)
        if value not in (None, ""):
            raise ResumeScopeReconciliationError(
                run_id,
                f"source already has terminal review/publication metadata in {field_name}",
            )
    if (
        last_snapshot.current_checkpoint_index is not None
        or last_snapshot.current_checkpoint_name is not None
        or last_snapshot.unchecked_checkpoint_count != 0
        or last_snapshot.current_checkpoint_unchecked_step_count != 0
        or last_snapshot.total_checkpoint_count < 1
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source complete snapshot has inconsistent checkpoint counts",
        )

    try:
        manager_resume_fields_strict(prev_run)
        hotplug_fields = hotplug_resume_fields(prev_run)
        manager_fields = _manager_resume_fields_for_scope(
            prev_run,
            reset_scope=False,
            run_dir=run_dir,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"source manager authority is invalid: {exc}",
        ) from exc
    scope = manager_fields.get("active_implementation_scope")
    if not isinstance(scope, ActiveImplementationScope) or not scope.awaiting_review:
        raise ResumeScopeReconciliationError(
            run_id,
            "source awaiting-review scope is incomplete or not current authority",
        )
    if (
        not isinstance(scope.checkpoint_index, int)
        or isinstance(scope.checkpoint_index, bool)
        or scope.checkpoint_index < 1
        or not isinstance(scope.checkpoint_name, str)
        or not scope.checkpoint_name.strip()
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source awaiting-review scope has no concrete checkpoint identity",
        )
    if (
        scope.current_partition_generation_id is not None
        or scope.current_partition_candidate_sha256 is not None
        or scope.current_partition_id is not None
        or manager_fields.get("pending_repartition") is not None
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source checkpoint partition state is still pending",
        )
    if scope.carried_reviewer_rejection_count != 0 or manager_fields.get("reviewer_rejection_count") != 0:
        raise ResumeScopeReconciliationError(
            run_id,
            "source awaiting-review scope carries reviewer-rejection state",
        )
    if any(
        getattr(item, "scope_id", None) == scope.scope_id
        for item in manager_fields.get("review_rejection_history", ())
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source awaiting-review scope already has review history",
        )
    for label in ("pending_manager_notes", "pending_step_team_override"):
        pending = manager_fields.get(label)
        if pending is not None and not getattr(pending, "consumed", False):
            raise ResumeScopeReconciliationError(
                run_id,
                f"source {label} is not consumed",
            )
    boundary = manager_fields.get("pending_boundary_decision")
    if boundary is not None and not (
        getattr(boundary, "applied", False) and getattr(boundary, "consumed", False)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source manager boundary is not consumed",
        )
    if prev_run.get("current_hotplug_transaction") is not None or prev_run.get("pending_hotplug_transaction") is not None:
        raise ResumeScopeReconciliationError(
            run_id,
            "source provider hotplug state is unresolved",
        )
    if hotplug_fields.get("current_hotplug_transaction") is not None or hotplug_fields.get("pending_hotplug_transaction") is not None:
        raise ResumeScopeReconciliationError(
            run_id,
            "source provider hotplug state is unresolved",
        )

    active_turn = prev_run.get("active_turn")
    turns_completed = prev_run.get("turns_completed")
    if (
        not isinstance(active_turn, int)
        or isinstance(active_turn, bool)
        or active_turn < 1
        or not isinstance(turns_completed, int)
        or isinstance(turns_completed, bool)
        or turns_completed != active_turn
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source turn ownership is not one finalized worker turn",
        )
    _resume_validate_inactive_unit(run_dir, prev_run, run_id=run_id)
    _resume_validate_workspace_identity(
        run_dir=run_dir,
        prev_run=prev_run,
        current_repo_root=current_repo_root,
        run_id=run_id,
    )
    result = _resume_scan_turn_results(
        run_dir,
        active_turn=active_turn,
        run_id=run_id,
    )
    if (
        result.get("turn_number") != active_turn
        or result.get("step_role") != "worker"
        or result.get("status") != "completed"
        or type(result.get("returncode")) is not int
        or result.get("returncode") != 0
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source active worker receipt is not a successful finalized turn",
        )
    step_name = result.get("step_name")
    selector = result.get("selector")
    active_plan_value = result.get("active_plan_path")
    original_plan_value = result.get("original_plan_path")
    chosen_transition = result.get("chosen_transition")
    chosen_condition = result.get("chosen_transition_condition")
    new_plan_value = result.get("new_plan_path")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            step_name,
            selector,
            active_plan_value,
            original_plan_value,
            chosen_transition,
            chosen_condition,
            new_plan_value,
        )
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker receipt lacks stable step, plan, or transition identity",
        )
    if chosen_transition == "END":
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker receipt ended the workflow before full review",
        )
    conditions = result.get("conditions")
    if not isinstance(conditions, Mapping) or any(
        type(conditions.get(name)) is not bool
        for name in ("DONE", "NEW_PLAN_EXISTS", "MAX_TURNS_REACHED")
    ) or conditions.get("DONE") is not True or conditions.get("NEW_PLAN_EXISTS") is not False or conditions.get("MAX_TURNS_REACHED") is not False:
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker receipt has invalid completion conditions",
        )
    snapshot_before = _resume_plan_snapshot(result.get("snapshot_before"))
    snapshot_after = _resume_plan_snapshot(result.get("snapshot_after"))
    if snapshot_before is None or snapshot_after is None or not snapshot_after.is_complete:
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker receipt lacks strict before/after snapshots",
        )
    if snapshot_after != last_snapshot:
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker receipt does not match the complete run snapshot",
        )
    if (
        snapshot_before.is_complete
        or not isinstance(snapshot_before.current_checkpoint_index, int)
        or isinstance(snapshot_before.current_checkpoint_index, bool)
        or snapshot_before.current_checkpoint_index < 1
        or not isinstance(snapshot_before.current_checkpoint_name, str)
        or not snapshot_before.current_checkpoint_name.strip()
        or snapshot_before.current_checkpoint_unchecked_step_count < 1
        or snapshot_before.unchecked_checkpoint_count < 1
        or snapshot_before.total_checkpoint_count != last_snapshot.total_checkpoint_count
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker before-snapshot is not a concrete unfinished checkpoint",
        )

    if workflow_steps is None:
        raise ResumeScopeReconciliationError(
            run_id,
            "current workflow has no executable step graph",
        )
    worker_step = workflow_steps.get(step_name)
    if worker_step is None or getattr(worker_step, "role", None) != "worker":
        raise ResumeScopeReconciliationError(
            run_id,
            f"source worker step '{step_name}' is not configured as a worker",
        )
    transitions = getattr(worker_step, "go", ())
    selected = [
        transition
        for transition in transitions
        if getattr(transition, "to", None) == chosen_transition
        and getattr(transition, "when", None) == chosen_condition
    ]
    if len(selected) != 1:
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker transition is not uniquely present in the current workflow graph",
        )
    transition = selected[0]
    try:
        condition_matches = evaluate_condition(
            chosen_condition,
            done=conditions["DONE"],
            new_plan_exists=conditions["NEW_PLAN_EXISTS"],
            max_turns_reached=conditions["MAX_TURNS_REACHED"],
        )
    except (WorkflowError, TypeError, ValueError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"source worker transition condition is invalid: {exc}",
        ) from exc
    if not condition_matches or not getattr(transition, "preserve_active_plan", False):
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker transition does not preserve the configured full-review boundary",
        )
    reviewer_step_name = getattr(transition, "to", None)
    reviewer_step = workflow_steps.get(reviewer_step_name)
    if reviewer_step is None or getattr(reviewer_step, "role", None) != "reviewer":
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker transition does not target a configured reviewer",
        )
    if prev_run.get("current_step_name") not in {step_name, reviewer_step_name}:
        raise ResumeScopeReconciliationError(
            run_id,
            "source run metadata does not identify the worker-to-review boundary",
        )

    owned_plan_path = _resume_owned_plan_path(plan_path, prev_run, run_id=run_id)
    if not owned_plan_path.is_file() or owned_plan_path.is_symlink():
        raise ResumeScopeReconciliationError(
            run_id,
            f"current owned plan is missing: {owned_plan_path}",
        )
    for label, value in (
        ("source original plan", prev_run.get("original_plan_path")),
        ("source active plan", prev_run.get("active_plan_path")),
    ):
        if value is not None and not (
            _resume_path_matches(value, plan_path)
            or _resume_path_matches(value, owned_plan_path)
        ):
            raise ResumeScopeReconciliationError(
                run_id,
                f"{label} names a repair overlay instead of the original plan",
            )
    result_active_path = _resume_result_path(
        active_plan_value,
        relocation=relocation,
    )
    result_original_path = _resume_result_path(
        original_plan_value,
        relocation=relocation,
    )
    if result_active_path is None or result_original_path is None or not (
        _resume_path_matches(result_active_path, plan_path)
        or _resume_path_matches(result_active_path, owned_plan_path)
    ) or not (
        _resume_path_matches(result_original_path, plan_path)
        or _resume_path_matches(result_original_path, owned_plan_path)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker receipt names a repair overlay instead of the original plan",
        )

    try:
        scope_envelope_bytes = load_scope_envelope_for_resume(run_dir, scope)
        load_scope_evidence_for_resume(run_dir, scope, scope_envelope_bytes)
        envelope, captured_text = _resume_scope_envelope_texts(
            run_dir,
            scope_envelope_bytes,
            run_id=run_id,
            plan_path=plan_path,
        )
        captured_plan = parse_plan_text(captured_text, source_path=plan_path)
        current_plan = load_plan(owned_plan_path)
        current_text = owned_plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, PlanParseError, ValueError, WorkflowError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"source or current plan evidence cannot be parsed strictly: {exc}",
        ) from exc
    if current_plan.snapshot != last_snapshot or not current_plan.snapshot.is_complete:
        raise ResumeScopeReconciliationError(
            run_id,
            "current owned plan does not match the complete worker snapshot",
        )
    if (
        captured_plan.snapshot.is_complete
        or captured_plan.snapshot.current_checkpoint_index != scope.checkpoint_index
        or captured_plan.snapshot.current_checkpoint_name != scope.checkpoint_name
        or captured_plan.snapshot.total_checkpoint_count != last_snapshot.total_checkpoint_count
        or getattr(envelope, "checkpoint_index", None) != scope.checkpoint_index
        or getattr(envelope, "checkpoint_name", None) != scope.checkpoint_name
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "captured scope evidence does not identify the stale active checkpoint",
        )
    try:
        logical_plan_path = plan_path.resolve().relative_to(current_repo_root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"original plan cannot be bound to the current repository: {exc}",
        ) from exc
    if (
        not _resume_path_matches(scope.original_plan_path, plan_path)
        and not _resume_path_matches(scope.original_plan_path, owned_plan_path)
    ) or getattr(envelope, "original_plan_path", None) != logical_plan_path:
        raise ResumeScopeReconciliationError(
            run_id,
            "scope envelope and active scope name different original-plan identities",
        )
    if len(captured_plan.sections) != len(current_plan.sections) or len(captured_plan.sections) != last_snapshot.total_checkpoint_count:
        raise ResumeScopeReconciliationError(
            run_id,
            "current owned plan changed its checkpoint structure",
        )
    expected_index = scope.checkpoint_index
    for index, (captured_section, current_section) in enumerate(
        zip(captured_plan.sections, current_plan.sections),
        start=1,
    ):
        if (
            captured_section.name != current_section.name
            or captured_section.checked_step_count + captured_section.unchecked_step_count
            != current_section.checked_step_count + current_section.unchecked_step_count
        ):
            raise ResumeScopeReconciliationError(
                run_id,
                "current owned plan changed checkpoint names or step structure",
            )
        if index < expected_index and (
            captured_section.heading_checked != current_section.heading_checked
            or captured_section.checked_step_count != current_section.checked_step_count
            or captured_section.unchecked_step_count != current_section.unchecked_step_count
        ):
            raise ResumeScopeReconciliationError(
                run_id,
                "current owned plan changed completed checkpoint evidence before the stale scope",
            )
        if index > expected_index and (
            current_section.checked_step_count < captured_section.checked_step_count
            or current_section.unchecked_step_count > captured_section.unchecked_step_count
            or captured_section.heading_checked and not current_section.heading_checked
        ):
            raise ResumeScopeReconciliationError(
                run_id,
                "current owned plan regressed a later checkpoint",
            )
    if not 1 <= expected_index <= len(current_plan.sections):
        raise ResumeScopeReconciliationError(
            run_id,
            "stale active checkpoint is not present in the current plan",
        )
    stale_section = current_plan.sections[expected_index - 1]
    if stale_section.name != scope.checkpoint_name or not stale_section.heading_checked or stale_section.unchecked_step_count != 0:
        raise ResumeScopeReconciliationError(
            run_id,
            "current owned plan does not show the stale scope as completed",
        )
    try:
        if _resume_normalized_checkpoint_bytes(
            captured_text,
            checkpoint_index=expected_index,
        ) != _resume_normalized_checkpoint_bytes(
            current_text,
            checkpoint_index=expected_index,
        ):
            raise ValueError("completed scope bytes changed beyond checklist markers")
    except ValueError as exc:
        raise ResumeScopeReconciliationError(run_id, str(exc)) from exc
    before_index = snapshot_before.current_checkpoint_index
    if before_index not in {expected_index, expected_index + 1} or before_index > len(current_plan.sections):
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker evidence skips the stale scope or identifies no next checkpoint",
        )
    before_section = current_plan.sections[before_index - 1]
    if snapshot_before.current_checkpoint_name != before_section.name:
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker before-snapshot checkpoint name differs from the current plan",
        )
    if before_index == expected_index and snapshot_before != captured_plan.snapshot:
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker before-snapshot differs from captured active-scope evidence",
        )
    new_plan_path = _resume_result_path(new_plan_value, relocation=relocation)
    expected_plan_parents = {
        plan_path.parent.resolve(),
        owned_plan_path.parent.resolve(),
    }
    expected_new_plan_pattern = re.compile(
        re.escape(
            f"{plan_path.stem}-cp{before_index:02d}-v"
        )
        + r"\d+"
        + re.escape(plan_path.suffix or ".md")
        + r"$"
    )
    if (
        new_plan_path is None
        or new_plan_path.parent.resolve() not in expected_plan_parents
        or expected_new_plan_pattern.fullmatch(new_plan_path.name) is None
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source worker receipt names an unexpected follow-up plan",
        )

    attempts = manager_fields.get("implementation_attempts", {})
    matching_attempts = [
        attempt
        for attempt in attempts.get(scope.scope_id, ())
        if (
            attempt.turn_number == active_turn
            and attempt.step_name == step_name
            and attempt.role == "worker"
            and attempt.selector == selector
            and attempt.outcome == "accepted"
        )
    ] if isinstance(attempts, Mapping) else []
    if len(matching_attempts) != 1:
        raise ResumeScopeReconciliationError(
            run_id,
            "source manager authority has no matching successful worker attempt",
        )
    if _latest_approved_checkpoint_index(current_text) is not None:
        raise ResumeScopeReconciliationError(
            run_id,
            "current plan already records an accepted checkpoint review",
        )
    return PendingCumulativeReview(
        source_run_dir=run_dir,
        worker_turn_number=active_turn,
        worker_step_name=step_name,
        reviewer_step_name=reviewer_step_name,
        snapshot_before=snapshot_before,
        worker_artifact_path=(
            f"resumed-from/{run_id}/turns/turn-{active_turn:03d}/result.json"
        ),
    )


def _resume_scope_routing_blocker(
    manager_fields: dict[str, object],
    *,
    scope: ActiveImplementationScope,
    prev_run: Mapping[str, object],
    run_id: str,
) -> None:
    """Reject one-hop state that must be consumed before scope closure."""
    if scope.awaiting_review:
        raise ResumeScopeReconciliationError(
            run_id,
            "the active scope is awaiting checkpoint review",
        )
    if any(
        value is not None
        for value in (
            scope.current_partition_generation_id,
            scope.current_partition_candidate_sha256,
            scope.current_partition_id,
            manager_fields.get("pending_repartition"),
        )
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "checkpoint partition state is still pending",
        )
    if scope.carried_reviewer_rejection_count != 0:
        raise ResumeScopeReconciliationError(
            run_id,
            "the active scope carries reviewer-rejection history",
        )
    if manager_fields.get("reviewer_rejection_count") != 0:
        raise ResumeScopeReconciliationError(
            run_id,
            "reviewer-rejection state must be resolved before scope closure",
        )
    if any(
        getattr(record, "scope_id", None) == scope.scope_id
        for record in manager_fields.get("review_rejection_history", ())
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "the active scope has a recorded reviewer rejection",
        )

    for label in ("pending_manager_notes", "pending_step_team_override"):
        pending = manager_fields.get(label)
        if pending is not None and not getattr(pending, "consumed", False):
            raise ResumeScopeReconciliationError(
                run_id,
                f"{label} must be consumed before scope closure",
            )
    boundary = manager_fields.get("pending_boundary_decision")
    if boundary is not None and not (
        getattr(boundary, "applied", False)
        and getattr(boundary, "consumed", False)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "an unconsumed manager boundary decision must be replayed first",
        )
    if any(
        prev_run.get(field) is not None
        for field in ("current_hotplug_transaction", "pending_hotplug_transaction")
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "provider hotplug state is unresolved",
        )


def _reconcile_verified_resume_scope(
    *,
    run_id: str,
    run_dir: Path,
    prev_run: Mapping[str, object],
    plan_path: Path,
    relocation: ResumeRelocation | None,
    scope: ActiveImplementationScope | None,
    manager_fields: Mapping[str, object],
    scope_envelope_bytes: bytes | None,
    pending_finalized_turn: PendingFinalizedTurn | None,
) -> bool:
    """Recognize only a strict transport-failure checkpoint progression.

    The function is deliberately read-only.  A true result means the caller
    may construct a successor with the old scope closed; the normal workflow
    then opens the next scope and captures its envelope through its canonical
    helpers.
    """
    if scope is None or scope_envelope_bytes is None:
        return False
    source_snapshot = _resume_plan_snapshot(prev_run.get("last_snapshot"))
    if source_snapshot is None or source_snapshot.is_complete:
        return False
    if (
        scope.checkpoint_index is None
        or source_snapshot.current_checkpoint_index is None
        or source_snapshot.current_checkpoint_index <= scope.checkpoint_index
    ):
        return False
    if source_snapshot.current_checkpoint_index != scope.checkpoint_index + 1:
        raise ResumeScopeReconciliationError(
            run_id,
            "source snapshot skips more than one checkpoint after the active scope",
        )

    _resume_scope_routing_blocker(
        manager_fields,
        scope=scope,
        prev_run=prev_run,
        run_id=run_id,
    )
    if pending_finalized_turn is not None:
        raise ResumeScopeReconciliationError(
            run_id,
            "a finalized turn boundary must be resolved before scope progression",
        )
    if prev_run.get("status") != "failed":
        raise ResumeScopeReconciliationError(
            run_id,
            "the source run is still active or has no terminal transport failure",
        )

    active_turn = prev_run.get("active_turn")
    turns_completed = prev_run.get("turns_completed")
    if (
        not isinstance(active_turn, int)
        or isinstance(active_turn, bool)
        or active_turn < 1
        or not isinstance(turns_completed, int)
        or isinstance(turns_completed, bool)
        or turns_completed != active_turn - 1
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source turn ownership is not a single terminal worker attempt",
        )

    result_path = run_dir / "turns" / f"turn-{active_turn:03d}" / "result.json"
    if any(
        path.is_symlink()
        for path in (run_dir, result_path.parent.parent, result_path.parent, result_path)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt is not an owned regular artifact",
        )
    try:
        result_value = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"terminal worker receipt is missing or unreadable: {exc}",
        ) from exc
    if not isinstance(result_value, Mapping):
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt must be a JSON object",
        )
    result = result_value
    if (
        result.get("turn_number") != active_turn
        or result.get("step_role") != "worker"
        or result.get("status") != "harness-failed"
        or not isinstance(result.get("returncode"), int)
        or isinstance(result.get("returncode"), bool)
        or result.get("returncode") == 0
        or result.get("chosen_transition") is not None
        or result.get("chosen_transition_condition") is not None
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt does not prove a transport-only failure",
        )
    conditions = result.get("conditions")
    if not isinstance(conditions, Mapping) or any(
        not isinstance(conditions.get(name), bool)
        or conditions.get(name)
        for name in ("DONE", "NEW_PLAN_EXISTS", "MAX_TURNS_REACHED")
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt has missing or non-terminal conditions",
        )

    snapshot_before = _resume_plan_snapshot(result.get("snapshot_before"))
    snapshot_after = _resume_plan_snapshot(result.get("snapshot_after"))
    if snapshot_before is None or snapshot_after is None:
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt lacks strict before/after plan snapshots",
        )
    if snapshot_after != source_snapshot:
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt does not match the source run snapshot",
        )

    owned_plan_path = _resume_owned_plan_path(plan_path, prev_run, run_id=run_id)
    if not (
        _resume_path_matches(prev_run.get("active_plan_path"), plan_path)
        or _resume_path_matches(prev_run.get("active_plan_path"), owned_plan_path)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "source metadata names an active plan overlay instead of the original plan",
        )
    result_active_path = _resume_result_path(
        result.get("active_plan_path"), relocation=relocation
    )
    if result_active_path is None or not (
        _resume_path_matches(result_active_path, plan_path)
        or _resume_path_matches(result_active_path, owned_plan_path)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt names a different active plan",
        )
    if not owned_plan_path.is_file() or owned_plan_path.is_symlink():
        raise ResumeScopeReconciliationError(
            run_id,
            f"current owned plan is missing: {owned_plan_path}",
        )

    envelope, envelope_plan_text = _resume_scope_envelope_texts(
        run_dir,
        scope_envelope_bytes,
        run_id=run_id,
        plan_path=plan_path,
    )
    try:
        captured_plan = parse_plan_text(envelope_plan_text, source_path=plan_path)
        current_plan = load_plan(owned_plan_path)
        current_plan_text = owned_plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, PlanParseError, ValueError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"captured or current plan cannot be parsed strictly: {exc}",
        ) from exc
    if snapshot_before != captured_plan.snapshot:
        raise ResumeScopeReconciliationError(
            run_id,
            "terminal worker receipt before-snapshot differs from captured scope evidence",
        )
    if current_plan.snapshot != source_snapshot:
        raise ResumeScopeReconciliationError(
            run_id,
            "current owned plan does not match the terminal worker after-snapshot",
        )

    expected_index = scope.checkpoint_index
    if not isinstance(expected_index, int):
        raise ResumeScopeReconciliationError(
            run_id,
            "captured scope has no concrete checkpoint index",
        )
    try:
        captured_normalized = _resume_normalized_plan_bytes(
            envelope_plan_text,
            checkpoint_index=expected_index,
        )
        current_normalized = _resume_normalized_plan_bytes(
            current_plan_text,
            checkpoint_index=expected_index,
        )
    except ValueError as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"plan checkpoint bytes cannot be normalized strictly: {exc}",
        ) from exc
    if current_normalized != captured_normalized:
        raise ResumeScopeReconciliationError(
            run_id,
            "current owned plan changed bytes beyond the completed checkpoint checklist",
        )
    if (
        captured_plan.snapshot.current_checkpoint_index != expected_index
        or captured_plan.snapshot.current_checkpoint_name != scope.checkpoint_name
        or getattr(envelope, "checkpoint_index", None) != expected_index
        or getattr(envelope, "checkpoint_name", None) != scope.checkpoint_name
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "captured scope evidence does not identify the active checkpoint",
        )
    try:
        repo_root_value = prev_run["repo_root"]
        if not isinstance(repo_root_value, str) or not repo_root_value.strip():
            raise ValueError("repo_root is missing")
        logical_plan_path = plan_path.resolve().relative_to(
            Path(repo_root_value).resolve()
        ).as_posix()
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise ResumeScopeReconciliationError(
            run_id,
            f"saved plan cannot be bound to the recorded repository: {exc}",
        ) from exc
    if (
        not _resume_path_matches(scope.original_plan_path, plan_path)
        or getattr(envelope, "original_plan_path", None) != logical_plan_path
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "scope envelope and active scope name different original-plan identities",
        )

    if len(captured_plan.sections) != len(current_plan.sections):
        raise ResumeScopeReconciliationError(
            run_id,
            "current owned plan changed its checkpoint structure",
        )
    if len(captured_plan.sections) != captured_plan.snapshot.total_checkpoint_count:
        raise ResumeScopeReconciliationError(
            run_id,
            "captured plan checkpoint count is inconsistent with its snapshot",
        )
    for index, (captured_section, current_section) in enumerate(
        zip(captured_plan.sections, current_plan.sections),
        start=1,
    ):
        if (
            captured_section.name != current_section.name
            or captured_section.checked_step_count
            + captured_section.unchecked_step_count
            != current_section.checked_step_count
            + current_section.unchecked_step_count
        ):
            raise ResumeScopeReconciliationError(
                run_id,
                "current owned plan changed checkpoint names or step structure",
            )
        if index != expected_index and (
            captured_section.heading_checked != current_section.heading_checked
            or captured_section.unchecked_step_count
            != current_section.unchecked_step_count
            or captured_section.checked_step_count
            != current_section.checked_step_count
        ):
            raise ResumeScopeReconciliationError(
                run_id,
                "current owned plan changed checkpoint structure",
            )
    if not (
        1 <= expected_index < len(current_plan.sections)
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "the next checkpoint is not present in the current owned plan",
        )
    old_section = current_plan.sections[expected_index - 1]
    next_section = current_plan.sections[expected_index]
    if (
        not old_section.heading_checked
        or old_section.unchecked_step_count != 0
        or next_section.heading_checked
        or next_section.name != source_snapshot.current_checkpoint_name
        or next_section.unchecked_step_count
        != source_snapshot.current_checkpoint_unchecked_step_count
        or source_snapshot.current_checkpoint_index != expected_index + 1
    ):
        raise ResumeScopeReconciliationError(
            run_id,
            "current owned plan does not show exactly the next unchecked checkpoint",
        )

    # This mirrors the canonical workflow scope close: only one-hop scope and
    # routing state are cleared. Attempts, budgets, histories, and pressure
    # diagnostics remain available to the successor.
    manager_fields["active_implementation_scope"] = None
    manager_fields["pending_manager_notes"] = None
    manager_fields["pending_step_team_override"] = None
    manager_fields["pending_boundary_decision"] = None
    manager_fields["reviewer_rejection_count"] = 0
    return True


def _reconstruct_resume_context(
    *,
    resolved_run_id: Path,
    run_dir: Path,
    prev_run: Mapping[str, object],
    plan_path: Path,
    frozen_run_identity: FrozenRunIdentity | None,
    reset_scope: bool,
    require_resume: bool,
    workflow_steps: Mapping[str, object] | None = None,
    relocation: ResumeRelocation | None = None,
    resumed_from_team: str | None = None,
    resume_team_override: str | None = None,
    effective_start_step: str | None = None,
    start_step_override: bool = False,
    team_explicit: bool | None = None,
    max_turns_explicit: bool | None = None,
    start_step_explicit: bool | None = None,
    effective_max_turns: int | None = None,
) -> ResumeContext | None:
    """Decode all durable resume state from one already-loaded run payload."""
    run_id = resolved_run_id.name
    raw_recovery_runtime = prev_run.get("recovery_runtime")
    if raw_recovery_runtime is not None:
        try:
            validate_recovery_runtime(
                raw_recovery_runtime,
                expected_target_run_id=run_id,
                allow_pending=False,
            )
        except RecoveryRuntimeValidationError as exc:
            raise ValueError(
                f"error: run '{run_id}' has an unresolved recovery operation: {exc}"
            ) from exc
    raw_feature_branch = prev_run.get("feature_branch")
    raw_worktree_path = prev_run.get("worktree_path")
    raw_main_branch = prev_run.get("main_branch")
    lifecycle_setup = prev_run.get("lifecycle_setup", [])
    lifecycle_teardown = prev_run.get("lifecycle_teardown", [])
    completion_phase = _completion_resume_phase(prev_run)
    terminal_completion_only = completion_phase is not None
    raw_completion_end_reason = prev_run.get("end_reason")
    completion_end_reason = (
        raw_completion_end_reason
        if raw_completion_end_reason
        in {"already_complete", "done", "max_turns_reached", "transition_end", "owner_stopped"}
        else None
    )
    continuation_from_branch = prev_run.get("continuation_from_branch")
    continuation_from_head = prev_run.get("continuation_from_head")
    continuation_mode = prev_run.get("continuation_mode")
    for field_name, value in (
        ("continuation_from_branch", continuation_from_branch),
        ("continuation_from_head", continuation_from_head),
        ("continuation_mode", continuation_mode),
    ):
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            if require_resume:
                raise _resume_metadata_error(
                    resolved_run_id,
                    field_name,
                    "expected a non-empty string or null",
                )
            return None
    if (
        not isinstance(lifecycle_setup, list)
        or not all(isinstance(item, str) for item in lifecycle_setup)
        or not isinstance(lifecycle_teardown, list)
        or not all(isinstance(item, str) for item in lifecycle_teardown)
    ):
        if require_resume:
            raise ValueError(
                f"error: run '{run_id}' has invalid lifecycle resume metadata."
            )
        return None

    feature_branch = (
        raw_feature_branch
        if isinstance(raw_feature_branch, str) and raw_feature_branch
        else None
    )
    worktree_path = (
        raw_worktree_path
        if isinstance(raw_worktree_path, str) and raw_worktree_path
        else None
    )
    main_branch = (
        raw_main_branch
        if isinstance(raw_main_branch, str) and raw_main_branch
        else None
    )
    if ("branch" in lifecycle_setup or (terminal_completion_only and lifecycle_setup)) and (
        feature_branch is None or main_branch is None
    ):
        if require_resume:
            raise ValueError(
                f"error: run '{run_id}' is missing branch resume metadata."
            )
        return None
    if "worktree" in lifecycle_setup and worktree_path is None and not terminal_completion_only:
        if require_resume:
            raise ValueError(
                f"error: run '{run_id}' is missing worktree resume metadata."
            )
        return None

    active_plan_path = prev_run.get("active_plan_path")
    terminal_integration_only = _is_terminal_integration_resume(prev_run)

    pending_finalized_turn = (
        None
        if reset_scope or terminal_completion_only
        else _pending_finalized_resume_turn(run_dir, prev_run)
    )
    if relocation is not None and pending_finalized_turn is not None:
        pending_finalized_turn = replace(
            pending_finalized_turn,
            active_plan_path=relocation.map_path(pending_finalized_turn.active_plan_path),
            new_plan_path=relocation.map_path(pending_finalized_turn.new_plan_path),
        )
    manager_fields = _manager_resume_fields_for_scope(
        prev_run,
        reset_scope=reset_scope,
        run_dir=run_dir,
    )
    preflight = prev_run.get("environment_preflight")
    replay_blocked_repartition = (
        prev_run.get("failure_kind") == "environment_preflight"
        and isinstance(preflight, Mapping)
        and preflight.get("invocation_kind") == "checkpoint_repartition"
        and pending_finalized_turn is not None
    )
    if replay_blocked_repartition:
        # The repartition harness did not start, so replay its finalized manager
        # boundary instead of applying the incomplete transaction.
        manager_fields["pending_repartition"] = None
    scope_envelope_source_path: str | None = None
    scope_envelope_bytes: bytes | None = None
    scope_evidence_artifact_bytes: dict[str, bytes] = {}
    active_scope = manager_fields.get("active_implementation_scope")
    if not reset_scope and active_scope is not None:
        try:
            scope_envelope_bytes = load_scope_envelope_for_resume(
                run_dir,
                active_scope,
            )
        except WorkflowError as exc:
            raise ValueError(
                f"error: run '{run_id}' has invalid scope envelope reference: "
                f"{exc.summary}"
            ) from exc
        scope_envelope_source_path = str(
            run_dir / active_scope.envelope_artifact_path
        )
        try:
            scope_evidence_artifact_bytes = load_scope_evidence_for_resume(
                run_dir, active_scope, scope_envelope_bytes
            )
        except WorkflowError as exc:
            raise ValueError(
                f"error: run '{run_id}' has invalid scope evidence reference: "
                f"{exc.summary}"
            ) from exc
    pending_repartition, repartition_artifact_bytes = (
        _validate_pending_repartition_resume_state(
            raw_pending_repartition=(
                _MISSING_RESUME_FIELD
                if replay_blocked_repartition
                else prev_run.get("pending_repartition", _MISSING_RESUME_FIELD)
            ),
            pending_repartition=manager_fields.get("pending_repartition"),
            run_dir=run_dir,
            run_id=run_id,
            reset_scope=reset_scope,
            active_scope=(
                active_scope
                if isinstance(active_scope, ActiveImplementationScope)
                else None
            ),
            manager_decision_number=manager_fields.get("manager_decision_number"),
            workflow_steps=workflow_steps,
            scope_envelope_bytes=scope_envelope_bytes,
        )
    )
    manager_fields["pending_repartition"] = pending_repartition
    if pending_finalized_turn is not None:
        # Any prior boundary belongs to an earlier finalized turn. The
        # recovered turn must receive a fresh manager decision before routing.
        manager_fields["pending_manager_notes"] = None
        manager_fields["pending_step_team_override"] = None
        manager_fields["pending_boundary_decision"] = None

    has_owner_stopped_pending_review = (
        not reset_scope
        and _owner_stopped_review_step(
            run_dir, prev_run, workflow_steps=workflow_steps
        ) is not None
    )
    pending_cumulative_review: PendingCumulativeReview | None = None
    if (
        not reset_scope
        and not terminal_completion_only
        and not terminal_integration_only
        and not has_owner_stopped_pending_review
    ):
        repo_root_value = prev_run.get("repo_root")
        if isinstance(repo_root_value, str) and repo_root_value.strip():
            pending_cumulative_review = _resume_pending_cumulative_review_resume(
                run_id=run_id,
                run_dir=run_dir,
                prev_run=prev_run,
                plan_path=plan_path,
                current_repo_root=Path(repo_root_value).resolve(),
                workflow_steps=workflow_steps,
                relocation=relocation,
            )

    reconciled_scope = False
    if (
        not reset_scope
        and not terminal_completion_only
        and not terminal_integration_only
        and pending_cumulative_review is None
        and not has_owner_stopped_pending_review
    ):
        reconciled_scope = _reconcile_verified_resume_scope(
            run_id=run_id,
            run_dir=run_dir,
            prev_run=prev_run,
            plan_path=plan_path,
            relocation=relocation,
            scope=(
                active_scope
                if isinstance(active_scope, ActiveImplementationScope)
                else None
            ),
            manager_fields=manager_fields,
            scope_envelope_bytes=scope_envelope_bytes,
            pending_finalized_turn=pending_finalized_turn,
        )
        if reconciled_scope:
            # The predecessor remains authoritative and immutable.  The
            # successor will open and capture the exact next scope normally.
            scope_envelope_source_path = None
            scope_envelope_bytes = None
            scope_evidence_artifact_bytes = {}

    recovered_active_plan = (
        str(plan_path)
        if (
            reconciled_scope
            or terminal_integration_only
            or terminal_completion_only
        )
        else active_plan_path
    )
    if (
        pending_finalized_turn is not None
        and pending_finalized_turn.conditions["NEW_PLAN_EXISTS"]
    ):
        recovered_active_plan = str(pending_finalized_turn.new_plan_path)

    override_value = prev_run.get("override_result")
    accepted_override_value = prev_run.get("last_accepted_override")
    override_resolution = resolve_resume_override(
        run_dir,
        override_value if isinstance(override_value, Mapping) else None,
        persisted_accepted_result=(
            accepted_override_value
            if isinstance(accepted_override_value, Mapping)
            else None
        ),
    )
    pending_override_notes = prev_run.get("pending_override_notes")
    if not isinstance(pending_override_notes, list) or not all(
        isinstance(note, str) for note in pending_override_notes
    ):
        pending_override_notes = []
    pending_override_target_step = prev_run.get("pending_override_target_step")
    if pending_override_target_step is not None and (
        not isinstance(pending_override_target_step, str)
        or not pending_override_target_step.strip()
    ):
        pending_override_target_step = None
    resolved_max_turns = effective_max_turns
    if resolved_max_turns is None:
        resolved_max_turns = prev_run.get("effective_max_turns")
    if not isinstance(resolved_max_turns, int) or isinstance(resolved_max_turns, bool) or resolved_max_turns < 1:
        if require_resume:
            raise _resume_metadata_error(
                resolved_run_id,
                "effective_max_turns",
                "expected a positive integer",
            )
        return None
    try:
        hotplug_fields = hotplug_resume_fields(prev_run)
    except (TypeError, ValueError, KeyError) as exc:
        if require_resume:
            raise ValueError(f"error: run '{run_id}' has invalid hotplug state: {exc}") from exc
        return None
    if resume_team_override is not None:
        # A named baseline-team change governs every future role. Completed
        # hotplug history stays durable, but its applied selectors and native
        # sessions must not take precedence over the target team's selectors.
        hotplug_fields["role_selectors"] = {}
        hotplug_fields["active_role_sessions"] = ()

    return ResumeContext(
        resumed_from_run_id=run_id,
        feature_branch=feature_branch,
        worktree_path=Path(worktree_path) if worktree_path is not None else None,
        main_branch=main_branch,
        setup=tuple(lifecycle_setup),
        teardown=tuple(lifecycle_teardown),
        continuation_from_branch=(
            continuation_from_branch
            if isinstance(continuation_from_branch, str)
            else None
        ),
        continuation_from_head=(
            continuation_from_head
            if isinstance(continuation_from_head, str)
            else None
        ),
        continuation_mode=(
            continuation_mode
            if isinstance(continuation_mode, str)
            else None
        ),
        active_plan_path=(
            None
            if reset_scope
            else Path(recovered_active_plan)
            if isinstance(recovered_active_plan, str)
            else None
        ),
        interrupted_step_name=(
            None
            if reset_scope
            else pending_cumulative_review.reviewer_step_name
            if pending_cumulative_review is not None
            else effective_start_step
            if start_step_override
            else (
                str(prev_run["current_step_name"])
                if (
                    (terminal_integration_only or terminal_completion_only)
                    and isinstance(prev_run.get("current_step_name"), str)
                )
                else _interrupted_resume_step(
                    run_dir,
                    prev_run,
                    workflow_steps=workflow_steps,
                )
            )
        ),
        pending_finalized_turn=pending_finalized_turn,
        pending_cumulative_review=pending_cumulative_review,
        frozen_run_identity=frozen_run_identity,
        live_config_path=(
            frozen_run_identity.live_config_path
            if frozen_run_identity is not None
            else (
                str(prev_run["live_config_path"])
                if isinstance(prev_run.get("live_config_path"), str)
                else None
            )
        ),
        team_explicit=(
            team_explicit
            if team_explicit is not None
            else (
                frozen_run_identity.team_explicit
                if frozen_run_identity is not None
                and frozen_run_identity.team_explicit is not None
                else (
                    prev_run.get("team") is not None
                    and bool(str(prev_run.get("team")).strip())
                )
            )
        ),
        max_turns_explicit=(
            max_turns_explicit
            if max_turns_explicit is not None
            else (
                frozen_run_identity.max_turns_explicit
                if frozen_run_identity is not None
                and frozen_run_identity.max_turns_explicit is not None
                else True
            )
        ),
        start_step_explicit=(
            start_step_explicit
            if start_step_explicit is not None
            else True
        ),
        override_result=override_resolution.override_result,
        last_accepted_override=override_resolution.last_accepted_override,
        effective_max_turns=resolved_max_turns,
        pending_override_notes=tuple(pending_override_notes),
        pending_override_target_step=pending_override_target_step,
        override_source_run_dir=override_resolution.source_run_dir,
        override_file_present=override_resolution.file_present,
        terminal_integration_only=terminal_integration_only,
        terminal_completion_only=terminal_completion_only,
        completion_phase=completion_phase,
        completion_end_reason=completion_end_reason,
        scope_envelope_bytes=scope_envelope_bytes,
        scope_envelope_source_path=scope_envelope_source_path,
        scope_evidence_artifact_bytes=scope_evidence_artifact_bytes,
        resume_scope_reconciled=reconciled_scope,
        repartition_artifact_bytes=repartition_artifact_bytes,
        resume_relocation=relocation.provenance() if relocation is not None else None,
        resumed_from_team=resumed_from_team,
        resume_team_override=resume_team_override,
        **hotplug_fields,
        **manager_fields,
    )


def _detect_resume_candidate(
    repo_root: Path,
    workflow_config: Any,
    workflow_name: str,
    plan_path: Path,
    team: str | None,
    selected_start_step: str | None,
    max_turns: int | None,
    extra_instructions: tuple[str, ...],
    requested_run_id: str | None = None,
    require_resume: bool = False,
    reset_scope: bool = False,
    resume_bootstrap: ResumeBootstrap | None = None,
    allow_start_step_override: bool = False,
    allow_max_turns_override: bool = False,
    team_explicit: bool | None = None,
    max_turns_explicit: bool | None = None,
) -> ResumeContext | None:
    """Detect if there's a valid resume candidate and prompt the user.

    Returns ResumeContext if the user accepts resume, None otherwise.
    """
    if resume_bootstrap is not None:
        resolved_run_id = resume_bootstrap.resolved_run_id
        run_dir = resume_bootstrap.run_dir
        prev_run = resume_bootstrap.run_json
        frozen_run_identity = resume_bootstrap.frozen_run_identity
    else:
        resolved_run_id, _source = resolve_run_id(requested_run_id, repo_root)
        if resolved_run_id is None:
            if require_resume:
                raise ValueError(
                    "error: no previous run could be resolved for resume from the current shell context. "
                    "Pass --resume RUN_ID to select a specific run."
                )
            return None

        runs_root = repo_root / ".aflow" / "runs"
        run_dir = runs_root / resolved_run_id.name
        prev_run = load_run_json(run_dir)
        if not isinstance(prev_run, dict):
            if require_resume:
                raise ValueError(
                    f"error: run '{resolved_run_id.name}' does not contain readable or valid run metadata."
                )
            return None
        try:
            frozen_run_identity = _decode_frozen_run_identity(
                prev_run,
                resolved_run_id,
            )
            _validate_current_resume_metadata(
                prev_run,
                resolved_run_id,
                reset_scope=reset_scope,
            )
        except ValueError:
            if require_resume:
                raise
            return None

    has_owner_stopped_pending_review = (
        _owner_stopped_review_step(
            run_dir,
            prev_run,
            workflow_steps=getattr(workflow_config, "steps", None),
        )
        is not None
    )
    reason = _resume_candidate_mismatch_reason(
        prev_run,
        workflow_config,
        repo_root,
        workflow_name,
        plan_path,
        team,
        selected_start_step,
        max_turns,
        extra_instructions,
        allow_team_override=(
            resume_bootstrap is not None
            and resume_bootstrap.resume_context.resume_team_override is not None
        ),
        allow_extra_instructions_override=resume_bootstrap is not None,
        allow_start_step_override=allow_start_step_override,
        allow_max_turns_override=allow_max_turns_override,
        allow_owner_stopped=has_owner_stopped_pending_review,
        allow_owner_stopped_pending_review=has_owner_stopped_pending_review,
        team_explicit=team_explicit,
        max_turns_explicit=max_turns_explicit,
        run_dir=run_dir,
        relocation=(
            resume_bootstrap.relocation
            if resume_bootstrap is not None
            else None
        ),
    )
    if reason is not None:
        if require_resume:
            raise ValueError(f"error: run '{resolved_run_id.name}' is not resumable: {reason}.")
        return None

    if resume_bootstrap is not None:
        return resume_bootstrap.resume_context

    raw_feature_branch = prev_run.get("feature_branch")
    raw_worktree_path = prev_run.get("worktree_path")
    feature_branch = (
        raw_feature_branch
        if isinstance(raw_feature_branch, str) and raw_feature_branch
        else None
    )
    worktree_path = (
        raw_worktree_path
        if isinstance(raw_worktree_path, str) and raw_worktree_path
        else None
    )

    if not require_resume and not _prompt_resume(
        resolved_run_id.name,
        feature_branch,
        worktree_path,
    ):
        return None

    return _reconstruct_resume_context(
        resolved_run_id=resolved_run_id,
        run_dir=run_dir,
        prev_run=prev_run,
        plan_path=plan_path,
        frozen_run_identity=frozen_run_identity,
        reset_scope=reset_scope,
        require_resume=require_resume,
        workflow_steps=getattr(workflow_config, "steps", None),
    )


def _resolve_repo_root() -> Path | None:
    """Resolve project root from cwd using git discovery.

    Returns the resolved root, or None when the run must be aborted due to an
    ambiguous root that cannot be resolved interactively.
    """
    working_dir = Path.cwd().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(working_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return working_dir

    if result.returncode != 0:
        return working_dir

    git_root = Path(result.stdout.strip()).resolve()
    if git_root == working_dir:
        return working_dir

    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not is_tty:
        print(
            f"error: current directory '{working_dir}' is inside a git repository "
            f"rooted at '{git_root}'.\n"
            f"Rerun from '{git_root}' to use the repository root, or rerun from a "
            f"directory that is its own git root.",
            file=sys.stderr,
        )
        return None

    try:
        response = input(
            f"Current directory '{working_dir}' is inside '{git_root}'.\n"
            f"Use git root '{git_root}' as project root? [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return working_dir

    if response in ("", "y", "yes"):
        return git_root
    return working_dir


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _deduplicate_preserve_order(seq: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aflow",
        description="Run plan-driven coding workflows through existing agent CLIs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run",
        description="Run an aflow workflow from a plan file or resume a saved run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=RUN_HELP,
    )
    run_parser.add_argument(
        "--plan", "-p",
        type=str,
        default=None,
        metavar="PLAN_FILE",
        help="Path to the plan Markdown file.",
    )
    run_parser.add_argument(
        "--workflow", "-w",
        type=str,
        default=None,
        metavar="WORKFLOW_NAME",
        help="Name of the workflow to run.",
    )
    run_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="CONFIG_FILE",
        help="Use this current workflow configuration for the run or resume.",
    )
    run_parser.add_argument(
        "--max-turns", "-mt",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Maximum number of turns for this run. Defaults to [aflow].max_turns.",
    )
    run_parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        metavar="RUN_ID",
        help="Use a canonical pre-reserved run identity for this launch.",
    )
    run_parser.add_argument(
        "--team", "-t",
        type=str,
        default=None,
        metavar="TEAM_NAME",
        help="Override the workflow team for this run.",
    )
    run_parser.add_argument(
        "--start-step", "-ss",
        type=str,
        default=None,
        metavar="STEP_NAME",
        help="Start the workflow from a specific step instead of the first step.",
    )
    run_parser.add_argument(
        "--resume",
        nargs="?",
        const="AUTO",
        default=None,
        metavar="RUN_ID",
        help=(
            "Resume a previous unfinished worktree run. With no RUN_ID, requires a resumable last run "
            "from the current shell context. With RUN_ID, resumes that exact run."
        ),
    )
    run_parser.add_argument(
        "--continue-from-current",
        action="store_true",
        help=(
            "Start a new worktree run from the currently checked-out accepted "
            "branch recorded in the plan. Cannot be combined with --resume."
        ),
    )
    run_parser.add_argument(
        "--resume-rehome-worktree",
        metavar="PATH",
        help="With an explicit --resume RUN_ID, validate and reuse a relocated registered worktree.",
    )
    run_parser.add_argument(
        "--resume-reset-scope",
        action="store_true",
        help=(
            "With an explicit --resume RUN_ID, reuse its worktree and manager history "
            "but restart from the invocation's original plan and fresh checkpoint scope."
        ),
    )
    run_parser.add_argument("run_args", nargs=argparse.REMAINDER)

    daemon_worker_parser = subparsers.add_parser(
        "daemon-worker",
        help=argparse.SUPPRESS,
    )
    daemon_worker_parser.add_argument("--repo-root", required=True, type=Path)
    daemon_worker_parser.add_argument("--config", required=True, type=Path)
    daemon_worker_parser.add_argument("--run-id", required=True)
    daemon_worker_parser.add_argument(
        "--extra-instruction",
        action="append",
        default=[],
        help=argparse.SUPPRESS,
    )

    install_parser = subparsers.add_parser(
        "install-skills",
        description="Install the bundled aflow skills into harness skill directories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=INSTALL_SKILLS_HELP,
    )
    install_parser.add_argument(
        "destination",
        nargs="?",
        help=(
            f"Root directory that will receive the {len(DEFAULT_BUNDLED_SKILL_NAMES)} default bundled skill subdirectories. "
            "Omit it to auto-detect supported harness CLIs on PATH and install into each harness's "
            "global skill directory."
        ),
    )
    install_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    install_parser.add_argument(
        "--include-optional",
        action="store_true",
        help="Include optional bundled skills in the installation.",
    )
    install_parser.add_argument(
        "--only",
        action="append",
        metavar="SKILL",
        help="Install only the named skill(s). Can be repeated. Cannot be combined with --include-optional.",
    )

    noop_parser = subparsers.add_parser(
        "noop-plan", help="Copy the bundled NO-OP plan and initialize external playbooks.",
        description="Create a fast checkpoint smoke plan; existing playbooks are preserved unless --reset is given.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    noop_parser.add_argument("--output", type=Path, default=Path("aflow-noop-plan.md"), help="Destination plan file.")
    noop_parser.add_argument("--state-dir", type=Path, default=Path("/tmp/aflow_noop_plan"), help="External playbook and marker directory.")
    noop_parser.add_argument("--checkpoints", type=int, default=3, help="Number of checkpoints (1–100).")
    noop_parser.add_argument("--force", action="store_true", help="Replace the output plan.")
    noop_parser.add_argument("--reset", action="store_true", help="Reset playbooks and CP markers.")
    noop_step_parser = subparsers.add_parser(
        "noop-step", help="Execute one external NO-OP playbook entry (used by the plan).",
    )
    noop_step_parser.add_argument("role", choices=("worker", "reviewer"))
    noop_step_parser.add_argument("checkpoint", type=int)
    noop_step_parser.add_argument("--state-dir", type=Path, default=Path("/tmp/aflow_noop_plan"))

    analyze_parser = subparsers.add_parser(
        "analyze",
        description="Analyze aflow run logs and extract high-signal debugging information.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze_parser.add_argument(
        "run_id",
        nargs="?",
        help="Run ID to analyze. If not provided, uses the current shell's last run, AFLOW_LAST_RUN_ID, or .aflow/last_run_id.",
    )
    analyze_parser.add_argument(
        "--all",
        action="store_true",
        help="Analyze a corpus of runs instead of a single run.",
    )
    analyze_parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root containing .aflow/runs. Defaults to current directory.",
    )
    analyze_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of run directories to include in corpus mode (default: 20).",
    )
    analyze_parser.add_argument(
        "--include-noise",
        action="store_true",
        help="Include low-signal test noise runs instead of filtering them out.",
    )
    analyze_parser.add_argument(
        "--manager-context",
        choices=("lite", "full"),
        help="Rebuild the read-only manager context for a finalized turn.",
    )
    analyze_parser.add_argument(
        "--turn",
        type=int,
        help="Finalized workflow turn number to use with --manager-context (default: latest).",
    )

    show_parser = subparsers.add_parser(
        "show",
        description="Show workflow diagrams and role/team relationships from the loaded config.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    show_parser.add_argument(
        "workflow_name",
        nargs="?",
        help="Optional workflow name. Omit it to show every workflow in config order.",
    )

    ui_parser = subparsers.add_parser(
        "ui",
        description="Serve the AFlow web UI from any directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ui_lifecycle = ui_parser.add_mutually_exclusive_group()
    ui_lifecycle.add_argument(
        "--daemon",
        action="store_true",
        help="Start the UI in the background and return once it is ready.",
    )
    ui_lifecycle.add_argument("--status", action="store_true", help="Report the UI server status and exit.")
    ui_lifecycle.add_argument("--stop", action="store_true", help="Stop the UI server (running workflows are not signalled).")
    ui_parser.add_argument("--host", type=str, default=None, help="Bind host override for this process (default: [server] bind_host).")
    ui_parser.add_argument("--port", type=int, default=None, help="Bind port override for this process (default: [server] bind_port).")
    ui_parser.add_argument(
        "--ui-internal-serve",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    ui_worker_parser = subparsers.add_parser(
        "ui-worker",
        help=argparse.SUPPRESS,
    )
    ui_worker_parser.add_argument("--receipt-dir", required=True, type=Path)
    ui_worker_parser.add_argument("--nonce", required=True)
    ui_worker_parser.add_argument("worker_argv", nargs=argparse.REMAINDER)

    return parser


def _parse_run_args(
    run_args: list[str],
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Split REMAINDER args into (workflow_name, plan_file, extra_instructions).

    With '--' present: everything before is positionals, everything after is extra.
    Without '--': all args are positionals, no extra instructions.

    Positional rules:
      1 positional  -> plan_file only, no workflow
      2+ positionals -> first is workflow, second is plan_file
    """
    if "--" in run_args:
        sep = run_args.index("--")
        extra = tuple(run_args[sep + 1 :])
        positionals = run_args[:sep]
    else:
        extra = ()
        positionals = run_args

    if not positionals:
        return None, None, extra

    if len(positionals) == 1:
        return None, positionals[0], extra

    workflow_name = positionals[0]
    plan_file = positionals[1]
    if len(positionals) > 2:
        extra = tuple(positionals[2:]) + extra
    return workflow_name, plan_file, extra


def _resolve_run_arguments(
    plan_flag: str | None,
    workflow_flag: str | None,
    run_args: list[str],
    workflow_config: WorkflowConfig,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Resolve plan and workflow from explicit flags and positional args.

    Positional parsing:
      - Extract positionals before '--' (if present); everything after is extra instructions
      - 1 positional: treat as plan file
      - 2+ positionals: infer by checking if token resolves to existing file vs configured workflow name
      - extra positionals beyond 2 are appended to extra instructions

    Duplicate handling:
      - If plan comes from both flag and positional, they must resolve to the same value
      - If workflow comes from both flag and positional, they must resolve to the same value
      - Conflicting duplicates trigger a clear error
      - Ambiguous dual-positionals (both plan candidates, both workflow candidates, or neither) trigger a clear error

    Returns (workflow_name, plan_file, extra_instructions) where workflow_name and/or plan_file
    may be None if not determinable.
    """
    if "--" in run_args:
        sep = run_args.index("--")
        extra_instructions = tuple(run_args[sep + 1 :])
        positionals = run_args[:sep]
    else:
        extra_instructions = ()
        positionals = run_args

    known_workflows = set(workflow_config.workflows.keys())

    # Extract positional plan and workflow candidates
    positional_plan = None
    positional_workflow = None
    extra_positionals = []

    if len(positionals) == 0:
        pass
    elif len(positionals) == 1:
        # Single positional is always treated as plan, never as workflow
        positional_plan = positionals[0]
    else:
        # Two or more positionals: resolve by meaning
        first_token = positionals[0]
        second_token = positionals[1]

        # Check which token is a workflow and whether each file exists
        first_is_workflow = first_token in known_workflows
        second_is_workflow = second_token in known_workflows
        first_exists = Path(first_token).exists()
        second_exists = Path(second_token).exists()

        # Apply resolution rules in order:
        # 1. If exactly one is a workflow, treat the other as plan (even if it doesn't exist)
        #    Only accept this if the workflow token is not also an existing file (which would create ambiguity)
        if first_is_workflow and not second_is_workflow:
            if first_exists and second_exists:
                # Both tokens are existing files, and first is also a workflow -> ambiguous
                raise ValueError(
                    f"error: cannot determine which positional is the plan file: "
                    f"'{first_token}' is a configured workflow and also resolves to an existing file, "
                    f"and '{second_token}' resolves to an existing file. "
                    f"Only one plan file is allowed per run. Use --plan to specify which one."
                )
            positional_workflow = first_token
            positional_plan = second_token
        elif second_is_workflow and not first_is_workflow:
            if first_exists and second_exists:
                # Both tokens are existing files, and second is also a workflow -> ambiguous
                raise ValueError(
                    f"error: cannot determine which positional is the plan file: "
                    f"'{second_token}' is a configured workflow and also resolves to an existing file, "
                    f"and '{first_token}' resolves to an existing file. "
                    f"Only one plan file is allowed per run. Use --plan to specify which one."
                )
            positional_workflow = second_token
            positional_plan = first_token
        # 2. If both are workflows, both are workflow names -> ambiguous
        elif first_is_workflow and second_is_workflow:
            raise ValueError(
                f"error: cannot determine which positional is the plan file and which is the workflow: "
                f"'{first_token}' and '{second_token}'. "
                f"Both are configured workflow names. "
                f"Use --plan and --workflow flags to disambiguate."
            )
        # 3. If neither is a workflow, check file existence to distinguish plan from workflow intent
        else:
            # Neither is a workflow name
            if first_exists and second_exists:
                # Both are existing files -> can't choose which is plan
                raise ValueError(
                    f"error: cannot determine which positional is the plan file: "
                    f"both '{first_token}' and '{second_token}' resolve to existing files. "
                    f"Only one plan file is allowed per run. Use --plan to specify which one."
                )
            elif first_exists and not second_exists:
                # First exists, second doesn't -> first is plan, second is unclassified
                raise ValueError(
                    f"error: cannot determine which positional is the plan file and which is the workflow: "
                    f"'{first_token}' resolves to an existing file, but '{second_token}' is neither a "
                    f"configured workflow name nor an existing file. "
                    f"Use --plan and --workflow flags to specify them explicitly."
                )
            elif second_exists and not first_exists:
                # Second exists, first doesn't -> second is plan, first is unclassified
                raise ValueError(
                    f"error: cannot determine which positional is the plan file and which is the workflow: "
                    f"'{second_token}' resolves to an existing file, but '{first_token}' is neither a "
                    f"configured workflow name nor an existing file. "
                    f"Use --plan and --workflow flags to specify them explicitly."
                )
            else:
                # Neither exists and neither is a workflow -> can't determine
                raise ValueError(
                    f"error: cannot determine which positional is the plan file and which is the workflow: "
                    f"'{first_token}' and '{second_token}'. "
                    f"Neither resolves to an existing file, and neither is a configured workflow name. "
                    f"Use --plan and --workflow flags to specify them explicitly."
                )

        # Collect extra positionals beyond the first two
        if len(positionals) > 2:
            extra_positionals = positionals[2:]

    # Resolve final values from flags and positionals
    final_plan = None
    final_workflow = None

    # Handle plan resolution
    if plan_flag is not None and positional_plan is not None:
        # Canonicalize both paths for comparison
        flag_resolved = Path(plan_flag).expanduser().resolve()
        positional_resolved = Path(positional_plan).expanduser().resolve()
        if flag_resolved != positional_resolved:
            raise ValueError(
                f"error: conflicting plan specifications: --plan='{plan_flag}' but positional '{positional_plan}'. "
                f"These must resolve to the same file."
            )
        final_plan = plan_flag  # Use the user-provided spelling, not the resolved one
    elif plan_flag is not None:
        final_plan = plan_flag
    elif positional_plan is not None:
        final_plan = positional_plan

    # Handle workflow resolution
    if workflow_flag is not None and positional_workflow is not None:
        if workflow_flag != positional_workflow:
            raise ValueError(
                f"error: conflicting workflow specifications: --workflow='{workflow_flag}' but positional '{positional_workflow}'. "
                f"These must resolve to the same workflow name."
            )
        final_workflow = workflow_flag
    elif workflow_flag is not None:
        final_workflow = workflow_flag
    elif positional_workflow is not None:
        final_workflow = positional_workflow

    # Append any extra positionals to extra instructions
    all_extra = tuple(extra_positionals) + extra_instructions

    return final_workflow, final_plan, all_extra


def _format_success_summary(workflow_name: str, turns_completed: int, end_reason: WorkflowEndReason) -> str:
    turn_label = "turn" if turns_completed == 1 else "turns"
    return (
        f"Workflow '{workflow_name}' completed after {turns_completed} {turn_label} "
        f"because {describe_end_reason(end_reason)}."
    )


def _pick_workflow_step(steps: dict[str, WorkflowStepConfig]) -> str | None:
    step_names = list(steps.keys())
    if not step_names:
        return None

    while True:
        print("Select the workflow step to start from:")
        for index, step_name in enumerate(step_names, start=1):
            print(f"  {index}. {step_name}")
        try:
            response = input(f"Enter a number between 1 and {len(step_names)}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            choice = int(response)
        except ValueError:
            print(
                f"error: enter a number between 1 and {len(step_names)}",
                file=sys.stderr,
            )
            continue
        if choice < 1 or choice > len(step_names):
            print(
                f"error: enter a number between 1 and {len(step_names)}",
                file=sys.stderr,
            )
            continue
        return step_names[choice - 1]


def _resolve_numeric_start_step(raw_value: str, workflow: WorkflowConfig) -> tuple[str, str | None]:
    """
    Resolve a raw start-step value (from --start-step/-ss) to a canonical step name.

    If raw_value is a plain ASCII base-10 integer (only ASCII decimal digits 0-9), treat it as a 1-based workflow step index.
    Otherwise, treat it as a step name.

    Returns (resolved_step_name, error_message).
    If successful, error_message is None.
    If parsing or validation fails, resolved_step_name is the raw_value and error_message describes the issue.

    Note: The library's prepare_startup() now handles numeric step resolution internally.
    This function is retained for backward compatibility and direct testing.
    """
    step_names = list(workflow.steps.keys())

    # Check if raw_value is a plain ASCII base-10 integer (only ASCII decimal digits, no signs or underscores)
    is_ascii_decimal = raw_value and all(c in '0123456789' for c in raw_value)
    if is_ascii_decimal:
        index = int(raw_value)

        # Validate numeric index
        if index < 1 or index > len(step_names):
            available = ", ".join(step_names)
            error = (
                f"error: start-step index {index} is out of range. "
                f"Valid indexes: 1 to {len(step_names)}. "
                f"Available steps: {available}"
            )
            return raw_value, error

        # Map 1-based index to step name
        resolved_name = step_names[index - 1]
        return resolved_name, None
    else:
        # Not a plain ASCII integer, treat as step name
        return raw_value, None


def _confirm_startup_recovery(error_message: str) -> bool:
    print(error_message, file=sys.stderr)
    try:
        response = input("Recover using the existing retry flow? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return response in ("y", "yes")


def _print_bootstrap_paths(config_path: Path) -> None:
    workflows_path = config_path.with_name("workflows.toml")
    print(
        "Bootstrapped aflow config files. Edit these paths and rerun when ready:",
        file=sys.stderr,
    )
    print(f"  {config_path}", file=sys.stderr)
    print(f"  {workflows_path}", file=sys.stderr)


def _maybe_move_completed_plan_to_done(repo_root: Path, plan_path: Path, *, is_complete: bool) -> Path:
    if not is_complete or not plan_path.is_file():
        return plan_path
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not is_tty:
        return plan_path

    in_progress_root = (repo_root / "plans" / "in-progress").resolve()
    try:
        plan_path.resolve().relative_to(in_progress_root)
    except ValueError:
        return plan_path

    try:
        response = input(
            f"Plan '{plan_path.name}' is complete and still in plans/in-progress. "
            "Move it to plans/done? [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return plan_path
    if response in ("", "y", "yes"):
        return move_completed_plan_to_done(repo_root, plan_path)
    return plan_path


def _add_controller_state_to_analysis(
    payload: object,
    *,
    repo_root: Path,
) -> None:
    """Add safe run-state diagnostics to CLI analysis without exposing notes."""
    if isinstance(payload, list):
        for item in payload:
            _add_controller_state_to_analysis(item, repo_root=repo_root)
        return
    if not isinstance(payload, dict):
        return
    run_id = payload.get("run_id")
    if isinstance(run_id, str) and "controller_state" not in payload:
        run_json = load_run_json(repo_root / ".aflow" / "runs" / run_id)
        if run_json is not None:
            override_result = run_json.get("override_result")
            safe_override_result = None
            if isinstance(override_result, Mapping):
                safe_override_result = {
                    key: override_result.get(key)
                    for key in (
                        "status",
                        "digest",
                        "message",
                        "next_step",
                        "team",
                        "max_turns",
                        "has_notes",
                        "applied",
                        "recorded_at",
                    )
                    if key in override_result
                }
            payload["controller_state"] = {
                "schema_version": run_json.get("schema_version"),
                "frozen_config": run_json.get("frozen_config"),
                "override_file_present": bool(
                    run_json.get("override_file_present", False)
                ),
                "last_override_result": safe_override_result,
                "corrected_override_required": (
                    isinstance(override_result, Mapping)
                    and override_result.get("status") == "rejected"
                ),
            }
    for value in tuple(payload.values()):
        _add_controller_state_to_analysis(value, repo_root=repo_root)


class TerminalObserver(ExecutionObserver):
    """Observer that formats execution events for terminal rendering."""

    def on_event(self, event: ExecutionEvent) -> None:
        pass


def _install_skills_exit_code(result) -> int:
    """Wrap the structured installer result into the CLI exit convention."""
    from .skill_installer import InstallResult

    if result is None:
        return 0
    if isinstance(result, InstallResult):
        if result.cancelled:
            return 0
        return 0 if result.succeeded else 1
    return 0


def run_install_skills(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(["install-skills"] + ([] if argv is None else argv))
    try:
        only_skills = _deduplicate_preserve_order(tuple(args.only)) if args.only else None
        result = install_skills(
            destination=args.destination,
            yes=args.yes,
            only_skills=only_skills,
            include_optional=args.include_optional,
        )
    except InstallerError as exc:
        print(exc, file=sys.stderr)
        return 1
    return _install_skills_exit_code(result)


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(tokens)

    if args.command in {"noop-plan", "noop-step"}:
        from .noop_plan import NoopError, execute_noop_step, install_noop_plan
        try:
            if args.command == "noop-plan":
                print(install_noop_plan(args.output, state_dir=args.state_dir,
                    checkpoints=args.checkpoints, force=args.force, reset=args.reset), end="")
                return 0
            code, message = execute_noop_step(args.role, args.checkpoint, state_dir=args.state_dir)
            print(message)
            return code
        except (NoopError, OSError, subprocess.SubprocessError) as exc:
            print(f"AFLOW_STOP: NO-OP fixture: {exc}", file=sys.stderr)
            return 1

    if args.command == "install-skills":
        try:
            only_skills = _deduplicate_preserve_order(tuple(args.only)) if args.only else None
            result = install_skills(
                destination=args.destination,
                yes=args.yes,
                only_skills=only_skills,
                include_optional=args.include_optional,
            )
        except InstallerError as exc:
            print(exc, file=sys.stderr)
            return 1
        return _install_skills_exit_code(result)

    if args.command == "ui":
        from .ui_cli import handle_ui_command

        return handle_ui_command(args)

    if args.command == "ui-worker":
        from .ui_cli import handle_ui_worker_command

        worker_argv = [arg for arg in args.worker_argv if arg != "--"]
        if not worker_argv:
            print("aflow ui-worker: no worker argv supplied after --", file=sys.stderr)
            return 2
        args.worker_argv = worker_argv
        return handle_ui_worker_command(args)

    if args.command == "daemon-worker":
        from .daemon import worker_main

        return worker_main(
            repo_root=args.repo_root,
            config_path=args.config,
            run_id=args.run_id,
            extra_instructions=tuple(args.extra_instruction),
        )

    if args.command == "analyze":
        import json

        request = AnalyzeRequest(
            repo_root=(args.repo_root or Path.cwd()).resolve(),
            run_id=args.run_id,
            all=args.all,
            limit=args.limit,
            include_noise=args.include_noise,
            manager_context=args.manager_context,
            turn=args.turn,
        )
        try:
            payload = analyze_runs(request)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        _add_controller_state_to_analysis(
            payload,
            repo_root=request.repo_root,
        )
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    if (
        args.command == "run"
        and args.resume_reset_scope
        and args.resume in (None, "AUTO")
    ):
        print(
            "error: --resume-reset-scope requires an explicit --resume RUN_ID",
            file=sys.stderr,
        )
        return 1

    if (
        args.command == "run"
        and args.continue_from_current
        and (args.resume is not None or args.resume_reset_scope)
    ):
        print(
            "error: --continue-from-current cannot be combined with any --resume option",
            file=sys.stderr,
        )
        return 1
    if args.command == "run" and args.resume_rehome_worktree is not None:
        if args.resume in (None, "AUTO") or args.resume_reset_scope:
            print("error: --resume-rehome-worktree requires an explicit --resume RUN_ID without --resume-reset-scope", file=sys.stderr)
            return 1

    config_path: Path | None = None
    config_path_is_explicit = False
    if args.command in (None, "run", "show"):
        if args.command == "run" and args.config is not None:
            config_path = Path(args.config).expanduser().resolve()
            config_path_is_explicit = True
            created_paths = ()
        else:
            config_path, created_paths = _bootstrap_config_files()
        if created_paths:
            _print_bootstrap_paths(config_path)
            return 0

    if args.command == "show":
        if config_path is None:
            config_path = bootstrap_config()
        try:
            workflow_config = load_workflow_config(config_path)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1

        validation_errors = validate_workflow_config(workflow_config)
        if validation_errors:
            errors = "\n".join(f"  {e}" for e in validation_errors)
            print(
                f"Config validation errors:\n{errors}",
                file=sys.stderr,
            )
            return 1

        workflow_name = args.workflow_name
        if workflow_name is not None and workflow_name not in workflow_config.workflows:
            available = ", ".join(workflow_config.workflows)
            suffix = f" Available workflows: {available}." if available else ""
            print(f"error: unknown workflow '{workflow_name}'.{suffix}", file=sys.stderr)
            return 1

        print(build_workflow_show(
            config=workflow_config,
            workflow_name=workflow_name,
        ))
        return 0

    if args.command != "run":
        parser.print_help(sys.stderr)
        return 1

    repo_root = _resolve_repo_root()
    if repo_root is None:
        return 1
    if config_path is None:
        config_path = bootstrap_config()

    requested_resume_run_id: str | None = None
    require_resume = False
    if args.resume is not None:
        require_resume = True
        if args.resume != "AUTO":
            requested_resume_run_id = args.resume

    if not require_resume and args.plan is None and not args.run_args:
        print("error: plan_file is required", file=sys.stderr)
        return 1

    if require_resume and not config_path_is_explicit:
        # Do not read the ordinary default before the resume metadata can
        # select its saved live source. Positional ambiguity is resolved after
        # that source has been loaded below.
        workflow_config = None
        workflow_arg = args.workflow
        plan_file_arg = args.plan
        _, _, extra_instructions = _parse_run_args(args.run_args)
        extra_instructions_provided = (
            "--" in args.run_args or bool(extra_instructions)
        )
    else:
        try:
            if require_resume:
                # Resume bootstrap resolves the saved live source before doing
                # current-workflow admission. The object here only parses
                # optional positional arguments and is replaced by the live
                # result before startup.
                workflow_config = load_workflow_config(config_path)
            else:
                workflow_config = load_live_config(
                    config_path,
                    loader=load_workflow_config,
                ).workflow_config
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1

        try:
            workflow_arg, plan_file_arg, extra_instructions = _resolve_run_arguments(
                args.plan, args.workflow, args.run_args, workflow_config
            )
            extra_instructions_provided = (
                "--" in args.run_args or bool(extra_instructions)
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

    if not require_resume and plan_file_arg is None:
        print("error: plan_file is required", file=sys.stderr)
        return 1

    if not require_resume:
        placeholders = find_placeholders(workflow_config)
        if placeholders:
            keys = "\n".join(f"  {k}" for k in placeholders)
            print(
                f"Config bootstrapped. Fill in the following model values before running:\n{keys}",
                file=sys.stderr,
            )
            return 1

        validation_errors = validate_workflow_config(workflow_config)
        if validation_errors:
            errors = "\n".join(f"  {e}" for e in validation_errors)
            print(
                f"Config validation errors:\n{errors}",
                file=sys.stderr,
            )
            return 1

    resume_bootstrap: ResumeBootstrap | None = None
    if require_resume:
        try:
            resume_bootstrap = _bootstrap_resume_invocation(
                repo_root=repo_root,
                config_path=(config_path if config_path_is_explicit else None),
                default_config_path=config_path,
                config_path_is_explicit=config_path_is_explicit,
                workflow_config=workflow_config,
                requested_run_id=requested_resume_run_id,
                workflow_arg=workflow_arg,
                plan_file_arg=plan_file_arg,
                team_arg=args.team,
                start_step_arg=args.start_step,
                max_turns_arg=args.max_turns,
                extra_instructions_arg=extra_instructions,
                extra_instructions_provided=extra_instructions_provided,
                reset_scope=args.resume_reset_scope,
                rehome_worktree=args.resume_rehome_worktree,
                live_loader=load_workflow_config,
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        if not config_path_is_explicit:
            try:
                parsed_workflow, parsed_plan, parsed_extra = _resolve_run_arguments(
                    args.plan,
                    args.workflow,
                    args.run_args,
                    resume_bootstrap.workflow_config,
                )
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            if parsed_workflow is not None and parsed_workflow != resume_bootstrap.workflow_name:
                print(
                    f"error: resume workflow mismatch: requested '{parsed_workflow}', "
                    f"but run '{resume_bootstrap.resolved_run_id.name}' saved "
                    f"'{resume_bootstrap.workflow_name}'.",
                    file=sys.stderr,
                )
                return 1
            if parsed_plan is not None:
                requested_plan = Path(parsed_plan).expanduser()
                if not requested_plan.is_absolute():
                    requested_plan = Path.cwd() / requested_plan
                if requested_plan.resolve() != resume_bootstrap.plan_path:
                    print(
                        f"error: resume plan mismatch: requested '{requested_plan.resolve()}', "
                        f"but run '{resume_bootstrap.resolved_run_id.name}' saved "
                        f"'{resume_bootstrap.plan_path}'.",
                        file=sys.stderr,
                    )
                    return 1
            extra_instructions = parsed_extra
        workflow_arg = resume_bootstrap.workflow_name
        plan_file_arg = str(resume_bootstrap.plan_path)
        startup_start_step = resume_bootstrap.start_step
        startup_max_turns = resume_bootstrap.max_turns
        startup_team = resume_bootstrap.team
        extra_instructions = resume_bootstrap.extra_instructions
        workflow_config = resume_bootstrap.workflow_config
    else:
        startup_start_step = args.start_step
        startup_max_turns = args.max_turns
        startup_team = args.team

    if require_resume:
        placeholders = find_placeholders(workflow_config)
        if placeholders:
            keys = "\n".join(f"  {k}" for k in placeholders)
            print(
                f"Config bootstrapped. Fill in the following model values before running:\n{keys}",
                file=sys.stderr,
            )
            return 1
        validation_errors = validate_workflow_config(workflow_config)
        if validation_errors:
            errors = "\n".join(f"  {e}" for e in validation_errors)
            print(
                f"Config validation errors:\n{errors}",
                file=sys.stderr,
            )
            return 1

    if plan_file_arg is None:
        print("error: plan_file is required", file=sys.stderr)
        return 1

    plan_path = Path(plan_file_arg).expanduser().resolve()
    startup_workflow_name = workflow_arg or workflow_config.aflow.default_workflow

    startup_request = StartupRequest(
        repo_root=repo_root,
        plan_path=plan_path,
        config_path=(
            resume_bootstrap.config_path if resume_bootstrap is not None else config_path
        ),
        workflow_config=(
            resume_bootstrap.workflow_config
            if resume_bootstrap is not None
            else workflow_config
        ),
        workflow_name=startup_workflow_name,
        start_step=startup_start_step,
        max_turns=startup_max_turns,
        team=startup_team,
        start_step_explicit=(
            True if resume_bootstrap is not None else args.start_step is not None
        ),
        team_explicit=(
            resume_bootstrap.team_explicit
            if resume_bootstrap is not None
            else args.team is not None
        ),
        max_turns_explicit=(
            resume_bootstrap.max_turns_explicit
            if resume_bootstrap is not None
            else args.max_turns is not None
        ),
        extra_instructions=extra_instructions,
        pre_recovered_plan=(
            resume_bootstrap.parsed_plan
            if resume_bootstrap is not None
            else None
        ),
        resume_requested=require_resume,
        continue_from_current=args.continue_from_current,
        reserved_run_id=args.run_id,
    )

    prepared_run = _handle_startup_questions(startup_request)
    if prepared_run is None:
        return 1

    if prepared_run.continuation_mode == "current_branch":
        resume_ctx = None
    else:
        try:
            resume_ctx = _detect_resume_candidate(
                repo_root=prepared_run.repo_root,
                workflow_config=workflow_config,
                workflow_name=prepared_run.workflow_name,
                plan_path=prepared_run.plan_path,
                team=prepared_run.team,
                selected_start_step=prepared_run.start_step,
                max_turns=prepared_run.max_turns,
                extra_instructions=prepared_run.extra_instructions,
                requested_run_id=requested_resume_run_id,
                require_resume=require_resume,
                reset_scope=args.resume_reset_scope,
                resume_bootstrap=resume_bootstrap,
                allow_start_step_override=(
                    resume_bootstrap is not None
                    and resume_bootstrap.start_step_override
                ),
                allow_max_turns_override=(
                    resume_bootstrap is not None
                    and args.max_turns is not None
                ),
                team_explicit=prepared_run.team_explicit,
                max_turns_explicit=prepared_run.max_turns_explicit,
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

    workflow_spec = workflow_config.workflows[prepared_run.workflow_name]
    workflow_graph_source = WorkflowGraphSource(
        declared_steps=dict(workflow_spec.declared_steps),
        executable_steps=dict(workflow_spec.steps),
        excluded_step_names=workflow_spec.excluded_steps,
    )
    banner = BannerRenderer(
        config_max_turns=prepared_run.max_turns,
        config_plan_path=prepared_run.plan_path,
        workflow_steps=workflow_spec.steps,
        workflow_graph_source=workflow_graph_source,
        config_banner_files_limit=workflow_config.aflow.banner_files_limit,
        workflow_name=prepared_run.workflow_name,
        original_plan_path=prepared_run.plan_path,
        repo_root=prepared_run.repo_root,
    )
    observer = TerminalObserver()

    try:
        result = execute_workflow(
            prepared_run,
            banner=banner,
            resume=resume_ctx,
            observer=observer,
        )
    except WorkflowError as exc:
        print(exc.summary, file=sys.stderr)
        return 1
    try:
        _maybe_move_completed_plan_to_done(
            prepared_run.repo_root,
            prepared_run.plan_path,
            is_complete=prepared_run.move_completed_plan_to_done,
        )
    except WorkflowError as exc:
        print(exc.summary, file=sys.stderr)
        return 1
    print(_format_success_summary(prepared_run.workflow_name, result.turns_completed, result.end_reason))
    return 0


def _handle_startup_questions(request: StartupRequest) -> PreparedRun | None:
    """Process startup questions interactively, returning PreparedRun or None on error."""
    from .api.startup import StartupError

    try:
        result = prepare_startup(request)
    except StartupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None

    while isinstance(result, StartupQuestion):
        answer = _answer_startup_question(result)
        if answer is None or (isinstance(answer, bool) and not answer):
            print("startup aborted", file=sys.stderr)
            return None

        try:
            result = prepare_startup_with_answer(result, request, answer)
            if isinstance(result, PreparedRun):
                break
        except StartupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None

    return result


def _answer_startup_question(question: StartupQuestion) -> str | int | bool | None:
    """Interactively answer a startup question."""
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()

    if question.kind == StartupQuestionKind.CONFIRM_RECOVERY:
        if not is_tty:
            print(
                f"error: {question.message} "
                "Interactive confirmation is required.",
                file=sys.stderr,
            )
            return None
        print(question.message, file=sys.stderr)
        try:
            response = input("Recover using the existing retry flow? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return response in ("y", "yes")

    if question.kind == StartupQuestionKind.PICK_STEP:
        if not is_tty:
            step_names = question.choices
            print(
                f"error: {question.message} "
                f"Re-run with --start-step STEP_NAME. Available steps: {', '.join(step_names)}",
                file=sys.stderr,
            )
            return None
        step_names = question.choices
        step_index = _pick_workflow_step_interactive(step_names)
        if step_index is None:
            return None
        return step_index

    if question.kind == StartupQuestionKind.CONFIRM_WORKTREE_DIRTY:
        if not is_tty:
            print(
                f"error: {question.message} "
                "Interactive confirmation is required.",
                file=sys.stderr,
            )
            return None
        try:
            response = input(f"{question.message} [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        return response in ("y", "yes")

    return None


def _pick_workflow_step_interactive(step_names: list[str]) -> int | None:
    """Interactively pick a workflow step by index."""
    while True:
        print("Select the workflow step to start from:")
        for index, step_name in enumerate(step_names, start=1):
            print(f"  {index}. {step_name}")
        try:
            response = input(f"Enter a number between 1 and {len(step_names)}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        try:
            choice = int(response)
        except ValueError:
            print(
                f"error: enter a number between 1 and {len(step_names)}",
                file=sys.stderr,
            )
            continue
        if choice < 1 or choice > len(step_names):
            print(
                f"error: enter a number between 1 and {len(step_names)}",
                file=sys.stderr,
            )
            continue
        return choice - 1


if __name__ == "__main__":
    raise SystemExit(main())

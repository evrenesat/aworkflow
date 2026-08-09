from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any, Mapping

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
    WorkflowStepConfig,
)
from .git_status import probe_worktree, classify_dirtiness_by_prefix
from .manager_context import scoped_reviewer_rejection_count
from .plan import PlanParseError, PlanSnapshot, load_plan, load_plan_tolerant
from .skill_installer import InstallerError, install_skills
from .skill_installer import DEFAULT_BUNDLED_SKILL_NAMES
from .run_state import (
    ActiveImplementationScope,
    FrozenRunIdentity,
    PendingFinalizedTurn,
    PendingRepartitionV1,
    RUN_STATE_SCHEMA_VERSION,
    ResumeContext,
    WorkflowEndReason,
    describe_end_reason,
    hotplug_resume_fields,
    manager_resume_fields,
    resolve_resume_override,
)


_MISSING_RESUME_FIELD = object()
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
    _freeze_run_identity,
    _frozen_identity_mismatch,
    _scope_envelope_reference,
    _validate_scope_envelope_bytes,
    load_scope_envelope_for_resume,
    move_completed_plan_to_done,
)
from .repartition import derive_generation_id
from .runlog import load_run_json
from .analyzer import resolve_run_id
from .status import BannerRenderer, WorkflowGraphSource, build_workflow_show

RUN_HELP = """\
Flags:
  --plan/-p PLAN_FILE       Path to the plan Markdown file.
  --workflow/-w WORKFLOW    Name of the workflow to run (default from config).
  --start-step/-ss STEP     Start from this step name or 1-based index (default: first).
  --team/-t TEAM_NAME       Override workflow team.
  --max-turns/-mt N         Maximum turns (default from config).
  --run-id RUN_ID           Canonical pre-reserved run identity (advanced use).
  --resume [RUN_ID]         Resume a saved run; plan and identity are optional when omitted.

Positional arguments:
  [workflow_name] [plan_file]   Either form works:
                                  - One positional: treated as plan_file
                                  - Two positionals: first is workflow (if it matches a config name),
                                    second is plan_file
                                  If only one token matches a workflow name, the other is the plan.

Extra instructions:
  Append -- followed by free-form text to pass extra instructions to each step prompt.

Examples:
  aflow run path/to/plan.md
  aflow run ralph path/to/plan.md
  aflow run --workflow ralph --plan path/to/plan.md
  aflow run --plan path/to/plan.md --start-step my_step
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
    frozen_run_identity: FrozenRunIdentity | None = None


INSTALL_SKILLS_HELP = """\
Auto mode: omit DESTINATION to install the default bundled skills into each supported harness skill
directory for the harness CLIs found on PATH.

Manual mode: provide DESTINATION to install the default bundled skills into that root, one
subdirectory per skill.

Selection flags:
  --include-optional    Include optional bundled skills in the installation.
  --only SKILL          Install only the named skill(s). Can be repeated. Cannot be combined
                        with --include-optional.

Supported auto targets:
  claude -> ~/.claude/skills
  codex -> ~/.agents/skills
  copilot -> ~/.agents/skills
  gemini -> ~/.agents/skills
  kiro -> ~/.kiro/skills
  opencode -> ~/.config/opencode/skills
  pi -> ~/.agents/skills
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


def _resume_plan_path(
    prev_run: Mapping[str, object],
    repo_root: Path,
) -> Path | None:
    """Return the saved original plan path, including the legacy fallback."""
    field_name = "original_plan_path" if "original_plan_path" in prev_run else "plan_path"
    value = prev_run.get(field_name)
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
    """Return the saved invocation max-turns value, with legacy fallback."""
    value = prev_run.get("max_turns")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if "max_turns" in prev_run:
        return None
    effective_value = prev_run.get("effective_max_turns")
    if (
        isinstance(effective_value, int)
        and not isinstance(effective_value, bool)
        and effective_value > 0
    ):
        return effective_value
    return None


def _resume_metadata_error(run_id: Path, field: str, detail: str) -> ValueError:
    return ValueError(f"error: run '{run_id.name}' has invalid {field}: {detail}.")


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
) -> FrozenRunIdentity | None:
    """Decode the persisted configuration identity without legacy guessing."""
    if "schema_version" not in prev_run:
        return None

    schema_version = prev_run.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != RUN_STATE_SCHEMA_VERSION
    ):
        raise _resume_metadata_error(
            run_id,
            "schema_version",
            f"expected integer {RUN_STATE_SCHEMA_VERSION}",
        )

    frozen_value = prev_run.get("frozen_config")
    if not isinstance(frozen_value, Mapping):
        raise _resume_metadata_error(
            run_id,
            "frozen_config",
            "expected a mapping for schema-versioned metadata",
        )

    values: dict[str, str] = {}
    for field_name in ("workflow_name", "config_path", "config_fingerprint"):
        value = frozen_value.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise _resume_metadata_error(
                run_id,
                f"frozen_config.{field_name}",
                "expected a non-empty string",
            )
        values[field_name] = value

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

    plan_path = _resume_plan_path(prev_run, repo_root)
    plan_field = (
        "original_plan_path" if "original_plan_path" in prev_run else "plan_path"
    )
    if plan_path is None:
        raise _resume_metadata_error(
            resolved_run_id,
            plan_field,
            "expected a non-empty string",
        )
    try:
        if not plan_path.is_file():
            raise OSError("file does not exist")
        plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _resume_metadata_error(
            resolved_run_id,
            plan_field,
            f"saved plan '{plan_path}' is not readable ({exc})",
        ) from exc

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

    frozen_run_identity = _decode_frozen_run_identity(prev_run, resolved_run_id)
    if frozen_run_identity is not None:
        current_identity = _freeze_run_identity(
            workflow_name,
            workflow_config,
            config_dir=config_path or (repo_root / "aflow.toml"),
        )
        mismatch = _frozen_identity_mismatch(frozen_run_identity, current_identity)
        if mismatch is not None:
            raise ValueError(
                f"error: run '{resolved_run_id.name}' frozen configuration mismatch: "
                f"{mismatch}."
            )

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
    if saved_start_step is not None and saved_start_step not in workflow_spec.steps:
        raise _resume_metadata_error(
            resolved_run_id,
            "selected_start_step",
            f"'{saved_start_step}' is not a configured step of workflow '{workflow_name}'",
        )

    max_turns = _resume_max_turns(prev_run)
    if max_turns is None:
        raise _resume_metadata_error(
            resolved_run_id,
            "max_turns",
            "expected a positive integer",
        )
    if "effective_max_turns" in prev_run:
        effective_max_turns = prev_run.get("effective_max_turns")
        if effective_max_turns is not None and (
            not isinstance(effective_max_turns, int)
            or isinstance(effective_max_turns, bool)
            or effective_max_turns < 1
        ):
            raise _resume_metadata_error(
                resolved_run_id,
                "effective_max_turns",
                "expected null or a positive integer",
            )

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
        saved_team if "team" in prev_run else workflow_spec.team
    )
    if team_arg is not None and team_arg != effective_saved_team:
        raise ValueError(
            f"error: resume team mismatch: requested '{team_arg}', "
            f"but run '{resolved_run_id.name}' saved '{effective_saved_team}'."
        )

    if start_step_arg is not None:
        resolved_start_step, start_step_error = _resolve_numeric_start_step(
            start_step_arg,
            workflow_spec,
        )
        if start_step_error is not None:
            raise ValueError(start_step_error)
        if saved_start_step is None or resolved_start_step != saved_start_step:
            raise ValueError(
                f"error: resume start-step mismatch: requested '{start_step_arg}', "
                f"but run '{resolved_run_id.name}' saved '{saved_start_step}'."
            )

    if max_turns_arg is not None and max_turns_arg != max_turns:
        raise ValueError(
            f"error: resume max-turns mismatch: requested {max_turns_arg}, "
            f"but run '{resolved_run_id.name}' saved {max_turns}."
        )
    if extra_instructions_provided and extra_instructions_arg != saved_extra:
        raise ValueError(
            f"error: resume extra-instructions mismatch: requested "
            f"{list(extra_instructions_arg)!r}, but run '{resolved_run_id.name}' "
            f"saved {list(saved_extra)!r}."
        )

    mismatch_reason = _resume_candidate_mismatch_reason(
        prev_run,
        workflow_spec,
        repo_root,
        workflow_name,
        plan_path,
        effective_saved_team,
        saved_start_step,
        max_turns,
        saved_extra,
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
    )
    assert resume_context is not None

    return ResumeBootstrap(
        resolved_run_id=resolved_run_id,
        run_dir=run_dir,
        run_json=prev_run,
        plan_path=plan_path,
        workflow_name=workflow_name,
        team=saved_team,
        start_step=saved_start_step,
        max_turns=max_turns,
        extra_instructions=saved_extra,
        resume_context=resume_context,
        frozen_run_identity=frozen_run_identity,
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

    current_setup = current_workflow_config.setup or ()
    if tuple(lifecycle_setup) != current_setup:
        return "its lifecycle setup does not match this invocation"

    feature_branch = prev_run.get("feature_branch")
    worktree_path = prev_run.get("worktree_path")
    main_branch = prev_run.get("main_branch")
    if "branch" in lifecycle_setup:
        if not isinstance(feature_branch, str) or not feature_branch:
            return "it has no recorded feature branch"
        if not isinstance(main_branch, str) or not main_branch:
            return "it has no recorded main branch"
    if "worktree" in lifecycle_setup:
        if not isinstance(worktree_path, str) or not worktree_path:
            return "it has no recorded worktree path"

    status = prev_run.get("status")
    if status not in ("failed", "running", "waiting_for_valid_override"):
        return (
            f"its status is '{status}', not 'failed', 'running', or "
            "'waiting_for_valid_override'"
        )

    last_snapshot = prev_run.get("last_snapshot")
    terminal_integration_only = _is_terminal_integration_resume(prev_run)
    if (
        isinstance(last_snapshot, dict)
        and last_snapshot.get("is_complete") is True
        and not terminal_integration_only
    ):
        return "its last saved plan snapshot was already complete"

    if "merge_status" in prev_run and not terminal_integration_only:
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
    prev_team_none_or_absent = prev_team is None or (isinstance(prev_team, str) and not prev_team.strip())
    current_team_none_or_absent = current_team is None or not current_team.strip()
    if prev_team_none_or_absent != current_team_none_or_absent:
        return "its effective team does not match this invocation"
    if not prev_team_none_or_absent and not current_team_none_or_absent:
        if prev_team != current_team:
            return "its effective team does not match this invocation"

    prev_selected_start_step = prev_run.get("selected_start_step")
    if prev_selected_start_step != current_selected_start_step:
        return "its selected start step does not match this invocation"

    prev_max_turns = _resume_max_turns(prev_run)
    if prev_max_turns is None or prev_max_turns != current_max_turns:
        return "its max-turns value does not match this invocation"

    prev_extra_instructions = prev_run.get("extra_instructions")
    if not isinstance(prev_extra_instructions, list) or tuple(prev_extra_instructions) != current_extra_instructions:
        return "its extra instructions do not match this invocation"

    if terminal_integration_only:
        current_teardown = getattr(current_workflow_config, "teardown", ()) or ()
        if tuple(lifecycle_teardown) != current_teardown:
            return "its lifecycle teardown does not match this invocation"

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


def _interrupted_resume_step(
    run_dir: Path,
    prev_run: Mapping[str, object],
) -> str | None:
    """Return the workflow step whose durable turn never finalized."""
    status = prev_run.get("status")
    current_step_name = prev_run.get("current_step_name")
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


def _pending_finalized_resume_turn(
    run_dir: Path,
    prev_run: Mapping[str, object],
) -> PendingFinalizedTurn | None:
    """Recover a completed harness turn whose manager boundary never ran."""
    preflight = prev_run.get("environment_preflight")
    blocked_manager_boundary = (
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
    checkpoint_name = snapshot.get("current_checkpoint_name")
    checkpoint_index = snapshot.get("current_checkpoint_index")
    snapshot_values = (
        snapshot.get("unchecked_checkpoint_count"),
        snapshot.get("current_checkpoint_unchecked_step_count"),
        snapshot.get("total_checkpoint_count", 0),
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
        isinstance(value, int) and not isinstance(value, bool)
        for value in snapshot_values
    ) or not isinstance(snapshot.get("is_complete"), bool):
        return None
    return PendingFinalizedTurn(
        source_run_dir=run_dir,
        turn_number=active_turn,
        step_name=step_name,
        step_role=step_role,
        selector=selector,
        active_plan_path=Path(active_plan_path),
        new_plan_path=Path(new_plan_path),
        snapshot_after=PlanSnapshot(
            current_checkpoint_name=checkpoint_name,
            unchecked_checkpoint_count=snapshot_values[0],
            current_checkpoint_unchecked_step_count=snapshot_values[1],
            is_complete=snapshot["is_complete"],
            total_checkpoint_count=snapshot_values[2],
            current_checkpoint_index=checkpoint_index,
        ),
        conditions={key: bool(value) for key, value in condition_values.items()},
        chosen_transition=chosen_transition,
        chosen_transition_condition=chosen_condition,
    )


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
) -> ResumeContext | None:
    """Decode all durable resume state from one already-loaded run payload."""
    run_id = resolved_run_id.name
    raw_feature_branch = prev_run.get("feature_branch")
    raw_worktree_path = prev_run.get("worktree_path")
    raw_main_branch = prev_run.get("main_branch")
    lifecycle_setup = prev_run.get("lifecycle_setup", [])
    lifecycle_teardown = prev_run.get("lifecycle_teardown", [])
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
    if "branch" in lifecycle_setup and (
        feature_branch is None or main_branch is None
    ):
        if require_resume:
            raise ValueError(
                f"error: run '{run_id}' is missing branch resume metadata."
            )
        return None
    if "worktree" in lifecycle_setup and worktree_path is None:
        if require_resume:
            raise ValueError(
                f"error: run '{run_id}' is missing worktree resume metadata."
            )
        return None

    active_plan_path = prev_run.get("active_plan_path")
    terminal_integration_only = _is_terminal_integration_resume(prev_run)

    pending_finalized_turn = (
        None
        if reset_scope
        else _pending_finalized_resume_turn(run_dir, prev_run)
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
    active_scope = manager_fields.get("active_implementation_scope")
    if (
        not reset_scope
        and active_scope is not None
        and hasattr(active_scope, "envelope_artifact_path")
    ):
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
        if scope_envelope_bytes is not None:
            scope_envelope_source_path = str(
                run_dir / active_scope.envelope_artifact_path
            )
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

    recovered_active_plan = (
        str(plan_path)
        if terminal_integration_only
        else active_plan_path
    )
    if (
        pending_finalized_turn is not None
        and pending_finalized_turn.conditions["NEW_PLAN_EXISTS"]
    ):
        recovered_active_plan = str(pending_finalized_turn.new_plan_path)

    override_value = prev_run.get("override_result")
    override_resolution = resolve_resume_override(
        run_dir,
        override_value if isinstance(override_value, Mapping) else None,
    )
    pending_override_notes = prev_run.get("pending_override_notes")
    if not isinstance(pending_override_notes, list) or not all(
        isinstance(note, str) for note in pending_override_notes
    ):
        pending_override_notes = []
    effective_max_turns = prev_run.get("effective_max_turns")
    if not isinstance(effective_max_turns, int) or isinstance(effective_max_turns, bool) or effective_max_turns < 1:
        effective_max_turns = None
    try:
        hotplug_fields = hotplug_resume_fields(prev_run)
    except (TypeError, ValueError, KeyError) as exc:
        if require_resume:
            raise ValueError(f"error: run '{run_id}' has invalid hotplug state: {exc}") from exc
        return None

    return ResumeContext(
        resumed_from_run_id=run_id,
        feature_branch=feature_branch,
        worktree_path=Path(worktree_path) if worktree_path is not None else None,
        main_branch=main_branch,
        setup=tuple(lifecycle_setup),
        teardown=tuple(lifecycle_teardown),
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
            else (
                str(prev_run["current_step_name"])
                if terminal_integration_only
                and isinstance(prev_run.get("current_step_name"), str)
                else _interrupted_resume_step(run_dir, prev_run)
            )
        ),
        pending_finalized_turn=pending_finalized_turn,
        frozen_run_identity=frozen_run_identity,
        override_result=override_resolution.override_result,
        effective_max_turns=effective_max_turns,
        pending_override_notes=tuple(pending_override_notes),
        override_source_run_dir=override_resolution.source_run_dir,
        override_file_present=override_resolution.file_present,
        terminal_integration_only=terminal_integration_only,
        scope_envelope_bytes=scope_envelope_bytes,
        scope_envelope_source_path=scope_envelope_source_path,
        repartition_artifact_bytes=repartition_artifact_bytes,
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
        except ValueError:
            if require_resume:
                raise
            return None

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
        "--resume-reset-scope",
        action="store_true",
        help=(
            "With an explicit --resume RUN_ID, reuse its worktree and manager history "
            "but restart from the invocation's original plan and fresh checkpoint scope."
        ),
    )
    run_parser.add_argument("run_args", nargs=argparse.REMAINDER)

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


def _print_renderable(renderable: object) -> None:
    try:
        from rich.console import Console
    except ImportError:
        print(renderable)
        return
    Console(file=sys.stdout).print(renderable)


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


def run_install_skills(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(["install-skills"] + ([] if argv is None else argv))
    try:
        only_skills = _deduplicate_preserve_order(tuple(args.only)) if args.only else None
        install_skills(
            destination=args.destination,
            yes=args.yes,
            only_skills=only_skills,
            include_optional=args.include_optional,
        )
    except InstallerError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(tokens)

    if args.command == "install-skills":
        try:
            only_skills = _deduplicate_preserve_order(tuple(args.only)) if args.only else None
            install_skills(
                destination=args.destination,
                yes=args.yes,
                only_skills=only_skills,
                include_optional=args.include_optional,
            )
        except InstallerError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

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

    config_path: Path | None = None
    if args.command in (None, "run", "show"):
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

        renderable = build_workflow_show(
            config=workflow_config,
            workflow_name=workflow_name,
        )
        if renderable is not None:
            _print_renderable(renderable)
        return 0

    if args.command != "run":
        parser.print_help(sys.stderr)
        return 1

    repo_root = _resolve_repo_root()
    if repo_root is None:
        return 1
    working_dir = Path.cwd()

    if config_path is None:
        config_path = bootstrap_config()

    try:
        workflow_config = load_workflow_config(config_path)
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

    requested_resume_run_id: str | None = None
    require_resume = False
    if args.resume is not None:
        require_resume = True
        if args.resume != "AUTO":
            requested_resume_run_id = args.resume

    if not require_resume and plan_file_arg is None:
        print("error: plan_file is required", file=sys.stderr)
        return 1

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
                config_path=config_path,
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
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        workflow_arg = resume_bootstrap.workflow_name
        plan_file_arg = str(resume_bootstrap.plan_path)
        startup_start_step = resume_bootstrap.start_step
        startup_max_turns = resume_bootstrap.max_turns
        startup_team = resume_bootstrap.team
        extra_instructions = resume_bootstrap.extra_instructions
    else:
        startup_start_step = args.start_step
        startup_max_turns = args.max_turns
        startup_team = args.team

    if plan_file_arg is None:
        print("error: plan_file is required", file=sys.stderr)
        return 1

    plan_path = Path(plan_file_arg).expanduser().resolve()
    startup_workflow_name = workflow_arg or workflow_config.aflow.default_workflow

    startup_request = StartupRequest(
        repo_root=repo_root,
        plan_path=plan_path,
        config_path=config_path,
        workflow_config=workflow_config,
        workflow_name=startup_workflow_name,
        start_step=startup_start_step,
        max_turns=startup_max_turns,
        team=startup_team,
        extra_instructions=extra_instructions,
        resume_requested=require_resume,
        reserved_run_id=args.run_id,
    )

    prepared_run = _handle_startup_questions(startup_request)
    if prepared_run is None:
        return 1

    try:
        resume_ctx = _detect_resume_candidate(
            repo_root=prepared_run.repo_root,
            workflow_config=workflow_config.workflows[prepared_run.workflow_name],
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

    if question.kind == StartupQuestionKind.CONFIRM_BASE_HEAD_REFRESH:
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

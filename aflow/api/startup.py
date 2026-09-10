"""Startup preparation library functions."""

from __future__ import annotations

import subprocess
from pathlib import Path
from dataclasses import replace

from aflow.git_status import (
    WorktreeInspectionError,
    WorktreePreflight,
    classify_status_items_by_prefix,
    preflight_worktree,
    probe_worktree,
    probe_repo_state,
)
from aflow.plan import (
    PlanParseError,
    load_plan,
    load_plan_tolerant,
    parse_git_tracking_metadata,
)
from aflow.run_state import RetryContext
from aflow.workflow import (
    _effective_retry_limit,
    _lifecycle_is_bootstrap_eligible,
    generate_new_plan_path,
    preflight_pre_handoff_base_head_refresh,
    render_step_prompts,
    resolve_role_selector,
    resolve_profile,
    StartupBaseHeadRefreshStatus,
)

from .models import (
    PreparedRun,
    StartupQuestion,
    StartupQuestionKind,
    StartupRequest,
)


class StartupError(Exception):
    """Error during startup preparation."""

    pass


_DEFAULT_PROBE_WORKTREE = probe_worktree


def _resolve_start_step(raw_start_step: str | None, workflow_name: str, request: StartupRequest) -> str | None:
    """Resolve start step from raw value (numeric index or step name).

    If raw_start_step is a plain ASCII base-10 integer (only digits 0-9), treat it as a 1-based
    workflow step index. Otherwise, treat it as a step name.

    Returns the resolved step name or None if raw_start_step is None.
    Raises StartupError if the step is invalid or out of range.
    """
    if raw_start_step is None:
        return None

    workflow = request.workflow_config.workflows[workflow_name]
    step_names = list(workflow.steps.keys())

    if not step_names:
        raise StartupError(
            f"Workflow '{workflow_name}' has no steps defined"
        )

    raw_str = str(raw_start_step)

    is_ascii_decimal = raw_str and all(c in '0123456789' for c in raw_str)
    if is_ascii_decimal:
        index = int(raw_str)

        if index < 1 or index > len(step_names):
            available = ", ".join(step_names)
            raise StartupError(
                f"Start-step index {index} is out of range for workflow '{workflow_name}'. "
                f"Valid indexes: 1 to {len(step_names)}. "
                f"Available steps: {available}"
            )

        return step_names[index - 1]

    return raw_str


def _resolve_workflow_name(request: StartupRequest) -> str:
    """Resolve workflow name from request or config default.

    Raises StartupError if no workflow can be determined.
    """
    workflow_name = request.workflow_name or request.workflow_config.aflow.default_workflow
    if workflow_name is None:
        raise StartupError(
            "No workflow specified and no default_workflow set in config."
        )
    if workflow_name not in request.workflow_config.workflows:
        raise StartupError(f"Workflow '{workflow_name}' not found in config.")
    return workflow_name


def _validate_start_step(workflow_name: str, start_step: str | None, request: StartupRequest) -> None:
    """Validate and resolve start_step (if provided).

    Resolves numeric indices to step names and validates that the step exists in the workflow.

    Raises StartupError if start_step is invalid.
    """
    if start_step is None:
        return
    resolved_step = _resolve_start_step(start_step, workflow_name, request)
    workflow = request.workflow_config.workflows[workflow_name]
    if resolved_step not in workflow.steps:
        raise StartupError(
            f"Step '{resolved_step}' not found in workflow '{workflow_name}'. "
            f"Available steps: {', '.join(workflow.steps.keys())}"
        )


def _load_plan_with_recovery(
    plan_path: Path,
) -> tuple[object, str | None]:
    """Load plan, returning (parsed_plan, startup_retry_error).

    startup_retry_error is non-None if recovery was attempted for inconsistent state.
    Raises StartupError if plan cannot be loaded without interactive recovery.
    """
    try:
        parsed_plan = load_plan(plan_path)
    except PlanParseError as exc:
        if exc.error_kind != "inconsistent_checkpoint_state":
            raise StartupError(str(exc))
        raise StartupError(f"inconsistent_checkpoint_state:{exc}")
    except FileNotFoundError as exc:
        raise StartupError(str(exc))
    return parsed_plan, None


def _plan_needs_step_selection(
    workflow_name: str,
    parsed_plan: object,
    request: StartupRequest,
) -> bool:
    """Check if the startup flow must prompt for step selection."""
    workflow = request.workflow_config.workflows[workflow_name]
    if len(workflow.steps) <= 1:
        return False
    if hasattr(parsed_plan, "snapshot") and hasattr(parsed_plan.snapshot, "is_complete"):
        if parsed_plan.snapshot.is_complete:
            return False
    if hasattr(parsed_plan, "sections"):
        has_completed_checkpoint = any(
            getattr(section, "heading_checked", False) for section in parsed_plan.sections
        )
        return has_completed_checkpoint
    return False


def _resolve_effective_max_turns(request: StartupRequest, workflow_name: str) -> int:
    """Resolve effective max_turns from request or workflow config."""
    if request.max_turns is not None:
        return request.max_turns
    return request.workflow_config.aflow.max_turns


def _resolve_effective_team(request: StartupRequest, workflow_name: str) -> str | None:
    """Resolve effective team from request or workflow config."""
    workflow = request.workflow_config.workflows[workflow_name]
    team = request.team if request.team is not None else workflow.team
    team_config = request.workflow_config.teams.get(team) if team is not None else None
    if team is not None and team_config is None:
        known_teams = ", ".join(sorted(request.workflow_config.teams)) or "none"
        raise StartupError(
            f"Workflow '{workflow_name}' references unknown team '{team}'. "
            f"Known teams: {known_teams}"
        )
    return team


def _check_plan_completion(parsed_plan: object, request: StartupRequest) -> tuple[bool, bool]:
    """Check plan completion status.

    Returns (is_complete, has_completed_checkpoint).
    """
    is_complete = False
    has_completed_checkpoint = False
    if hasattr(parsed_plan, "snapshot"):
        is_complete = getattr(parsed_plan.snapshot, "is_complete", False)
    if hasattr(parsed_plan, "sections"):
        has_completed_checkpoint = any(
            getattr(section, "heading_checked", False) for section in parsed_plan.sections
        )
    return is_complete, has_completed_checkpoint


def _validate_current_branch_continuation(
    request: StartupRequest,
    parsed_plan: object,
    *,
    workflow_name: str,
    has_completed_checkpoint: bool,
) -> tuple[str, str]:
    """Validate the exact branch and HEAD boundary for continuation."""
    workflow = request.workflow_config.workflows[workflow_name]
    if (
        tuple(workflow.setup or ()) != ("worktree", "branch")
        or tuple(workflow.teardown or ()) != ("merge", "rm_worktree")
    ):
        raise StartupError(
            "current-branch continuation requires lifecycle setup "
            "[worktree, branch] and teardown [merge, rm_worktree]"
        )

    branch_result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(request.repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or not branch:
        raise StartupError(
            "current-branch continuation requires a symbolic current branch; "
            "detached HEAD is not accepted"
        )

    head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(request.repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    current_head = head_result.stdout.strip()
    if head_result.returncode != 0 or not current_head:
        raise StartupError(
            "current-branch continuation requires a resolvable full current HEAD"
        )

    git_dir_result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(request.repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if git_dir_result.returncode == 0:
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = request.repo_root / git_dir
        in_progress_markers = (
            "MERGE_HEAD",
            "REBASE_HEAD",
            "rebase-merge",
            "rebase-apply",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "sequencer",
        )
        active_marker = next(
            (marker for marker in in_progress_markers if (git_dir / marker).exists()),
            None,
        )
        if active_marker is not None:
            raise StartupError(
                "current-branch continuation requires no in-progress Git operation "
                f"({active_marker} exists)"
            )

    try:
        metadata = parse_git_tracking_metadata(
            request.plan_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise StartupError(str(exc)) from exc
    if metadata is None or not metadata.plan_branch:
        raise StartupError(
            "current-branch continuation requires non-empty Git Tracking Plan Branch metadata"
        )
    if metadata.plan_branch != branch:
        raise StartupError(
            "current-branch continuation Plan Branch mismatch: "
            f"plan records '{metadata.plan_branch}', current branch is '{branch}'"
        )
    if metadata.pre_handoff_base_head != current_head:
        raise StartupError(
            "current-branch continuation Pre-Handoff Base HEAD must equal the "
            f"full current HEAD '{current_head}'"
        )

    snapshot = getattr(parsed_plan, "snapshot", None)
    unchecked_checkpoint_count = getattr(snapshot, "unchecked_checkpoint_count", 0)
    if (
        not has_completed_checkpoint
        or getattr(snapshot, "is_complete", False)
        or not isinstance(unchecked_checkpoint_count, int)
        or unchecked_checkpoint_count < 1
    ):
        raise StartupError(
            "current-branch continuation requires at least one completed and one unchecked checkpoint"
        )
    return branch, current_head


def _build_retry_context(
    workflow_name: str,
    selected_start_step: str,
    startup_retry_error: str,
    parsed_plan: object,
    request: StartupRequest,
    effective_team: str | None,
    config_path: Path,
) -> RetryContext:
    """Build RetryContext for startup recovery."""
    workflow = request.workflow_config.workflows[workflow_name]
    step = workflow.steps[selected_start_step]
    step_path = f"workflow.{workflow_name}.steps.{selected_start_step}"

    selector = resolve_role_selector(
        step.role,
        effective_team,
        request.workflow_config,
        step_path=step_path,
    )
    resolved = resolve_profile(selector, request.workflow_config, step_path=step_path)

    checkpoint_index = getattr(
        getattr(parsed_plan, "snapshot", None),
        "current_checkpoint_index",
        None,
    ) or 1
    new_plan_path = generate_new_plan_path(request.plan_path, checkpoint_index=checkpoint_index)

    base_user_prompt = render_step_prompts(
        step,
        request.workflow_config,
        config_dir=config_path.parent,
        working_dir=Path.cwd(),
        original_plan_path=request.plan_path,
        new_plan_path=new_plan_path,
        active_plan_path=request.plan_path,
    )

    return RetryContext(
        step_name=selected_start_step,
        step_role=step.role,
        resolved_selector=selector,
        resolved_harness_name=resolved.harness_name,
        resolved_model=resolved.model,
        resolved_effort=resolved.effort,
        snapshot_before=getattr(parsed_plan, "snapshot"),
        active_plan_path=request.plan_path,
        new_plan_path=new_plan_path,
        base_user_prompt=base_user_prompt,
        parse_error_str=startup_retry_error,
        attempt=1,
        retry_limit=_effective_retry_limit(workflow, request.workflow_config.aflow),
    )


def _check_worktree_dirtiness(
    request: StartupRequest,
    workflow_name: str,
    *,
    reject_blockers: bool = True,
) -> WorktreePreflight:
    """Run the shared read-only startup worktree preflight.

    Normal startup rejects blockers before asking the existing confirmation
    question.  Remote read-only callers can set ``reject_blockers=False`` to
    inspect that same typed result without changing the launch contract.
    """
    workflow = request.workflow_config.workflows[workflow_name]
    execution_mode = (
        "new_worktree"
        if workflow.setup and "worktree" in workflow.setup
        else "same_checkout"
    )
    # Keep the established injectable probe seam available to callers and
    # tests that deliberately provide synthetic dirtiness.  The normal path
    # below uses only the shared typed preflight and therefore performs one
    # status inspection.
    if not workflow.setup and probe_worktree is not _DEFAULT_PROBE_WORKTREE:
        legacy_probe = probe_worktree(request.repo_root)
        dirty = bool(getattr(legacy_probe, "is_dirty", False))
        return WorktreePreflight(
            checkout_path=Path(request.repo_root).resolve(),
            execution_mode=execution_mode,
            dirty=dirty,
            requires_confirmation=dirty,
            blockers=(),
            total_items=(1 if dirty else 0),
            items=(),
        )

    # Lifecycle bootstrap owns the initial files in a supported non-Git
    # directory (or an unborn repository).  Defer strict status inspection
    # until bootstrap has created a commit; the existing lifecycle preflight
    # then performs the Git-dependent check before setup proceeds.
    repo_state = probe_repo_state(request.repo_root)
    if _lifecycle_is_bootstrap_eligible(workflow, repo_state):
        return WorktreePreflight(
            checkout_path=Path(request.repo_root).resolve(),
            execution_mode=execution_mode,
            dirty=False,
            requires_confirmation=False,
            blockers=(),
            total_items=0,
            items=(),
        )

    try:
        result = preflight_worktree(
            request.repo_root,
            execution_mode=execution_mode,
        )
    except WorktreeInspectionError as exc:
        # Non-lifecycle workflows have historically been usable from a
        # directory that is not a Git checkout.  Keep that narrow compatibility
        # path, while the shared preflight remains strict for real checkouts
        # and all other inspection failures.
        if not workflow.setup and "not a git repository" in str(exc).lower():
            legacy_probe = probe_worktree(request.repo_root)
            if legacy_probe is None:
                return WorktreePreflight(
                    checkout_path=Path(request.repo_root).resolve(),
                    execution_mode=execution_mode,
                    dirty=False,
                    requires_confirmation=False,
                    blockers=(),
                    total_items=0,
                    items=(),
                )
            dirty = bool(getattr(legacy_probe, "is_dirty", False))
            return WorktreePreflight(
                checkout_path=Path(request.repo_root).resolve(),
                execution_mode=execution_mode,
                dirty=dirty,
                requires_confirmation=dirty,
                blockers=(),
                total_items=(1 if dirty else 0),
                items=(),
            )
        raise StartupError(f"worktree preflight inspection failed: {exc}") from exc

    if reject_blockers and result.blockers:
        raise StartupError(
            "worktree preflight blocked startup: " + "; ".join(result.blockers)
        )
    if reject_blockers and request.continue_from_current and result.requires_confirmation:
        _, non_plan_paths = classify_status_items_by_prefix(
            result.items,
            ignore_lifecycle_owned=True,
        )
        raise StartupError(
            "current-branch continuation has non-plan dirtiness; "
            f"untracked or uncommitted paths outside plans/: "
            f"{', '.join(non_plan_paths[:3])}{'...' if len(non_plan_paths) > 3 else ''}"
        )
    return result


def _preflight_startup_base_head_refresh(
    request: StartupRequest,
    parsed_plan: object,
) -> object:
    plan_text = request.plan_path.read_text(encoding="utf-8")
    try:
        return preflight_pre_handoff_base_head_refresh(
            request.repo_root,
            plan_text,
            parsed_plan,
        )
    except ValueError as exc:
        raise StartupError(str(exc)) from exc


def prepare_startup(request: StartupRequest) -> PreparedRun | StartupQuestion:
    """Prepare workflow startup, returning either a prepared run or a question.

    This function processes startup decisions and returns either:
    - PreparedRun: all startup decisions are made and run is ready to execute
    - StartupQuestion: a structured question that requires user input

    The caller should answer the question and call prepare_startup_with_answer().

    Raises StartupError if startup cannot proceed due to configuration errors.
    """
    if request.continue_from_current and request.resume_requested:
        raise StartupError(
            "current-branch continuation cannot be combined with resume"
        )

    workflow_name = _resolve_workflow_name(request)
    _validate_start_step(workflow_name, request.start_step, request)

    resolved_start_step = _resolve_start_step(request.start_step, workflow_name, request)

    if request.pre_recovered_plan is not None:
        parsed_plan = request.pre_recovered_plan
        startup_retry_error = request.startup_retry_error
    else:
        try:
            parsed_plan, startup_retry_error = _load_plan_with_recovery(request.plan_path)
        except StartupError as exc:
            if str(exc).startswith("inconsistent_checkpoint_state:"):
                recovery_msg = str(exc).replace("inconsistent_checkpoint_state:", "")
                return StartupQuestion(
                    kind=StartupQuestionKind.CONFIRM_RECOVERY,
                    message=recovery_msg,
                    continuation_request=request,
                )
            raise

    is_complete, has_completed_checkpoint = _check_plan_completion(parsed_plan, request)
    effective_max_turns = _resolve_effective_max_turns(request, workflow_name)
    effective_team = _resolve_effective_team(request, workflow_name)

    continuation_from_branch: str | None = None
    continuation_from_head: str | None = None
    if request.continue_from_current:
        (
            continuation_from_branch,
            continuation_from_head,
        ) = _validate_current_branch_continuation(
            request,
            parsed_plan,
            workflow_name=workflow_name,
            has_completed_checkpoint=has_completed_checkpoint,
        )

    if is_complete:
        if resolved_start_step is not None and not request.resume_requested:
            raise StartupError("plan is already complete, --start-step has no effect")
        selected_start_step = (
            resolved_start_step
            or request.workflow_config.workflows[workflow_name].first_step
        )
    else:
        if resolved_start_step is not None:
            selected_start_step = resolved_start_step
        elif _plan_needs_step_selection(workflow_name, parsed_plan, request):
            workflow = request.workflow_config.workflows[workflow_name]
            step_names = list(workflow.steps.keys())
            return StartupQuestion(
                kind=StartupQuestionKind.PICK_STEP,
                message="Select the workflow step to start from:",
                choices=step_names,
                continuation_request=request,
            )
        else:
            selected_start_step = request.workflow_config.workflows[workflow_name].first_step

    startup_retry = None
    if startup_retry_error is not None:
        if selected_start_step is None:
            raise StartupError(
                f"Workflow '{workflow_name}' has no steps"
            )
        startup_retry = _build_retry_context(
            workflow_name,
            selected_start_step,
            startup_retry_error,
            parsed_plan,
            request,
            effective_team,
            request.config_path,
        )

    effective_startup_base_head_refresh_sha = None
    if not request.resume_requested:
        startup_base_head_refresh = _preflight_startup_base_head_refresh(
            request,
            parsed_plan,
        )
        if startup_base_head_refresh.status in {
            StartupBaseHeadRefreshStatus.MALFORMED,
            StartupBaseHeadRefreshStatus.EMPTY_BASE_STARTED,
            StartupBaseHeadRefreshStatus.MISMATCH_STARTED,
        }:
            raise StartupError(
                f"startup preflight rejected Pre-Handoff Base HEAD state: "
                f"{startup_base_head_refresh.status.value}"
            )

        effective_startup_base_head_refresh_sha = (
            request.startup_base_head_refresh_sha
        )
        if startup_base_head_refresh.status in {
            StartupBaseHeadRefreshStatus.EMPTY_BASE_PRISTINE,
            StartupBaseHeadRefreshStatus.MISMATCH_PRISTINE,
        } and effective_startup_base_head_refresh_sha is None:
            effective_startup_base_head_refresh_sha = (
                startup_base_head_refresh.current_head
            )
        if (
            startup_base_head_refresh.status
            in {
                StartupBaseHeadRefreshStatus.EMPTY_BASE_PRISTINE,
                StartupBaseHeadRefreshStatus.MISMATCH_PRISTINE,
            }
            and effective_startup_base_head_refresh_sha is not None
            and startup_base_head_refresh.current_head
            != effective_startup_base_head_refresh_sha
        ):
            raise StartupError(
                "startup refresh target does not match current HEAD for "
                "Pre-Handoff Base HEAD refresh"
            )

    worktree_preflight = _check_worktree_dirtiness(request, workflow_name)
    if worktree_preflight.requires_confirmation and not request.dirty_worktree_confirmed:
        dirty_paths = tuple(item.path for item in worktree_preflight.items)
        dirty_desc = (
            f"{worktree_preflight.total_items} changed path(s)"
            + (f": {', '.join(dirty_paths[:3])}{'...' if len(dirty_paths) > 3 else ''}" if dirty_paths else "")
        )
        return StartupQuestion(
            kind=StartupQuestionKind.CONFIRM_WORKTREE_DIRTY,
            message=f"Worktree is dirty ({dirty_desc}). Start anyway?",
            continuation_request=request,
        )

    is_complete_plan = (
        hasattr(parsed_plan, "snapshot")
        and getattr(parsed_plan.snapshot, "is_complete", False)
    )
    executable_steps = tuple(
        request.workflow_config.workflows[workflow_name].steps
    )
    skipped_steps = (
        ()
        if request.resume_requested
        else executable_steps[: executable_steps.index(selected_start_step)]
    )

    return PreparedRun(
        workflow_name=workflow_name,
        repo_root=request.repo_root,
        plan_path=request.plan_path,
        config_path=request.config_path,
        max_turns=effective_max_turns,
        team=effective_team,
        extra_instructions=request.extra_instructions,
        start_step=selected_start_step,
        dirty_worktree_confirmed=request.dirty_worktree_confirmed,
        startup_retry=startup_retry,
        startup_base_head_refresh_sha=effective_startup_base_head_refresh_sha,
        move_completed_plan_to_done=is_complete_plan,
        parsed_plan=parsed_plan,
        reserved_run_id=request.reserved_run_id,
        idempotency_key=request.idempotency_key,
        caller_scope=request.caller_scope,
        continuation_from_branch=continuation_from_branch,
        continuation_from_head=continuation_from_head,
        continuation_mode=(
            "current_branch" if request.continue_from_current else None
        ),
        restarted_from_run_id=request.restarted_from_run_id,
        skipped_steps=skipped_steps,
        team_explicit=(
            request.team_explicit
            if request.team_explicit is not None
            else request.team is not None
        ),
        max_turns_explicit=(
            request.max_turns_explicit
            if request.max_turns_explicit is not None
            else request.max_turns is not None
        ),
        start_step_explicit=(
            request.start_step_explicit
            if request.start_step_explicit is not None
            else request.start_step is not None
        ),
    )


def prepare_startup_with_answer(
    question: StartupQuestion,
    request: StartupRequest,
    answer: str | int | bool,
) -> PreparedRun | StartupQuestion:
    """Resume startup after answering a question.

    Takes a previous StartupQuestion and an answer, then returns either:
    - PreparedRun: ready to execute
    - StartupQuestion: another question (rare, but possible)

    The continuation_request from the question should be used for subsequent calls.

    Raises StartupError if the answer is invalid or startup cannot proceed.
    """
    effective_request = question.continuation_request or request

    if question.kind == StartupQuestionKind.CONFIRM_RECOVERY:
        if not isinstance(answer, bool) or not answer:
            raise StartupError("Startup recovery declined")

        try:
            tolerant_result = load_plan_tolerant(effective_request.plan_path)
        except FileNotFoundError as exc:
            raise StartupError(str(exc))

        parsed_plan = tolerant_result.parsed_plan
        if tolerant_result.parse_error:
            startup_retry_error = str(tolerant_result.parse_error)
        else:
            startup_retry_error = None

        new_request = replace(
            effective_request,
            pre_recovered_plan=parsed_plan,
            startup_retry_error=startup_retry_error,
        )
        result = prepare_startup(new_request)
        if isinstance(result, StartupQuestion):
            return result.__class__(
                kind=result.kind,
                message=result.message,
                options=result.options,
                choices=result.choices,
                continuation_request=new_request,
            )
        return result

    if question.kind == StartupQuestionKind.PICK_STEP:
        if isinstance(answer, int):
            if 0 <= answer < len(question.choices):
                selected_step = question.choices[answer]
            else:
                raise StartupError(f"Invalid step choice: {answer}")
        elif isinstance(answer, str):
            if answer in question.choices:
                selected_step = answer
            else:
                raise StartupError(f"Step '{answer}' not in choices: {question.choices}")
        else:
            raise StartupError(f"Invalid step answer type: {type(answer)}")

        new_request = replace(
            effective_request,
            start_step=selected_step,
            start_step_explicit=True,
        )
        result = prepare_startup(new_request)
        if isinstance(result, StartupQuestion):
            return result.__class__(
                kind=result.kind,
                message=result.message,
                options=result.options,
                choices=result.choices,
                continuation_request=new_request,
            )
        return result

    if question.kind == StartupQuestionKind.CONFIRM_WORKTREE_DIRTY:
        if not isinstance(answer, bool) or not answer:
            raise StartupError("Startup aborted due to dirty worktree")
        new_request = replace(
            effective_request,
            dirty_worktree_confirmed=True,
        )
        result = prepare_startup(new_request)
        if isinstance(result, StartupQuestion):
            return result.__class__(
                kind=result.kind,
                message=result.message,
                options=result.options,
                choices=result.choices,
                continuation_request=new_request,
            )
        return result

    raise StartupError(f"Unknown question kind: {question.kind}")

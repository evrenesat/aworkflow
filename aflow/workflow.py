from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

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
from .git_status import classify_dirtiness_by_prefix, RepoState, probe_repo_state
from .harnesses import get_adapter
from .harnesses.base import HarnessAdapter, HarnessInvocation
from .plan import (
    FENCE_RE,
    ParsedPlan,
    PlanParseError,
    PlanSnapshot,
    is_handoff_pristine_for_base_refresh,
    load_plan,
    load_plan_tolerant,
    parse_git_tracking_metadata,
    plan_has_git_tracking,
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
from .run_state import ControllerConfig, ControllerRunResult, ControllerState, ExecutionContext, HarnessRecoveryAction, HarnessRecoveryContext, IssueRecord, RetryContext, ResumeContext, TurnRecord, WorkflowEndReason, format_harness_model_display
from .runlog import create_run_paths, finalize_turn_artifacts, prune_old_runs, write_issue_summary, write_run_metadata, write_turn_artifacts_start
from .status import BannerRenderer, WorkflowGraphSource
from aflow.api.events import (
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


def _turn_artifact_display_path(repo_root: Path, turn_dir: Path, filename: str) -> str | None:
    artifact_path = turn_dir / filename
    if not artifact_path.is_file():
        return None
    if not artifact_path.read_text(encoding="utf-8").strip():
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


def _run_process(
    invocation: HarnessInvocation,
    repo_root: Path,
    banner: BannerRenderer,
    state: ControllerState,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        list(invocation.argv),
        cwd=str(repo_root),
        env={**os.environ, **invocation.env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

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

    while True:
        try:
            proc.wait(timeout=PROCESS_POLL_INTERVAL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass

    t_out.join()
    t_err.join()

    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode or 0,
        "".join(stdout_chunks),
        "".join(stderr_chunks),
    )


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


_STOP_SENTINEL_PREFIX = "AFLOW_STOP:"
_STOP_SENTINEL_FALLBACK_REASON = "implementer requested stop without a reason"
_STOP_SENTINEL_PLACEHOLDER_REASON = "<reason>"


def _iter_non_fenced_lines(text: str):
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


def _detect_stop_marker(stdout: str, stderr: str) -> str | None:
    for text in (stdout, stderr):
        for line in _iter_non_fenced_lines(text):
            if line.startswith(_STOP_SENTINEL_PREFIX):
                reason = line[len(_STOP_SENTINEL_PREFIX):].strip()
                if reason == _STOP_SENTINEL_PLACEHOLDER_REASON:
                    continue
                return reason or _STOP_SENTINEL_FALLBACK_REASON
    return None


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
    if expected_new_plan_path.is_file():
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
    return runner(
        list(invocation.argv),
        cwd=str(primary_root),
        env={**os.environ, **invocation.env},
        capture_output=True,
        text=True,
        check=False,
    )


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
        completed = runner(
            list(invocation.argv),
            cwd=str(repo_root),
            env={**os.environ, **invocation.env},
            capture_output=True,
            text=True,
            check=False,
        )
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
    return runner(
        list(invocation.argv),
        cwd=str(primary_root),
        env={**os.environ, **invocation.env},
        capture_output=True,
        text=True,
        check=False,
    )


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
    current_step_name = config.start_step or wf.first_step
    working_dir = working_dir or Path.cwd()

    run_paths = create_run_paths(config)
    state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    state.run_id = run_paths.run_dir.name
    state.resumed_from_run_id = resumed_from_run_id
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
    if done:
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

    execution_repo_root = exec_ctx.execution_repo_root if exec_ctx else config.repo_root

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
    ) -> None:
        record = state.turn_history[-1]
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
            status=status,
            started_at=started_at,
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
        )
        record.turn_dir = turn_dir
        record.stdout_artifact_path = _turn_artifact_display_path(run_paths.repo_root, turn_dir, "stdout.txt")
        record.stderr_artifact_path = _turn_artifact_display_path(run_paths.repo_root, turn_dir, "stderr.txt")
        record.outcome = "completed" if status in {"running", "completed"} else status
        record.finished_at = datetime.now(timezone.utc)
        record.duration_seconds = (record.finished_at - record.started_at).total_seconds()

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

    for turn_number in range(1, config.max_turns + 1):
        retry_ctx = state.pending_retry
        active_team_name = (
            state.current_team_override
            if state.current_team_override is not None
            else state.current_team
        )
        followup_candidates_before: set[Path] = set()

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
            selector, resolved = _resolve_step_runtime(
                step,
                workflow_config,
                team_name=active_team_name,
                step_path=step_path,
            )
            step_adapter = adapter or get_adapter(resolved.harness_name)
            snapshot_before = retry_ctx.snapshot_before
            try:
                user_prompt = retry_ctx.base_user_prompt + "\n\n" + _build_retry_appendix(retry_ctx.parse_error_str)
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

            new_plan_path = generate_new_plan_path(
                original_plan_path,
                checkpoint_index=checkpoint_index,
            )

            step = wf.steps[current_step_name]
            step_path = f"workflow.{workflow_name}.steps.{current_step_name}"
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

        if use_popen:
            completed = _run_process(invocation, execution_repo_root, banner, state)
        else:
            assert runner is not None
            completed = runner(
                list(invocation.argv),
                cwd=str(execution_repo_root),
                env={**os.environ, **invocation.env},
                capture_output=True,
                text=True,
                check=False,
            )

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
            summary = _format_failure(
                reason=f"workflow stopped by explicit AFLOW_STOP marker: {stop_reason}",
                run_dir=run_paths.run_dir,
                snapshot=snapshot_before,
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

            if is_retryable and current_attempt <= retry_limit and turn_number < config.max_turns:
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
                    conditions={"DONE": done, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": turn_number >= config.max_turns},
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
                conditions={"DONE": done, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": turn_number >= config.max_turns},
            )
            summary = _format_failure(
                reason=str(exc),
                run_dir=run_paths.run_dir,
                snapshot=snapshot_before,
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
                conditions={"DONE": post_snapshot.is_complete, "NEW_PLAN_EXISTS": False, "MAX_TURNS_REACHED": turn_number >= config.max_turns},
            )
            summary = _format_failure(
                reason=f"harness '{invocation.label}' exited with code {completed.returncode}",
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

        state.last_snapshot = post_snapshot
        state.turns_completed += 1

        done = post_snapshot.is_complete
        new_plan_exists = _exec_plan_path(new_plan_path, exec_ctx).is_file()

        if new_plan_exists:
            active_plan_path = new_plan_path

        max_turns_reached = turn_number >= config.max_turns

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
        )

        if transition_target != "END":
            state.current_team_override = None
        if not new_plan_exists and active_plan_path != original_plan_path:
            active_plan_path = original_plan_path

        banner.set_context(
            active_plan_path=active_plan_path,
            new_plan_path=new_plan_path if new_plan_exists else None,
        )
        banner.update(state)

        write_run_metadata(
            run_paths, config, state, status="running",
            execution_context=exec_ctx,
            last_snapshot=post_snapshot,
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
                prepared_primary_plan: _PreparedPrimaryPlanForMerge | None = None
                try:
                    prepared_primary_plan = _prepare_primary_plan_for_merge(
                        config.repo_root,
                        original_plan_path,
                    )
                    _ensure_merge_handoff_clean(
                        exec_ctx,
                        original_plan_path=original_plan_path,
                    )
                    merge_completed = _execute_merge_handoff(
                        exec_ctx, wf, workflow_config,
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
                except WorkflowError as exc:
                    _restore_primary_plan_after_merge(prepared_primary_plan)
                    merge_status = "failed"
                    merge_failure_reason = exc.summary
                else:
                    stop_reason = _detect_stop_marker(merge_completed.stdout, merge_completed.stderr)
                    if stop_reason is not None:
                        _restore_primary_plan_after_merge(prepared_primary_plan)
                        merge_status = "failed"
                        merge_failure_reason = f"AFLOW_STOP: {stop_reason}"
                    elif merge_completed.returncode != 0:
                        _restore_primary_plan_after_merge(prepared_primary_plan)
                        merge_status = "failed"
                        merge_failure_reason = f"merge agent exited with code {merge_completed.returncode}"
                    else:
                        _restore_primary_plan_after_merge(prepared_primary_plan)
                        check_failure = _verify_merge_success(
                            config.repo_root,
                            exec_ctx.main_branch,
                            exec_ctx.feature_branch,
                            original_plan_path=original_plan_path,
                        )
                        if check_failure is not None:
                            merge_status = "failed"
                            merge_failure_reason = f"merge verification failed: {check_failure}"
                        else:
                            merge_status = "success"
                            if "rm_worktree" in exec_ctx.teardown and exec_ctx.worktree_path is not None:
                                try:
                                    _rm_worktree_safe(config.repo_root, exec_ctx.worktree_path)
                                except WorkflowError as exc:
                                    merge_status = "failed"
                                    merge_failure_reason = exc.summary

            if merge_status == "failed":
                state.status_message = "failed"
                summary = _format_failure(
                    reason=merge_failure_reason or "merge teardown failed",
                    run_dir=run_paths.run_dir,
                    snapshot=post_snapshot,
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

    state.status_message = "failed"
    summary = _format_failure(
        reason=f"reached max turns limit of {config.max_turns} without a transition to END",
        run_dir=run_paths.run_dir,
        snapshot=state.last_snapshot,
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

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .plan import PlanSnapshot
from .recovery import build_recovery_payload
from .run_state import (
    ControllerConfig,
    ControllerState,
    ExecutionContext,
    HarnessRecoveryContext,
    RetryContext,
    RUN_STATE_SCHEMA_VERSION,
    WorkflowEndReason,
    manager_state_payload,
    hotplug_state_payload,
)
from .harnesses.base import HarnessInvocation

SHELL_ID_ENV_VARS: tuple[str, ...] = (
    "AFLOW_SHELL_ID",
    "TERM_SESSION_ID",
    "LC_TERMINAL_SESSION_ID",
    "ITERM_SESSION_ID",
    "KITTY_WINDOW_ID",
    "KITTY_PID",
    "WEZTERM_PANE",
    "TMUX_PANE",
    "ZELLIJ_SESSION_NAME",
)
SHELL_PROCESS_NAMES = frozenset({
    "bash",
    "dash",
    "fish",
    "ksh",
    "sh",
    "zsh",
})
_SHELL_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class RunPaths:
    repo_root: Path
    runs_root: Path
    run_dir: Path
    turns_dir: Path
    manager_dir: Path
    run_json: Path


@dataclass(frozen=True)
class ManagerDecisionPaths:
    directory: Path
    context: Path
    system_prompt: Path
    user_prompt: Path
    stdout: Path
    stderr: Path
    result: Path
    boundary: Path


@dataclass(frozen=True)
class ManagerNoteCorrectionPaths:
    directory: Path
    system_prompt: Path
    user_prompt: Path
    stdout: Path
    stderr: Path
    result: Path


@dataclass(frozen=True)
class RepartitionAttemptPaths:
    directory: Path
    source_plan: Path
    envelope: Path
    evidence: Path
    propose_system_prompt: Path
    propose_user_prompt: Path
    propose_stdout: Path
    propose_stderr: Path
    proposal: Path
    candidate_plan: Path
    mechanical_validation: Path
    validate_system_prompt: Path
    validate_user_prompt: Path
    validate_stdout: Path
    validate_stderr: Path
    semantic_verdict: Path
    result: Path


def _utc_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:8]}"


def _json_dump(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(_json_dump(payload), encoding="utf-8")


def _write_atomic_json(path: Path, payload: dict[str, object]) -> None:
    """Durably replace one JSON file without exposing a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(_json_dump(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def manager_decision_paths(paths: RunPaths, decision_number: int) -> ManagerDecisionPaths:
    if decision_number < 1:
        raise ValueError("manager decision numbers start at 1")
    directory = paths.manager_dir / f"decision-{decision_number:03d}"
    return ManagerDecisionPaths(
        directory=directory,
        context=directory / "context.json",
        system_prompt=directory / "system-prompt.txt",
        user_prompt=directory / "user-prompt.txt",
        stdout=directory / "stdout.txt",
        stderr=directory / "stderr.txt",
        result=directory / "result.json",
        boundary=directory / "boundary.json",
    )


def write_manager_artifacts(
    paths: RunPaths,
    *,
    decision_number: int,
    context: Mapping[str, Any],
    system_prompt: str,
    user_prompt: str,
    stdout: str = "",
    stderr: str = "",
    result: Mapping[str, Any] | None = None,
    boundary: Mapping[str, Any] | None = None,
) -> ManagerDecisionPaths:
    """Persist exact manager inputs and outputs outside workflow turn artifacts."""
    artifact_paths = manager_decision_paths(paths, decision_number)
    artifact_paths.directory.mkdir(parents=True, exist_ok=False)
    _write_json(artifact_paths.context, dict(context))
    artifact_paths.system_prompt.write_text(system_prompt, encoding="utf-8")
    artifact_paths.user_prompt.write_text(user_prompt, encoding="utf-8")
    artifact_paths.stdout.write_text(stdout, encoding="utf-8")
    artifact_paths.stderr.write_text(stderr, encoding="utf-8")
    _write_json(artifact_paths.result, dict(result or {}))
    if boundary is not None:
        _write_json(artifact_paths.boundary, dict(boundary))
    return artifact_paths


def write_manager_note_correction_artifacts(
    paths: RunPaths,
    *,
    decision_number: int,
    system_prompt: str,
    user_prompt: str,
    stdout: str = "",
    stderr: str = "",
    result: Mapping[str, Any] | None = None,
) -> ManagerNoteCorrectionPaths:
    """Add one immutable note-correction attempt beneath an existing decision."""
    decision_directory = manager_decision_paths(paths, decision_number).directory
    if not decision_directory.is_dir():
        raise FileNotFoundError(
            f"manager decision directory does not exist: {decision_directory}"
        )
    directory = decision_directory / "note-authority-correction"
    directory.mkdir(parents=False, exist_ok=False)
    artifact_paths = ManagerNoteCorrectionPaths(
        directory=directory,
        system_prompt=directory / "system-prompt.txt",
        user_prompt=directory / "user-prompt.txt",
        stdout=directory / "stdout.txt",
        stderr=directory / "stderr.txt",
        result=directory / "result.json",
    )
    artifact_paths.system_prompt.write_text(system_prompt, encoding="utf-8")
    artifact_paths.user_prompt.write_text(user_prompt, encoding="utf-8")
    artifact_paths.stdout.write_text(stdout, encoding="utf-8")
    artifact_paths.stderr.write_text(stderr, encoding="utf-8")
    _write_json(artifact_paths.result, dict(result or {}))
    return artifact_paths


def create_repartition_attempt_paths(
    paths: RunPaths,
    *,
    decision_number: int,
    attempt_number: int,
) -> RepartitionAttemptPaths:
    if decision_number < 1 or attempt_number not in {1, 2}:
        raise ValueError("repartition attempt requires positive decision and attempt 1 or 2")
    directory = (
        manager_decision_paths(paths, decision_number).directory
        / "repartition"
        / f"attempt-{attempt_number:03d}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    return RepartitionAttemptPaths(
        directory=directory,
        source_plan=directory / "source-plan.md",
        envelope=directory / "envelope.json",
        evidence=directory / "repair-evidence.json",
        propose_system_prompt=directory / "propose-system-prompt.txt",
        propose_user_prompt=directory / "propose-user-prompt.txt",
        propose_stdout=directory / "propose-stdout.txt",
        propose_stderr=directory / "propose-stderr.txt",
        proposal=directory / "proposal.json",
        candidate_plan=directory / "candidate-plan.md",
        mechanical_validation=directory / "mechanical-validation.json",
        validate_system_prompt=directory / "validate-system-prompt.txt",
        validate_user_prompt=directory / "validate-user-prompt.txt",
        validate_stdout=directory / "validate-stdout.txt",
        validate_stderr=directory / "validate-stderr.txt",
        semantic_verdict=directory / "semantic-verdict.json",
        result=directory / "result.json",
    )


def write_repartition_artifact(
    path: Path,
    content: str | bytes | Mapping[str, object],
) -> None:
    """Durably write one immutable attempt artifact without overwriting."""
    if path.exists():
        raise FileExistsError(f"repartition artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, Mapping):
        payload = _json_dump(dict(content)).encode("utf-8")
    elif isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        payload = content
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _aflow_dir(repo_root: Path) -> Path:
    return repo_root / ".aflow"


def _global_last_run_id_path(repo_root: Path) -> Path:
    return _aflow_dir(repo_root) / "last_run_id"


def _shell_last_run_ids_dir(repo_root: Path) -> Path:
    return _aflow_dir(repo_root) / "last_run_ids"


def _sanitize_shell_id(raw: str) -> str | None:
    trimmed = raw.strip()
    if not trimmed:
        return None
    sanitized = _SHELL_ID_UNSAFE_RE.sub("_", trimmed).strip("._-")
    if not sanitized:
        digest = hashlib.sha1(trimmed.encode("utf-8")).hexdigest()[:12]
        return f"shell-{digest}"
    if len(sanitized) > 80:
        digest = hashlib.sha1(trimmed.encode("utf-8")).hexdigest()[:12]
        sanitized = f"{sanitized[:48]}-{digest}"
    return sanitized


def _ps_field(pid: int, field: str) -> str | None:
    result = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _detect_shell_pid() -> int | None:
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        command = _ps_field(pid, "comm")
        if command is not None and Path(command).name in SHELL_PROCESS_NAMES:
            return pid
        ppid_value = _ps_field(pid, "ppid")
        if ppid_value is None:
            return None
        try:
            pid = int(ppid_value)
        except ValueError:
            return None
    return None


def resolve_shell_id() -> str | None:
    """Return a stable shell/session identifier when one can be detected."""
    for env_var in SHELL_ID_ENV_VARS:
        value = os.environ.get(env_var)
        if value:
            shell_id = _sanitize_shell_id(f"{env_var.lower()}-{value}")
            if shell_id is not None:
                return shell_id
    shell_pid = _detect_shell_pid()
    if shell_pid is None:
        return None
    return f"shell-pid-{shell_pid}"


def shell_last_run_id_path(repo_root: Path, shell_id: str | None = None) -> Path | None:
    resolved_shell_id = shell_id or resolve_shell_id()
    if resolved_shell_id is None:
        return None
    return _shell_last_run_ids_dir(repo_root) / resolved_shell_id


def _read_run_id_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        run_id = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return run_id or None


def resolve_last_run_id(
    explicit_run_id: str | None,
    repo_root: Path | None,
) -> tuple[Path | None, str | None]:
    """Resolve a run id from explicit args, shell-scoped state, env, or repo fallback."""
    if explicit_run_id is not None:
        return Path(explicit_run_id), "explicit_run_id"

    if repo_root is not None:
        shell_file = shell_last_run_id_path(repo_root)
        if shell_file is not None:
            shell_run_id = _read_run_id_file(shell_file)
            if shell_run_id is not None:
                return Path(shell_run_id), "shell_last_run_id_file"

    env_run_id = os.environ.get("AFLOW_LAST_RUN_ID")
    if env_run_id is not None:
        return Path(env_run_id), "env_var"

    if repo_root is not None:
        run_id = _read_run_id_file(_global_last_run_id_path(repo_root))
        if run_id is not None:
            return Path(run_id), "last_run_id_file"

    return None, None


def write_last_run_id(repo_root: Path, run_id: str) -> None:
    """Write the last run ID to repo-global and shell-scoped state when possible.

    This should be called immediately after run paths are created so that
    even if the run fails or is interrupted, the ID is available for analysis.
    """
    aflow_dir = _aflow_dir(repo_root)
    aflow_dir.mkdir(parents=True, exist_ok=True)
    _global_last_run_id_path(repo_root).write_text(run_id, encoding="utf-8")
    shell_file = shell_last_run_id_path(repo_root)
    if shell_file is None:
        return
    shell_file.parent.mkdir(parents=True, exist_ok=True)
    shell_file.write_text(run_id, encoding="utf-8")


def create_run_paths(config: ControllerConfig) -> RunPaths:
    runs_root = config.repo_root / ".aflow" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    # A control-plane caller may reserve a canonical identity before launch;
    # direct CLI/library callers retain the historical generated-ID behavior.
    if config.reserved_run_id is not None:
        from .control_plane.persistence import validate_run_id

        run_id = validate_run_id(config.reserved_run_id)
    else:
        run_id = _utc_run_id()
    run_dir = runs_root / run_id
    try:
        resolved_runs_root = runs_root.resolve()
        run_dir.resolve(strict=False).relative_to(resolved_runs_root)
    except (OSError, ValueError) as exc:
        raise ValueError("run directory escapes .aflow/runs") from exc
    turns_dir = run_dir / "turns"
    turns_dir.mkdir(parents=True, exist_ok=False)
    manager_dir = run_dir / "manager"
    run_json = run_dir / "run.json"
    paths = RunPaths(
        repo_root=config.repo_root,
        runs_root=runs_root,
        run_dir=run_dir,
        turns_dir=turns_dir,
        manager_dir=manager_dir,
        run_json=run_json,
    )
    prune_old_runs(runs_root, config.keep_runs)
    write_last_run_id(config.repo_root, run_dir.name)
    return paths


def _run_dir_sort_key(path: Path) -> tuple[int, str]:
    stat_result = path.stat()
    return (stat_result.st_mtime_ns, path.name)


def prune_old_runs(runs_root: Path, keep_runs: int) -> None:
    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    run_dirs.sort(key=_run_dir_sort_key)
    while len(run_dirs) > keep_runs:
        doomed = run_dirs.pop(0)
        shutil.rmtree(doomed)


def load_run_json(run_dir: Path) -> dict[str, object] | None:
    """Safely load a run.json file from a run directory.

    Returns None if the file doesn't exist or contains invalid JSON.
    """
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        return None
    try:
        content = run_json.read_text(encoding="utf-8")
        return json.loads(content)
    except (OSError, json.JSONDecodeError):
        return None


def _snapshot_payload(snapshot: PlanSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return snapshot.to_dict()


def _repo_relative_path(repo_root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _issue_summary_relative_path(paths: RunPaths) -> str:
    return str((paths.run_dir / "issues.md").relative_to(paths.repo_root))


def _render_issue_summary(paths: RunPaths, state: ControllerState) -> str:
    run_dir_path = _repo_relative_path(paths.repo_root, paths.run_dir)
    lines: list[str] = [
        "# Issues",
        "",
        f"Run: [`run.json`](run.json)",
        f"Run directory: `{run_dir_path}`",
        f"Count: {state.issues_accumulated}",
        "",
    ]
    for record in state.issue_history:
        lines.extend([
            f"## {record.issue_number}. {record.kind}",
            f"- Message: {record.message}",
        ])
        if record.turn_number is not None:
            lines.append(f"- Turn: {record.turn_number}")
        lines.append(f"- Run metadata: [`run.json`](run.json)")
        if record.result_artifact_path is not None:
            lines.append(f"- Turn result: [`{record.result_artifact_path}`]({record.result_artifact_path})")
        if record.stdout_artifact_path is not None:
            lines.append(f"- Stdout: [`{record.stdout_artifact_path}`]({record.stdout_artifact_path})")
        if record.stderr_artifact_path is not None:
            lines.append(f"- Stderr: [`{record.stderr_artifact_path}`]({record.stderr_artifact_path})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_issue_summary(paths: RunPaths, state: ControllerState) -> str | None:
    if not state.issue_history:
        issue_summary = paths.run_dir / "issues.md"
        if issue_summary.exists():
            issue_summary.unlink()
        state.issues_summary_path = None
        return None

    issue_summary_path = paths.run_dir / "issues.md"
    issue_summary_path.write_text(_render_issue_summary(paths, state), encoding="utf-8")
    relative_path = _issue_summary_relative_path(paths)
    state.issues_summary_path = relative_path
    return relative_path


def _turn_result_payload(
    *,
    turn_number: int,
    invocation: HarnessInvocation,
    snapshot_before: PlanSnapshot,
    snapshot_after: PlanSnapshot | None,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    duration_seconds: float | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    returncode: int | None = None,
    step_name: str | None = None,
    step_role: str | None = None,
    selector: str | None = None,
    original_plan_path: Path | None = None,
    active_plan_path: Path | None = None,
    new_plan_path: Path | None = None,
    conditions: dict[str, bool] | None = None,
    chosen_transition: str | None = None,
    chosen_transition_condition: str | None = None,
    issues_summary_path: str | None = None,
    end_reason: WorkflowEndReason | None = None,
    error: str | None = None,
    retry_attempt: int | None = None,
    retry_limit: int | None = None,
    retry_reason: str | None = None,
    retry_next_turn: bool | None = None,
    was_retry: bool | None = None,
    recovery: HarnessRecoveryContext | None = None,
    review_rejection: object | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "turn_number": turn_number,
        "label": invocation.label,
        "status": status,
        "snapshot_before": snapshot_before.to_dict(),
        "snapshot_after": _snapshot_payload(snapshot_after),
        "started_at": started_at.isoformat(),
        "review_rejection": review_rejection,
    }
    if stdout is not None:
        payload["stdout"] = stdout
    if stderr is not None:
        payload["stderr"] = stderr
    if returncode is not None:
        payload["returncode"] = returncode
    if finished_at is not None:
        payload["finished_at"] = finished_at.isoformat()
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    if step_name is not None:
        payload["step_name"] = step_name
    if step_role is not None:
        payload["step_role"] = step_role
    if selector is not None:
        payload["selector"] = selector
    if original_plan_path is not None:
        payload["original_plan_path"] = str(original_plan_path)
    if active_plan_path is not None:
        payload["active_plan_path"] = str(active_plan_path)
    if new_plan_path is not None:
        payload["new_plan_path"] = str(new_plan_path)
    if conditions is not None:
        payload["conditions"] = conditions
    if chosen_transition is not None:
        payload["chosen_transition"] = chosen_transition
    if chosen_transition_condition is not None:
        payload["chosen_transition_condition"] = chosen_transition_condition
    if end_reason is not None:
        payload["end_reason"] = end_reason
    if error is not None:
        payload["error"] = error
    if retry_attempt is not None:
        payload["retry_attempt"] = retry_attempt
    if retry_limit is not None:
        payload["retry_limit"] = retry_limit
    if retry_reason is not None:
        payload["retry_reason"] = retry_reason
    if retry_next_turn is not None:
        payload["retry_next_turn"] = retry_next_turn
    if was_retry is not None:
        payload["was_retry"] = was_retry
    if issues_summary_path is not None:
        payload["issues_summary_path"] = issues_summary_path
    if recovery is not None:
        payload["recovery_source"] = recovery.source
        payload["recovery_action"] = recovery.action
        payload["recovery_match_terms"] = list(recovery.match_terms)
        payload["recovery_matched_terms"] = list(recovery.matched_terms)
        payload["recovery_delay_seconds"] = recovery.delay_seconds
        payload["recovery_from_team"] = recovery.from_team
        payload["recovery_to_team"] = recovery.to_team
        payload["recovery_reason"] = recovery.reason
        payload["recovery_consecutive_count"] = recovery.consecutive_count
        payload["recovery_suggested_keywords"] = list(recovery.suggested_keywords)
        payload["recovery_suggested_action"] = recovery.suggested_action
        payload["recovery_executed"] = recovery.executed
        payload["recovery_rejection_reason"] = recovery.rejection_reason
    return payload


def _validated_environment_preflight_payload(
    value: Mapping[str, object],
) -> dict[str, object]:
    required = (
        "classification",
        "reason_code",
        "harness",
        "invocation_kind",
        "required_executable",
        "remediation",
    )
    if value.get("schema_version") != 1 or value.get("classification") != "harness_environment_preflight":
        raise ValueError("invalid environment preflight payload classification")
    for field in required[1:]:
        item = value.get(field)
        if (
            not isinstance(item, str)
            or not item.strip()
            or "/" in item
            or chr(92) in item
            or len(item) > 240
        ):
            raise ValueError(f"invalid environment preflight field: {field}")
    checked = value.get("checked_command")
    if (
        not isinstance(checked, (list, tuple))
        or not checked
        or any(
            not isinstance(item, str)
            or not item.strip()
            or "/" in item
            or chr(92) in item
            or len(item) > 240
            for item in checked
        )
    ):
        raise ValueError("invalid environment preflight checked_command")
    diagnostics = value.get("safe_diagnostics", {})
    if not isinstance(diagnostics, Mapping) or any(
        not isinstance(key, str)
        or not isinstance(item, str)
        or not key.isidentifier()
        or not item.strip()
        or "/" in item
        or chr(92) in item
        for key, item in diagnostics.items()
    ):
        raise ValueError("invalid environment preflight safe_diagnostics")
    result: dict[str, object] = {
        "schema_version": 1,
        "classification": value["classification"],
        "reason_code": value["reason_code"],
        "harness": value["harness"],
        "invocation_kind": value["invocation_kind"],
        "required_executable": value["required_executable"],
        "checked_command": list(checked),
        "remediation": value["remediation"],
        "safe_diagnostics": dict(diagnostics),
    }
    for field in ("step_name", "manager_level", "lifecycle_phase"):
        item = value.get(field)
        if isinstance(item, str) and item.strip() and "/" not in item and chr(92) not in item:
            result[field] = item
    turn_number = value.get("turn_number")
    if isinstance(turn_number, int) and turn_number > 0:
        result["turn_number"] = turn_number
    return result


@dataclass(frozen=True)
class RunMetadataWriter:
    paths: RunPaths
    config: ControllerConfig
    state: ControllerState | None
    workflow_name: str
    resumed_from_run_id: str | None = None

    def write(
        self,
        *,
        status: str,
        execution_context: ExecutionContext | None = None,
        end_reason: WorkflowEndReason | None = None,
        failure_reason: str | None = None,
        failure_kind: str | None = None,
        environment_preflight: Mapping[str, object] | None = None,
        merge_status: str | None = None,
        merge_failure_reason: str | None = None,
        last_snapshot: PlanSnapshot | None = None,
        turns_completed: int | None = None,
        current_step_name: str | None = None,
        original_plan_path: Path,
        active_plan_path: Path | None = None,
        new_plan_path: Path | None = None,
        pending_retry: RetryContext | None = None,
        team: str | None = None,
        issues_summary_path: str | None = None,
    ) -> None:
        if not isinstance(self.workflow_name, str) or not self.workflow_name.strip():
            raise ValueError("workflow_name must be a non-empty string")
        if not isinstance(original_plan_path, Path) or not str(original_plan_path):
            raise ValueError("original_plan_path must be a non-empty path")
        for field_name, value in (
            ("max_turns", self.config.max_turns),
            (
                "effective_max_turns",
                self.state.effective_max_turns
                if self.state is not None and self.state.effective_max_turns is not None
                else self.config.max_turns,
            ),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if any(
            not isinstance(item, str) for item in self.config.extra_instructions
        ):
            raise ValueError("extra_instructions must be a list of strings")
        if self.state is not None:
            if self.state.current_team is not None and (
                not isinstance(self.state.current_team, str) or not self.state.current_team.strip()
            ):
                raise ValueError("team must be null or a non-empty string")
            if (
                self.state.selected_start_step is not None
                and (
                    not isinstance(self.state.selected_start_step, str)
                    or not self.state.selected_start_step.strip()
                )
            ):
                raise ValueError(
                    "selected_start_step must be null or a non-empty string"
                )
        previous: Mapping[str, object] = {}
        run_json_present = self.paths.run_json.exists() or self.paths.run_json.is_symlink()
        if run_json_present:
            try:
                loaded = json.loads(self.paths.run_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "cannot overwrite existing run metadata: run.json is unreadable"
                ) from exc
            if not isinstance(loaded, Mapping):
                raise ValueError(
                    "cannot overwrite existing run metadata: run.json must contain a JSON object"
                )
            previous = loaded
            if previous.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
                raise ValueError(
                    "cannot overwrite run metadata with an unsupported resume state schema"
                )
        # A terminal manager report is the authoritative failure summary.  Some
        # callers add merge/recovery metadata in a later write without repeating
        # that summary; retain it rather than replacing it with an empty field.
        if failure_reason is None and status == "failed":
            previous_reason = previous.get("failure_reason")
            if isinstance(previous_reason, str) and previous_reason.startswith("# AFlow manager report"):
                failure_reason = previous_reason
        payload: dict[str, object] = {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "repo_root": str(self.paths.repo_root),
            "run_dir": str(self.paths.run_dir),
            "status": status,
            "plan_path": str(self.config.plan_path),
            "workflow_name": self.workflow_name,
            "original_plan_path": str(original_plan_path),
            "max_turns": self.config.max_turns,
            "keep_runs": self.config.keep_runs,
            "extra_instructions": list(self.config.extra_instructions),
            "turns_completed": turns_completed if turns_completed is not None else (self.state.turns_completed if self.state else 0),
            "last_snapshot": _snapshot_payload(last_snapshot if last_snapshot is not None else (self.state.last_snapshot if self.state else None)),
        }
        if execution_context is not None:
            payload["execution_repo_root"] = str(execution_context.execution_repo_root)
            payload["feature_branch"] = execution_context.feature_branch
            payload["main_branch"] = execution_context.main_branch
            payload["lifecycle_setup"] = list(execution_context.setup)
            payload["lifecycle_teardown"] = list(execution_context.teardown)
            if execution_context.worktree_path is not None:
                payload["worktree_path"] = str(execution_context.worktree_path)
        else:
            # Manager and status boundaries often update run metadata without
            # carrying the execution context again. Lifecycle identity is durable
            # resume state, so omission must not erase an already-recorded worktree
            # or branch.
            for key in (
                "execution_repo_root",
                "feature_branch",
                "main_branch",
                "lifecycle_setup",
                "lifecycle_teardown",
                "worktree_path",
            ):
                if key in previous:
                    payload[key] = previous[key]
        if current_step_name is not None:
            payload["current_step_name"] = current_step_name
        if active_plan_path is not None:
            payload["active_plan_path"] = str(active_plan_path)
        if new_plan_path is not None:
            payload["new_plan_path"] = str(new_plan_path)
        payload["team"] = team if team is not None else (
            self.state.current_team
            if self.state is not None and self.state.current_team is not None
            else self.config.team
        )
        if self.state is not None and self.state.issues_summary_path is not None:
            payload["issues_summary_path"] = self.state.issues_summary_path
        elif issues_summary_path is not None:
            payload["issues_summary_path"] = issues_summary_path
        if self.resumed_from_run_id is not None:
            payload["resumed_from_run_id"] = self.resumed_from_run_id
        if self.state is not None:
            payload["run_started_at"] = self.state.run_started_at.isoformat()
            payload["active_turn"] = self.state.active_turn
            payload["status_message"] = self.state.status_message
            payload["selected_start_step"] = self.state.selected_start_step
            payload["startup_recovery_used"] = self.state.startup_recovery_used
            payload["startup_recovery_reason"] = self.state.startup_recovery_reason
            payload["effective_max_turns"] = (
                self.state.effective_max_turns
                if self.state.effective_max_turns is not None
                else self.config.max_turns
            )
            payload["override_file_present"] = self.state.override_file_present
            if self.state.frozen_run_identity is not None:
                payload["frozen_config"] = asdict(self.state.frozen_run_identity)
            if self.state.override_result is not None:
                payload["override_result"] = asdict(self.state.override_result)
            if self.state.pending_override_notes:
                payload["pending_override_notes"] = list(self.state.pending_override_notes)
            if self.state.override_source_run_dir is not None:
                payload["override_source_run_dir"] = str(self.state.override_source_run_dir)
            if end_reason is None:
                end_reason = self.state.end_reason
        else:
            payload["selected_start_step"] = None
            payload["startup_recovery_used"] = False
            payload["startup_recovery_reason"] = None
            payload["effective_max_turns"] = self.config.max_turns

        lifecycle_setup = payload.get("lifecycle_setup", [])
        lifecycle_teardown = payload.get("lifecycle_teardown", [])
        if not isinstance(lifecycle_setup, list) or not all(
            isinstance(item, str) for item in lifecycle_setup
        ):
            raise ValueError("lifecycle_setup must be a list of strings")
        if not isinstance(lifecycle_teardown, list) or not all(
            isinstance(item, str) for item in lifecycle_teardown
        ):
            raise ValueError("lifecycle_teardown must be a list of strings")
        payload["lifecycle_setup"] = lifecycle_setup
        payload["lifecycle_teardown"] = lifecycle_teardown

        frozen_config = payload.get("frozen_config")
        if frozen_config is None and previous.get("schema_version") == RUN_STATE_SCHEMA_VERSION:
            frozen_config = previous.get("frozen_config")
        if not isinstance(frozen_config, Mapping) or any(
            not isinstance(frozen_config.get(field), str)
            or not str(frozen_config.get(field)).strip()
            for field in ("workflow_name", "config_path", "config_fingerprint")
        ):
            raise ValueError("frozen_config must contain current non-empty identity fields")
        payload["frozen_config"] = dict(frozen_config)

        durable_state = self.state
        if durable_state is None:
            durable_state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        payload.update(manager_state_payload(durable_state))
        payload.update(hotplug_state_payload(durable_state))
        if end_reason is not None:
            payload["end_reason"] = end_reason
        if failure_reason is not None:
            payload["failure_reason"] = failure_reason
        if merge_status is not None:
            payload["merge_status"] = merge_status
        if merge_failure_reason is not None:
            payload["merge_failure_reason"] = merge_failure_reason
        effective_retry = pending_retry if pending_retry is not None else (self.state.pending_retry if self.state is not None else None)
        if effective_retry is not None:
            payload["pending_retry_step_name"] = effective_retry.step_name
            payload["pending_retry_attempt"] = effective_retry.attempt
            payload["pending_retry_limit"] = effective_retry.retry_limit
            payload["pending_retry_reason"] = "inconsistent_checkpoint_state"
        if self.state is not None and self.state.current_harness_recovery is not None:
            recovery = self.state.current_harness_recovery
            payload["recovery_source"] = recovery.source
            payload["recovery_action"] = recovery.action
            payload["recovery_match_terms"] = list(recovery.match_terms)
            payload["recovery_matched_terms"] = list(recovery.matched_terms)
            payload["recovery_delay_seconds"] = recovery.delay_seconds
        if failure_kind is not None:
            if failure_kind != "environment_preflight":
                raise ValueError("invalid environment preflight failure kind")
            payload["failure_kind"] = failure_kind
        elif isinstance(previous.get("failure_kind"), str):
            payload["failure_kind"] = previous["failure_kind"]
        if environment_preflight is not None:
            payload["environment_preflight"] = _validated_environment_preflight_payload(
                environment_preflight
            )
        elif isinstance(previous.get("environment_preflight"), Mapping):
            try:
                payload["environment_preflight"] = _validated_environment_preflight_payload(
                    previous["environment_preflight"]
                )
            except ValueError:
                pass
        if self.state is not None and self.state.harness_recovery_history:
            payload.update(build_recovery_payload(self.state.current_harness_recovery, self.state.harness_recovery_history))
        _write_atomic_json(self.paths.run_json, payload)


def write_turn_artifacts_start(
    paths: RunPaths,
    *,
    turn_number: int,
    invocation: HarnessInvocation,
    snapshot_before: PlanSnapshot,
    started_at: datetime | None = None,
    status: str,
    step_name: str | None = None,
    step_role: str | None = None,
    selector: str | None = None,
    original_plan_path: Path | None = None,
    active_plan_path: Path | None = None,
    new_plan_path: Path | None = None,
    recovery: HarnessRecoveryContext | None = None,
    review_rejection: object | None = None,
) -> Path:
    turn_dir = paths.turns_dir / f"turn-{turn_number:03d}"
    turn_dir.mkdir(parents=False, exist_ok=False)
    (turn_dir / "system-prompt.txt").write_text(invocation.system_prompt, encoding="utf-8")
    (turn_dir / "user-prompt.txt").write_text(invocation.user_prompt, encoding="utf-8")
    (turn_dir / "effective-prompt.txt").write_text(invocation.effective_prompt, encoding="utf-8")
    _write_json(turn_dir / "argv.json", {"argv": list(invocation.argv), "label": invocation.label, "prompt_mode": invocation.prompt_mode})
    _write_json(turn_dir / "env.json", {"env": dict(invocation.env)})
    result_payload = _turn_result_payload(
        turn_number=turn_number,
        invocation=invocation,
        snapshot_before=snapshot_before,
        snapshot_after=None,
        status=status,
        started_at=started_at or datetime.now(timezone.utc),
        step_name=step_name,
        step_role=step_role,
        selector=selector,
        original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
        recovery=recovery,
        review_rejection=review_rejection,
    )
    _write_json(turn_dir / "result.json", result_payload)
    return turn_dir


def finalize_turn_artifacts(
    turn_dir: Path,
    *,
    turn_number: int,
    invocation: HarnessInvocation,
    stdout: str,
    stderr: str,
    returncode: int,
    snapshot_before: PlanSnapshot,
    snapshot_after: PlanSnapshot | None,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
    error: str | None = None,
    step_name: str | None = None,
    step_role: str | None = None,
    selector: str | None = None,
    original_plan_path: Path | None = None,
    active_plan_path: Path | None = None,
    new_plan_path: Path | None = None,
    conditions: dict[str, bool] | None = None,
    chosen_transition: str | None = None,
    chosen_transition_condition: str | None = None,
    issues_summary_path: str | None = None,
    end_reason: WorkflowEndReason | None = None,
    retry_attempt: int | None = None,
    retry_limit: int | None = None,
    retry_reason: str | None = None,
    retry_next_turn: bool | None = None,
    was_retry: bool | None = None,
    recovery: HarnessRecoveryContext | None = None,
    review_rejection: object | None = None,
) -> Path:
    (turn_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (turn_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    result_payload = _turn_result_payload(
        turn_number=turn_number,
        invocation=invocation,
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        step_name=step_name,
        step_role=step_role,
        selector=selector,
        original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
        conditions=conditions,
        chosen_transition=chosen_transition,
        chosen_transition_condition=chosen_transition_condition,
        issues_summary_path=issues_summary_path,
        end_reason=end_reason,
        error=error,
        retry_attempt=retry_attempt,
        retry_limit=retry_limit,
        retry_reason=retry_reason,
        retry_next_turn=retry_next_turn,
        was_retry=was_retry,
        recovery=recovery,
        review_rejection=review_rejection,
    )
    _write_json(turn_dir / "result.json", result_payload)
    return turn_dir

"""Exclusive execution resource turn admission tests (checkpoint 4).

Covers the shared control-aware admission gate that fronts exclusive
(harness, model, effort) combinations: FIFO fairness, queued controls
(owner stop, live configuration revalidation), position retention on
prompt-only edits, journal/pair lock contention, resume freshness, and
worker upgrade chain integrity.  The final-only paired-controller
matrix lives in :class:`TestPairedControllers` and is exercised during
final verification, not the checkpoint 4 gate.
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import queue
import signal
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from aflow.config import (
    AflowSection,
    ErrorHandlingConfig,
    GoTransition,
    HarnessErrorRecoveryConfig,
    HarnessProfileConfig,
    ManagerConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
    execution_resource_key,
    load_workflow_config,
)
from aflow.execution_resources import (
    ClaimSpec,
    ControllerIdentity,
    ExecutionLease,
    ExecutionResourceAdmission,
    ExecutionResourceStore,
    Outcome,
    ProcessEvidence,
    RevalidationVerdict,
    ResourceLeaseError,
    TurnReprepareRequired,
    UNCHANGED,
    owned_child_binding,
)
from aflow.harnesses.base import HarnessInvocation
from aflow.harnesses.codex import CodexAdapter
from aflow.harnesses.preflight import NoOpHarnessPreflightProbe
from aflow.harnesses.session import (
    SessionCapabilities,
    SessionExecutionResult,
    SessionRequest,
    SessionResult,
    retain_lifecycle_after_failure,
)
from aflow.hotplug import (
    HANDOVER_HEADINGS,
    HarnessSessionRefV1,
    HotplugTransactionV1,
    hotplug_transaction_id,
)
from aflow.process_identity import process_birth_identity, process_liveness
from aflow.run_state import (
    ControllerConfig,
    ImplementationAttempt,
    ResumeContext,
    _mark_validated_resume_context,
    manager_resume_fields,
)
from aflow.workflow import (
    ResolvedProfile,
    WorkflowError,
    _AuxiliaryAdmissionContext,
    load_scope_evidence_for_resume,
    resolve_profile,
    run_workflow,
)
from tests._support import (
    _git_commit_file,
    _git_merge_feature_into_main,
    _make_lifecycle_git_repo,
    _run_git_in_test,
    _write_plan,
    _write_split_config,
)

# ---------------------------------------------------------------------------
# Configuration templates
# ---------------------------------------------------------------------------

_WORKER_BASE = "worker-base"
_WORKER_HIGH = "worker-high"
_WORKER_TOP = "worker-top"
_WORKER_PLAIN = "worker-plain"
_REVIEWER = "model-reviewer"
_AUDITOR = "model-auditor"
_MANAGER_MODEL = "model-manager"

_VALID_PLAN = "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n"
_REPAIR_PLAN = (
    "# Repair\n\n### [ ] Checkpoint 1: Repair\n- [ ] repair the first checkpoint\n"
)


def _aflow_toml(
    *,
    worker_model: str = _WORKER_BASE,
    worker_exclusive: bool = True,
    reviewer_model: str = _REVIEWER,
    reviewer_exclusive: bool = True,
    auditor_model: str = _AUDITOR,
    auditor_exclusive: bool = True,
    prompt: str = "old prompt {ACTIVE_PLAN_PATH}",
    prompt2: str | None = None,
    max_turns: int = 12,
    manager_enabled: bool = False,
    manager_exclusive: bool = False,
    manager_model: str = _MANAGER_MODEL,
) -> str:
    w_excl = "exclusive = true\n" if worker_exclusive else ""
    r_excl = "exclusive = true\n" if reviewer_exclusive else ""
    a_excl = "exclusive = true\n" if auditor_exclusive else ""
    m_excl = "exclusive = true\n" if manager_exclusive else ""
    manager_profiles = (
        f"""
[harness.codex.profiles.manager]
model = "{manager_model}"
{m_excl}"""
        if manager_enabled
        else ""
    )
    manager_role = "manager = \"codex.manager\"\n" if manager_enabled else ""
    prompt2_line = f'q = "{prompt2}"\n' if prompt2 is not None else ""
    manager_table = (
        """
[manager]
lite_role = "manager"
full_role = "manager"
"""
        if manager_enabled
        else ""
    )
    return f"""
[aflow]
default_workflow = "live"
max_turns = {max_turns}

[harness.codex.profiles.base]
model = "{worker_model}"
{w_excl}
[harness.codex.profiles.base-high]
model = "{_WORKER_HIGH}"
exclusive = true

[harness.codex.profiles.base-top]
model = "{_WORKER_TOP}"
exclusive = true

[harness.codex.profiles.plain]
model = "{_WORKER_PLAIN}"

[harness.codex.profiles.reviewer]
model = "{reviewer_model}"
{r_excl}
[harness.codex.profiles.auditor]
model = "{auditor_model}"
{a_excl}{manager_profiles}
[roles]
worker = "codex.base"
reviewer = "codex.reviewer"
auditor = "codex.auditor"
{manager_role}
[teams.base]
worker = "codex.base"
reviewer = "codex.reviewer"
auditor = "codex.auditor"
{manager_role}upgrade_to = "high"

[teams.high]
worker = "codex.base-high"
extends = "base"
upgrade_to = "top"

[teams.top]
worker = "codex.base-top"
extends = "base"

[prompts]
p = "{prompt}"
{prompt2_line}{manager_table}
"""


def _workflows_toml(
    *,
    steps: str = "work_review",
    upgrade_after_repairs: int | None = None,
    manager_enabled: bool = False,
    work_role: str = "worker",
    work_prompts: str = '"p"',
) -> str:
    upgrade = (
        f"upgrade_after_repairs = {upgrade_after_repairs}\n"
        if upgrade_after_repairs is not None
        else ""
    )
    manager_flag = "manager_enabled = true\n" if manager_enabled else ""
    if steps == "work_only":
        work_go = '[{ to = "END", when = "DONE" }, { to = "work" }]'
        review, audit = "", ""
    elif steps == "work_review":
        work_go = '[{ to = "review" }]'
        review = """
[workflow.live.steps.review]
role = "reviewer"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }, { to = "work" }]
"""
        audit = ""
    else:
        work_go = '[{ to = "review" }]'
        review = """
[workflow.live.steps.review]
role = "reviewer"
prompts = ["p"]
go = [{ to = "audit", when = "DONE" }, { to = "work" }]
"""
        audit = """
[workflow.live.steps.audit]
role = "auditor"
prompts = ["p"]
go = [{ to = "END" }]
"""
    return f"""
[workflow.live]
team = "base"
{manager_flag}{upgrade}[workflow.live.steps.work]
role = "{work_role}"
prompts = [{work_prompts}]
go = {work_go}
{review}{audit}
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _Observer:
    def __init__(self) -> None:
        self.events: list[object] = []

    def on_event(self, event: object) -> None:
        self.events.append(event)

    def resource_events(self) -> list[object]:
        return [
            event
            for event in self.events
            if getattr(event, "event_type", "")
            in {
                "execution_resource_waiting",
                "execution_resource_acquired",
                "execution_resource_cancelled",
                "execution_resource_released",
            }
        ]


def _resource_for(
    config_path: Path, selector: str, step_path: str = "workflow.live.steps.work"
) -> str | None:
    config = load_workflow_config(config_path)
    resolved = resolve_profile(selector, config, step_path=step_path)
    return resolved.exclusive_resource


def _foreign_identity(store: ExecutionResourceStore) -> ControllerIdentity:
    real = store.current_controller_identity()
    assert real is not None, "controller identity must be available in tests"
    return ControllerIdentity(pid=999999, birth="foreign-birth", boot=real.boot)


def _foreign_enqueue(
    store: ExecutionResourceStore, resource: str, role: str = "worker"
) -> tuple[ControllerIdentity, str]:
    identity = _foreign_identity(store)
    invocation_id = "foreign-invocation"
    spec = ClaimSpec(
        project_root="/foreign",
        run_id="foreign-run",
        invocation_id=invocation_id,
        kind="turn",
        role=role,
        selector="codex.base",
    )
    outcome = store.enqueue(resource, spec, identity)
    assert outcome.state in ("queued", "acquired"), outcome
    return identity, invocation_id


def _foreign_acquire(
    store: ExecutionResourceStore,
    resource: str,
    identity: ControllerIdentity,
    invocation_id: str,
    *,
    timeout: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        outcome = store.try_acquire(resource, invocation_id, identity)
        if outcome.state == "acquired":
            return
        assert outcome.state in ("queued", "contended"), outcome
        time.sleep(0.01)
    pytest.fail(f"foreign controller never acquired {resource[:8]}")


def _occupy(
    store: ExecutionResourceStore,
    resource: str,
    role: str = "worker",
    *,
    timeout: float = 20.0,
) -> tuple[ControllerIdentity, str]:
    """Enqueue a foreign claim and poll until it owns the resource."""
    identity, invocation_id = _foreign_enqueue(store, resource, role)
    _foreign_acquire(
        store, resource, identity, invocation_id, timeout=timeout
    )
    return identity, invocation_id


def _journal_owner(tmp_path: Path, resource: str) -> dict | None:
    """Read the current journal owner claim (or None) for a resource."""
    path = tmp_path / "resource-store" / f"{resource}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["owner"]


def _release(
    store: ExecutionResourceStore,
    resource: str,
    identity: ControllerIdentity,
    invocation_id: str,
    *,
    required: bool = True,
    timeout: float = 20.0,
) -> None:
    """Release an owned claim, tolerating journal-lock contention.

    A foreign owner can lose the release race to a concurrently active
    controller that holds the same journal lock.  Contention changes
    nothing about ownership, so a bounded retry is safe.
    """
    deadline = time.monotonic() + timeout
    while True:
        outcome = store.record_completion(resource, invocation_id, identity)
        if outcome.state != "contended" or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    # A second release of an already completed claim is a no-op.
    if required:
        assert outcome.state in ("released", "cancelled", "acquired"), outcome


def _run_dirs(root: Path) -> list[Path]:
    runs = root / ".aflow" / "runs"
    return sorted(runs.iterdir()) if runs.is_dir() else []


def _first_run_dir(root: Path) -> Path:
    assert _wait_until(
        lambda: any(
            (run_dir / "run.json").is_file() for run_dir in _run_dirs(root)
        )
    )
    return _run_dirs(root)[0]


def _last_run_dir(root: Path) -> Path:
    dirs = _run_dirs(root)
    assert len(dirs) >= 2, "expected a resumed run directory"

    def _newest() -> Path:
        # Run ids embed a random suffix, so name order does not reliably
        # reflect creation order within the same second.  The resumed run's
        # directory is the one still receiving writes.
        return max(
            _run_dirs(root),
            key=lambda p: (p.stat().st_mtime, p.name),
        )

    assert _wait_until(lambda: (_newest() / "run.json").is_file())
    return _newest()


def _run_json(run_dir: Path) -> dict:
    path = run_dir / "run.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_until(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _stop_run(run_dir: Path) -> None:
    (run_dir / "overrides.toml").write_text(
        "owner_stop = true\n", encoding="utf-8"
    )


def _launch(
    tmp_path: Path,
    config_path: Path,
    plan_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    observer: _Observer | None = None,
    *,
    resume: ResumeContext | None = None,
    max_turns: int = 12,
    config_kwargs: dict | None = None,
) -> tuple[threading.Thread, dict]:
    config = ControllerConfig(
        repo_root=tmp_path,
        plan_path=plan_path,
        max_turns=max_turns,
        **(config_kwargs or {}),
    )
    result: dict = {}

    def target() -> None:
        try:
            result["value"] = run_workflow(
                config,
                load_workflow_config(config_path),
                "live",
                config_dir=config_path,
                snapshot_config=False,
                adapter=CodexAdapter(),
                runner=runner,
                observer=observer,
                resume=resume,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by assertions
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _launch_config(
    tmp_path: Path,
    wf_config: WorkflowUserConfig,
    plan_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    observer: _Observer | None = None,
    *,
    workflow_name: str = "live",
    max_turns: int = 12,
    config_kwargs: dict | None = None,
    repo_root: Path | None = None,
) -> tuple[threading.Thread, dict]:
    """Launch ``run_workflow`` with an in-memory config (no config file)."""
    root = repo_root if repo_root is not None else tmp_path
    config = ControllerConfig(
        repo_root=root,
        plan_path=plan_path,
        max_turns=max_turns,
        **(config_kwargs or {}),
    )
    result: dict = {}

    def target() -> None:
        try:
            result["value"] = run_workflow(
                config,
                wf_config,
                workflow_name,
                config_dir=root,
                snapshot_config=False,
                adapter=CodexAdapter(),
                runner=runner,
                observer=observer,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by assertions
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _worker_runner(
    plan_path: Path,
    calls: list[str],
    *,
    complete_after: int = 1,
    hooks: Callable[[str, int], None] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    worker_count = 0

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal worker_count
        model = argv[argv.index("--model") + 1]
        prompt = str(kwargs.get("input", ""))
        if model == _MANAGER_MODEL and "schema_version" in prompt:
            calls.append("manager")
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "schema_version": 1,
                        "action": "continue",
                        "reason": "synthetic continue",
                        "next_step_notes": [],
                        "stop_report": None,
                    }
                ),
                "",
            )
        calls.append(model)
        if model in (_WORKER_BASE, _WORKER_HIGH, _WORKER_TOP, _WORKER_PLAIN):
            worker_count += 1
            if hooks is not None:
                hooks(model, worker_count)
            if worker_count >= complete_after:
                plan_path.write_text(
                    _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
                )
        return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

    return runner


@pytest.fixture
def resource_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = ExecutionResourceStore(root=tmp_path / "resource-store")
    monkeypatch.setattr(
        "aflow.workflow._default_execution_resource_store", lambda: store
    )
    monkeypatch.setattr(
        "aflow.workflow._execution_resource_admission_options",
        lambda: {"poll_interval": 0.02, "control_interval": 0.05},
    )
    return store


# ---------------------------------------------------------------------------
# Turn admission matrix
# ---------------------------------------------------------------------------


class TestTurnAdmission:
    @pytest.mark.parametrize("seam", ["keyword_only", "kwargs"])
    def test_owned_session_receives_marked_lease_by_keyword(
        self, tmp_path: Path, resource_store: ExecutionResourceStore, seam: str
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        assert resource is not None
        calls: list[ExecutionLease] = []

        class OwnedDriver:
            capabilities = SessionCapabilities(session_identity=True)

            def build_invocation(self, request):
                return HarnessInvocation(
                    label="owned-keyword-test", argv=("owned-keyword-test",), env={},
                    prompt_mode="owned-session", system_prompt=request.system_prompt,
                    user_prompt=request.user_prompt, effective_prompt=request.user_prompt,
                )

            def finish(self, request, lifecycle):
                assert isinstance(lifecycle, ExecutionLease)
                calls.append(lifecycle)
                # This finite local fake creates no process. Its synchronous
                # return proves cessation and releases the reserved claim.
                lifecycle.complete()
                plan_path.write_text(_VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8")
                return SessionExecutionResult(
                    result=SessionResult(
                        session_id="owned-keyword-session", selector=request.selector,
                        model=request.model, effort=request.effort, final_output="DONE",
                        capabilities=self.capabilities,
                    ),
                    raw_transport="local fake completed",
                )

            def keyword_only(self, request, invocation, control_callback=None, *, lifecycle):
                return self.finish(request, lifecycle)

            def kwargs(self, request, invocation, control_callback=None, **kwargs):
                assert set(kwargs) == {"lifecycle"}
                return self.finish(request, kwargs["lifecycle"])

        driver = OwnedDriver()
        driver.execute_session = getattr(driver, seam)
        result = run_workflow(
            ControllerConfig(repo_root=tmp_path, plan_path=plan_path, max_turns=1),
            load_workflow_config(config_path), "live", config_dir=config_path,
            snapshot_config=False, adapter=CodexAdapter(), session_driver=driver,
            preflight_probe=NoOpHarnessPreflightProbe(),
        )
        assert result.status == "completed"
        assert len(calls) == 1
        journal = json.loads((tmp_path / "resource-store" / f"{resource}.json").read_text())
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_worker_waits_for_busy_resource_then_acquires(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        assert resource is not None
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == resource
            assert waiting["role"] == "worker"
            assert waiting["step"] == "work"
            assert waiting["ticket"] == 2
            # Complete version-1 wait evidence.
            assert waiting["version"] == 1
            assert waiting["label"] == "codex / worker-base"
            assert waiting["kind"] == "turn"
            assert waiting["selector"] == "codex.base"
            assert isinstance(waiting["invocation_id"], str)
            assert len(waiting["invocation_id"]) > 0
            assert waiting["wait_started_at"]
            assert isinstance(waiting["controller_pid"], int)
            assert waiting.get("controller_birth")
            assert _run_json(run_dir)["status_message"].startswith(
                "Waiting for codex / worker-base (exclusive)"
            )
            assert calls == [], "no model call may happen while waiting"
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        assert not thread.is_alive()
        run = _run_json(run_dir)
        assert run.get("execution_resource_wait") is None
        assert not str(run.get("status_message", "")).startswith(
            "Waiting for"
        )
        assert run["turns_completed"] == 1
        phases = [event.phase for event in observer.resource_events()]
        assert phases.count("waiting") >= 1
        assert "acquired" in phases
        assert "released" in phases
        assert "cancelled" not in phases

    def test_reviewer_and_custom_role_admit_on_their_own_resources(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_review_audit"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        worker_resource = _resource_for(config_path, "codex.base")
        reviewer_resource = _resource_for(
            config_path, "codex.reviewer", "workflow.live.steps.review"
        )
        auditor_resource = _resource_for(
            config_path, "codex.auditor", "workflow.live.steps.audit"
        )
        assert worker_resource != reviewer_resource != auditor_resource
        reviewer_identity, reviewer_invocation = _occupy(
            resource_store, reviewer_resource, role="reviewer"
        )
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # The worker is free and runs first; the reviewer is busy.
            assert _wait_until(lambda: calls == [_WORKER_BASE])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == reviewer_resource
            assert waiting["role"] == "reviewer"
            assert calls == [_WORKER_BASE]
            _release(
                resource_store, reviewer_resource,
                reviewer_identity, reviewer_invocation,
            )
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, reviewer_resource,
                reviewer_identity, reviewer_invocation,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE, _REVIEWER, _AUDITOR]
        assert not thread.is_alive()
        run = _run_json(run_dir)
        assert run.get("execution_resource_wait") is None
        assert run["turns_completed"] == 3

    def test_unmarked_profile_never_touches_broker(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(worker_exclusive=False),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), observer,
        )
        thread.join(timeout=20)
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        assert not observer.resource_events()
        run = _run_json(_run_dirs(tmp_path)[0])
        assert run.get("execution_resource_wait") is None
        # The broker root must never have been created for unmarked runs.
        assert not (tmp_path / "resource-store").exists()

    def test_owner_stop_precedes_invalid_config_while_waiting(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        # The malformed configuration is written while the configuration
        # pair lock is held: the controller cannot revalidate it mid-wait
        # and keeps waiting, so the stop request below is already pending
        # when the pair becomes readable.  Owner stop is serviced before
        # any live configuration parse and must win.
        lock_path = config_path.parent / ".aflow-config-pair.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            config_path.write_text("[aflow\nbroken toml", encoding="utf-8")
            _stop_run(run_dir)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        try:
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == []
        run = _run_json(run_dir)
        assert run["status"] == "owner_stopped"
        assert run.get("execution_resource_wait") is None

    def test_malformed_changed_config_fails_queued_claim(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        """A malformed changed live configuration fails the queued turn.

        Waiting behind an occupied resource with an unusable configuration
        could never become dispatchable: the changed-config error cancels
        the claim and surfaces as an actionable pre-turn failure with zero
        turns, zero provider calls, and a clean journal.
        """
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            config_path.write_text("[aflow\nbroken toml", encoding="utf-8")
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" in result, "the changed-config failure must surface"
        assert calls == [], "no model call may happen for an unusable config"
        assert not thread.is_alive()
        run = _run_json(run_dir)
        assert run["status"] == "failed"
        assert run["turns_completed"] == 0
        assert "admission revalidation failed" in run.get(
            "failure_reason", ""
        )
        assert run.get("execution_resource_wait") is None
        assert not (run_dir / "turns").is_dir() or not any(
            (run_dir / "turns").iterdir()
        ), "no turn artifacts may exist for a pre-turn failure"
        journal = json.loads(
            (tmp_path / "resource-store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_invalid_override_while_waiting_is_nonfatal(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            (run_dir / "overrides.toml").write_text(
                'next_step = "nonexistent"\n', encoding="utf-8"
            )
            time.sleep(0.3)
            assert calls == []
            assert _run_json(run_dir).get("execution_resource_wait") is not None
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]

    def test_route_change_cancels_claim_and_reprepares(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            # Repoint the worker at an unmarked profile: the claim on the
            # exclusive resource must be cancelled and the turn re-prepared.
            config_path.write_text(
                _aflow_toml(worker_model=_WORKER_PLAIN, worker_exclusive=False),
                encoding="utf-8",
            )
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_PLAIN]
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" in phases
        run = _run_json(run_dir)
        assert run.get("execution_resource_wait") is None
        # The cancelled claim leaves no owner or queue entry behind.
        journal = json.loads(
            (tmp_path / "resource-store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_model_edit_changes_resource_identity_and_waits_on_new_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        old_resource = _resource_for(config_path, "codex.base")
        new_resource = _resource_for(
            config_path, "codex.base-high", "workflow.live.steps.work"
        )
        assert old_resource != new_resource
        old_identity, old_invocation = _occupy(resource_store, old_resource)
        new_identity, new_invocation = _occupy(
            resource_store, new_resource
        )
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            assert (
                _run_json(run_dir)["execution_resource_wait"]["resource"]
                == old_resource
            )
            # Live model edit: the resource identity changes and the claim
            # moves to the new resource, which is also busy.
            config_path.write_text(
                _aflow_toml(worker_model=_WORKER_HIGH), encoding="utf-8"
            )
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait")
                    and _run_json(run_dir)["execution_resource_wait"][
                        "resource"
                    ]
                    == new_resource
                )
            )
            assert calls == []
            _release(resource_store, old_resource, old_identity, old_invocation)
            _release(
                resource_store, new_resource, new_identity, new_invocation
            )
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, old_resource, old_identity, old_invocation,
                required=False,
            )
            _release(
                resource_store, new_resource, new_identity, new_invocation,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_HIGH]

    def test_prompt_only_edit_retains_queue_position(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(prompt="first prompt {ACTIVE_PLAN_PATH}"),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            first_ticket = _run_json(run_dir)["execution_resource_wait"][
                "ticket"
            ]
            assert first_ticket == 2
            # A prompt-only edit re-prepares the turn but must retain the
            # queue position (same invocation id, same ticket).
            config_path.write_text(
                _aflow_toml(prompt="second prompt {ACTIVE_PLAN_PATH}"),
                encoding="utf-8",
            )
            time.sleep(0.4)
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["ticket"] == first_ticket
            assert calls == []
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]

    def test_file_backed_prompt_edit_rebuilds_payload_keeping_ticket(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        """A changed ``file://`` prompt body is caught at final validation.

        Editing only the referenced file leaves the configuration pair
        revision and the raw prompt text unchanged, so mid-wait polling
        stays cheap and undisturbed.  The final prelaunch validation must
        detect the new body, rebuild the payload within the same
        reservation (ticket retained, nothing cancelled), and dispatch the
        new text.
        """
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(prompt="file://prompts/worker.md"),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        prompts_dir = config_path.parent / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompts_dir / "worker.md"
        prompt_file.write_text("first file prompt", encoding="utf-8")
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        prompts: list[str] = []
        observer = _Observer()

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            model = argv[argv.index("--model") + 1]
            calls.append(model)
            prompts.append(str(kwargs.get("input", "")))
            plan_path.write_text(
                _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner, observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {})
                    .get("ticket")
                    == 2
                )
            )
            prompt_file.write_text("second file prompt", encoding="utf-8")
            # Only the referenced file changed: the queued wait keeps its
            # ticket (the cheap revision check never reads the file).
            time.sleep(0.3)
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["ticket"] == 2
            assert calls == []
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        assert len(prompts) == 1
        assert "second file prompt" in prompts[0]
        assert "first file prompt" not in prompts[0]
        acquired = [
            event for event in observer.resource_events()
            if event.phase == "acquired"
        ]
        assert {event.ticket for event in acquired} == {2}, (
            "the payload-only rebuild must keep the queue ticket"
        )
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" not in phases
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None

    def test_two_file_prompt_boundary_edit_rebuilds_payload_keeping_ticket(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        """A boundary-preserving two-file body swap is still detected.

        Two ``file://`` step prompts whose bodies change from ``ab``/``c``
        to ``a``/``bc`` keep the configuration pair, the raw prompt texts,
        and even the concatenated referenced bytes unchanged, yet
        ``render_step_prompts`` renders different joined text.  The final
        prelaunch validation distinguishes the per-file bodies, rebuilds
        the payload under the retained ticket, and dispatches the newly
        joined text exactly once.
        """
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                prompt="file://prompts/worker.md",
                prompt2="file://prompts/extra.md",
            ),
            workflows_text=_workflows_toml(
                steps="work_only", work_prompts='"p", "q"'
            ),
        )
        prompts_dir = config_path.parent / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        first = prompts_dir / "worker.md"
        second = prompts_dir / "extra.md"
        first.write_text("ab", encoding="utf-8")
        second.write_text("c", encoding="utf-8")
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        prompts: list[str] = []
        observer = _Observer()

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            model = argv[argv.index("--model") + 1]
            calls.append(model)
            prompts.append(str(kwargs.get("input", "")))
            plan_path.write_text(
                _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner, observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {})
                    .get("ticket")
                    == 2
                )
            )
            # Same total bytes, different per-file boundary; the
            # configuration pair itself is untouched.
            first.write_text("a", encoding="utf-8")
            second.write_text("bc", encoding="utf-8")
            # Only the referenced files changed: the queued wait keeps its
            # ticket (the cheap revision check never reads the files).
            time.sleep(0.3)
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["ticket"] == 2
            assert calls == []
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        assert len(prompts) == 1
        assert "a\n\nbc" in prompts[0], prompts[0]
        assert "ab\n\nc" not in prompts[0]
        acquired = [
            event for event in observer.resource_events()
            if event.phase == "acquired"
        ]
        assert {event.ticket for event in acquired} == {2}, (
            "the payload-only rebuild must keep the queue ticket"
        )
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" not in phases
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None

    def test_missing_prompt_file_fails_before_launch(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        """A deleted ``file://`` prompt body fails the turn before launch.

        The mid-wait revision check never reads the referenced file; the
        final prelaunch validation fails closed with an actionable
        pre-turn failure, zero provider calls, and a clean journal.
        """
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(prompt="file://prompts/worker.md"),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        prompts_dir = config_path.parent / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompts_dir / "worker.md"
        prompt_file.write_text("first file prompt", encoding="utf-8")
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {})
                    .get("ticket")
                    == 2
                )
            )
            prompt_file.unlink()
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" in result, "the missing prompt file must fail the turn"
        assert calls == [], "no model call may happen without the prompt body"
        assert not thread.is_alive()
        run = _run_json(run_dir)
        assert run["status"] == "failed"
        assert run["turns_completed"] == 0
        assert "prompt file cannot be read" in run.get("failure_reason", "")
        assert run.get("execution_resource_wait") is None
        journal = json.loads(
            (tmp_path / "resource-store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_unrelated_config_edit_keeps_waiting_under_pair_lock(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, workflows_path = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            # An unrelated edit lands while the configuration pair lock is
            # held: the controller cannot revalidate and must keep waiting
            # on the same route.
            lock_path = config_path.parent / ".aflow-config-pair.lock"
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                workflows_path.write_text(
                    _workflows_toml(steps="work_only").replace(
                        'team = "base"', 'team = "base"\n# unrelated edit'
                    ),
                    encoding="utf-8",
                )
                time.sleep(0.3)
                assert calls == []
                waiting = _run_json(run_dir)["execution_resource_wait"]
                assert waiting["resource"] == resource
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]

    def test_journal_lock_contention_yields_and_then_admits(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        calls: list[str] = []
        lock_path = tmp_path / "resource-store" / f"{resource}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            thread, result = _launch(
                tmp_path, config_path, plan_path,
                _worker_runner(plan_path, calls),
            )
            run_dir = _first_run_dir(tmp_path)
            # The broker journal is contended: the controller yields without
            # a ticket and without any model call.
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["ticket"] is None
            assert calls == []
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        thread.join(timeout=20)
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        assert _run_json(run_dir).get("execution_resource_wait") is None

    def test_retried_enqueue_publishes_allocated_ticket_after_lock_contention(
        self, tmp_path: Path, resource_store: ExecutionResourceStore,
    ) -> None:
        resource = execution_resource_key("codex", "retry-ticket", None)
        foreign, foreign_invocation = _occupy(resource_store, resource)
        controller = resource_store.current_controller_identity()
        assert controller is not None
        spec = ClaimSpec(str(tmp_path), "run", "retry", "turn", "worker", "codex.base")
        waiting: list[tuple[str | None, int | None]] = []
        lock_fd = os.open(tmp_path / "resource-store" / f"{resource}.lock", os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def on_waiting(reason: str | None, ticket: int | None) -> None:
            waiting.append((reason, ticket))
            if ticket is None:
                assert reason == "lock_contended"
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

        def release_after_ticket_published(_seconds: float) -> None:
            # Enqueue recovered while the foreign owner still holds the
            # resource: the new ticket must replace the provisional record.
            assert waiting == [("lock_contended", None), (None, 2)]
            _release(resource_store, resource, foreign, foreign_invocation)

        admission = ExecutionResourceAdmission(
            resource_store, sleeper=release_after_ticket_published, clock=lambda: 0.0,
        )
        try:
            lease = admission.admit(
                resource=resource, spec=spec, controller=controller,
                stop_check=lambda: None, revalidate=lambda final: UNCHANGED,
                on_waiting=on_waiting,
            )
            assert lease is not None
            assert waiting == [("lock_contended", None), (None, 2)]
            lease.complete()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            resource_store.cancel(resource, spec.invocation_id, controller)
            _release(resource_store, resource, foreign, foreign_invocation, required=False)

    def test_stale_post_acquire_preparation_reprepares(
        self, tmp_path: Path, resource_store: ExecutionResourceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        observer = _Observer()
        # A wide control interval means the only mid-wait control service is
        # the first one (before the live edit), so the route change is only
        # visible to the final post-acquire check.
        monkeypatch.setattr(
            "aflow.workflow._execution_resource_admission_options",
            lambda: {"poll_interval": 0.02, "control_interval": 30.0},
        )
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            # The edit lands, then the resource frees: the controller
            # acquires, then the final check finds the stale route and must
            # release the unlaunched reservation and re-prepare.
            config_path.write_text(
                _aflow_toml(worker_model=_WORKER_HIGH), encoding="utf-8"
            )
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_HIGH]
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" in phases
        journal = json.loads(
            (tmp_path / "resource-store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_queued_step_role_change_reprepares_on_new_route(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        """Changing the queued workflow step's role in workflows.toml re-prepares.

        The admission target is the *current* workflow step resolved from
        the loaded configuration, not the captured prepare snapshot: a role
        change moves the route to the new role's resource and the old claim
        is cancelled (no retention).
        """
        config_path, workflows_path = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        worker_resource = _resource_for(config_path, "codex.base")
        reviewer_resource = _resource_for(
            config_path, "codex.reviewer", "workflow.live.steps.review"
        )
        assert worker_resource != reviewer_resource
        worker_identity, worker_invocation = _occupy(
            resource_store, worker_resource
        )
        reviewer_identity, reviewer_invocation = _occupy(
            resource_store, reviewer_resource, role="reviewer"
        )
        calls: list[str] = []

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            model = argv[argv.index("--model") + 1]
            calls.append(model)
            plan_path.write_text(
                _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            first = _run_json(run_dir)["execution_resource_wait"]
            assert first["resource"] == worker_resource
            # The live step role moves to the reviewer: the controller must
            # cancel the worker claim and re-prepare on the new route.
            workflows_path.write_text(
                _workflows_toml(
                    steps="work_only",
                    work_role="reviewer",
                ),
                encoding="utf-8",
            )
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {})
                    .get("resource")
                    == reviewer_resource
                )
            )
            second = _run_json(run_dir)["execution_resource_wait"]
            assert second["invocation_id"] != first["invocation_id"], (
                "a route change re-prepares a fresh invocation"
            )
            assert second["ticket"] == 2
            assert second["role"] == "reviewer"
            assert second["selector"] == "codex.reviewer"
            assert calls == [], "no model call may happen while waiting"
            _release(
                resource_store, worker_resource,
                worker_identity, worker_invocation,
            )
            _release(
                resource_store, reviewer_resource,
                reviewer_identity, reviewer_invocation,
            )
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, worker_resource,
                worker_identity, worker_invocation, required=False,
            )
            _release(
                resource_store, reviewer_resource,
                reviewer_identity, reviewer_invocation, required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_REVIEWER], "the worker was never admitted"
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None
        journal = json.loads(
            (tmp_path / "resource-store"
             / f"{worker_resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_queued_team_edit_moves_route_to_new_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        """A workflows.toml team edit re-derives the effective team.

        The queued turn was prepared under the workflow's team ``base``.
        Editing the workflow's team to ``high`` must revalidate through the
        boundary reload's precedence (not the captured team name), cancel
        the old claim, and reprepare at the tail of the high resource —
        only the high model may ever be dispatched.
        """
        config_path, workflows_path = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        base_resource = _resource_for(config_path, "codex.base")
        high_resource = _resource_for(
            config_path, "codex.base-high", "workflow.live.steps.work"
        )
        assert base_resource != high_resource
        base_identity, base_invocation = _occupy(resource_store, base_resource)
        high_identity, high_invocation = _occupy(resource_store, high_resource)
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {})
                    .get("resource")
                    == base_resource
                )
            )
            first = _run_json(run_dir)["execution_resource_wait"]
            assert first["selector"] == "codex.base"
            # Live team edit: the effective team follows the refreshed
            # workflow, so the route moves to the (busy) high resource.
            workflows_path.write_text(
                _workflows_toml(steps="work_only").replace(
                    'team = "base"', 'team = "high"'
                ),
                encoding="utf-8",
            )
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {})
                    .get("resource")
                    == high_resource
                )
            )
            second = _run_json(run_dir)["execution_resource_wait"]
            assert second["invocation_id"] != first["invocation_id"], (
                "a team-driven route change re-prepares a fresh invocation"
            )
            assert second["selector"] == "codex.base-high"
            assert second["ticket"] == 2, (
                "the new claim joins the destination tail"
            )
            assert calls == [], "no model call may happen while waiting"
            _release(
                resource_store, base_resource,
                base_identity, base_invocation,
            )
            _release(
                resource_store, high_resource,
                high_identity, high_invocation,
            )
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, base_resource,
                base_identity, base_invocation, required=False,
            )
            _release(
                resource_store, high_resource,
                high_identity, high_invocation, required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_HIGH], "only the high model may be dispatched"
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" in phases, "the old claim must be cancelled"
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None
        journal = json.loads(
            (tmp_path / "resource-store" / f"{base_resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_explicit_run_team_keeps_route_through_team_edit(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        """An explicit run team outranks a workflows.toml team edit.

        The turn is prepared under the explicit run team ``base``.  While
        queued, the workflow's team changes to ``high``; because the run
        team is explicit, the effective route must not move: the claim and
        its ticket are kept and only the base model is dispatched.
        """
        config_path, workflows_path = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        base_resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, base_resource)
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), observer,
            config_kwargs={"team": "base"},
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {})
                    .get("ticket")
                    == 2
                )
            )
            workflows_path.write_text(
                _workflows_toml(steps="work_only").replace(
                    'team = "base"', 'team = "high"'
                ),
                encoding="utf-8",
            )
            # The lower-priority workflow edit does not change the
            # effective target: the ticket and resource are kept.
            time.sleep(0.3)
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == base_resource
            assert waiting["ticket"] == 2
            assert calls == []
            _release(
                resource_store, base_resource, identity, invocation_id,
            )
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, base_resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" not in phases
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None

    def test_final_check_pair_lock_contention_retries_then_admits(
        self, tmp_path: Path, resource_store: ExecutionResourceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A lock-contended final check keeps the reservation and retries.

        The final post-acquire check must perform a real lock-consistent
        load even when the revision tuple is unchanged.  While that load
        is contended the controller keeps the unlaunched reservation and
        retries (no launch, no model call); once the load succeeds it
        validates and proceeds.
        """
        import aflow.workflow as workflow_module
        from aflow.live_config import LiveConfigPairLockBusy

        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        foreign_identity, foreign_invocation = _occupy(
            resource_store, resource
        )
        calls: list[str] = []

        real_load = workflow_module.load_live_config
        state: dict[str, object] = {
            "controller_invocation": None,
            "contended": False,
            "resolved": False,
        }

        def flaky_load(config_path_arg, *args, **kwargs):
            if (
                kwargs.get("nonblocking")
                and state["contended"]
                and not state["resolved"]
                and state["controller_invocation"] is not None
            ):
                owner = _journal_owner(tmp_path, resource)
                if (
                    owner is not None
                    and owner.get("invocation_id")
                    == state["controller_invocation"]
                ):
                    raise LiveConfigPairLockBusy(
                        "injected pair lock contention"
                    )
            return real_load(config_path_arg, *args, **kwargs)

        monkeypatch.setattr(workflow_module, "load_live_config", flaky_load)
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # The controller queues behind the foreign owner; learn its
            # invocation from the durable wait record.
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            state["controller_invocation"] = (
                _run_json(run_dir)["execution_resource_wait"]["invocation_id"]
            )
            # Contend the final check, then free the resource: the
            # controller acquires, the final load fails, and the reserved
            # claim must be kept while it retries.
            state["contended"] = True
            _release(
                resource_store, resource,
                foreign_identity, foreign_invocation,
            )
            assert _wait_until(
                lambda: (
                    (_journal_owner(tmp_path, resource) or {}).get(
                        "invocation_id"
                    )
                    == state["controller_invocation"]
                )
            )
            time.sleep(0.3)
            assert calls == [], (
                "no launch may be decided while the pair is contended"
            )
            assert _run_json(run_dir).get("execution_resource_wait") is None, (
                "a held reservation is not a wait"
            )
            assert (
                (_journal_owner(tmp_path, resource) or {}).get("invocation_id")
                == state["controller_invocation"]
            ), "the reservation must be retained across retries"
            # Resolve the contention: the next final check validates and
            # the run launches and completes.
            state["resolved"] = True
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource,
                foreign_identity, foreign_invocation, required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None
        journal = json.loads(
            (tmp_path / "resource-store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_launch_intent_failure_fails_turn_before_start(
        self, tmp_path: Path, resource_store: ExecutionResourceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed durable launch intent fails the turn with zero accounting.

        The claim is durably "launching" before any turn, repair, or
        recovery accounting may exist.  A confirmed non-launch releases the
        reserved claim and the run fails at the pre-turn boundary.
        """
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        calls: list[str] = []

        def broken_mark_launching(self: ExecutionLease) -> None:
            raise ResourceLeaseError("mark_launching", "injected_failure")

        monkeypatch.setattr(
            ExecutionLease, "mark_launching", broken_mark_launching
        )
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        thread.join(timeout=20)
        run_dir = _first_run_dir(tmp_path)
        assert "error" in result, "the pre-turn failure must surface"
        assert calls == [], "no model call may happen before launch intent"
        run = _run_json(run_dir)
        assert run["status"] == "failed"
        assert run["turns_completed"] == 0
        assert run.get("execution_resource_wait") is None
        assert not (run_dir / "turns").is_dir() or not any(
            (run_dir / "turns").iterdir()
        ), "no turn artifacts may exist for a pre-turn failure"
        journal = json.loads(
            (tmp_path / "resource-store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_same_resource_subsequent_round_requeues(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        calls: list[str] = []
        round_one_done = threading.Event()
        proceed = threading.Event()

        def hooks(model: str, count: int) -> None:
            if count == 1:
                round_one_done.set()
                proceed.wait(timeout=20)

        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(
                plan_path, calls, complete_after=2, hooks=hooks
            ),
        )
        run_dir = _first_run_dir(tmp_path)
        identity, invocation_id = None, None
        try:
            assert _wait_until(round_one_done.is_set)
            # A foreign controller queues while round one is in flight; when
            # round one releases it takes the resource, and the second round
            # must queue behind it.
            identity, invocation_id = _foreign_enqueue(resource_store, resource)
            proceed.set()
            _foreign_acquire(resource_store, resource, identity, invocation_id)
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {}).get("ticket") == 3
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["ticket"] == 3
            assert calls == [_WORKER_BASE]
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            proceed.set()
            if identity is not None and invocation_id is not None:
                _release(
                    resource_store, resource, identity, invocation_id,
                    required=False,
                )
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE, _WORKER_BASE]
        assert _run_json(run_dir).get("execution_resource_wait") is None

    def test_resume_starts_fresh_without_stale_wait(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.base")
        identity, invocation_id = _occupy(resource_store, resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            assert (
                _run_json(run_dir)["execution_resource_wait"]["ticket"] == 2
            )
            _stop_run(run_dir)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert calls == []
        stopped = _run_json(run_dir)
        assert stopped["status"] == "owner_stopped"

        # Resume with fresh state: the stale ticket must not be inherited.
        fields = manager_resume_fields(stopped)
        scope = fields["active_implementation_scope"]
        envelope_bytes = (
            (run_dir / str(scope.envelope_artifact_path)).read_bytes()
            if scope is not None
            else None
        )
        resume = _mark_validated_resume_context(
            ResumeContext(
                resumed_from_run_id=run_dir.name,
                feature_branch=None,
                worktree_path=None,
                main_branch=None,
                setup=(),
                teardown=(),
                active_plan_path=plan_path,
                interrupted_step_name="work",
                effective_max_turns=8,
                scope_envelope_bytes=envelope_bytes,
                scope_evidence_artifact_bytes=(
                    load_scope_evidence_for_resume(
                        run_dir, scope, envelope_bytes
                    )
                    if scope is not None and envelope_bytes is not None
                    else None
                ),
                **fields,
            )
        )
        calls.clear()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls), resume=resume,
        )
        assert _wait_until(lambda: len(_run_dirs(tmp_path)) >= 2)
        resumed_run_dir = _last_run_dir(tmp_path)
        # The resource is free now: the resumed controller acquires with a
        # fresh invocation and completes without any stale wait.
        thread.join(timeout=20)
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE]
        run = _run_json(resumed_run_dir)
        assert run.get("execution_resource_wait") is None
        assert run["turns_completed"] == 1

    @pytest.mark.parametrize("manager_enabled", [False, True])
    @pytest.mark.parametrize("threshold", [0, 1])
    def test_busy_primary_does_not_advance_worker_chain(
        self,
        tmp_path: Path,
        resource_store: ExecutionResourceStore,
        manager_enabled: bool,
        threshold: int,
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(manager_enabled=manager_enabled),
            workflows_text=_workflows_toml(
                steps="work_only",
                upgrade_after_repairs=threshold,
                manager_enabled=manager_enabled,
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        base_resource = _resource_for(config_path, "codex.base")
        high_resource = _resource_for(
            config_path, "codex.base-high", "workflow.live.steps.work"
        )
        identity, invocation_id = _occupy(resource_store, base_resource)
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path, _worker_runner(plan_path, calls)
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            # The busy primary stays the primary: no upgrade selection.
            assert waiting["resource"] == base_resource
            time.sleep(0.3)
            assert calls == [], (
                "a busy primary must never call an upgraded worker"
            )
            _stop_run(run_dir)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, base_resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        run = _run_json(run_dir)
        assert run["status"] == "owner_stopped"
        assert run.get("implementation_attempts") in (None, {})
        assert run.get("review_rejection_history") in (None, [])
        assert high_resource is not None
        assert not (
            tmp_path / "resource-store" / f"{high_resource}.json"
        ).exists(), "the upgraded resource must never be touched"

    @pytest.mark.parametrize("manager_enabled", [False, True])
    @pytest.mark.parametrize("threshold", [0, 1])
    def test_busy_review_selected_upgrade_does_not_skip_chain_edge(
        self,
        tmp_path: Path,
        resource_store: ExecutionResourceStore,
        manager_enabled: bool,
        threshold: int,
    ) -> None:
        # A real authoritative rejection selects the upgraded worker under
        # the existing repair policy.  The selected upgrade is busy while
        # the base and the later chain edge stay idle: the controller must
        # wait for the selected upgrade, never call it, and never skip to
        # the next edge.  Queue observations must not change the
        # attempt/rejection/repair/upgrade history.
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(manager_enabled=manager_enabled),
            workflows_text=_workflows_toml(
                steps="work_review",
                upgrade_after_repairs=threshold,
                manager_enabled=manager_enabled,
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        high_resource = _resource_for(
            config_path, "codex.base-high", "workflow.live.steps.work"
        )
        top_resource = _resource_for(
            config_path, "codex.base-top", "workflow.live.steps.work"
        )
        assert high_resource is not None and top_resource is not None
        # Occupy only the would-be selected upgrade; base and top stay free.
        identity, invocation_id = _occupy(resource_store, high_resource)
        calls: list[str] = []
        version = 0

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal version
            model = argv[argv.index("--model") + 1]
            prompt = str(kwargs.get("input", ""))
            if model == _MANAGER_MODEL and "schema_version" in prompt:
                calls.append("manager")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "schema_version": 1,
                            "action": "continue",
                            "reason": "synthetic continue",
                            "next_step_notes": [],
                            "stop_report": None,
                        }
                    ),
                    "",
                )
            calls.append(model)
            if model == _REVIEWER:
                version += 1
                (tmp_path / f"plan-cp01-v{version:02d}.md").write_text(
                    _REPAIR_PLAN, encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    argv, 0, "synthetic rejection", ""
                )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(tmp_path, config_path, plan_path, runner)
        run_dir = _first_run_dir(tmp_path)
        try:
            # The authoritative rejections select the upgraded worker and
            # the controller reaches its admission gate on the busy upgrade.
            # Free resources clear their transient wait record on acquire,
            # so wait specifically for the busy upgrade's record.
            assert _wait_until(
                lambda: (
                    (_run_json(run_dir).get("execution_resource_wait") or {}).get(
                        "resource"
                    )
                    == high_resource
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == high_resource
            time.sleep(0.3)
            assert _WORKER_HIGH not in calls, (
                "a busy selected upgrade must never be invoked"
            )
            assert _WORKER_TOP not in calls, (
                "the later chain edge must never be invoked"
            )
            _stop_run(run_dir)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, high_resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        run = _run_json(run_dir)
        assert run["status"] == "owner_stopped"
        expected_rejections = 1 if threshold == 0 else 2
        assert len(run["review_rejection_history"]) == expected_rejections
        rejection = run["review_rejection_history"][-1]
        attempts = run["implementation_attempts"][rejection["scope_id"]]
        selectors = [row["selector"] for row in attempts]
        assert selectors == ["codex.base"] * expected_rejections, (
            "no new attempt may be recorded while waiting"
        )
        assert (tmp_path / "resource-store" / f"{high_resource}.json").exists()
        assert not (
            tmp_path / "resource-store" / f"{top_resource}.json"
        ).exists(), "the later chain edge must never be touched"

    def test_authoritative_review_selects_next_worker(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(
                steps="work_review", upgrade_after_repairs=0
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        calls: list[str] = []

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            model = argv[argv.index("--model") + 1]
            calls.append(model)
            if model == _REVIEWER:
                if not (tmp_path / "plan-cp01-v01.md").exists():
                    (tmp_path / "plan-cp01-v01.md").write_text(
                        _REPAIR_PLAN, encoding="utf-8"
                    )
                    return subprocess.CompletedProcess(
                        argv, 0, "synthetic rejection", ""
                    )
                plan_path.write_text(
                    _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    argv, 0, "synthetic approval", ""
                )
            if model == _WORKER_HIGH:
                plan_path.write_text(
                    _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
                )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner
        )
        thread.join(timeout=30)
        assert "error" not in result, result.get("error")
        assert calls == [_WORKER_BASE, _REVIEWER, _WORKER_HIGH, _REVIEWER]
        run = _run_json(_run_dirs(tmp_path)[0])
        assert len(run["review_rejection_history"]) == 1
        assert run["turns_completed"] == 4
        assert run.get("execution_resource_wait") is None

    def test_final_reprepare_releases_unlaunched_reservation(
        self, tmp_path: Path
    ) -> None:
        from aflow.config import execution_resource_key

        store = ExecutionResourceStore(root=tmp_path / "store")
        resource = execution_resource_key("codex", "model-a", "high")
        controller = store.current_controller_identity()
        assert controller is not None
        spec = ClaimSpec(
            project_root="/p",
            run_id="run",
            invocation_id="inv",
            kind="turn",
            role="worker",
            selector="codex.base",
        )
        calls: list[str] = []
        admission = ExecutionResourceAdmission(
            store,
            poll_interval=0.01,
            control_interval=0.02,
            sleeper=lambda _seconds: None,
            clock=lambda: 0.0,
        )

        def revalidate(final: bool) -> RevalidationVerdict:
            if final:
                calls.append("final")
                return RevalidationVerdict("reprepare", None, "route_changed")
            calls.append("mid")
            return UNCHANGED

        with pytest.raises(TurnReprepareRequired) as excinfo:
            admission.admit(
                resource=resource,
                spec=spec,
                controller=controller,
                stop_check=lambda: None,
                revalidate=revalidate,
                on_waiting=lambda reason, ticket: calls.append(
                    f"waiting:{ticket}"
                ),
                on_acquired=lambda ticket: calls.append("acquired"),
                on_cancelled=lambda reason: calls.append(f"cancelled:{reason}"),
            )
        assert excinfo.value.retained_invocation_id is None
        assert "acquired" not in calls, "no launch may be reported"
        assert calls[-1] == "cancelled:route_changed"
        journal = json.loads(
            (tmp_path / "store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    @pytest.mark.parametrize("stage", ["queued", "reserved"])
    @pytest.mark.parametrize("cause", ["route_change", "owner_stop", "config_error"])
    def test_withdrawal_waits_for_lock_before_control_exit(self, tmp_path, stage, cause):
        store = ExecutionResourceStore(root=tmp_path / "store")
        resource = execution_resource_key("codex", "withdrawal", None)
        controller = store.current_controller_identity()
        assert controller is not None
        spec = ClaimSpec(str(tmp_path), "run", "obsolete", "turn", "worker", "codex.base")
        peer = replace(spec, invocation_id="peer", run_id="peer-run")
        held = []
        hooks = []
        clock = [0.0]
        triggered = [False]

        class ControlExit(Exception):
            pass

        def trigger():
            triggered[0] = True
            assert store.enqueue(resource, peer, controller).state == "queued"
            fd = (tmp_path / "store" / f"{resource}.lock").open("a+")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)

        def stop_check():
            if cause != "owner_stop" or triggered[0]:
                return
            journal = json.loads((tmp_path / "store" / f"{resource}.json").read_text())
            if (journal["owner"] is not None) == (stage == "reserved"):
                trigger()
                raise ControlExit("owner_stop")

        def revalidate(final):
            if cause == "owner_stop" or final != (stage == "reserved"):
                return UNCHANGED
            trigger()
            if cause == "config_error":
                raise ControlExit("config_error")
            return RevalidationVerdict("reprepare", None, "route_changed")

        def unlock(seconds):
            assert held and hooks == [], "no cancellation may precede confirmed withdrawal"
            clock[0] += seconds
            fd = held.pop()
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

        admission = ExecutionResourceAdmission(store, clock=lambda: clock[0], sleeper=unlock)
        try:
            with pytest.raises(TurnReprepareRequired if cause == "route_change" else ControlExit):
                admission.admit(resource=resource, spec=spec, controller=controller,
                                stop_check=stop_check, revalidate=revalidate,
                                on_cancelled=hooks.append)
            assert clock[0] > 0, "the withdrawal must encounter the real held flock"
            journal = json.loads((tmp_path / "store" / f"{resource}.json").read_text())
            assert journal["owner"] is None
            assert [c["invocation_id"] for c in journal["queue"]] == ["peer"]
            assert hooks == (["route_changed"] if cause == "route_change" else
                             ["stop_before_launch"] if stage == "reserved" and cause == "owner_stop" else
                             ["config_failure_before_launch"] if stage == "reserved" else [])
            # The original controller is alive: reconciliation cannot hide
            # the defect, and the peer must acquire immediately after cleanup.
            assert store.try_acquire(resource, "peer", controller).state == "acquired"
            assert store.cancel(resource, "peer", controller).state == "cancelled"
        finally:
            for fd in held:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()

    @pytest.mark.parametrize("stage", ["queued", "reserved"])
    @pytest.mark.parametrize("failure", ["lock_contended", "io_failure", "not_cancellable"])
    def test_failed_withdrawal_never_reports_cancel_or_reprepare(self, tmp_path, monkeypatch, stage, failure):
        store = ExecutionResourceStore(root=tmp_path / "store")
        resource = execution_resource_key("codex", "withdrawal-failure", None)
        controller = store.current_controller_identity()
        spec = ClaimSpec(str(tmp_path), "run", "obsolete", "turn", "worker", "codex.base")
        hooks = []
        clock = [0.0]

        def revalidate(final):
            if final != (stage == "reserved"):
                return UNCHANGED
            monkeypatch.setattr(store, "cancel", lambda *args: Outcome(
                "contended" if failure == "lock_contended" else "rejected", reason=failure))
            return RevalidationVerdict("reprepare", None, "route_changed")

        admission = ExecutionResourceAdmission(store, clock=lambda: clock[0],
            sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            contention_deadline_seconds=0.03)
        with pytest.raises(ResourceLeaseError) as error:
            admission.admit(resource=resource, spec=spec, controller=controller,
                stop_check=lambda: None, revalidate=revalidate, on_cancelled=hooks.append)
        assert error.value.stage == "withdrawal" and error.value.reason == failure
        assert hooks == []
        journal = json.loads((tmp_path / "store" / f"{resource}.json").read_text())
        claim = journal["owner"] if stage == "reserved" else journal["queue"][0]
        assert claim["invocation_id"] == "obsolete" and claim["status"] == stage


# ---------------------------------------------------------------------------
# Final-only paired controller matrix (checkpoint 7 / final verification)
# ---------------------------------------------------------------------------


_IPC_HARNESS = '''\
import json, os, socket, sys, time
from pathlib import Path
from aflow.process_identity import process_birth_identity
sys.stdin.read()
root = Path.cwd()
counter = root / '.harness-round'
round_number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(round_number))
settings = json.loads(os.environ['AFLOW_TEST_IPC'])
model = sys.argv[1]
with socket.create_connection(tuple(settings['address']), timeout=30) as sock:
    with sock.makefile('rwb') as wire:
        message = {'round': round_number, 'model': model, 'pid': os.getpid(),
                   'birth': process_birth_identity(os.getpid()), 'group': os.getpgid(0),
                   'effort': settings['effort'], 'phase_at': time.monotonic_ns()}
        wire.write((json.dumps(message) + '\\n').encode()); wire.flush()
        assert wire.readline() == b'release\\n'
        failed = settings['fail_first'] and round_number == 1
        if round_number == settings['rounds'] and not failed:
            plan = root / settings['plan_name']
            plan.write_text(plan.read_text().replace('[ ]', '[x]'))
        wire.write(b'end\\n'); wire.flush()
        assert wire.readline() == b'ack\\n'
if failed:
    print('synthetic retryable failure', file=sys.stderr)
    sys.exit(1)
print('synthetic local result')
'''


def _ipc_controller(root, config_path, store_root, label, gates, trace, rounds, fail_first):
    """Real controller and real harness children; only the model is local."""
    import aflow.workflow as workflow_module

    store = ExecutionResourceStore(root=Path(store_root))
    workflow_module._default_execution_resource_store = lambda: store
    workflow_module._execution_resource_admission_options = lambda: {"poll_interval": 0.02, "control_interval": 0.05}
    # Spawn does not inherit pytest's ordinary synthetic-publication fixture.
    workflow_module.publish_completed_run = lambda *args, **kwargs: None
    trace.put(("identity", label, 0, {
        "workflow_source": str(Path(workflow_module.__file__).resolve()),
        "config_root": str(Path(config_path).resolve()),
        "store_root": str(Path(store_root).resolve()),
    }, os.getpid()))
    plan = Path(root) / f"plan-{label}.md"
    children = {}

    class GateHandler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(30)
            message = json.loads(self.rfile.readline())
            number = message["round"]
            children[message["pid"]] = message
            trace.put(("start", label, number, message["model"], message["pid"], message))
            assert gates[number - 1].wait(30), f"unreleased gate {label}:{number}"
            self.wfile.write(b"release\n"); self.wfile.flush()
            assert self.rfile.readline() == b"end\n"
            trace.put(("end", label, number, message["model"], message["pid"],
                       {**message, "phase_at": time.monotonic_ns()}))
            self.wfile.write(b"ack\n"); self.wfile.flush()

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), GateHandler)
    server.daemon_threads = True
    bridge = threading.Thread(target=server.serve_forever, daemon=True)
    bridge.start()

    class LocalAdapter(CodexAdapter):
        # Discovery must never invoke an installed provider binary. Harness
        # selection/key is still codex; only this test adapter's executable
        # and preflight are local, with the normal Popen dispatch untouched.
        name = "local-ipc-test"

        def build_invocation(self, **kwargs):
            invocation = super().build_invocation(**kwargs)
            return replace(invocation, argv=(sys.executable, str(Path(root) / "fake-harness.py"), kwargs["model"]),
                env={"PYTHONPATH": str(Path(workflow_module.__file__).resolve().parent.parent),
                     "AFLOW_TEST_IPC": json.dumps({"address": server.server_address,
                                                  "rounds": rounds, "fail_first": fail_first,
                                                  "plan_name": plan.name, "effort": kwargs.get("effort")})})

    original_complete = store.record_completion

    def record_completion(resource, invocation_id, controller, **kwargs):
        owner = json.loads((Path(store_root) / f"{resource}.json").read_text())["owner"]
        if owner and owner["child_pid"] is not None and not kwargs.get("unconfirmed"):
            pid = owner["child_pid"]
            # The owned child must have been wait()ed by the production seam.
            with pytest.raises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            assert process_liveness(pid) == "absent"
            message = children[pid]
            assert (owner["child_birth"], owner["process_group"]) == (message["birth"], message["group"])
            trace.put(("reaped", label, message["round"], message["model"], pid,
                       {**message, "phase_at": time.monotonic_ns()}))
        return original_complete(resource, invocation_id, controller, **kwargs)

    store.record_completion = record_completion

    try:
        result = run_workflow(
            ControllerConfig(repo_root=Path(root), plan_path=plan, max_turns=8),
            load_workflow_config(Path(config_path)), "live", config_dir=Path(config_path),
            snapshot_config=False, adapter=LocalAdapter(),
            preflight_probe=NoOpHarnessPreflightProbe(),
        )
        trace.put(("done", label, len(children), str(result), os.getpid()))
    except BaseException as exc:
        trace.put(("error", label, len(children), repr(exc), os.getpid()))
        raise
    finally:
        server.shutdown()
        server.server_close()
        bridge.join(2)


class _IPCPair:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.context = multiprocessing.get_context("spawn")
        self.trace = self.context.Queue()
        self.events = []
        self.processes = {}
        self.gates = {}
        self.trees = {}
        self.repo = tmp_path / "primary-repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("Disposable paired-controller fixture.\n")
        (self.repo / ".gitignore").write_text(".aflow/\nconfig/\nfake-harness.py\n.harness-round\n")
        self._git("init", "-q")
        self._git("add", "-A")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def launch(self, label, *, role="worker", model="shared-model", rounds=1, retry=False, effort=None, review=False):
        tree = self.root / label
        self._git("worktree", "add", "-q", "-b", f"fixture-{label}", str(tree))
        text = _aflow_toml(worker_model=model, reviewer_model="independent-review" if review else model)
        # The second controller uses a different marked profile alias.
        if label == "B":
            text = text.replace(f'{role} = "codex.{"base" if role == "worker" else "reviewer"}"', f'{role} = "codex.alias"')
            text += f'\n[harness.codex.profiles.alias]\nmodel = "{model}"\nexclusive = true\n'
        if effort is not None:
            text = text.replace(f'model = "{model}"\n', f'model = "{model}"\neffort = "{effort}"\n')
        if retry:
            text += '\n[error_handling.harness_error_recovery]\nmax_consecutive_recoveries = 1\n[[error_handling.harness_error_recovery.rules]]\nmatch = ["synthetic retryable failure"]\naction = "retry_same_team_after_delay"\ndelay_seconds = 0\n'
        config, _ = _write_split_config(home_dir=tree, aflow_text=text,
                                        workflows_text=_workflows_toml(steps="work_review" if review else "work_only", work_role=role,
                                                                      upgrade_after_repairs=0 if review else None))
        (tree / f"plan-{label}.md").write_text(_VALID_PLAN)
        (tree / "fake-harness.py").write_text(_IPC_HARNESS)
        self.gates[label] = [self.context.Event() for _ in range(rounds)]
        proc = self.context.Process(target=_ipc_controller, args=(str(tree), str(config),
            str(self.root / "shared-ipc-store"), label, self.gates[label], self.trace, rounds, retry))
        self.trees[label] = tree
        self.processes[label] = proc
        proc.start()

    def observe(self, event, label, round_number=1):
        deadline = time.monotonic() + 25
        target = (event, label, round_number)
        while not any(e[:3] == target for e in self.events):
            assert time.monotonic() < deadline, self.events
            try:
                entry = self.trace.get(timeout=0.1)
                self.events.append(entry)
                assert entry[0] != "error", entry
            except queue.Empty:
                pass
        if event == "start":
            entry = next(e for e in self.events if e[:3] == target)
            assert entry[4] != self.processes[label].pid and entry[5]["birth"] is not None
            def bound():
                for journal in (self.root / "shared-ipc-store").glob("*.json"):
                    owner = json.loads(journal.read_text())["owner"]
                    if owner and owner["child_pid"] == entry[4]:
                        return owner["status"] == "running" and owner["child_birth"] == entry[5]["birth"] and owner["process_group"] == entry[5]["group"]
                return False
            assert _wait_until(bound), "real child must be durably bound while held at its IPC gate"

    def waiting(self, label):
        def current():
            while True:
                try:
                    entry = self.trace.get_nowait()
                    self.events.append(entry)
                    assert entry[0] != "error", entry
                except queue.Empty:
                    break
            dirs = _run_dirs(self.trees[label])
            return bool(dirs) and _run_json(dirs[0]).get("execution_resource_wait") is not None
        assert _wait_until(current), self.events
        dirs = _run_dirs(self.trees[label])
        return dirs[0]

    def release(self, label, round_number=1):
        self.gates[label][round_number - 1].set()

    def close(self):
        for gates in self.gates.values():
            for gate in gates:
                gate.set()
        for proc in self.processes.values():
            proc.join(15)
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
        while True:
            try:
                self.events.append(self.trace.get(timeout=0.1))
            except queue.Empty:
                break
        starts = [e for e in self.events if e[0] == "start"]
        (self.root / "ipc-trace.json").write_text(json.dumps({
            "controllers": {label: proc.pid for label, proc in self.processes.items()},
            "worktrees": {label: str(tree) for label, tree in self.trees.items()},
            "events": self.events,
        }, indent=2) + "\n")
        for entry in starts:
            pid, identity = entry[4], entry[5]["birth"]
            if process_liveness(pid) == "present" and process_birth_identity(pid) == identity:
                os.kill(pid, signal.SIGTERM)
                if not _wait_until(lambda: process_liveness(pid) == "absent", timeout=3):
                    if process_birth_identity(pid) == identity:
                        os.kill(pid, signal.SIGKILL)
            assert process_liveness(pid) == "absent", entry
        for proc in self.processes.values():
            assert proc.exitcode == 0, (proc.pid, proc.exitcode, self.events)
        for entry in starts:
            end = next(e for e in self.events if e[0] == "end" and e[1:3] == entry[1:3])
            reaped = next(e for e in self.events if e[0] == "reaped" and e[1:3] == entry[1:3])
            assert self.events.index(entry) < self.events.index(end) < self.events.index(reaped)
            assert entry[4] == end[4] == reaped[4]
            assert entry[5]["phase_at"] < end[5]["phase_at"] < reaped[5]["phase_at"]
            for other in starts:
                if (entry[3], entry[5]["effort"]) != (other[3], other[5]["effort"]) or other == entry:
                    continue
                if entry[5]["phase_at"] < other[5]["phase_at"]:
                    assert reaped[5]["phase_at"] < other[5]["phase_at"], "shared-resource children must not overlap"
        identities = [entry for entry in self.events if entry[0] == "identity"]
        assert len(identities) == len(self.processes), self.events
        assert {entry[3]["store_root"] for entry in identities} == {
            str((self.root / "shared-ipc-store").resolve())
        }
        assert len({entry[3]["config_root"] for entry in identities}) == len(self.processes)
        assert all(entry[3]["workflow_source"] == str(Path(sys.modules["aflow.workflow"].__file__).resolve())
                   for entry in identities)
        for journal in (self.root / "shared-ipc-store").glob("*.json"):
            state = json.loads(journal.read_text())
            assert state["owner"] is None and state["queue"] == [], state
        self.trace.close()
        self.trace.join_thread()


class TestPairedControllers:
    """Two independent controllers sharing one account store root.

    These cases are exercised during final verification with the full
    suite; checkpoint 4 gates on :class:`TestTurnAdmission` only.
    """

    @pytest.mark.parametrize(("role_a", "role_b"), [("worker", "worker"), ("reviewer", "reviewer"), ("worker", "reviewer")])
    def test_independent_processes_share_marked_aliases(self, tmp_path, role_a, role_b):
        pair = _IPCPair(tmp_path)
        try:
            pair.launch("A", role=role_a)
            pair.observe("start", "A")
            pair.launch("B", role=role_b)
            queued = pair.waiting("B")
            assert _run_json(queued)["turns_completed"] == 0
            assert not list((queued / "turns").glob("turn-*"))
            assert pair.processes["A"].pid != pair.processes["B"].pid
            pair.release("A")
            pair.observe("start", "B")
            pair.release("B")
            pair.observe("done", "A")
            pair.observe("done", "B")
            phases = [e[:2] for e in pair.events if e[0] in ("start", "end")]
            assert phases == [("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")]
        finally:
            pair.close()

    @pytest.mark.parametrize("role", ["worker", "reviewer"])
    def test_independent_process_rounds_join_fifo_tail(self, tmp_path, role):
        pair = _IPCPair(tmp_path)
        try:
            pair.launch("A", role=role, rounds=2)
            pair.observe("start", "A")
            pair.launch("B", role=role)
            pair.waiting("B")
            pair.release("A")
            pair.observe("start", "B")
            pair.waiting("A")
            pair.release("B")
            pair.observe("start", "A", 2)
            pair.release("A", 2)
            pair.observe("done", "A", 2)
            pair.observe("done", "B")
            assert [e[:3] for e in pair.events if e[0] == "start"] == [("start", "A", 1), ("start", "B", 1), ("start", "A", 2)]
        finally:
            pair.close()

    @pytest.mark.parametrize("different", ["model", "effort"])
    def test_independent_resources_execute_concurrently(self, tmp_path, different):
        pair = _IPCPair(tmp_path)
        try:
            pair.launch("A")
            pair.observe("start", "A")
            pair.launch("B", model="other-model" if different == "model" else "shared-model", effort="high" if different == "effort" else None)
            pair.observe("start", "B")
            assert not any(e[0] == "end" for e in pair.events)
            assert all(p.is_alive() for p in pair.processes.values())
            pair.release("A")
            pair.release("B")
            pair.observe("done", "A")
            pair.observe("done", "B")
        finally:
            pair.close()

    def test_queued_process_cancellation_never_calls_harness(self, tmp_path):
        pair = _IPCPair(tmp_path)
        try:
            pair.launch("A")
            pair.observe("start", "A")
            pair.launch("B")
            queued = pair.waiting("B")
            _stop_run(queued)
            pair.observe("done", "B", 0)
            stopped = _run_json(queued)
            assert stopped["status"] == "owner_stopped"
            assert stopped.get("execution_resource_wait") is None
            assert stopped["turns_completed"] == 0
            assert not list((queued / "turns").glob("turn-*"))
            pair.release("A")
            pair.observe("done", "A")
        finally:
            pair.close()

    def test_process_retry_joins_behind_waiting_peer(self, tmp_path):
        pair = _IPCPair(tmp_path)
        try:
            pair.launch("A", rounds=2, retry=True)
            pair.observe("start", "A")
            pair.launch("B")
            pair.waiting("B")
            pair.release("A")
            pair.observe("start", "B")
            pair.waiting("A")
            pair.release("B")
            pair.observe("start", "A", 2)
            pair.release("A", 2)
            pair.observe("done", "A", 2)
            pair.observe("done", "B")
            assert [e[:3] for e in pair.events if e[0] == "start"] == [("start", "A", 1), ("start", "B", 1), ("start", "A", 2)]
        finally:
            pair.close()

    def test_independent_process_owner_worker_review_handoff(self, tmp_path):
        pair = _IPCPair(tmp_path)
        try:
            pair.launch("A", rounds=2, review=True)
            pair.observe("start", "A")
            pair.launch("B")
            queued = pair.waiting("B")
            assert _run_json(queued)["turns_completed"] == 0
            pair.release("A")
            pair.observe("start", "A", 2)
            pair.observe("start", "B")
            starts = [e for e in pair.events if e[0] == "start"]
            assert starts[1][3] == "independent-review" or starts[2][3] == "independent-review"
            assert {e[3] for e in starts} == {"shared-model", "independent-review"}
            assert all(p.is_alive() for p in pair.processes.values())
            assert not any(e[0] == "end" and (e[1] == "B" or e[2] == 2) for e in pair.events)
            pair.release("B")
            pair.release("A", 2)
            pair.observe("done", "A", 2)
            pair.observe("done", "B")
        finally:
            pair.close()

    @pytest.fixture
    def paired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        store = ExecutionResourceStore(root=tmp_path / "shared-store")
        monkeypatch.setattr(
            "aflow.workflow._default_execution_resource_store", lambda: store
        )
        monkeypatch.setattr(
            "aflow.workflow._execution_resource_admission_options",
            lambda: {"poll_interval": 0.02, "control_interval": 0.05},
        )
        worktree_a = tmp_path / "worktree-a"
        worktree_b = tmp_path / "worktree-b"
        worktree_a.mkdir()
        worktree_b.mkdir()
        return {
            "store": store,
            "worktree_a": worktree_a,
            "worktree_b": worktree_b,
        }

    def test_shared_worker_worker_fifo(
        self, tmp_path: Path, paired: dict
    ) -> None:
        """Two controllers on one exclusive worker: FIFO by enqueue time.

        A is admitted first and holds the resource inside its worker hook.
        B queues behind it (durable wait, zero B calls).  When A releases,
        A re-queues for its second round *behind* B, so B is admitted
        first; then A takes its second round.  Both plans complete and the
        journal is clean.
        """
        worktree_a: Path = paired["worktree_a"]
        worktree_b: Path = paired["worktree_b"]
        config_path_a, _ = _write_split_config(
            home_dir=worktree_a,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        config_path_b, _ = _write_split_config(
            home_dir=worktree_b,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_a = worktree_a / "plan.md"
        plan_b = worktree_b / "plan.md"
        plan_a.write_text(_VALID_PLAN, encoding="utf-8")
        plan_b.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path_a, "codex.base")
        calls_a: list[str] = []
        calls_b: list[str] = []
        a_started = threading.Event()
        b_started = threading.Event()
        release_a = threading.Event()
        release_b = threading.Event()

        def hooks_a(model: str, count: int) -> None:
            a_started.set()
            release_a.wait(timeout=30)

        def hooks_b(model: str, count: int) -> None:
            b_started.set()
            release_b.wait(timeout=30)

        # A loops for two rounds; B completes after one.  A starts first
        # and must hold the resource before B is launched, so the FIFO
        # order (A ticket 1, B ticket 2) is deterministic.
        thread_a, result_a = _launch(
            worktree_a, config_path_a, plan_a,
            _worker_runner(plan_a, calls_a, complete_after=2, hooks=hooks_a),
        )
        thread_b = None
        result_b: dict = {}
        try:
            # A is admitted first and holds the resource in its hook.
            assert _wait_until(a_started.is_set)
            thread_b, result_b = _launch(
                worktree_b, config_path_b, plan_b,
                _worker_runner(plan_b, calls_b, complete_after=1, hooks=hooks_b),
            )

            def _waiting(tree: Path) -> bool:
                dirs = _run_dirs(tree)
                return bool(dirs) and (
                    _run_json(dirs[0]).get("execution_resource_wait") is not None
                )

            # B queued second: durable wait, zero B calls (re-read the
            # current run metadata on every poll).
            assert _wait_until(lambda: _waiting(worktree_b))
            assert calls_a == [_WORKER_BASE]
            assert calls_b == []
            # Release A: A re-queues for its second round behind B, so B is
            # admitted before A's next same-resource round.
            release_a.set()
            assert _wait_until(b_started.is_set)
            assert _wait_until(lambda: _waiting(worktree_a))
            assert calls_b == [_WORKER_BASE]
            assert calls_a == [_WORKER_BASE]
            # B completes its plan; A then takes its second round.
            release_b.set()
            thread_a.join(timeout=30)
            if thread_b is not None:
                thread_b.join(timeout=30)
        finally:
            release_a.set()
            release_b.set()
        assert "error" not in result_a, result_a.get("error")
        assert "error" not in result_b, result_b.get("error")
        assert calls_a == [_WORKER_BASE, _WORKER_BASE]
        assert calls_b == [_WORKER_BASE]
        run_a = _run_json(_run_dirs(worktree_a)[0])
        run_b = _run_json(_run_dirs(worktree_b)[0])
        assert run_a["status"] == "completed"
        assert run_b["status"] == "completed"
        assert run_a.get("execution_resource_wait") is None
        assert run_b.get("execution_resource_wait") is None
        journal = json.loads(
            (tmp_path / "shared-store" / f"{resource}.json").read_text()
        )
        assert journal["owner"] is None
        assert journal["queue"] == []

    def test_owner_two_plan_handoff(
        self, tmp_path: Path, paired: dict
    ) -> None:
        """Owner-confirmed scheduling handoff across two plans.

        Plan A runs work -> review; its worker holds the exclusive primary
        while plan B waits on the same worker.  When A finishes its worker
        round and moves to the *independent* reviewer resource, B takes the
        primary worker: A is under review while B works.  Both plans
        complete; the idle later worker is never used; both journals are
        clean.  Stop and retry coverage belongs to the final matrix, not
        here.
        """
        worktree_a: Path = paired["worktree_a"]
        worktree_b: Path = paired["worktree_b"]
        config_path_a, _ = _write_split_config(
            home_dir=worktree_a,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_review"),
        )
        config_path_b, _ = _write_split_config(
            home_dir=worktree_b,
            aflow_text=_aflow_toml(),
            workflows_text=_workflows_toml(steps="work_only"),
        )
        plan_a = worktree_a / "plan.md"
        plan_b = worktree_b / "plan.md"
        plan_a.write_text(_VALID_PLAN, encoding="utf-8")
        plan_b.write_text(_VALID_PLAN, encoding="utf-8")
        worker_resource = _resource_for(config_path_a, "codex.base")
        reviewer_resource = _resource_for(
            config_path_a, "codex.reviewer", "workflow.live.steps.review"
        )
        assert worker_resource != reviewer_resource
        calls_a: list[str] = []
        calls_b: list[str] = []
        a_worker_started = threading.Event()
        release_a_worker = threading.Event()
        a_review_started = threading.Event()
        release_a_review = threading.Event()
        b_worker_started = threading.Event()
        release_b_worker = threading.Event()

        def runner_a(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            model = argv[argv.index("--model") + 1]
            calls_a.append(model)
            if model == _WORKER_BASE:
                a_worker_started.set()
                release_a_worker.wait(timeout=30)
            elif model == _REVIEWER:
                a_review_started.set()
                release_a_review.wait(timeout=30)
                plan_a.write_text(
                    _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
                )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        def hooks_b(model: str, count: int) -> None:
            b_worker_started.set()
            release_b_worker.wait(timeout=30)

        thread_a, result_a = _launch(
            worktree_a, config_path_a, plan_a, runner_a,
        )
        thread_b = None
        result_b: dict = {}
        try:
            # A holds the primary worker before B is launched, so the
            # overlap (A review / B worker) is deterministic.  Re-read the
            # current run metadata on every poll.
            assert _wait_until(a_worker_started.is_set)
            thread_b, result_b = _launch(
                worktree_b, config_path_b, plan_b,
                _worker_runner(plan_b, calls_b, complete_after=1, hooks=hooks_b),
            )

            def _b_waiting() -> bool:
                dirs = _run_dirs(worktree_b)
                return bool(dirs) and (
                    _run_json(dirs[0]).get("execution_resource_wait") is not None
                )

            assert _wait_until(_b_waiting)
            assert calls_b == []
            # A finishes its worker round and moves to the independent
            # reviewer resource; B then takes the primary worker.
            release_a_worker.set()
            assert _wait_until(a_review_started.is_set)
            assert _wait_until(b_worker_started.is_set)
            # Overlap: A is under review while B runs the primary worker.
            assert calls_a == [_WORKER_BASE, _REVIEWER]
            assert calls_b == [_WORKER_BASE]
            # B completes its plan; A's review completes the plan and ends.
            release_b_worker.set()
            release_a_review.set()
            thread_a.join(timeout=30)
            if thread_b is not None:
                thread_b.join(timeout=30)
        finally:
            release_a_worker.set()
            release_a_review.set()
            release_b_worker.set()
        assert "error" not in result_a, result_a.get("error")
        assert "error" not in result_b, result_b.get("error")
        assert calls_a == [_WORKER_BASE, _REVIEWER]
        assert calls_b == [_WORKER_BASE]
        # The idle later worker is never used.
        assert _WORKER_HIGH not in calls_a + calls_b
        run_a = _run_json(_run_dirs(worktree_a)[0])
        run_b = _run_json(_run_dirs(worktree_b)[0])
        assert run_a["status"] == "completed"
        assert run_b["status"] == "completed"
        assert run_a.get("execution_resource_wait") is None
        assert run_b.get("execution_resource_wait") is None
        # Both exclusive resources end clean.
        for resource in (worker_resource, reviewer_resource):
            journal = json.loads(
                (tmp_path / "shared-store" / f"{resource}.json").read_text()
            )
            assert journal["owner"] is None
            assert journal["queue"] == []


# ---------------------------------------------------------------------------
# Auxiliary admission matrix (checkpoint 5)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Provider handover lifetime fixtures (checkpoint 5)
# ---------------------------------------------------------------------------

_HANDOVER_OUTPUT = "\n".join(
    f"## {heading}\n- bounded operational evidence"
    for heading in HANDOVER_HEADINGS
)
_HANDOVER_SOURCE_RESOURCE = execution_resource_key("codex", "source-m", None)


def _handover_source_identity() -> tuple[str, str | None, str | None]:
    """The exclusive (harness, model, effort) tuple captured at dispatch."""
    return ("codex", "source-m", None)


def _record_inactive_source(root: Path, run_id: str) -> None:
    """Prove a named predecessor stopped before continuing its session."""
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    assert not (run_dir / "run.json").exists()
    (run_dir / "run.json").write_text(
        json.dumps({"status": "interrupted"}), encoding="utf-8"
    )


def _make_handover_transaction() -> HotplugTransactionV1:
    digest = "a" * 64
    return HotplugTransactionV1(
        transaction_id=hotplug_transaction_id("run-1", digest, 1),
        run_id="run-1", accepted_override_digest=digest, transaction_number=1,
        source_role="worker", target_role="worker",
        source_selector="codex.source", target_selector="reasonix.ds4-1-flash",
        source_harness="codex", target_harness="reasonix",
        source_profile="source", target_profile="ds4-1-flash",
        source_model_display="codex / source-m",
        target_model_display="reasonix / ds4-1-flash",
        stage="accepted",
    )


def _handover_resume(
    *,
    source_identity: tuple[str, str | None, str | None] | None,
    source_selector: str | None = None,
    session_profile: str | None = None,
) -> ResumeContext:
    transaction = _make_handover_transaction()
    if source_selector is not None:
        transaction = replace(transaction, source_selector=source_selector)
    source = HarnessSessionRefV1(
        session_id="codex-source", role="worker",
        selector=transaction.source_selector,
        harness=transaction.source_harness,
        profile=session_profile or transaction.source_profile,
        model_display=transaction.source_model_display,
        resource_identity=source_identity,
    )
    attempts = (ImplementationAttempt(
        turn_number=1, step_name="implement", role="worker", team="source",
        selector=transaction.source_selector, outcome="progress",
        attempt_ordinal=1,
    ),)
    return ResumeContext(
        resumed_from_run_id="codex-source-run", feature_branch=None,
        worktree_path=None, main_branch=None, setup=(), teardown=(),
        interrupted_step_name="implement",
        role_selectors={"worker": transaction.target_selector},
        current_hotplug_transaction=transaction,
        pending_hotplug_transaction=transaction,
        active_role_sessions=(source,), hotplug_transaction_number=1,
        implementation_attempts={"scope-1": attempts},
    )


def _handover_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A disposable git repo; the broker journal stays in its sibling."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path = repo / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    (repo / ".gitignore").write_text(".aflow/\n", encoding="utf-8")
    for args in (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        (
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "initial",
        ),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    _record_inactive_source(repo, "codex-source-run")
    return repo, plan_path


def _handover_config(
    *,
    source_exclusive: bool = True,
    source_model: str = "source-m",
) -> WorkflowUserConfig:
    # The source profile is declared so the transaction's source selector
    # resolves against the current configuration; its exclusive mark supplies
    # the handover opt-in while the session's captured tuple supplies the
    # resource key.  The fresh target profile is unmarked.
    return WorkflowUserConfig(
        roles={"worker": "reasonix.ds4-1-flash"},
        harnesses={
            "codex": WorkflowHarnessConfig(profiles={
                "source": HarnessProfileConfig(
                    model=source_model, exclusive=source_exclusive,
                ),
            }),
            "reasonix": WorkflowHarnessConfig(profiles={
                "ds4-1-flash": HarnessProfileConfig(model="ds4-1-flash"),
            }),
        },
        workflows={"live": WorkflowConfig(
            steps={"implement": WorkflowStepConfig(
                role="worker",
                prompts=("p",),
                go=(GoTransition(to="END", when="DONE"),),
            )},
            first_step="implement",
        )},
        prompts={"p": "Work."},
    )


def _handover_runner(
    plan_path: Path,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        _write_plan(plan_path, _VALID_PLAN.replace("[ ]", "[x]"))
        return subprocess.CompletedProcess(argv, 0, "wire", "")

    return runner


def _launch_handover(
    repo: Path,
    plan_path: Path,
    resume: ResumeContext,
    source_driver: object,
    target_driver: object,
    *,
    config_path: Path | None = None,
    wf_config: WorkflowUserConfig | None = None,
    observer: _Observer | None = None,
) -> tuple[threading.Thread, dict]:
    config = ControllerConfig(repo_root=repo, plan_path=plan_path, max_turns=2)
    launch_config_dir = config_path if config_path is not None else repo
    if wf_config is None and config_path is not None:
        wf_config = load_workflow_config(config_path)
    if wf_config is None:
        wf_config = _handover_config()
    result: dict = {}

    def target() -> None:
        try:
            result["value"] = run_workflow(
                config, wf_config, "live", config_dir=launch_config_dir,
                snapshot_config=False, adapter=CodexAdapter(),
                runner=_handover_runner(plan_path), session_driver=target_driver,
                source_session_driver=source_driver, resume=resume,
                preflight_probe=NoOpHarnessPreflightProbe(),
                observer=observer,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by assertions
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _handover_aflow_toml(*, source_exclusive: bool, source_model: str = "source-m") -> str:
    """Live aflow.toml pair text whose source profile is resolvable."""
    marker = "exclusive = true\n" if source_exclusive else ""
    return f"""
[aflow]
default_workflow = "live"
max_turns = 2

[harness.codex.profiles.source]
model = "{source_model}"
{marker}[harness.reasonix.profiles.ds4-1-flash]
model = "ds4-1-flash"

[roles]
worker = "reasonix.ds4-1-flash"

[prompts]
p = "Work."
"""


def _handover_workflows_toml() -> str:
    return """
[workflow.live]
[workflow.live.steps.implement]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]
"""


def _reap_test_child(child: subprocess.Popen) -> None:
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


class _HandoverSourceDriver:
    """Fake provider source driver whose handover owns a real child process.

    ``mode`` selects the tested exit: ``success`` reaps the child and
    releases the lease; ``released_failure`` does the same and then raises a
    plain callback error; ``unconfirmed_failure`` retains the claim through
    the shared production helper while the child is still alive.
    """

    capabilities = SessionCapabilities(
        session_identity=True, followup_turn=True, read_only_teardown=True,
        idempotent_turn_start=True,
    )

    def __init__(
        self, *, mode: str, release: threading.Event, started: threading.Event,
        on_entry: Callable[[], str | None] | None = None,
        require_lifecycle: bool = True,
    ) -> None:
        self.mode = mode
        self.release = release
        self.started = started
        self.on_entry = on_entry
        self.require_lifecycle = require_lifecycle
        self.entry_status: str | None = None
        self.calls = 0
        self.legacy_call = False
        self.child: subprocess.Popen | None = None
        self.bound: tuple[int, str | None, int | None] | None = None

    def build_full_context(self, run_dir: Path) -> dict:
        return {"plan_state": {"checkpoint": 1}}

    def handover(self, request: SessionRequest, prompt: str, lifecycle=None) -> str:
        if self.require_lifecycle:
            assert lifecycle is not None, "exclusive handover must carry the lease"
        else:
            self.legacy_call = lifecycle is None
        self.calls += 1
        if self.on_entry is not None:
            # Sampled before any callback work: the durable launch intent
            # must already exist when the model-bearing callback begins.
            self.entry_status = self.on_entry()
        # Idempotent no-op: the workflow persisted launch intent already.
        if lifecycle is not None:
            lifecycle.mark_launching()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.child = child
        child_pid, child_birth, process_group = owned_child_binding(child)
        self.bound = (child_pid, child_birth, process_group)
        if lifecycle is not None:
            lifecycle.bind_child(child_pid, child_birth, process_group)
        self.started.set()
        if not self.release.wait(timeout=30):
            raise AssertionError("handover callback was abandoned by the test")
        if self.mode == "unconfirmed_failure":
            # The child is still alive: cessation cannot be confirmed, so
            # the production helper retains the claim as unconfirmed.
            retain_lifecycle_after_failure(
                lifecycle, "handover",
                ValueError("handover transport failed mid-turn"),
            )
        _reap_test_child(child)
        if lifecycle is not None:
            lifecycle.complete()
        if self.mode == "released_failure":
            raise ValueError("handover output rejected after confirmed reap")
        return _HANDOVER_OUTPUT


class _HandoverTargetDriver:
    capabilities = SessionCapabilities(
        session_identity=True, idempotent_turn_start=True,
    )

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.starts = 0

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        self.prompts.append(request.user_prompt)
        return HarnessInvocation(
            label="handover-target", argv=("handover-target",), env={},
            prompt_mode="synthetic", system_prompt=request.system_prompt,
            user_prompt=request.user_prompt, effective_prompt=request.user_prompt,
        )

    def parse_result(
        self, request: SessionRequest, stdout: str, *, returncode: int = 0,
    ) -> SessionResult:
        del stdout, returncode
        self.starts += 1
        return SessionResult(
            session_id="reasonix-target", selector=request.selector,
            model=request.model, effort=request.effort, final_output="DONE",
            capabilities=self.capabilities,
        )


class TestAuxiliaryAdmission:
    """Auxiliary model-bearing calls share the admission/lifetime contract.

    The manager decision is the primary auxiliary model call and exercises
    the shared :class:`_AuxiliaryAdmissionContext` through the real
    ``run_workflow`` path: an exclusive manager queues as an independent
    FIFO participant, dispatches zero model calls while blocked, and acquires
    only after the resource frees.  Unmarked managers keep the legacy fast
    path and never touch the broker.  Owner stop while a manager claim is
    queued finalizes the run as ``owner_stopped`` with no manager dispatch.
    """

    @pytest.mark.parametrize("correction", [False, True])
    def test_manager_failed_launch_intent_has_no_phantom_start(self, tmp_path, resource_store, monkeypatch, correction):
        config_path, _ = _write_split_config(home_dir=tmp_path,
            aflow_text=_aflow_toml(worker_exclusive=False, manager_enabled=True, manager_exclusive=True),
            workflows_text=_workflows_toml(steps="work_only", manager_enabled=True))
        plan = tmp_path / "plan.md"
        plan.write_text(_VALID_PLAN)
        calls = []
        observer = _Observer()
        worker = _worker_runner(plan, calls, complete_after=2)
        original_mark = resource_store.mark_launching
        attempts = []

        def mark(resource, invocation_id, controller):
            attempts.append(invocation_id)
            if len(attempts) == (2 if correction else 1):
                return Outcome("rejected", reason="journal_io_failure")
            return original_mark(resource, invocation_id, controller)

        def runner(argv, **kwargs):
            model = argv[argv.index("--model") + 1]
            if model != _MANAGER_MODEL:
                return worker(argv, **kwargs)
            calls.append("manager")
            assert "MANAGER_NOTE_CORRECTION_JSON" not in str(kwargs.get("input", ""))
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "schema_version": 1, "action": "continue", "reason": "synthetic continue",
                "next_step_notes": ["use plans/done/other.md for the next round"], "stop_report": None}), "")

        monkeypatch.setattr(resource_store, "mark_launching", mark)
        thread, result = _launch(tmp_path, config_path, plan, runner, observer)
        thread.join(20)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), ResourceLeaseError)
        assert result["error"].reason == "journal_io_failure"
        assert calls == [_WORKER_BASE] + (["manager"] if correction else [])
        assert len([e for e in observer.events if getattr(e, "event_type", "") == "manager_started"]) == int(correction)
        run_dir = _first_run_dir(tmp_path)
        run = _run_json(run_dir)
        assert run["turns_completed"] == 1
        assert run.get("manager_history", []) == []
        assert not (run_dir / "manager" / "decision-001" / "note-authority-correction").exists()
        if correction:
            decision = json.loads((run_dir / "manager" / "decision-001" / "result.json").read_text())
            assert decision.get("correction_attempted", False) is False
        resource = _resource_for(config_path, "codex.manager", "manager")
        journal = json.loads((tmp_path / "resource-store" / f"{resource}.json").read_text())
        assert journal["owner"] is None and journal["queue"] == []

    def test_manager_decision_admits_on_its_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                manager_enabled=True,
                manager_exclusive=True,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.manager", "manager")
        assert resource is not None
        identity, invocation_id = _occupy(resource_store, resource, role="manager")
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls, complete_after=2), observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # The unmarked worker runs freely; the exclusive manager blocks.
            assert _wait_until(lambda: calls == [_WORKER_BASE])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == resource
            assert waiting["role"] == "manager"
            assert waiting["kind"] == "manager"
            assert waiting["selector"] == "codex.manager"
            assert "manager" not in calls, "no manager dispatch while blocked"
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert not thread.is_alive()
        assert calls.count("manager") >= 1, "manager dispatched after release"
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None
        phases = [event.phase for event in observer.resource_events()]
        assert "acquired" in phases
        assert "released" in phases

    def test_manager_unmarked_never_touches_broker(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                manager_enabled=True,
                manager_exclusive=False,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.manager", "manager")
        assert resource is None, "unmarked manager has no exclusive resource"
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls, complete_after=2),
        )
        thread.join(timeout=20)
        assert "error" not in result, result.get("error")
        assert calls.count("manager") >= 1
        run = _run_json(_first_run_dir(tmp_path))
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None
        # The unmarked manager never created a broker journal entry.
        journal = tmp_path / "resource-store" / f"{resource}.json" if resource else None
        if journal is not None:
            assert not journal.exists()

    def test_manager_owner_stop_while_blocked(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                manager_enabled=True,
                manager_exclusive=True,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.manager", "manager")
        assert resource is not None
        identity, invocation_id = _occupy(resource_store, resource, role="manager")
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls, complete_after=2),
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(lambda: calls == [_WORKER_BASE])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            _stop_run(run_dir)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert "manager" not in calls, "no manager dispatch after owner stop"
        run = _run_json(run_dir)
        assert run["status"] == "owner_stopped"
        assert run.get("execution_resource_wait") is None

    def test_no_resource_auxiliary_compatibility(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # No exclusive roles at all: every auxiliary call takes the legacy
        # fast path and the run completes without touching the broker.
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                reviewer_exclusive=False,
                auditor_exclusive=False,
                manager_enabled=True,
                manager_exclusive=False,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls, complete_after=2),
        )
        thread.join(timeout=20)
        assert "error" not in result, result.get("error")
        assert calls.count(_WORKER_BASE) == 2
        assert calls.count("manager") >= 1
        run = _run_json(_first_run_dir(tmp_path))
        assert run["status"] == "completed"
        assert run.get("execution_resource_wait") is None

    # ------------------------------------------------------------------
    # Checkpoint 5: every auxiliary model-bearing call category
    # ------------------------------------------------------------------

    def test_manager_note_correction_admits_as_independent_entrant(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A correctable note violation triggers a second manager call that
        # must re-enter the queue as its own participant: it acquires only
        # after the parent decision's lease is released, with a fresh
        # invocation id of its own.
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                manager_enabled=True,
                manager_exclusive=True,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.manager", "manager")
        assert resource is not None
        identity, invocation_id = _occupy(resource_store, resource, role="manager")
        calls: list[str] = []
        decision_count = 0
        observer = _Observer()
        worker_runner = _worker_runner(plan_path, calls, complete_after=2)

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal decision_count
            model = argv[argv.index("--model") + 1]
            prompt = str(kwargs.get("input", ""))
            if model == _MANAGER_MODEL:
                if "MANAGER_NOTE_CORRECTION_JSON" in prompt:
                    calls.append("manager-correction")
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps({
                            "schema_version": 1,
                            "action": "continue",
                            "reason": "synthetic continue",
                            "next_step_notes": [],
                            "stop_report": None,
                        }),
                        "",
                    )
                decision_count += 1
                calls.append("manager")
                notes = (
                    ("use plans/done/other.md for the next round",)
                    if decision_count == 1
                    else ()
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({
                        "schema_version": 1,
                        "action": "continue",
                        "reason": "synthetic continue",
                        "next_step_notes": list(notes),
                        "stop_report": None,
                    }),
                    "",
                )
            return worker_runner(argv, **kwargs)

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner, observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # The unmarked worker runs; the exclusive manager decision (and
            # any correction it would trigger) dispatch zero model calls.
            assert _wait_until(lambda: calls == [_WORKER_BASE])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == resource
            assert waiting["kind"] == "manager"
            assert "manager" not in calls
            assert "manager-correction" not in calls
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert not thread.is_alive()
        assert calls.index("manager") < calls.index("manager-correction")
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        # The correction re-queued as an independent FIFO participant: every
        # manager grant (decision, correction, later decisions) has a unique
        # invocation id and is released exactly once, in order.
        acquired = [
            e for e in observer.resource_events()
            if e.phase == "acquired" and e.role == "manager"
        ]
        released = [
            e for e in observer.resource_events()
            if e.phase == "released" and e.role == "manager"
        ]
        assert len(acquired) >= 2
        assert len({e.invocation_id for e in acquired}) == len(acquired)
        assert [e.invocation_id for e in acquired] == [
            e.invocation_id for e in released
        ]
        # The correction sub-attempt is recorded on the parent decision.
        decision = json.loads(
            (run_dir / "manager" / "decision-001" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert decision["correction_attempted"] is True
        assert decision["correction"]["status"] == "accepted"

    # ------------------------------------------------------------------
    # Checkpoint 5 repair: auxiliary dispatch follows its admitted profile
    # ------------------------------------------------------------------

    def test_manager_decision_model_edit_reprepares_on_new_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A live edit to the manager model while the decision is queued must
        # cancel the old resource ticket and re-queue on the new combination,
        # dispatching the newly built invocation (new model argv).
        new_model = "manager-high"
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                manager_enabled=True,
                manager_exclusive=True,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        old_resource = _resource_for(config_path, "codex.manager", "manager")
        new_resource = execution_resource_key("codex", new_model, None)
        assert old_resource != new_resource
        old_identity, old_invocation = _occupy(
            resource_store, old_resource, role="manager"
        )
        new_identity, new_invocation = _occupy(
            resource_store, new_resource, role="manager"
        )
        calls: list[str] = []
        manager_models: list[str] = []
        observer = _Observer()
        worker_count = 0

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal worker_count
            model = argv[argv.index("--model") + 1]
            prompt = str(kwargs.get("input", ""))
            if model in (_MANAGER_MODEL, new_model) and "schema_version" in prompt:
                calls.append("manager")
                manager_models.append(model)
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({
                        "schema_version": 1, "action": "continue",
                        "reason": "synthetic continue",
                        "next_step_notes": [], "stop_report": None,
                    }), ""
                )
            calls.append(model)
            worker_count += 1
            if worker_count >= 1:
                plan_path.write_text(
                    _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
                )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner, observer
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # Worker turn 1 runs and completes the plan; the exclusive manager
            # decision blocks on the old resource with zero dispatches.
            assert _wait_until(lambda: calls == [_WORKER_BASE])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            assert (
                _run_json(run_dir)["execution_resource_wait"]["resource"]
                == old_resource
            )
            assert "manager" not in calls
            # Live manager-model edit: the queued decision must cancel the
            # old ticket and re-queue on the new (also busy) resource.
            config_path.write_text(
                _aflow_toml(
                    worker_exclusive=False,
                    manager_enabled=True,
                    manager_exclusive=True,
                    manager_model=new_model,
                ),
                encoding="utf-8",
            )
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait")
                    and _run_json(run_dir)["execution_resource_wait"][
                        "resource"
                    ]
                    == new_resource
                )
            )
            assert "manager" not in calls, "no manager dispatch before admission"
            # The manager's old ticket was cancelled: it is no longer queued
            # behind the (still-occupying) foreign claim.
            old_journal = json.loads(
                (tmp_path / "resource-store" / f"{old_resource}.json").read_text()
            )
            assert old_journal["queue"] == []
            assert old_journal["owner"]["invocation_id"] == "foreign-invocation"
            _release(resource_store, new_resource, new_identity, new_invocation)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, old_resource, old_identity, old_invocation,
                required=False,
            )
            _release(
                resource_store, new_resource, new_identity, new_invocation,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert not thread.is_alive()
        assert "manager" in calls
        # The dispatched manager invocation used the new profile's model.
        assert manager_models == [new_model]
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" in phases
        assert "acquired" in phases

    def test_manager_note_correction_model_edit_reprepares_on_new_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A live manager-model edit while the note correction is queued must
        # cancel the old resource ticket and re-queue on the new combination,
        # dispatching the newly built correction invocation (new model argv).
        new_model = "manager-high"
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                manager_enabled=True,
                manager_exclusive=True,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        old_resource = _resource_for(config_path, "codex.manager", "manager")
        new_resource = execution_resource_key("codex", new_model, None)
        assert old_resource != new_resource
        # Pre-occupy the new resource so the re-queued correction stays
        # blocked there after the live edit.
        new_identity, new_invocation = _occupy(
            resource_store, new_resource, role="manager"
        )
        calls: list[str] = []
        correction_models: list[str] = []
        observer = _Observer()
        worker_count = 0
        decision_count = 0
        foreign: dict = {}

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal worker_count, decision_count
            model = argv[argv.index("--model") + 1]
            prompt = str(kwargs.get("input", ""))
            if model in (_MANAGER_MODEL, new_model):
                if "MANAGER_NOTE_CORRECTION_JSON" in prompt:
                    calls.append("manager-correction")
                    correction_models.append(model)
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps({
                            "schema_version": 1, "action": "continue",
                            "reason": "synthetic continue",
                            "next_step_notes": [], "stop_report": None,
                        }), ""
                    )
                decision_count += 1
                calls.append("manager")
                if decision_count == 1:
                    # Enqueue a foreign claim on the manager resource *while
                    # the decision holds its lease*: it takes the resource the
                    # instant the decision releases, deterministically
                    # blocking the follow-on note correction.
                    real = resource_store.current_controller_identity()
                    assert real is not None
                    fid = ControllerIdentity(
                        pid=888888, birth="foreign-birth", boot=real.boot
                    )
                    finv = "foreign-correction-block"
                    spec = ClaimSpec(
                        project_root="/foreign", run_id="foreign-run",
                        invocation_id=finv, kind="manager",
                        role="manager", selector="codex.manager",
                    )
                    outcome = resource_store.enqueue(old_resource, spec, fid)
                    assert outcome.state in ("queued", "acquired"), outcome
                    foreign["identity"] = fid
                    foreign["invocation"] = finv

                    def _hold() -> None:
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            out = resource_store.try_acquire(
                                old_resource, finv, fid
                            )
                            if out.state in ("acquired", "rejected"):
                                return
                            time.sleep(0.01)

                    threading.Thread(target=_hold, daemon=True).start()
                notes = (
                    ("use plans/done/other.md for the next round",)
                    if decision_count == 1
                    else ()
                )
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({
                        "schema_version": 1, "action": "continue",
                        "reason": "synthetic continue",
                        "next_step_notes": list(notes),
                        "stop_report": None,
                    }), ""
                )
            calls.append(model)
            worker_count += 1
            if worker_count >= 2:
                plan_path.write_text(
                    _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
                )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner, observer
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # Worker turn 1 runs; the manager decision acquires the (free)
            # old resource, dispatches a correctable note violation, and the
            # follow-on correction is blocked behind the foreign claim.
            assert _wait_until(lambda: calls == [_WORKER_BASE, "manager"])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            assert (
                _run_json(run_dir)["execution_resource_wait"]["resource"]
                == old_resource
            )
            assert "manager-correction" not in calls
            # Live manager-model edit while the correction is queued.
            config_path.write_text(
                _aflow_toml(
                    worker_exclusive=False,
                    manager_enabled=True,
                    manager_exclusive=True,
                    manager_model=new_model,
                ),
                encoding="utf-8",
            )
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait")
                    and _run_json(run_dir)["execution_resource_wait"][
                        "resource"
                    ]
                    == new_resource
                )
            )
            assert "manager-correction" not in calls, (
                "no correction dispatch before admission"
            )
            _release(resource_store, new_resource, new_identity, new_invocation)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, new_resource, new_identity, new_invocation,
                required=False,
            )
            if foreign.get("identity") is not None:
                _release(
                    resource_store, old_resource,
                    foreign["identity"], foreign["invocation"],
                    required=False,
                )
        assert "error" not in result, result.get("error")
        assert not thread.is_alive()
        assert "manager-correction" in calls
        # The dispatched correction used the new profile's model.
        assert correction_models == [new_model]
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" in phases

    def test_manager_prompt_only_edit_retains_queue_position(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A prompt-only (unrelated) config edit that does not change the
        # manager route must retain the queued decision's ticket.
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=False,
                manager_enabled=True,
                manager_exclusive=True,
                prompt="first prompt {ACTIVE_PLAN_PATH}",
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        resource = _resource_for(config_path, "codex.manager", "manager")
        identity, invocation_id = _occupy(resource_store, resource, role="manager")
        calls: list[str] = []
        thread, result = _launch(
            tmp_path, config_path, plan_path,
            _worker_runner(plan_path, calls, complete_after=2),
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            assert _wait_until(lambda: calls == [_WORKER_BASE])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            first_ticket = _run_json(run_dir)["execution_resource_wait"]["ticket"]
            # A prompt-only edit does not change the manager route: the queued
            # decision retains its ticket and dispatches nothing.
            config_path.write_text(
                _aflow_toml(
                    worker_exclusive=False,
                    manager_enabled=True,
                    manager_exclusive=True,
                    prompt="second prompt {ACTIVE_PLAN_PATH}",
                ),
                encoding="utf-8",
            )
            time.sleep(0.4)
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting is not None
            assert waiting["ticket"] == first_ticket
            assert "manager" not in calls
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert "manager" in calls

    def _repartition_aflow_toml(self, full_model: str) -> str:
        return f"""
[aflow]
default_workflow = "live"
max_turns = 6

[harness.codex.profiles.default]
model = "worker-m"
[harness.codex.profiles.reviewer]
model = "reviewer-m"
[harness.codex.profiles.manager-lite]
model = "lite-m"
[harness.codex.profiles.manager-full]
model = "{full_model}"
exclusive = true

[roles]
worker = "codex.default"
reviewer = "codex.reviewer"
manager_lite = "codex.manager-lite"
manager_full = "codex.manager-full"

[teams.base]
worker = "codex.default"
reviewer = "codex.reviewer"

[prompts]
p = "Work from {{ACTIVE_PLAN_PATH}}."

[manager]
lite_role = "manager_lite"
full_role = "manager_full"
"""

    def _repartition_workflows_toml(self) -> str:
        return """
[workflow.live]
team = "base"
manager_enabled = true

[workflow.live.steps.implement]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }, { to = "review" }]

[workflow.live.steps.review]
role = "reviewer"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }, { to = "implement" }]
"""

    def test_repartition_full_profile_edit_agrees_argv_and_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A live Full-profile edit while a repartition subcall is queued must
        # reprepare it on the new combination; every resulting Full
        # invocation's argv, preflight adapter, and admitted resource agree,
        # and decision/proposal/validation remain distinct entrants.
        old_model = "full-m"
        new_model = "full-high"
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=self._repartition_aflow_toml(old_model),
            workflows_text=self._repartition_workflows_toml(),
        )
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, _VALID_PLAN)
        old_resource = execution_resource_key("codex", old_model, None)
        new_resource = execution_resource_key("codex", new_model, None)
        assert old_resource != new_resource
        old_identity, old_invocation = _occupy(
            resource_store, old_resource, role="manager"
        )
        new_identity, new_invocation = _occupy(
            resource_store, new_resource, role="manager"
        )
        calls: list[str] = []
        full_models: list[str] = []
        observer = _Observer()

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            prompt = str(kwargs.get("input", ""))
            model = argv[argv.index("--model") + 1]
            if model == "worker-m":
                calls.append("worker")
                if calls.count("worker") == 1:
                    return subprocess.CompletedProcess(
                        argv, 0, "AFLOW_SCOPE_PRESSURE: split this checkpoint", ""
                    )
                text = plan_path.read_text(encoding="utf-8")
                text = text.replace(
                    "### [ ] Checkpoint 1: First / Partition 2/2: Part 2",
                    "### [x] Checkpoint 1: First / Partition 2/2: Part 2",
                    1,
                ).replace("- [ ] Implement part 2.", "- [x] Implement part 2.", 1)
                plan_path.write_text(text, encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv, 0, "second child complete", ""
                )
            if model == "reviewer-m":
                calls.append("reviewer")
                text = plan_path.read_text(encoding="utf-8")
                text = text.replace(
                    "### [ ] Checkpoint 1: First / Partition 1/2: Part 1",
                    "### [x] Checkpoint 1: First / Partition 1/2: Part 1",
                    1,
                ).replace("- [ ] Implement part 1.", "- [x] Implement part 1.", 1)
                plan_path.write_text(text, encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv, 0, "first child approved", ""
                )
            if model == "lite-m":
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({
                        "schema_version": 1, "action": "continue",
                        "reason": "synthetic continue",
                        "next_step_notes": [], "stop_report": None,
                    }), ""
                )
            if model in (old_model, new_model):
                full_models.append(model)
                if "REPARTITION_PROPOSE_CONTEXT_JSON:\n" in prompt:
                    calls.append("propose")
                    payload = json.loads(
                        prompt.split("REPARTITION_PROPOSE_CONTEXT_JSON:\n", 1)[1]
                    )
                    envelope = payload["envelope"]
                    source_ids = [b["block_id"] for b in envelope["source_blocks"]]
                    repair_ids = [
                        b["block_id"] for b in payload["repair_evidence_blocks"]
                    ]
                    children = []
                    for ordinal in (1, 2):
                        children.append({
                            "title": f"Part {ordinal}",
                            "narrow_goal": f"Implement part {ordinal}.",
                            "source_block_ids": source_ids,
                            "repair_evidence_ids": repair_ids,
                            "implementation_steps": [f"Implement part {ordinal}."],
                            "verification_commands": ["uv run pytest -q"],
                            "done_criteria": [f"Part {ordinal} is observable."],
                        })
                    proposal = {
                        "schema_version": 1,
                        "envelope_sha256": envelope["canonical_envelope_sha256"],
                        "source_plan_sha256": payload["source_plan_sha256"],
                        "rationale": "Two independently reviewable slices.",
                        "children": children,
                        "current_disposition": "review_current_partition",
                        "cross_cutting_source_reasons": {
                            block_id: "The obligation constrains both slices."
                            for block_id in source_ids
                        },
                    }
                    return subprocess.CompletedProcess(argv, 0, json.dumps(proposal), "")
                if "REPARTITION_VALIDATE_CONTEXT_JSON:\n" in prompt:
                    calls.append("validate")
                    payload = json.loads(
                        prompt.split("REPARTITION_VALIDATE_CONTEXT_JSON:\n", 1)[1]
                    )
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps({
                            "schema_version": 1,
                            "proposal_sha256": payload["proposal_sha256"],
                            "candidate_sha256": payload["candidate_plan_sha256"],
                            "verdict": "accept",
                            "reason": "The split preserves all exact obligations.",
                            "findings": [],
                        }), ""
                    )
                calls.append("decision")
                action = (
                    "repartition_current_checkpoint"
                    if calls.count("decision") == 1
                    else "continue"
                )
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({
                        "schema_version": 1, "action": action,
                        "reason": "The scope has two independently reviewable slices.",
                        "next_step_notes": [], "stop_report": None,
                    }), ""
                )
            raise AssertionError(f"unexpected model {model}")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner, observer, max_turns=6,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # Worker turn 1 emits scope pressure; the exclusive Full decision
            # blocks on the old resource with zero dispatches.
            assert _wait_until(lambda: calls == ["worker"])
            assert "decision" not in calls
            assert "propose" not in calls
            assert "validate" not in calls
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            assert (
                _run_json(run_dir)["execution_resource_wait"]["resource"]
                == old_resource
            )
            # Live Full-profile model edit: the queued decision must cancel
            # the old ticket and re-queue on the new (also busy) resource.
            config_path.write_text(
                self._repartition_aflow_toml(new_model), encoding="utf-8"
            )
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait")
                    and _run_json(run_dir)["execution_resource_wait"][
                        "resource"
                    ]
                    == new_resource
                )
            )
            assert "decision" not in calls
            assert "propose" not in calls
            assert "validate" not in calls
            _release(resource_store, new_resource, new_identity, new_invocation)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, old_resource, old_identity, old_invocation,
                required=False,
            )
            _release(
                resource_store, new_resource, new_identity, new_invocation,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert "decision" in calls and "propose" in calls and "validate" in calls
        # Every Full invocation launched with the new profile's model: the
        # dispatched argv, the (codex) preflight adapter, and the admitted
        # resource all agree on the new combination.
        assert full_models == [new_model, new_model, new_model]
        full_events = [
            e for e in observer.resource_events()
            if e.phase in ("acquired", "released") and e.label.endswith(new_model)
        ]
        acquired = [e for e in full_events if e.phase == "acquired"]
        released = [e for e in full_events if e.phase == "released"]
        # Decision, proposal, and validation are distinct FIFO entrants.
        assert len(acquired) == 3
        assert len(released) == 3
        assert len({e.invocation_id for e in acquired}) == 3
        assert [e.invocation_id for e in acquired] == [
            e.invocation_id for e in released
        ]
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" in phases

    def test_same_resource_worker_then_manager_requeue_independently(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # The manager shares the worker's exact (harness, model, effort)
        # tuple: both are exclusive on one resource, and each round must
        # requeue as its own FIFO participant instead of nesting.
        config_path, _ = _write_split_config(
            home_dir=tmp_path,
            aflow_text=_aflow_toml(
                worker_exclusive=True,
                manager_enabled=True,
                manager_exclusive=True,
                manager_model=_WORKER_BASE,
            ),
            workflows_text=_workflows_toml(
                steps="work_only", manager_enabled=True
            ),
        )
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        worker_resource = _resource_for(config_path, "codex.base")
        manager_resource = _resource_for(config_path, "codex.manager", "manager")
        assert worker_resource == manager_resource is not None
        identity, invocation_id = _occupy(resource_store, worker_resource)
        calls: list[str] = []
        observer = _Observer()
        worker_count = 0

        def runner(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal worker_count
            model = argv[argv.index("--model") + 1]
            prompt = str(kwargs.get("input", ""))
            # The manager shares the worker's model, so the call kind is
            # detected from the prompt, not the model tuple.
            if "schema_version" in prompt:
                calls.append("manager")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({
                        "schema_version": 1,
                        "action": "continue",
                        "reason": "synthetic continue",
                        "next_step_notes": [],
                        "stop_report": None,
                    }),
                    "",
                )
            calls.append(model)
            worker_count += 1
            if worker_count >= 2:
                plan_path.write_text(
                    _VALID_PLAN.replace("[ ]", "[x]"), encoding="utf-8"
                )
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        thread, result = _launch(
            tmp_path, config_path, plan_path, runner, observer,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # The worker turn is blocked behind the foreign owner; no model
            # call of any kind may happen while the shared resource is held.
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == worker_resource
            assert waiting["kind"] == "turn"
            assert calls == []
            _release(resource_store, worker_resource, identity, invocation_id)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, worker_resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert not thread.is_alive()
        assert calls.count(_WORKER_BASE) == 2
        assert calls.count("manager") >= 1
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        # Worker turn 1, the manager decision, and worker turn 2 each took
        # the shared resource as independent participants: unique invocation
        # ids, every grant released exactly once, in acquisition order.
        acquired = [
            e for e in observer.resource_events() if e.phase == "acquired"
        ]
        released = [
            e for e in observer.resource_events() if e.phase == "released"
        ]
        assert len(acquired) >= 3
        roles = [e.role for e in acquired]
        assert "worker" in roles and "manager" in roles
        assert len({e.invocation_id for e in acquired}) == len(acquired)
        assert [e.invocation_id for e in acquired] == [
            e.invocation_id for e in released
        ]

    def _branch_only_config(self, lead_exclusive: bool = True) -> WorkflowUserConfig:
        return WorkflowUserConfig(
            aflow=AflowSection(team_lead="senior_architect"),
            roles={"architect": "codex.default", "senior_architect": "codex.lead"},
            harnesses={"codex": WorkflowHarnessConfig(profiles={
                "default": HarnessProfileConfig(model="worker-m"),
                "lead": HarnessProfileConfig(
                    model="lead-m", exclusive=lead_exclusive
                ),
            })},
            workflows={"branch_wf": WorkflowConfig(
                steps={"impl": WorkflowStepConfig(
                    role="architect",
                    prompts=("p",),
                    go=(
                        GoTransition(to="END", when="DONE || MAX_TURNS_REACHED"),
                        GoTransition(to="impl"),
                    ),
                )},
                first_step="impl",
                setup=("branch",),
                teardown=("merge",),
                main_branch="main",
            )},
            prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
        )

    def _bootstrap_runner(self, tmp_path: Path, plan_path: Path, calls: list[str]):
        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            model = argv[argv.index("--model") + 1]
            cwd = Path(kwargs["cwd"])
            calls.append(model)
            if model == "lead-m":
                if not (cwd / ".git").exists():
                    # Lifecycle bootstrap: initialize the repository.
                    subprocess.run(
                        ["git", "init", "-b", "main"],
                        cwd=str(cwd), check=True, capture_output=True,
                    )
                    subprocess.run(
                        ["git", "config", "user.email", "test@test.com"],
                        cwd=str(cwd), check=True, capture_output=True,
                    )
                    subprocess.run(
                        ["git", "config", "user.name", "Test"],
                        cwd=str(cwd), check=True, capture_output=True,
                    )
                    (cwd / "README.md").write_text(
                        "# Plan\n\nBootstrapped.\n", encoding="utf-8"
                    )
                    subprocess.run(
                        ["git", "add", "README.md"],
                        cwd=str(cwd), check=True, capture_output=True,
                    )
                    subprocess.run(
                        ["git", "commit", "-m", "Initial commit"],
                        cwd=str(cwd), check=True, capture_output=True,
                    )
                else:
                    # Merge teardown (only when fast-forward is impossible).
                    _git_merge_feature_into_main(cwd, "main")
                return subprocess.CompletedProcess(argv, 0, "ok", "")
            # Worker turn: complete the plan.
            _write_plan(plan_path, _VALID_PLAN.replace("[ ]", "[x]"))
            _git_commit_file(cwd, plan_path)
            return subprocess.CompletedProcess(argv, 0, "synthetic output", "")

        return runner

    def test_bootstrap_admits_on_team_lead_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # Lifecycle bootstrap is a model-bearing team-lead call: an
        # exclusive team lead must be admitted on its own resource before
        # any repository side effect, dispatching zero calls while blocked.
        # The repo lives in a subdirectory so the resource-store journal
        # (a sibling) never dirties the primary checkout.
        repo = tmp_path / "repo"
        repo.mkdir()
        wf_config = self._branch_only_config()
        plan_path = repo / "plan.md"
        _write_plan(plan_path, _VALID_PLAN)
        resource = execution_resource_key("codex", "lead-m", None)
        identity, invocation_id = _occupy(resource_store, resource, role="team_lead")
        calls: list[str] = []
        observer = _Observer()
        thread, result = _launch_config(
            tmp_path, wf_config, plan_path,
            self._bootstrap_runner(repo, plan_path, calls), observer,
            workflow_name="branch_wf",
            repo_root=repo,
        )
        try:
            # Bootstrap is the very first model-bearing work: while the
            # exclusive team-lead resource is held there must be zero
            # dispatches and no repository side effects.
            assert _wait_until(
                lambda: any(
                    e.phase == "waiting" and e.role == "team_lead"
                    for e in observer.resource_events()
                )
            )
            assert calls == []
            assert not (repo / ".git").exists()
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert "lead-m" in calls, "bootstrap dispatched after release"
        assert (repo / ".git").exists()
        run = _run_json(_first_run_dir(repo))
        assert run["status"] == "completed"
        bootstrap_events = [
            e for e in observer.resource_events() if e.role == "team_lead"
        ]
        phases = [e.phase for e in bootstrap_events]
        assert "acquired" in phases
        assert "released" in phases
        assert phases.count("acquired") == phases.count("released")

    def test_merge_admits_on_team_lead_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # The model-based merge handoff is admitted on the exclusive team
        # lead resource: the unmarked worker turn proceeds, while the merge
        # dispatches zero model calls until the resource frees.
        # The repo lives in a subdirectory so the resource-store journal
        # (a sibling) never dirties the primary checkout.
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_lifecycle_git_repo(repo, branch="main")
        wf_config = self._branch_only_config()
        plan_path = repo / "plan.md"
        _write_plan(plan_path, _VALID_PLAN)
        _git_commit_file(repo, plan_path)
        resource = execution_resource_key("codex", "lead-m", None)
        identity, invocation_id = _occupy(resource_store, resource, role="team_lead")
        calls: list[str] = []
        observer = _Observer()

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            model = argv[argv.index("--model") + 1]
            cwd = Path(kwargs["cwd"])
            calls.append(model)
            if model == "worker-m":
                _write_plan(plan_path, _VALID_PLAN.replace("[ ]", "[x]"))
                _git_commit_file(cwd, plan_path)
                # Diverge main so the teardown merge cannot fast-forward.
                rc, branch, err = _run_git_in_test(
                    ["branch", "--show-current"], cwd=cwd
                )
                assert rc == 0, err
                subprocess.run(
                    ["git", "checkout", "main"],
                    cwd=str(cwd), check=True, capture_output=True,
                )
                (cwd / "main-only.txt").write_text("main change\n", encoding="utf-8")
                _git_commit_file(cwd, cwd / "main-only.txt")
                subprocess.run(
                    ["git", "checkout", branch],
                    cwd=str(cwd), check=True, capture_output=True,
                )
                return subprocess.CompletedProcess(argv, 0, "synthetic output", "")
            # Model-based merge handoff: a true (non-ff) merge.
            rc, out, err = _run_git_in_test(
                ["branch", "--list", "aflow-*"], cwd=cwd
            )
            assert rc == 0 and out.strip(), f"no aflow feature branch: {err}"
            feature = out.strip().lstrip("+* ").strip()
            subprocess.run(
                ["git", "checkout", "main"],
                cwd=str(cwd), check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "merge", "--no-ff", "-m", "Merge feature", feature],
                cwd=str(cwd), check=True, capture_output=True,
            )
            return subprocess.CompletedProcess(argv, 0, "merged", "")

        thread, result = _launch_config(
            tmp_path, wf_config, plan_path, runner, observer,
            workflow_name="branch_wf",
            repo_root=repo,
        )
        run_dir = _first_run_dir(repo)
        try:
            # The worker turn runs on its unmarked profile; the exclusive
            # merge handoff blocks with zero model dispatches.
            assert _wait_until(lambda: calls == ["worker-m"])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == resource
            assert waiting["kind"] == "merge"
            assert "lead-m" not in calls, "no merge dispatch while blocked"
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert "lead-m" in calls, "merge dispatched after release"
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        merge_events = [
            e for e in observer.resource_events() if e.role == "team_lead"
        ]
        phases = [e.phase for e in merge_events]
        assert "acquired" in phases
        assert "released" in phases
        assert phases.count("acquired") == phases.count("released")

    def test_team_lead_recovery_admits_on_its_resource(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # Team-lead harness recovery is a model-bearing call: an exclusive
        # team lead is admitted on its own resource, dispatching zero calls
        # while blocked, before the retry decision is recorded.
        wf_config = WorkflowUserConfig(
            aflow=AflowSection(team_lead="senior_architect"),
            roles={"architect": "codex.primary", "senior_architect": "codex.lead"},
            teams={
                "primary": TeamConfig(roles={
                    "architect": "codex.primary",
                    "senior_architect": "codex.lead",
                }),
            },
            harnesses={"codex": WorkflowHarnessConfig(profiles={
                "primary": HarnessProfileConfig(model="worker-m"),
                "lead": HarnessProfileConfig(model="lead-m", exclusive=True),
            })},
            workflows={"simple": WorkflowConfig(
                steps={"implement_plan": WorkflowStepConfig(
                    role="architect",
                    prompts=("p",),
                    go=(
                        GoTransition(to="END", when="DONE"),
                        GoTransition(to="implement_plan"),
                    ),
                )},
                first_step="implement_plan",
                team="primary",
            )},
            prompts={"p": "Work."},
            error_handling=ErrorHandlingConfig(
                harness_error_recovery=HarnessErrorRecoveryConfig(rules=()),
            ),
        )
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, _VALID_PLAN)
        resource = execution_resource_key("codex", "lead-m", None)
        identity, invocation_id = _occupy(resource_store, resource, role="team_lead")
        calls: list[str] = []
        observer = _Observer()
        count = 0

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal count
            count += 1
            model = argv[argv.index("--model") + 1]
            calls.append(model)
            if model == "lead-m":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({
                        "action": "retry_same_team_after_delay",
                        "delay_seconds": None,
                        "reason": "retry the same team once",
                        "suggested_keywords": ["mystery failure"],
                        "suggested_action": None,
                    }) + "\n",
                    "",
                )
            if count == 1:
                return subprocess.CompletedProcess(
                    argv, 1, "", "mystery failure\n"
                )
            _write_plan(plan_path, _VALID_PLAN.replace("[ ]", "[x]"))
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        thread, result = _launch_config(
            tmp_path, wf_config, plan_path, runner, observer,
            workflow_name="simple", max_turns=4,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # The failed worker turn must not dispatch the exclusive team
            # lead recovery while the resource is held.
            assert _wait_until(lambda: calls == ["worker-m"])
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == resource
            assert waiting["kind"] == "team_lead_recovery"
            assert "lead-m" not in calls, "no recovery dispatch while blocked"
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert "lead-m" in calls, "recovery dispatched after release"
        run = _run_json(run_dir)
        assert run["status"] == "completed"
        assert run["recovery_summary"]["source"] == "team_lead"
        assert run["recovery_summary"]["executed"] is True
        recovery_events = [
            e for e in observer.resource_events() if e.role == "team_lead"
        ]
        phases = [e.phase for e in recovery_events]
        assert "acquired" in phases
        assert "released" in phases
        assert phases.count("acquired") == phases.count("released")

    def test_repartition_subcalls_admit_as_independent_entries(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # Repartitioning is a multi-call cycle (Full decision, proposal,
        # validation) on the exclusive Full manager resource: each subcall
        # is its own FIFO entry with a fresh invocation id, and nothing
        # dispatches while the resource is held.
        workflow = WorkflowConfig(
            manager_enabled=True,
            steps={
                "implement": WorkflowStepConfig(
                    role="worker",
                    prompts=("p",),
                    go=(
                        GoTransition(to="END", when="DONE"),
                        GoTransition(to="review"),
                    ),
                ),
                "review": WorkflowStepConfig(
                    role="reviewer",
                    prompts=("p",),
                    go=(
                        GoTransition(to="END", when="DONE"),
                        GoTransition(to="implement"),
                    ),
                ),
            },
            first_step="implement",
            team="base",
        )
        wf_config = WorkflowUserConfig(
            roles={
                "worker": "codex.default",
                "reviewer": "codex.reviewer",
                "manager_lite": "codex.manager-lite",
                "manager_full": "codex.manager-full",
            },
            teams={
                "base": TeamConfig(roles={
                    "worker": "codex.default",
                    "reviewer": "codex.reviewer",
                }),
            },
            harnesses={"codex": WorkflowHarnessConfig(profiles={
                "default": HarnessProfileConfig(model="worker-m"),
                "reviewer": HarnessProfileConfig(model="reviewer-m"),
                "manager-lite": HarnessProfileConfig(model="lite-m"),
                "manager-full": HarnessProfileConfig(
                    model="full-m", exclusive=True
                ),
            })},
            workflows={"managed": workflow},
            prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
            manager=ManagerConfig(
                lite_role="manager_lite", full_role="manager_full",
            ),
        )
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, _VALID_PLAN)
        resource = execution_resource_key("codex", "full-m", None)
        identity, invocation_id = _occupy(resource_store, resource, role="manager")
        calls: list[str] = []
        observer = _Observer()

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            prompt = str(kwargs.get("input", ""))
            model = argv[argv.index("--model") + 1]
            if model == "worker-m":
                calls.append("worker")
                if calls.count("worker") == 1:
                    return subprocess.CompletedProcess(
                        argv, 0, "AFLOW_SCOPE_PRESSURE: split this checkpoint", ""
                    )
                text = plan_path.read_text(encoding="utf-8")
                text = text.replace(
                    "### [ ] Checkpoint 1: First / Partition 2/2: Part 2",
                    "### [x] Checkpoint 1: First / Partition 2/2: Part 2",
                    1,
                ).replace("- [ ] Implement part 2.", "- [x] Implement part 2.", 1)
                plan_path.write_text(text, encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv, 0, "second child complete", ""
                )
            if model == "reviewer-m":
                calls.append("reviewer")
                text = plan_path.read_text(encoding="utf-8")
                text = text.replace(
                    "### [ ] Checkpoint 1: First / Partition 1/2: Part 1",
                    "### [x] Checkpoint 1: First / Partition 1/2: Part 1",
                    1,
                ).replace("- [ ] Implement part 1.", "- [x] Implement part 1.", 1)
                plan_path.write_text(text, encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv, 0, "first child approved", ""
                )
            if "REPARTITION_PROPOSE_CONTEXT_JSON:\n" in prompt:
                calls.append("propose")
                payload = json.loads(
                    prompt.split("REPARTITION_PROPOSE_CONTEXT_JSON:\n", 1)[1]
                )
                envelope = payload["envelope"]
                source_ids = [b["block_id"] for b in envelope["source_blocks"]]
                repair_ids = [
                    b["block_id"] for b in payload["repair_evidence_blocks"]
                ]
                children = []
                for ordinal in (1, 2):
                    children.append({
                        "title": f"Part {ordinal}",
                        "narrow_goal": f"Implement part {ordinal}.",
                        "source_block_ids": source_ids,
                        "repair_evidence_ids": repair_ids,
                        "implementation_steps": [f"Implement part {ordinal}."],
                        "verification_commands": ["uv run pytest -q"],
                        "done_criteria": [f"Part {ordinal} is observable."],
                    })
                proposal = {
                    "schema_version": 1,
                    "envelope_sha256": envelope["canonical_envelope_sha256"],
                    "source_plan_sha256": payload["source_plan_sha256"],
                    "rationale": "Two independently reviewable slices.",
                    "children": children,
                    "current_disposition": "review_current_partition",
                    "cross_cutting_source_reasons": {
                        block_id: "The obligation constrains both slices."
                        for block_id in source_ids
                    },
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(proposal), "")
            if "REPARTITION_VALIDATE_CONTEXT_JSON:\n" in prompt:
                calls.append("validate")
                payload = json.loads(
                    prompt.split("REPARTITION_VALIDATE_CONTEXT_JSON:\n", 1)[1]
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({
                        "schema_version": 1,
                        "proposal_sha256": payload["proposal_sha256"],
                        "candidate_sha256": payload["candidate_plan_sha256"],
                        "verdict": "accept",
                        "reason": "The split preserves all exact obligations.",
                        "findings": [],
                    }),
                    "",
                )
            calls.append("decision")
            action = (
                "repartition_current_checkpoint"
                if calls.count("decision") == 1
                else "continue"
            )
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({
                    "schema_version": 1,
                    "action": action,
                    "reason": "The scope has two independently reviewable slices.",
                    "next_step_notes": [],
                    "stop_report": None,
                }),
                "",
            )

        thread, result = _launch_config(
            tmp_path, wf_config, plan_path, runner, observer,
            workflow_name="managed", max_turns=6,
        )
        run_dir = _first_run_dir(tmp_path)
        try:
            # Scope pressure forces a Full decision: the exclusive Full
            # manager blocks with zero dispatches (no decision, proposal,
            # or validation call).
            assert _wait_until(lambda: calls == ["worker"])
            assert "decision" not in calls
            assert "propose" not in calls
            assert "validate" not in calls
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait") is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == resource
            assert waiting["selector"] == "codex.manager-full"
            _release(resource_store, resource, identity, invocation_id)
            thread.join(timeout=30)
        finally:
            _release(
                resource_store, resource, identity, invocation_id,
                required=False,
            )
        assert "error" not in result, result.get("error")
        assert "decision" in calls and "propose" in calls and "validate" in calls
        assert result["value"].final_snapshot.is_complete
        # The Full decision, the proposal, and the validation each took the
        # exclusive resource as independent FIFO participants: unique
        # invocation ids, every grant released exactly once, in order.
        full_events = [
            e for e in observer.resource_events()
            if e.phase in ("acquired", "released") and e.label.endswith("full-m")
        ]
        acquired = [e for e in full_events if e.phase == "acquired"]
        released = [e for e in full_events if e.phase == "released"]
        assert len(acquired) == 3
        assert len(released) == 3
        assert len({e.invocation_id for e in acquired}) == 3
        assert [e.invocation_id for e in acquired] == [
            e.invocation_id for e in released
        ]

    def test_handover_admission_routes_to_source_resource_identity(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # Provider handover is admitted against the source session's fixed
        # (harness, model, effort) tuple -- never the target profile's --
        # and the legacy (unmarked) source takes the fast path.
        source_resource = execution_resource_key("codex", "source-m", None)
        target_resource = execution_resource_key("codex", "target-m", None)
        source_profile = ResolvedProfile(
            harness_name="codex",
            profile_name="source",
            model="source-m",
            effort=None,
            exclusive_resource=source_resource,
        )
        events: list[dict[str, object]] = []

        def emit(phase: str, **kwargs: object) -> None:
            events.append({"phase": phase, **kwargs})

        context = _AuxiliaryAdmissionContext(
            project_root=tmp_path,
            run_id="handover-run",
            kind="handover",
            role="worker",
            observer=None,
            stop_check=lambda: None,
            emit_event=emit,
        )
        identity, invocation_id = _occupy(
            resource_store, source_resource, role="worker"
        )
        grant_box: dict[str, object] = {}

        def admit() -> None:
            grant_box["grant"] = context.admit(
                lambda cfg: (
                    "codex.source",
                    source_resource,
                    source_profile,
                    None,
                )
            )

        thread = threading.Thread(target=admit, daemon=True)
        thread.start()
        try:
            # The handover blocks on the source's resource (not the target
            # tuple) with zero dispatch while it is held.
            assert _wait_until(
                lambda: _journal_owner(tmp_path, source_resource) is not None
            )
            assert thread.is_alive(), "handover must block while source is held"
            assert _journal_owner(tmp_path, target_resource) is None, (
                "handover must never queue on the target profile's resource"
            )
            _release(resource_store, source_resource, identity, invocation_id)
            thread.join(timeout=20)
        finally:
            _release(
                resource_store, source_resource, identity, invocation_id,
                required=False,
            )
        grant = grant_box["grant"]
        assert grant is not None
        assert grant.lease is not None
        assert grant.resolved.exclusive_resource == source_resource
        grant.lease.complete()
        context.note_released()
        phases = [e["phase"] for e in events]
        assert "acquired" in phases
        assert "released" in phases
        assert phases.count("acquired") == phases.count("released")

    def test_handover_legacy_source_without_resource_tuple_skips_broker(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A legacy source session carries no exclusive tuple: the handover
        # takes the fast path and never contacts the broker.
        legacy_profile = ResolvedProfile(
            harness_name="codex",
            profile_name="legacy",
            model="legacy-m",
            effort=None,
            exclusive_resource=None,
        )
        context = _AuxiliaryAdmissionContext(
            project_root=tmp_path,
            run_id="handover-legacy",
            kind="handover",
            role="worker",
            observer=None,
            stop_check=lambda: None,
        )
        grant = context.admit(
            lambda cfg: ("codex.legacy", None, legacy_profile, None)
        )
        assert grant.lease is None
        assert grant.resolved.exclusive_resource is None
        # No broker journal was created for the legacy source tuple.
        store_dir = tmp_path / "resource-store"
        if store_dir.exists():
            assert list(store_dir.iterdir()) == []
        context.note_released()  # no-op for a non-exclusive grant

    def test_session_ref_serializes_raw_tuple_from_unmarked_session(self) -> None:
        # A session dispatched under an unmarked profile still persists its
        # raw (harness, model, effort) tuple, and the serializer round-trips
        # it; old serialized sessions without the field stay readable.
        ref = HarnessSessionRefV1(
            session_id="unmarked-source", role="worker",
            selector="codex.source", harness="codex", profile="source",
            model_display="codex / source-m", status="active",
            resource_identity=("codex", "source-m", None),
        )
        assert "resource_identity" in ref.to_dict()
        restored = HarnessSessionRefV1.from_dict(ref.to_dict())
        assert restored.resource_identity == ("codex", "source-m", None)
        legacy_raw = {
            "schema_version": 1,
            "session_id": "legacy-source", "role": "worker",
            "selector": "codex.source", "harness": "codex",
            "profile": "source", "model_display": "codex / source-m",
            "status": "active",
        }
        assert "resource_identity" not in legacy_raw
        legacy = HarnessSessionRefV1.from_dict(legacy_raw)
        assert legacy.resource_identity is None

    def test_unmarked_new_session_persists_raw_tuple(
        self, tmp_path: Path
    ) -> None:
        # End to end: a fresh session dispatched under an unmarked profile
        # still carries its validated raw (harness, model, effort) tuple in
        # the durable session reference, without deriving it from
        # model_display.
        repo, plan_path = _handover_repo(tmp_path)
        target = _HandoverTargetDriver()
        result = run_workflow(
            ControllerConfig(repo_root=repo, plan_path=plan_path, max_turns=1),
            _handover_config(source_exclusive=False), "live", config_dir=repo,
            snapshot_config=False, adapter=CodexAdapter(),
            runner=_handover_runner(plan_path), session_driver=target,
            preflight_probe=NoOpHarnessPreflightProbe(),
        )
        state = _run_json(Path(result.run_dir))
        sessions = state["active_role_sessions"]
        assert sessions, "the owned turn must leave an active session ref"
        assert sessions[0]["session_id"] == "reasonix-target"
        # JSON serialization round-trips the tuple as a list.
        assert sessions[0]["resource_identity"] == [
            "reasonix", "ds4-1-flash", None,
        ]

    # -- source opt-in vs captured tuple (actual run_workflow handover) -----

    def test_handover_unmarked_session_queues_on_captured_tuple_when_marked(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # The source session was dispatched while its profile was unmarked,
        # so only the raw captured tuple proves its combination; the current
        # source profile is marked and supplies the opt-in.  The handover
        # must queue on the captured tuple's resource with no callback or
        # target start while it is occupied.
        repo, plan_path = _handover_repo(tmp_path)
        resume = _handover_resume(source_identity=_handover_source_identity())
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="success", release=release, started=started,
        )
        target = _HandoverTargetDriver()
        identity, invocation_id = _occupy(
            resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
        )
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
        )
        try:
            assert _wait_until(lambda: len(_run_dirs(repo)) >= 2)
            run_dir = _last_run_dir(repo)
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait")
                    is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == _HANDOVER_SOURCE_RESOURCE
            assert waiting["kind"] == "handover"
            assert source_driver.calls == 0, (
                "the callback must not start while the resource is occupied"
            )
            assert target.starts == 0, (
                "the target must not start while the resource is occupied"
            )
            _release(resource_store, _HANDOVER_SOURCE_RESOURCE,
                     identity, invocation_id)
            assert started.wait(timeout=30), (
                "handover callback never started after release"
            )
            owner = _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE)
            assert owner is not None and owner["status"] == "running"
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
            _release(
                resource_store, _HANDOVER_SOURCE_RESOURCE,
                identity, invocation_id, required=False,
            )
        thread.join(timeout=30)
        assert not thread.is_alive(), "workflow never finished the handover"
        assert "error" not in result, result.get("error")
        assert source_driver.calls == 1
        assert target.starts == 1
        assert _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) is None

    def test_handover_marked_session_takes_legacy_path_when_unmarked(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # The source session was dispatched while its profile was marked
        # (captured tuple present), but the current source profile is
        # unmarked: the handover keeps the legacy ungrouped call shape,
        # dispatches without waiting for the occupied resource, and the
        # broker is never consulted by the workflow.
        repo, plan_path = _handover_repo(tmp_path)
        resume = _handover_resume(source_identity=_handover_source_identity())
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="success", release=release, started=started,
            require_lifecycle=False,
        )
        target = _HandoverTargetDriver()
        observer = _Observer()
        identity, invocation_id = _occupy(
            resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
        )
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
            wf_config=_handover_config(source_exclusive=False),
            observer=observer,
        )
        try:
            assert started.wait(timeout=30), (
                "legacy handover must dispatch without the broker"
            )
            assert source_driver.calls == 1
            assert source_driver.legacy_call, (
                "the ungrouped handover must be called without a lease"
            )
            assert target.starts == 0, "target waits for the callback release"
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
            _release(
                resource_store, _HANDOVER_SOURCE_RESOURCE,
                identity, invocation_id, required=False,
            )
        thread.join(timeout=30)
        assert not thread.is_alive(), "workflow never finished the handover"
        assert "error" not in result, result.get("error")
        assert target.starts == 1
        assert observer.resource_events() == [], (
            "a legacy handover must never emit resource events"
        )
        assert _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) is None, (
            "the workflow must never own a claim on the legacy path"
        )

    @pytest.mark.parametrize(
        "variant",
        ["missing_tuple", "missing_selector", "mismatched_selector"],
    )
    def test_handover_marked_source_rejected_before_callback(
        self, tmp_path: Path, resource_store: ExecutionResourceStore,
        variant: str,
    ) -> None:
        # A marked current source profile without a captured tuple, an
        # unresolvable source selector, and a source session whose profile
        # contradicts the transaction each fail before any callback, target
        # start, or claim.
        repo, plan_path = _handover_repo(tmp_path)
        if variant == "missing_tuple":
            resume = _handover_resume(source_identity=None)
        elif variant == "missing_selector":
            resume = _handover_resume(
                source_identity=_handover_source_identity(),
                source_selector="codex.missing",
            )
        else:
            resume = _handover_resume(
                source_identity=_handover_source_identity(),
                session_profile="stale",
            )

        class SilentDriver:
            capabilities = SessionCapabilities(
                session_identity=True, followup_turn=True, read_only_teardown=True,
            )

            def __init__(self) -> None:
                self.calls = 0

            def build_full_context(self, run_dir: Path) -> dict:
                return {"plan_state": {"checkpoint": 1}}

            def handover(self, request: SessionRequest, prompt: str,
                         lifecycle=None) -> str:
                self.calls += 1
                return _HANDOVER_OUTPUT

        source_driver = SilentDriver()
        target = _HandoverTargetDriver()
        with pytest.raises(WorkflowError) as ctx:
            run_workflow(
                ControllerConfig(repo_root=repo, plan_path=plan_path, max_turns=2),
                _handover_config(), "live", config_dir=repo,
                snapshot_config=False, adapter=CodexAdapter(),
                runner=_handover_runner(plan_path), session_driver=target,
                source_session_driver=source_driver, resume=resume,
                preflight_probe=NoOpHarnessPreflightProbe(),
            )
        assert source_driver.calls == 0, "rejection must precede any model call"
        assert target.starts == 0
        assert not (
            tmp_path / "resource-store"
            / f"{_HANDOVER_SOURCE_RESOURCE}.json"
        ).exists(), "no claim may be created for the rejection"
        state = _run_json(ctx.value.run_dir)
        assert state["status"] == "failed"
        assert state["hotplug_history"][-1]["stage"] == "failed"

    def test_handover_captured_tuple_defines_key_despite_source_model_edit(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # The current source profile stays marked but its model was edited
        # while the handover was already queued: the captured tuple still
        # defines the key and the ticket is kept, while the edited
        # combination is never consulted.
        repo, plan_path = _handover_repo(tmp_path)
        config_path = repo / "aflow.toml"
        config_path.write_text(
            _handover_aflow_toml(source_exclusive=True), encoding="utf-8"
        )
        (repo / "workflows.toml").write_text(
            _handover_workflows_toml(), encoding="utf-8"
        )
        resume = _handover_resume(source_identity=_handover_source_identity())
        edited_resource = execution_resource_key("codex", "edited-m", None)
        assert edited_resource != _HANDOVER_SOURCE_RESOURCE
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="success", release=release, started=started,
        )
        target = _HandoverTargetDriver()
        observer = _Observer()
        identity, invocation_id = _occupy(
            resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
        )
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
            config_path=config_path, observer=observer,
        )
        try:
            assert _wait_until(lambda: len(_run_dirs(repo)) >= 2)
            run_dir = _last_run_dir(repo)
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait")
                    is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == _HANDOVER_SOURCE_RESOURCE, (
                "the captured tuple must define the resource key"
            )
            # Edit the marked source row's model while the handover is
            # queued: the ticket and captured tuple must survive.  A short
            # bounded pause lets at least two poll intervals observe the
            # edit before asserting nothing changed.
            config_path.write_text(
                _handover_aflow_toml(
                    source_exclusive=True, source_model="edited-m"
                ),
                encoding="utf-8",
            )
            time.sleep(0.6)
            still_waiting = _run_json(run_dir).get("execution_resource_wait")
            assert still_waiting is not None, (
                "a source model edit must not cancel the queued ticket"
            )
            assert still_waiting["resource"] == _HANDOVER_SOURCE_RESOURCE
            assert not (
                tmp_path / "resource-store" / f"{edited_resource}.json"
            ).exists(), "the edited source model must never be consulted"
            assert source_driver.calls == 0
            _release(resource_store, _HANDOVER_SOURCE_RESOURCE,
                     identity, invocation_id)
            assert started.wait(timeout=30)
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
            _release(
                resource_store, _HANDOVER_SOURCE_RESOURCE,
                identity, invocation_id, required=False,
            )
        thread.join(timeout=30)
        assert not thread.is_alive(), "workflow never finished the handover"
        assert "error" not in result, result.get("error")
        assert source_driver.calls == 1
        assert target.starts == 1
        phases = [event.phase for event in observer.resource_events()]
        assert "cancelled" not in phases, (
            "a source model edit must keep the ticket without cancelling"
        )

    def test_handover_queued_optin_edit_moves_to_legacy_path(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # While an exclusive handover is queued, a live-config edit that
        # unmarks the source row cancels the ticket and re-prepares onto the
        # legacy ungrouped path through the nonblocking reload path; the
        # dispatch then proceeds without waiting for the occupied resource.
        repo, plan_path = _handover_repo(tmp_path)
        config_path = repo / "aflow.toml"
        config_path.write_text(
            _handover_aflow_toml(source_exclusive=True), encoding="utf-8"
        )
        (repo / "workflows.toml").write_text(
            _handover_workflows_toml(), encoding="utf-8"
        )
        resume = _handover_resume(source_identity=_handover_source_identity())
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="success", release=release, started=started,
            require_lifecycle=False,
        )
        target = _HandoverTargetDriver()
        observer = _Observer()
        identity, invocation_id = _occupy(
            resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
        )
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
            config_path=config_path, observer=observer,
        )
        try:
            assert _wait_until(lambda: len(_run_dirs(repo)) >= 2)
            run_dir = _last_run_dir(repo)
            assert _wait_until(
                lambda: (
                    _run_json(run_dir).get("execution_resource_wait")
                    is not None
                )
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == _HANDOVER_SOURCE_RESOURCE
            assert source_driver.calls == 0
            # Unmark the source row while the handover is queued.
            config_path.write_text(
                _handover_aflow_toml(source_exclusive=False),
                encoding="utf-8",
            )
            assert started.wait(timeout=30), (
                "the unmarked edit must move the handover to the legacy path"
            )
            assert source_driver.legacy_call, (
                "the re-prepared call must be ungrouped"
            )
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
            _release(
                resource_store, _HANDOVER_SOURCE_RESOURCE,
                identity, invocation_id, required=False,
            )
        thread.join(timeout=30)
        assert not thread.is_alive(), "workflow never finished the handover"
        assert "error" not in result, result.get("error")
        assert target.starts == 1
        phases = [event.phase for event in observer.resource_events()]
        assert "waiting" in phases
        assert "cancelled" in phases, "the old ticket must be cancelled"
        assert "acquired" not in phases, (
            "the handover must never own the resource after the edit"
        )
        assert _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) is None


    # -- provider handover lifetime (actual run_workflow handover path) -----

    def test_handover_lifecycle_source_binds_child_and_gates_target(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # An exclusive source handover follows the full lifecycle contract:
        # durable launch intent before the callback, exact child binding
        # before prompting, and release only after confirmed reap.  While
        # the callback owns the live child, the target starts nothing and a
        # dead controller cannot free the active child; the waiting peer
        # admits only after the confirmed release.
        repo, plan_path = _handover_repo(tmp_path)
        resume = _handover_resume(source_identity=_handover_source_identity())
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="success", release=release, started=started,
            on_entry=lambda: (
                (_journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) or {}).get(
                    "status"
                )
            ),
        )
        target = _HandoverTargetDriver()
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
        )
        try:
            assert started.wait(timeout=30), "handover callback never started"
            owner = _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE)
            assert owner is not None and owner["status"] == "running"
            assert owner["child_pid"] == source_driver.bound[0]
            assert owner["child_birth"] == source_driver.bound[1]
            # Launch intent was durable before the callback could spawn.
            assert source_driver.entry_status == "launching"
            assert target.starts == 0, "target must not start before release"
            controller_pid = owner["controller"]["pid"]
            # The waiting peer queues behind the executing handover.
            peer_identity, peer_invocation = _foreign_enqueue(
                resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
            )
            # Injected observations: the controller is dead but the child is
            # alive, so reconcile must keep the owner occupied.
            def _child_alive_evidence(pid: int) -> ProcessEvidence:
                if pid == controller_pid:
                    return ProcessEvidence(liveness="absent", birth=None)
                if pid == source_driver.bound[0]:
                    return ProcessEvidence(
                        liveness="present", birth=source_driver.bound[1]
                    )
                return ProcessEvidence(liveness="unknown", birth=None)

            outcome = resource_store.reconcile(
                _HANDOVER_SOURCE_RESOURCE,
                process_evidence=_child_alive_evidence,
                group_evidence=lambda pgid: "present",
            )
            assert outcome.state == "unchanged", outcome
            blocked = resource_store.try_acquire(
                _HANDOVER_SOURCE_RESOURCE, peer_invocation, peer_identity
            )
            assert blocked.state == "queued", blocked
            release.set()
            thread.join(timeout=30)
            assert not thread.is_alive(), "workflow never finished the handover"
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
            _release(
                resource_store, _HANDOVER_SOURCE_RESOURCE,
                peer_identity, peer_invocation, required=False,
            )
        assert "error" not in result, result.get("error")
        assert source_driver.calls == 1
        assert target.starts == 1, "target must start only after source release"
        assert "Source worker handover:" in target.prompts[-1]
        assert _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) is None
        # The peer that queued during the handover wins the freed resource.
        _foreign_acquire(
            resource_store, _HANDOVER_SOURCE_RESOURCE,
            peer_identity, peer_invocation,
        )
        run = _run_json(_last_run_dir(repo))
        assert run["status"] == "completed"

    def test_handover_opaque_entry_point_rejected_before_dispatch(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A marked source whose handover entry point cannot carry the
        # lifecycle contract is rejected before admission and before any
        # model call: no claim, no dispatch, and a failed run.
        repo, plan_path = _handover_repo(tmp_path)
        resume = _handover_resume(source_identity=_handover_source_identity())

        class OpaqueDriver:
            capabilities = SessionCapabilities(
                session_identity=True, followup_turn=True, read_only_teardown=True,
            )

            def __init__(self) -> None:
                self.calls = 0

            def build_full_context(self, run_dir: Path) -> dict:
                return {"plan_state": {"checkpoint": 1}}

            def handover(self, request: SessionRequest, prompt: str) -> str:
                self.calls += 1
                return _HANDOVER_OUTPUT

        source_driver = OpaqueDriver()
        target = _HandoverTargetDriver()
        with pytest.raises(WorkflowError) as ctx:
            run_workflow(
                ControllerConfig(repo_root=repo, plan_path=plan_path, max_turns=2),
                _handover_config(), "live", config_dir=repo,
                snapshot_config=False, adapter=CodexAdapter(),
                runner=_handover_runner(plan_path), session_driver=target,
                source_session_driver=source_driver, resume=resume,
                preflight_probe=NoOpHarnessPreflightProbe(),
            )
        assert source_driver.calls == 0, "rejection must precede any model call"
        assert target.starts == 0
        assert _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) is None
        journal = (
            tmp_path / "resource-store" / f"{_HANDOVER_SOURCE_RESOURCE}.json"
        )
        assert not journal.exists(), "no claim may be created for the rejection"
        assert "lifecycle-capable" in str(ctx.value)
        state = _run_json(ctx.value.run_dir)
        assert state["status"] == "failed"
        assert state["hotplug_history"][-1]["stage"] == "failed"

    def test_handover_callback_failure_after_confirmed_cleanup_releases(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A callback that reaps its child (confirmed cessation) and then
        # raises leaves the claim released, and its original error -- not a
        # resource error -- is the truthful failure.  A waiting peer can
        # acquire the freed resource.
        repo, plan_path = _handover_repo(tmp_path)
        resume = _handover_resume(source_identity=_handover_source_identity())
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="released_failure", release=release, started=started,
        )
        target = _HandoverTargetDriver()
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
        )
        try:
            assert started.wait(timeout=30), "handover callback never started"
            release.set()
            thread.join(timeout=30)
            assert not thread.is_alive(), "workflow never settled the failure"
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
        error = result.get("error")
        assert isinstance(error, WorkflowError)
        assert "handover output rejected" in str(error)
        assert "execution resource" not in str(error), (
            "a confirmed cleanup failure must not surface as a resource error"
        )
        assert _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) is None
        assert target.starts == 0
        state = _run_json(error.run_dir)
        assert state["status"] == "failed"
        # The freed resource admits a new claimant immediately.
        peer_identity, peer_invocation = _foreign_enqueue(
            resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
        )
        _foreign_acquire(
            resource_store, _HANDOVER_SOURCE_RESOURCE,
            peer_identity, peer_invocation,
        )
        _release(
            resource_store, _HANDOVER_SOURCE_RESOURCE,
            peer_identity, peer_invocation, required=False,
        )

    def test_handover_owner_stop_clears_queued_ticket(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # Owner stop while the handover is queued clears the ticket and
        # finalizes the run as owner-stopped with zero dispatches; the
        # acquired-claim counterpart is structural: nothing in the handover
        # block cancels after acquisition, so a stop cannot abandon an
        # executing child.
        repo, plan_path = _handover_repo(tmp_path)
        resume = _handover_resume(source_identity=_handover_source_identity())
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="success", release=release, started=started,
        )
        target = _HandoverTargetDriver()
        identity, invocation_id = _occupy(
            resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
        )
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
        )
        try:
            assert _wait_until(lambda: len(_run_dirs(repo)) >= 2)
            run_dir = _last_run_dir(repo)
            assert _wait_until(
                lambda: _run_json(run_dir).get("execution_resource_wait") is not None
            )
            waiting = _run_json(run_dir)["execution_resource_wait"]
            assert waiting["resource"] == _HANDOVER_SOURCE_RESOURCE
            assert waiting["kind"] == "handover"
            assert source_driver.calls == 0
            _stop_run(run_dir)
            thread.join(timeout=30)
            assert not thread.is_alive(), "run never finalized the owner stop"
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
            _release(
                resource_store, _HANDOVER_SOURCE_RESOURCE,
                identity, invocation_id, required=False,
            )
        assert "error" not in result, result.get("error")
        assert source_driver.calls == 0, "stop must prevent the handover dispatch"
        assert target.starts == 0
        state = _run_json(run_dir)
        assert state["status"] == "owner_stopped"
        assert state.get("execution_resource_wait") is None
        assert _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE) is None, (
            "the queued ticket must be cleared"
        )

    def test_handover_uncertain_child_retains_claim_until_confirmed(
        self, tmp_path: Path, resource_store: ExecutionResourceStore
    ) -> None:
        # A callback that fails while its child is still alive retains the
        # claim as unconfirmed and surfaces the resource-specific error with
        # the original cause.  No controller death, injected or otherwise,
        # frees the active child; the waiting peer admits only after the
        # child's cessation is positively confirmed.
        repo, plan_path = _handover_repo(tmp_path)
        resume = _handover_resume(source_identity=_handover_source_identity())
        started = threading.Event()
        release = threading.Event()
        source_driver = _HandoverSourceDriver(
            mode="unconfirmed_failure", release=release, started=started,
        )
        target = _HandoverTargetDriver()
        thread, result = _launch_handover(
            repo, plan_path, resume, source_driver, target,
        )
        peer_identity: ControllerIdentity | None = None
        peer_invocation: str | None = None
        try:
            assert started.wait(timeout=30), "handover callback never started"
            owner = _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE)
            assert owner is not None and owner["status"] == "running"
            peer_identity, peer_invocation = _foreign_enqueue(
                resource_store, _HANDOVER_SOURCE_RESOURCE, role="worker"
            )
            release.set()
            thread.join(timeout=30)
            assert not thread.is_alive(), "workflow never settled the failure"
        finally:
            release.set()
            if source_driver.child is not None and source_driver.child.poll() is None:
                _reap_test_child(source_driver.child)
        error = result.get("error")
        assert isinstance(error, WorkflowError)
        assert "execution resource handover failed" in str(error)
        assert target.starts == 0
        owner = _journal_owner(tmp_path, _HANDOVER_SOURCE_RESOURCE)
        assert owner is not None and owner["status"] == "unconfirmed"
        assert owner["child_pid"] == source_driver.bound[0]
        controller_pid = owner["controller"]["pid"]
        assert peer_identity is not None and peer_invocation is not None
        blocked = resource_store.try_acquire(
            _HANDOVER_SOURCE_RESOURCE, peer_invocation, peer_identity
        )
        assert blocked.state == "queued", blocked

        def _child_alive_evidence(pid: int) -> ProcessEvidence:
            if pid == controller_pid:
                return ProcessEvidence(liveness="absent", birth=None)
            if pid == source_driver.bound[0]:
                return ProcessEvidence(
                    liveness="present", birth=source_driver.bound[1]
                )
            return ProcessEvidence(liveness="unknown", birth=None)

        outcome = resource_store.reconcile(
            _HANDOVER_SOURCE_RESOURCE,
            process_evidence=_child_alive_evidence,
            group_evidence=lambda pgid: "present",
        )
        assert outcome.state == "unchanged", (
            "a dead controller cannot free an active child"
        )
        still_blocked = resource_store.try_acquire(
            _HANDOVER_SOURCE_RESOURCE, peer_invocation, peer_identity
        )
        assert still_blocked.state == "queued", still_blocked
        # Positive cessation: reap the child, then reconcile frees the claim.
        _reap_test_child(source_driver.child)
        child_pid = source_driver.bound[0]

        def _child_gone_evidence(pid: int) -> ProcessEvidence:
            if pid in (controller_pid, child_pid):
                return ProcessEvidence(liveness="absent", birth=None)
            return ProcessEvidence(liveness="unknown", birth=None)

        reclaimed = resource_store.reconcile(
            _HANDOVER_SOURCE_RESOURCE,
            process_evidence=_child_gone_evidence,
            group_evidence=lambda pgid: "absent",
        )
        assert reclaimed.state == "reclaimed", reclaimed
        _foreign_acquire(
            resource_store, _HANDOVER_SOURCE_RESOURCE,
            peer_identity, peer_invocation,
        )
        _release(
            resource_store, _HANDOVER_SOURCE_RESOURCE,
            peer_identity, peer_invocation, required=False,
        )

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
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from aflow.config import load_workflow_config
from aflow.execution_resources import (
    ClaimSpec,
    ControllerIdentity,
    ExecutionLease,
    ExecutionResourceAdmission,
    ExecutionResourceStore,
    RevalidationVerdict,
    ResourceLeaseError,
    TurnReprepareRequired,
    UNCHANGED,
)
from aflow.harnesses.codex import CodexAdapter
from aflow.run_state import (
    ControllerConfig,
    ResumeContext,
    _mark_validated_resume_context,
    manager_resume_fields,
)
from aflow.workflow import (
    load_scope_evidence_for_resume,
    resolve_profile,
    run_workflow,
)
from tests._support import _write_split_config

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
) -> str:
    w_excl = "exclusive = true\n" if worker_exclusive else ""
    r_excl = "exclusive = true\n" if reviewer_exclusive else ""
    a_excl = "exclusive = true\n" if auditor_exclusive else ""
    manager_profiles = (
        f"""
[harness.codex.profiles.manager]
model = "{_MANAGER_MODEL}"
"""
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
                    _run_json(run_dir).get("execution_resource_wait") is not None
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


# ---------------------------------------------------------------------------
# Final-only paired controller matrix (checkpoint 7 / final verification)
# ---------------------------------------------------------------------------


class TestPairedControllers:
    """Two independent controllers sharing one account store root.

    These cases are exercised during final verification with the full
    suite; checkpoint 4 gates on :class:`TestTurnAdmission` only.
    """

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

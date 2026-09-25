"""Automatic plan scans use the managed admission boundary exactly once."""

from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path
from threading import Event
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from aflow.plan_consumer import PlanConsumer
from aflow.plan_backups import create_plan_identity, move_plan_identity
from aflow.plan_lifecycle import PlanLifecycle
from aflow.project_admission import (
    ProjectAdmission, ProjectAdmissionConflict, ProjectAutomaticDisabled,
    ProjectPlanDependencyBlocked,
)
from aflow.project_settings import ProjectSettings, ProjectSettingsService


PLAN = "# Work\n\n### [ ] Checkpoint 1: Work\n\n- [ ] Do the work\n"


def _config(tmp_path: Path, *, default: str = "managed") -> Path:
    config = tmp_path / "global" / "aflow.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[aflow]\ndefault_workflow = "{default}"\n'
        f'worktree_root = "{tmp_path / "worktrees"}"\n'
        'team_lead = "worker"\n'
        '[harness.codex.profiles.test]\nmodel = "test"\n'
        '[roles]\nworker = "codex.test"\n'
        '[teams.stable.roles]\nworker = "codex.test"\n'
        '[prompts]\np = "Work."\n',
        encoding="utf-8",
    )
    config.with_name("workflows.toml").write_text(
        '[workflow.managed]\nsetup = ["worktree", "branch"]\n'
        'teardown = ["merge", "rm_worktree"]\nmain_branch = "main"\n'
        'team = "stable"\n'
        '[workflow.managed.steps.implement]\nrole = "worker"\n'
        'prompts = ["p"]\ngo = [{ to = "END", when = "DONE" }]\n',
        encoding="utf-8",
    )
    return config


def _project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    (root / "plans" / "in-progress").mkdir(parents=True)
    return root


def _plan(root: Path, name: str, content: str = PLAN) -> Path:
    path = root / "plans" / "in-progress" / name
    path.write_text(content, encoding="utf-8")
    return path


def _consumer(
    root: Path, config: Path, launch, classify=lambda *_: None,
    *, projects=None,
) -> PlanConsumer:
    return PlanConsumer(
        projects=projects or (lambda: ((root.name, root),)),
        launch=launch,
        classify_invalid=classify,
        config_path=config,
    )


def _second_process_scan(root: Path, config: Path, queue) -> None:
    attempts: list[str] = []
    consumer = _consumer(
        root, config,
        lambda _project, path, *_: attempts.append(path),
    )
    consumer.scan_once()
    consumer.scan_once()
    queue.put(attempts)
    consumer.stop()


def test_stable_plans_fill_two_slots_and_third_waits(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    for name in ("a.md", "b.md", "c.md"):
        _plan(root, name)
    calls: list[tuple[str, str, str | None]] = []

    def launch(project, path, key, revision, workflow, team, identity):
        calls.append((path, workflow, team))
        ProjectAdmission(root).acquire(
            f"run-{len(calls)}", plan_path=root / path,
            idempotency_key=key, expected_plan_revision=revision,
            expected_plan_identity=identity,
        )
        return SimpleNamespace(run_id=f"run-{len(calls)}")

    consumer = _consumer(root, config, launch)
    consumer.scan_once()
    assert calls == []
    consumer.scan_once()
    assert calls == [
        ("plans/in-progress/a.md", "managed", "stable"),
        ("plans/in-progress/b.md", "managed", "stable"),
    ]
    assert consumer.reason(root, "c.md") == "capacity"
    consumer.scan_once()
    assert len(calls) == 2
    consumer.stop()


def test_managed_queue_delivery_failure_and_restart_scenario(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    first = _plan(root, "feature_P01_first.md")
    successor = _plan(root, "feature_P03_next.md")
    _plan(root, "other.md")
    _plan(root, "third.md")
    invalid = _plan(root, "invalid.md", "not a checkpoint plan")
    admission = ProjectAdmission(root)
    lifecycle = PlanLifecycle(root)
    launched: dict[str, object] = {}
    rejected: list[str] = []
    attempts = 0

    def classify(_project, name, revision):
        rejected.append(name)
        lifecycle.move(root / "plans" / "in-progress" / name,
                       "needs_plan_change", expected_revision=revision,
                       reason_code="invalid_plan", reason="Invalid plan")

    def launch(_project, name, key, revision, _workflow, _team, identity):
        nonlocal attempts
        attempts += 1
        run_id = f"run-{attempts}"
        reservation = admission.acquire(
            run_id, plan_path=root / name, idempotency_key=key,
            expected_plan_revision=revision, expected_plan_identity=identity,
        )
        launched[name] = reservation
        return SimpleNamespace(run_id=run_id)

    consumer = _consumer(root, config, launch, classify)
    consumer.scan_once()
    consumer.scan_once()
    assert rejected == ["invalid.md"]
    assert (root / "plans" / "needs-plan-change" / invalid.name).exists()
    assert set(launched) == {
        "plans/in-progress/feature_P01_first.md", "plans/in-progress/other.md",
    }
    assert consumer.reason(root, successor.name) == "dependency"
    assert consumer.reason(root, "third.md") == "capacity"
    assert admission.snapshot().occupied_count == 2

    # The consumer can restart without changing active worker reservations.
    consumer.stop()
    restarted = _consumer(root, config, launch, classify)
    restarted.scan_once()
    restarted.scan_once()
    assert admission.snapshot().occupied_count == 2
    assert len(launched) == 2

    first_reservation = launched["plans/in-progress/feature_P01_first.md"]
    admission.release(first_reservation.run_id, first_reservation.nonce)
    done = root / "plans" / "done" / first.name
    done.parent.mkdir()
    first.rename(done)
    assert move_plan_identity(root, source_plan_path=first, destination_plan_path=done)
    assert done.exists()
    receipt = root / ".aflow" / "runs" / "delivered-run" / "publication.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    (receipt.parent / "run.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8",
    )
    receipt.write_text(json.dumps({
        "status": "published", "commit": "a" * 40, "source_commit": "b" * 40,
        "remote": "origin", "branch": "main",
        "plan_lifecycle": {"phase": "committed", "complete": True,
                           "source": f"plans/in-progress/{first.name}",
                           "destination": f"plans/done/{first.name}"},
    }), encoding="utf-8")
    restarted.scan_once()
    restarted.scan_once()
    assert "plans/in-progress/feature_P03_next.md" in launched, (
        restarted.reason(root, successor.name), admission.snapshot().occupied_count,
    )
    assert restarted.reason(root, "third.md") == "capacity"

    next_reservation = launched["plans/in-progress/feature_P03_next.md"]
    admission.release(next_reservation.run_id, next_reservation.nonce)
    failed = lifecycle.move(
        successor, "failed", expected_revision=hashlib.sha256(successor.read_bytes()).hexdigest(),
        reason_code="terminal_execution_failure", reason="Worker failed",
        source_run_id=next_reservation.run_id,
    )
    other_reservation = launched["plans/in-progress/other.md"]
    admission.release(other_reservation.run_id, other_reservation.nonce)
    later = _plan(root, "feature_P05_later.md")
    with pytest.raises(ProjectPlanDependencyBlocked):
        admission.acquire("later", plan_path=later, idempotency_key="later")
    restarted.scan_once()
    restarted.scan_once()
    assert "plans/in-progress/third.md" in launched
    failed.write_text(PLAN + "\nCorrected.\n", encoding="utf-8")
    corrected = lifecycle.move(
        failed, "in_progress", expected_revision=hashlib.sha256(failed.read_bytes()).hexdigest(),
        reason_code="requeued", reason="Corrected by operator",
    )
    assert corrected.read_text(encoding="utf-8").endswith("Corrected.\n")
    assert lifecycle.record_for(corrected)["source_run_id"] == next_reservation.run_id
    restarted.stop()


def test_opt_out_and_file_edit_require_new_stable_observation(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    path = _plan(root, "one.md")
    calls: list[str] = []
    consumer = _consumer(root, config, lambda _p, name, *_: calls.append(name))
    settings = ProjectSettingsService(root)
    snapshot = settings.read()
    disabled = settings.save(
        ProjectSettings(auto_consume_plans=False), expected_revision=snapshot.revision,
    )
    consumer.scan_once()
    consumer.scan_once()
    assert calls == []
    settings.save(ProjectSettings(), expected_revision=disabled.revision)
    consumer.scan_once()
    path.write_text(PLAN + "\nMore detail.\n", encoding="utf-8")
    consumer.scan_once()
    assert calls == []
    consumer.scan_once()
    assert calls == ["plans/in-progress/one.md"]
    consumer.stop()


def test_automatic_opt_out_is_checked_again_at_admission(tmp_path: Path) -> None:
    root = _project(tmp_path)
    path = _plan(root, "one.md")
    settings = ProjectSettingsService(root)
    settings.save(
        ProjectSettings(auto_consume_plans=False),
        expected_revision=settings.read().revision,
    )
    from hashlib import sha256

    with pytest.raises(ProjectAutomaticDisabled):
        ProjectAdmission(root).acquire(
            "disabled-run", plan_path=path, idempotency_key="automatic",
            automatic=True, expected_plan_revision=sha256(path.read_bytes()).hexdigest(),
        )
    assert ProjectAdmission(root).snapshot().occupied_count == 0


def test_series_successor_waits_while_other_project_proceeds(tmp_path: Path) -> None:
    first = _project(tmp_path, "first")
    second = _project(tmp_path, "second")
    _plan(first, "feature_P01_intro.md")
    _plan(first, "feature_P03_finish.md")
    _plan(second, "independent.md")
    config = _config(tmp_path)
    calls: list[tuple[str, str]] = []

    def launch(project, name, key, revision, _workflow, _team, identity):
        root = first if project == "first" else second
        try:
            ProjectAdmission(root).acquire(
                f"{project}-{len(calls)}", plan_path=root / name,
                idempotency_key=key, expected_plan_revision=revision,
                expected_plan_identity=identity,
            )
        except ProjectPlanDependencyBlocked as exc:
            calls.append((project, "blocked"))
            raise exc
        calls.append((project, name))
        return SimpleNamespace(run_id=f"{project}-{len(calls)}")

    consumer = _consumer(
        first, config, launch,
        projects=lambda: (("first", first), ("second", second)),
    )
    consumer.scan_once()
    consumer.scan_once()
    assert ("first", "plans/in-progress/feature_P01_intro.md") in calls
    assert ("first", "blocked") in calls
    assert ("second", "plans/in-progress/independent.md") in calls
    assert consumer.reason(first, "feature_P03_finish.md") == "dependency"
    consumer.stop()


def test_slow_launch_cannot_stall_another_project(tmp_path: Path) -> None:
    first = _project(tmp_path, "slow")
    second = _project(tmp_path, "fast")
    _plan(first, "one.md")
    _plan(second, "two.md")
    config = _config(tmp_path)
    slow_entered = Event()
    release_slow = Event()
    fast_started = Event()

    def launch(project, *_args):
        if project == "slow":
            slow_entered.set()
            release_slow.wait(5)
        else:
            fast_started.set()
        return SimpleNamespace(run_id=f"{project}-run")

    consumer = PlanConsumer(
        projects=lambda: (("slow", first), ("fast", second)),
        launch=launch, classify_invalid=lambda *_: None,
        config_path=config, interval_seconds=0.02,
    )
    consumer.start()
    try:
        assert slow_entered.wait(3)
        assert fast_started.wait(1)
    finally:
        release_slow.set()
        consumer.stop()


def test_completion_artifact_wakes_project_before_next_poll(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    consumer = _consumer(root, config, lambda *_: None)
    scanned = Event()
    watcher_ready = Event()
    rescanned = Event()
    count = 0
    marker = root / "plans" / "in-progress" / ".watch-ready"
    receipt = root / ".aflow" / "runs" / "complete" / "publication.json"

    def observe(*_args):
        nonlocal count
        count += 1
        if count == 1:
            scanned.set()
        elif receipt.exists():
            rescanned.set()
        elif marker.exists():
            watcher_ready.set()

    monkeypatch.setattr(consumer, "_scan_project", observe)
    consumer.start()
    try:
        assert scanned.wait(2)
        # A scan alone does not prove the watcher subscribed. Keep changing a
        # harmless marker until its event actually wakes this worker.
        deadline = time.monotonic() + 3
        while not watcher_ready.is_set() and time.monotonic() < deadline:
            marker.write_text(str(time.monotonic_ns()), encoding="utf-8")
            watcher_ready.wait(0.25)
        assert watcher_ready.is_set()
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"status":"published"}', encoding="utf-8")
        assert rescanned.wait(2)
    finally:
        consumer.stop()


def test_process_owner_lock_and_restart_replay_key(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    _plan(root, "one.md")
    keys: list[str] = []
    owner = _consumer(root, config, lambda _p, _path, key, *_: keys.append(key))
    owner.scan_once()
    owner.scan_once()
    assert len(keys) == 1
    context = get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_second_process_scan, args=(root, config, queue))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    assert queue.get(timeout=2) == []
    owner.stop()

    successor = _consumer(root, config, lambda _p, _path, key, *_: keys.append(key))
    successor.scan_once()
    successor.scan_once()
    assert keys == [keys[0], keys[0]]
    successor.stop()


def test_new_plan_identity_at_reused_name_gets_a_new_key(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    path = _plan(root, "one.md")
    keys: list[str] = []
    consumer = _consumer(root, config, lambda _p, _name, key, *_: keys.append(key))
    consumer.scan_once()
    consumer.scan_once()
    assert len(keys) == 1
    path.unlink()
    consumer.scan_once()
    path.write_text(PLAN, encoding="utf-8")
    create_plan_identity(root, path)
    consumer.scan_once()
    consumer.scan_once()
    assert len(keys) == 2
    assert keys[1] != keys[0]
    consumer.stop()


def test_startup_question_is_left_for_explicit_answer(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    _plan(root, "one.md")
    attempts: list[str] = []

    def launch(_project, _path, key, *_rest):
        attempts.append(key)
        return SimpleNamespace(question_id="pending-question", run_id="pending-run")

    consumer = _consumer(root, config, launch)
    consumer.scan_once()
    consumer.scan_once()
    consumer.scan_once()
    assert len(attempts) == 1
    assert consumer.reason(root, "one.md") == "startup_input"
    consumer.stop()


def test_revision_is_rechecked_under_admission_after_scan(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    path = _plan(root, "one.md")
    calls = 0

    def launch(_project, _name, key, revision, _workflow, _team, identity):
        nonlocal calls
        calls += 1
        path.write_text(PLAN + "\nChanged before reservation.\n", encoding="utf-8")
        with pytest.raises(ProjectAdmissionConflict, match="changed"):
            ProjectAdmission(root).acquire(
                    "changed-run", plan_path=path, idempotency_key=key,
                    expected_plan_revision=revision, expected_plan_identity=identity,
            )
        raise ProjectAdmissionConflict("automatic plan changed during admission")

    consumer = _consumer(root, config, launch)
    consumer.scan_once()
    consumer.scan_once()
    assert calls == 1
    assert ProjectAdmission(root).snapshot().occupied_count == 0
    consumer.scan_once()
    assert calls == 1
    consumer.stop()


def test_invalid_content_is_classified_and_symlink_is_never_followed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = _config(tmp_path)
    _plan(root, "bad.md", "# No checkpoints\n")
    outside = tmp_path / "outside.md"
    outside.write_text(PLAN, encoding="utf-8")
    (root / "plans" / "in-progress" / "link.md").symlink_to(outside)
    classified: list[tuple[str, str, str]] = []
    consumer = _consumer(
        root, config, lambda *_: pytest.fail("invalid plan launched"),
        lambda project, name, revision: classified.append((project, name, revision)),
    )
    consumer.scan_once()
    consumer.scan_once()
    assert [item[1] for item in classified] == ["bad.md"]
    assert consumer.reason(root, "link.md") == "unsafe_plan"
    assert outside.read_text(encoding="utf-8") == PLAN
    consumer.stop()


def test_bad_default_does_not_block_another_project_or_shutdown(tmp_path: Path) -> None:
    blocked = _project(tmp_path, "blocked")
    ready = _project(tmp_path, "ready")
    _plan(blocked, "one.md")
    _plan(ready, "one.md")
    config = _config(tmp_path, default="missing")
    calls: list[str] = []
    consumer = _consumer(
        blocked, config, lambda project, *_: calls.append(project),
        projects=lambda: (("blocked", blocked), ("ready", ready)),
    )
    consumer.scan_once()
    consumer.scan_once()
    assert calls == []
    assert consumer.reason(blocked, "one.md") == "configuration"
    config.write_text(config.read_text().replace('"missing"', '"managed"'))
    consumer.scan_once()
    assert calls == ["blocked", "ready"]
    consumer.stop()
    assert not consumer._owners

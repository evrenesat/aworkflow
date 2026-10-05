"""Focused tests for the durable exclusive-execution resource broker."""

from __future__ import annotations

import fcntl
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
from queue import Empty
from dataclasses import dataclass
from pathlib import Path

import pytest

from aflow import execution_resources as er
from aflow.execution_resources import (
    ClaimSpec,
    ControllerIdentity,
    ExecutionResourceStore,
    ProcessEvidence,
)

RESOURCE_A = "a" * 64
RESOURCE_B = "b" * 64
BOOT = "boot:test-uuid"


def make_controller(pid: int = 1000) -> ControllerIdentity:
    return ControllerIdentity(pid=pid, birth=f"linux-start-ticks:{pid}", boot=BOOT)


def make_spec(invocation: str, *, role: str = "worker") -> ClaimSpec:
    return ClaimSpec(
        project_root=f"/tmp/project-{invocation}",
        run_id=f"run-{invocation}",
        invocation_id=invocation,
        kind="turn",
        role=role,
        selector=f"selector-{invocation}",
    )


def alive(pid: int) -> ProcessEvidence:
    return ProcessEvidence(liveness="present", birth=f"linux-start-ticks:{pid}")


def make_store(
    root: Path,
    *,
    liveness: dict[int, str] | None = None,
    births: dict[int, str] | None = None,
    group: str = "absent",
    boot: str | None = BOOT,
) -> ExecutionResourceStore:
    liveness = liveness or {}
    births = births or {}

    def process_evidence(pid: int) -> ProcessEvidence:
        state = liveness.get(pid, "present")
        birth = births.get(pid) if state == "present" else None
        return ProcessEvidence(liveness=state, birth=birth)  # type: ignore[arg-type]

    return ExecutionResourceStore(
        root,
        process_evidence=process_evidence,
        group_evidence=lambda _pgid: group,  # type: ignore[arg-type]
        boot_provider=lambda: boot,
    )


def write_journal(root: Path, resource: str, journal: dict) -> None:
    (root / f"{resource}.json").write_text(json.dumps(journal), encoding="utf-8")


def read_journal(root: Path, resource: str) -> dict:
    return json.loads((root / f"{resource}.json").read_text(encoding="utf-8"))


def claim_dict(invocation: str, ticket: int, *, status: str = "queued", pid: int = 1000) -> dict:
    return {
        "status": status,
        "project_root": f"/tmp/project-{invocation}",
        "run_id": f"run-{invocation}",
        "invocation_id": invocation,
        "kind": "turn",
        "role": "worker",
        "selector": f"selector-{invocation}",
        "ticket": ticket,
        "controller": {"pid": pid, "birth": f"linux-start-ticks:{pid}", "boot": BOOT},
        "child_pid": None,
        "child_birth": None,
        "process_group": None,
    }


def empty_journal(resource: str, **overrides: object) -> dict:
    journal = {
        "version": 1,
        "resource": resource,
        "revision": 1,
        "next_ticket": 1,
        "owner": None,
        "queue": [],
    }
    journal.update(overrides)
    return journal


def acquire(store: ExecutionResourceStore, resource: str, invocation: str, controller: ControllerIdentity) -> None:
    assert store.enqueue(resource, make_spec(invocation), controller).state == "queued"
    assert store.try_acquire(resource, invocation, controller).state == "acquired"


# -- identity and admission ---------------------------------------------------


def test_enqueue_assigns_monotonic_tickets_and_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    controller = make_controller()

    first = store.enqueue(RESOURCE_A, make_spec("A"), controller)
    repeat = store.enqueue(RESOURCE_A, make_spec("A"), controller)
    second = store.enqueue(RESOURCE_A, make_spec("B"), controller)

    assert first.state == "queued" and first.ticket == 1
    assert repeat.state == "queued" and repeat.ticket == 1
    assert second.ticket == 2
    journal = read_journal(tmp_path, RESOURCE_A)
    assert [claim["invocation_id"] for claim in journal["queue"]] == ["A", "B"]


def test_enqueue_rejects_conflicting_identity(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.enqueue(RESOURCE_A, make_spec("A"), make_controller(1))
    assert first.state == "queued"

    impostor = make_controller(2)
    outcome = store.enqueue(RESOURCE_A, make_spec("A"), impostor)
    assert outcome.state == "rejected" and outcome.reason == "identity_conflict"
    assert len(read_journal(tmp_path, RESOURCE_A)["queue"]) == 1


def test_enqueue_identical_repeat_preserves_ticket_and_journal_bytes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    controller = make_controller()

    first = store.enqueue(RESOURCE_A, make_spec("A"), controller)
    assert first.state == "queued" and first.ticket == 1
    before = (tmp_path / f"{RESOURCE_A}.json").read_bytes()

    repeat = store.enqueue(RESOURCE_A, make_spec("A"), controller)
    assert repeat.state == "queued" and repeat.ticket == first.ticket
    assert (tmp_path / f"{RESOURCE_A}.json").read_bytes() == before


def test_enqueue_conflicting_spec_from_same_controller_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    controller = make_controller()
    assert store.enqueue(RESOURCE_A, make_spec("A"), controller).state == "queued"
    before = (tmp_path / f"{RESOURCE_A}.json").read_bytes()

    conflict = store.enqueue(RESOURCE_A, make_spec("A", role="reviewer"), controller)
    assert conflict.state == "rejected" and conflict.reason == "identity_conflict"
    assert (tmp_path / f"{RESOURCE_A}.json").read_bytes() == before


def test_enqueue_rejects_missing_controller_identity(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    outcome = store.enqueue(RESOURCE_A, make_spec("A"), None)
    assert outcome.state == "rejected" and outcome.reason == "controller_identity_unavailable"
    assert not (tmp_path / f"{RESOURCE_A}.json").exists()


@pytest.mark.parametrize("resource", ["A" * 64, "g" * 64, "a" * 63, ""])
def test_resource_id_validation(tmp_path: Path, resource: str) -> None:
    store = make_store(tmp_path)
    outcome = store.enqueue(resource, make_spec("A"), make_controller())
    assert outcome.state == "rejected" and outcome.reason == "invalid_resource_id"


def test_current_controller_identity_uses_real_observations(tmp_path: Path) -> None:
    store = ExecutionResourceStore(tmp_path)
    identity = store.current_controller_identity()
    assert identity is not None
    assert identity.pid == os.getpid()
    assert identity.birth
    assert identity.boot


def test_fifo_release_requeue_admits_first_surviving_ticket(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a, b, c = make_controller(1), make_controller(2), make_controller(3)

    acquire(store, RESOURCE_A, "A", a)
    assert store.enqueue(RESOURCE_A, make_spec("B"), b).ticket == 2
    assert store.enqueue(RESOURCE_A, make_spec("C"), c).ticket == 3

    assert store.try_acquire(RESOURCE_A, "B", b) == er.Outcome("queued", 2, "busy")
    assert store.try_acquire(RESOURCE_A, "C", c) == er.Outcome("queued", 3, "busy")

    assert store.record_completion(RESOURCE_A, "A", a).state == "released"
    assert store.try_acquire(RESOURCE_A, "B", b).state == "acquired"
    assert store.try_acquire(RESOURCE_A, "C", c).state == "queued"

    assert store.record_completion(RESOURCE_A, "B", b).state == "released"
    assert store.try_acquire(RESOURCE_A, "C", c).state == "acquired"


def test_release_with_wrong_nonce_is_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a = make_controller(1)
    acquire(store, RESOURCE_A, "A", a)

    foreign = make_controller(2)
    outcome = store.record_completion(RESOURCE_A, "B", foreign)
    assert outcome.state == "rejected" and outcome.reason == "not_owner"

    outcome = store.record_completion(RESOURCE_A, "A", make_controller(99))
    assert outcome.state == "rejected" and outcome.reason == "identity_conflict"

    outcome = store.cancel(RESOURCE_A, "B", foreign)
    assert outcome.state == "rejected" and outcome.reason == "unknown_invocation"

    journal = read_journal(tmp_path, RESOURCE_A)
    assert journal["owner"]["invocation_id"] == "A"


def test_independent_resources_do_not_block_each_other(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a = make_controller(1)
    acquire(store, RESOURCE_A, "A", a)

    b = make_controller(2)
    assert store.enqueue(RESOURCE_B, make_spec("B"), b).state == "queued"
    assert store.try_acquire(RESOURCE_B, "B", b).state == "acquired"

    assert (tmp_path / f"{RESOURCE_A}.json").exists()
    assert (tmp_path / f"{RESOURCE_B}.json").exists()
    assert (tmp_path / f"{RESOURCE_A}.lock").exists()


def test_project_and_config_roots_do_not_split_a_resource(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    second = ClaimSpec(
        project_root="/some/other/project",
        run_id="other-run",
        invocation_id="B",
        kind="turn",
        role="reviewer",
        selector="other-selector",
    )
    a, b = make_controller(1), make_controller(2)
    acquire(store, RESOURCE_A, "A", a)
    assert store.enqueue(RESOURCE_A, second, b).state == "queued"
    assert store.try_acquire(RESOURCE_A, "B", b).state == "queued"


def test_cancel_queued_and_reserved_claims(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a, b, c = make_controller(1), make_controller(2), make_controller(3)
    acquire(store, RESOURCE_A, "A", a)
    store.enqueue(RESOURCE_A, make_spec("B"), b)
    store.enqueue(RESOURCE_A, make_spec("C"), c)

    assert store.cancel(RESOURCE_A, "B", b).state == "cancelled"
    assert store.record_completion(RESOURCE_A, "A", a).state == "released"
    assert store.try_acquire(RESOURCE_A, "C", c).state == "acquired"

    store.enqueue(RESOURCE_A, make_spec("B"), b)
    store.record_completion(RESOURCE_A, "C", c)
    assert store.try_acquire(RESOURCE_A, "B", b).state == "acquired"
    assert store.cancel(RESOURCE_A, "B", b).state == "cancelled"
    assert read_journal(tmp_path, RESOURCE_A)["owner"] is None


def test_cancellation_rejects_launching_and_running_owners(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a = make_controller(1)
    acquire(store, RESOURCE_A, "A", a)
    assert store.mark_launching(RESOURCE_A, "A", a).state == "launching"
    assert store.cancel(RESOURCE_A, "A", a).reason == "not_cancellable"
    store.register_child(RESOURCE_A, "A", a, 4242, "linux-start-ticks:4242", 4242)
    assert store.cancel(RESOURCE_A, "A", a).reason == "not_cancellable"
    assert store.record_completion(RESOURCE_A, "A", a).state == "released"


# -- fail-closed storage -------------------------------------------------------


def test_capacity_rejects_new_claims_without_evicting(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    queue = [
        claim_dict(f"inv-{index}", index + 1, pid=1000 + index)
        for index in range(er.MAX_OUTSTANDING_CLAIMS)
    ]
    journal = empty_journal(RESOURCE_A, queue=queue, next_ticket=er.MAX_OUTSTANDING_CLAIMS + 1)
    write_journal(tmp_path, RESOURCE_A, journal)
    before = (tmp_path / f"{RESOURCE_A}.json").read_bytes()

    outcome = store.enqueue(RESOURCE_A, make_spec("new"), make_controller(9999))
    assert outcome.state == "rejected" and outcome.reason == "capacity"
    assert (tmp_path / f"{RESOURCE_A}.json").read_bytes() == before


def test_oversized_journal_fails_closed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    journal = empty_journal(
        RESOURCE_A,
        queue=[claim_dict("A", 1, pid=1000) | {"selector": "x" * (er.MAX_JOURNAL_BYTES + 1)}],
        next_ticket=2,
    )
    write_journal(tmp_path, RESOURCE_A, journal)
    before = (tmp_path / f"{RESOURCE_A}.json").read_bytes()

    outcome = store.enqueue(RESOURCE_A, make_spec("B"), make_controller(2))
    assert outcome.state == "rejected" and outcome.reason == "journal_size_exceeded"
    assert (tmp_path / f"{RESOURCE_A}.json").read_bytes() == before


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda journal: journal.update(version=2), "malformed_journal"),
        (lambda journal: journal.update(resource=RESOURCE_B), "malformed_journal"),
        (
            lambda journal: journal.update(
                queue=[claim_dict("A", 1), claim_dict("B", 1)], next_ticket=3
            ),
            "malformed_journal",
        ),
        (
            lambda journal: journal.update(
                queue=[claim_dict("A", 1), claim_dict("A", 2)], next_ticket=3
            ),
            "malformed_journal",
        ),
        (
            lambda journal: journal.update(
                queue=[claim_dict("A", 1, status="running")], next_ticket=2
            ),
            "malformed_journal",
        ),
        (
            lambda journal: journal.update(
                queue=[{**claim_dict("A", 1), "controller": {}}]
            ),
            "malformed_journal",
        ),
        (lambda journal: journal.update(next_ticket=1), "malformed_journal"),
        (
            lambda journal: journal.update(
                queue=[], owner=claim_dict("A", 1, status="running"), next_ticket=1
            ),
            "malformed_journal",
        ),
    ],
    ids=[
        "unknown-version",
        "resource-mismatch",
        "duplicate-ticket",
        "duplicate-invocation",
        "bad-status",
        "bad-controller",
        "stale-next-ticket-behind-queued",
        "stale-next-ticket-behind-owner",
    ],
)
def test_corrupt_journal_fails_closed_and_is_never_reset(
    tmp_path: Path,
    mutate,
    reason: str,
) -> None:
    store = make_store(tmp_path)
    journal = empty_journal(RESOURCE_A, queue=[claim_dict("A", 1)], next_ticket=2)
    mutate(journal)
    write_journal(tmp_path, RESOURCE_A, journal)
    before = (tmp_path / f"{RESOURCE_A}.json").read_bytes()

    outcome = store.enqueue(RESOURCE_A, make_spec("B"), make_controller(2))
    assert outcome.state == "rejected" and outcome.reason == reason
    assert (tmp_path / f"{RESOURCE_A}.json").read_bytes() == before


def test_unparsable_journal_fails_closed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    (tmp_path / f"{RESOURCE_A}.json").write_text("{not json", encoding="utf-8")
    outcome = store.enqueue(RESOURCE_A, make_spec("A"), make_controller())
    assert outcome.state == "rejected" and outcome.reason == "malformed_journal"
    assert (tmp_path / f"{RESOURCE_A}.json").read_text(encoding="utf-8") == "{not json"


def test_symlink_journal_fails_closed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    target = tmp_path / "real.json"
    target.write_text(json.dumps(empty_journal(RESOURCE_A)), encoding="utf-8")
    os.symlink(target, tmp_path / f"{RESOURCE_A}.json")

    outcome = store.enqueue(RESOURCE_A, make_spec("A"), make_controller())
    assert outcome.state == "rejected" and outcome.reason == "journal_not_regular"
    assert target.read_text(encoding="utf-8") == json.dumps(empty_journal(RESOURCE_A))


def test_no_write_on_unchanged_polls(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a, b = make_controller(1), make_controller(2)
    acquire(store, RESOURCE_A, "A", a)
    store.enqueue(RESOURCE_A, make_spec("B"), b)
    before = (tmp_path / f"{RESOURCE_A}.json").read_bytes()

    assert store.try_acquire(RESOURCE_A, "B", b).state == "queued"
    assert store.enqueue(RESOURCE_A, make_spec("B"), b).state == "queued"
    assert store.try_acquire(RESOURCE_A, "A", a).state == "queued"
    assert store.reconcile(RESOURCE_A).state == "unchanged"
    assert (tmp_path / f"{RESOURCE_A}.json").read_bytes() == before


def test_lock_contention_returns_contended_without_blocking(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    acquire(store, RESOURCE_A, "A", make_controller())
    descriptor = os.open(tmp_path / f"{RESOURCE_A}.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert store.try_acquire(RESOURCE_A, "A", make_controller()).state == "contended"
        assert store.enqueue(RESOURCE_A, make_spec("B"), make_controller(2)).state == "contended"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert store.try_acquire(RESOURCE_A, "A", make_controller()).state == "queued"


def test_lock_open_failure_is_rejected_not_contended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)

    real_open = os.open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith(f"{RESOURCE_A}.lock"):
            raise PermissionError(13, "permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    outcome = store.enqueue(RESOURCE_A, make_spec("A"), make_controller())
    assert outcome.state == "rejected" and outcome.reason == "store_io_failure"
    assert not (tmp_path / f"{RESOURCE_A}.json").exists()
    assert not (tmp_path / f"{RESOURCE_A}.lock").exists()


def test_store_root_uncreatable_is_rejected_not_contended(tmp_path: Path) -> None:
    blocker = tmp_path / "store"
    blocker.write_text("not a directory", encoding="utf-8")
    store = make_store(blocker)

    outcome = store.enqueue(RESOURCE_A, make_spec("A"), make_controller())
    assert outcome.state == "rejected" and outcome.reason == "store_io_failure"


def test_journal_stat_failure_is_rejected_not_contended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    acquire(store, RESOURCE_A, "A", make_controller())
    before = (tmp_path / f"{RESOURCE_A}.json").read_bytes()

    real_lstat = os.lstat

    def failing_lstat(path, *args, **kwargs):
        if str(path).endswith(f"{RESOURCE_A}.json"):
            raise PermissionError(13, "permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", failing_lstat)
    outcome = store.enqueue(RESOURCE_A, make_spec("B"), make_controller(2))
    assert outcome.state == "rejected" and outcome.reason == "journal_io_failure"
    assert (tmp_path / f"{RESOURCE_A}.json").read_bytes() == before


def test_noncontention_flock_error_is_rejected_not_contended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)

    def failing_flock(descriptor: int, flags: int) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(er.fcntl, "flock", failing_flock)
    outcome = store.enqueue(RESOURCE_A, make_spec("A"), make_controller())
    assert outcome.state == "rejected" and outcome.reason == "lock_io_failure"
    assert not (tmp_path / f"{RESOURCE_A}.json").exists()


# -- state transitions ----------------------------------------------------------


def test_reserved_claim_requires_launch_intent_before_running(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a = make_controller(1)
    acquire(store, RESOURCE_A, "A", a)

    outcome = store.register_child(RESOURCE_A, "A", a, 4242, "b", 4242)
    assert outcome.state == "rejected" and outcome.reason == "not_owner"

    assert store.mark_launching(RESOURCE_A, "A", a).state == "launching"
    assert store.register_child(RESOURCE_A, "A", a, 4242, "linux-start-ticks:4242", 4242).state == "running"
    owner = read_journal(tmp_path, RESOURCE_A)["owner"]
    assert owner["status"] == "running"
    assert owner["child_pid"] == 4242
    assert owner["process_group"] == 4242


def test_prelaunch_failure_releases_reserved_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    a, b = make_controller(1), make_controller(2)
    acquire(store, RESOURCE_A, "A", a)
    store.enqueue(RESOURCE_A, make_spec("B"), b)

    assert store.record_completion(RESOURCE_A, "A", a).state == "released"
    assert store.try_acquire(RESOURCE_A, "B", b).state == "acquired"


# -- reconciliation and crash boundaries -----------------------------------------


def _seed_owner(
    tmp_path: Path,
    status: str,
    *,
    child_pid: int | None = None,
    child_birth: str | None = "linux-start-ticks:4242",
    process_group: int | None = 4242,
) -> ExecutionResourceStore:
    store = make_store(tmp_path)
    a = make_controller(1)
    acquire(store, RESOURCE_A, "A", a)
    if status in ("launching", "running", "unconfirmed"):
        store.mark_launching(RESOURCE_A, "A", a)
    if status in ("running", "unconfirmed"):
        if child_pid is None:
            child_pid = 4242
        store.register_child(RESOURCE_A, "A", a, child_pid, child_birth, process_group)
        if status == "unconfirmed":
            store.record_completion(RESOURCE_A, "A", a, unconfirmed=True)
    return store


def test_reconcile_removes_confirmed_dead_queued_caller(tmp_path: Path) -> None:
    store = _seed_owner(tmp_path, "reserved")
    b = make_controller(2)
    store.enqueue(RESOURCE_A, make_spec("B"), b)

    assert store.reconcile(RESOURCE_A).state == "unchanged"

    store = make_store(tmp_path, liveness={2: "absent"})
    outcome = store.reconcile(RESOURCE_A)
    assert outcome.state == "reclaimed"
    journal = read_journal(tmp_path, RESOURCE_A)
    assert journal["queue"] == []
    assert journal["owner"]["invocation_id"] == "A"


def test_reconcile_never_skips_live_or_unknown_head_waiter(tmp_path: Path) -> None:
    store = _seed_owner(tmp_path, "reserved")
    b, c = make_controller(2), make_controller(3)
    store.enqueue(RESOURCE_A, make_spec("B"), b)
    store.enqueue(RESOURCE_A, make_spec("C"), c)

    for liveness in ({2: "present"}, {2: "unknown"}):
        live_store = make_store(tmp_path, liveness=liveness)
        assert live_store.reconcile(RESOURCE_A).state == "unchanged"
        assert live_store.try_acquire(RESOURCE_A, "C", c).state == "queued"
    assert [claim["invocation_id"] for claim in read_journal(tmp_path, RESOURCE_A)["queue"]] == ["B", "C"]


def test_reconcile_treats_pid_reuse_as_controller_absence(tmp_path: Path) -> None:
    store = _seed_owner(tmp_path, "reserved")
    b = make_controller(2)
    store.enqueue(RESOURCE_A, make_spec("B"), b)

    reused = make_store(tmp_path, births={2: "linux-start-ticks:9999"})
    assert reused.reconcile(RESOURCE_A).state == "reclaimed"
    assert read_journal(tmp_path, RESOURCE_A)["queue"] == []


@pytest.mark.parametrize("status", ["queued", "reserved"])
def test_reconcile_removes_dead_queued_or_reserved_owners(tmp_path: Path, status: str) -> None:
    store = _seed_owner(tmp_path, status)
    store = make_store(tmp_path, liveness={1: "absent"})
    assert store.reconcile(RESOURCE_A).state == "reclaimed"
    assert read_journal(tmp_path, RESOURCE_A)["owner"] is None


def test_reconcile_retains_launching_owner_after_controller_loss(tmp_path: Path) -> None:
    _seed_owner(tmp_path, "launching")
    store = make_store(tmp_path, liveness={1: "absent"})
    assert store.reconcile(RESOURCE_A).state == "unchanged"
    assert read_journal(tmp_path, RESOURCE_A)["owner"]["status"] == "launching"


def test_reconcile_retains_running_owner_with_surviving_child(tmp_path: Path) -> None:
    _seed_owner(tmp_path, "running")
    store = make_store(tmp_path, liveness={1: "absent", 4242: "present"})
    assert store.reconcile(RESOURCE_A).state == "unchanged"
    assert read_journal(tmp_path, RESOURCE_A)["owner"]["status"] == "running"


def test_reconcile_retains_running_owner_with_live_process_group(tmp_path: Path) -> None:
    _seed_owner(tmp_path, "running")
    store = make_store(
        tmp_path,
        liveness={1: "absent", 4242: "absent"},
        group="present",
    )
    assert store.reconcile(RESOURCE_A).state == "unchanged"


def test_reconcile_retains_running_owner_when_group_unobservable(tmp_path: Path) -> None:
    _seed_owner(tmp_path, "running")
    store = make_store(
        tmp_path,
        liveness={1: "absent", 4242: "absent"},
        group="unknown",
    )
    assert store.reconcile(RESOURCE_A).state == "unchanged"


def test_reconcile_reclaims_running_owner_after_confirmed_cessation(tmp_path: Path) -> None:
    store = _seed_owner(tmp_path, "running")
    store = make_store(tmp_path, liveness={1: "absent", 4242: "absent"})
    assert store.reconcile(RESOURCE_A).state == "reclaimed"
    assert read_journal(tmp_path, RESOURCE_A)["owner"] is None


def test_reconcile_reclaims_running_owner_when_child_pid_is_reused(tmp_path: Path) -> None:
    _seed_owner(tmp_path, "running")
    store = make_store(
        tmp_path,
        liveness={1: "absent", 4242: "present"},
        births={4242: "linux-start-ticks:9999"},
    )
    assert store.reconcile(RESOURCE_A).state == "reclaimed"
    journal = read_journal(tmp_path, RESOURCE_A)
    assert journal["owner"] is None
    assert journal["queue"] == []


@pytest.mark.parametrize(
    "liveness, births, child_birth, group",
    [
        (
            {1: "absent", 4242: "present"},
            {4242: "linux-start-ticks:4242"},
            "linux-start-ticks:4242",
            "absent",
        ),
        ({1: "absent", 4242: "present"}, {}, "linux-start-ticks:4242", "absent"),
        (
            {1: "absent", 4242: "present"},
            {4242: "linux-start-ticks:9999"},
            None,
            "absent",
        ),
        (
            {1: "absent", 4242: "present"},
            {4242: "linux-start-ticks:9999"},
            "linux-start-ticks:4242",
            "present",
        ),
        (
            {1: "absent", 4242: "present"},
            {4242: "linux-start-ticks:9999"},
            "linux-start-ticks:4242",
            "unknown",
        ),
        (
            {1: "absent", 4242: "unknown"},
            {4242: "linux-start-ticks:9999"},
            "linux-start-ticks:4242",
            "absent",
        ),
    ],
    ids=[
        "same-birth-child",
        "unknown-observed-birth",
        "missing-stored-child-birth",
        "reused-child-live-group",
        "reused-child-unobservable-group",
        "unknown-child-liveness",
    ],
)
def test_reconcile_retains_running_owner_on_uncertain_child_evidence(
    tmp_path: Path,
    liveness: dict[int, str],
    births: dict[int, str],
    child_birth: str | None,
    group: str,
) -> None:
    _seed_owner(tmp_path, "running", child_birth=child_birth)
    store = make_store(tmp_path, liveness=liveness, births=births, group=group)  # type: ignore[arg-type]
    assert store.reconcile(RESOURCE_A).state == "unchanged"
    assert read_journal(tmp_path, RESOURCE_A)["owner"]["status"] == "running"


def test_reconcile_retains_owner_without_bound_child(tmp_path: Path) -> None:
    _seed_owner(tmp_path, "running", child_pid=None)
    journal = read_journal(tmp_path, RESOURCE_A)
    journal["owner"]["child_pid"] = None
    write_journal(tmp_path, RESOURCE_A, journal)
    store = make_store(tmp_path, liveness={1: "absent"})
    assert store.reconcile(RESOURCE_A).state == "unchanged"


def test_reconcile_reclaims_after_confirmed_host_reboot(tmp_path: Path) -> None:
    _seed_owner(tmp_path, "running")
    store = make_store(tmp_path, liveness={1: "absent"}, boot="boot:second-uuid")
    assert store.reconcile(RESOURCE_A).state == "reclaimed"
    assert read_journal(tmp_path, RESOURCE_A)["owner"] is None


def test_unconfirmed_owner_retains_claim_and_reports_reason(tmp_path: Path) -> None:
    store = _seed_owner(tmp_path, "unconfirmed")
    b = make_controller(2)
    store.enqueue(RESOURCE_A, make_spec("B"), b)

    outcome = store.try_acquire(RESOURCE_A, "B", b)
    assert outcome.state == "queued" and outcome.reason == "owner_unconfirmed"
    assert store.reconcile(RESOURCE_A).state == "unchanged"

    store = make_store(tmp_path, liveness={1: "absent", 4242: "absent"})
    assert store.reconcile(RESOURCE_A).state == "reclaimed"
    assert read_journal(tmp_path, RESOURCE_A)["owner"] is None


def test_stale_identity_cannot_cancel_or_release_another_owner(tmp_path: Path) -> None:
    store = _seed_owner(tmp_path, "running")
    a = make_controller(1)
    impostor = ControllerIdentity(pid=1, birth="linux-start-ticks:777", boot=BOOT)

    assert store.cancel(RESOURCE_A, "A", impostor).reason == "identity_conflict"
    assert store.record_completion(RESOURCE_A, "A", impostor).reason == "identity_conflict"
    assert store.mark_launching(RESOURCE_A, "A", impostor).reason == "identity_conflict"
    assert read_journal(tmp_path, RESOURCE_A)["owner"]["status"] == "running"
    assert store.record_completion(RESOURCE_A, "A", a).state == "released"


def test_reconcile_aborts_when_journal_changes_after_sampling(tmp_path: Path) -> None:
    store = _seed_owner(tmp_path, "reserved")
    b = make_controller(2)
    store.enqueue(RESOURCE_A, make_spec("B"), b)

    side_effects: list[str] = []

    def sampling_evidence(pid: int) -> ProcessEvidence:
        if pid == 2:
            side_effects.append("enqueue")
            other = make_store(tmp_path)
            other.enqueue(RESOURCE_A, make_spec("C"), make_controller(3))
        return ProcessEvidence(liveness="absent", birth=None)

    store = ExecutionResourceStore(
        tmp_path,
        process_evidence=sampling_evidence,
        group_evidence=lambda _pgid: "absent",
        boot_provider=lambda: BOOT,
    )
    outcome = store.reconcile(RESOURCE_A)
    assert outcome.state == "unchanged" and outcome.reason == "revision_changed"
    assert side_effects == ["enqueue"]
    assert [claim["invocation_id"] for claim in read_journal(tmp_path, RESOURCE_A)["queue"]] == ["B", "C"]


# -- default macOS process-group observer ---------------------------------------


def _spawn_group_child() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _reap_group_child(child: subprocess.Popen) -> None:
    child.terminate()
    child.wait(timeout=10)


def make_default_group_store(root: Path, *, liveness: dict[int, str]) -> ExecutionResourceStore:
    """Store with injected process evidence and the real default group observer."""

    def process_evidence(pid: int) -> ProcessEvidence:
        state = liveness.get(pid, "present")
        birth = f"linux-start-ticks:{pid}" if state == "present" else None
        return ProcessEvidence(liveness=state, birth=birth)  # type: ignore[arg-type]

    return ExecutionResourceStore(root, process_evidence=process_evidence, boot_provider=lambda: BOOT)


# The null-signal group probe follows POSIX semantics on the Linux and macOS
# kernels alike, so the darwin branch is exercised against real process groups
# by faking only the platform label.


def test_darwin_default_observer_reports_live_then_reaped_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    child = _spawn_group_child()
    try:
        assert er._default_group_evidence(child.pid) == "present"
        _reap_group_child(child)
    finally:
        if child.poll() is None:
            _reap_group_child(child)
    assert er._default_group_evidence(child.pid) == "absent"


@pytest.mark.parametrize(
    "error",
    [PermissionError(1, "Operation not permitted"), OSError(5, "Input/output error")],
    ids=["permission-denied", "other-oserror"],
)
def test_darwin_default_observer_inconclusive_kill_errors_are_unknown(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def failing_kill(pgid: int, sig: int) -> None:
        raise error

    monkeypatch.setattr(os, "kill", failing_kill)
    assert er._default_group_evidence(4242) == "unknown"


@pytest.mark.parametrize("pgid", [None, 0, -4242], ids=["missing", "zero", "negative"])
def test_darwin_default_observer_invalid_pgids_are_unknown(
    monkeypatch: pytest.MonkeyPatch, pgid: int | None
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def unexpected_kill(pgid: int, sig: int) -> None:
        raise AssertionError("kill must not probe missing or nonpositive pgids")

    monkeypatch.setattr(os, "kill", unexpected_kill)
    assert er._default_group_evidence(pgid) == "unknown"


def test_unsupported_platform_default_observer_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    def unexpected_kill(pgid: int, sig: int) -> None:
        raise AssertionError("kill must not probe unsupported platforms")

    monkeypatch.setattr(os, "kill", unexpected_kill)
    assert er._default_group_evidence(4242) == "unknown"


@pytest.mark.parametrize("status", ["running", "unconfirmed"])
def test_darwin_default_observer_reclaims_owner_after_confirmed_group_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    child = _spawn_group_child()
    try:
        pgid = child.pid
        _reap_group_child(child)
    finally:
        if child.poll() is None:
            _reap_group_child(child)

    _seed_owner(
        tmp_path,
        status,
        child_pid=pgid,
        child_birth=f"linux-start-ticks:{pgid}",
        process_group=pgid,
    )
    store = make_default_group_store(tmp_path, liveness={1: "absent", pgid: "absent"})
    assert store.reconcile(RESOURCE_A).state == "reclaimed"
    assert read_journal(tmp_path, RESOURCE_A)["owner"] is None


def test_darwin_default_observer_retains_owner_with_live_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    child = _spawn_group_child()
    try:
        _seed_owner(tmp_path, "running", process_group=child.pid)
        store = make_default_group_store(tmp_path, liveness={1: "absent", 4242: "absent"})
        assert store.reconcile(RESOURCE_A).state == "unchanged"
        assert read_journal(tmp_path, RESOURCE_A)["owner"]["status"] == "running"
    finally:
        if child.poll() is None:
            _reap_group_child(child)


def test_darwin_default_observer_retains_owner_with_unobservable_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def denied_kill(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", denied_kill)
    _seed_owner(tmp_path, "running")
    store = make_default_group_store(tmp_path, liveness={1: "absent", 4242: "absent"})
    assert store.reconcile(RESOURCE_A).state == "unchanged"
    assert read_journal(tmp_path, RESOURCE_A)["owner"]["status"] == "running"


def test_darwin_default_observer_retains_owner_with_missing_recorded_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _seed_owner(tmp_path, "running", process_group=None)
    store = make_default_group_store(tmp_path, liveness={1: "absent", 4242: "absent"})
    assert store.reconcile(RESOURCE_A).state == "unchanged"
    assert read_journal(tmp_path, RESOURCE_A)["owner"]["status"] == "running"


# -- real multiprocess scenario ---------------------------------------------------


@dataclass(frozen=True)
class MpArgs:
    root: str
    resource: str
    invocation: str
    queue: object
    events: tuple


def _mp_spec(invocation: str) -> ClaimSpec:
    return ClaimSpec(
        project_root="/tmp/mp-project",
        run_id=f"mp-run-{invocation}",
        invocation_id=invocation,
        kind="turn",
        role="worker",
        selector=f"mp-selector-{invocation}",
    )


def _mp_first_holder(args: MpArgs) -> None:
    root, resource, invocation, queue, (holding, release, released) = (
        args.root,
        args.resource,
        args.invocation,
        args.queue,
        args.events,
    )
    store = ExecutionResourceStore(root)
    identity = store.current_controller_identity()
    queue.put(("A-identity", identity is not None))
    assert identity is not None
    result = store.enqueue(resource, _mp_spec(invocation), identity)
    queue.put(("A-enqueue", result.state, result.ticket))
    acquired = store.try_acquire(resource, invocation, identity)
    queue.put(("A-acquire", acquired.state))
    assert acquired.state == "acquired"
    assert store.mark_launching(resource, invocation, identity).state == "launching"
    assert (
        store.register_child(resource, invocation, identity, os.getpid(), "self", os.getpid()).state
        == "running"
    )
    holding.set()
    assert release.wait(timeout=60)
    assert store.record_completion(resource, invocation, identity).state == "released"
    released.set()
    queue.put(("A-release", "released"))


def _mp_second_waiter(args: MpArgs) -> None:
    root, resource, invocation, queue, (holding, started, _release, released) = (
        args.root,
        args.resource,
        args.invocation,
        args.queue,
        args.events,
    )
    # Enqueue only after the first holder owns the resource, so ticket order
    # is deterministic and B is genuinely queued behind A.
    assert holding.wait(timeout=60)
    store = ExecutionResourceStore(root)
    identity = store.current_controller_identity()
    assert identity is not None
    result = store.enqueue(resource, _mp_spec(invocation), identity)
    queue.put(("B-enqueue", result.state, result.ticket))
    started.set()
    deadline = time.monotonic() + 60
    acquired = None
    while time.monotonic() < deadline:
        outcome = store.try_acquire(resource, invocation, identity)
        if outcome.state == "acquired":
            acquired = outcome
            break
        time.sleep(0.025)
    queue.put(
        ("B-acquire", acquired.state if acquired else "timeout", released.is_set() if acquired else None)
    )
    if acquired is not None:
        queue.put(("B-release", store.record_completion(resource, invocation, identity).state))


def _mp_independent_holder(args: MpArgs) -> None:
    root, resource, invocation, queue, (holding, _release, _released) = (
        args.root,
        args.resource,
        args.invocation,
        args.queue,
        args.events,
    )
    store = ExecutionResourceStore(root)
    identity = store.current_controller_identity()
    assert identity is not None
    result = store.enqueue(resource, _mp_spec(invocation), identity)
    acquired = store.try_acquire(resource, invocation, identity)
    queue.put(("C-acquire", result.ticket, acquired.state))
    assert acquired.state == "acquired"
    holding.set()
    assert store.record_completion(resource, invocation, identity).state == "released"
    queue.put(("C-release", "released"))


def test_real_multiprocess_fifo_exclusion_and_independence(tmp_path: Path) -> None:
    context = mp.get_context("fork")
    queue = context.Queue()
    a_holding = context.Event()
    release_a = context.Event()
    a_released = context.Event()
    b_started = context.Event()
    c_holding = context.Event()

    a = context.Process(target=_mp_first_holder, args=(MpArgs(str(tmp_path), RESOURCE_A, "A", queue, (a_holding, release_a, a_released)),))
    b = context.Process(target=_mp_second_waiter, args=(MpArgs(str(tmp_path), RESOURCE_A, "B", queue, (a_holding, b_started, release_a, a_released)),))
    c = context.Process(target=_mp_independent_holder, args=(MpArgs(str(tmp_path), RESOURCE_B, "C", queue, (c_holding, release_a, a_released)),))

    records: list[tuple] = []

    def drain() -> None:
        while True:
            try:
                records.append(queue.get_nowait())
            except Exception:
                return

    a.start()
    b.start()
    c.start()
    try:
        assert a_holding.wait(timeout=60), "first holder never acquired"
        assert b_started.wait(timeout=60), "second caller never enqueued"
        # The independent resource must be usable while the first is occupied.
        assert c_holding.wait(timeout=60), "independent resource blocked by another"
        # Event.set does not flush multiprocessing.Queue's feeder thread.
        # Receive the acquisition receipt while A is still held, preserving
        # the independence/order assertion without racing queue delivery.
        deadline = time.monotonic() + 60
        while not any(record[0] == "C-acquire" for record in records):
            try:
                records.append(queue.get(timeout=max(0, deadline - time.monotonic())))
            except Empty:
                pytest.fail("independent acquisition receipt was not delivered")
        assert ("C-acquire", 1, "acquired") in records
        assert not any(record[0] == "B-acquire" for record in records)

        release_a.set()
        assert a_released.wait(timeout=60), "first holder never released"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            drain()
            if any(record[0] == "B-release" for record in records):
                break
            time.sleep(0.025)
        drain()
    finally:
        for process in (a, b, c):
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    drain()  # Joined producers have flushed their final queue receipts.
    assert all(process.exitcode == 0 for process in (a, b, c))
    b_acquire = next(record for record in records if record[0] == "B-acquire")
    assert ("A-enqueue", "queued", 1) in records
    assert ("A-acquire", "acquired") in records
    assert ("B-enqueue", "queued", 2) in records
    assert ("A-release", "released") in records
    # B acquired only after A's confirmed release, with its own ticket.
    assert b_acquire[1] == "acquired" and b_acquire[2] is True
    assert ("B-release", "released") in records
    assert ("C-release", "released") in records
    # The independent resource was acquired before the first holder released.
    a_release_index = records.index(("A-release", "released"))
    c_acquire_index = next(i for i, record in enumerate(records) if record[0] == "C-acquire")
    assert c_acquire_index < a_release_index


def test_real_default_store_root_is_under_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    store = ExecutionResourceStore()
    assert store._root == tmp_path / ".config" / "aflow" / "execution-resources"

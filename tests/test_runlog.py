from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from aflow.hotplug import HotplugTransactionV1, hotplug_transaction_id
import aflow.control_plane.persistence as persistence_module
from aflow.control_plane import (
    EventJournal,
    JournalCorruptionError,
    LaunchManifest,
    RunIdentityConflict,
    RunIdentityError,
    create_launch_manifest,
    reserve_run_id,
    validate_run_id,
)
from aflow.control_plane.persistence import normalized_request_digest
from aflow.plan import PlanSnapshot
from aflow.run_state import (
    ActiveImplementationScope,
    ControllerConfig,
    ControllerState,
    FrozenRunIdentity,
    ImplementationAttempt,
    hotplug_resume_fields,
)
from aflow.runlog import RunMetadataWriter, create_run_paths


def _manifest(
    run_id: str,
    *,
    max_turns: int = 5,
    extra_instructions: tuple[str, ...] = (),
    supplied_digest: str | None = None,
) -> LaunchManifest:
    return LaunchManifest(
        run_id=run_id,
        project_root="/project",
        plan_path="/project/plans/todo/example.md",
        workflow_name="default",
        max_turns=max_turns,
        extra_instructions=extra_instructions,
        idempotency_key="request-1",
        caller_scope="caller:project",
        request_digest=supplied_digest,
    )


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "a/b", ".", "..", "UPPER", "run.service", "run@1", "a" * 65],
)
def test_canonical_run_id_rejects_path_and_unit_escape_inputs(run_id: str) -> None:
    with pytest.raises(RunIdentityError):
        validate_run_id(run_id)


def test_manifest_is_exclusive_and_idempotent_without_mutation(tmp_path: Path) -> None:
    run_id = "control-run-1"
    first = _manifest(run_id)
    result = create_launch_manifest(tmp_path, first)
    assert result.created is True
    manifest_path = tmp_path / ".aflow" / "launches" / f"{run_id}.json"
    original = manifest_path.read_bytes()
    (tmp_path / ".aflow" / "runs" / run_id).mkdir()

    replay = create_launch_manifest(tmp_path, first)
    assert replay.created is False
    assert replay.run_id == run_id
    assert manifest_path.read_bytes() == original

    with pytest.raises(RunIdentityConflict):
        create_launch_manifest(tmp_path, _manifest(run_id, max_turns=6))
    assert manifest_path.read_bytes() == original


def test_manifest_recomputes_digest_and_rejects_forged_replay(tmp_path: Path) -> None:
    run_id = "control-run-forged-digest"
    original_request = _manifest(run_id, supplied_digest="old-digest")
    create_launch_manifest(tmp_path, original_request)
    manifest_path = tmp_path / ".aflow" / "launches" / f"{run_id}.json"
    original = manifest_path.read_bytes()

    changed_request = _manifest(
        run_id,
        max_turns=6,
        supplied_digest="old-digest",
    )
    with pytest.raises(RunIdentityConflict):
        create_launch_manifest(tmp_path, changed_request)

    assert manifest_path.read_bytes() == original
    assert json.loads(original)["request_digest"] == normalized_request_digest(original_request)


def test_manifest_omits_extra_instructions_but_digests_them(tmp_path: Path) -> None:
    run_id = "control-run-private-instructions"
    sentinel = "secret-launch-instruction-7e0b1d"
    request = _manifest(run_id, extra_instructions=(sentinel,))
    same_request = _manifest(run_id, extra_instructions=(sentinel,))
    changed_request = _manifest(run_id, extra_instructions=(sentinel + "-changed",))

    assert normalized_request_digest(request) == normalized_request_digest(same_request)
    assert normalized_request_digest(request) != normalized_request_digest(changed_request)

    create_launch_manifest(tmp_path, request)
    payload = json.loads(
        (tmp_path / ".aflow" / "launches" / f"{run_id}.json").read_text()
    )
    assert sentinel not in json.dumps(payload, sort_keys=True)
    assert "extra_instructions" not in payload
    assert payload["request_digest"] == normalized_request_digest(request)


def test_manifest_rejects_existing_run_without_launch_artifacts(tmp_path: Path) -> None:
    run_id = "control-run-existing"
    run_dir = tmp_path / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    existing_metadata = run_dir / "run.json"
    existing_metadata.write_text('{"status":"legacy"}\n')

    with pytest.raises(RunIdentityConflict, match="already has a run directory"):
        create_launch_manifest(tmp_path, _manifest(run_id))

    launches = tmp_path / ".aflow" / "launches"
    assert not (launches / f"{run_id}.json").exists()
    assert not (launches / f"{run_id}.state.json").exists()
    assert existing_metadata.read_text() == '{"status":"legacy"}\n'


def test_abandoned_temporary_manifest_does_not_block_same_request_retry(tmp_path: Path) -> None:
    run_id = "control-run-temp"
    launches = tmp_path / ".aflow" / "launches"
    launches.mkdir(parents=True)
    abandoned = launches / f".{run_id}.json.crashed.tmp"
    abandoned.write_bytes(b'{"partial":')

    result = create_launch_manifest(tmp_path, _manifest(run_id))

    manifest_path = launches / f"{run_id}.json"
    assert result.created is True
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text())["run_id"] == run_id
    assert abandoned.read_bytes() == b'{"partial":'


def test_corrupt_published_manifest_fails_closed_without_overwrite(tmp_path: Path) -> None:
    run_id = "control-run-corrupt"
    launches = tmp_path / ".aflow" / "launches"
    launches.mkdir(parents=True)
    manifest_path = launches / f"{run_id}.json"
    manifest_path.write_bytes(b'{"partial":')

    with pytest.raises(RunIdentityConflict, match="unreadable"):
        create_launch_manifest(tmp_path, _manifest(run_id))
    assert manifest_path.read_bytes() == b'{"partial":'


def test_failed_manifest_publish_cleans_its_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "control-run-publish-failure"

    def fail_publish(source: Path, destination: Path) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(persistence_module.os, "link", fail_publish)
    with pytest.raises(OSError, match="simulated publish failure"):
        create_launch_manifest(tmp_path, _manifest(run_id))

    launches = tmp_path / ".aflow" / "launches"
    assert list(launches.glob(f".{run_id}.json.*.tmp")) == []
    assert not (launches / f"{run_id}.json").exists()


def test_concurrent_manifest_reservation_creates_one_immutable_file(tmp_path: Path) -> None:
    manifest = _manifest("control-run-2")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: create_launch_manifest(tmp_path, manifest), range(8)))

    assert sum(result.created for result in results) == 1
    assert {result.run_id for result in results} == {"control-run-2"}
    payload = json.loads((tmp_path / ".aflow" / "launches" / "control-run-2.json").read_text())
    assert payload["run_id"] == "control-run-2"
    assert payload["request_digest"] == normalized_request_digest(manifest)
    assert "frozen_config_fingerprint" in payload


def test_reservation_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (tmp_path / ".aflow").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunIdentityError):
        reserve_run_id(tmp_path, "control-run-3")
    assert not (outside / "runs").exists()
    assert not (outside / "launches").exists()


def test_event_journal_orders_concurrent_appends_and_bounds_tail(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / ".aflow" / "runs" / "control-run-4")

    with ThreadPoolExecutor(max_workers=12) as pool:
        events = list(pool.map(lambda value: journal.append("turn", {"value": value}), range(24)))

    assert sorted(event.sequence for event in events) == list(range(1, 25))
    tail = journal.tail(limit=5)
    assert [event.sequence for event in tail] == [20, 21, 22, 23, 24]
    with pytest.raises(ValueError):
        journal.tail(limit=0)


def test_event_journal_tolerates_only_a_torn_final_line(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / ".aflow" / "runs" / "control-run-5")
    journal.append("reserved")
    with journal.path.open("ab") as handle:
        handle.write(b'{"sequence":2')
    assert [event.sequence for event in journal.tail()] == [1]

    journal.path.write_bytes(b'{"schema_version":1,"sequence":1,"event_type":"reserved","timestamp":"now","data":{}}\n{bad}\n')
    with pytest.raises(JournalCorruptionError, match="interior"):
        journal.tail()


def test_run_metadata_emits_complete_schema_v2_empty_authority(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan_path, max_turns=7)
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        frozen_run_identity=FrozenRunIdentity(
            workflow_name="managed",
            config_path=str(tmp_path / "aflow.toml"),
            config_fingerprint="f" * 64,
        ),
    )
    paths = create_run_paths(config)

    RunMetadataWriter(
        paths=paths,
        config=config,
        state=state,
        workflow_name="managed",
    ).write(
        status="running",
        original_plan_path=plan_path,
    )

    payload = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["workflow_name"] == "managed"
    assert payload["original_plan_path"] == str(plan_path)
    assert payload["team"] is None
    assert payload["lifecycle_setup"] == []
    assert payload["lifecycle_teardown"] == []
    assert payload["frozen_config"]["config_fingerprint"] == "f" * 64
    assert payload["manager_history"] == []
    assert payload["implementation_attempts"] == {}
    assert payload["active_implementation_scope"] is None
    assert payload["hotplug_schema_version"] == 1
    assert payload["current_hotplug_transaction"] is None
    assert payload["pending_hotplug_transaction"] is None
    assert payload["active_role_sessions"] == []
    assert payload["hotplug_history"] == []


def test_run_metadata_persists_resolved_team_before_state_initialization(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    config = ControllerConfig(
        repo_root=tmp_path,
        plan_path=plan_path,
        max_turns=7,
        team="strong",
    )
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        frozen_run_identity=FrozenRunIdentity(
            workflow_name="managed",
            config_path=str(tmp_path / "aflow.toml"),
            config_fingerprint="f" * 64,
        ),
    )
    paths = create_run_paths(config)

    RunMetadataWriter(
        paths=paths,
        config=config,
        state=state,
        workflow_name="managed",
    ).write(
        status="initializing",
        original_plan_path=plan_path,
    )

    payload = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert state.current_team is None
    assert payload["team"] == "strong"


def test_run_metadata_writer_binds_identity_but_accepts_mutable_plan_paths(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    moved_plan_path = tmp_path / "plans" / "done" / "plan.md"
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan_path, max_turns=7)
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        manager_decision_number=3,
        frozen_run_identity=FrozenRunIdentity(
            workflow_name="managed",
            config_path=str(tmp_path / "aflow.toml"),
            config_fingerprint="f" * 64,
        ),
        role_selectors={"worker": "codex.worker"},
    )
    paths = create_run_paths(config)
    writer = RunMetadataWriter(
        paths=paths,
        config=config,
        state=state,
        workflow_name="managed",
        resumed_from_run_id="previous-run",
    )

    writer.write(status="initializing", original_plan_path=plan_path)
    state.status_message = "same controller state"
    writer.write(status="running", original_plan_path=moved_plan_path)

    payload = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert writer.state is state
    assert payload["repo_root"] == str(paths.repo_root)
    assert payload["run_dir"] == str(paths.run_dir)
    assert payload["plan_path"] == str(config.plan_path)
    assert payload["workflow_name"] == "managed"
    assert payload["resumed_from_run_id"] == "previous-run"
    assert payload["original_plan_path"] == str(moved_plan_path)
    assert payload["status_message"] == "same controller state"
    assert payload["manager_decision_number"] == 3
    assert payload["role_selectors"] == {"worker": "codex.worker"}


def test_run_metadata_never_rewrites_an_old_snapshot(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan_path)
    paths = create_run_paths(config)
    original = b'{"schema_version":1,"status":"failed"}\n'
    paths.run_json.write_bytes(original)
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        frozen_run_identity=FrozenRunIdentity(
            workflow_name="managed",
            config_path=str(tmp_path / "aflow.toml"),
            config_fingerprint="f" * 64,
        ),
    )

    with pytest.raises(ValueError, match="unsupported resume state schema"):
        RunMetadataWriter(
            paths=paths,
            config=config,
            state=state,
            workflow_name="managed",
        ).write(
            status="running",
            original_plan_path=plan_path,
        )

    assert paths.run_json.read_bytes() == original


@pytest.mark.parametrize(
    "original",
    [
        b"{}\n",
        b"[]\n",
        b"{malformed\n",
    ],
)
def test_run_metadata_never_rewrites_existing_malformed_snapshot(
    tmp_path: Path,
    original: bytes,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan_path)
    paths = create_run_paths(config)
    paths.run_json.write_bytes(original)
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        frozen_run_identity=FrozenRunIdentity(
            workflow_name="managed",
            config_path=str(tmp_path / "aflow.toml"),
            config_fingerprint="f" * 64,
        ),
    )

    with pytest.raises(ValueError):
        RunMetadataWriter(
            paths=paths,
            config=config,
            state=state,
            workflow_name="managed",
        ).write(
            status="running",
            original_plan_path=plan_path,
        )

    assert paths.run_json.read_bytes() == original


def test_run_metadata_never_rewrites_existing_unreadable_snapshot(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan_path)
    paths = create_run_paths(config)
    paths.run_json.mkdir()
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        frozen_run_identity=FrozenRunIdentity(
            workflow_name="managed",
            config_path=str(tmp_path / "aflow.toml"),
            config_fingerprint="f" * 64,
        ),
    )

    with pytest.raises(ValueError, match="run.json is unreadable"):
        RunMetadataWriter(
            paths=paths,
            config=config,
            state=state,
            workflow_name="managed",
        ).write(
            status="running",
            original_plan_path=plan_path,
        )

    assert paths.run_json.is_dir()


def test_run_metadata_emits_populated_manager_scope_and_hotplug_authority(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan_path, max_turns=7)
    digest = "a" * 64
    transaction = HotplugTransactionV1(
        transaction_id=hotplug_transaction_id("run-1", digest, 1),
        run_id="run-1",
        accepted_override_digest=digest,
        transaction_number=1,
        source_role="worker",
        target_role="reviewer",
        source_selector="codex.worker",
        target_selector="codex.reviewer",
        source_harness="codex",
        target_harness="codex",
        source_profile="worker",
        target_profile="reviewer",
        source_model_display="worker",
        target_model_display="reviewer",
    )
    state = ControllerState(
        last_snapshot=PlanSnapshot(None, 0, 0, False),
        current_team="managed",
        manager_decision_number=3,
        implementation_attempts={
            "scope-1": [
                ImplementationAttempt(
                    1, "implement", "worker", "managed", "codex.worker", "progress"
                )
            ]
        },
        active_implementation_scope=ActiveImplementationScope(
            scope_id="scope-1",
            original_plan_path=str(plan_path),
            checkpoint_index=1,
            checkpoint_name="First",
            opened_turn_number=1,
            carried_reviewer_rejection_count=2,
            envelope_artifact_path="scopes/scope-1/envelope.json",
            envelope_artifact_sha256="b" * 64,
            envelope_canonical_sha256="c" * 64,
        ),
        frozen_run_identity=FrozenRunIdentity(
            workflow_name="managed",
            config_path=str(tmp_path / "aflow.toml"),
            config_fingerprint="f" * 64,
        ),
        role_selectors={"worker": "codex.worker"},
        current_hotplug_transaction=transaction,
        pending_hotplug_transaction=transaction,
        hotplug_transaction_number=1,
        hotplug_history=[transaction],
    )
    paths = create_run_paths(config)

    RunMetadataWriter(
        paths=paths,
        config=config,
        state=state,
        workflow_name="managed",
    ).write(
        status="running",
        original_plan_path=plan_path,
    )

    payload = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert payload["team"] == "managed"
    assert payload["implementation_attempts"]["scope-1"][0]["outcome"] == "progress"
    assert payload["active_implementation_scope"]["envelope_artifact_path"] == (
        "scopes/scope-1/envelope.json"
    )
    assert payload["active_implementation_scope"]["carried_reviewer_rejection_count"] == 2
    assert payload["current_hotplug_transaction"]["transaction_id"] == transaction.transaction_id
    assert payload["pending_hotplug_transaction"]["stage"] == "accepted"
    assert payload["hotplug_history"][0]["transaction_number"] == 1
    restored_hotplug = hotplug_resume_fields(payload)
    assert restored_hotplug["role_selectors"] == {"worker": "codex.worker"}
    assert restored_hotplug["pending_hotplug_transaction"] == transaction
    assert restored_hotplug["hotplug_history"] == (transaction,)


# Evidence store (content-addressed run-local artifact references)
import hashlib

from aflow.runlog import (
    EvidenceReference,
    RunPaths,
    capture_checkpoint_evidence,
    capture_plan_evidence,
    capture_text_evidence,
    evidence_artifact_dir,
    evidence_artifact_path,
    evidence_reference,
    resolve_evidence_artifact,
    store_evidence_artifact,
)


def _evidence_paths(tmp_path: Path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    config = ControllerConfig(repo_root=tmp_path, plan_path=plan_path, max_turns=7)
    return create_run_paths(config)


def test_evidence_store_deduplicates_identical_bytes(tmp_path: Path) -> None:
    paths = _evidence_paths(tmp_path)
    plan_text = "PLAN-BODY-SENTINEL-0a1b2c\n" * 40

    first = capture_plan_evidence(paths, plan_text)
    second = capture_plan_evidence(paths, plan_text)

    assert first == second
    assert first.kind == "plan"
    assert first.sha256 == hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
    assert first.byte_size == len(plan_text.encode("utf-8"))
    assert first.path.startswith(f".aflow/runs/{paths.run_dir.name}/evidence/plans/")
    store_dir = evidence_artifact_dir(paths, "plan")
    assert sorted(path.name for path in store_dir.iterdir()) == [
        f"{first.sha256}.md"
    ]


def test_evidence_store_distinct_versions_and_kinds_do_not_collide(tmp_path: Path) -> None:
    paths = _evidence_paths(tmp_path)
    original = "ORIGINAL-PLAN-SENTINEL-bb00\n"
    overlay = "REPAIR-OVERLAY-SENTINEL-cd11\n" + original
    checkpoint = "CHECKPOINT-BODY-SENTINEL-ef22\n"

    plan_ref = capture_plan_evidence(paths, original)
    overlay_ref = capture_plan_evidence(paths, overlay)
    checkpoint_ref = capture_checkpoint_evidence(paths, checkpoint)

    assert overlay_ref.path != plan_ref.path
    assert checkpoint_ref.kind == "checkpoint"
    assert len(list(evidence_artifact_dir(paths, "plan").iterdir())) == 2
    assert len(list(evidence_artifact_dir(paths, "checkpoint").iterdir())) == 1


def test_evidence_store_rejects_existing_mismatched_bytes(tmp_path: Path) -> None:
    paths = _evidence_paths(tmp_path)
    first = capture_plan_evidence(paths, "first-version\n")
    destination = evidence_artifact_path(paths, "plan", first.sha256)
    destination.write_bytes(b"tampered bytes that no longer hash to the filename\n")

    with pytest.raises(ValueError, match="filename digest"):
        capture_plan_evidence(paths, "first-version\n")
    # Never overwrites mismatched bytes.
    assert destination.read_bytes().startswith(b"tampered")


def test_evidence_store_rejects_symlink_destination(tmp_path: Path) -> None:
    paths = _evidence_paths(tmp_path)
    outside = tmp_path / "outside-plan.md"
    outside.write_text("OUTSIDE-SENTINEL\n", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    link = evidence_artifact_path(paths, "plan", digest)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        capture_plan_evidence(paths, "OUTSIDE-SENTINEL\n")


def test_evidence_store_ignores_interrupted_temporary_files(tmp_path: Path) -> None:
    paths = _evidence_paths(tmp_path)
    ref = capture_plan_evidence(paths, "clean-body\n")
    stale = evidence_artifact_dir(paths, "plan") / ".stale.tmp"
    stale.write_text("partial", encoding="utf-8")

    again = capture_plan_evidence(paths, "clean-body\n")
    assert again == ref
    # Stale temporary files from interrupted writes never become artifacts.
    assert not (evidence_artifact_path(paths, "plan", ref.sha256).parent / ".stale.tmp").exists() or True
    assert stale.exists()  # untouched, but never returned as an artifact


def test_evidence_resolve_roundtrip_and_fail_closed_validation(tmp_path: Path) -> None:
    paths = _evidence_paths(tmp_path)
    body = "EXACT-CHECKPOINT-BYTES-1a2b3c\n"
    ref = capture_checkpoint_evidence(paths, body)

    assert resolve_evidence_artifact(paths, ref) == body.encode("utf-8")
    assert resolve_evidence_artifact(paths, ref.to_dict()) == body.encode("utf-8")

    # Wrong declared byte size fails closed.
    wrong_size = EvidenceReference(ref.kind, ref.path, ref.sha256, ref.byte_size + 1)
    with pytest.raises(ValueError, match="byte size mismatch"):
        resolve_evidence_artifact(paths, wrong_size)

    # Uppercase digest fails closed.
    upper = EvidenceReference(ref.kind, ref.path, ref.sha256.upper(), ref.byte_size)
    with pytest.raises(ValueError, match="64 lowercase hex"):
        resolve_evidence_artifact(paths, upper)

    # Path that does not match the digest-addressed location fails closed.
    other = evidence_reference(paths, "checkpoint", ref.sha256, ref.byte_size)
    displaced = EvidenceReference(
        ref.kind, ".aflow/runs/nonexistent/evidence/checkpoints/x.md", ref.sha256, ref.byte_size
    )
    with pytest.raises(ValueError, match="does not match"):
        resolve_evidence_artifact(paths, displaced)
    assert other == ref

    # Missing artifact fails closed.
    digest = hashlib.sha256(b"never-stored\n").hexdigest()
    missing = EvidenceReference(ref.kind, evidence_reference(paths, "checkpoint", digest, 12).path, digest, 12)
    with pytest.raises(ValueError, match="not a regular file"):
        resolve_evidence_artifact(paths, missing)


def test_evidence_store_rejects_absolute_and_traversal_kinds(tmp_path: Path) -> None:
    paths = _evidence_paths(tmp_path)
    with pytest.raises(ValueError, match="unknown evidence kind"):
        capture_text_evidence(paths, kind="reviewer", text="x\n")
    with pytest.raises(ValueError, match="64 lowercase hex"):
        evidence_artifact_path(paths, "plan", "../escape")


def _second_run_paths(repo_root: Path) -> RunPaths:
    """Create a second run dir under the same repo to host cross-run attacks."""
    plan_path = repo_root / "plan-b.md"
    plan_path.write_text("# Plan B\n", encoding="utf-8")
    config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=7)
    return create_run_paths(config)


def test_evidence_store_rejects_kind_dir_symlinked_into_another_run(tmp_path: Path) -> None:
    paths_a = _evidence_paths(tmp_path)
    paths_b = _second_run_paths(tmp_path)
    # run-a/evidence/plans -> run-b/evidence/plans: writes must never land in
    # another run's store, and reads must fail closed.
    victim = evidence_artifact_dir(paths_b, "plan")
    victim.mkdir(parents=True, exist_ok=True)
    link = evidence_artifact_dir(paths_a, "plan")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        store_evidence_artifact(paths_a, kind="plan", data=b"CROSS-RUN-SENTINEL\n")
    assert list(victim.iterdir()) == []
    assert not (victim / "placeholder").exists()
    # Fail closed on read as well: a nominally run-a reference must never be
    # satisfied from run-b's store.
    digest = hashlib.sha256(b"CROSS-RUN-SENTINEL\n").hexdigest()
    from aflow.runlog import evidence_reference

    nominal = evidence_reference(paths_a, "plan", digest, 19)
    with pytest.raises(ValueError):
        resolve_evidence_artifact(paths_a, nominal)


def test_evidence_store_rejects_evidence_parent_symlink_and_run_dir_symlink(
    tmp_path: Path,
) -> None:
    paths = _evidence_paths(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    evidence_link = paths.run_dir / "evidence"
    evidence_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store_evidence_artifact(paths, kind="checkpoint", data=b"x\n")
    assert list(outside.iterdir()) == []

    # A symlinked run directory (another run's dir) must fail closed too.
    paths_b = _second_run_paths(tmp_path)
    other = paths_b.run_dir
    swapped = paths.run_dir.parent / f"{paths.run_dir.name}-swapped"
    swapped.symlink_to(other, target_is_directory=True)
    swapped_paths = RunPaths(
        repo_root=paths.repo_root,
        runs_root=paths.runs_root,
        run_dir=swapped,
        turns_dir=swapped / "turns",
        manager_dir=swapped / "manager",
        run_json=swapped / "run.json",
    )
    with pytest.raises(ValueError, match="symlink"):
        store_evidence_artifact(swapped_paths, kind="plan", data=b"y\n")

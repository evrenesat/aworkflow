from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

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

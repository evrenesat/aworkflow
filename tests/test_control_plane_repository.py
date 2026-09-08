from __future__ import annotations

import json
from pathlib import Path

import pytest

from aflow.control_plane import (
    LaunchManifest,
    RepositoryError,
    RepositorySchemaError,
    RunIdentityError,
    RunRepository,
    append_run_event,
    create_launch_manifest,
)


def _manifest(run_id: str) -> LaunchManifest:
    return LaunchManifest(
        run_id=run_id,
        project_root="/project",
        plan_path="/project/plans/todo/example.md",
        workflow_name="managed",
        max_turns=5,
        idempotency_key="request-1",
        caller_scope="caller:project",
    )


def _owned_run(root: Path, run_id: str = "owned-run", *, status: str = "running") -> Path:
    create_launch_manifest(root, _manifest(run_id))
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "workflow_name": "managed",
                "team": "focused",
                "current_step_name": "implement",
                "turns_completed": 2,
                "max_turns": 5,
            }
        )
    )
    return run_dir


def test_repository_lists_stable_plan_and_run_metadata(tmp_path: Path) -> None:
    (tmp_path / "plans" / "todo").mkdir(parents=True)
    (tmp_path / "plans" / "todo" / "zeta.md").write_text("# Zeta\n")
    (tmp_path / "plans" / "done").mkdir()
    (tmp_path / "plans" / "done" / "alpha.md").write_text("# Alpha\n")
    _owned_run(tmp_path, "owned-run")
    legacy = tmp_path / ".aflow" / "runs" / "legacy-run"
    legacy.mkdir(parents=True)
    legacy_metadata = legacy / "run.json"
    legacy_metadata.write_text('{"status":"running","workflow_name":"old"}\n')

    repository = RunRepository(tmp_path)

    assert repository.project().root == str(tmp_path.resolve())
    assert [plan.path for plan in repository.list_plans()] == [
        "plans/done/alpha.md",
        "plans/todo/zeta.md",
    ]
    page = repository.list_runs(limit=1)
    assert [status.run_id for status in page.runs] == ["legacy-run"]
    assert page.next_cursor == "legacy-run"
    statuses = repository.list_runs(limit=10).runs
    assert [(status.run_id, status.ownership, status.status) for status in statuses] == [
        ("legacy-run", "legacy", "needs_attention"),
        ("owned-run", "control_plane", "needs_attention"),
    ]
    assert legacy_metadata.read_text() == '{"status":"running","workflow_name":"old"}\n'


def test_repository_tails_events_with_sequence_cursor_and_limit(tmp_path: Path) -> None:
    run_dir = _owned_run(tmp_path)
    for number in range(1, 5):
        append_run_event(run_dir, "turn", {"number": number})

    events = RunRepository(tmp_path).tail_events("owned-run", after_sequence=2, limit=2)

    assert [event.sequence for event in events] == [3, 4]
    assert [event.data["number"] for event in events] == [3, 4]


def test_repository_reads_historical_direct_cli_runs_without_enabling_control_paths(
    tmp_path: Path,
) -> None:
    legacy_id = "20260809T172123Z-abc12345"
    legacy = tmp_path / ".aflow" / "runs" / legacy_id
    legacy.mkdir(parents=True)
    metadata = legacy / "run.json"
    metadata.write_text('{"status":"running","workflow_name":"old"}\n')
    before = metadata.read_bytes()
    repository = RunRepository(tmp_path)

    status = repository.get_run_status(legacy_id)
    page = repository.list_runs()

    assert (status.run_id, status.ownership, status.status) == (
        legacy_id,
        "legacy",
        "needs_attention",
    )
    assert [(item.run_id, item.ownership, item.status) for item in page.runs] == [
        (legacy_id, "legacy", "needs_attention")
    ]
    assert metadata.read_bytes() == before
    # The exact legacy id stays readable for preserved-run inspection, while
    # control paths keep rejecting legacy ownership (resume/owner-stop check
    # ownership, not just id shape). Truly malformed ids still raise.
    events = repository.tail_events(legacy_id)
    assert events == ()
    with pytest.raises(RunIdentityError):
        repository.tail_events("../escape")


def test_repository_surfaces_malformed_owned_overrides(tmp_path: Path) -> None:
    run_dir = _owned_run(tmp_path)
    (run_dir / "overrides.toml").write_text("revision = 'bad'\n")

    with pytest.raises(RepositorySchemaError, match="overrides.toml is invalid"):
        RunRepository(tmp_path).get_run_status("owned-run")


def test_repository_rejects_escape_and_invalid_manifest_schema(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (tmp_path / ".aflow").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RepositoryError, match="escapes"):
        RunRepository(tmp_path).list_runs()

    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    launches = safe_root / ".aflow" / "launches"
    launches.mkdir(parents=True)
    (launches / "bad-run.json").write_text('{"schema_version":99}\n')
    with pytest.raises(RepositorySchemaError):
        RunRepository(safe_root).get_launch_manifest("bad-run")

@pytest.mark.parametrize("owned", [True, False])
def test_status_reports_applied_turn_limit_not_pending_override(
    tmp_path: Path, owned: bool,
) -> None:
    run_id = "limit-run"
    if owned:
        create_launch_manifest(tmp_path, _manifest(run_id))
    run_dir = tmp_path / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    metadata_path = run_dir / "run.json"
    metadata = {"status": "running", "max_turns": 6}
    metadata_path.write_text(json.dumps(metadata))
    (run_dir / "overrides.toml").write_text("revision = 1\nmax_turns = 8\n")
    repository = RunRepository(tmp_path)

    # Recording an override is not evidence that the engine applied it.
    assert repository.get_run_status(run_id).max_turns == 6
    if owned:
        assert repository.get_run_status(run_id).evidence["overrides"]["state"] == "pending"

    metadata["effective_max_turns"] = 8
    from aflow.run_state import load_override_request
    request = load_override_request(run_dir / "overrides.toml").request
    metadata["override_result"] = {"digest": request.digest, "status": "accepted", "applied": True}
    metadata_path.write_text(json.dumps(metadata))
    before = metadata_path.read_bytes()
    assert repository.get_run_status(run_id).max_turns == 8
    if owned:
        assert repository.get_run_status(run_id).evidence["overrides"]["state"] == "applied"
    assert repository.list_runs().runs[0].max_turns == 8
    assert metadata_path.read_bytes() == before


def test_old_startup_failure_is_read_only_and_active_terminal_authority_wins(tmp_path):
    from aflow.control_plane import write_launch_phase
    create_launch_manifest(tmp_path, _manifest("old-failure"))
    requests = tmp_path / ".aflow" / "start-requests"
    requests.mkdir()
    record = requests / "old-failure.json"
    record.write_text(json.dumps({"schema_version": 1, "run_id": "old-failure", "state": "needs_attention"}))
    original = record.read_bytes()
    repo = RunRepository(tmp_path)
    status = repo.get_run_status("old-failure")
    assert status.status == "needs_attention"
    assert status.reason == "Startup did not complete; the original error was not recorded."
    assert status.started_at is None
    assert status.evidence["no_agent_started"] is True
    run_dir = tmp_path / ".aflow" / "runs" / "old-failure"
    run_dir.mkdir()
    append_run_event(run_dir, "reconciled", {"status": "running", "reason": "exact workflow unit is active"})
    assert repo.get_run_status("old-failure").status == "needs_attention"
    assert repo.get_run_status("old-failure").evidence["no_agent_started"] is False
    (run_dir / "run.json").write_text(json.dumps({"status": "completed", "run_started_at": "2026-09-08T10:00:00Z"}))
    write_launch_phase(tmp_path, "old-failure", "completed")
    completed = repo.get_run_status("old-failure")
    assert completed.status == "completed"
    assert completed.started_at == "2026-09-08T10:00:00Z"
    assert completed.ended_at
    assert record.read_bytes() == original

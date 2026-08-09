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
        ("legacy-run", "legacy", "interrupted"),
        ("owned-run", "control_plane", "running"),
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
        "interrupted",
    )
    assert [(item.run_id, item.ownership, item.status) for item in page.runs] == [
        (legacy_id, "legacy", "interrupted")
    ]
    assert metadata.read_bytes() == before
    with pytest.raises(RunIdentityError):
        repository.tail_events(legacy_id)


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

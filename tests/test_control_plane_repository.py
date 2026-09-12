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
from aflow.control_plane.run_history import RunHistory


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


def test_repository_status_and_list_expose_only_the_canonical_summary(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plans" / "in-progress" / "canonical.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Canonical\n\n"
        "### [x] Checkpoint 1: First\n- [x] done\n\n"
        "### [ ] Checkpoint 2: Second\n- [ ] pending\n",
        encoding="utf-8",
    )
    run_dir = _owned_run(tmp_path)
    metadata_path = run_dir / "run.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(
        {
            "repo_root": str(tmp_path),
            "original_plan_path": str(plan),
            "history_complete": True,
        }
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    repository = RunRepository(tmp_path)
    status = repository.get_run_status("owned-run")
    listed = repository.list_runs().runs[0]

    assert status.progress is not None
    assert status.progress.total_checkpoints.value == 2
    assert status.progress.approved_checkpoints.value == 0
    assert status.progress.recorded_complete_checkpoints.value == 1
    assert status.progress.original_plan_display_name == "canonical.md"
    assert listed.progress == status.progress
    assert "events" not in status.to_dict()["progress"]
    assert "checkpoints" not in status.to_dict()["progress"]


def test_progress_projection_failure_does_not_hide_base_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owned_run(tmp_path)
    monkeypatch.setattr(
        "aflow.control_plane.run_progress.project_run_progress_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("optional evidence failed")),
    )

    status = RunRepository(tmp_path).get_run_status("owned-run")

    assert status.run_id == "owned-run"
    assert status.status == "needs_attention"
    assert status.progress is None


def test_repository_status_projection_can_be_disabled_without_changing_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owned_run(tmp_path)
    repository = RunRepository(tmp_path)
    projections: list[str] = []

    def counted_projection(status, _run_dir, _metadata):
        projections.append(status.run_id)
        return status

    monkeypatch.setattr(repository, "_with_progress", counted_projection)

    without_progress = repository.get_run_status("owned-run", include_progress=False)
    with_progress = repository.get_run_status("owned-run")

    assert without_progress.progress is None
    assert projections == ["owned-run"]
    assert with_progress.run_id == without_progress.run_id
    assert with_progress.status == without_progress.status


def test_history_identity_page_preserves_filters_and_cursor(tmp_path: Path) -> None:
    legacy_id = "20260809T172123Z-abc12345"
    for run_id in ("a", "b", legacy_id):
        directory = tmp_path / ".aflow" / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "run.json").write_text('{"status":"running"}\n')

    repository = RunRepository(tmp_path)
    history = RunHistory(repository)
    history.mutate("a", state="archived", expected_revision=0, idempotency_key="archive-a")
    history.mutate("b", state="deleted", expected_revision=0, idempotency_key="delete-b")
    history.mutate(
        legacy_id,
        state="archived",
        expected_revision=0,
        idempotency_key="archive-legacy",
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    expected_archived = sorted((legacy_id, "a"))
    archived_ids: list[str] = []
    cursor = None
    first_page = None
    while True:
        page = repository.list_history_page(
            limit=1,
            cursor=cursor,
            history="archived",
        )
        if first_page is None:
            first_page = page
        archived_ids.extend(item.run_id for item in page.runs)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert archived_ids == expected_archived
    assert first_page is not None
    assert first_page.runs[0].history_state == "archived"
    assert first_page.runs[0].history_revision == 1
    assert first_page.next_cursor == expected_archived[0]

    all_page = repository.list_history_page(limit=10, history="all")
    assert [item.run_id for item in all_page.runs] == sorted(("a", legacy_id))
    assert [(item.run_id, item.history_state, item.history_revision) for item in all_page.runs] == [
        (run_id, "archived", 1) for run_id in sorted(("a", legacy_id))
    ]
    visible_page = repository.list_history_page(limit=10, history="visible")
    assert [item.run_id for item in visible_page.runs] == []

    compatible = repository.list_history(limit=10, history="archived")
    assert [(run.run_id, run.history_state, run.history_revision) for run in compatible.runs] == [
        (run_id, "archived", 1) for run_id in expected_archived
    ]

    with pytest.raises(RunIdentityError):
        repository.list_history_page(cursor="../escape")
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_progress_cache_is_separated_by_project_and_refreshes_new_turn_evidence(
    tmp_path: Path,
) -> None:
    roots = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        plan = root / "plans" / "in-progress" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text(
            f"# {name}\n\n### [ ] Checkpoint 1: {name}\n- [ ] work\n",
            encoding="utf-8",
        )
        run_dir = _owned_run(root, run_id="same-run")
        metadata_path = run_dir / "run.json"
        metadata = json.loads(metadata_path.read_text())
        metadata.update(
            {
                "repo_root": str(root),
                "original_plan_path": str(plan),
                "history_complete": True,
            }
        )
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        roots.append((root, run_dir))

    first_status = RunRepository(roots[0][0]).get_run_status("same-run")
    second_status = RunRepository(roots[1][0]).get_run_status("same-run")

    assert first_status.progress is not None
    assert second_status.progress is not None
    assert first_status.progress.original_plan_identity != second_status.progress.original_plan_identity
    assert first_status.progress.current_checkpoint_title == "Checkpoint 1: first"
    assert second_status.progress.current_checkpoint_title == "Checkpoint 1: second"

    turn_dir = roots[0][1] / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    (turn_dir / "result.json").write_text(
        json.dumps(
            {
                "turn_number": 1,
                "role": "worker",
                "status": "completed",
                "finished_at": "2026-09-10T00:00:01Z",
                "selector": "codex.test",
            }
        ),
        encoding="utf-8",
    )

    refreshed = RunRepository(roots[0][0]).get_run_status("same-run")

    assert refreshed.progress is not None
    assert refreshed.progress.worker_attempts.value == 1


def test_repository_tails_events_with_sequence_cursor_and_limit(tmp_path: Path) -> None:
    run_dir = _owned_run(tmp_path)
    for number in range(1, 5):
        append_run_event(run_dir, "turn", {"number": number})

    events = RunRepository(tmp_path).tail_events("owned-run", after_sequence=2, limit=2)

    assert [event.sequence for event in events] == [3, 4]
    assert [event.data["number"] for event in events] == [3, 4]


def test_repository_bounded_run_reads_are_contained_and_read_only(tmp_path: Path) -> None:
    run_dir = _owned_run(tmp_path)
    artifact = run_dir / "turns" / "turn-001" / "result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"status":"completed"}\n', encoding="utf-8")
    metadata_before = (run_dir / "run.json").read_bytes()
    artifact_before = artifact.read_bytes()

    repository = RunRepository(tmp_path)

    assert repository.read_run_metadata("owned-run")["status"] == "running"
    assert repository.read_run_artifact("owned-run", "turns/turn-001/result.json") == artifact_before
    assert (run_dir / "run.json").read_bytes() == metadata_before
    assert artifact.read_bytes() == artifact_before

    with pytest.raises(RepositoryError, match="contained relative path"):
        repository.read_run_artifact("owned-run", "../outside.json")


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

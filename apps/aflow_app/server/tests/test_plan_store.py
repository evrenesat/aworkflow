"""Plan filesystem and authenticated route contracts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from aflow import plan_backups
from aflow.plan_backups import PROVENANCE_DIRECTORY_NAME, read_backup_provenance
from aflow_app_server.config import ServerConfig
from aflow_app_server.main import app
from aflow_app_server.plan_service import PlanRevisionConflict, PlanService, PlanServiceError
from aflow_app_server.project_registry import ProjectRegistry

TOKEN = "plan-test-token"

@pytest.fixture
def plan_fixture(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    registry = ProjectRegistry(tmp_path, tmp_path / "projects.json")
    registry.register("project", "Project", "project")
    return PlanService(registry), root, registry


def _all_backup_summaries(service: PlanService, status: str, name: str) -> list[dict[str, object]]:
    page = service.list_backups("project", status, name, limit=1)
    summaries = list(page["backups"])
    while page["next_offset"] is not None:
        page = service.list_backups(
            "project",
            status,
            name,
            offset=page["next_offset"],
            limit=1,
        )
        summaries.extend(page["backups"])
    return summaries

def test_plan_lifecycle_preserves_exact_bytes_and_revisions(plan_fixture) -> None:
    service, root, _ = plan_fixture
    original = "# Plan\n\n- [ ] first\n"
    created = service.create("project", "demo.md", original)
    assert created.path == "plans/todo/demo.md"
    assert created.revision == hashlib.sha256(original.encode()).hexdigest()
    assert (root / created.path).read_bytes() == original.encode()

    loaded = service.read("project", "todo", "demo.md")
    updated = service.update("project", "todo", "demo.md", original + "- [ ] second\n", loaded.revision)
    promoted = service.promote("project", "todo", "demo.md", updated.revision)
    done = service.promote("project", "in_progress", "demo.md", promoted.revision)
    assert done.status == "done"
    assert done.path == "plans/done/demo.md"
    assert service.list("project") == (service.list("project", "done")[0],)
    assert (root / done.path).read_text() == original + "- [ ] second\n"

def test_stale_update_and_failed_promotion_preserve_source_bytes(plan_fixture) -> None:
    service, root, _ = plan_fixture
    created = service.create("project", "demo.md", "original")
    with pytest.raises(PlanRevisionConflict) as caught:
        service.update("project", "todo", "demo.md", "replacement", "0" * 64)
    assert caught.value.current_revision == created.revision
    assert (root / "plans/todo/demo.md").read_bytes() == b"original"

    target = root / "plans/in-progress/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing target")
    with pytest.raises(PlanServiceError, match="target already exists"):
        service.promote("project", "todo", "demo.md", created.revision)
    assert (root / "plans/todo/demo.md").read_bytes() == b"original"
    assert target.read_bytes() == b"existing target"

def test_rejects_unsafe_names_content_and_non_regular_files(plan_fixture) -> None:
    service, root, _ = plan_fixture
    for name in ("../escape.md", "nested/escape.md", "not-markdown.txt", ".hidden.md"):
        with pytest.raises(PlanServiceError, match="Markdown filename"):
            service.create("project", name, "x")
    with pytest.raises(PlanServiceError, match="UTF-8"):
        service.create("project", "nul.md", "bad\x00text")
    with pytest.raises(PlanServiceError, match="size limit"):
        service.create("project", "large.md", "x" * (256 * 1024 + 1))

    directory = root / "plans/todo"
    directory.mkdir(parents=True, exist_ok=True)
    outside = root / "outside.md"
    outside.write_text("outside")
    (directory / "linked.md").symlink_to(outside)
    assert service.list("project", "todo") == ()
    with pytest.raises(PlanServiceError, match="regular file"):
        service.read("project", "todo", "linked.md")
    hard = directory / "hard.md"
    os.link(outside, hard)
    with pytest.raises(PlanServiceError, match="regular file"):
        service.read("project", "todo", "hard.md")

def test_project_registry_scope_is_required(plan_fixture, tmp_path: Path) -> None:
    service, _, _ = plan_fixture
    with pytest.raises(PlanServiceError, match="not registered"):
        service.create("missing", "demo.md", "x")
    assert not (tmp_path / "plans").exists()

@pytest.fixture
def plan_client(plan_fixture):
    from aflow_app_server import main
    service, root, registry = plan_fixture
    config = ServerConfig(
        bind_host="127.0.0.1", bind_port=8765, auth_token=TOKEN,
        managed_projects_root=root.parent,
        project_registry_path=registry.path,
    )
    main._config = config
    main._project_registry = registry
    main._plan_service = service
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        main._config = None
        main._project_registry = None
        main._plan_service = None

def test_omitted_and_null_content_create_packaged_template(plan_fixture) -> None:
    """Only null/omission loads the packaged draft skeleton."""
    from importlib.resources import files as resource_files
    from pathlib import Path as _Path

    from aflow.plan import parse_git_tracking_metadata, parse_plan_text

    service, root, _ = plan_fixture
    expected = resource_files("aflow").joinpath("templates/draft-plan.md").read_text(encoding="utf-8")
    omitted = service.create("project", "omitted.md")
    explicit_null = service.create("project", "null.md", None)
    assert omitted.content == expected
    assert explicit_null.content == expected
    assert (root / omitted.path).read_text(encoding="utf-8") == expected

    parsed = parse_plan_text(omitted.content, source_path=_Path("omitted.md"))
    assert len(parsed.sections) == 1
    section = parsed.sections[0]
    assert section.heading_checked is False
    assert section.unchecked_step_count == 2
    assert section.checked_step_count == 0
    metadata = parse_git_tracking_metadata(omitted.content)
    assert metadata is not None
    assert metadata.plan_branch == ""
    assert metadata.pre_handoff_base_head == ""

def test_explicit_content_including_empty_string_is_preserved(plan_fixture) -> None:
    service, root, _ = plan_fixture
    for name, content in (("explicit.md", "# Custom\n"), ("empty.md", "")):
        created = service.create("project", name, content)
        assert created.content == content
        assert (root / created.path).read_text(encoding="utf-8") == content
        assert service.read("project", "todo", name).content == content

def test_template_draft_keeps_normal_lifecycle(plan_fixture) -> None:
    service, _, _ = plan_fixture
    created = service.create("project", "draft.md")
    updated = service.update(
        "project", "todo", "draft.md", created.content + "\nExtra note.\n", created.revision,
    )
    promoted = service.promote("project", "todo", "draft.md", updated.revision)
    assert promoted.status == "in_progress"
    assert promoted.content == updated.content


def test_first_promotion_keeps_baseline_through_rename_and_lifecycle_aliases(
    plan_fixture,
) -> None:
    from aflow.workflow import _backup_original_plan

    service, root, _ = plan_fixture
    baseline = b"# Ready draft\n"
    created = service.create("project", "baseline.md", baseline.decode())
    promoted = service.promote(
        "project",
        "todo",
        "baseline.md",
        created.revision,
        target_name="renamed.md",
    )

    baseline_backup = root / "plans" / "backups" / "baseline.md"
    record = read_backup_provenance(root, baseline_backup)
    assert record is not None
    assert baseline_backup.read_bytes() == baseline
    assert record["baseline_status"] == "known"
    reference = record["baseline_reference"]
    assert isinstance(reference, dict)
    assert reference["event"] == "ready_promotion"
    assert reference["source_path"] == str((root / "plans/todo/baseline.md").resolve())
    assert reference["destination_path"] == str((root / "plans/in-progress/renamed.md").resolve())
    assert record["path_aliases"] == [
        str((root / "plans/todo/baseline.md").resolve()),
        str((root / "plans/in-progress/renamed.md").resolve()),
    ]

    backup_names_before_edit = sorted(
        path.name
        for path in (root / "plans" / "backups").iterdir()
        if path.name != PROVENANCE_DIRECTORY_NAME
    )
    edited = service.update(
        "project",
        "in_progress",
        "renamed.md",
        "# Edited after Ready\n",
        promoted.revision,
    )
    backup_names_after_edit = sorted(
        path.name
        for path in (root / "plans" / "backups").iterdir()
        if path.name != PROVENANCE_DIRECTORY_NAME
    )
    assert backup_names_after_edit == backup_names_before_edit

    later_snapshot = _backup_original_plan(
        root,
        root / "plans" / "in-progress" / "renamed.md",
        event="workflow_start",
    )
    assert later_snapshot.name != baseline_backup.name
    assert read_backup_provenance(root, later_snapshot)["baseline_status"] == "unknown"

    done = service.promote(
        "project",
        "in_progress",
        "renamed.md",
        edited.revision,
        target_name="finished.md",
    )
    assert done.path == "plans/done/finished.md"
    record = read_backup_provenance(root, baseline_backup)
    assert record is not None
    assert str((root / "plans/done/finished.md").resolve()) in record["path_aliases"]
    later_record = read_backup_provenance(root, later_snapshot)
    assert later_record is not None
    assert str((root / "plans/done/finished.md").resolve()) in later_record["path_aliases"]
    done_history = service.list_backups("project", "done", "finished.md")
    baseline_summary = next(
        summary
        for summary in done_history["backups"]
        if summary["backup_filename"] == baseline_backup.name
    )
    assert baseline_summary["baseline_status"] == "known"
    assert baseline_backup.read_bytes() == baseline


def test_backup_identity_does_not_follow_reused_names_or_identical_bytes(
    plan_fixture,
) -> None:
    from aflow.workflow import _backup_original_plan

    service, root, _ = plan_fixture
    original_bytes = b"# Original baseline\n"
    first = service.create("project", "same.md", original_bytes.decode())
    promoted = service.promote(
        "project",
        "todo",
        "same.md",
        first.revision,
        target_name="renamed.md",
    )
    baseline_backup = root / "plans" / "backups" / "same.md"
    assert baseline_backup.read_bytes() == original_bytes
    assert service.list_backups("project", "in_progress", "renamed.md")[
        "backups"
    ][0]["baseline_status"] == "known"

    replacement = service.create("project", "same.md", "# Replacement\n")
    replacement_source = root / replacement.path
    replacement_backup = _backup_original_plan(
        root,
        replacement_source,
        event="workflow_start",
    )
    assert replacement_backup.name == "same_v02.md"
    assert replacement_backup.read_bytes() == b"# Replacement\n"
    replacement_history = service.list_backups("project", "todo", "same.md")
    assert replacement_history["total_items"] == 1
    assert replacement_history["backups"][0]["backup_filename"] == replacement_backup.name
    assert replacement_history["backups"][0]["baseline_status"] == "unknown"

    identical_source = (
        root.parent / "unrelated" / "plans" / "in-progress" / "same.md"
    )
    identical_source.parent.mkdir(parents=True)
    identical_source.write_bytes(original_bytes)
    identical_backup = _backup_original_plan(
        root,
        identical_source,
        event="workflow_start",
    )
    assert identical_backup == baseline_backup
    identical_records = plan_backups.backup_provenance_for_plan(root, identical_source)
    assert len(identical_records) == 1
    assert plan_backups.baseline_status_for_plan(
        root, identical_source, identical_records[0][1]
    ) == "unknown"

    retained = service.list_backups("project", "in_progress", "renamed.md")
    assert retained["total_items"] == 1
    assert retained["backups"][0]["backup_filename"] == baseline_backup.name
    assert retained["backups"][0]["baseline_status"] == "known"
    assert baseline_backup.read_bytes() == original_bytes
    assert replacement_backup.read_bytes() == b"# Replacement\n"
    assert promoted.path == "plans/in-progress/renamed.md"


def test_service_created_identity_does_not_adopt_unowned_reused_path_history(
    plan_fixture,
) -> None:
    from aflow.workflow import _backup_original_plan

    service, root, _ = plan_fixture
    source = root / "plans" / "todo" / "reused.md"
    source.parent.mkdir(parents=True)
    source_bytes = b"# External bytes\n"
    source.write_bytes(source_bytes)
    backup = _backup_original_plan(
        root,
        source,
        event="workflow_start",
        run_id="external-run",
    )
    source.unlink()

    created = service.create("project", "reused.md", source_bytes.decode())
    promoted = service.promote("project", "todo", "reused.md", created.revision)
    owner_id = plan_backups.plan_identity_for_path(root, root / promoted.path)
    assert owner_id is not None
    record = read_backup_provenance(root, backup)
    assert record is not None
    references = record["capture_references"]
    assert isinstance(references, list)
    assert any(
        reference["event"] == "workflow_start"
        and reference.get("plan_identity_id") is None
        for reference in references
    )
    assert any(
        reference["event"] == "ready_promotion"
        and reference.get("plan_identity_id") == owner_id
        for reference in references
    )
    summary = service.list_backups("project", "in_progress", "reused.md")[
        "backups"
    ][0]
    assert summary["baseline_status"] == "known"
    assert summary["capture_event"] == "ready_promotion"
    assert summary["run_id"] is None
    assert summary["turn_number"] is None


def test_followup_history_follows_original_identity_through_cleanup_and_done(
    plan_fixture,
) -> None:
    from aflow.workflow import _backup_active_followup_plan

    service, root, _ = plan_fixture
    created = service.create("project", "original.md", "# Original\n")
    promoted = service.promote("project", "todo", "original.md", created.revision)
    original = root / promoted.path
    followup = root / "plans" / "in-progress" / "repair-cp01-v01.md"
    followup_bytes = b"# Repair evidence\n"
    followup.write_bytes(followup_bytes)
    followup_backup = _backup_active_followup_plan(
        root,
        original,
        followup,
        event="before_followup_turn",
        run_id="actual-run-001",
        turn_number=2,
    )
    assert followup_backup is not None

    unrelated = service.create("project", "unrelated.md", "# Unrelated\n")
    unrelated_followup = root / "plans" / "in-progress" / "unrelated-fix.md"
    unrelated_followup.write_bytes(b"# Unrelated repair\n")
    assert _backup_active_followup_plan(
        root,
        root / unrelated.path,
        unrelated_followup,
        event="before_followup_turn",
        run_id="actual-run-002",
        turn_number=4,
    ) is not None

    def paged(status: str, name: str) -> list[dict[str, object]]:
        page = service.list_backups("project", status, name, limit=1)
        results = list(page["backups"])
        while page["next_offset"] is not None:
            page = service.list_backups(
                "project", status, name, offset=page["next_offset"], limit=1
            )
            results.extend(page["backups"])
        return results

    before = paged("in_progress", "original.md")
    followup_summary = next(
        summary for summary in before if summary["kind"] == "follow_up"
    )
    assert followup_summary["run_id"] == "actual-run-001"
    assert followup_summary["turn_number"] == 2
    assert all(summary["backup_filename"] != "unrelated-fix.md" for summary in before)

    followup.unlink()
    after_cleanup = paged("in_progress", "original.md")
    assert any(summary["kind"] == "follow_up" for summary in after_cleanup)

    done = service.promote(
        "project",
        "in_progress",
        "original.md",
        promoted.revision,
        target_name="finished.md",
    )
    after_done = paged("done", "finished.md")
    done_followup = next(
        summary for summary in after_done if summary["kind"] == "follow_up"
    )
    assert done_followup["run_id"] == "actual-run-001"
    assert done_followup["turn_number"] == 2
    assert followup_backup.read_bytes() == followup_bytes
    record = read_backup_provenance(root, followup_backup)
    assert record is not None
    assert record["source_plan_path"] == str(followup.resolve())
    assert record["original_plan_path"] == str(original.resolve())
    assert done.path == "plans/done/finished.md"


def test_external_ready_history_survives_identity_adoption_and_rollback(
    plan_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.workflow import _backup_active_followup_plan, _backup_original_plan
    import aflow_app_server.plan_service as plan_service_module

    service, root, _ = plan_fixture
    source = root / "plans" / "in-progress" / "cli-ready.md"
    source.parent.mkdir(parents=True)
    source_bytes = b"# External Ready\n"
    source.write_bytes(source_bytes)
    followup = root / "plans" / "in-progress" / "cli-repair.md"
    followup_bytes = b"# External repair\n"
    followup.write_bytes(followup_bytes)

    snapshot = _backup_original_plan(
        root,
        source,
        event="workflow_start",
        run_id="cli-ready-run",
        turn_number=1,
    )
    followup_backup = _backup_active_followup_plan(
        root,
        source,
        followup,
        event="before_followup_turn",
        run_id="cli-repair-run",
        turn_number=2,
    )
    assert followup_backup is not None

    snapshot_sidecar = (
        root
        / "plans"
        / "backups"
        / PROVENANCE_DIRECTORY_NAME
        / f"{snapshot.name}.json"
    )
    snapshot_payload = json.loads(snapshot_sidecar.read_text(encoding="utf-8"))
    snapshot_payload["capture_references"][0].pop("timestamp")
    snapshot_sidecar.write_text(json.dumps(snapshot_payload), encoding="utf-8")

    backup_names = sorted((snapshot.name, followup_backup.name))
    backup_hashes = {
        path.name: read_backup_provenance(root, path)["content_sha256"]
        for path in (snapshot, followup_backup)
    }
    assert source.read_bytes() == source_bytes
    assert service.read("project", "in_progress", "cli-ready.md").revision == hashlib.sha256(
        source_bytes
    ).hexdigest()

    before = _all_backup_summaries(service, "in_progress", "cli-ready.md")
    assert len(before) == 2
    assert {summary["baseline_status"] for summary in before} == {"unknown"}
    before_snapshot = next(
        summary for summary in before if summary["backup_filename"] == snapshot.name
    )
    assert before_snapshot["capture_event"] == "workflow_start"
    assert before_snapshot["timestamp"] is None
    assert before_snapshot["run_id"] == "cli-ready-run"
    assert before_snapshot["turn_number"] == 1

    followup.unlink()
    after_cleanup = _all_backup_summaries(service, "in_progress", "cli-ready.md")
    assert len(after_cleanup) == 2

    def fail_lifecycle(*args: object, **kwargs: object) -> None:
        raise OSError("lifecycle metadata unavailable")

    monkeypatch.setattr(plan_service_module, "record_plan_lifecycle_move", fail_lifecycle)
    document = service.read("project", "in_progress", "cli-ready.md")
    with pytest.raises(PlanServiceError, match="plan promotion failed"):
        service.promote(
            "project",
            "in_progress",
            "cli-ready.md",
            document.revision,
            target_name="cli-finished.md",
        )
    assert source.read_bytes() == source_bytes
    assert not (root / "plans" / "done" / "cli-finished.md").exists()
    after_rollback = _all_backup_summaries(service, "in_progress", "cli-ready.md")
    assert len(after_rollback) == 2
    assert {
        summary["run_id"]
        for summary in after_rollback
        if summary["kind"] == "follow_up"
    } == {"cli-repair-run"}

    monkeypatch.undo()
    promoted = service.promote(
        "project",
        "in_progress",
        "cli-ready.md",
        document.revision,
        target_name="cli-finished.md",
    )
    assert promoted.revision == hashlib.sha256(source_bytes).hexdigest()
    assert (root / promoted.path).read_bytes() == source_bytes
    after_done = _all_backup_summaries(service, "done", "cli-finished.md")
    assert len(after_done) == 2
    done_snapshot = next(
        summary for summary in after_done if summary["backup_filename"] == snapshot.name
    )
    done_followup = next(
        summary
        for summary in after_done
        if summary["backup_filename"] == followup_backup.name
    )
    assert done_snapshot["baseline_status"] == "unknown"
    assert done_snapshot["capture_event"] == "workflow_start"
    assert done_snapshot["timestamp"] is None
    assert done_snapshot["run_id"] == "cli-ready-run"
    assert done_followup["baseline_status"] == "unknown"
    assert done_followup["capture_event"] == "before_followup_turn"
    assert done_followup["run_id"] == "cli-repair-run"
    assert done_followup["turn_number"] == 2
    assert sorted(path.name for path in (root / "plans" / "backups").iterdir() if path.is_file()) == backup_names
    assert snapshot.read_bytes() == source_bytes
    assert followup_backup.read_bytes() == followup_bytes
    assert {
        path.name: read_backup_provenance(root, path)["content_sha256"]
        for path in (snapshot, followup_backup)
    } == backup_hashes


def test_controller_move_preserves_external_backup_details_after_cleanup_and_reuse(
    plan_fixture,
) -> None:
    from aflow.workflow import (
        WorkflowError,
        _backup_active_followup_plan,
        _backup_original_plan,
        move_completed_plan_to_done,
    )

    service, root, _ = plan_fixture
    source = root / "plans" / "in-progress" / "cli.md"
    source.parent.mkdir(parents=True)
    source_bytes = b"# External controller plan\n"
    source.write_bytes(source_bytes)
    followup = root / "plans" / "in-progress" / "cli-followup.md"
    followup_bytes = b"# External controller follow-up\n"
    followup.write_bytes(followup_bytes)

    snapshot = _backup_original_plan(
        root,
        source,
        event="resume_start",
        run_id="cli-run",
    )
    followup_backup = _backup_active_followup_plan(
        root,
        source,
        followup,
        event="before_followup_turn",
        run_id="cli-run",
        turn_number=2,
    )
    assert followup_backup is not None

    first_page = service.list_backups(
        "project",
        "in_progress",
        "cli.md",
        limit=1,
    )
    assert first_page["total_items"] == 2
    assert first_page["next_offset"] == 1
    before = _all_backup_summaries(service, "in_progress", "cli.md")
    before_by_name = {
        summary["backup_filename"]: summary for summary in before
    }
    assert set(before_by_name) == {snapshot.name, followup_backup.name}
    assert {summary["baseline_status"] for summary in before} == {"unknown"}
    snapshot_before = before_by_name[snapshot.name]
    followup_before = before_by_name[followup_backup.name]
    assert snapshot_before["capture_event"] == "resume_start"
    assert snapshot_before["run_id"] == "cli-run"
    assert snapshot_before["turn_number"] is None
    assert isinstance(snapshot_before["timestamp"], str)
    assert followup_before["capture_event"] == "before_followup_turn"
    assert followup_before["run_id"] == "cli-run"
    assert followup_before["turn_number"] == 2
    assert isinstance(followup_before["timestamp"], str)
    source_revision = hashlib.sha256(source_bytes).hexdigest()
    assert service.read("project", "in_progress", "cli.md").revision == source_revision
    backup_names = sorted(before_by_name)
    backup_hashes = {
        name: before_by_name[name]["content_sha256"] for name in backup_names
    }

    followup.unlink()
    assert len(_all_backup_summaries(service, "in_progress", "cli.md")) == 2
    destination = move_completed_plan_to_done(root, source)

    assert destination == root / "plans" / "done" / "cli.md"
    assert not source.exists()
    assert destination.read_bytes() == source_bytes
    assert service.read("project", "done", "cli.md").revision == source_revision
    after = _all_backup_summaries(service, "done", "cli.md")
    after_by_name = {
        summary["backup_filename"]: summary for summary in after
    }
    assert set(after_by_name) == set(before_by_name)
    assert {summary["baseline_status"] for summary in after} == {"unknown"}
    for name in backup_names:
        for field in (
            "capture_event",
            "timestamp",
            "run_id",
            "turn_number",
            "content_sha256",
        ):
            assert after_by_name[name][field] == before_by_name[name][field]
    assert sorted(path.name for path in (snapshot, followup_backup)) == backup_names
    assert snapshot.read_bytes() == source_bytes
    assert followup_backup.read_bytes() == followup_bytes
    assert {
        path.name: read_backup_provenance(root, path)["content_sha256"]
        for path in (snapshot, followup_backup)
    } == backup_hashes

    reuse_source = root / "plans" / "in-progress" / "cli-reuse.md"
    reuse_destination = root / "plans" / "done" / "cli-reuse.md"
    reuse_bytes = b"# Same-byte controller plan\n"
    reuse_source.write_bytes(reuse_bytes)
    reuse_destination.parent.mkdir(parents=True, exist_ok=True)
    reuse_destination.write_bytes(reuse_bytes)
    reuse_snapshot = _backup_original_plan(
        root,
        reuse_source,
        event="resume_start",
        run_id="reuse-run",
    )
    reuse_before = _all_backup_summaries(service, "in_progress", "cli-reuse.md")
    assert len(reuse_before) == 1
    reuse_destination_result = move_completed_plan_to_done(root, reuse_source)
    assert reuse_destination_result == reuse_destination
    assert not reuse_source.exists()
    assert reuse_destination.read_bytes() == reuse_bytes
    reuse_after = _all_backup_summaries(service, "done", "cli-reuse.md")
    assert len(reuse_after) == 1
    assert reuse_after[0]["backup_filename"] == reuse_snapshot.name
    assert reuse_after[0]["content_sha256"] == reuse_before[0]["content_sha256"]
    assert reuse_after[0]["capture_event"] == "resume_start"
    assert reuse_after[0]["timestamp"] == reuse_before[0]["timestamp"]
    assert reuse_after[0]["run_id"] == "reuse-run"
    assert reuse_after[0]["turn_number"] is None
    assert reuse_after[0]["baseline_status"] == "unknown"

    blocked_source = root / "plans" / "in-progress" / "cli-blocked.md"
    blocked_destination = root / "plans" / "done" / "cli-blocked.md"
    blocked_bytes = b"# Blocked controller plan\n"
    blocked_source.write_bytes(blocked_bytes)
    blocked_snapshot = _backup_original_plan(
        root,
        blocked_source,
        event="resume_start",
        run_id="blocked-run",
    )
    blocked_before = _all_backup_summaries(
        service,
        "in_progress",
        "cli-blocked.md",
    )
    blocked_destination.parent.mkdir(parents=True, exist_ok=True)
    blocked_destination.write_bytes(b"# Conflicting destination\n")
    with pytest.raises(WorkflowError, match="done plan path already exists"):
        move_completed_plan_to_done(root, blocked_source)
    assert blocked_source.read_bytes() == blocked_bytes
    assert blocked_destination.read_bytes() == b"# Conflicting destination\n"
    assert plan_backups.plan_identity_for_path(root, blocked_source) is None
    blocked_after = _all_backup_summaries(
        service,
        "in_progress",
        "cli-blocked.md",
    )
    assert blocked_after == blocked_before
    blocked_record = read_backup_provenance(root, blocked_snapshot)
    assert blocked_record is not None
    blocked_references = blocked_record["capture_references"]
    assert isinstance(blocked_references, list)
    assert all(
        reference.get("plan_identity_id") is None
        for reference in blocked_references
        if isinstance(reference, dict)
    )


def test_external_todo_history_binds_before_exact_ready_baseline(
    plan_fixture,
) -> None:
    from aflow.workflow import _backup_active_followup_plan, _backup_original_plan

    service, root, _ = plan_fixture
    source = root / "plans" / "todo" / "cli-todo.md"
    source.parent.mkdir(parents=True)
    source_bytes = b"# External todo\n"
    source.write_bytes(source_bytes)
    followup = root / "plans" / "in-progress" / "cli-todo-repair.md"
    followup_bytes = b"# Todo repair\n"
    followup.parent.mkdir(parents=True)
    followup.write_bytes(followup_bytes)

    snapshot = _backup_original_plan(
        root,
        source,
        event="workflow_start",
        run_id="cli-todo-run",
        turn_number=1,
    )
    followup_backup = _backup_active_followup_plan(
        root,
        source,
        followup,
        event="before_followup_turn",
        run_id="cli-todo-repair-run",
        turn_number=2,
    )
    assert followup_backup is not None
    before = _all_backup_summaries(service, "todo", "cli-todo.md")
    assert len(before) == 2
    assert {summary["baseline_status"] for summary in before} == {"unknown"}
    source_revision = hashlib.sha256(source_bytes).hexdigest()
    backup_names = sorted((snapshot.name, followup_backup.name))
    backup_hashes = {
        path.name: read_backup_provenance(root, path)["content_sha256"]
        for path in (snapshot, followup_backup)
    }

    promoted = service.promote(
        "project",
        "todo",
        "cli-todo.md",
        source_revision,
        target_name="cli-todo-ready.md",
    )
    assert promoted.revision == source_revision
    assert (root / promoted.path).read_bytes() == source_bytes
    after = _all_backup_summaries(service, "in_progress", "cli-todo-ready.md")
    assert len(after) == 2
    baseline = next(
        summary for summary in after if summary["backup_filename"] == snapshot.name
    )
    followup_summary = next(
        summary
        for summary in after
        if summary["backup_filename"] == followup_backup.name
    )
    assert baseline["baseline_status"] == "known"
    assert baseline["capture_event"] == "ready_promotion"
    assert baseline["run_id"] is None
    assert baseline["turn_number"] is None
    baseline_record = read_backup_provenance(root, snapshot)
    assert baseline_record is not None
    baseline_reference = baseline_record["baseline_reference"]
    assert isinstance(baseline_reference, dict)
    assert baseline["timestamp"] == baseline_reference["timestamp"]
    assert baseline_reference["source_path"] == str(source.resolve())
    assert baseline_reference["destination_path"] == str(
        (root / promoted.path).resolve()
    )
    assert followup_summary["baseline_status"] == "unknown"
    assert followup_summary["capture_event"] == "before_followup_turn"
    assert followup_summary["run_id"] == "cli-todo-repair-run"
    assert followup_summary["turn_number"] == 2
    assert sorted(
        path.name
        for path in (root / "plans" / "backups").iterdir()
        if path.is_file()
    ) == backup_names
    assert snapshot.read_bytes() == source_bytes
    assert followup_backup.read_bytes() == followup_bytes
    assert {
        path.name: read_backup_provenance(root, path)["content_sha256"]
        for path in (snapshot, followup_backup)
    } == backup_hashes


def test_shared_body_capture_details_stay_with_each_plan_owner(
    plan_fixture,
) -> None:
    from aflow.workflow import _backup_active_followup_plan, _backup_original_plan

    service, root, _ = plan_fixture
    shared_bytes = b"# Shared plan body\n"
    first = service.create("project", "shared.md", shared_bytes.decode())
    first_source = root / first.path
    first_snapshot = _backup_original_plan(
        root,
        first_source,
        event="workflow_start",
        run_id="old-plan-run",
        turn_number=1,
    )
    old = service.promote(
        "project",
        "todo",
        "shared.md",
        first.revision,
        target_name="old.md",
    )

    second = service.create("project", "shared.md", shared_bytes.decode())
    second_source = root / second.path
    second_snapshot = _backup_original_plan(
        root,
        second_source,
        event="resume_start",
        run_id="new-plan-run",
        turn_number=3,
    )
    new = service.promote(
        "project",
        "todo",
        "shared.md",
        second.revision,
        target_name="new.md",
    )
    assert first_snapshot == second_snapshot
    assert old.path == "plans/in-progress/old.md"
    assert new.path == "plans/in-progress/new.md"

    followup = root / "plans" / "in-progress" / "shared-repair.md"
    followup_bytes = b"# Shared follow-up body\n"
    followup.write_bytes(followup_bytes)
    old_followup = _backup_active_followup_plan(
        root,
        root / old.path,
        followup,
        event="before_followup_turn",
        run_id="old-followup-run",
        turn_number=2,
    )
    assert old_followup is not None
    followup.unlink()
    followup.write_bytes(followup_bytes)
    new_followup = _backup_active_followup_plan(
        root,
        root / new.path,
        followup,
        event="before_followup_turn",
        run_id="new-followup-run",
        turn_number=5,
    )
    duplicate = _backup_active_followup_plan(
        root,
        root / new.path,
        followup,
        event="before_followup_turn",
        run_id="new-followup-run",
        turn_number=5,
    )
    assert new_followup == duplicate == old_followup
    old_owner_id = plan_backups.plan_identity_for_path(root, root / old.path)
    new_owner_id = plan_backups.plan_identity_for_path(root, root / new.path)
    assert old_owner_id is not None
    assert new_owner_id is not None
    followup_sidecar = (
        root
        / "plans"
        / "backups"
        / PROVENANCE_DIRECTORY_NAME
        / f"{old_followup.name}.json"
    )
    followup_payload = json.loads(followup_sidecar.read_text(encoding="utf-8"))
    for reference in followup_payload["capture_references"]:
        if reference.get("original_plan_identity_id") == old_owner_id:
            reference.pop("timestamp")
    followup_sidecar.write_text(json.dumps(followup_payload), encoding="utf-8")
    assert _backup_active_followup_plan(
        root,
        root / new.path,
        followup,
        event="before_followup_turn",
        run_id="new-followup-run",
        turn_number=5,
    ) == new_followup

    old_history = _all_backup_summaries(service, "in_progress", "old.md")
    new_history = _all_backup_summaries(service, "in_progress", "new.md")
    old_baseline = next(
        summary for summary in old_history if summary["backup_filename"] == first_snapshot.name
    )
    new_baseline = next(
        summary for summary in new_history if summary["backup_filename"] == second_snapshot.name
    )
    old_followup_summary = next(
        summary for summary in old_history if summary["backup_filename"] == old_followup.name
    )
    new_followup_summary = next(
        summary for summary in new_history if summary["backup_filename"] == new_followup.name
    )
    assert old_baseline["baseline_status"] == new_baseline["baseline_status"] == "known"
    assert old_baseline["capture_event"] == new_baseline["capture_event"] == "ready_promotion"
    assert old_baseline["run_id"] is None
    assert old_baseline["turn_number"] is None
    assert new_baseline["run_id"] is None
    assert new_baseline["turn_number"] is None
    assert old_followup_summary["capture_event"] == "before_followup_turn"
    assert old_followup_summary["run_id"] == "old-followup-run"
    assert old_followup_summary["turn_number"] == 2
    assert old_followup_summary["timestamp"] is None
    assert new_followup_summary["capture_event"] == "before_followup_turn"
    assert new_followup_summary["run_id"] == "new-followup-run"
    assert new_followup_summary["turn_number"] == 5
    assert isinstance(new_followup_summary["timestamp"], str)

    shared_record = read_backup_provenance(root, first_snapshot)
    assert shared_record is not None
    references = shared_record["capture_references"]
    assert isinstance(references, list)
    assert len(references) == 4
    assert {reference["run_id"] for reference in references} >= {
        "old-plan-run",
        "new-plan-run",
    }
    followup_record = read_backup_provenance(root, old_followup)
    assert followup_record is not None
    followup_references = followup_record["capture_references"]
    assert isinstance(followup_references, list)
    assert len(followup_references) == 2
    assert len({
        (reference["run_id"], reference["turn_number"])
        for reference in followup_references
    }) == 2
    baseline_references = shared_record["baseline_references"]
    assert isinstance(baseline_references, list)
    baseline_by_owner = {
        reference["plan_identity_id"]: reference
        for reference in baseline_references
    }
    assert old_baseline["timestamp"] == baseline_by_owner[old_owner_id]["timestamp"]
    assert new_baseline["timestamp"] == baseline_by_owner[new_owner_id]["timestamp"]

    backup_bodies = (first_snapshot, old_followup)
    backup_bytes = {path.name: path.read_bytes() for path in backup_bodies}
    backup_hashes = {
        path.name: read_backup_provenance(root, path)["content_sha256"]
        for path in backup_bodies
    }
    assert sorted(path.name for path in backup_bodies) == [
        "shared-repair.md",
        "shared.md",
    ]
    assert {path.name: path.read_bytes() for path in backup_bodies} == backup_bytes
    assert {
        path.name: read_backup_provenance(root, path)["content_sha256"]
        for path in backup_bodies
    } == backup_hashes


def test_promotion_rolls_back_when_baseline_metadata_cannot_be_written(
    plan_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aflow_app_server.plan_service as plan_service_module

    service, root, _ = plan_fixture
    created = service.create("project", "rollback.md", "# Keep me\n")

    def fail_baseline(*args: object, **kwargs: object) -> None:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(plan_service_module, "mark_ready_baseline", fail_baseline)
    with pytest.raises(PlanServiceError, match="plan promotion failed"):
        service.promote("project", "todo", "rollback.md", created.revision)

    source = root / "plans" / "todo" / "rollback.md"
    target = root / "plans" / "in-progress" / "rollback.md"
    assert source.read_bytes() == b"# Keep me\n"
    assert not target.exists()
    backup = root / "plans" / "backups" / "rollback.md"
    record = read_backup_provenance(root, backup)
    assert record is not None
    assert record["baseline_status"] == "unknown"


def test_promotion_reverts_baseline_when_alias_metadata_cannot_be_written(
    plan_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aflow_app_server.plan_service as plan_service_module

    service, root, _ = plan_fixture
    created = service.create("project", "alias-rollback.md", "# Keep aliases\n")

    def fail_aliases(*args: object, **kwargs: object) -> None:
        raise OSError("alias metadata unavailable")

    monkeypatch.setattr(plan_service_module, "record_plan_lifecycle_move", fail_aliases)
    with pytest.raises(PlanServiceError, match="plan promotion failed"):
        service.promote("project", "todo", "alias-rollback.md", created.revision)

    source = root / "plans" / "todo" / "alias-rollback.md"
    target = root / "plans" / "in-progress" / "alias-rollback.md"
    assert source.read_bytes() == b"# Keep aliases\n"
    assert not target.exists()
    backup = root / "plans" / "backups" / "alias-rollback.md"
    record = read_backup_provenance(root, backup)
    assert record is not None
    assert record["baseline_status"] == "unknown"
    assert str(target.resolve()) not in record["path_aliases"]


def test_promotion_fails_closed_when_baseline_capture_fails(
    plan_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aflow.workflow as workflow_module

    service, root, _ = plan_fixture
    created = service.create("project", "capture-failure.md", "content")

    def fail_capture(*args: object, **kwargs: object) -> None:
        raise RuntimeError("backup unavailable")

    monkeypatch.setattr(workflow_module, "_backup_original_plan", fail_capture)
    with pytest.raises(PlanServiceError, match="baseline capture failed"):
        service.promote("project", "todo", "capture-failure.md", created.revision)

    source = root / "plans" / "todo" / "capture-failure.md"
    assert source.read_text(encoding="utf-8") == "content"
    assert not (root / "plans" / "in-progress" / "capture-failure.md").exists()


def test_backup_history_is_exactly_bound_and_paginates_without_writing_source(
    plan_fixture,
) -> None:
    service, root, _ = plan_fixture
    created = service.create("project", "history.md", "# History\n")
    source = root / "plans" / "todo" / "history.md"
    backup_dir = root / "plans" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for index in range(205):
        backup = backup_dir / f"history-capture-{index:03d}.md"
        backup.write_bytes(f"capture {index}\n".encode())
        plan_backups.record_backup_provenance(
            root,
            backup,
            kind="snapshot",
            source_plan_path=source,
            original_plan_path=None,
            event="workflow_start",
            historical_origin_known=False,
        )
    before = source.read_bytes()

    first = service.list_backups("project", "todo", "history.md")
    assert first["total_items"] == 205
    assert len(first["backups"]) == 50
    assert first["offset"] == 0
    assert first["limit"] == 50
    assert first["next_offset"] == 50

    last = service.list_backups(
        "project", "todo", "history.md", offset=200, limit=200
    )
    assert len(last["backups"]) == 5
    assert last["next_offset"] is None
    assert source.read_bytes() == before == created.content.encode()


def test_backup_history_does_not_match_same_basename_or_malformed_metadata(
    plan_client,
    plan_fixture,
) -> None:
    service, root, registry = plan_fixture
    headers = {"Authorization": f"Bearer {TOKEN}"}
    created = service.create("project", "shared.md", "# Exact\n")
    unrelated = root.parent / "unrelated" / "plans" / "todo" / "shared.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"# Exact\n")
    backup_dir = root / "plans" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    unrelated_backup = backup_dir / "unrelated.md"
    unrelated_backup.write_bytes(unrelated.read_bytes())
    plan_backups.record_backup_provenance(
        root,
        unrelated_backup,
        kind="snapshot",
        source_plan_path=unrelated,
        original_plan_path=None,
        event="workflow_start",
        historical_origin_known=False,
    )
    assert service.list_backups("project", "todo", "shared.md")["backups"] == []

    promoted = service.promote("project", "todo", "shared.md", created.revision)
    history_path = root / "plans" / "backups" / "shared.md"
    sidecar = (
        root
        / "plans"
        / "backups"
        / PROVENANCE_DIRECTORY_NAME
        / "shared.md.json"
    )
    sidecar.write_text("{malformed", encoding="utf-8")
    source = root / promoted.path
    before = source.read_bytes()
    response = plan_client.get(
        "/api/projects/project/plans/in_progress/shared.md/backups",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["backups"] == []
    assert source.read_bytes() == before
    assert history_path.read_bytes() == before
    assert registry.resolve("project")[1] == root


def test_backup_history_route_requires_auth_and_enforces_limit(plan_client) -> None:
    path = "/api/projects/project/plans/todo/unknown.md/backups"
    assert plan_client.get(path).status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = plan_client.get(path, headers=headers, params={"limit": 201})
    assert response.status_code == 422

def test_missing_template_fails_before_writing_any_file(plan_fixture, monkeypatch) -> None:
    import aflow_app_server.plan_service as plan_service_module

    service, root, _ = plan_fixture
    def _missing(*args, **kwargs):
        raise OSError("package resources are unavailable")
    monkeypatch.setattr(plan_service_module, "files", _missing)
    with pytest.raises(PlanServiceError, match="template is unavailable"):
        service.create("project", "broken.md", None)
    assert not (root / "plans/todo/broken.md").exists()

def test_create_routes_accept_omitted_and_null_content(plan_client: TestClient) -> None:
    from importlib.resources import files as resource_files
    from pathlib import Path as _Path

    from aflow.plan import parse_git_tracking_metadata, parse_plan_text

    headers = {"Authorization": f"Bearer {TOKEN}"}
    path = "/api/projects/project/plans"
    expected = resource_files("aflow").joinpath("templates/draft-plan.md").read_text(encoding="utf-8")

    omitted = plan_client.post(path, headers=headers, json={"name": "omitted.md"})
    assert omitted.status_code == 201
    assert omitted.json()["content"] == expected
    parsed = parse_plan_text(omitted.json()["content"], source_path=_Path("omitted.md"))
    assert len(parsed.sections) == 1
    assert parsed.sections[0].unchecked_step_count == 2
    assert parse_git_tracking_metadata(omitted.json()["content"]) is not None

    explicit_null = plan_client.post(path, headers=headers, json={"name": "null.md", "content": None})
    assert explicit_null.status_code == 201
    assert explicit_null.json()["content"] == expected

    explicit_empty = plan_client.post(path, headers=headers, json={"name": "empty.md", "content": ""})
    assert explicit_empty.status_code == 201
    assert explicit_empty.json()["content"] == ""

    explicit = plan_client.post(path, headers=headers, json={"name": "custom.md", "content": "# Custom\n"})
    assert explicit.status_code == 201
    assert explicit.json()["content"] == "# Custom\n"

def test_authenticated_plan_routes_and_removed_remote_routes(plan_client: TestClient) -> None:
    path = "/api/projects/project/plans"
    assert plan_client.get(path).status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    created = plan_client.post(path, headers=headers, json={"name": "route.md", "content": "# route"})
    assert created.status_code == 201
    body = created.json()
    assert body["path"] == "plans/todo/route.md"
    assert plan_client.get(f"{path}/todo/route.md", headers=headers).json()["content"] == "# route"
    updated = plan_client.put(
        f"{path}/todo/route.md", headers=headers,
        json={"content": "# updated", "expected_revision": body["revision"]},
    )
    assert updated.status_code == 200
    promoted = plan_client.post(
        f"{path}/todo/route.md/promote", headers=headers,
        json={"expected_revision": updated.json()["revision"]},
    )
    assert promoted.json()["path"] == "plans/in-progress/route.md"
    assert plan_client.get(path, headers=headers).json()[0]["status"] == "in_progress"

    for removed in (
        "/api/planning/" + "providers",
        "/api/projects/project/planning/" + "sessions",
        "/api/transcribe",
    ):
        assert plan_client.get(removed, headers=headers).status_code == 404

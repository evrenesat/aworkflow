from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from aflow.control_plane import project_plan_startup_context
from aflow.control_plane import startup_context as startup_module
from aflow.plan_backups import create_plan_identity


def _plan(repo: Path, body: str) -> Path:
    path = repo / "plans" / "in-progress" / "plan.md"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def _tree_state(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_mtime_ns,
            info.st_ctime_ns,
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
        )
    return result


def test_partial_plan_without_run_directory_is_exact_and_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    done = "".join(f"### [x] Checkpoint {i}: Done {i}\n- [x] done\n" for i in range(1, 4))
    body = (
        "# Plan\n" + done
        + "### [ ] Checkpoint 4: Current\n- [x] completed task\n- [ ] remaining one\n- [ ] remaining two\n"
        + "### [ ] Checkpoint 5: Later\n- [ ] later\n"
        + "## User Acceptance Pending\n- [ ] device check\n"
    )
    path = _plan(repo, body)
    identity = create_plan_identity(repo, path)
    before = _tree_state(repo)

    context = project_plan_startup_context(repo, path)

    assert _tree_state(repo) == before
    assert not (repo / ".aflow").exists()
    assert context.availability == "available"
    assert context.plan_path == "plans/in-progress/plan.md"
    assert context.plan_identity == identity
    assert context.plan_revision == hashlib.sha256(body.encode()).hexdigest()
    assert context.total_checkpoints == 5
    assert context.recorded_complete_checkpoints == 3
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.ordinal == 4
    assert context.next_checkpoint.checked_tasks == 1
    assert context.next_checkpoint.total_tasks == 3
    assert context.pending_tasks == ("remaining one", "remaining two")
    assert context.workflow_name is None
    assert context.selected_step is None
    assert context.recommendation is None
    assert "approved" not in json.dumps(context.to_dict()).lower()


def test_fences_and_other_sections_use_parser_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(
        repo,
        "# Plan\n```\n### [x] Checkpoint 9: Fake\n- [ ] fake\n```\n"
        "### [ ] Checkpoint 1: Real\n- [ ] real one\n"
        "~~~\n- [ ] fake task\n~~~\n- [x] done\n"
        "## User Acceptance Pending\n- [ ] owner task\n"
        "### [ ] Not a checkpoint\n- [ ] another fake\n",
    )
    context = project_plan_startup_context(repo, path)
    assert context.total_checkpoints == 1
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.total_tasks == 2
    assert context.pending_tasks == ("real one",)


def test_all_complete_has_no_next_checkpoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [x] Checkpoint 1: Done\n- [x] task\n")
    context = project_plan_startup_context(repo, path)
    assert context.availability == "available"
    assert context.recorded_complete_checkpoints == 1
    assert context.next_checkpoint is None
    assert context.pending_tasks == ()


def test_long_plan_keeps_exact_counts_and_late_next_checkpoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sections = []
    for index in range(1, 111):
        if index < 105:
            sections.append(f"### [x] Checkpoint {index}: Done\n- [x] task\n")
        elif index == 105:
            tasks = "".join(f"- [ ] task {i} {'🦋' * 100}\n" for i in range(7))
            sections.append(f"### [ ] Checkpoint {index}: {'🦋' * 100}\n" + tasks)
        else:
            sections.append(f"### [ ] Checkpoint {index}: Later\n- [ ] task\n")
    path = _plan(repo, "".join(sections))
    context = project_plan_startup_context(repo, path)
    assert context.total_checkpoints == 110
    assert context.recorded_complete_checkpoints == 104
    assert len(context.checkpoints) == 100
    assert context.checkpoint_outline_truncated
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.ordinal == 105
    assert len(context.next_checkpoint.title.encode()) <= 256
    assert len(context.pending_tasks) == 5
    assert all(len(task.encode()) <= 256 for task in context.pending_tasks)
    assert context.pending_tasks_truncated
    assert context.text_truncated
    assert len(json.dumps(context.to_dict(), ensure_ascii=False).encode()) <= 48 * 1024


@pytest.mark.parametrize(
    ("body", "reason", "availability"),
    [
        ("# Notes\n- [ ] something\n", "non_checkpoint_plan", "not_applicable"),
        ("### [x] Checkpoint 1: Broken\n- [ ] task\n", "inconsistent_checkpoint_state", "unavailable"),
    ],
)
def test_non_checkpoint_and_inconsistent_plan_are_not_invented(
    tmp_path: Path, body: str, reason: str, availability: str
) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, body)
    context = project_plan_startup_context(repo, path)
    assert context.availability == availability
    assert reason in context.reason_codes
    assert context.next_checkpoint is None
    assert context.total_checkpoints is None


def test_blank_task_text_retains_counts_but_omits_unreliable_text(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [ ] Checkpoint 1: Work\n- [ ] \n- [ ] named\n")
    context = project_plan_startup_context(repo, path)
    assert context.availability == "partial"
    assert context.next_checkpoint is not None
    assert context.next_checkpoint.total_tasks == 2
    assert context.pending_tasks == ()
    assert "pending_task_text_unavailable" in context.reason_codes


def test_missing_unsafe_invalid_and_oversized_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [ ] Checkpoint 1: Work\n- [ ] task\n")
    outside = tmp_path / "outside.md"
    outside.write_text(path.read_text())
    (path.parent / "link.md").symlink_to(outside)
    (repo / "linked-plans").symlink_to(path.parent, target_is_directory=True)
    assert project_plan_startup_context(repo, "plans/in-progress/missing.md").reason_codes == ("missing_plan",)
    for unsafe in (outside, path.parent / "link.md", "linked-plans/plan.md", "../outside.md"):
        context = project_plan_startup_context(repo, unsafe)
        assert context.availability == "unavailable"
        assert context.next_checkpoint is None
        assert context.reason_codes[0] in {"unsafe_plan_path", "unsafe_or_unreadable_plan"}
    path.write_bytes(b"\xff")
    assert project_plan_startup_context(repo, path).reason_codes == ("invalid_plan_encoding",)
    path.write_bytes(b"a" * (startup_module.MAX_PLAN_BYTES + 1))
    assert project_plan_startup_context(repo, path).reason_codes == ("plan_too_large",)


def test_concurrent_replacement_is_not_returned_as_stable_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    path = _plan(repo, "### [ ] Checkpoint 1: Original\n- [ ] task\n")
    original_read = startup_module._read_plan

    def replacing_read(root: Path, parts: tuple[str, ...], *, body: bool):
        if not body:
            replacement = path.with_suffix(".new")
            replacement.write_text("### [x] Checkpoint 1: Replaced\n- [x] task\n")
            os.replace(replacement, path)
        return original_read(root, parts, body=body)

    monkeypatch.setattr(startup_module, "_read_plan", replacing_read)
    context = project_plan_startup_context(repo, path)
    assert context.availability == "unavailable"
    assert context.reason_codes == ("plan_changed_during_read",)
    assert context.next_checkpoint is None
    assert context.plan_revision is None

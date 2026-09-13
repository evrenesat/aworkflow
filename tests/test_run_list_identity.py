from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from aflow.control_plane import LaunchManifest, RunRepository, create_launch_manifest
from aflow.control_plane import run_progress
from aflow.control_plane.run_progress import (
    project_run_original_plan_identity,
    project_run_progress_summary,
)


def _plan_text() -> str:
    return "# Original plan\n\n### [ ] Checkpoint 1: First\n- [ ] pending\n"


def _make_run(
    tmp_path: Path,
    *,
    run_id: str = "owned-run",
    metadata: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    original = root / "plans" / "in-progress" / "original.md"
    original.parent.mkdir(parents=True)
    original.write_text(_plan_text(), encoding="utf-8")
    overlay = root / "execution" / "overlay.md"
    overlay.parent.mkdir()
    overlay.write_text("# Active overlay\n", encoding="utf-8")
    create_launch_manifest(
        root,
        LaunchManifest(
            run_id=run_id,
            project_root=str(root.resolve()),
            plan_path=str(original.resolve()),
            workflow_name="managed",
            max_turns=5,
            idempotency_key=f"{run_id}-key",
            caller_scope="test",
        ),
    )
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "running",
        "workflow_name": "managed",
        "repo_root": str(root.resolve()),
        "original_plan_path": str(original.resolve()),
        "active_plan_path": str(overlay.resolve()),
        "history_complete": True,
    }
    if metadata:
        payload.update(metadata)
    (run_dir / "run.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return root, run_dir, original, overlay


def _metadata(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def test_full_and_raw_status_share_identity_without_changing_active_plan_path(
    tmp_path: Path,
) -> None:
    root, run_dir, _original, overlay = _make_run(tmp_path)
    repository = RunRepository(root)

    rich = repository.get_run_status("owned-run")
    raw = repository.get_run_status("owned-run", include_progress=False)
    lightweight = repository.with_original_plan_identity(raw)

    assert rich.progress is not None
    assert (
        rich.original_plan_display_name,
        rich.original_plan_path,
    ) == (
        rich.progress.original_plan_display_name,
        rich.progress.original_plan_path,
    )
    assert (lightweight.original_plan_display_name, lightweight.original_plan_path) == (
        rich.original_plan_display_name,
        rich.original_plan_path,
    )
    assert raw.progress is None
    assert raw.plan_path == lightweight.plan_path == str(overlay.resolve())
    assert replace(
        rich,
        original_plan_display_name=None,
        original_plan_path=None,
        progress=None,
    ) == raw
    assert project_run_original_plan_identity(
        run_dir, metadata=_metadata(run_dir)
    ) == ("original.md", "plans/in-progress/original.md")


def test_raw_status_and_identity_skip_rich_collectors_and_preserve_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, run_dir, _original, _overlay = _make_run(tmp_path)
    repository = RunRepository(root)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }

    def forbidden(name: str):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"raw identity called rich collector {name}")

        return fail

    for name in (
        "project_run_progress_detail",
        "project_run_progress_summary",
        "_canonical_cache_key",
        "_canonical_history_children",
        "_canonical_manager_records",
        "_canonical_events",
    ):
        monkeypatch.setattr(run_progress, name, forbidden(name))

    raw = repository.get_run_status("owned-run", include_progress=False)
    lightweight = repository.with_original_plan_identity(raw)

    assert raw.progress is None
    assert lightweight.original_plan_display_name == "original.md"
    assert lightweight.original_plan_path == "plans/in-progress/original.md"
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize("variant", ("captured", "missing", "invalid_scope", "symlink"))
def test_lightweight_identity_matches_rich_identity_for_guarded_evidence(
    tmp_path: Path, variant: str
) -> None:
    root, run_dir, original, _overlay = _make_run(tmp_path)
    metadata = _metadata(run_dir)

    if variant == "captured":
        original.unlink()
        metadata["captured_plan_state"] = {
            "checkpoints": [{"index": 1, "name": "Checkpoint 1: First"}],
            "is_complete": False,
        }
    elif variant == "missing":
        original.unlink()
    elif variant == "invalid_scope":
        metadata["active_implementation_scope"] = {
            "scope_id": "original::checkpoint-1",
            "original_plan_path": str(original),
            "checkpoint_index": 1,
            "checkpoint_name": "Checkpoint 1: First",
            "opened_turn_number": 1,
            "envelope_artifact_path": "scopes/not-the-scope/envelope.json",
            "envelope_artifact_sha256": "a" * 64,
            "envelope_canonical_sha256": "b" * 64,
        }
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "original.md").write_text(_plan_text(), encoding="utf-8")
        (root / "linked").symlink_to(outside, target_is_directory=True)
        metadata["original_plan_path"] = str(root / "linked" / "original.md")

    summary = project_run_progress_summary(
        run_dir, metadata=metadata, use_cache=False
    )
    identity = project_run_original_plan_identity(run_dir, metadata=metadata)

    assert identity == (
        summary.original_plan_display_name,
        summary.original_plan_path,
    )

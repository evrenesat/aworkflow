"""Focused tests for the installed guard report route."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from aflow.control_plane.models import RunStatus
from aflow.control_plane.run_activity import project_activity


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "aflow" / "bundled_skills" / "aflow-guard-development-run"
SNAPSHOT_SCRIPT = SKILL_ROOT / "scripts" / "aflow_guard_snapshot.py"
INPUT_SCRIPT = SKILL_ROOT / "scripts" / "aflow_guard_report_input.py"
RENDERER_SCRIPT = SKILL_ROOT / "scripts" / "aflow_guard_report.py"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _report_fixture(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "guarded-repository"
    run_id = "20260911T120000Z-cp3-route"
    run_dir = repository / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "current_step": "implement",
                "last_snapshot": {
                    "current_checkpoint_index": 3,
                    "current_checkpoint_name": "Checkpoint 3: Route",
                    "total_checkpoint_count": 3,
                    "is_complete": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return repository, run_id


def _repo_files(repository: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository)): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }


def _snapshot(
    repository: Path, run_id: str, state_path: Path, snapshot_path: Path
) -> None:
    result = _run(
        [
            sys.executable,
            str(SNAPSHOT_SCRIPT),
            "--repo",
            str(repository),
            "--run-id",
            run_id,
            "--state-file",
            str(state_path),
            "--no-write",
        ]
    )
    assert result.returncode == 0, result.stderr
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(result.stdout, encoding="utf-8")


def test_vr_route_is_pinned_report_only_and_builder_precedes_renderer() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    start = skill.index("## Produce an explicit `:vr` report")
    end = skill.index("## Report an AFlow engine defect", start)
    route = skill[start:end]

    assert ":vr" in route
    assert "report-only" in route
    assert "<guarded-repo>" in route
    assert "<run-id>" in route
    assert "--no-write" in route
    assert "--mark-notified` is forbidden" in route
    assert route.count("--mark-notified") == 1
    assert route.index("aflow_guard_report_input.py") < route.index(
        "aflow_guard_report.py"
    )
    assert "--output <absolute-external-directory>/guard-report-input.json" in route
    assert "--output-dir <absolute-external-directory>" in route
    assert "![AFlow guard report](/absolute/external/path/guard-report.png)" in route
    assert "[Normalized report JSON](/absolute/external/path/guard-report.json)" in route
    assert "visualization" in route
    assert "image-generation" in route
    assert "aflow_guard_recovery.py" not in route
    assert "aflow_guard_issue.py" not in route
    assert "before\nrequesting owner action" in route
    assert "confirmed facts" in route
    assert "use `unknown`" in route
    assert "exact decision" in route
    assert "Healthy scheduled ticks remain silent" in route
    assert "unchanged fingerprint receives no second report" in route


def test_report_reference_keeps_the_current_external_png_json_contract() -> None:
    reference = (SKILL_ROOT / "references" / "report-input.md").read_text(
        encoding="utf-8"
    )

    assert "`:vr`" in reference
    assert "uv run --script <skill-dir>/scripts/aflow_guard_report.py" in reference
    assert "guard-report.png" in reference
    assert "guard-report.json" in reference
    assert "outside the guarded repository" in reference
    assert "generic visualization" in reference
    assert "authorize recovery" in reference
    assert "--canonical-observation" in reference
    assert "--ownership-mode <ui-server|aflowd>" in reference


def test_pinned_report_journey_builds_png_and_json_without_guard_side_effects(
    tmp_path: Path,
) -> None:
    repository, run_id = _report_fixture(tmp_path)
    state_path = tmp_path / "external" / "observer-state.json"
    snapshot_path = tmp_path / "external" / "guard-snapshot.json"
    input_path = tmp_path / "external" / "guard-report-input.json"
    output_dir = tmp_path / "external" / "rendered"
    repeat_dir = tmp_path / "external" / "rendered-repeat"
    before = _repo_files(repository)

    _snapshot(repository, run_id, state_path, snapshot_path)
    input_result = _run(
        [
            sys.executable,
            str(INPUT_SCRIPT),
            "--snapshot",
            str(snapshot_path),
            "--repo",
            str(repository),
            "--run-id",
            run_id,
            "--output",
            str(input_path),
        ]
    )
    assert input_result.returncode == 0, input_result.stderr
    assert input_path.is_file()

    render_result = _run(
        [
            "uv",
            "run",
            "--script",
            str(RENDERER_SCRIPT),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
        ]
    )
    assert render_result.returncode == 0, render_result.stderr
    png_path = output_dir / "guard-report.png"
    json_path = output_dir / "guard-report.json"
    assert png_path.is_file()
    assert json_path.is_file()
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "guard-report.json",
        "guard-report.png",
    ]
    normalized = json.loads(json_path.read_text(encoding="utf-8"))
    assert normalized["schema_version"] == 1
    assert normalized["repository"] == str(repository.resolve())
    assert normalized["run_id"] == run_id

    repeat_result = _run(
        [
            "uv",
            "run",
            "--script",
            str(RENDERER_SCRIPT),
            "--input",
            str(input_path),
            "--output-dir",
            str(repeat_dir),
        ]
    )
    assert repeat_result.returncode == 0, repeat_result.stderr
    assert png_path.read_bytes() == (repeat_dir / "guard-report.png").read_bytes()
    assert json_path.read_bytes() == (repeat_dir / "guard-report.json").read_bytes()

    assert not state_path.exists(), "report routing must not persist notification state"
    assert _repo_files(repository) == before, "report routing must not mutate the run"


def test_report_input_rejects_wrong_pinned_identity_without_writes(
    tmp_path: Path,
) -> None:
    repository, run_id = _report_fixture(tmp_path)
    state_path = tmp_path / "external" / "observer-state.json"
    snapshot_path = tmp_path / "external" / "guard-snapshot.json"
    before = _repo_files(repository)
    _snapshot(repository, run_id, state_path, snapshot_path)

    wrong_run_output = tmp_path / "external" / "wrong-run.json"
    wrong_run = _run(
        [
            sys.executable,
            str(INPUT_SCRIPT),
            "--snapshot",
            str(snapshot_path),
            "--repo",
            str(repository),
            "--run-id",
            "different-pinned-run",
            "--output",
            str(wrong_run_output),
        ]
    )
    assert wrong_run.returncode == 2
    assert "pinned run" in wrong_run.stderr
    assert not wrong_run_output.exists()

    wrong_repo = tmp_path / "different-repository"
    wrong_repo_output = tmp_path / "external" / "wrong-repository.json"
    wrong_repository = _run(
        [
            sys.executable,
            str(INPUT_SCRIPT),
            "--snapshot",
            str(snapshot_path),
            "--repo",
            str(wrong_repo),
            "--run-id",
            run_id,
            "--output",
            str(wrong_repo_output),
        ]
    )
    assert wrong_repository.returncode == 2
    assert "pinned repository" in wrong_repository.stderr
    assert not wrong_repo_output.exists()

    assert not state_path.exists()
    assert _repo_files(repository) == before


def test_canonical_get_run_route_uses_pinned_metadata_and_renders_without_writes(
    tmp_path: Path,
) -> None:
    repository, run_id = _report_fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    before = _repo_files(repository)

    for name, observation in (
        (
            "healthy",
            RunStatus(
                run_id=run_id,
                status="running",
                activity="active",
                status_reason_code="turn_active",
                current_step="review",
                worker_exit={"selector": "codex.worker", "model": "gpt-5"},
            ).to_dict(),
        ),
        (
            "needs-attention",
            RunStatus(
                run_id=run_id,
                status="needs_attention",
                activity="inactive",
                status_reason_code="worker_failed",
                evidence={"reason": "worker ended without a terminal receipt"},
            ).to_dict(),
        ),
        (
            "startup-answer",
            project_activity(
                RunStatus(
                    run_id=run_id,
                    status="starting",
                    evidence={
                        "unit_active": False,
                        "startup_state": "awaiting_startup_answer",
                        "startup_question_valid": True,
                    },
                )
            ).to_dict(),
        ),
        (
            "missing-unit",
            project_activity(
                RunStatus(
                    run_id=run_id,
                    status="running",
                    evidence={
                        "recorded_status": "running",
                        "has_run_metadata": True,
                        "unit_active": False,
                        "unit_observation": "missing",
                    },
                )
            ).to_dict(),
        ),
    ):
        observation_path = external / f"{name}-get-run.json"
        input_path = external / f"{name}-input.json"
        output_dir = external / f"{name}-rendered"
        repeat_dir = external / f"{name}-rendered-repeat"
        observation_path.write_text(json.dumps(observation), encoding="utf-8")
        input_result = _run(
            [
                sys.executable,
                str(INPUT_SCRIPT),
                "--canonical-observation",
                str(observation_path),
                "--repo",
                str(repository),
                "--run-id",
                run_id,
                "--ownership-mode",
                "ui-server",
                "--observed-at",
                "2026-09-11T12:01:00+00:00",
                "--output",
                str(input_path),
            ]
        )
        assert input_result.returncode == 0, input_result.stderr
        normalized = json.loads(input_path.read_text(encoding="utf-8"))
        assert normalized["repository"] == str(repository.resolve())
        assert normalized["run_id"] == run_id
        assert normalized["ownership"] == "control_plane"
        assert normalized["checkpoint_name"] is None
        assert normalized["finalized_turn_summary"] is None
        if name == "healthy":
            assert normalized["worker_selector"] == "codex.worker"
            assert normalized["diagnosis"]["owner_action"] == (
                "No owner action is indicated by this observation."
            )
        elif name == "needs-attention":
            assert "failed turn or ownership evidence" in normalized["diagnosis"]["owner_action"]
        elif name == "startup-answer":
            assert "answer the existing pinned startup question" in normalized["diagnosis"]["owner_action"]
        elif name == "missing-unit":
            assert normalized["diagnosis"]["duplicate_operation_risk"] is True
            assert "Inspect the exact recorded provider operation" in normalized["diagnosis"]["owner_action"]

        for rendered in (output_dir, repeat_dir):
            render_result = _run(
                [
                    "uv",
                    "run",
                    "--script",
                    str(RENDERER_SCRIPT),
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(rendered),
                ]
            )
            assert render_result.returncode == 0, render_result.stderr
        assert (output_dir / "guard-report.png").read_bytes() == (
            repeat_dir / "guard-report.png"
        ).read_bytes()
        assert (output_dir / "guard-report.json").read_bytes() == (
            repeat_dir / "guard-report.json"
        ).read_bytes()

    for name, invalid in (
        ("wrong-run", RunStatus(run_id="different-run", status="running").to_dict()),
        ("wrong-ownership", RunStatus(run_id=run_id, status="running", ownership="legacy").to_dict()),
    ):
        observation_path = external / f"{name}.json"
        output_path = external / f"{name}-input.json"
        observation_path.write_text(json.dumps(invalid), encoding="utf-8")
        rejected = _run(
            [
                sys.executable,
                str(INPUT_SCRIPT),
                "--canonical-observation",
                str(observation_path),
                "--repo",
                str(repository),
                "--run-id",
                run_id,
                "--ownership-mode",
                "aflowd",
                "--observed-at",
                "2026-09-11T12:01:00+00:00",
                "--output",
                str(output_path),
            ]
        )
        assert rejected.returncode == 2
        assert not output_path.exists()

    assert _repo_files(repository) == before

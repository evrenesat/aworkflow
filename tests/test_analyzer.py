from __future__ import annotations

from pathlib import Path

from aflow.analyzer import analyze_single_run


def test_analyzer_reads_legacy_run_metadata_without_mutation(tmp_path: Path) -> None:
    runs_root = tmp_path / ".aflow" / "runs"
    run_dir = runs_root / "legacy-run"
    run_dir.mkdir(parents=True)
    run_json = run_dir / "run.json"
    run_json.write_text(
        '{"status":"interrupted","workflow_name":"legacy","turns_completed":0}\n'
    )
    before = run_json.read_bytes()

    payload = analyze_single_run(
        run_dir=run_dir,
        runs_root=runs_root,
        selection="explicit_run_id",
        include_noise=True,
    )

    assert payload["run"]["run_id"] == "legacy-run"
    assert payload["run"]["workflow_name"] == "legacy"
    assert run_json.read_bytes() == before


def test_manager_summary_labels_referenced_artifact_bytes(tmp_path) -> None:
    import json
    from pathlib import Path

    from aflow.analyzer import _labeled_manager_prompt_metrics

    run_dir = Path(tmp_path)
    decision = run_dir / "manager" / "decision-001"
    decision.mkdir(parents=True)
    (decision / "result.json").write_text(json.dumps({
        "prompt_metrics": {
            "system_prompt_bytes": 3096,
            "user_prompt_bytes": 12340,
            "argv_bytes": 141,
            "referenced_artifact_count": 3,
            "referenced_artifact_bytes": 81724,
        },
    }), encoding="utf-8")
    labeled = _labeled_manager_prompt_metrics(run_dir, "manager/decision-001")
    assert labeled is not None
    assert labeled["user_prompt_bytes"] == 12340
    assert labeled["referenced_artifact_bytes"] == 81724
    assert labeled["referenced_artifact_bytes_are_not_model_input"] is True
    # Analysis output never embeds prompt or evidence bodies.
    assert "prompt_text" not in json.dumps(labeled)

    assert _labeled_manager_prompt_metrics(run_dir, None) is None
    assert _labeled_manager_prompt_metrics(run_dir, "manager/missing") is None
    # Decisions without persisted metrics keep their historical shape.
    (decision / "result.json").write_text("{}", encoding="utf-8")
    assert _labeled_manager_prompt_metrics(run_dir, "manager/decision-001") is None

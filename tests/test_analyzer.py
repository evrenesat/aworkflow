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

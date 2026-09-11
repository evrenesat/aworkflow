from __future__ import annotations

import json
from pathlib import Path

from aflow.analyzer import analyze_single_run
from aflow.stop_marker import STRUCTURED_TRANSPORT_OUTPUT_SOURCE


def _write_turn(
    run_dir: Path,
    *,
    stdout: str,
    stderr: str,
    status: str = "completed",
    output_contract: str = "agent",
    semantic_output_source: str | None = None,
    chosen_transition: str = "next",
) -> tuple[Path, bytes]:
    turn_dir = run_dir / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    (turn_dir / "result.json").write_text(
        json.dumps(
            {
                "turn_number": 1,
                "step_name": "review",
                "step_role": "reviewer",
                "status": status,
                "returncode": 0,
                "output_contract": output_contract,
                **(
                    {"semantic_output_source": semantic_output_source}
                    if semantic_output_source is not None
                    else {}
                ),
                "chosen_transition": chosen_transition,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (turn_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    stderr_path = turn_dir / "stderr.txt"
    stderr_path.write_text(stderr, encoding="utf-8")
    return turn_dir, stderr_path.read_bytes()


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


def test_analyzer_successful_review_ignores_agent_stderr_history(tmp_path: Path) -> None:
    run_dir = tmp_path / ".aflow" / "runs" / "review-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "workflow_name": "managed",
                "current_step_name": "review",
                "turns_completed": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stderr = (
        "tool transcript: opened an old review artifact\n"
        "AFLOW_STOP: HISTORY: recorded stop from an earlier run\n"
    )
    turn_dir, stderr_before = _write_turn(
        run_dir,
        stdout="approved final response\n",
        stderr=stderr,
    )

    payload = analyze_single_run(
        run_dir=run_dir,
        runs_root=run_dir.parent,
        selection="explicit_run_id",
        include_noise=True,
    )

    summary = payload["run"]
    assert summary["aflow_stop_messages"] == []
    assert summary["outcome"] == {"kind": "completed", "status": "completed"}
    assert summary["focus_turns"][0]["highlights"] == []
    assert (turn_dir / "stderr.txt").read_bytes() == stderr_before


def test_analyzer_detects_current_structured_assistant_stop_only(tmp_path: Path) -> None:
    run_dir = tmp_path / ".aflow" / "runs" / "current-stop-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"status":"failed","workflow_name":"managed","turns_completed":1}\n',
        encoding="utf-8",
    )
    stdout = "\n".join(
        (
            '{"type":"thread.started","thread_id":"session-123"}',
            '{"type":"item.completed","thread_id":"session-123",'
            '"item":{"type":"command_execution",'
            '"aggregated_output":"AFLOW_STOP: HISTORY: tool output"}}',
            '{"type":"message.completed","thread_id":"session-123",'
            '"role":"user","text":"AFLOW_STOP: HISTORY: echoed prompt"}',
            '{"type":"message.completed","thread_id":"session-123",'
            '"role":"assistant","text":"AFLOW_STOP: current assistant stop"}',
        )
    )
    _write_turn(
        run_dir,
        stdout=stdout,
        stderr="diagnostic only\n",
        semantic_output_source=STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
    )

    payload = analyze_single_run(
        run_dir=run_dir,
        runs_root=run_dir.parent,
        selection="explicit_run_id",
        include_noise=True,
    )

    assert payload["run"]["aflow_stop_messages"] == ["current assistant stop"]


def test_analyzer_keeps_final_text_controls_around_json_examples(tmp_path: Path) -> None:
    run_dir = tmp_path / ".aflow" / "runs" / "final-text-controls"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"status":"failed","workflow_name":"managed","turns_completed":1}\n',
        encoding="utf-8",
    )
    current_stop = (
        "Diagnostic details:\n"
        "```json\n"
        '{"ok":false}\n'
        "```\n"
        "AFLOW_STOP: needs owner input\n"
    )
    _write_turn(run_dir, stdout=current_stop, stderr="diagnostic only\n")

    payload = analyze_single_run(
        run_dir=run_dir,
        runs_root=run_dir.parent,
        selection="explicit_run_id",
        include_noise=True,
    )

    assert payload["run"]["aflow_stop_messages"] == ["needs owner input"]


def test_analyzer_ignores_fenced_historical_json_control_example(tmp_path: Path) -> None:
    run_dir = tmp_path / ".aflow" / "runs" / "fenced-example"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"status":"completed","workflow_name":"managed","turns_completed":1}\n',
        encoding="utf-8",
    )
    approved = (
        "Approved implementation.\n"
        "```json\n"
        '{"result":"AFLOW_STOP: HISTORY: old example"}\n'
        "```\n"
    )
    _write_turn(run_dir, stdout=approved, stderr="diagnostic only\n")

    payload = analyze_single_run(
        run_dir=run_dir,
        runs_root=run_dir.parent,
        selection="explicit_run_id",
        include_noise=True,
    )

    assert payload["run"]["aflow_stop_messages"] == []
    assert payload["run"]["outcome"] == {
        "kind": "completed",
        "status": "completed",
    }


def test_analyzer_preserves_recorded_failure_when_fresh_marker_is_transcript_only(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / ".aflow" / "runs" / "historical-failure"
    run_dir.mkdir(parents=True)
    recorded_reason = "AFLOW_STOP: HISTORY: controller recorded the original failure"
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "failure_reason": recorded_reason,
                "workflow_name": "managed",
                "turns_completed": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_turn(
        run_dir,
        stdout="approved final response\n",
        stderr="tool transcript\nAFLOW_STOP: HISTORY: old marker\n",
    )

    payload = analyze_single_run(
        run_dir=run_dir,
        runs_root=run_dir.parent,
        selection="explicit_run_id",
        include_noise=True,
    )

    summary = payload["run"]
    assert summary["aflow_stop_messages"] == []
    assert summary["outcome"]["status"] == "failed"
    assert summary["outcome"]["kind"] == "workflow_failure"
    assert summary["failure"]["failure_reason"] == recorded_reason


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

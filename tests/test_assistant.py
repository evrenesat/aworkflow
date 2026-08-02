from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aflow.analyzer import analyze_turn, classify_turn_text_signals, extract_text_signals


INCIDENT_SIGNAL_TEXT = "\n".join(
    (
        "Plan Branch: main; the current checkout is aflow-feature and requires me to stop and escalate",
        "Need your direction on one point",
        "the original plan file is missing after the turn",
        "merge verification cannot be completed safely",
    )
)

INCIDENT_SIGNAL_NAMES = [
    "branch_mismatch_review_block",
    "dirty_merge_verification",
    "needs_human_direction",
    "original_plan_missing",
]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_turn(
    run_dir: Path,
    turn_number: int,
    *,
    result: dict[str, object],
    stdout: str = "",
    stderr: str = "",
) -> None:
    turn_dir = run_dir / "turns" / f"turn-{turn_number:03d}"
    turn_dir.mkdir(parents=True, exist_ok=True)
    _write_json(turn_dir / "result.json", result)
    (turn_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (turn_dir / "stderr.txt").write_text(stderr, encoding="utf-8")


def _snapshot(
    *,
    checkpoint_index: int = 1,
    checkpoint_name: str = "Checkpoint 1: First",
    unchecked_steps: int = 2,
    unchecked_checkpoints: int = 1,
    is_complete: bool = False,
) -> dict[str, object]:
    return {
        "current_checkpoint_index": checkpoint_index,
        "current_checkpoint_name": checkpoint_name,
        "current_checkpoint_unchecked_step_count": unchecked_steps,
        "is_complete": is_complete,
        "total_checkpoint_count": 1,
        "unchecked_checkpoint_count": unchecked_checkpoints,
    }


class AflowAssistantAnalyzerTests(unittest.TestCase):
    def _script_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "aflow" / "bundled_skills" / "aflow-assistant" / "scripts" / "analyze_runs.py"

    def _run_analyzer(self, *args: str) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(self._script_path()), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def _run_aflow_analyze(self, *args: str) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, "-m", "aflow", "analyze", *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def _analyze_turn(
        self,
        root: Path,
        *,
        status: str | None = "completed",
        returncode: int | None = 0,
        stdout: str = "",
        stderr: str = "",
        **extra: object,
    ) -> dict[str, object]:
        turn_dir = root / "turn"
        turn_dir.mkdir(parents=True, exist_ok=True)
        (turn_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (turn_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
        turn = {
            "_turn_dir": turn_dir,
            "status": status,
            "returncode": returncode,
            **extra,
        }
        return analyze_turn(turn)

    def test_text_signal_classifier_preserves_single_text_contract(self) -> None:
        assert extract_text_signals(INCIDENT_SIGNAL_TEXT) == [
            "branch_mismatch_review_block",
            "needs_human_direction",
            "original_plan_missing",
            "dirty_merge_verification",
        ]

    def test_successful_transcript_stderr_does_not_emit_text_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            run_dir = repo_root / ".aflow" / "runs" / "20260802T001252Z-transcript"
            _write_json(
                run_dir / "run.json",
                {
                    "status": "running",
                    "workflow_name": "medium",
                    "turns_completed": 1,
                    "current_step_name": "review_cp_implementation",
                },
            )
            _write_turn(
                run_dir,
                1,
                result={
                    "turn_number": 1,
                    "step_name": "implement_plan",
                    "status": "completed",
                    "returncode": 0,
                    "chosen_transition": "review_cp_implementation",
                    "snapshot_before": _snapshot(),
                    "snapshot_after": _snapshot(),
                },
                stderr=INCIDENT_SIGNAL_TEXT,
            )
            payload = self._run_aflow_analyze(
                "20260802T001252Z-transcript",
                "--repo-root",
                str(repo_root),
            )

        assert payload["run"]["failure"]["signals"] == []
        assert payload["run"]["focus_turns"][0]["signals"] == []
        assert payload["run"]["focus_turns"][0]["signal_provenance"] == []

    def test_successful_semantic_stdout_emits_exact_signal_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            turn = self._analyze_turn(Path(tmpdir), stdout=INCIDENT_SIGNAL_TEXT)

        assert turn["signals"] == INCIDENT_SIGNAL_NAMES
        assert turn["signal_provenance"] == [
            {"name": name, "source": "semantic_stdout", "trust_class": "semantic_output"}
            for name in INCIDENT_SIGNAL_NAMES
        ]

    def test_nonzero_stderr_emits_failure_diagnostic_provenance(self) -> None:
        evidence = classify_turn_text_signals("", INCIDENT_SIGNAL_TEXT, "completed", 1)

        assert [item.name for item in evidence] == INCIDENT_SIGNAL_NAMES
        assert [
            {"name": item.name, "source": item.source, "trust_class": item.trust_class}
            for item in evidence
        ] == [
            {"name": name, "source": "failure_stderr", "trust_class": "failure_diagnostic"}
            for name in INCIDENT_SIGNAL_NAMES
        ]

    def test_each_failure_like_status_unlocks_stderr_without_nonzero_returncode(self) -> None:
        for status in (
            "failed",
            "harness-failed",
            "recovery-failed",
            "plan-invalid",
            "retry-scheduled",
            "transition-failed",
        ):
            for returncode in (0, None):
                with self.subTest(status=status, returncode=returncode):
                    evidence = classify_turn_text_signals("", INCIDENT_SIGNAL_TEXT, status, returncode)
                    assert [item.name for item in evidence] == INCIDENT_SIGNAL_NAMES
                    assert all(item.source == "failure_stderr" for item in evidence)
                    assert all(item.trust_class == "failure_diagnostic" for item in evidence)

    def test_unknown_or_missing_status_does_not_unlock_stderr(self) -> None:
        for status in ("unknown", None):
            for returncode in (0, None):
                with self.subTest(status=status, returncode=returncode):
                    assert classify_turn_text_signals("", INCIDENT_SIGNAL_TEXT, status, returncode) == []

    def test_text_signal_provenance_deduplicates_by_name_and_source(self) -> None:
        evidence = classify_turn_text_signals(
            INCIDENT_SIGNAL_TEXT + "\n" + INCIDENT_SIGNAL_TEXT,
            INCIDENT_SIGNAL_TEXT,
            "failed",
            1,
        )

        assert [(item.name, item.source) for item in evidence] == sorted(
            (name, source)
            for name in INCIDENT_SIGNAL_NAMES
            for source in ("failure_stderr", "semantic_stdout")
        )

    def test_structured_and_explicit_stop_signals_remain_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            turn = self._analyze_turn(
                Path(tmpdir),
                status="plan-invalid",
                returncode=2,
                stderr="AFLOW_STOP: halt for owner direction",
                error="Inconsistent checkpoint state",
                recovery_source="deterministic",
                recovery_action="fail_immediately",
                recovery_from_team="team-a",
                recovery_to_team="team-a",
            )

        assert turn["aflow_stop_messages"] == ["halt for owner direction"]
        assert turn["signal_provenance"] == []
        assert turn["signals"] == [
            "harness_recovery_deterministic",
            "harness_recovery_fail_immediately",
            "harness_recovery_present",
            "inconsistent_checkpoint_state",
            "nonzero_returncode",
            "plan_invalid",
        ]

    def test_retry_scheduled_structured_signals_remain_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            turn = self._analyze_turn(Path(tmpdir), status="retry-scheduled", returncode=None)

        assert turn["signals"] == ["inconsistent_checkpoint_state", "retry_scheduled"]
        assert turn["signal_provenance"] == []

    def test_analyzer_defaults_to_latest_substantive_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"

            noise_run = runs_root / "20260401T000200Z-noise"
            _write_json(
                noise_run / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "other",
                    "turns_completed": 0,
                    "end_reason": "already_complete",
                },
            )

            merge_run = runs_root / "20260401T000100Z-merge"
            _write_json(
                merge_run / "run.json",
                {
                    "status": "failed",
                    "workflow_name": "medium",
                    "turns_completed": 8,
                    "merge_failure_reason": "AFLOW_STOP: feature worktree and primary checkout are dirty, so merge verification cannot be completed safely",
                    "current_step_name": "review_implementation",
                },
            )
            _write_turn(
                merge_run,
                1,
                result={
                    "turn_number": 1,
                    "step_name": "review_implementation",
                    "status": "completed",
                    "returncode": 0,
                    "chosen_transition": "END",
                    "snapshot_before": _snapshot(),
                    "snapshot_after": _snapshot(is_complete=True, unchecked_steps=0, unchecked_checkpoints=0),
                },
            )

            payload = self._run_analyzer("--repo-root", str(repo_root))
            assert payload["analysis_scope"]["mode"] == "single_run"
            # The wrapper script now finds the latest run and passes it explicitly
            assert payload["analysis_scope"]["selection"] in ("latest_run", "explicit_run_id")
            assert payload["analysis_scope"]["run_count_considered"] == 1
            # Note: run_count_skipped_as_noise is 0 because the wrapper script does the run discovery
            # and doesn't communicate the skipped count to aflow analyze
            assert payload["analysis_scope"]["run_count_skipped_as_noise"] == 0
            assert payload["run"]["run_id"] == "20260401T000100Z-merge"
            assert payload["run"]["outcome"]["kind"] == "merge_failure"
            assert payload["run"]["failure"]["signals"] == ["dirty_merge_verification", "merge_failure"]
            assert payload["run"]["focus_turns"][0]["turn_number"] == 1

    def test_analyzer_detects_blocked_review_loop_and_interrupted_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            blocked_run = runs_root / "20260401T000300Z-blocked"

            _write_json(
                blocked_run / "run.json",
                {
                    "status": "running",
                    "workflow_name": "hard",
                    "turns_completed": 4,
                    "current_step_name": "review_cp_implementation",
                    "last_snapshot": _snapshot(),
                },
            )

            blocked_stdout = (
                "Blocked on a required `aflow-review-checkpoint` precondition.\n\n"
                "The original plan records `Plan Branch: main`, but the current checkout is "
                "`aflow-feature-branch` and the skill requires me to stop and escalate.\n\n"
                "Need your direction on one point:\n"
                "1. Review this branch.\n"
                "2. Switch to main.\n"
            )

            for turn_number, step_name in ((1, "implement_plan"), (2, "review_cp_implementation"), (3, "implement_plan"), (4, "review_cp_implementation")):
                _write_turn(
                    blocked_run,
                    turn_number,
                    result={
                        "turn_number": turn_number,
                        "step_name": step_name,
                        "status": "running",
                        "returncode": 0,
                        "chosen_transition": "implement_plan" if "review" in step_name else "review_cp_implementation",
                        "snapshot_before": _snapshot(),
                        "snapshot_after": _snapshot(),
                    },
                    stdout=blocked_stdout if turn_number == 4 else "",
                )

            _write_turn(
                blocked_run,
                5,
                result={
                    "turn_number": 5,
                    "step_name": "review_cp_implementation",
                    "status": "starting",
                    "snapshot_before": _snapshot(),
                    "snapshot_after": None,
                },
            )

            payload = self._run_analyzer("--repo-root", str(repo_root))
            assert payload["run"]["run_id"] == "20260401T000300Z-blocked"
            assert payload["run"]["failure"]["signals"] == [
                "alternating_review_execute_loop",
                "blocked_review_precondition",
                "branch_mismatch_review_block",
                "interrupted_or_abandoned",
                "needs_human_direction",
                "no_progress_tail",
            ]
            assert payload["run"]["outcome"]["kind"] == "interrupted_or_abandoned"
            assert payload["run"]["progress"]["tail_start_turn"] == 1
            focus_turn_numbers = [turn["turn_number"] for turn in payload["run"]["focus_turns"]]
            assert focus_turn_numbers == [1, 4, 5]

    def test_analyzer_supports_run_id_and_extracts_plan_invalid_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            run_dir = runs_root / "20260401T000400Z-invalid"
            _write_json(
                run_dir / "run.json",
                {
                    "status": "failed",
                    "workflow_name": "rir",
                    "turns_completed": 6,
                    "failure_reason": "plans/in-progress/original.md: original plan file is missing after the turn",
                    "current_step_name": "implement_plan",
                    "original_plan_path": "plans/in-progress/original.md",
                    "active_plan_path": "plans/in-progress/fix.md",
                },
            )
            _write_turn(
                run_dir,
                6,
                result={
                    "turn_number": 6,
                    "step_name": "implement_plan",
                    "status": "plan-invalid",
                    "returncode": 0,
                    "error": "plans/in-progress/original.md: original plan file is missing after the turn",
                    "snapshot_before": _snapshot(is_complete=True, unchecked_steps=0, unchecked_checkpoints=0),
                    "snapshot_after": None,
                },
            )

            payload = self._run_analyzer("--repo-root", str(repo_root), "--run-id", "20260401T000400Z-invalid")
            assert payload["analysis_scope"]["selection"] == "explicit_run_id"
            assert payload["analysis_scope"]["runs_root"] == str(runs_root.resolve())
            assert payload["analysis_scope"]["run_count_considered"] == 1
            assert payload["run"]["outcome"]["kind"] == "plan_invalid"
            assert payload["run"]["failure"]["signals"] == ["original_plan_missing", "plan_invalid", "workflow_failure"]
            assert payload["run"]["signal_turns"][0]["turn_number"] == 6
            assert payload["run"]["signal_turns"][0]["artifact_paths"]["result_json"].endswith("turn-006/result.json")

    def test_analyzer_supports_corpus_mode_and_filters_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"

            noise_run = runs_root / "20260401T000000Z-noise"
            _write_json(
                noise_run / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "other",
                    "turns_completed": 0,
                    "end_reason": "already_complete",
                },
            )

            merge_run = runs_root / "20260401T000100Z-merge"
            _write_json(
                merge_run / "run.json",
                {
                    "status": "failed",
                    "workflow_name": "medium",
                    "turns_completed": 8,
                    "merge_failure_reason": "AFLOW_STOP: feature worktree and primary checkout are dirty, so merge verification cannot be completed safely",
                },
            )
            _write_turn(
                merge_run,
                8,
                result={
                    "turn_number": 8,
                    "step_name": "review_implementation",
                    "status": "completed",
                    "returncode": 0,
                    "chosen_transition": "END",
                    "snapshot_before": _snapshot(is_complete=True, unchecked_steps=0, unchecked_checkpoints=0),
                    "snapshot_after": _snapshot(is_complete=True, unchecked_steps=0, unchecked_checkpoints=0),
                },
            )

            invalid_run = runs_root / "20260401T000200Z-invalid"
            _write_json(
                invalid_run / "run.json",
                {
                    "status": "failed",
                    "workflow_name": "rir",
                    "turns_completed": 5,
                    "failure_reason": "plans/in-progress/original.md: original plan file is missing after the turn",
                    "current_step_name": "implement_plan",
                },
            )
            _write_turn(
                invalid_run,
                6,
                result={
                    "turn_number": 6,
                    "step_name": "implement_plan",
                    "status": "plan-invalid",
                    "returncode": 0,
                    "error": "plans/in-progress/original.md: original plan file is missing after the turn",
                    "snapshot_before": _snapshot(),
                    "snapshot_after": None,
                },
            )

            payload = self._run_analyzer("--repo-root", str(repo_root), "--all")
            assert payload["analysis_scope"]["mode"] == "corpus"
            assert payload["analysis_scope"]["run_count_considered"] == 2
            assert payload["analysis_scope"]["run_count_skipped_as_noise"] == 1
            runs = {run["run_id"]: run for run in payload["runs"]}
            assert runs["20260401T000100Z-merge"]["failure"]["signals"] == ["dirty_merge_verification", "merge_failure"]
            assert runs["20260401T000200Z-invalid"]["failure"]["signals"] == ["original_plan_missing", "plan_invalid", "workflow_failure"]


class AflowAnalyzeCliTests(unittest.TestCase):
    def _get_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("AFLOW_LAST_RUN_ID", None)
        env.pop("AFLOW_SHELL_ID", None)
        # Ensure the local aflow module is found
        repo_root = Path(__file__).resolve().parents[1]
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = f"{repo_root}:{env['PYTHONPATH']}"
        else:
            env["PYTHONPATH"] = str(repo_root)
        return env

    def _run_aflow_analyze(self, *args: str, cwd: Path | None = None) -> dict[str, object]:
        env = self._get_env()
        result = subprocess.run(
            [sys.executable, "-m", "aflow", "analyze", *args],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )
        return json.loads(result.stdout)

    def _run_aflow_analyze_with_env(
        self,
        *args: str,
        env_var: str | None = None,
        shell_id: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, object]:
        env = self._get_env()
        if env_var is not None:
            env["AFLOW_LAST_RUN_ID"] = env_var
        if shell_id is not None:
            env["AFLOW_SHELL_ID"] = shell_id
        result = subprocess.run(
            [sys.executable, "-m", "aflow", "analyze", *args],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )
        return json.loads(result.stdout)

    def test_aflow_analyze_explicit_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            run_dir = runs_root / "20260401T000100Z-test"
            _write_json(
                run_dir / "run.json",
                {
                    "status": "failed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                    "failure_reason": "test failure",
                },
            )
            _write_turn(
                run_dir,
                1,
                result={
                    "turn_number": 1,
                    "step_name": "implement",
                    "status": "completed",
                    "returncode": 0,
                    "snapshot_before": _snapshot(),
                    "snapshot_after": _snapshot(),
                },
            )

            payload = self._run_aflow_analyze("20260401T000100Z-test", "--repo-root", str(repo_root))
            assert payload["analysis_scope"]["mode"] == "single_run"
            assert payload["analysis_scope"]["selection"] == "explicit_run_id"
            assert payload["run"]["run_id"] == "20260401T000100Z-test"

    def test_aflow_analyze_env_var_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            run_dir = runs_root / "20260401T000200Z-env"
            _write_json(
                run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            payload = self._run_aflow_analyze_with_env("--repo-root", str(repo_root), env_var="20260401T000200Z-env")
            assert payload["analysis_scope"]["mode"] == "single_run"
            assert payload["analysis_scope"]["selection"] == "env_var"
            assert payload["run"]["run_id"] == "20260401T000200Z-env"

    def test_aflow_analyze_shell_last_run_id_fallback(self) -> None:
        from aflow.runlog import shell_last_run_id_path

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            run_dir = runs_root / "20260401T000250Z-shell"
            _write_json(
                run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            with patch.dict(os.environ, {"AFLOW_SHELL_ID": "shell-1"}):
                shell_file = shell_last_run_id_path(repo_root)
            assert shell_file is not None
            shell_file.parent.mkdir(parents=True, exist_ok=True)
            shell_file.write_text("20260401T000250Z-shell", encoding="utf-8")

            payload = self._run_aflow_analyze_with_env("--repo-root", str(repo_root), shell_id="shell-1")
            assert payload["analysis_scope"]["mode"] == "single_run"
            assert payload["analysis_scope"]["selection"] == "shell_last_run_id_file"
            assert payload["run"]["run_id"] == "20260401T000250Z-shell"

    def test_aflow_analyze_last_run_id_file_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            run_dir = runs_root / "20260401T000300Z-file"
            _write_json(
                run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            aflow_dir = repo_root / ".aflow"
            aflow_dir.mkdir(parents=True, exist_ok=True)
            last_run_id_file = aflow_dir / "last_run_id"
            last_run_id_file.write_text("20260401T000300Z-file", encoding="utf-8")

            payload = self._run_aflow_analyze("--repo-root", str(repo_root))
            assert payload["analysis_scope"]["mode"] == "single_run"
            assert payload["analysis_scope"]["selection"] == "last_run_id_file"
            assert payload["run"]["run_id"] == "20260401T000300Z-file"

    def test_aflow_analyze_fails_without_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            env = self._get_env()
            result = subprocess.run(
                [sys.executable, "-m", "aflow", "analyze", "--repo-root", str(repo_root)],
                capture_output=True,
                text=True,
                cwd=repo_root,
                env=env,
            )
            assert result.returncode != 0
            assert "no run ID specified and no last run ID found" in result.stderr

    def test_aflow_analyze_corpus_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"

            run1 = runs_root / "20260401T000100Z-run1"
            _write_json(
                run1 / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            run2 = runs_root / "20260401T000200Z-run2"
            _write_json(
                run2 / "run.json",
                {
                    "status": "failed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            payload = self._run_aflow_analyze("--all", "--repo-root", str(repo_root))
            assert payload["analysis_scope"]["mode"] == "corpus"
            assert payload["analysis_scope"]["run_count_considered"] == 2
            assert len(payload["runs"]) == 2

    def test_aflow_analyze_surfaces_recovery_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"
            run_dir = runs_root / "20260401T000500Z-recovery"
            _write_json(
                run_dir / "run.json",
                {
                    "status": "failed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                    "failure_reason": "harness failure",
                    "recovery_summary": {
                        "action": "switch_to_backup_team_and_retry",
                        "consecutive_count": 1,
                        "delay_seconds": 0,
                        "executed": True,
                        "from_team": "team-a",
                        "matched_terms": ["quota"],
                        "match_terms": ["quota"],
                        "reason": "switch to the backup team",
                        "rejection_reason": None,
                        "source": "team_lead",
                        "suggested_action": "switch_to_backup_team_and_retry",
                        "suggested_keywords": ["throttled", "quota"],
                        "to_team": "team-b",
                    },
                    "recovery_history": [],
                },
            )
            _write_turn(
                run_dir,
                1,
                result={
                    "turn_number": 1,
                    "step_name": "implement",
                    "status": "failed",
                    "returncode": 1,
                    "snapshot_before": _snapshot(),
                    "snapshot_after": _snapshot(),
                    "recovery_action": "switch_to_backup_team_and_retry",
                    "recovery_consecutive_count": 1,
                    "recovery_delay_seconds": 0,
                    "recovery_executed": True,
                    "recovery_from_team": "team-a",
                    "recovery_matched_terms": ["quota"],
                    "recovery_match_terms": ["quota"],
                    "recovery_reason": "switch to the backup team",
                    "recovery_rejection_reason": None,
                    "recovery_source": "team_lead",
                    "recovery_suggested_action": "switch_to_backup_team_and_retry",
                    "recovery_suggested_keywords": ["throttled", "quota"],
                    "recovery_to_team": "team-b",
                },
            )

            payload = self._run_aflow_analyze("20260401T000500Z-recovery", "--repo-root", str(repo_root))
            assert payload["analysis_scope"]["selection"] == "explicit_run_id"
            assert payload["run"]["recovery_summary"]["action"] == "switch_to_backup_team_and_retry"
            assert payload["run"]["recovery_summary"]["source"] == "team_lead"
            assert payload["run"]["focus_turns"][0]["recovery"]["source"] == "team_lead"
            assert payload["run"]["focus_turns"][0]["recovery"]["to_team"] == "team-b"
            assert "harness_recovery_team_lead" in payload["run"]["failure"]["signals"]
            assert "harness_recovery_team_switch" in payload["run"]["failure"]["signals"]
            assert "harness_recovery_keyword_suggestions" in payload["run"]["failure"]["signals"]

    def test_aflow_analyze_env_var_takes_priority_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"

            env_run_dir = runs_root / "20260401T000100Z-env"
            _write_json(
                env_run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            file_run_dir = runs_root / "20260401T000200Z-file"
            _write_json(
                file_run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            aflow_dir = repo_root / ".aflow"
            aflow_dir.mkdir(parents=True, exist_ok=True)
            last_run_id_file = aflow_dir / "last_run_id"
            last_run_id_file.write_text("20260401T000200Z-file", encoding="utf-8")

            payload = self._run_aflow_analyze_with_env("--repo-root", str(repo_root), env_var="20260401T000100Z-env")
            assert payload["analysis_scope"]["selection"] == "env_var"
            assert payload["run"]["run_id"] == "20260401T000100Z-env"

    def test_aflow_analyze_shell_last_run_id_takes_priority_over_env_and_global_file(self) -> None:
        from aflow.runlog import shell_last_run_id_path

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            runs_root = repo_root / ".aflow" / "runs"

            shell_run_dir = runs_root / "20260401T000050Z-shell"
            _write_json(
                shell_run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            env_run_dir = runs_root / "20260401T000100Z-env"
            _write_json(
                env_run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            file_run_dir = runs_root / "20260401T000200Z-file"
            _write_json(
                file_run_dir / "run.json",
                {
                    "status": "completed",
                    "workflow_name": "test",
                    "turns_completed": 1,
                },
            )

            with patch.dict(os.environ, {"AFLOW_SHELL_ID": "shell-2"}):
                shell_file = shell_last_run_id_path(repo_root)
            assert shell_file is not None
            shell_file.parent.mkdir(parents=True, exist_ok=True)
            shell_file.write_text("20260401T000050Z-shell", encoding="utf-8")

            aflow_dir = repo_root / ".aflow"
            aflow_dir.mkdir(parents=True, exist_ok=True)
            (aflow_dir / "last_run_id").write_text("20260401T000200Z-file", encoding="utf-8")

            payload = self._run_aflow_analyze_with_env(
                "--repo-root",
                str(repo_root),
                env_var="20260401T000100Z-env",
                shell_id="shell-2",
            )
            assert payload["analysis_scope"]["selection"] == "shell_last_run_id_file"
            assert payload["run"]["run_id"] == "20260401T000050Z-shell"


class LastRunIdTrackingTests(unittest.TestCase):
    def test_create_run_paths_writes_last_run_id(self) -> None:
        from aflow.runlog import create_run_paths, shell_last_run_id_path

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            aflow_dir = repo_root / ".aflow"
            aflow_dir.mkdir(parents=True, exist_ok=True)

            config = type("ControllerConfig", (), {
                "repo_root": repo_root,
                "keep_runs": 5,
            })()

            with patch.dict(os.environ, {"AFLOW_SHELL_ID": "shell-a"}):
                paths = create_run_paths(config)
                shell_file = shell_last_run_id_path(repo_root)
            last_run_id_file = aflow_dir / "last_run_id"

            assert last_run_id_file.exists()
            run_id = last_run_id_file.read_text(encoding="utf-8").strip()
            assert run_id == paths.run_dir.name
            assert shell_file is not None
            assert shell_file.exists()
            assert shell_file.read_text(encoding="utf-8").strip() == paths.run_dir.name

    def test_write_last_run_id_creates_directory(self) -> None:
        from aflow.runlog import shell_last_run_id_path, write_last_run_id

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            aflow_dir = repo_root / ".aflow"

            with patch.dict(os.environ, {"AFLOW_SHELL_ID": "shell-b"}):
                write_last_run_id(repo_root, "20260401T000100Z-test")
                shell_file = shell_last_run_id_path(repo_root)

            assert aflow_dir.exists()
            assert (aflow_dir / "last_run_id").exists()
            content = (aflow_dir / "last_run_id").read_text(encoding="utf-8").strip()
            assert content == "20260401T000100Z-test"
            assert shell_file is not None
            assert shell_file.exists()
            assert shell_file.read_text(encoding="utf-8").strip() == "20260401T000100Z-test"


if __name__ == "__main__":
    unittest.main()

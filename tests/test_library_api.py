"""Tests for the public aflow library API."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from aflow.api import (
    AnalyzeRequest,
    PreparedRun,
    StartupError,
    StartupQuestion,
    StartupQuestionKind,
    StartupRequest,
    analyze_runs,
    prepare_startup,
    prepare_startup_with_answer,
)
from aflow.config import (
    AflowSection,
    load_workflow_config,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.plan import load_plan


def _write_config(home_dir: Path, text: str) -> Path:
    config_path = home_dir / ".config" / "aflow" / "aflow.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    aflow_lines: list[str] = []
    workflow_lines: list[str] = []
    current = aflow_lines
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            header = stripped[1 : stripped.find("]")]
            current = workflow_lines if header.startswith("workflow") else aflow_lines
        current.append(line)
    config_path.write_text("".join(aflow_lines), encoding="utf-8")
    config_path.with_name("workflows.toml").write_text("".join(workflow_lines), encoding="utf-8")
    return config_path


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


class LibraryStartupTests(unittest.TestCase):
    """Test the public startup library API."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home_dir = Path(self.temp_dir.name)
        self.repo_root = self.home_dir / "repo"
        self.repo_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_prepare_startup_requires_valid_workflow(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: Test\n- [ ] step one\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="invalid",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
            dirty_worktree_confirmed=True,
        )

        with self.assertRaises(StartupError) as ctx:
            prepare_startup(request)
        self.assertIn("not found", str(ctx.exception))

    def test_prepare_startup_uses_default_workflow(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: Test\n- [ ] step one\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name=None,
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
            dirty_worktree_confirmed=True,
        )

        result = prepare_startup(request)
        self.assertIsInstance(result, PreparedRun)
        self.assertEqual(result.workflow_name, "test")

    def test_prepare_startup_invalid_start_step(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step2"}]\n\n'
            '[workflow.test.steps.step2]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: Test\n- [ ] step one\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step="invalid_step",
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        with self.assertRaises(StartupError) as ctx:
            prepare_startup(request)
        self.assertIn("not found", str(ctx.exception))

    def test_prepare_startup_plan_complete_rejects_start_step(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step="step1",
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        with self.assertRaises(StartupError) as ctx:
            prepare_startup(request)
        self.assertIn("already complete", str(ctx.exception))

    def test_prepare_startup_multi_step_plan_asks_for_selection(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step2"}]\n\n'
            '[workflow.test.steps.step2]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n\n### [ ] Checkpoint 2: Next\n- [ ] step two\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
            dirty_worktree_confirmed=True,
        )

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            result = prepare_startup(request)
        self.assertIsInstance(result, StartupQuestion)
        self.assertEqual(result.kind, StartupQuestionKind.PICK_STEP)
        self.assertIn("step1", result.choices)
        self.assertIn("step2", result.choices)

    def test_prepare_startup_with_answer_pick_step(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step2"}]\n\n'
            '[workflow.test.steps.step2]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n\n### [ ] Checkpoint 2: Next\n- [ ] step two\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            question = prepare_startup(request)
        self.assertIsInstance(question, StartupQuestion)

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            result = prepare_startup_with_answer(question, request, 0)
        self.assertIsInstance(result, PreparedRun)
        self.assertEqual(result.start_step, "step1")

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            result = prepare_startup_with_answer(question, request, 1)
        self.assertIsInstance(result, PreparedRun)
        self.assertEqual(result.start_step, "step2")

    def test_prepare_startup_with_answer_step_by_name(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step2"}]\n\n'
            '[workflow.test.steps.step2]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n\n### [ ] Checkpoint 2: Next\n- [ ] step two\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            question = prepare_startup(request)
        self.assertIsInstance(question, StartupQuestion)

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            result = prepare_startup_with_answer(question, request, "step2")
        self.assertIsInstance(result, PreparedRun)
        self.assertEqual(result.start_step, "step2")

    def test_prepare_startup_recovery_then_step_selection(self) -> None:
        from aflow.plan import PlanParseError

        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step2"}]\n\n'
            '[workflow.test.steps.step2]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        broken_plan = "# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n\n### [ ] Checkpoint 2: Next\n- [ ] step two\n"
        plan_path.write_text(broken_plan)

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            question1 = prepare_startup(request)

        self.assertIsInstance(question1, StartupQuestion)
        self.assertEqual(question1.kind, StartupQuestionKind.CONFIRM_RECOVERY)
        self.assertIsNotNone(question1.continuation_request)

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            question2 = prepare_startup_with_answer(question1, request, True)

        self.assertIsInstance(question2, StartupQuestion)
        self.assertEqual(question2.kind, StartupQuestionKind.PICK_STEP)
        self.assertIsNotNone(question2.continuation_request)
        self.assertIsNotNone(question2.continuation_request.pre_recovered_plan)

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            result = prepare_startup_with_answer(question2, request, "step2")

        self.assertIsInstance(result, PreparedRun)
        self.assertEqual(result.start_step, "step2")
        self.assertIsNotNone(result.startup_retry)

    def test_prepare_startup_recovery_then_dirty_confirmation(self) -> None:
        from unittest.mock import patch as mock_patch

        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        broken_plan = "# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n"
        plan_path.write_text(broken_plan)

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            question1 = prepare_startup(request)

        self.assertIsInstance(question1, StartupQuestion)
        self.assertEqual(question1.kind, StartupQuestionKind.CONFIRM_RECOVERY)

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             mock_patch('aflow.api.startup.probe_worktree') as mock_probe:
            mock_probe.return_value = type('obj', (object,), {'is_dirty': True, 'modified_count': 1, 'added_count': 0, 'removed_count': 0})()
            question2 = prepare_startup_with_answer(question1, request, True)

        self.assertIsInstance(question2, StartupQuestion)
        self.assertEqual(question2.kind, StartupQuestionKind.CONFIRM_WORKTREE_DIRTY)
        self.assertIsNotNone(question2.continuation_request)

        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            result = prepare_startup_with_answer(question2, request, True)

        self.assertIsInstance(result, PreparedRun)
        self.assertIsNotNone(result.startup_retry)

    def test_prepare_startup_recovery_step_selection_dirty_confirmation(self) -> None:
        """Test the combined recovery -> step selection -> dirty confirmation chain.

        This regression test ensures that the full public API chain works without
        hidden state reconstruction: CONFIRM_RECOVERY -> PICK_STEP -> CONFIRM_WORKTREE_DIRTY -> PreparedRun.
        """
        from unittest.mock import patch as mock_patch

        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step2"}]\n\n'
            '[workflow.test.steps.step2]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        broken_plan = "# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n\n### [ ] Checkpoint 2: Next\n- [ ] step two\n"
        plan_path.write_text(broken_plan)

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        # Step 1: Ask for recovery confirmation
        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            question1 = prepare_startup(request)

        self.assertIsInstance(question1, StartupQuestion)
        self.assertEqual(question1.kind, StartupQuestionKind.CONFIRM_RECOVERY)
        self.assertIsNotNone(question1.continuation_request)

        # Steps 2-4: Keep dirty worktree mock active throughout the chain
        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             mock_patch('aflow.api.startup.probe_worktree') as mock_probe:
            dirty_probe = type('obj', (object,), {'is_dirty': True, 'modified_count': 1, 'added_count': 0, 'removed_count': 0})()
            mock_probe.return_value = dirty_probe

            # Step 2: Confirm recovery, ask for step selection
            question2 = prepare_startup_with_answer(question1, request, True)
            self.assertIsInstance(question2, StartupQuestion)
            self.assertEqual(question2.kind, StartupQuestionKind.PICK_STEP)
            self.assertIn("step1", question2.choices)
            self.assertIn("step2", question2.choices)
            self.assertIsNotNone(question2.continuation_request)
            self.assertIsNotNone(question2.continuation_request.pre_recovered_plan)

            # Step 3: Select a step, ask for dirty confirmation
            question3 = prepare_startup_with_answer(question2, request, "step2")
            self.assertIsInstance(question3, StartupQuestion)
            self.assertEqual(question3.kind, StartupQuestionKind.CONFIRM_WORKTREE_DIRTY)
            self.assertIsNotNone(question3.continuation_request)

            # Step 4: Confirm dirty worktree, reach final PreparedRun
            result = prepare_startup_with_answer(question3, request, True)
            self.assertIsInstance(result, PreparedRun)
            # Verify selected step survived through the chain
            self.assertEqual(result.start_step, "step2")
            # Verify startup retry context survived
            self.assertIsNotNone(result.startup_retry)

    def test_prepare_startup_numeric_start_step_resolves_to_step_name(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step2"}]\n\n'
            '[workflow.test.steps.step2]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "step3"}]\n\n'
            '[workflow.test.steps.step3]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: Test\n- [ ] step one\n")

        # Test numeric step "1" resolves to "step1"
        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step="1",
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        result = prepare_startup(request)
        self.assertIsInstance(result, PreparedRun)
        self.assertEqual(result.start_step, "step1")

        # Test numeric step "2" resolves to "step2"
        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step="2",
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        result = prepare_startup(request)
        self.assertIsInstance(result, PreparedRun)
        self.assertEqual(result.start_step, "step2")

    def test_prepare_startup_numeric_start_step_out_of_range_raises_error(self) -> None:
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: Test\n- [ ] step one\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step="5",  # Out of range
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        with self.assertRaises(StartupError) as ctx:
            prepare_startup(request)
        self.assertIn("out of range", str(ctx.exception))

    def test_prepare_startup_auto_sets_base_head_refresh_for_pristine_git_tracking_plan(self) -> None:
        subprocess.run(['git', 'init', '-b', 'main'], cwd=str(self.repo_root), check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=str(self.repo_root), check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=str(self.repo_root), check=True, capture_output=True)
        (self.repo_root / 'README.md').write_text('init\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'README.md'], cwd=str(self.repo_root), check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=str(self.repo_root), check=True, capture_output=True)
        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text(textwrap.dedent("""\
            # Plan

            ## Git Tracking

            - Plan Branch: ``
            - Pre-Handoff Base HEAD: ``

            ### [ ] Checkpoint 1: Test
            - [ ] step one
        """), encoding="utf-8")
        subprocess.run(['git', 'add', 'plan.md'], cwd=str(self.repo_root), check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'add plan'], cwd=str(self.repo_root), check=True, capture_output=True)
        current_head = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=None,
            team=None,
            extra_instructions=(),
        )

        result = prepare_startup(request)
        self.assertIsInstance(result, PreparedRun)
        assert isinstance(result, PreparedRun)
        self.assertEqual(result.startup_base_head_refresh_sha, current_head)

    def test_prepare_startup_resume_skips_started_base_head_mismatch(self) -> None:
        subprocess.run(
            ['git', 'init', '-b', 'main'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ['git', 'config', 'user.email', 'test@test.com'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ['git', 'config', 'user.name', 'Test'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )
        readme_path = self.repo_root / 'README.md'
        readme_path.write_text('init\n', encoding='utf-8')
        subprocess.run(
            ['git', 'add', 'README.md'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ['git', 'commit', '-m', 'init'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )
        base_head = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        config_path = _write_config(
            self.home_dir,
            (
                '[aflow]\ndefault_workflow = "test"\n\n'
                '[workflow.test.steps.step1]\n'
                'role = "architect"\nprompts = ["p"]\n'
                'go = [{to = "END"}]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n'
            ),
        )
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / 'plan.md'
        plan_path.write_text(
            textwrap.dedent(
                f"""\
                # Plan

                ## Git Tracking

                - Plan Branch: `feature/work`
                - Pre-Handoff Base HEAD: `{base_head}`

                ### [ ] Checkpoint 1: Test
                - [ ] step one
                """
            ),
            encoding='utf-8',
        )
        subprocess.run(
            ['git', 'add', 'plan.md'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ['git', 'commit', '-m', 'add started plan'],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )

        result = prepare_startup(
            StartupRequest(
                repo_root=self.repo_root,
                plan_path=plan_path,
                config_path=config_path,
                workflow_config=workflow_config,
                workflow_name='test',
                start_step=None,
                max_turns=None,
                team=None,
                extra_instructions=(),
                resume_requested=True,
            )
        )

        self.assertIsInstance(result, PreparedRun)
        assert isinstance(result, PreparedRun)
        self.assertIsNone(result.startup_base_head_refresh_sha)


class LibraryRunnerTests(unittest.TestCase):
    """Test the public runner library API with events."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home_dir = Path(self.temp_dir.name)
        self.repo_root = self.home_dir / "repo"
        self.repo_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_execute_workflow_with_observer_emits_events(self) -> None:
        """Test that execute_workflow emits events through the observer."""
        from aflow.api import (
            CollectingObserver,
            RunCompletedEvent,
            RunStartedEvent,
            execute_workflow,
        )
        from aflow.harnesses.base import HarnessAdapter

        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n## Done When\n- Plan is complete\n\n### [x] Checkpoint 1: Test\n- [x] step one\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=5,
            team=None,
            extra_instructions=(),
        )

        result = prepare_startup(request)
        self.assertIsInstance(result, PreparedRun)

        observer = CollectingObserver()

        class FakeAdapter(HarnessAdapter):
            name = "fake"
            supports_effort = False

            def build_invocation(self, repo_root, model, system_prompt, user_prompt, effort=None):
                from aflow.harnesses.base import HarnessInvocation
                return HarnessInvocation(
                    label="fake",
                    argv=(sys.executable, "-c", "print('done')"),
                    env={},
                    prompt_mode="text",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    effective_prompt=user_prompt,
                )

        execute_workflow(
            result,
            observer=observer,
            adapter=FakeAdapter(),
            runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "done\n", ""),
        )

        events = observer.events
        event_types = [type(e) for e in events if e]

        self.assertIn(RunStartedEvent, event_types)
        self.assertTrue(any(isinstance(e, RunCompletedEvent) for e in events))

    def test_execute_workflow_emits_recovery_metadata_in_events(self) -> None:
        from aflow.api import CollectingObserver, RunCompletedEvent, TurnFinishedEvent, execute_workflow
        from aflow.harnesses.base import HarnessAdapter

        config_text = (
            '[aflow]\ndefault_workflow = "test"\n\n'
            '[workflow.test.steps.step1]\nrole = "architect"\nprompts = ["p"]\ngo = [{to = "END", when = "DONE"}, {to = "step1"}]\n\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[prompts]\np = "do it"\n\n'
            '[error_handling.harness_error_recovery]\n'
            '[[error_handling.harness_error_recovery.rules]]\n'
            'action = "retry_same_team_after_delay"\n'
            'match = ["throttled"]\n'
        )
        config_path = _write_config(self.home_dir, config_text)
        workflow_config = load_workflow_config(config_path)
        plan_path = self.repo_root / "plan.md"
        plan_path.write_text("# Plan\n\n### [ ] Checkpoint 1: Test\n- [ ] step one\n")

        request = StartupRequest(
            repo_root=self.repo_root,
            plan_path=plan_path,
            config_path=config_path,
            workflow_config=workflow_config,
            workflow_name="test",
            start_step=None,
            max_turns=5,
            team=None,
            extra_instructions=(),
        )

        result = prepare_startup(request)
        self.assertIsInstance(result, PreparedRun)

        observer = CollectingObserver()

        class FakeAdapter(HarnessAdapter):
            name = "fake"
            supports_effort = False

            def build_invocation(self, repo_root, model, system_prompt, user_prompt, effort=None):
                from aflow.harnesses.base import HarnessInvocation
                return HarnessInvocation(
                    label="fake",
                    argv=(sys.executable, "-c", "print('done')"),
                    env={},
                    prompt_mode="text",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    effective_prompt=user_prompt,
                )

        call_count = {"count": 0}

        def runner(argv, **kwargs):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return subprocess.CompletedProcess(argv, 1, "", "throttled\n")
            plan_path.write_text("# Plan\n\n### [x] Checkpoint 1: Test\n- [x] step one\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "done\n", "")

        execute_workflow(
            result,
            observer=observer,
            adapter=FakeAdapter(),
            runner=runner,
        )

        turn_finished_events = [e for e in observer.events if isinstance(e, TurnFinishedEvent)]
        self.assertTrue(turn_finished_events)
        self.assertIsNotNone(turn_finished_events[0].recovery)
        self.assertEqual(turn_finished_events[0].recovery.action, "retry_same_team_after_delay")
        run_completed_events = [e for e in observer.events if isinstance(e, RunCompletedEvent)]
        self.assertTrue(run_completed_events)
        self.assertEqual(run_completed_events[0].recovery_summary.action, "retry_same_team_after_delay")
        self.assertEqual(len(run_completed_events[0].recovery_history), 1)

    def test_collecting_observer_collects_events(self) -> None:
        """Test that CollectingObserver properly collects events."""
        from aflow.api import CollectingObserver, RunStartedEvent

        observer = CollectingObserver()
        event = RunStartedEvent.create(workflow_name="test")

        observer.on_event(event)

        self.assertEqual(len(observer.events), 1)
        self.assertIsInstance(observer.events[0], RunStartedEvent)
        self.assertEqual(observer.events[0].workflow_name, "test")

    def test_callback_observer_calls_callback(self) -> None:
        """Test that CallbackObserver calls the provided callback."""
        from aflow.api import CallbackObserver, RunStartedEvent

        collected = []

        def callback(event):
            collected.append(event)

        observer = CallbackObserver(callback)
        event = RunStartedEvent.create(workflow_name="test")

        observer.on_event(event)

        self.assertEqual(len(collected), 1)
        self.assertIsInstance(collected[0], RunStartedEvent)
        self.assertEqual(collected[0].workflow_name, "test")

    def test_manager_event_payload_contains_only_compact_artifact_metadata(self) -> None:
        from aflow.api import ManagerDecidedEvent

        event = ManagerDecidedEvent.create(
            decision_number=3,
            level="full",
            trigger="lite_escalation",
            action="upgrade_next_implementation",
            target_step="implement",
            target_team="strong",
            report_path=None,
            artifact_paths={"context": "manager/decision-003/context.json"},
        )

        payload = asdict(event)
        self.assertEqual(payload["event_type"].value, "manager_decided")
        self.assertEqual(payload["artifact_paths"]["context"], "manager/decision-003/context.json")
        self.assertNotIn("active_plan_content", json.dumps(payload, default=str))


class LibraryAnalyzeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home_dir = Path(self.temp_dir.name)
        self.repo_root = self.home_dir / "repo"
        self.repo_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_analyze_runs_explicit_run_id_surfaces_recovery_metadata(self) -> None:
        runs_root = self.repo_root / ".aflow" / "runs"
        run_dir = runs_root / "20260401T000100Z-fail"
        _write_json(
            run_dir / "run.json",
            {
                "status": "failed",
                "workflow_name": "test",
                "turns_completed": 1,
                "failure_reason": "harness failure",
                "recovery_summary": {
                    "action": "fail_immediately",
                    "consecutive_count": 1,
                    "delay_seconds": 0,
                    "executed": True,
                    "from_team": "team-a",
                    "matched_terms": ["throttled"],
                    "match_terms": ["throttled"],
                    "reason": "stop immediately",
                    "rejection_reason": None,
                    "source": "deterministic",
                    "suggested_action": None,
                    "suggested_keywords": [],
                    "to_team": "team-a",
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
                "recovery_action": "fail_immediately",
                "recovery_consecutive_count": 1,
                "recovery_delay_seconds": 0,
                "recovery_executed": True,
                "recovery_from_team": "team-a",
                "recovery_matched_terms": ["throttled"],
                "recovery_match_terms": ["throttled"],
                "recovery_reason": "stop immediately",
                "recovery_rejection_reason": None,
                "recovery_source": "deterministic",
                "recovery_suggested_action": None,
                "recovery_suggested_keywords": [],
                "recovery_to_team": "team-a",
            },
        )

        payload = analyze_runs(AnalyzeRequest(repo_root=self.repo_root, run_id="20260401T000100Z-fail"))

        self.assertEqual(payload["analysis_scope"]["selection"], "explicit_run_id")
        self.assertEqual(payload["run"]["recovery_summary"]["action"], "fail_immediately")
        self.assertEqual(payload["run"]["recovery_summary"]["source"], "deterministic")
        self.assertEqual(payload["run"]["focus_turns"][0]["recovery"]["action"], "fail_immediately")
        self.assertIn("harness_recovery_fail_immediately", payload["run"]["failure"]["signals"])
        self.assertIn("harness_recovery_present", payload["run"]["failure"]["signals"])

    def test_analyze_runs_last_run_fallback_uses_last_run_file(self) -> None:
        runs_root = self.repo_root / ".aflow" / "runs"
        run_dir = runs_root / "20260401T000200Z-last"
        _write_json(
            run_dir / "run.json",
            {
                "status": "completed",
                "workflow_name": "test",
                "turns_completed": 1,
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
        aflow_dir = self.repo_root / ".aflow"
        aflow_dir.mkdir(parents=True, exist_ok=True)
        (aflow_dir / "last_run_id").write_text("20260401T000200Z-last", encoding="utf-8")

        payload = analyze_runs(AnalyzeRequest(repo_root=self.repo_root))

        self.assertEqual(payload["analysis_scope"]["selection"], "last_run_id_file")
        self.assertEqual(payload["run"]["run_id"], "20260401T000200Z-last")

    def test_analyze_runs_corpus_mode_surfaces_recovery_counts(self) -> None:
        runs_root = self.repo_root / ".aflow" / "runs"

        run1 = runs_root / "20260401T000300Z-team-lead"
        _write_json(
            run1 / "run.json",
            {
                "status": "failed",
                "workflow_name": "test",
                "turns_completed": 1,
                "recovery_summary": {
                    "action": "switch_to_backup_team_and_retry",
                    "consecutive_count": 1,
                    "delay_seconds": 0,
                    "executed": True,
                    "from_team": "team-a",
                    "matched_terms": ["quota"],
                    "match_terms": ["quota"],
                    "reason": "switch teams",
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
            run1,
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
                "recovery_reason": "switch teams",
                "recovery_rejection_reason": None,
                "recovery_source": "team_lead",
                "recovery_suggested_action": "switch_to_backup_team_and_retry",
                "recovery_suggested_keywords": ["throttled", "quota"],
                "recovery_to_team": "team-b",
            },
        )

        run2 = runs_root / "20260401T000400Z-noise"
        _write_json(
            run2 / "run.json",
            {
                "status": "completed",
                "workflow_name": "other",
                "turns_completed": 0,
                "end_reason": "already_complete",
            },
        )

        payload = analyze_runs(AnalyzeRequest(repo_root=self.repo_root, all=True))

        self.assertEqual(payload["analysis_scope"]["mode"], "corpus")
        self.assertEqual(payload["analysis_scope"]["run_count_considered"], 1)
        self.assertEqual(payload["analysis_scope"]["run_count_skipped_as_noise"], 1)
        self.assertEqual(payload["runs"][0]["recovery_summary"]["source"], "team_lead")
        self.assertEqual(payload["runs"][0]["recovery_summary"]["suggested_keywords"], ["throttled", "quota"])
        self.assertIn("harness_recovery_team_lead", payload["signal_counts"])
        self.assertIn("harness_recovery_keyword_suggestions", payload["signal_counts"])

    def test_analyze_runs_rebuilds_selected_manager_context_without_plan_leak(self) -> None:
        run_dir = self.repo_root / ".aflow" / "runs" / "20260401T000500Z-manager"
        plan_path = self.repo_root / "plans" / "in-progress" / "plan.md"
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text(
            "# Confidential plan\n\n### [ ] Checkpoint 1: Context\n- [ ] private step\n",
            encoding="utf-8",
        )
        _write_json(
            run_dir / "run.json",
            {
                "status": "running", "workflow_name": "test", "turns_completed": 2,
                "plan_path": str(plan_path), "active_plan_path": str(plan_path),
                "original_plan_path": str(plan_path), "manager_decision_number": 1,
                "manager_history": [{
                    "decision_number": 1, "level": "lite", "trigger": "post_turn",
                    "action": "continue", "reason": "first pass",
                    "artifact_path": "manager/decision-001",
                }],
            },
        )
        _write_turn(
            run_dir, 1,
            result={"turn_number": 1, "step_name": "implement", "step_role": "implementer",
                    "status": "completed", "returncode": 0,
                    "snapshot_before": _snapshot(), "snapshot_after": _snapshot()},
            stdout="first semantic result",
        )
        _write_turn(
            run_dir, 2,
            result={"turn_number": 2, "step_name": "review", "step_role": "reviewer",
                    "status": "completed", "returncode": 0,
                    "snapshot_before": _snapshot(), "snapshot_after": _snapshot()},
            stdout="second semantic result",
        )

        context = analyze_runs(AnalyzeRequest(
            repo_root=self.repo_root, run_id=run_dir.name, manager_context="lite", turn=1,
        ))

        self.assertEqual(context["level"], "lite")
        self.assertEqual(context["finished_turn"]["turn_number"], 1)
        self.assertEqual(len(context["run_extract"]), 1)
        self.assertIsNone(context["active_plan_content"])
        self.assertNotIn("Confidential plan", json.dumps(context))

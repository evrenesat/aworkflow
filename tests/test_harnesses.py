from tests._support import *  # noqa: F401,F403
from dataclasses import replace
from typing import Mapping
from aflow.harnesses.base import HarnessInvocation
from aflow.harnesses.codex import CodexAdapter
from aflow.harnesses.preflight import (
    HarnessDiagnosticResult,
    HarnessEnvironmentBlocker,
    HarnessEnvironmentPreflight,
    HarnessPreflightContext,
    OSHarnessPreflightProbe,
    evaluate_harness_environment,
)
from aflow.harnesses.reasonix import ReasonixAdapter

class AdaptersTests(unittest.TestCase):

    def test_reasonix_without_effort(self) -> None:
        adapter = ReasonixAdapter()
        invocation = adapter.build_invocation(
            repo_root=Path('/repo'),
            model='deepseek-pro',
            system_prompt='SYSTEM',
            user_prompt='USER',
        )
        assert invocation.argv == (
            'reasonix',
            'run',
            '--dir',
            '/repo',
            '--model',
            'deepseek-pro',
        )
        assert '-dir' not in invocation.argv
        assert adapter.supports_effort
        assert invocation.prompt_mode == 'stdin'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'
        assert invocation.stdin_text == invocation.effective_prompt
        final_invocation = invocation.for_final_output()
        assert '--print' not in invocation.argv
        assert final_invocation.argv == (
            'reasonix',
            'run',
            '--dir',
            '/repo',
            '--model',
            'deepseek-pro',
            '--print',
        )
        # The prompt is never an argv element in either form.
        assert 'SYSTEM\n\nUSER' not in final_invocation.argv
        assert final_invocation.stdin_text == invocation.effective_prompt

    def test_reasonix_without_model_and_with_effort(self) -> None:
        adapter = ReasonixAdapter()
        invocation = adapter.build_invocation(
            repo_root=Path('/repo'),
            model=None,
            system_prompt='SYSTEM',
            user_prompt='USER',
            effort='high',
        )
        assert invocation.argv == (
            'reasonix', 'run', '--dir', '/repo', '--effort', 'high'
        )
        assert '-dir' not in invocation.argv
        assert invocation.argv[invocation.argv.index('--effort') + 1] == 'high'
        assert '--model' not in invocation.argv
        assert invocation.for_final_output().argv == (
            'reasonix', 'run', '--dir', '/repo', '--effort', 'high', '--print'
        )
        assert invocation.stdin_text == 'SYSTEM\n\nUSER'

    def test_reasonix_flash_max_effort_is_forwarded(self) -> None:
        adapter = ReasonixAdapter()
        invocation = adapter.build_invocation(
            repo_root=Path('/repo'),
            model='deepseek-flash',
            system_prompt='SYSTEM',
            user_prompt='USER',
            effort='max',
        )
        assert invocation.argv == (
            'reasonix', 'run', '--dir', '/repo', '--model', 'deepseek-flash',
            '--effort', 'max'
        )
        assert invocation.stdin_text == 'SYSTEM\n\nUSER'
        assert invocation.prompt_mode == 'stdin'

    def test_codex_without_effort(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='SYSTEM', user_prompt='USER')
        assert invocation.argv == ('codex', 'exec', '--dangerously-bypass-approvals-and-sandbox', '-C', '/repo', '--model', 'gpt-5.4', '-')
        assert invocation.prompt_mode == 'stdin'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'
        assert invocation.stdin_text == invocation.effective_prompt
        assert invocation.for_final_output().stdin_text == invocation.effective_prompt
        final_invocation = replace(
            invocation,
            final_output_argv=(*invocation.argv, '--final-output'),
        ).for_final_output()
        assert final_invocation.argv[-1] == '--final-output'
        assert final_invocation.stdin_text == invocation.effective_prompt

    def test_codex_with_effort(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        argv = invocation.argv
        assert '-c' in argv
        assert 'model_reasoning_effort=\'high\'' in argv
        effort_index = argv.index('model_reasoning_effort=\'high\'')
        assert argv[effort_index - 1] == '-c'
        assert argv[-1] == '-'
        assert invocation.stdin_text == 'SYSTEM\n\nUSER'

    def test_codex_effort_preserves_prompt_as_stdin(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='PROMPT', user_prompt='INSTRUCTIONS', effort='low')
        assert invocation.argv[-1] == '-'
        assert invocation.stdin_text == 'PROMPT\n\nINSTRUCTIONS'

    def test_codex_without_model_omits_model_flag(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv[-1] == '-'
        assert invocation.stdin_text == 'SYSTEM\n\nUSER'

    def test_copilot_without_effort(self) -> None:
        adapter = CopilotAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='SYSTEM', user_prompt='USER')
        assert invocation.argv == ('copilot', '-p', 'SYSTEM\n\nUSER', '-s', '--allow-all', '--no-ask-user', '--model', 'gpt-5.4')
        assert invocation.prompt_mode == 'prefix-system-into-user-prompt'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'

    def test_copilot_with_effort(self) -> None:
        adapter = CopilotAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        argv = invocation.argv
        assert '--reasoning-effort' in argv
        assert 'high' in argv
        assert argv[-2:] == ('--reasoning-effort', 'high')

    def test_copilot_without_model_omits_model_flag(self) -> None:
        adapter = CopilotAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv[:6] == ('copilot', '-p', 'SYSTEM\n\nUSER', '-s', '--allow-all', '--no-ask-user')

    def test_copilot_without_model_and_with_effort_uses_reasoning_effort_flag(self) -> None:
        adapter = CopilotAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER', effort='low')
        argv = invocation.argv
        assert '--model' not in argv
        assert '--reasoning-effort' in argv
        assert argv[-2:] == ('--reasoning-effort', 'low')

    def test_pi_without_effort(self) -> None:
        adapter = PiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='sonnet', system_prompt='SYSTEM', user_prompt='USER')
        assert invocation.argv == ('pi', '--print', '--system-prompt', 'SYSTEM', '--model', 'sonnet', '--tools', 'read,bash,edit,write,grep,find,ls', 'USER')
        assert invocation.prompt_mode == 'system-prompt-flag'

    def test_pi_with_effort(self) -> None:
        adapter = PiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='sonnet', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        argv = invocation.argv
        assert '--models' in argv
        assert 'sonnet:high' in argv
        assert '--model' not in argv
        models_index = argv.index('--models')
        assert argv[models_index + 1] == 'sonnet:high'

    def test_pi_with_effort_does_not_pass_both_model_flags(self) -> None:
        adapter = PiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='sonnet', system_prompt='S', user_prompt='U', effort='high')
        assert '--models' in invocation.argv
        assert '--model' not in invocation.argv

    def test_pi_without_model_and_with_effort_uses_thinking_flag(self) -> None:
        adapter = PiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER', effort='high')
        argv = invocation.argv
        assert '--thinking' in argv
        assert 'high' in argv
        assert '--models' not in argv
        assert '--model' not in argv

    def test_pi_without_model_and_without_effort_omits_model_flags(self) -> None:
        adapter = PiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert '--models' not in invocation.argv
        assert '--thinking' not in invocation.argv

    def test_claude_without_effort(self) -> None:
        adapter = ClaudeAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='claude-sonnet-4-6', system_prompt='SYSTEM', user_prompt='USER')
        assert '--effort' not in invocation.argv
        assert invocation.argv == ('claude', '-p', '--system-prompt', 'SYSTEM', '--model', 'claude-sonnet-4-6', '--permission-mode', 'bypassPermissions', '--dangerously-skip-permissions', '--tools=default', 'USER')

    def test_claude_with_effort(self) -> None:
        adapter = ClaudeAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='claude-sonnet-4-6', system_prompt='SYSTEM', user_prompt='USER', effort='low')
        argv = invocation.argv
        assert '--effort' in argv
        assert 'low' in argv
        effort_index = argv.index('--effort')
        assert argv[effort_index + 1] == 'low'

    def test_claude_without_model_omits_model_flag(self) -> None:
        adapter = ClaudeAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv[0] == 'claude'

    def test_opencode_without_effort(self) -> None:
        adapter = OpencodeAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='glm-5-turbo', system_prompt='SYSTEM', user_prompt='USER')
        assert invocation.argv == ('opencode', 'run', '--model', 'glm-5-turbo', '--format', 'default', '--dir', '/repo', 'SYSTEM\n\nUSER')
        assert invocation.prompt_mode == 'prefix-system-into-user-prompt'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'

    def test_opencode_with_effort_ignores_effort(self) -> None:
        adapter = OpencodeAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='glm-5-turbo', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        assert not adapter.supports_effort
        argv = invocation.argv
        assert 'effort' not in ' '.join(argv).lower()
        assert argv == ('opencode', 'run', '--model', 'glm-5-turbo', '--format', 'default', '--dir', '/repo', 'SYSTEM\n\nUSER')

    def test_opencode_without_model_omits_model_flag(self) -> None:
        adapter = OpencodeAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv[0] == 'opencode'

    def test_gemini_without_effort(self) -> None:
        adapter = GeminiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gemini-2.5-pro', system_prompt='SYSTEM', user_prompt='USER')
        assert invocation.argv == ('gemini', '--prompt', 'SYSTEM\n\nUSER', '--model', 'gemini-2.5-pro', '--approval-mode', 'yolo', '--sandbox=false', '--output-format', 'text')
        assert invocation.prompt_mode == 'prefix-system-into-user-prompt'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'

    def test_gemini_with_effort_ignores_effort(self) -> None:
        adapter = GeminiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gemini-2.5-pro', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        assert not adapter.supports_effort
        argv = invocation.argv
        assert 'effort' not in ' '.join(argv).lower()
        assert argv == ('gemini', '--prompt', 'SYSTEM\n\nUSER', '--model', 'gemini-2.5-pro', '--approval-mode', 'yolo', '--sandbox=false', '--output-format', 'text')

    def test_gemini_without_model_omits_model_flag(self) -> None:
        adapter = GeminiAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv[0] == 'gemini'

    def test_kiro_without_effort(self) -> None:
        adapter = KiroAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='kiro-model', system_prompt='SYSTEM', user_prompt='USER')
        assert not adapter.supports_effort
        assert invocation.argv == ('kiro-cli', 'chat', '--no-interactive', '--trust-all-tools', '--model', 'kiro-model', 'SYSTEM\n\nUSER')
        assert invocation.prompt_mode == 'prefix-system-into-user-prompt'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'

    def test_kiro_without_model_omits_model_flag(self) -> None:
        adapter = KiroAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv == ('kiro-cli', 'chat', '--no-interactive', '--trust-all-tools', 'SYSTEM\n\nUSER')

    def test_kiro_ignores_effort(self) -> None:
        adapter = KiroAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='kiro-model', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        assert not adapter.supports_effort
        assert 'effort' not in ' '.join(invocation.argv).lower()
        assert invocation.argv == ('kiro-cli', 'chat', '--no-interactive', '--trust-all-tools', '--model', 'kiro-model', 'SYSTEM\n\nUSER')

    def test_muse_without_effort(self) -> None:
        adapter = MuseAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='meta-llama', system_prompt='SYSTEM', user_prompt='USER')
        assert adapter.supports_effort
        assert invocation.argv == ('muse', 'exec', '--yolo', '--workspace', '/repo', '--model', 'meta-llama', 'SYSTEM\n\nUSER')
        assert invocation.prompt_mode == 'prefix-system-into-user-prompt'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'
        assert invocation.stdin_text is None

    def test_muse_with_effort(self) -> None:
        adapter = MuseAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='meta-llama', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        argv = invocation.argv
        assert '--reasoning-effort' in argv
        assert argv[argv.index('--reasoning-effort') + 1] == 'high'
        assert argv[-1] == 'SYSTEM\n\nUSER'

    def test_muse_without_model_omits_model_flag(self) -> None:
        adapter = MuseAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv == ('muse', 'exec', '--yolo', '--workspace', '/repo', 'SYSTEM\n\nUSER')

    def test_muse_without_model_and_with_effort_uses_reasoning_effort_flag(self) -> None:
        adapter = MuseAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER', effort='low')
        argv = invocation.argv
        assert '--model' not in argv
        assert '--reasoning-effort' in argv
        assert argv[argv.index('--reasoning-effort') + 1] == 'low'
        assert argv[-1] == 'SYSTEM\n\nUSER'


class RetentionTests(unittest.TestCase):

    def test_retention_prune_old_runs_keeps_newest_twenty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_root = Path(tmpdir)
            for index in range(23):
                run_dir = runs_root / f'20260329T120000Z-{22 - index:08x}'
                run_dir.mkdir()
                mtime_ns = 1700000000000000000 + index * 1000000
                os.utime(run_dir, ns=(mtime_ns, mtime_ns))
            prune_old_runs(runs_root, keep_runs=20)
            remaining = sorted((path.name for path in runs_root.iterdir()))
            assert len(remaining) == 20
            assert remaining == sorted((f'20260329T120000Z-{22 - index:08x}' for index in range(3, 23)))


class GitStatusTests(unittest.TestCase):

    def setUp(self) -> None:
        from aflow.git_status import capture_baseline, probe_worktree, summarize_since_baseline
        self._capture_baseline = capture_baseline
        self._probe_worktree = probe_worktree
        self._summarize_since_baseline = summarize_since_baseline

    def test_probe_worktree_clean_returns_not_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            result = self._probe_worktree(repo)
            assert result is not None
            assert result.is_dirty is False
            assert result.modified_count == 0
            assert result.added_count == 0
            assert result.removed_count == 0

    def test_probe_worktree_modified_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            (repo / "README.md").write_text("changed\n", encoding="utf-8")
            result = self._probe_worktree(repo)
            assert result is not None
            assert result.is_dirty is True
            assert result.modified_count == 1

    def test_probe_worktree_added_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
            result = self._probe_worktree(repo)
            assert result is not None
            assert result.is_dirty is True
            assert result.added_count >= 1

    def test_capture_baseline_returns_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            assert baseline.head_sha is not None
            assert len(baseline.tree_oid) == 40

    def test_summarize_clean_baseline_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            summary = self._summarize_since_baseline(repo, baseline)
            assert summary is not None
            assert summary.modified_count == 0
            assert summary.added_count == 0
            assert summary.removed_count == 0
            assert summary.lines_added == 0
            assert summary.lines_removed == 0
            assert summary.commit_count == 0
            assert summary.changed_paths == ()

    def test_summarize_modified_file_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            (repo / "README.md").write_text("line1\nline2\n", encoding="utf-8")
            summary = self._summarize_since_baseline(repo, baseline)
            assert summary is not None
            assert summary.modified_count == 1
            assert summary.added_count == 0
            assert summary.removed_count == 0
            assert "README.md" in summary.changed_paths

    def test_summarize_added_file_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
            summary = self._summarize_since_baseline(repo, baseline)
            assert summary is not None
            assert summary.added_count == 1
            assert summary.lines_added >= 1
            assert "new.py" in summary.changed_paths

    def test_summarize_deleted_file_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            (repo / "README.md").unlink()
            summary = self._summarize_since_baseline(repo, baseline)
            assert summary is not None
            assert summary.removed_count == 1

    def test_summarize_commit_after_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add new"], check=True, capture_output=True)
            summary = self._summarize_since_baseline(repo, baseline)
            assert summary is not None
            assert summary.commit_count == 1

    def test_summarize_dirty_at_start_reports_only_post_baseline_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            (repo / "pre.py").write_text("pre = 1\n", encoding="utf-8")
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            (repo / "post.py").write_text("post = 1\n", encoding="utf-8")
            summary = self._summarize_since_baseline(repo, baseline)
            assert summary is not None
            assert "post.py" in summary.changed_paths
            assert "pre.py" not in summary.changed_paths

    def test_summarize_returns_to_baseline_shows_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            original_content = (repo / "README.md").read_text(encoding="utf-8")
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            (repo / "README.md").write_text("changed\n", encoding="utf-8")
            summary1 = self._summarize_since_baseline(repo, baseline)
            assert summary1 is not None
            assert summary1.modified_count == 1
            (repo / "README.md").write_text(original_content, encoding="utf-8")
            summary2 = self._summarize_since_baseline(repo, baseline)
            assert summary2 is not None
            assert summary2.modified_count == 0
            assert summary2.changed_paths == ()

    def test_capture_baseline_no_commits_returns_none_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
            (repo / "f.txt").write_text("x\n", encoding="utf-8")
            baseline = self._capture_baseline(repo)
            assert baseline is not None
            assert baseline.head_sha is None

    def test_probe_returns_none_outside_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            result = self._probe_worktree(repo)
            assert result is None

    def test_classify_dirtiness_all_under_plans(self) -> None:
        from aflow.git_status import classify_dirtiness_by_prefix
        porcelain = "?? plans/a.txt\nM  plans/b.txt\nA  plans/c.txt\n"
        plan_paths, non_plan_paths = classify_dirtiness_by_prefix(porcelain)
        assert len(plan_paths) == 3
        assert len(non_plan_paths) == 0
        assert "plans/a.txt" in plan_paths
        assert "plans/b.txt" in plan_paths
        assert "plans/c.txt" in plan_paths

    def test_classify_dirtiness_all_outside_plans(self) -> None:
        from aflow.git_status import classify_dirtiness_by_prefix
        porcelain = "?? src/a.txt\nM  aflow/b.txt\nA  tests/c.txt\n"
        plan_paths, non_plan_paths = classify_dirtiness_by_prefix(porcelain)
        assert len(plan_paths) == 0
        assert len(non_plan_paths) == 3
        assert "src/a.txt" in non_plan_paths
        assert "aflow/b.txt" in non_plan_paths
        assert "tests/c.txt" in non_plan_paths

    def test_classify_dirtiness_mixed(self) -> None:
        from aflow.git_status import classify_dirtiness_by_prefix
        porcelain = "?? plans/a.txt\nM  src/b.txt\nA  plans/c.txt\nD  aflow/d.txt\n"
        plan_paths, non_plan_paths = classify_dirtiness_by_prefix(porcelain)
        assert len(plan_paths) == 2
        assert len(non_plan_paths) == 2
        assert "plans/a.txt" in plan_paths
        assert "plans/c.txt" in plan_paths
        assert "src/b.txt" in non_plan_paths
        assert "aflow/d.txt" in non_plan_paths

    def test_classify_dirtiness_rejects_similar_prefixes(self) -> None:
        from aflow.git_status import classify_dirtiness_by_prefix
        porcelain = "?? plans_backup/a.txt\nM  my-plans/b.txt\nA  xplans/c.txt\n"
        plan_paths, non_plan_paths = classify_dirtiness_by_prefix(porcelain)
        assert len(plan_paths) == 0
        assert len(non_plan_paths) == 3
        assert "plans_backup/a.txt" in non_plan_paths
        assert "my-plans/b.txt" in non_plan_paths
        assert "xplans/c.txt" in non_plan_paths

    def test_classify_dirtiness_empty_porcelain(self) -> None:
        from aflow.git_status import classify_dirtiness_by_prefix
        porcelain = ""
        plan_paths, non_plan_paths = classify_dirtiness_by_prefix(porcelain)
        assert len(plan_paths) == 0
        assert len(non_plan_paths) == 0


class RepoStateProbeTests(unittest.TestCase):

    def setUp(self) -> None:
        from aflow.git_status import probe_repo_state, RepoState
        self._probe_repo_state = probe_repo_state
        self._RepoState = RepoState

    def test_probe_repo_state_not_a_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            result = self._probe_repo_state(repo)
            assert result == self._RepoState.NOT_A_REPO

    def test_probe_repo_state_unborn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            subprocess.run(
                ['git', 'init', '-b', 'main'], cwd=str(repo), check=True, capture_output=True
            )
            result = self._probe_repo_state(repo)
            assert result == self._RepoState.UNBORN

    def test_probe_repo_state_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _make_git_repo(repo)
            result = self._probe_repo_state(repo)
            assert result == self._RepoState.READY

    def test_probe_repo_state_no_git_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            with patch('aflow.git_status.shutil.which', return_value=None):
                result = self._probe_repo_state(repo)
            assert result == self._RepoState.NO_GIT_BINARY

    def test_probe_repo_state_file_not_found_is_no_git_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            with patch('subprocess.run', side_effect=FileNotFoundError):
                result = self._probe_repo_state(repo)
            assert result == self._RepoState.NO_GIT_BINARY

    def test_preflight_still_fails_when_committed_repo_main_branch_missing(self) -> None:
        """Committed repos with a missing main_branch must still fail after the split."""
        from aflow.workflow import run_workflow, WorkflowError
        from aflow.run_state import ControllerConfig
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_branch_only_wf_config(main_branch='nonexistent')
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                    wf_config, 'branch_wf', config_dir=repo_root,
                )
            assert 'nonexistent' in str(ctx.value)


class PlainStatusOutputTests(unittest.TestCase):
    """Plain append-only status records and ASCII show output."""

    @staticmethod
    def _step(role: str, *targets: str):
        from aflow.config import GoTransition, WorkflowStepConfig

        return WorkflowStepConfig(role=role, go=tuple(GoTransition(to=t) for t in targets))

    def _renderer(self, stream, **kwargs):
        from aflow.status import BannerRenderer

        defaults = {
            "config_max_turns": 5,
            "config_plan_path": Path("plans/demo.md"),
        }
        defaults.update(kwargs)
        return BannerRenderer(stream=stream, **defaults)

    @staticmethod
    def _records(stream):
        return [line for line in stream.getvalue().splitlines() if line.startswith("aflow ")]

    def test_records_include_git_rows_and_respect_files_limit(self) -> None:
        import aflow.git_status as git_status_mod
        from aflow.git_status import GitSummary

        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        state.run_id = "run-git"
        summary = GitSummary(
            modified_count=1,
            added_count=1,
            removed_count=0,
            lines_added=12,
            lines_removed=3,
            commit_count=2,
            changed_paths=tuple(f"src/file{i}.py" for i in range(12)),
        )
        stream = io.StringIO()
        renderer = self._renderer(
            stream, repo_root=Path("/repo"), workflow_name="managed",
            config_banner_files_limit=10,
        )
        with patch.object(git_status_mod, "capture_baseline", return_value=object()), \
             patch.object(git_status_mod, "summarize_since_baseline", return_value=summary):
            renderer.start(state)

        record = self._records(stream)[0]
        assert 'git="M 1, A 1, D 0 | +12/-3 | 2 commits"' in record
        files_value = record.split("files=", 1)[1].split(" status=", 1)[0]
        assert "src/file9.py" in files_value
        assert "src/file10.py" not in files_value
        assert "+2 more" in files_value

    def test_records_show_clean_git_state_without_files_row(self) -> None:
        import aflow.git_status as git_status_mod
        from aflow.git_status import GitSummary

        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        summary = GitSummary(
            modified_count=0,
            added_count=0,
            removed_count=0,
            lines_added=0,
            lines_removed=0,
            commit_count=0,
            changed_paths=(),
        )
        stream = io.StringIO()
        renderer = self._renderer(stream, repo_root=Path("/repo"))
        with patch.object(git_status_mod, "capture_baseline", return_value=object()), \
             patch.object(git_status_mod, "summarize_since_baseline", return_value=summary):
            renderer.start(state)

        record = self._records(stream)[0]
        assert 'git="clean since start | +0/-0 | 0 commits"' in record
        assert "files=" not in record

    def test_git_probe_failure_still_emits_records(self) -> None:
        import aflow.git_status as git_status_mod

        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        state.run_id = "run-git"
        stream = io.StringIO()
        renderer = self._renderer(stream, repo_root=Path("/repo"))
        with patch.object(git_status_mod, "capture_baseline", side_effect=OSError("no git")), \
             patch.object(git_status_mod, "summarize_since_baseline", side_effect=OSError("no git")):
            renderer.start(state)
            renderer.stop(state)

        records = self._records(stream)
        assert len(records) == 2
        assert "git=" not in records[0]
        assert "event=final" in records[1]

    def test_records_carry_run_lineage_and_skipped_step_words(self) -> None:
        from aflow.status import WorkflowGraphSource

        source = WorkflowGraphSource(
            declared_steps={
                "plan": self._step("planner", "implement"),
                "implement": self._step("worker", "review"),
                "review": self._step("reviewer", "END"),
            },
            executable_steps={
                "implement": self._step("worker", "review"),
                "review": self._step("reviewer", "END"),
            },
            excluded_step_names=("plan",),
        )
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False, 3, 1))
        state.run_id = "run-lineage"
        state.resumed_from_run_id = "run-source"
        state.selected_start_step = "review"
        stream = io.StringIO()
        renderer = self._renderer(
            stream,
            workflow_name="managed",
            workflow_graph_source=source,
        )
        renderer.start(state)

        record = self._records(stream)[0]
        assert "run=run-lineage" in record
        assert "resumed_from=run-source" in record
        assert "start_step=review" in record
        assert "skipped=implement" in record
        assert "skipped=plan" not in record

    def test_turn_finalization_records_transition_words(self) -> None:
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        state.turn_history.append(TurnRecord(
            turn_number=2,
            step_name="implement",
            resolved_harness_name="claude",
            resolved_model_display="claude / opus",
            chosen_transition="review",
            chosen_transition_condition="tests_pass",
            outcome="completed",
        ))
        state.turn_history.append(TurnRecord(
            turn_number=3,
            step_name="review",
            resolved_harness_name="claude",
            resolved_model_display="claude / opus",
            chosen_transition="END",
            outcome="completed",
        ))
        stream = io.StringIO()
        renderer = self._renderer(stream, workflow_name="managed")
        renderer.update(state)

        record = self._records(stream)[0]
        assert "step=review" in record
        assert "transition=END" in record
        assert "outcome=completed" in record

    def test_set_context_feeds_next_record_without_emitting(self) -> None:
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        stream = io.StringIO()
        renderer = self._renderer(stream, workflow_name="managed")
        renderer.set_context(
            current_step_name="implement",
            active_plan_path=Path("plans/in-progress/demo.md"),
            config_harness="claude",
        )
        assert self._records(stream) == []
        renderer.update(state)

        record = self._records(stream)[0]
        assert "step=implement" in record
        assert "active_plan=plans/in-progress/demo.md" in record
        assert "harness=claude" in record

    def test_workflow_show_renders_end_transitions_and_excluded_words(self) -> None:
        import aflow.status as status_mod
        from aflow.config import TeamConfig, WorkflowConfig, WorkflowUserConfig

        active = self._step("worker", "review", "END")
        excluded = self._step("reviewer", "END")
        workflow = WorkflowConfig(
            declared_steps={"implement": active, "review": excluded},
            steps={"implement": active},
            excluded_steps=("review",),
        )
        config = WorkflowUserConfig(
            roles={"worker": "codex.default", "reviewer": "claude.opus"},
            teams={"base": TeamConfig(roles={"worker": "codex.default"})},
            workflows={"managed": workflow},
        )
        output = status_mod.build_workflow_show(config=config)

        assert "step implement [executable] role=worker" in output
        assert "step review [excluded] role=reviewer" in output
        assert "go -> review" in output
        assert "go -> END [terminal]" in output
        assert "\x1b" not in output
        assert not any(True for ch in output if ord(ch) < 0x20 and ch != "\n")

    def test_workflow_show_lists_shared_roles_and_every_workflow_in_order(self) -> None:
        import aflow.status as status_mod
        from aflow.config import WorkflowConfig, WorkflowUserConfig

        first = self._step("worker", "END")
        second = self._step("architect", "END")
        config = WorkflowUserConfig(
            roles={"worker": "codex.default", "architect": "claude.opus"},
            teams={
                "zteam": TeamConfig(roles={"worker": "codex.default"}),
                "ateam": TeamConfig(roles={"architect": "claude.opus"}),
            },
            workflows={"alpha": WorkflowConfig(
                declared_steps={"work": first}, steps={"work": first},
            )},
        )
        config.workflows["beta"] = WorkflowConfig(
            declared_steps={"ship": second}, steps={"ship": second},
        )
        output = status_mod.build_workflow_show(config=config)

        roles_index = output.index("Roles / Teams")
        alpha_index = output.index("workflow alpha")
        beta_index = output.index("workflow beta")
        assert roles_index < alpha_index < beta_index
        assert "role worker -> codex.default" in output
        assert "role architect -> claude.opus" in output
        assert "team ateam: architect -> claude.opus" in output
        assert "team zteam: worker -> codex.default" in output
        assert "workflow alpha" in output
        assert "workflow beta" in output

    def test_skipped_step_names_respect_exclusions_and_selected_start(self) -> None:
        import aflow.status as status_mod
        from aflow.status import WorkflowGraphSource

        source = WorkflowGraphSource(
            declared_steps={
                "plan": self._step("planner", "implement"),
                "implement": self._step("worker", "review"),
                "review": self._step("reviewer", "END"),
                "extra": self._step("worker", "END"),
            },
            executable_steps={
                "implement": self._step("worker", "review"),
                "review": self._step("reviewer", "END"),
            },
            excluded_step_names=("extra",),
        )
        skipped = status_mod._visual_start_skipped_step_names(
            declared_steps=source.declared_steps,
            executable_steps=source.executable_steps,
            excluded_step_names=source.excluded_step_names,
            selected_start_step="review",
        )
        assert skipped == ("implement",)
        assert status_mod._visual_start_skipped_step_names(
            declared_steps=source.declared_steps,
            executable_steps=source.executable_steps,
            excluded_step_names=source.excluded_step_names,
            selected_start_step=None,
        ) == ()


class _FakePreflightProbe:
    def __init__(
        self,
        *,
        primary: bool = True,
        bwrap: bool = False,
        diagnostic: object = None,
    ) -> None:
        self.primary = primary
        self.bwrap = bwrap
        self.diagnostic = diagnostic
        self.diagnostic_calls: list[tuple[str, ...]] = []

    def resolve_executable(self, command: str, *, env: Mapping[str, str]) -> str | None:
        if command == "bwrap":
            return "/usr/bin/bwrap" if self.bwrap else None
        return f"/fake/{command}" if self.primary else None

    def run_diagnostic(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> object:
        self.diagnostic_calls.append(argv)
        return self.diagnostic


def _preflight_invocation(label: str, command: str) -> HarnessInvocation:
    return HarnessInvocation(
        label=label,
        argv=(command,),
        env={"PATH": "/fake/bin", "TOKEN": "secret"},
        prompt_mode="",
        system_prompt="SYSTEM",
        user_prompt="USER",
        effective_prompt="SYSTEM\n\nUSER",
    )


def _preflight_context(invocation: HarnessInvocation) -> HarnessPreflightContext:
    return HarnessPreflightContext(
        invocation_kind="workflow_turn",
        cwd=Path("/repo"),
        env={**os.environ, **invocation.env},
        invocation=invocation,
    )


class PreflightTests(unittest.TestCase):
    def test_result_contract_requires_matching_blocker(self) -> None:
        with self.assertRaises(ValueError):
            HarnessEnvironmentPreflight("blocked")
        with self.assertRaises(ValueError):
            HarnessEnvironmentPreflight(
                "ready",
                HarnessEnvironmentBlocker(
                    "harness_environment_preflight", "x", "codex", "codex",
                    ("codex",), "remediate", {},
                ),
            )

    def test_missing_primary_executable_is_provider_neutral(self) -> None:
        for adapter, command in ((CodexAdapter(), "codex"), (ReasonixAdapter(), "reasonix")):
            with self.subTest(command=command):
                probe = _FakePreflightProbe(primary=False)
                result = evaluate_harness_environment(
                    _preflight_context(_preflight_invocation(command, command)),
                    adapter,
                    probe,
                )
                self.assertEqual(result.status, "blocked")
                assert result.blocker is not None
                self.assertEqual(result.blocker.reason_code, "harness_executable_missing")
                self.assertEqual(result.blocker.required_executable, command)
                self.assertEqual(probe.diagnostic_calls, [])

    def test_build_only_adapter_has_no_optional_capability(self) -> None:
        adapter = type("BuildOnlyAdapter", (), {"name": "custom", "supports_effort": False})()
        result = evaluate_harness_environment(
            _preflight_context(_preflight_invocation("custom", "custom")),
            adapter,
            _FakePreflightProbe(),
        )
        self.assertEqual(result.status, "ready")

    def test_reasonix_enforced_sandbox_requires_bwrap(self) -> None:
        probe = _FakePreflightProbe(
            diagnostic=HarnessDiagnosticResult(
                0, '{"sandbox":{"bash":"enforce"},"config":{"token":"secret"}}'
            )
        )
        result = evaluate_harness_environment(
            _preflight_context(_preflight_invocation("reasonix", "reasonix")),
            ReasonixAdapter(),
            probe,
        )
        self.assertEqual(result.status, "blocked")
        assert result.blocker is not None
        self.assertEqual(result.blocker.reason_code, "reasonix_sandbox_bwrap_missing")
        self.assertEqual(result.blocker.required_executable, "bwrap")
        self.assertEqual(result.blocker.checked_command, ("reasonix", "doctor", "--json"))
        self.assertNotIn("secret", repr(result.blocker))
        self.assertEqual(probe.diagnostic_calls, [("/fake/reasonix", "doctor", "--json")])

    def test_reasonix_non_enforced_or_present_bwrap_is_ready(self) -> None:
        for payload, bwrap in (
            ('{"sandbox":{"bash":"off"}}', False),
            ('{"sandbox":{"bash":"enforce"}}', True),
        ):
            with self.subTest(payload=payload, bwrap=bwrap):
                result = evaluate_harness_environment(
                    _preflight_context(_preflight_invocation("reasonix", "reasonix")),
                    ReasonixAdapter(),
                    _FakePreflightProbe(
                        bwrap=bwrap, diagnostic=HarnessDiagnosticResult(0, payload)
                    ),
                )
                self.assertEqual(result.status, "ready")

    def test_reasonix_diagnostic_failures_are_compatible_ready(self) -> None:
        for diagnostic in (
            None,
            HarnessDiagnosticResult(2, '{"sandbox":{"bash":"enforce"}}'),
            HarnessDiagnosticResult(0, "not-json"),
            HarnessDiagnosticResult(None, timed_out=True),
            HarnessDiagnosticResult(0, "[]"),
        ):
            with self.subTest(diagnostic=diagnostic):
                result = evaluate_harness_environment(
                    _preflight_context(_preflight_invocation("reasonix", "reasonix")),
                    ReasonixAdapter(),
                    _FakePreflightProbe(diagnostic=diagnostic),
                )
                self.assertEqual(result.status, "ready")

    def test_adapter_blocker_is_secret_safe(self) -> None:
        class BlockedAdapter:
            name = "custom"
            supports_effort = False

            def preflight_environment(self, context: object, probe: object) -> HarnessEnvironmentBlocker:
                return HarnessEnvironmentBlocker(
                    "harness_environment_preflight", "custom_blocker",
                    "/private/provider", "/private/bin/tool",
                    ("/private/bin/tool", "--check"), "fixed remediation",
                    {"path": "/private/config", "safe": "yes"},
                )

        result = evaluate_harness_environment(
            _preflight_context(_preflight_invocation("custom", "custom")),
            BlockedAdapter(),
            _FakePreflightProbe(),
        )
        self.assertEqual(result.status, "blocked")
        assert result.blocker is not None
        self.assertEqual(result.blocker.harness, "provider")
        self.assertEqual(result.blocker.required_executable, "tool")
        self.assertEqual(result.blocker.checked_command, ("tool", "--check"))
        self.assertNotIn("path", result.blocker.safe_diagnostics)
        self.assertEqual(result.blocker.safe_diagnostics["safe"], "yes")

    def test_os_probe_bounds_timeout(self) -> None:
        with patch(
            "aflow.harnesses.preflight.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["reasonix"], 5),
        ):
            result = OSHarnessPreflightProbe().run_diagnostic(
                ("reasonix", "doctor", "--json"),
                cwd=Path("/repo"),
                env={"PATH": "/fake"},
                timeout_seconds=30,
            )
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)

    def test_reasonix_oversized_prompt_never_touches_argv(self) -> None:
        adapter = ReasonixAdapter()
        # Far beyond Linux's per-argument execve limit (128 KiB): the prompt
        # must travel on stdin so execve can never fail with E2BIG.
        prompt = "E2BIG-PROMPT-SENTINEL-" + ("p" * 200_000)
        invocation = adapter.build_invocation(
            repo_root=Path('/repo'),
            model='deepseek-pro',
            system_prompt='SYSTEM',
            user_prompt=prompt,
        )
        argv_bytes = sum(len(argument.encode('utf-8')) for argument in invocation.argv)
        assert invocation.prompt_mode == 'stdin'
        assert invocation.stdin_text == 'SYSTEM\n\n' + prompt
        assert argv_bytes < 1_024
        assert all('E2BIG-PROMPT-SENTINEL' not in argument for argument in invocation.argv)
        final = invocation.for_final_output()
        assert final.argv == (*invocation.argv, '--print')
        assert final.stdin_text == invocation.stdin_text

    def test_reasonix_declares_manager_workspace_read(self) -> None:
        from aflow.harnesses.base import adapter_manager_workspace_read
        assert ReasonixAdapter().manager_workspace_read is True
        assert adapter_manager_workspace_read(ReasonixAdapter()) is True

    def test_manager_workspace_read_is_fail_closed_for_unknown_adapters(self) -> None:
        from aflow.harnesses.base import adapter_manager_workspace_read

        class LegacyAdapter:
            name = 'legacy'
            supports_effort = False

            def build_invocation(self, **kwargs):  # pragma: no cover
                raise NotImplementedError

        assert adapter_manager_workspace_read(LegacyAdapter()) is False
        assert adapter_manager_workspace_read(object()) is False

    def test_coding_adapters_advertise_manager_workspace_read(self) -> None:
        from aflow.harnesses.base import adapter_manager_workspace_read
        from aflow.harnesses.claude import ClaudeAdapter
        from aflow.harnesses.copilot import CopilotAdapter
        from aflow.harnesses.gemini import GeminiAdapter
        from aflow.harnesses.kiro import KiroAdapter
        from aflow.harnesses.muse import MuseAdapter
        from aflow.harnesses.opencode import OpencodeAdapter
        from aflow.harnesses.pi import PiAdapter
        for adapter in (
            CodexAdapter(), ClaudeAdapter(), CopilotAdapter(), GeminiAdapter(),
            KiroAdapter(), MuseAdapter(), OpencodeAdapter(), PiAdapter(), ReasonixAdapter(),
        ):
            assert adapter_manager_workspace_read(adapter) is True, adapter.name

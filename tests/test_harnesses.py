from aflow._test_support import *  # noqa: F401,F403
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
            '-dir',
            '/repo',
            '--model',
            'deepseek-pro',
            'SYSTEM\n\nUSER',
        )
        assert not adapter.supports_effort
        assert invocation.prompt_mode == 'prefix-system-into-user-prompt'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'

    def test_reasonix_without_model_and_with_effort_ignores_effort(self) -> None:
        adapter = ReasonixAdapter()
        invocation = adapter.build_invocation(
            repo_root=Path('/repo'),
            model=None,
            system_prompt='SYSTEM',
            user_prompt='USER',
            effort='high',
        )
        assert invocation.argv == ('reasonix', 'run', '-dir', '/repo', 'SYSTEM\n\nUSER')
        assert '--effort' not in invocation.argv
        assert '--model' not in invocation.argv

    def test_codex_without_effort(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='SYSTEM', user_prompt='USER')
        assert invocation.argv == ('codex', 'exec', '--dangerously-bypass-approvals-and-sandbox', '-C', '/repo', '--model', 'gpt-5.4', 'SYSTEM\n\nUSER')
        assert invocation.prompt_mode == 'prefix-system-into-user-prompt'
        assert invocation.effective_prompt == 'SYSTEM\n\nUSER'

    def test_codex_with_effort(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='SYSTEM', user_prompt='USER', effort='high')
        argv = invocation.argv
        assert '-c' in argv
        assert 'model_reasoning_effort=\'high\'' in argv
        prompt_index = argv.index('SYSTEM\n\nUSER')
        assert argv[prompt_index - 2] == '-c'
        assert argv[-1] == 'SYSTEM\n\nUSER'

    def test_codex_effort_preserves_prompt_as_final_element(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model='gpt-5.4', system_prompt='PROMPT', user_prompt='INSTRUCTIONS', effort='low')
        assert invocation.argv[-1] == 'PROMPT\n\nINSTRUCTIONS'

    def test_codex_without_model_omits_model_flag(self) -> None:
        adapter = CodexAdapter()
        invocation = adapter.build_invocation(repo_root=Path('/repo'), model=None, system_prompt='SYSTEM', user_prompt='USER')
        assert '--model' not in invocation.argv
        assert invocation.argv[-1] == 'SYSTEM\n\nUSER'

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


class GitBannerTests(unittest.TestCase):

    def test_build_banner_renders_git_row_when_clean(self) -> None:
        from rich.console import Console
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
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=state,
            git_summary=summary,
        )
        assert panel is not None
        console = Console(record=True, width=100)
        console.print(panel)
        text = console.export_text()
        assert "Git" in text
        assert "clean since start" in text

    def test_build_banner_renders_git_row_with_changes(self) -> None:
        from rich.console import Console
        from aflow.git_status import GitSummary
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        summary = GitSummary(
            modified_count=2,
            added_count=1,
            removed_count=0,
            lines_added=10,
            lines_removed=3,
            commit_count=1,
            changed_paths=("foo.py", "bar.py", "baz.py"),
        )
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=state,
            git_summary=summary,
        )
        assert panel is not None
        console = Console(record=True, width=120)
        console.print(panel)
        text = console.export_text()
        assert "Git" in text
        assert "M 2" in text
        assert "Files" in text
        assert "foo.py" in text

    def test_build_banner_files_row_respects_config_limit(self) -> None:
        from rich.console import Console
        from aflow.git_status import GitSummary
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        summary = GitSummary(
            modified_count=5,
            added_count=0,
            removed_count=0,
            lines_added=0,
            lines_removed=0,
            commit_count=0,
            changed_paths=("a.py", "b.py", "c.py", "d.py", "e.py"),
        )
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            config_banner_files_limit=3,
            state=state,
            git_summary=summary,
        )
        assert panel is not None
        console = Console(record=True, width=120)
        console.print(panel)
        text = console.export_text()
        assert "+2 more" in text
        assert "d.py" not in text
        assert "a.py" in text

    def test_build_banner_no_git_summary_omits_git_rows(self) -> None:
        from rich.console import Console
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=state,
        )
        assert panel is not None
        console = Console(record=True, width=100)
        console.print(panel)
        text = console.export_text()
        assert "Git" not in text
        assert "Files" not in text

    def test_build_banner_uses_title_case_plan_stem(self) -> None:
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/config-plan.md"),
            original_plan_path=Path("/fake/workflow-visualization_show-and_exclusions.md"),
            state=state,
        )
        assert panel is not None
        title = panel.title
        title_text = title.plain if hasattr(title, "plain") else str(title)
        assert title_text == "Workflow Visualization Show And Exclusions"

    def test_build_banner_omits_issue_row_when_count_is_zero(self) -> None:
        from rich.console import Console
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=state,
        )
        assert panel is not None
        console = Console(record=True, width=100)
        console.print(panel)
        text = console.export_text()
        assert "Issues" not in text
        assert "Harness/Model" not in text
        assert "Active Plan" not in text
        assert "Step" not in text

    def test_build_banner_renders_issue_summary_path_when_present(self) -> None:
        from rich.console import Console
        state = ControllerState(
            last_snapshot=PlanSnapshot(None, 0, 0, False),
            issues_accumulated=1,
            issues_summary_path=".aflow/runs/20260407T120000Z-00000001/issues.md",
        )
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=state,
        )
        assert panel is not None
        console = Console(record=True, width=120)
        console.print(panel)
        text = console.export_text()
        assert ".aflow/runs/20260407T120000Z-00000001/issues.md" in text

    def test_build_banner_renders_run_id_and_resumed_from(self) -> None:
        from rich.console import Console
        state = ControllerState(
            last_snapshot=PlanSnapshot(None, 0, 0, False),
            run_id="20260407T120000Z-00000001",
            resumed_from_run_id="20260407T110000Z-00000000",
        )
        panel = build_banner(
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=state,
        )
        assert panel is not None
        console = Console(record=True, width=120)
        console.print(panel)
        text = console.export_text()
        assert "Run ID" in text
        assert "20260407T120000Z-00000001" in text
        assert "Resumed From" in text
        assert "20260407T110000Z-00000000" in text

    def test_build_banner_renders_last_step_exits(self) -> None:
        from rich.console import Console
        from unittest.mock import patch
        import aflow.status as status_mod

        steps = {
            "plan": WorkflowStepConfig(role="planner", go=(GoTransition(to="review"),)),
            "review": WorkflowStepConfig(
                role="reviewer",
                go=(
                    GoTransition(to="ship", when="DONE"),
                    GoTransition(to="plan", when="!DONE"),
                ),
            ),
        }
        source = status_mod.WorkflowGraphSource(
            declared_steps=steps,
            executable_steps=steps,
        )
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        with patch.object(status_mod, "load_workflow_config", side_effect=AssertionError("should not reload")):
            panel = build_banner(
                workflow_name="demo",
                workflow_graph_source=source,
                config_max_turns=10,
                config_plan_path=Path("/fake/plan.md"),
                state=state,
            )
            assert panel is not None
            console = Console(record=True, width=120)
            console.print(panel)
            text = console.export_text()
        assert "go→ ship" in text
        assert "[DONE]" in text

    def test_build_banner_renders_excluded_steps_from_explicit_graph_source(self) -> None:
        from rich.console import Console
        from unittest.mock import patch
        import aflow.status as status_mod

        declared_steps = {
            "plan": WorkflowStepConfig(role="planner", go=(GoTransition(to="review"),)),
            "review": WorkflowStepConfig(role="reviewer", go=(GoTransition(to="ship"),)),
            "ship": WorkflowStepConfig(role="shipper"),
        }
        source = status_mod.WorkflowGraphSource(
            declared_steps=declared_steps,
            executable_steps={
                "plan": declared_steps["plan"],
                "ship": declared_steps["ship"],
            },
            excluded_step_names=("review",),
        )
        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        with patch.object(status_mod, "load_workflow_config", side_effect=AssertionError("should not reload")):
            panel = build_banner(
                workflow_name="demo",
                workflow_graph_source=source,
                config_max_turns=10,
                config_plan_path=Path("/fake/plan.md"),
                state=state,
            )
            assert panel is not None
            console = Console(record=True, width=120)
            console.print(panel)
            text = console.export_text()
        assert "plan" in text
        assert "review" in text
        assert "ship" in text

    def test_turn_panels_render_transition_and_active_plan(self) -> None:
        from rich.console import Console
        import aflow.status as status_mod

        steps = {
            "review": WorkflowStepConfig(role="reviewer", go=(GoTransition(to="implement_plan", when="NEW_PLAN_EXISTS || !DONE"),)),
        }
        source = status_mod.WorkflowGraphSource(
            declared_steps=steps,
            executable_steps=steps,
        )
        record = TurnRecord(
            turn_number=1,
            step_name="review",
            step_role="reviewer",
            resolved_harness_name="codex",
            resolved_model_display="codex / gpt-5.4",
            active_plan_path="/fake/plan.md",
            chosen_transition="implement_plan",
            chosen_transition_condition="NEW_PLAN_EXISTS || !DONE",
            issues_summary_path=".aflow/runs/20260407T120000Z-00000001/issues.md",
            outcome="completed",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        state = ControllerState(
            last_snapshot=PlanSnapshot(None, 0, 0, False),
            turn_history=[record],
        )
        panel = build_banner(
            workflow_name="demo",
            workflow_graph_source=source,
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=state,
        )
        assert panel is not None
        console = Console(record=True, width=120)
        console.print(panel)
        text = console.export_text()
        assert "review" in text
        assert "Harness/Model" in text
        assert "codex / gpt-5.4" in text
        assert "Active Plan" in text
        assert "go→ implement_plan" in text
        assert "[NEW_PLAN_EXISTS || !DONE]" in text
        assert ".aflow/runs/20260407T120000Z-00000001/issues.md" in text

    def test_shared_step_classification_distinguishes_active_inactive_excluded_and_skipped(self) -> None:
        import aflow.status as status_mod

        steps = {
            "plan": WorkflowStepConfig(role="planner"),
            "review": WorkflowStepConfig(role="reviewer"),
            "ship": WorkflowStepConfig(role="shipper"),
            "deploy": WorkflowStepConfig(role="deployer"),
        }
        source = status_mod.WorkflowGraphSource(
            declared_steps=steps,
            executable_steps={"plan": steps["plan"], "ship": steps["ship"], "deploy": steps["deploy"]},
            excluded_step_names=("review",),
        )
        assert status_mod._visual_start_skipped_step_names(
            declared_steps=source.declared_steps,
            executable_steps=source.executable_steps,
            excluded_step_names=source.excluded_step_names,
            selected_start_step="ship",
        ) == ("plan",)
        context = status_mod.WorkflowGraphContext(
            source=source,
            visual_start_step_skipped_step_names=("plan",),
            current_step_name="ship",
            current_turn_is_running=True,
        )
        assert status_mod._workflow_step_kind(step_name="plan", context=context) == "skipped"
        assert status_mod._workflow_step_kind(step_name="review", context=context) == "excluded"
        assert status_mod._workflow_step_kind(step_name="ship", context=context) == "active"
        assert status_mod._workflow_step_kind(step_name="deploy", context=context) == "inactive"
        assert status_mod._workflow_transition_target_kind(target_name="END", context=context) == "terminal"
        assert status_mod._workflow_transition_target_kind(target_name="review", context=context) == "excluded"
        assert status_mod._workflow_step_style("active") == "bold green"
        assert status_mod._workflow_step_style("inactive") == "green"
        assert status_mod._workflow_step_style("excluded") == "grey50"
        assert status_mod._workflow_transition_style(source_kind="active", target_kind="terminal") == "white"
        assert status_mod._workflow_transition_style(source_kind="inactive", target_kind="terminal") == "green"
        assert status_mod._workflow_transition_style(source_kind="skipped", target_kind="terminal") == "grey50"
        assert status_mod._workflow_transition_style(source_kind="active", target_kind="inactive") == "white"
        assert status_mod._workflow_transition_style(source_kind="inactive", target_kind="inactive") == "green"
        assert status_mod._workflow_transition_style(source_kind="excluded", target_kind="inactive") == "grey50"
        assert status_mod._workflow_transition_style(source_kind="inactive", target_kind="excluded") == "grey50"

    def test_build_banner_renders_end_transitions_with_runtime_styles(self) -> None:
        from rich.console import Console
        from datetime import datetime, timezone
        import aflow.status as status_mod

        active_steps = {
            "go": WorkflowStepConfig(role="worker", go=(GoTransition(to="END"),)),
        }
        active_source = status_mod.WorkflowGraphSource(
            declared_steps=active_steps,
            executable_steps=active_steps,
        )
        active_record = TurnRecord(
            turn_number=1,
            step_name="go",
            step_role="worker",
            resolved_harness_name="codex",
            resolved_model_display="codex / gpt-5.4",
            outcome="running",
            started_at=datetime.now(timezone.utc),
        )
        active_state = ControllerState(
            last_snapshot=PlanSnapshot(None, 0, 0, False),
            turn_history=[active_record],
            current_turn_started_at=datetime.now(timezone.utc),
            active_turn=1,
        )
        active_panel = build_banner(
            workflow_name="demo",
            workflow_graph_source=active_source,
            current_step_name="go",
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=active_state,
        )
        assert active_panel is not None
        active_console = Console(record=True, width=120, force_terminal=True, color_system="standard")
        active_console.print(active_panel)
        active_text = active_console.export_text(styles=True)
        assert "\x1b[37m  ├─go→ \x1b[0m\x1b[1mEND\x1b[0m" in active_text

        skipped_steps = {
            "prep": WorkflowStepConfig(role="worker", go=(GoTransition(to="END"),)),
            "go": WorkflowStepConfig(role="worker"),
        }
        skipped_source = status_mod.WorkflowGraphSource(
            declared_steps=skipped_steps,
            executable_steps=skipped_steps,
        )
        skipped_state = ControllerState(
            last_snapshot=PlanSnapshot(None, 0, 0, False),
            selected_start_step="go",
        )
        skipped_panel = build_banner(
            workflow_name="demo",
            workflow_graph_source=skipped_source,
            config_max_turns=10,
            config_plan_path=Path("/fake/plan.md"),
            state=skipped_state,
        )
        assert skipped_panel is not None
        skipped_console = Console(record=True, width=120, force_terminal=True, color_system="standard")
        skipped_console.print(skipped_panel)
        skipped_text = skipped_console.export_text(styles=True)
        assert "\x1b[38;5;244m  ├─go→ \x1b[0m\x1b[1;38;5;244mEND\x1b[0m" in skipped_text

    def test_workflow_show_renders_end_transition_with_included_style(self) -> None:
        from rich.console import Console
        import aflow.status as status_mod
        from aflow.config import WorkflowConfig, WorkflowUserConfig

        steps = {
            "go": WorkflowStepConfig(role="worker", go=(GoTransition(to="END"),)),
        }
        config = WorkflowUserConfig(
            workflows={
                "demo": WorkflowConfig(
                    declared_steps=steps,
                    steps=steps,
                    first_step="go",
                ),
            },
        )
        renderable = status_mod.build_workflow_show(config=config, workflow_name="demo")
        assert renderable is not None
        console = Console(record=True, width=120, force_terminal=True, color_system="standard")
        console.print(renderable)
        text = console.export_text(styles=True)
        assert "\x1b[32m  ├─go→ \x1b[0m\x1b[1;32mEND\x1b[0m" in text

    def test_workflow_show_renders_shared_roles_and_all_workflows(self) -> None:
        from rich.console import Console
        import aflow.status as status_mod
        from aflow.config import GoTransition, TeamConfig, WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig

        alpha_steps = {
            "plan": WorkflowStepConfig(role="architect", go=(GoTransition(to="END"),)),
        }
        beta_steps = {
            "ship": WorkflowStepConfig(role="worker", go=(GoTransition(to="END"),)),
        }
        config = WorkflowUserConfig(
            roles={
                "architect": "codex.default",
                "worker": "codex.default",
            },
            teams={
                "7teen": TeamConfig(roles={"worker": "codex.nano"}),
            },
            workflows={
                "alpha": WorkflowConfig(
                    declared_steps=alpha_steps,
                    steps=alpha_steps,
                    first_step="plan",
                ),
                "beta": WorkflowConfig(
                    declared_steps=beta_steps,
                    steps=beta_steps,
                    first_step="ship",
                ),
            },
        )
        renderable = status_mod.build_workflow_show(config=config)
        assert renderable is not None
        console = Console(record=True, width=120)
        console.print(renderable)
        text = console.export_text()
        assert "Roles / Teams" in text
        assert "architect" in text
        assert "worker" in text
        assert "7teen" in text
        assert "alpha" in text
        assert "beta" in text

    def test_workflow_show_single_filters_applicable_roles_and_teams(self) -> None:
        from rich.console import Console
        import aflow.status as status_mod
        from aflow.config import GoTransition, TeamConfig, WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig

        declared_steps = {
            "review": WorkflowStepConfig(role="reviewer", go=(GoTransition(to="implement"),)),
            "implement": WorkflowStepConfig(role="architect", go=(GoTransition(to="END"),)),
        }
        config = WorkflowUserConfig(
            roles={
                "reviewer": "claude.opus",
                "architect": "codex.default",
            },
            teams={
                "7teen": TeamConfig(roles={"worker": "codex.nano"}),
                "reviewers": TeamConfig(roles={"reviewer": "claude.opus"}),
            },
            workflows={
                "alpha": WorkflowConfig(
                    declared_steps=declared_steps,
                    steps={"implement": declared_steps["implement"]},
                    first_step="implement",
                    excluded_steps=("review",),
                    team="7teen",
                ),
            },
        )
        renderable = status_mod.build_workflow_show(config=config, workflow_name="alpha")
        assert renderable is not None
        assert status_mod._workflow_effective_role_names(config.workflows["alpha"]) == ("architect",)
        assert status_mod._workflow_applicable_team_names(
            config=config,
            workflow=config.workflows["alpha"],
            role_names=("architect",),
        ) == ("7teen",)
        console = Console(record=True, width=120)
        console.print(renderable)
        text = console.export_text()
        assert "Roles / Teams" in text
        assert "architect" in text
        assert "7teen" in text
        assert "reviewers" not in text
        assert "review" in text
        assert "implement" in text

    def test_banner_renderer_refresh_thread_updates_live(self) -> None:
        import aflow.status as status_mod
        from unittest.mock import MagicMock, patch
        live_updates: list[object] = []

        class FakeLive:
            def __init__(self, panel, **kwargs: object) -> None:
                live_updates.append(panel)
            def start(self) -> None:
                pass
            def update(self, panel: object) -> None:
                live_updates.append(panel)
            def stop(self) -> None:
                pass

        state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
        with patch.object(status_mod, "_RICH_AVAILABLE", True), \
             patch.object(status_mod, "Live", FakeLive):
            renderer = status_mod.BannerRenderer(
                config_max_turns=10,
                config_plan_path=Path("/fake/plan.md"),
                refresh_interval_seconds=0.05,
                git_poll_interval_seconds=9999.0,
            )
            renderer.start(state)
            time.sleep(0.2)
            renderer.stop(state)
        assert len(live_updates) >= 3

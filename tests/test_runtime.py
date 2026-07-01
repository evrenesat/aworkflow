from aflow._test_support import *  # noqa: F401,F403
from aflow.config import ErrorHandlingConfig, HarnessErrorRecoveryConfig, HarnessErrorRecoveryRuleConfig

class WorkflowRuntimeTests(unittest.TestCase):

    def test_run_process_captures_harness_output_without_echoing_to_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = root / 'emit_output.py'
            script.write_text(
                "import sys\n"
                "print('visible stdout')\n"
                "print('visible stderr', file=sys.stderr)\n",
                encoding='utf-8',
            )
            invocation = HarnessInvocation(
                label='test',
                argv=(sys.executable, str(script)),
                env={},
                prompt_mode='stdin',
                system_prompt='',
                user_prompt='',
                effective_prompt='',
            )
            state = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))

            class FakeBanner:
                def __init__(self) -> None:
                    self.updated = False

                def update(self, state: ControllerState) -> None:
                    self.updated = True

            banner = FakeBanner()
            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()

            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture), \
                 patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True):
                completed = _run_process(invocation, root, banner, state)  # type: ignore[arg-type]

            assert completed.returncode == 0
            assert completed.stdout == 'visible stdout\n'
            assert completed.stderr == 'visible stderr\n'
            assert stdout_capture.getvalue() == ''
            assert stderr_capture.getvalue() == ''
            assert banner.updated

    def test_prompt_rendering_supports_inline_and_file_uri_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / 'config'
            working_dir = root / 'cwd'
            config_dir.mkdir()
            working_dir.mkdir()
            config_prompt = config_dir / 'relative.txt'
            config_prompt.write_text('Config content with {ACTIVE_PLAN_PATH}', encoding='utf-8')
            absolute_prompt = root / 'absolute' / 'path.txt'
            absolute_prompt.parent.mkdir()
            absolute_prompt.write_text('Absolute content with {ORIGINAL_PLAN_PATH}', encoding='utf-8')
            cwd_prompt = working_dir / 'relative.txt'
            cwd_prompt.write_text('Cwd content with {NEW_PLAN_PATH}', encoding='utf-8')
            original = root / 'plan.md'
            new_plan = root / 'plan-cp01-v01.md'
            active = root / 'active.md'
            result = render_prompt('file://relative.txt', config_dir=config_dir, working_dir=working_dir, original_plan_path=original, new_plan_path=new_plan, active_plan_path=active)
            assert result == f'Config content with {active}'
            absolute_result = render_prompt(f'file://{absolute_prompt}', config_dir=config_dir, working_dir=working_dir, original_plan_path=original, new_plan_path=new_plan, active_plan_path=active)
            assert absolute_result == f'Absolute content with {original}'
            cwd_result = render_prompt('file://./relative.txt', config_dir=config_dir, working_dir=working_dir, original_plan_path=original, new_plan_path=new_plan, active_plan_path=active)
            assert cwd_result == f'Cwd content with {new_plan}'
            result_inline = render_prompt('Work from {ACTIVE_PLAN_PATH}. New: {NEW_PLAN_PATH}. Original: {ORIGINAL_PLAN_PATH}', config_dir=config_dir, working_dir=working_dir, original_plan_path=original, new_plan_path=new_plan, active_plan_path=active)
            assert result_inline == f'Work from {active}. New: {new_plan}. Original: {original}'

    def test_prompt_rendering_expands_next_checkpoint_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / 'config'
            working_dir = root / 'cwd'
            config_dir.mkdir()
            working_dir.mkdir()
            plan = root / 'plan.md'
            plan.write_text(
                '# Plan\n\n'
                '### [x] Checkpoint 1: Done\n'
                '- [x] step one\n\n'
                '### [ ] Checkpoint 2: Current\n'
                '- [ ] step two\n',
                encoding='utf-8',
            )
            result = render_prompt(
                'Next: {NEXT_CP}. {WORK_ON_NEXT_CHECKPOINT_CMD}',
                config_dir=config_dir,
                working_dir=working_dir,
                original_plan_path=plan,
                new_plan_path=root / 'plan-cp02-v01.md',
                active_plan_path=plan,
            )
            assert result == (
                'Next: 2. Work only on Checkpoint #2. '
                'Do not repeat earlier checkpoints, and do not skip ahead.'
            )

    def test_prompt_rendering_uses_empty_next_checkpoint_command_for_completed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / 'config'
            working_dir = root / 'cwd'
            config_dir.mkdir()
            working_dir.mkdir()
            plan = root / 'plan.md'
            plan.write_text(
                '# Plan\n\n'
                '### [x] Checkpoint 1: Done\n'
                '- [x] step one\n',
                encoding='utf-8',
            )
            result = render_prompt(
                'Next: {NEXT_CP}. Cmd:{WORK_ON_NEXT_CHECKPOINT_CMD}',
                config_dir=config_dir,
                working_dir=working_dir,
                original_plan_path=plan,
                new_plan_path=root / 'plan-cp01-v02.md',
                active_plan_path=plan,
            )
            assert result == 'Next: -. Cmd:'

    def test_prompt_rendering_uses_empty_next_checkpoint_command_for_non_checkpoint_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / 'config'
            working_dir = root / 'cwd'
            config_dir.mkdir()
            working_dir.mkdir()
            followup = root / 'followup.md'
            followup.write_text('- [ ] fix the review finding\n', encoding='utf-8')
            result = render_prompt(
                'Next: {NEXT_CP}. Cmd:{WORK_ON_NEXT_CHECKPOINT_CMD}',
                config_dir=config_dir,
                working_dir=working_dir,
                original_plan_path=root / 'original.md',
                new_plan_path=root / 'followup-v02.md',
                active_plan_path=followup,
            )
            assert result == 'Next: -. Cmd:'

    def test_prompt_rendering_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / 'config'
            working_dir = root / 'cwd'
            config_dir.mkdir()
            working_dir.mkdir()
            with pytest.raises(WorkflowError) as ctx:
                render_prompt('file://./nonexistent.txt', config_dir=config_dir, working_dir=working_dir, original_plan_path=Path('/fake/plan.md'), new_plan_path=Path('/fake/new.md'), active_plan_path=Path('/fake/plan.md'))
            assert str(working_dir / 'nonexistent.txt') in str(ctx.value)

    def test_render_step_prompts_unknown_key_raises(self) -> None:
        step = WorkflowStepConfig(role='architect', prompts=('missing_key',))
        config = WorkflowUserConfig(prompts={})
        with pytest.raises(WorkflowError) as ctx:
            render_step_prompts(step, config, config_dir=Path('/cfg'), working_dir=Path('/cwd'), original_plan_path=Path('/p.md'), new_plan_path=Path('/n.md'), active_plan_path=Path('/a.md'))
        assert 'missing_key' in str(ctx.value)

    def test_render_step_prompts_joins_multiple_prompts(self) -> None:
        step = WorkflowStepConfig(role='architect', prompts=('p1', 'p2'))
        config = WorkflowUserConfig(prompts={'p1': 'First {ORIGINAL_PLAN_PATH}', 'p2': 'Second {ACTIVE_PLAN_PATH}'})
        result = render_step_prompts(step, config, config_dir=Path('/cfg'), working_dir=Path('/cwd'), original_plan_path=Path('/orig.md'), new_plan_path=Path('/new.md'), active_plan_path=Path('/active.md'))
        assert result == 'First /orig.md\n\nSecond /active.md'

    def test_new_plan_path_increments_version_for_checkpoint_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            original = parent / 'plan.md'
            original.write_text('dummy', encoding='utf-8')
            p1 = generate_new_plan_path(original, checkpoint_index=1)
            assert p1.name == 'plan-cp01-v01.md'
            p1.touch()
            p2 = generate_new_plan_path(original, checkpoint_index=1)
            assert p2.name == 'plan-cp01-v02.md'
            p2.touch()
            p3 = generate_new_plan_path(original, checkpoint_index=1)
            assert p3.name == 'plan-cp01-v03.md'
            p4 = generate_new_plan_path(original, checkpoint_index=2)
            assert p4.name == 'plan-cp02-v01.md'

    def test_new_plan_path_uses_correct_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            original = parent / 'plan.markdown'
            original.write_text('dummy', encoding='utf-8')
            p1 = generate_new_plan_path(original, checkpoint_index=1)
            assert p1.name == 'plan-cp01-v01.markdown'

    def test_new_plan_path_none_checkpoint_uses_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            original = parent / 'plan.md'
            original.write_text('dummy', encoding='utf-8')
            p1 = generate_new_plan_path(original, checkpoint_index=None)
            assert p1.name == 'plan-cp01-v01.md'

    def test_original_plan_backup_creates_repo_root_backup_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            original = root / 'plan.md'
            original.write_text('# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n', encoding='utf-8')

            backup_path = _backup_original_plan(repo_root, original)

            expected = repo_root / 'plans' / 'backups' / 'plan.md'
            assert backup_path == expected
            assert expected.read_text(encoding='utf-8') == original.read_text(encoding='utf-8')
            assert len(list((repo_root / 'plans' / 'backups').iterdir())) == 1

    def test_original_plan_backup_reuses_identical_existing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            original = root / 'plan.md'
            text = '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n'
            original.write_text(text, encoding='utf-8')
            backup_dir = repo_root / 'plans' / 'backups'
            backup_dir.mkdir(parents=True)
            (backup_dir / 'plan.md').write_text(text, encoding='utf-8')

            first = _backup_original_plan(repo_root, original)
            second = _backup_original_plan(repo_root, original)

            assert first == backup_dir / 'plan.md'
            assert second == backup_dir / 'plan.md'
            assert sorted(child.name for child in backup_dir.iterdir()) == ['plan.md']

    def test_original_plan_backup_reuses_identical_versioned_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            original = root / 'plan.md'
            text = '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n'
            original.write_text(text, encoding='utf-8')
            backup_dir = repo_root / 'plans' / 'backups'
            backup_dir.mkdir(parents=True)
            (backup_dir / 'plan.md').write_text('different\n', encoding='utf-8')
            (backup_dir / 'plan_v02.md').write_text(text, encoding='utf-8')

            backup_path = _backup_original_plan(repo_root, original)

            assert backup_path == backup_dir / 'plan_v02.md'
            assert sorted(child.name for child in backup_dir.iterdir()) == ['plan.md', 'plan_v02.md']

    def test_original_plan_backup_versions_conflicting_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            original = root / 'plan.md'
            backup_dir = repo_root / 'plans' / 'backups'
            backup_dir.mkdir(parents=True)
            original.write_text('first version\n', encoding='utf-8')
            (backup_dir / 'plan.md').write_text('different base\n', encoding='utf-8')

            first_backup = _backup_original_plan(repo_root, original)
            assert first_backup == backup_dir / 'plan_v02.md'

            original.write_text('second version\n', encoding='utf-8')
            second_backup = _backup_original_plan(repo_root, original)
            assert second_backup == backup_dir / 'plan_v03.md'
            assert sorted(child.name for child in backup_dir.iterdir()) == ['plan.md', 'plan_v02.md', 'plan_v03.md']

    def test_condition_parsing_simple_symbols(self) -> None:
        assert evaluate_condition('DONE', done=True, new_plan_exists=False, max_turns_reached=False)
        assert not evaluate_condition('DONE', done=False, new_plan_exists=False, max_turns_reached=False)
        assert evaluate_condition('NEW_PLAN_EXISTS', done=False, new_plan_exists=True, max_turns_reached=False)
        assert evaluate_condition('MAX_TURNS_REACHED', done=False, new_plan_exists=False, max_turns_reached=True)

    def test_condition_parsing_or(self) -> None:
        assert evaluate_condition('DONE || MAX_TURNS_REACHED', done=True, new_plan_exists=False, max_turns_reached=False)
        assert evaluate_condition('DONE || MAX_TURNS_REACHED', done=False, new_plan_exists=False, max_turns_reached=True)
        assert not evaluate_condition('DONE || MAX_TURNS_REACHED', done=False, new_plan_exists=False, max_turns_reached=False)

    def test_condition_parsing_and(self) -> None:
        assert evaluate_condition('DONE && NEW_PLAN_EXISTS', done=True, new_plan_exists=True, max_turns_reached=False)
        assert not evaluate_condition('DONE && NEW_PLAN_EXISTS', done=True, new_plan_exists=False, max_turns_reached=False)

    def test_condition_parsing_negation(self) -> None:
        assert evaluate_condition('!DONE', done=False, new_plan_exists=False, max_turns_reached=False)
        assert not evaluate_condition('!DONE', done=True, new_plan_exists=False, max_turns_reached=False)

    def test_condition_parsing_parentheses(self) -> None:
        assert evaluate_condition('(DONE || MAX_TURNS_REACHED) && NEW_PLAN_EXISTS', done=True, new_plan_exists=True, max_turns_reached=False)
        assert not evaluate_condition('(DONE || MAX_TURNS_REACHED) && NEW_PLAN_EXISTS', done=False, new_plan_exists=False, max_turns_reached=False)

    def test_condition_parsing_complex(self) -> None:
        expr = '!(DONE || MAX_TURNS_REACHED) && NEW_PLAN_EXISTS'
        assert evaluate_condition(expr, done=False, new_plan_exists=True, max_turns_reached=False)
        assert not evaluate_condition(expr, done=True, new_plan_exists=True, max_turns_reached=False)

    def test_ordered_transitions_first_match_wins(self) -> None:
        transitions = (GoTransition(to='END', when='DONE'), GoTransition(to='END', when='MAX_TURNS_REACHED'), GoTransition(to='step2'))
        assert pick_transition(transitions, step_path='workflow.w.steps.s', done=True, new_plan_exists=False, max_turns_reached=False) == 'END'
        assert pick_transition(transitions, step_path='workflow.w.steps.s', done=False, new_plan_exists=False, max_turns_reached=True) == 'END'
        assert pick_transition(transitions, step_path='workflow.w.steps.s', done=False, new_plan_exists=False, max_turns_reached=False) == 'step2'

    def test_ordered_transitions_unconditional_fallback(self) -> None:
        transitions = (GoTransition(to='END', when='DONE'), GoTransition(to='step2'))
        assert pick_transition(transitions, step_path='workflow.w.steps.s', done=False, new_plan_exists=False, max_turns_reached=False) == 'step2'
        assert pick_transition(transitions, step_path='workflow.w.steps.s', done=True, new_plan_exists=False, max_turns_reached=False) == 'END'

    def test_pick_transition_no_match_raises(self) -> None:
        transitions = (GoTransition(to='END', when='DONE'), GoTransition(to='END', when='NEW_PLAN_EXISTS'))
        with pytest.raises(WorkflowError) as ctx:
            pick_transition(transitions, step_path='workflow.w.steps.s', done=False, new_plan_exists=False, max_turns_reached=False)
        assert 'no transition matched' in str(ctx.value)

    def test_resolve_profile_success(self) -> None:
        config = WorkflowUserConfig(harnesses={'opencode': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m', effort='high')})})
        result = resolve_profile('opencode.default', config, step_path='workflow.w.steps.s')
        assert result.harness_name == 'opencode'
        assert result.profile_name == 'default'
        assert result.model == 'm'
        assert result.effort == 'high'

    def test_resolve_profile_unknown_harness_raises(self) -> None:
        config = WorkflowUserConfig()
        with pytest.raises(WorkflowError) as ctx:
            resolve_profile('unknown.default', config, step_path='workflow.w.steps.s')
        assert 'unknown harness' in str(ctx.value)

    def test_resolve_profile_unknown_profile_raises(self) -> None:
        config = WorkflowUserConfig(harnesses={'opencode': WorkflowHarnessConfig(profiles={})})
        with pytest.raises(WorkflowError) as ctx:
            resolve_profile('opencode.missing', config, step_path='workflow.w.steps.s')
        assert 'unknown profile' in str(ctx.value)

    def test_resolve_profile_bare_selector_raises(self) -> None:
        config = WorkflowUserConfig()
        with pytest.raises(WorkflowError) as ctx:
            resolve_profile('opencode', config, step_path='workflow.w.steps.s')
        assert 'fully qualified' in str(ctx.value)

    def test_resolve_role_selector_uses_team_override_then_global_fallback(self) -> None:
        config = WorkflowUserConfig(
            roles={
                'architect': 'codex.default',
                'senior_architect': 'opencode.default',
            },
            teams={
                '7teen': TeamConfig(roles={'architect': 'gemini.fast'}),
            },
        )
        assert resolve_role_selector('architect', '7teen', config, step_path='workflow.w.steps.review') == 'gemini.fast'
        assert resolve_role_selector('senior_architect', '7teen', config, step_path='workflow.w.steps.review') == 'opencode.default'

    def test_load_workflow_config_accepts_legacy_inline_team_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            config_path = home_dir / '.config' / 'aflow' / 'aflow.toml'
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '[aflow]\ndefault_workflow = "simple"\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\nsenior_architect = "opencode.default"\n\n'
                '[teams.legacy]\narchitect = "opencode.default"\nsenior_architect = "opencode.default"\n\n'
                '[prompts]\np = "Work."\n',
                encoding='utf-8',
            )
            (config_path.parent / 'workflows.toml').write_text(
                '[workflow.simple.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n',
                encoding='utf-8',
            )
            config = load_workflow_config(config_path)
            assert config.workflows['simple'].first_step == 'impl'
            assert config.teams['legacy'].roles == {
                'architect': 'opencode.default',
                'senior_architect': 'opencode.default',
            }

    def test_resolve_role_selector_unknown_team_raises(self) -> None:
        config = WorkflowUserConfig(roles={'architect': 'codex.default'})
        with pytest.raises(WorkflowError) as ctx:
            resolve_role_selector('architect', 'missing', config, step_path='workflow.w.steps.review')
        assert 'unknown team' in str(ctx.value)

    def test_workflow_ends_only_via_end_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('implementation_prompt',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'implementation_prompt': 'Work from {ACTIVE_PLAN_PATH}.'})
            call_count = 0

            def runner(argv, **kwargs):
                nonlocal call_count
                call_count += 1
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert result.turns_completed == 1
            assert result.final_snapshot.is_complete
            assert call_count == 1

    def test_workflow_loops_implementer_steps_without_stagnation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n- [ ] step two\n')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('implementation_prompt',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'implementation_prompt': 'Work from {ACTIVE_PLAN_PATH}.'})
            call_count = 0

            def runner(argv, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [x] step one\n- [ ] step two\n')
                elif call_count == 2:
                    _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n- [x] step two\n')
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert result.turns_completed == 2
            assert result.final_snapshot.is_complete
            assert call_count == 2

    def test_active_plan_updates_only_when_generated_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'loop': WorkflowConfig(steps={'review': WorkflowStepConfig(role='architect', prompts=('review_prompt',), go=(GoTransition(to='implement'),)), 'implement': WorkflowStepConfig(role='architect', prompts=('impl_prompt',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='review')))}, first_step='review')}, prompts={'review_prompt': 'Review. New plan: {NEW_PLAN_PATH}. Active: {ACTIVE_PLAN_PATH}.', 'impl_prompt': 'Implement. New plan: {NEW_PLAN_PATH}. Active: {ACTIVE_PLAN_PATH}.'})
            turn_number = [0]

            def capturing_runner(argv, **kwargs):
                turn_number[0] += 1
                if turn_number[0] == 1:
                    _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            run_workflow(controller_config, wf_config, 'loop', config_dir=config_dir, adapter=CodexAdapter(), runner=capturing_runner)

    def test_active_plan_remains_unchanged_when_review_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n- [ ] step two\n')
            captured_active_paths: list[str] = []

            def capturing_runner(argv, **kwargs):
                prompt_text = ' '.join(argv)
                import re
                match = re.search('Active: (\\S+)', prompt_text)
                if match:
                    captured = match.group(1).rstrip('.')
                    captured_active_paths.append(captured)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'loop': WorkflowConfig(steps={'review': WorkflowStepConfig(role='architect', prompts=('review_prompt',), go=(GoTransition(to='implement'),)), 'implement': WorkflowStepConfig(role='architect', prompts=('impl_prompt',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='review')))}, first_step='review')}, prompts={'review_prompt': 'Active: {ACTIVE_PLAN_PATH}. New: {NEW_PLAN_PATH}.', 'impl_prompt': 'Active: {ACTIVE_PLAN_PATH}. New: {NEW_PLAN_PATH}.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=4)
            run_workflow(controller_config, wf_config, 'loop', config_dir=config_dir, adapter=CodexAdapter(), runner=capturing_runner)
            for p in captured_active_paths:
                assert str(plan_path) == p

    def test_active_plan_updates_when_generated_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            captured_active_paths: list[str] = []
            turn_counter = [0]

            def capturing_runner(argv, **kwargs):
                turn_counter[0] += 1
                prompt_text = ' '.join(argv)
                import re as re_mod
                match = re_mod.search('Active: (\\S+)', prompt_text)
                if match:
                    captured_active_paths.append(match.group(1).rstrip('.'))
                if turn_counter[0] == 1:
                    new_path = repo_root / 'plan-cp01-v01.md'
                    new_path.write_text('# Generated plan', encoding='utf-8')
                    _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'loop': WorkflowConfig(steps={'review': WorkflowStepConfig(role='architect', prompts=('review_prompt',), go=(GoTransition(to='implement'),)), 'implement': WorkflowStepConfig(role='architect', prompts=('impl_prompt',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='review')))}, first_step='review')}, prompts={'review_prompt': 'Active: {ACTIVE_PLAN_PATH}. New: {NEW_PLAN_PATH}.', 'impl_prompt': 'Active: {ACTIVE_PLAN_PATH}.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            run_workflow(controller_config, wf_config, 'loop', config_dir=config_dir, adapter=CodexAdapter(), runner=capturing_runner)
            assert len(captured_active_paths) == 2
            assert captured_active_paths[0] == str(plan_path)
            expected_new = str(repo_root / 'plan-cp01-v01.md')
            assert captured_active_paths[1] == expected_new

    def test_active_plan_updates_when_review_creates_alternate_followup_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            captured_active_paths: list[str] = []
            turn_counter = [0]

            def capturing_runner(argv, **kwargs):
                turn_counter[0] += 1
                prompt_text = ' '.join(argv)
                import re as re_mod
                match = re_mod.search('Active: (\\S+)', prompt_text)
                if match:
                    captured_active_paths.append(match.group(1).rstrip('.'))
                if turn_counter[0] == 1:
                    alt_followup = repo_root / 'plan-fix-cp01-v01.md'
                    alt_followup.write_text('# Generated follow-up\n', encoding='utf-8')
                    _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'loop': WorkflowConfig(
                    steps={
                        'review': WorkflowStepConfig(
                            role='architect',
                            prompts=('review_prompt',),
                            go=(GoTransition(to='followup', when='NEW_PLAN_EXISTS'), GoTransition(to='END')),
                        ),
                        'followup': WorkflowStepConfig(
                            role='architect',
                            prompts=('followup_prompt',),
                            go=(GoTransition(to='END'),),
                        ),
                    },
                    first_step='review',
                )},
                prompts={
                    'review_prompt': 'Review. Active: {ACTIVE_PLAN_PATH}. New: {NEW_PLAN_PATH}.',
                    'followup_prompt': 'Follow up. Active: {ACTIVE_PLAN_PATH}.',
                },
            )
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)

            result = run_workflow(
                controller_config,
                wf_config,
                'loop',
                config_dir=config_dir,
                adapter=CodexAdapter(),
                runner=capturing_runner,
            )

            assert result.turns_completed == 2
            assert captured_active_paths == [
                str(plan_path),
                str((repo_root / 'plan-fix-cp01-v01.md').resolve()),
            ]

    def test_workflow_multistep_review_and_implement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            call_order: list[str] = []

            def capturing_runner(argv, **kwargs):
                call_order.append(argv[0])
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            wf_config = WorkflowUserConfig(roles={'reviewer': 'claude.opus', 'worker': 'opencode.turbo'}, harnesses={'claude': WorkflowHarnessConfig(profiles={'opus': HarnessProfileConfig(model='claude-opus-4')}), 'opencode': WorkflowHarnessConfig(profiles={'turbo': HarnessProfileConfig(model='glm-5-turbo')})}, workflows={'review_loop': WorkflowConfig(steps={'review_plan': WorkflowStepConfig(role='reviewer', prompts=('review_prompt',), go=(GoTransition(to='implement_plan'),)), 'implement_plan': WorkflowStepConfig(role='worker', prompts=('impl_prompt',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='review_plan')))}, first_step='review_plan')}, prompts={'review_prompt': 'Review the plan.', 'impl_prompt': 'Implement from {ACTIVE_PLAN_PATH}.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'review_loop', config_dir=config_dir, runner=capturing_runner)
            assert result.turns_completed == 2
            assert result.final_snapshot.is_complete
            assert call_order == ['claude', 'opencode']

    def test_workflow_max_turns_routing_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout='noop', stderr='')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert result.turns_completed == 3
            assert not result.final_snapshot.is_complete

    def test_workflow_no_matching_transition_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE'),))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert 'no transition matched' in str(ctx.value)
            assert 'workflow.simple.steps.implement_plan' in str(ctx.value)
            assert 'DONE=False' in str(ctx.value)

    def test_workflow_no_matching_transition_writes_failed_run_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n- [ ] step two\n')
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [x] step one\n- [ ] step two\n')
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'loop': WorkflowConfig(steps={'review': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='implement'),)), 'implement': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE'),))}, first_step='review')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'loop', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert 'workflow.loop.steps.implement' in str(ctx.value)
            run_dir = ctx.value.run_dir
            assert run_dir is not None
            assert run_dir is not None
            run_json = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            assert run_json['failure_reason'] in str(ctx.value)
            assert run_json['turns_completed'] == 2
            assert run_json['last_snapshot']['current_checkpoint_name'] == 'Checkpoint 1: First'

    def test_workflow_done_reflects_original_plan_not_fix_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            fix_plan = repo_root / 'plan-cp01-v01.md'
            _write_plan(fix_plan, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            turn_counter = [0]
            ended_at_turn = [0]

            def runner(argv, **kwargs):
                turn_counter[0] += 1
                ended_at_turn[0] = turn_counter[0]
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            with pytest.raises(WorkflowError):
                run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert ended_at_turn[0] == 5

    def test_workflow_missing_workflow_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, WorkflowUserConfig(), 'nonexistent', config_dir=repo_root)
            assert 'not found' in str(ctx.value)

    def test_workflow_extra_instructions_appended(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            captured_user_prompts: list[str] = []

            class CapturingAdapter:
                name = 'codex'
                supports_effort = False

                def build_invocation(self, *, repo_root, model, system_prompt, user_prompt, effort=None):
                    captured_user_prompts.append(user_prompt)
                    return HarnessInvocation(label='codex', argv=('codex', 'run', user_prompt), env={}, prompt_mode='prefix-system-into-user-prompt', system_prompt=system_prompt, user_prompt=user_prompt, effective_prompt=f'{system_prompt}\n\n{user_prompt}' if system_prompt else user_prompt)
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'p': 'Work from {ACTIVE_PLAN_PATH}.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1, extra_instructions=('be careful', 'use tests'))
            run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CapturingAdapter(), runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '', ''))
            assert len(captured_user_prompts) == 1
            assert 'Work from' in captured_user_prompts[0]
            assert 'be careful use tests' in captured_user_prompts[0]

    def test_workflow_harness_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, stdout='bad', stderr='err')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert 'exited with code 1' in str(ctx.value)

    def test_workflow_prompt_render_failure_marks_run_failed_without_turn_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'file://./missing-prompt.txt'},
            )
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '', ''))
            assert 'prompt file not found' in str(ctx.value)
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            run_json = json.loads((run_dirs[-1] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            assert 'prompt file not found' in run_json['failure_reason']
            assert (run_dirs[-1] / 'turns').is_dir()
            assert list((run_dirs[-1] / 'turns').iterdir()) == []

    def test_workflow_already_complete_returns_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                return subprocess.CompletedProcess(argv, 0, 'ok', '')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END'),))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert result.turns_completed == 0
            assert result.final_snapshot.is_complete
            assert result.end_reason == 'already_complete'
            assert result.to_dict()['end_reason'] == 'already_complete'
            assert call_count[0] == 0

    def test_workflow_unconditional_end_uses_transition_end_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, 'ok', '')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END'),))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert result.turns_completed == 1
            assert result.end_reason == 'transition_end'
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['end_reason'] == 'transition_end'
            turn_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['end_reason'] == 'transition_end'
            assert turn_result['status'] == 'running'

    def test_workflow_end_reason_prefers_done_when_plan_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            completed_plan_path = repo_root / 'completed.md'
            new_plan_path = repo_root / 'plan-cp01-v01.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_plan(completed_plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')

            def runner(argv, **kwargs):
                shutil.copyfile(completed_plan_path, plan_path)
                new_plan_path.write_text('# Generated\n', encoding='utf-8')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='NEW_PLAN_EXISTS'),))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert result.turns_completed == 1
            assert result.end_reason == 'done'
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['end_reason'] == 'done'
            turn_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['end_reason'] == 'done'
            assert turn_result['status'] == 'completed'

    def test_workflow_completes_when_all_checkpoints_done_despite_unchecked_final_checklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            initial_plan = (
                '# Plan\n\n'
                '### [x] Checkpoint 1: Setup\n- [x] step a\n\n'
                '### [x] Checkpoint 2: Core\n- [x] step b\n\n'
                '### [x] Checkpoint 3: Tests\n- [x] step c\n\n'
                '### [ ] Checkpoint 4: Cleanup\n'
                '- [ ] cleanup step one\n'
                '- [ ] cleanup step two\n'
                '- [ ] cleanup step three\n'
                '- [ ] cleanup step four\n'
                '- [ ] cleanup step five\n'
                '- [ ] cleanup step six\n'
                '- [ ] cleanup step seven\n'
                '- [ ] cleanup step eight\n\n'
                '## Final Checklist\n'
                '- [ ] final item one\n'
                '- [ ] final item two\n'
                '- [ ] final item three\n'
                '- [ ] final item four\n'
                '- [ ] final item five\n'
                '- [ ] final item six\n'
                '- [ ] final item seven\n'
            )
            completed_plan = (
                '# Plan\n\n'
                '### [x] Checkpoint 1: Setup\n- [x] step a\n\n'
                '### [x] Checkpoint 2: Core\n- [x] step b\n\n'
                '### [x] Checkpoint 3: Tests\n- [x] step c\n\n'
                '### [x] Checkpoint 4: Cleanup\n'
                '- [x] cleanup step one\n'
                '- [x] cleanup step two\n'
                '- [x] cleanup step three\n'
                '- [x] cleanup step four\n'
                '- [x] cleanup step five\n'
                '- [x] cleanup step six\n'
                '- [x] cleanup step seven\n'
                '- [x] cleanup step eight\n\n'
                '## Final Checklist\n'
                '- [ ] final item one\n'
                '- [ ] final item two\n'
                '- [ ] final item three\n'
                '- [ ] final item four\n'
                '- [ ] final item five\n'
                '- [ ] final item six\n'
                '- [ ] final item seven\n'
            )
            _write_plan(plan_path, initial_plan)
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'Work.'},
            )

            def runner(argv, **kwargs):
                _write_plan(plan_path, completed_plan)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            assert result.end_reason == 'done'
            assert result.final_snapshot.is_complete

    def test_workflow_invalid_plan_failure_reports_parse_error_counts_not_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            initial_plan = (
                '# Plan\n\n'
                '### [x] Checkpoint 1: Done\n- [x] step a\n\n'
                '### [ ] Checkpoint 2: Current\n'
                '- [ ] real step one\n'
                '- [ ] real step two\n'
                '- [ ] real step three\n'
                '- [ ] real step four\n'
                '- [ ] real step five\n'
                '- [ ] real step six\n'
                '- [ ] real step seven\n'
                '- [ ] real step eight\n'
                '- [ ] real step nine\n'
                '- [ ] real step ten\n'
                '- [ ] real step eleven\n'
                '- [ ] real step twelve\n'
                '- [ ] real step thirteen\n'
                '- [ ] real step fourteen\n'
                '- [ ] real step fifteen\n'
            )
            broken_plan = (
                '# Plan\n\n'
                '### [x] Checkpoint 1: Done\n- [x] step a\n\n'
                '### [x] Checkpoint 2: Current\n'
                '- [x] real step one\n'
                '- [ ] real step two\n'
                '- [ ] real step three\n'
            )
            _write_plan(plan_path, initial_plan)
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'Work.'},
            )

            def runner(argv, **kwargs):
                _write_plan(plan_path, broken_plan)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            error_msg = str(ctx.value)
            assert 'Checkpoint 2: Current' in error_msg
            assert 'current checkpoint unchecked step count: 2' in error_msg
            assert 'current checkpoint unchecked step count: 15' not in error_msg
            run_dir = ctx.value.run_dir
            assert run_dir is not None
            run_json = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            assert 'current checkpoint unchecked step count: 2' in run_json['failure_reason']
            assert 'current checkpoint unchecked step count: 15' not in run_json['failure_reason']


class RunlogSingleRunDirTests(unittest.TestCase):

    def test_single_run_dir_for_multistep_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_multistep_wf_config()
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] >= 3:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=10),
                wf_config, 'loop', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            runs_root = repo_root / '.aflow' / 'runs'
            run_dirs = [d for d in runs_root.iterdir() if d.is_dir()]
            assert len(run_dirs) == 1, f"Expected exactly 1 run dir, got {len(run_dirs)}"
            run_dir = run_dirs[0]
            assert run_dir == result.run_dir
            turns_dir = run_dir / 'turns'
            turn_dirs = sorted(turns_dir.iterdir())
            assert len(turn_dirs) >= 2
            assert (run_dir / 'turns' / 'turn-001').is_dir()
            assert (run_dir / 'turns' / 'turn-002').is_dir()

    def test_turn_start_artifacts_written_under_single_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_multistep_wf_config()
            observed_run_dirs: list[int] = []
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                runs_root = repo_root / '.aflow' / 'runs'
                if runs_root.exists():
                    dirs = [d for d in runs_root.iterdir() if d.is_dir()]
                    observed_run_dirs.append(len(dirs))
                    current_dir = dirs[0]
                    turn_dir = current_dir / 'turns' / f'turn-{call_count[0]:03d}'
                    assert turn_dir.is_dir(), f"turn-start dir should exist before harness completes: {turn_dir}"
                    assert (turn_dir / 'user-prompt.txt').is_file()
                if call_count[0] >= 3:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=10),
                wf_config, 'loop', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            assert all(n == 1 for n in observed_run_dirs), \
                f"Run dir count changed during turns: {observed_run_dirs}"

    def test_no_sibling_run_dir_with_empty_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_multistep_wf_config()
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                runs_root = repo_root / '.aflow' / 'runs'
                for run_dir in runs_root.iterdir():
                    if run_dir.is_dir():
                        turns_dir = run_dir / 'turns'
                        turns_content = list(turns_dir.iterdir()) if turns_dir.exists() else []
                        assert turns_dir.exists(), f"turns/ should exist in {run_dir}"
                if call_count[0] >= 3:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=10),
                wf_config, 'loop', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            runs_root = repo_root / '.aflow' / 'runs'
            run_dirs = [d for d in runs_root.iterdir() if d.is_dir()]
            assert len(run_dirs) == 1


class WorkflowArtifactTests(unittest.TestCase):

    def test_run_json_includes_workflow_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END'),))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            result = run_workflow(ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3), wf_config, 'simple', config_dir=config_dir)
            run_dir = result.run_dir
            run_json = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['workflow_name'] == 'simple'
            assert run_json['original_plan_path'] == str(plan_path)
            assert run_json['status'] == 'completed'
            assert run_json['end_reason'] == 'already_complete'
            assert run_json['selected_start_step'] is None
            assert run_json['startup_recovery_used'] is False
            assert run_json['startup_recovery_reason'] is None
            assert 'issues_summary_path' not in run_json
            assert not (run_dir / 'issues.md').exists()

    def test_turn_artifacts_include_workflow_step_and_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')))}, first_step='implement_plan')},
                prompts={'p': 'Work.'},
            )
            result = run_workflow(ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5), wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            turn_dir = result.run_dir / 'turns' / 'turn-001'
            result_json = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
            assert result_json['step_name'] == 'implement_plan'
            assert result_json['step_role'] == 'architect'
            assert result_json['selector'] == 'codex.default'
            assert result_json['conditions']['DONE'] == True
            assert result_json['conditions']['NEW_PLAN_EXISTS'] == False
            assert result_json['chosen_transition'] == 'END'
            assert result_json['chosen_transition_condition'] == 'DONE || MAX_TURNS_REACHED'
            assert result_json['end_reason'] == 'done'

    def test_turn_artifacts_include_plan_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n- [ ] step two\n')

            def runner(argv, **kwargs):
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n- [x] step two\n')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            result = run_workflow(ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5), wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            turn_dir = result.run_dir / 'turns' / 'turn-001'
            result_json = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
            assert result_json['original_plan_path'] == str(plan_path)
            assert 'active_plan_path' in result_json
            assert 'new_plan_path' in result_json

    def test_issue_summary_is_persisted_for_failed_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, 'stdout failure', 'stderr failure')

            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END'),))}, first_step='implement_plan')},
                prompts={'p': 'Work.'},
            )

            with pytest.raises(WorkflowError):
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                    wf_config,
                    'simple',
                    config_dir=config_dir,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_dir = run_dirs[0]
            run_json = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            assert run_json['issues_summary_path'] == f".aflow/runs/{run_dir.name}/issues.md"

            issues_md = (run_dir / 'issues.md').read_text(encoding='utf-8')
            assert 'run.json' in issues_md
            assert 'turns/turn-001/result.json' in issues_md
            assert 'turns/turn-001/stdout.txt' in issues_md
            assert 'turns/turn-001/stderr.txt' in issues_md

            turn_result = json.loads((run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['issues_summary_path'] == run_json['issues_summary_path']
            assert turn_result['status'] == 'harness-failed'

    def test_turn_directory_exists_before_harness_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'Work.'},
            )

            def runner(argv, **kwargs):
                runs_root = repo_root / '.aflow' / 'runs'
                run_dirs = sorted(runs_root.iterdir())
                assert len(run_dirs) == 1
                turn_dir = run_dirs[0] / 'turns' / 'turn-001'
                assert turn_dir.is_dir()
                for filename in ('system-prompt.txt', 'user-prompt.txt', 'effective-prompt.txt', 'argv.json', 'env.json', 'result.json'):
                    assert (turn_dir / filename).exists()
                start_result = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
                assert start_result['status'] == 'starting'
                assert start_result['snapshot_after'] is None
                assert 'stdout' not in start_result
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                wf_config,
                'simple',
                config_dir=config_dir,
                adapter=CodexAdapter(),
                runner=runner,
            )
            assert result.turns_completed == 1
            turn_dir = result.run_dir / 'turns' / 'turn-001'
            final_result = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
            assert final_result['status'] == 'completed'
            assert final_result['returncode'] == 0
            assert final_result['stdout'] == 'ok'
            assert final_result['stderr'] == ''
            assert (turn_dir / 'stdout.txt').read_text(encoding='utf-8') == 'ok'
            assert (turn_dir / 'stderr.txt').read_text(encoding='utf-8') == ''

    def test_turn_artifacts_finalize_on_harness_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'Work.'},
            )

            def runner(argv, **kwargs):
                runs_root = repo_root / '.aflow' / 'runs'
                run_dirs = sorted(runs_root.iterdir())
                assert len(run_dirs) == 1
                turn_dir = run_dirs[0] / 'turns' / 'turn-001'
                assert turn_dir.is_dir()
                return subprocess.CompletedProcess(argv, 1, 'bad', 'err')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    wf_config,
                    'simple',
                    config_dir=config_dir,
                    adapter=CodexAdapter(),
                    runner=runner,
                )
            turn_dir = ctx.value.run_dir / 'turns' / 'turn-001'
            result_json = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
            assert result_json['status'] == 'harness-failed'
            assert result_json['returncode'] == 1
            assert result_json['stdout'] == 'bad'
            assert result_json['stderr'] == 'err'
            assert (turn_dir / 'stdout.txt').read_text(encoding='utf-8') == 'bad'
            assert (turn_dir / 'stderr.txt').read_text(encoding='utf-8') == 'err'

    def test_harness_recovery_retries_same_step_after_delay_and_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='retry_same_team_after_delay',
                            match=('throttled',),
                            delay_seconds=0,
                        ),),
                    ),
                ),
            )

            call_count = {'count': 0}

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'throttled\n')
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=4),
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
            )

            assert result.turns_completed == 2
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['recovery_summary']['action'] == 'retry_same_team_after_delay'
            assert run_json['recovery_summary']['source'] == 'deterministic'
            assert run_json['recovery_history'][0]['match_terms'] == ['throttled']
            turn1_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['status'] == 'recovery-scheduled'
            assert turn1_result['recovery_action'] == 'retry_same_team_after_delay'
            assert turn1_result['recovery_source'] == 'deterministic'
            assert turn1_result['recovery_match_terms'] == ['throttled']
            turn2_result = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn2_result['status'] == 'completed'

    def test_zero_exit_matched_error_with_progress_does_not_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='retry_same_team_after_delay',
                            match=('please try again',),
                            delay_seconds=0,
                        ),),
                    ),
                ),
            )

            def runner(argv, **kwargs):
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, 'please try again\n', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
            )

            assert result.turns_completed == 1
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert 'recovery_summary' not in run_json
            turn_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['status'] == 'completed'
            assert 'recovery_action' not in turn_result

    def test_fail_immediately_recovery_fails_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='fail_immediately',
                            match=('quota exhausted',),
                        ),),
                    ),
                ),
            )

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, '', 'quota exhausted\n')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config,
                    'simple',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            run_json = json.loads((ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            assert run_json['recovery_summary']['action'] == 'fail_immediately'
            assert 'quota exhausted' in run_json['failure_reason']
            turn_result = json.loads((ctx.value.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['status'] == 'recovery-failed'
            assert turn_result['recovery_action'] == 'fail_immediately'

    def test_team_lead_recovery_executes_valid_json_decision_and_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(team_lead='senior_architect'),
                roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'}),
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4'), 'lead': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(),
                    ),
                ),
            )

            prompts: list[str] = []
            call_count = {'count': 0}

            class TrackingAdapter(CodexAdapter):
                def build_invocation(self, repo_root, model, system_prompt, user_prompt, effort=None):
                    prompts.append(user_prompt)
                    return super().build_invocation(
                        repo_root=repo_root,
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'mystery failure\n')
                if call_count['count'] == 2:
                    assert 'aflow-harness-recovery-lead' in prompts[-1]
                    assert 'Return exactly one JSON object' in prompts[-1]
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps({
                            'action': 'retry_same_team_after_delay',
                            'delay_seconds': None,
                            'reason': 'retry the same team once after inspecting the failure',
                            'suggested_keywords': ['mystery failure', 'retry after failure'],
                            'suggested_action': None,
                        }) + '\n',
                        '',
                    )
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=4),
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=TrackingAdapter(),
                runner=runner,
            )

            assert call_count['count'] == 3
            assert result.turns_completed == 2
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['recovery_summary']['source'] == 'team_lead'
            assert run_json['recovery_summary']['action'] == 'retry_same_team_after_delay'
            assert run_json['recovery_summary']['delay_seconds'] is None
            assert run_json['recovery_summary']['suggested_keywords'] == ['mystery failure', 'retry after failure']
            assert run_json['recovery_summary']['suggested_action'] is None
            assert run_json['recovery_summary']['executed'] is True
            turn1_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['status'] == 'recovery-scheduled'
            assert turn1_result['recovery_source'] == 'team_lead'
            assert turn1_result['recovery_action'] == 'retry_same_team_after_delay'
            assert turn1_result['recovery_suggested_keywords'] == ['mystery failure', 'retry after failure']
            assert turn1_result['recovery_executed'] is True
            turn2_result = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn2_result['status'] == 'completed'

    def test_zero_exit_no_match_no_progress_does_not_escalate_to_team_lead_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(team_lead='senior_architect'),
                roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'}),
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4'), 'lead': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(rules=()),
                ),
            )

            prompts: list[str] = []
            call_count = {'count': 0}

            class TrackingAdapter(CodexAdapter):
                def build_invocation(self, repo_root, model, system_prompt, user_prompt, effort=None):
                    prompts.append(user_prompt)
                    return super().build_invocation(
                        repo_root=repo_root,
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 0, 'steady but unchanged\n', '')
                if call_count['count'] == 2:
                    _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                    return subprocess.CompletedProcess(argv, 0, 'ok\n', '')
                raise AssertionError(f'unexpected harness invocation #{call_count["count"]}')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=4),
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=TrackingAdapter(),
                runner=runner,
            )

            assert call_count['count'] == 2
            assert result.turns_completed == 2
            assert all('aflow-harness-recovery-lead' not in prompt for prompt in prompts)
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'completed'
            assert 'recovery_summary' not in run_json
            turn1_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['status'] == 'running'
            assert 'recovery_source' not in turn1_result
            turn2_result = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn2_result['status'] == 'completed'
            assert 'recovery_source' not in turn2_result

    def test_team_lead_recovery_rejects_invalid_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(team_lead='senior_architect'),
                roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'}),
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4'), 'lead': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='retry_same_team_after_delay',
                            match=('throttled',),
                        ),),
                    ),
                ),
            )

            call_count = {'count': 0}

            class TrackingAdapter(CodexAdapter):
                def build_invocation(self, repo_root, model, system_prompt, user_prompt, effort=None):
                    return super().build_invocation(
                        repo_root=repo_root,
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'mystery failure\n')
                return subprocess.CompletedProcess(argv, 0, 'this is not json\n', '')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config,
                    'simple',
                    config_dir=repo_root,
                    adapter=TrackingAdapter(),
                    runner=runner,
                )

            assert call_count['count'] == 2
            assert 'team lead recovery response was not valid JSON' in str(ctx.value)
            run_json = json.loads((ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            turn1_result = json.loads((ctx.value.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['status'] == 'recovery-failed'
            assert 'team lead recovery response was not valid JSON' in turn1_result['error']

    def test_team_lead_recovery_rejects_extra_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(team_lead='senior_architect'),
                roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'}),
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4'), 'lead': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='retry_same_team_after_delay',
                            match=('throttled',),
                        ),),
                    ),
                ),
            )

            call_count = {'count': 0}

            class TrackingAdapter(CodexAdapter):
                def build_invocation(self, repo_root, model, system_prompt, user_prompt, effort=None):
                    return super().build_invocation(
                        repo_root=repo_root,
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'mystery failure\n')
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({
                        'action': 'retry_same_team_after_delay',
                        'delay_seconds': None,
                        'reason': 'retry the same team once after inspecting the failure',
                        'suggested_keywords': ['mystery failure', 'retry after failure'],
                        'suggested_action': None,
                        'extra_field': 'not allowed',
                    }) + '\n',
                    '',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config,
                    'simple',
                    config_dir=repo_root,
                    adapter=TrackingAdapter(),
                    runner=runner,
                )

            assert call_count['count'] == 2
            assert 'unexpected keys' in str(ctx.value)
            run_json = json.loads((ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            turn1_result = json.loads((ctx.value.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['status'] == 'recovery-failed'
            assert 'unexpected keys' in turn1_result['error']

    def test_team_lead_recovery_surfaces_handoff_process_failure_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(team_lead='senior_architect'),
                roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary', 'senior_architect': 'codex.lead'}),
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4'), 'lead': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(rules=()),
                ),
            )

            call_count = {'count': 0}

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'mystery failure\n')
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    '',
                    'ThrottlingException: 5-minute credit limit exceeded\n',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config,
                    'simple',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            assert call_count['count'] == 2
            assert 'team lead recovery handoff failed with exit code 1' in str(ctx.value)
            assert 'ThrottlingException: 5-minute credit limit exceeded' in str(ctx.value)
            run_json = json.loads((ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            turn1_result = json.loads((ctx.value.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['status'] == 'recovery-failed'
            assert 'team lead recovery handoff failed with exit code 1' in turn1_result['error']
            assert 'ThrottlingException: 5-minute credit limit exceeded' in turn1_result['error']

    def test_harness_recovery_chains_backup_team_over_multiple_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.primary'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4'), 'backup': HarnessProfileConfig(model='gpt-5.4'), 'backup2': HarnessProfileConfig(model='gpt-5.4')})},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary'}, backup_team='backup'),
                    'backup': TeamConfig(roles={'architect': 'codex.backup'}, backup_team='backup2'),
                    'backup2': TeamConfig(roles={'architect': 'codex.backup2'}),
                },
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='switch_to_backup_team_and_retry',
                            match=('capacity exhausted',),
                        ),),
                    ),
                ),
            )

            call_count = {'count': 0}

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'capacity exhausted\n')
                if call_count['count'] == 2:
                    return subprocess.CompletedProcess(argv, 1, '', 'capacity exhausted\n')
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=4),
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
            )

            assert result.turns_completed == 3
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['team'] == 'primary'
            assert run_json['recovery_summary']['to_team'] == 'backup2'
            turn1_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['recovery_to_team'] == 'backup'
            turn2_result = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn2_result['recovery_to_team'] == 'backup2'
            turn3_result = json.loads((result.run_dir / 'turns' / 'turn-003' / 'result.json').read_text(encoding='utf-8'))
            assert turn3_result['selector'] == 'codex.backup2'

    def test_harness_recovery_resets_to_original_team_after_successful_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n\n### [ ] Checkpoint 2: Second\n- [ ] step two\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.primary'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4'), 'backup': HarnessProfileConfig(model='gpt-5.4')})},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary'}, backup_team='backup'),
                    'backup': TeamConfig(roles={'architect': 'codex.backup'}),
                },
                workflows={'simple': WorkflowConfig(
                    steps={
                        'step1': WorkflowStepConfig(
                            role='architect',
                            prompts=('p',),
                            go=(GoTransition(to='step2', when='DONE'), GoTransition(to='step1')),
                        ),
                        'step2': WorkflowStepConfig(
                            role='architect',
                            prompts=('p',),
                            go=(GoTransition(to='END', when='DONE'), GoTransition(to='step2')),
                        ),
                    },
                    first_step='step1',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='switch_to_backup_team_and_retry',
                            match=('capacity exhausted',),
                        ),),
                    ),
                ),
            )

            call_count = {'count': 0}

            def runner(argv, **kwargs):
                call_count['count'] += 1
                if call_count['count'] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'capacity exhausted\n')
                if call_count['count'] == 2:
                    _write_plan(
                        plan_path,
                        '# Plan\n\n'
                        '### [x] Checkpoint 1: First\n'
                        '- [x] step one\n\n'
                        '### [x] Checkpoint 2: Second\n'
                        '- [x] step two\n',
                    )
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                _write_plan(
                    plan_path,
                    '# Plan\n\n'
                    '### [x] Checkpoint 1: First\n'
                    '- [x] step one\n\n'
                    '### [x] Checkpoint 2: Second\n'
                    '- [x] step two\n',
                )
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
            )

            assert result.turns_completed == 3
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['team'] == 'primary'
            assert run_json['recovery_summary']['to_team'] == 'backup'
            turn1_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['recovery_to_team'] == 'backup'
            turn2_result = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn2_result['selector'] == 'codex.backup'
            turn3_result = json.loads((result.run_dir / 'turns' / 'turn-003' / 'result.json').read_text(encoding='utf-8'))
            assert turn3_result['selector'] == 'codex.primary'

    def test_terminal_backup_recovery_uses_original_team_for_merge_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _git_commit_file(repo_root, plan_path)
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(team_lead='senior_architect', worktree_root=str(worktree_root)),
                roles={
                    'architect': 'codex.teamA_step',
                    'senior_architect': 'codex.teamA_lead',
                },
                teams={
                    'teamA': TeamConfig(
                        roles={
                            'architect': 'codex.teamA_step',
                            'senior_architect': 'codex.teamA_lead',
                        },
                        backup_team='teamB',
                    ),
                    'teamB': TeamConfig(
                        roles={
                            'architect': 'codex.teamB_step',
                            'senior_architect': 'codex.teamB_lead',
                        },
                    ),
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'teamA_step': HarnessProfileConfig(model='teamA-step-model'),
                    'teamA_lead': HarnessProfileConfig(model='teamA-lead-model'),
                    'teamB_step': HarnessProfileConfig(model='teamB-step-model'),
                    'teamB_lead': HarnessProfileConfig(model='teamB-lead-model'),
                })},
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    setup=('worktree', 'branch'),
                    teardown=('merge', 'rm_worktree'),
                    main_branch='main',
                    team='teamA',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='switch_to_backup_team_and_retry',
                            match=('capacity exhausted',),
                        ),),
                    ),
                ),
            )

            models: list[str | None] = []
            call_count: list[int] = [0]

            class TrackingAdapter:
                name = 'codex'
                supports_effort = True

                def build_invocation(self, *, repo_root, model, system_prompt, user_prompt, effort=None):
                    models.append(model)
                    from aflow.harnesses.codex import CodexAdapter as CA
                    return CA().build_invocation(
                        repo_root=repo_root,
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                exec_plan = cwd / plan_path.relative_to(repo_root)
                if call_count[0] == 1:
                    return subprocess.CompletedProcess(argv, 1, '', 'capacity exhausted\n')
                if call_count[0] == 2:
                    _write_plan(
                        exec_plan,
                        '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n',
                    )
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'merged', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=TrackingAdapter(),
                runner=runner,
            )

            assert call_count[0] == 2
            assert models == ['teamA-step-model', 'teamB-step-model']
            assert result.turns_completed == 2
            assert result.final_snapshot.is_complete
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['team'] == 'teamA'
            assert run_json['recovery_summary']['from_team'] == 'teamA'
            assert run_json['recovery_summary']['to_team'] == 'teamB'
            turn1_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['recovery_to_team'] == 'teamB'
            turn2_result = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn2_result['selector'] == 'codex.teamB_step'

    def test_missing_backup_team_boundary_fails_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.primary'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'primary': HarnessProfileConfig(model='gpt-5.4')})},
                teams={
                    'primary': TeamConfig(roles={'architect': 'codex.primary'}),
                },
                workflows={'simple': WorkflowConfig(
                    steps={'implement_plan': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')),
                    )},
                    first_step='implement_plan',
                    team='primary',
                )},
                prompts={'p': 'Work.'},
                error_handling=ErrorHandlingConfig(
                    harness_error_recovery=HarnessErrorRecoveryConfig(
                        rules=(HarnessErrorRecoveryRuleConfig(
                            action='switch_to_backup_team_and_retry',
                            match=('capacity exhausted',),
                        ),),
                    ),
                ),
            )

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, '', 'capacity exhausted\n')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config,
                    'simple',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            assert 'backup_team' in str(ctx.value)
            run_json = json.loads((ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            assert run_json['recovery_summary']['to_team'] is None
            turn_result = json.loads((ctx.value.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['status'] == 'recovery-failed'
            assert turn_result['recovery_to_team'] is None

    def test_run_workflow_moves_completed_plan_to_done_on_terminal_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n\n### [ ] Checkpoint 2: Review\n- [ ] reviewer step\n')
            wf_config = WorkflowUserConfig(
                roles={'reviewer': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'review_implementation': WorkflowStepConfig(
                        role='reviewer',
                        prompts=('p',),
                        go=(GoTransition(to='END'),),
                    )},
                    first_step='review_implementation',
                )},
                prompts={'p': 'Review.'},
            )

            def runner(argv, **kwargs):
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n\n### [x] Checkpoint 2: Review\n- [x] reviewer step\n')
                return subprocess.CompletedProcess(argv, 0, 'approved', '')

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=2,
                    start_step='review_implementation',
                ),
                wf_config,
                'simple',
                config_dir=config_dir,
                adapter=CodexAdapter(),
                runner=runner,
            )

            assert result.turns_completed == 1
            assert result.end_reason == 'transition_end'
            assert result.final_snapshot.is_complete is True
            done_path = repo_root / 'plans' / 'done' / 'plan.md'
            assert done_path.is_file()
            assert not plan_path.exists()
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'completed'
            assert Path(run_json['original_plan_path']).resolve() == done_path.resolve()
            turn_result = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['status'] == 'completed'
            assert Path(turn_result['original_plan_path']).resolve() == plan_path.resolve()
            assert Path(turn_result['active_plan_path']).resolve() == plan_path.resolve()

    def test_run_workflow_rejects_agent_moving_original_plan_mid_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n\n### [ ] Checkpoint 2: Review\n- [ ] reviewer step\n')
            wf_config = WorkflowUserConfig(
                roles={'reviewer': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'simple': WorkflowConfig(
                    steps={'review_implementation': WorkflowStepConfig(
                        role='reviewer',
                        prompts=('p',),
                        go=(GoTransition(to='END'),),
                    )},
                    first_step='review_implementation',
                )},
                prompts={'p': 'Review.'},
            )

            def runner(argv, **kwargs):
                _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n\n### [x] Checkpoint 2: Review\n- [x] reviewer step\n')
                move_completed_plan_to_done(repo_root, plan_path)
                return subprocess.CompletedProcess(argv, 0, 'approved', '')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=2,
                        start_step='review_implementation',
                    ),
                    wf_config,
                    'simple',
                    config_dir=config_dir,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            assert 'workflow-owned finalization requires agents to keep the original plan under plans/in-progress until terminal success' in str(ctx.value)
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'

    def test_run_json_records_workflow_step_on_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, 'noop', '')
            wf_config = WorkflowUserConfig(roles={'architect': 'codex.default'}, harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})}, workflows={'simple': WorkflowConfig(steps={'implement_plan': WorkflowStepConfig(role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE'), GoTransition(to='implement_plan')))}, first_step='implement_plan')}, prompts={'p': 'Work.'})
            with pytest.raises(WorkflowError):
                run_workflow(ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2), wf_config, 'simple', config_dir=config_dir, adapter=CodexAdapter(), runner=runner)
            run_dir = repo_root / '.aflow' / 'runs'
            run_dirs = sorted(run_dir.iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['workflow_name'] == 'simple'
            assert run_json['current_step_name'] == 'implement_plan'


class WorkflowEndToEndTests(unittest.TestCase):

    def test_already_complete_workflow_reports_success_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }]\n\n[prompts]\np = "Work."\n')
            plan_path = tmp_path / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            count_file = tmp_path / 'count.txt'
            result = _run_workflow_launcher(repo_root, str(plan_path), env=_workflow_test_env(repo_root, scenario='noop', plan_path=plan_path, count_file=count_file, home_dir=home_dir))
            assert result.returncode == 0
            assert result.stdout.strip() == "Workflow 'simple' completed after 0 turns because the original plan was already complete."
            assert not count_file.exists()
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['end_reason'] == 'already_complete'
            assert run_json['turns_completed'] == 0

    def test_simple_workflow_completion_on_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "implement_plan" },\n]\n\n[prompts]\np = "Work from {ACTIVE_PLAN_PATH}."\n')
            plan_path = tmp_path / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = tmp_path / 'count.txt'
            original_plan_text = '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n'
            _write_plan(plan_path, original_plan_text)
            _write_plan(completed_plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            result = _run_workflow_launcher(repo_root, '--max-turns', '1', str(plan_path), env=_workflow_test_env(repo_root, scenario='complete', plan_path=plan_path, count_file=count_file, home_dir=home_dir, completed_plan_path=completed_plan_path))
            assert result.returncode == 0
            assert result.stdout.strip() == "Workflow 'simple' completed after 1 turn because DONE evaluated true."
            backup_path = repo_root / 'plans' / 'backups' / 'plan.md'
            assert backup_path.exists()
            assert backup_path.read_text(encoding='utf-8') == original_plan_text
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'completed'
            assert run_json['workflow_name'] == 'simple'
            assert run_json['turns_completed'] == 1
            assert run_json['end_reason'] == 'done'
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['end_reason'] == 'done'

    def test_kiro_workflow_invokes_chat_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[harness.kiro.profiles.default]\nmodel = "kiro-model"\n\n[roles]\narchitect = "kiro.default"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "implement_plan" },\n]\n\n[prompts]\np = "Work from {ACTIVE_PLAN_PATH}."\n')
            plan_path = tmp_path / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_plan(completed_plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            _write_workflow_harness_script(repo_root, 'kiro-cli')
            result = _run_workflow_launcher(repo_root, str(plan_path), env=_workflow_test_env(repo_root, scenario='complete', plan_path=plan_path, count_file=count_file, home_dir=home_dir, completed_plan_path=completed_plan_path))
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'completed'
            assert run_json['turns_completed'] == 1
            assert run_json['end_reason'] == 'done'
            turn_dir = run_dirs[0] / 'turns' / 'turn-001'
            turn_result = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['selector'] == 'kiro.default'
            assert turn_result['end_reason'] == 'done'
            argv_json = json.loads((turn_dir / 'argv.json').read_text(encoding='utf-8'))
            assert argv_json['argv'][:4] == ['kiro-cli', 'chat', '--no-interactive', '--trust-all-tools']

    def test_reviewer_created_plan_becomes_active_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "loop"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.loop.steps.review]\nrole = "architect"\nprompts = ["review_p"]\ngo = [{ to = "implement" }]\n\n[workflow.loop.steps.implement]\nrole = "architect"\nprompts = ["impl_p"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "review" },\n]\n\n[prompts]\nreview_p = "Active: {ACTIVE_PLAN_PATH}. New: {NEW_PLAN_PATH}."\nimpl_p = "Active: {ACTIVE_PLAN_PATH}."\n')
            plan_path = tmp_path / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_plan(completed_plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            call_count = [0]

            def count_env():
                nonlocal call_count
                call_count[0] += 1
                new_plan = plan_path.parent / 'plan-cp01-v01.md'
                scenario = 'create_plan' if call_count[0] == 1 else 'complete'
                return _workflow_test_env(repo_root, scenario=scenario, plan_path=plan_path, count_file=count_file, home_dir=home_dir, completed_plan_path=completed_plan_path, new_plan_path=new_plan if call_count[0] == 1 else None)
            result = _run_workflow_launcher(repo_root, '--max-turns', '5', '--start-step', 'review', str(plan_path), env=count_env())
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'completed'
            assert run_json['turns_completed'] == 2
            assert run_json['end_reason'] == 'done'
            turn2_result = json.loads((run_dirs[0] / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert Path(turn2_result['active_plan_path']).resolve() == (plan_path.parent / 'plan-cp01-v01.md').resolve()
            assert turn2_result['end_reason'] == 'done'

    def test_reviewer_without_generated_plan_keeps_active_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "loop"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.loop.steps.review]\nrole = "architect"\nprompts = ["review_p"]\ngo = [{ to = "implement" }]\n\n[workflow.loop.steps.implement]\nrole = "architect"\nprompts = ["impl_p"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "review" },\n]\n\n[prompts]\nreview_p = "Active: {ACTIVE_PLAN_PATH}."\nimpl_p = "Active: {ACTIVE_PLAN_PATH}."\n')
            plan_path = tmp_path / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n- [ ] step two\n')
            _write_plan(completed_plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n- [x] step two\n')
            _write_workflow_harness_script(repo_root, 'codex')
            result = _run_workflow_launcher(repo_root, '--max-turns', '4', '--start-step', 'review', str(plan_path), env=_workflow_test_env(repo_root, scenario='noop', plan_path=plan_path, count_file=count_file, home_dir=home_dir, completed_plan_path=completed_plan_path))
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'completed'
            assert run_json['turns_completed'] == 4
            assert run_json['end_reason'] == 'max_turns_reached'
            for turn_dir in sorted((run_dirs[0] / 'turns').iterdir()):
                turn_result = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
                assert Path(turn_result['active_plan_path']).resolve() == plan_path.resolve()
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-004' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['end_reason'] == 'max_turns_reached'

    def test_max_turns_routes_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "implement_plan" },\n]\n\n[prompts]\np = "Work."\n')
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            result = _run_workflow_launcher(repo_root, '--max-turns', '3', str(plan_path), env=_workflow_test_env(repo_root, scenario='noop', plan_path=plan_path, count_file=count_file, home_dir=home_dir))
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'completed'
            assert run_json['turns_completed'] == 3
            assert run_json['end_reason'] == 'max_turns_reached'
            assert result.stdout.strip() == "Workflow 'simple' completed after 3 turns because MAX_TURNS_REACHED matched."
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-003' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['end_reason'] == 'max_turns_reached'
            assert turn_result['status'] == 'running'

    def test_team_override_takes_precedence_and_falls_back_to_global_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_split_config(
                home_dir,
                textwrap.dedent('''\
                    [aflow]
                    default_workflow = "loop"
                    max_turns = 2

                    [harness.codex.profiles.default]
                    model = "gpt-5.4"

                    [harness.gemini.profiles.fast]
                    model = "gemini-2.0"

                    [harness.opencode.profiles.default]
                    model = "glm-5"

                    [harness.claude.profiles.default]
                    model = "claude-3"

                    [roles]
                    architect = "codex.default"
                    senior_architect = "opencode.default"

                    [teams.pi]
                    architect = "claude.default"
                    senior_architect = "claude.default"

                    [teams.7teen]
                    architect = "gemini.fast"

                    [prompts]
                    review_p = "Review {ACTIVE_PLAN_PATH}."
                    impl_p = "Implement {ACTIVE_PLAN_PATH}."
                '''),
                textwrap.dedent('''\
                    [workflow.loop]
                    team = "pi"

                    [workflow.loop.steps.review]
                    role = "architect"
                    prompts = ["review_p"]
                    go = [{ to = "implement" }]

                    [workflow.loop.steps.implement]
                    role = "senior_architect"
                    prompts = ["impl_p"]
                    go = [
                      { to = "END", when = "DONE || MAX_TURNS_REACHED" },
                      { to = "review" },
                    ]
                '''),
            )
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_workflow_harness_script(repo_root, 'gemini')
            _write_workflow_harness_script(repo_root, 'opencode')
            result = _run_workflow_launcher(
                repo_root,
                '--team', '7teen',
                str(plan_path),
                env=_workflow_test_env(
                    repo_root,
                    scenario='noop',
                    plan_path=plan_path,
                    count_file=count_file,
                    home_dir=home_dir,
                ),
            )
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['turns_completed'] == 2
            turn1_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            turn2_result = json.loads((run_dirs[0] / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['selector'] == 'gemini.fast'
            assert turn2_result['selector'] == 'opencode.default'
            assert turn1_result['step_role'] == 'architect'
            assert turn2_result['step_role'] == 'senior_architect'

    def test_workflow_team_applies_when_cli_team_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_split_config(
                home_dir,
                textwrap.dedent('''\
                    [aflow]
                    default_workflow = "simple"
                    max_turns = 1

                    [harness.codex.profiles.default]
                    model = "gpt-5.4"

                    [harness.claude.profiles.default]
                    model = "claude-3"

                    [roles]
                    architect = "codex.default"

                    [teams.pi]
                    architect = "claude.default"

                    [prompts]
                    p = "Work."
                '''),
                textwrap.dedent('''\
                    [workflow.simple]
                    team = "pi"

                    [workflow.simple.steps.implement_plan]
                    role = "architect"
                    prompts = ["p"]
                    go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }]
                '''),
            )
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_workflow_harness_script(repo_root, 'claude')
            result = _run_workflow_launcher(
                repo_root,
                str(plan_path),
                env=_workflow_test_env(
                    repo_root,
                    scenario='noop',
                    plan_path=plan_path,
                    count_file=count_file,
                    home_dir=home_dir,
                ),
            )
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['selector'] == 'claude.default'
            assert turn_result['step_role'] == 'architect'

    def test_unknown_team_is_rejected_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_split_config(
                home_dir,
                textwrap.dedent('''\
                    [aflow]
                    default_workflow = "simple"

                    [harness.codex.profiles.default]
                    model = "gpt-5.4"

                    [roles]
                    architect = "codex.default"

                    [prompts]
                    p = "Work."
                '''),
                textwrap.dedent('''\
                    [workflow.simple.steps.implement_plan]
                    role = "architect"
                    prompts = ["p"]
                    go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }]
                '''),
            )
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            result = _run_workflow_launcher(
                repo_root,
                '--team',
                'missing',
                str(plan_path),
                env=_workflow_test_env(
                    repo_root,
                    scenario='noop',
                    plan_path=plan_path,
                    count_file=count_file,
                    home_dir=home_dir,
                ),
            )
            assert result.returncode == 1
            assert "unknown team 'missing'" in result.stderr

    def test_config_max_turns_is_used_when_flag_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_split_config(
                home_dir,
                textwrap.dedent('''\
                    [aflow]
                    default_workflow = "simple"
                    max_turns = 2

                    [harness.codex.profiles.default]
                    model = "gpt-5.4"

                    [roles]
                    architect = "codex.default"

                    [prompts]
                    p = "Work."
                '''),
                textwrap.dedent('''\
                    [workflow.simple.steps.implement_plan]
                    role = "architect"
                    prompts = ["p"]
                    go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }, { to = "implement_plan" }]
                '''),
            )
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            result = _run_workflow_launcher(
                repo_root,
                str(plan_path),
                env=_workflow_test_env(
                    repo_root,
                    scenario='noop',
                    plan_path=plan_path,
                    count_file=count_file,
                    home_dir=home_dir,
                ),
            )
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['turns_completed'] == 2
            assert run_json['end_reason'] == 'max_turns_reached'

    def test_cli_max_turns_overrides_config_max_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_split_config(
                home_dir,
                textwrap.dedent('''\
                    [aflow]
                    default_workflow = "simple"
                    max_turns = 3

                    [harness.codex.profiles.default]
                    model = "gpt-5.4"

                    [roles]
                    architect = "codex.default"

                    [prompts]
                    p = "Work."
                '''),
                textwrap.dedent('''\
                    [workflow.simple.steps.implement_plan]
                    role = "architect"
                    prompts = ["p"]
                    go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }, { to = "implement_plan" }]
                '''),
            )
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            result = _run_workflow_launcher(
                repo_root,
                '--max-turns', '1',
                str(plan_path),
                env=_workflow_test_env(
                    repo_root,
                    scenario='noop',
                    plan_path=plan_path,
                    count_file=count_file,
                    home_dir=home_dir,
                ),
            )
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['turns_completed'] == 1
            assert run_json['end_reason'] == 'max_turns_reached'

    def test_launcher_numeric_start_step_matches_named_start_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi_step"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.multi_step.steps.review_plan]\nrole = "architect"\nprompts = ["review_prompt"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.multi_step.steps.implement_plan]\nrole = "architect"\nprompts = ["impl_prompt"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "review_plan" },\n]\n\n[prompts]\nreview_prompt = "Review."\nimpl_prompt = "Implement."\n')
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')

            # Run with numeric start-step index 2
            env_numeric = _workflow_test_env(repo_root, scenario='noop', plan_path=plan_path, count_file=count_file, home_dir=home_dir)
            result_numeric = _run_workflow_launcher(
                repo_root,
                '--max-turns', '1',
                '--start-step', '2',
                str(plan_path),
                env=env_numeric,
            )
            assert result_numeric.returncode == 0
            run_dirs_numeric = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs_numeric) == 1
            run_json_numeric = json.loads((run_dirs_numeric[0] / 'run.json').read_text(encoding='utf-8'))
            selected_step_numeric = run_json_numeric['selected_start_step']

            # Clean up runs directory
            import shutil
            shutil.rmtree(repo_root / '.aflow' / 'runs')

            # Run with named start-step
            env_named = _workflow_test_env(repo_root, scenario='noop', plan_path=plan_path, count_file=count_file, home_dir=home_dir)
            result_named = _run_workflow_launcher(
                repo_root,
                '--max-turns', '1',
                '--start-step', 'implement_plan',
                str(plan_path),
                env=env_named,
            )
            assert result_named.returncode == 0
            run_dirs_named = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs_named) == 1
            run_json_named = json.loads((run_dirs_named[0] / 'run.json').read_text(encoding='utf-8'))
            selected_step_named = run_json_named['selected_start_step']

            # Both should resolve to the same step
            assert selected_step_numeric == selected_step_named == 'implement_plan'


class WorkflowPreflightTests(unittest.TestCase):

    def _make_review_wf_config(self) -> WorkflowUserConfig:
        return WorkflowUserConfig(
            roles={'architect': 'codex.default'},
            harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m')})},
            workflows={'review_wf': WorkflowConfig(
                steps={'step1': WorkflowStepConfig(
                    role='architect',
                    prompts=('review_prompt',),
                    go=(GoTransition(to='END'),),
                )},
                first_step='step1',
            )},
            prompts={'review_prompt': "Use 'aflow-review-squash' skill."},
        )

    def test_preflight_fails_when_review_skill_and_no_git_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1)
            wf_config = self._make_review_wf_config()
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(config, wf_config, 'review_wf', config_dir=repo_root, runner=lambda *a, **k: None)
            assert 'Git Tracking' in str(ctx.value)

    def test_preflight_passes_when_review_skill_and_git_tracking_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n## Git Tracking\n\nBase: abc\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            wf_config = self._make_review_wf_config()
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1)
            result = run_workflow(config, wf_config, 'review_wf', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            assert result.turns_completed == 1

    def test_preflight_skipped_for_non_review_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n')
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m')})},
                workflows={'simple': WorkflowConfig(
                    steps={'impl': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END'),),
                    )},
                    first_step='impl',
                )},
                prompts={'p': "Use 'aflow-execute-plan' skill."},
            )
            config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1)
            result = run_workflow(config, wf_config, 'simple', config_dir=repo_root)
            assert result.end_reason == 'already_complete'

    def test_preflight_fails_for_git_tracking_only_inside_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n```\n## Git Tracking\n```\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1)
            wf_config = self._make_review_wf_config()
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(config, wf_config, 'review_wf', config_dir=repo_root, runner=lambda *a, **k: None)
            assert 'Git Tracking' in str(ctx.value)

    def test_preflight_auto_refreshes_pristine_base_head_before_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, textwrap.dedent(f'''\
                # Plan

                ## Git Tracking

                - Plan Branch: ``
                - Pre-Handoff Base HEAD: `{initial_head}`

                ### [ ] Checkpoint 1: First
                - [ ] step one
            '''))
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'advance head\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')

            wf_config = _make_simple_wf_config()
            call_count = [0]
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0

            def runner(argv, **kwargs):
                call_count[0] += 1
                text = (Path(kwargs['cwd']) / 'plan.md').read_text(encoding='utf-8')
                assert f'`{current_head}`' in text
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                wf_config, 'simple', config_dir=repo_root,
                runner=runner,
            )
            assert call_count[0] == 1
            assert f'`{current_head}`' in plan_path.read_text(encoding='utf-8')

    def test_preflight_blocks_started_handoff_base_head_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            plan_path = repo_root / 'plan.md'
            started_plan = _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`').replace(
                'Last Reviewed HEAD: `none`',
                'Last Reviewed HEAD: `abc123`',
            ).replace(
                '  - None yet.\n',
                '  - Reviewed checkpoint 1.\n',
            )
            _write_plan(plan_path, started_plan)
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'advance head\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')

            wf_config = _make_simple_wf_config()
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                    wf_config, 'simple', config_dir=repo_root,
                    runner=runner,
                )
            assert call_count[0] == 0
            assert 'startup preflight rejected Pre-Handoff Base HEAD state' in str(ctx.value)

    def test_preflight_blocks_started_handoff_with_empty_base_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, _initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            plan_path = repo_root / 'plan.md'
            started_plan = _VALID_GIT_TRACKING_PLAN.replace('`base`', '``').replace(
                'Last Reviewed HEAD: `none`',
                'Last Reviewed HEAD: `abc123`',
            ).replace(
                '  - None yet.\n',
                '  - Reviewed checkpoint 1.\n',
            )
            _write_plan(plan_path, started_plan)
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'advance head\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')

            wf_config = _make_simple_wf_config()
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                    wf_config, 'simple', config_dir=repo_root,
                    runner=runner,
                )
            assert call_count[0] == 0
            assert 'startup preflight rejected Pre-Handoff Base HEAD state' in str(ctx.value)

    def test_preflight_applies_base_head_refresh_before_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            plan_path = repo_root / 'plan.md'
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`'),
            )
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'advance head\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0

            wf_config = _make_simple_wf_config()
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                text = (cwd / 'plan.md').read_text(encoding='utf-8')
                assert f'`{current_head}`' in text
                updated = text.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                updated = updated.replace('- [ ] step one', '- [x] step one')
                (cwd / 'plan.md').write_text(updated, encoding='utf-8')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                wf_config, 'simple', config_dir=repo_root,
                runner=runner,
            )
            assert call_count[0] == 1
            assert result.turns_completed == 1
            assert f'`{current_head}`' in plan_path.read_text(encoding='utf-8')


class WorkflowLifecycleRuntimeTests(unittest.TestCase):

    def test_bootstrap_succeeds_for_unborn_main_branch_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            subprocess.run(['git', 'init', '-b', 'main'], cwd=str(repo_root), check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=str(repo_root), check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=str(repo_root), check=True, capture_output=True)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    (cwd / 'README.md').write_text('# Plan\n\nBootstrapped.\n', encoding='utf-8')
                    subprocess.run(['git', 'add', 'README.md'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(cwd), check=True, capture_output=True)
                    return subprocess.CompletedProcess(argv, 0, 'bootstrap ok', '')
                elif call_count[0] == 2:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                    return subprocess.CompletedProcess(argv, 0, 'merged', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            assert call_count[0] >= 2
            assert result.final_snapshot.is_complete

    def test_preflight_fails_when_not_on_main_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            subprocess.run(['git', 'checkout', '-b', 'other'], cwd=str(repo_root), check=True, capture_output=True)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                    wf_config, 'branch_wf', config_dir=repo_root,
                )
            assert 'main' in str(ctx.value)
            assert 'other' in str(ctx.value)

    def test_preflight_fails_when_main_branch_does_not_exist(self) -> None:
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

    def test_preflight_fails_when_worktree_plan_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = root / 'outside_plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            base_wf = _make_worktree_wf_config(worktree_root=str(worktree_root))
            wf_config = WorkflowUserConfig(
                aflow=base_wf.aflow,
                roles=base_wf.roles,
                harnesses=base_wf.harnesses,
                workflows=base_wf.workflows,
                prompts=base_wf.prompts,
            )
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                    wf_config, 'wt_wf', config_dir=repo_root,
                )
            assert 'primary repo root' in str(ctx.value)
            assert str(plan_path) in str(ctx.value)

    def test_worktree_accepts_untracked_original_plan(self) -> None:
        """Verify untracked plans are now accepted for worktree workflows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            exclude_path = repo_root / '.git' / 'info' / 'exclude'
            exclude_path.write_text(
                exclude_path.read_text(encoding='utf-8') + '\n/plans\n',
                encoding='utf-8',
            )
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, _VALID_PLAN)
            base_wf = _make_worktree_wf_config(worktree_root=str(worktree_root))
            wf_config = WorkflowUserConfig(
                aflow=base_wf.aflow,
                roles=base_wf.roles,
                harnesses=base_wf.harnesses,
                workflows=base_wf.workflows,
                prompts=base_wf.prompts,
            )
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    # In worktree: write to the translated plan path
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    # In primary root: do the merge
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            # Worktree workflow succeeded with untracked plan, proving the new sync support works
            assert call_count[0] >= 1

    def test_worktree_accepts_tracked_modified_original_plan(self) -> None:
        """Tracked plan edits in the primary checkout must not be misclassified as non-plan dirtiness."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, _VALID_PLAN)
            _git_force_commit_file(repo_root, plan_path)
            rc, committed_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{committed_head}`'),
            )
            base_wf = _make_worktree_no_merge_wf_config(worktree_root=str(worktree_root))
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(worktree_root=str(worktree_root)),
                roles=base_wf.roles,
                harnesses=base_wf.harnesses,
                workflows=base_wf.workflows,
                prompts=base_wf.prompts,
            )
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                exec_plan = cwd / plan_path.relative_to(repo_root)
                text = exec_plan.read_text(encoding='utf-8')
                assert '- Plan Branch: `main`' not in text
                updated = text.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                updated = updated.replace('- [ ] step one', '- [x] step one')
                _write_plan(exec_plan, updated)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )

            assert call_count[0] == 1

    def test_preflight_branch_only_refreshes_base_head_after_branch_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            plan_path = repo_root / 'plan.md'
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`'),
            )
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'advance head\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            wf_config = _make_branch_only_wf_config(main_branch='main')
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                rc, branch_name, _ = _run_git_in_test(['branch', '--show-current'], cwd=cwd)
                assert rc == 0
                text = (cwd / plan_path.relative_to(repo_root)).read_text(encoding='utf-8')
                if call_count[0] == 1:
                    assert branch_name != 'main'
                    assert f'`{current_head}`' in text
                    updated = text.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                    updated = updated.replace('- [ ] step one', '- [x] step one')
                    (cwd / plan_path.relative_to(repo_root)).write_text(updated, encoding='utf-8')
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                wf_config, 'branch_wf', config_dir=repo_root,
                runner=runner,
            )

            assert call_count[0] == 2
            assert result.turns_completed == 1
            assert f'`{current_head}`' in plan_path.read_text(encoding='utf-8')

    def test_preflight_worktree_refreshes_primary_and_execution_plan_before_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`'),
            )
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'advance head\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                exec_plan = cwd / plan_path.relative_to(repo_root)
                primary_plan = repo_root / plan_path.relative_to(repo_root)
                exec_text = exec_plan.read_text(encoding='utf-8')
                primary_text = primary_plan.read_text(encoding='utf-8')
                if call_count[0] == 1:
                    assert f'`{current_head}`' in primary_text
                    assert f'`{current_head}`' in exec_text
                    updated = exec_text.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                    updated = updated.replace('- [ ] step one', '- [x] step one')
                    exec_plan.write_text(updated, encoding='utf-8')
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                wf_config, 'wt_wf', config_dir=repo_root,
                runner=runner,
            )

            assert call_count[0] == 2
            assert result.turns_completed == 1
            assert f'`{current_head}`' in plan_path.read_text(encoding='utf-8')

    def test_worktree_rewrites_plan_branch_to_feature_branch_before_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, _VALID_GIT_TRACKING_PLAN.replace('`main`', '``', 1))
            base_wf = _make_worktree_no_merge_wf_config(worktree_root=str(worktree_root))
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(worktree_root=str(worktree_root)),
                roles=base_wf.roles,
                harnesses=base_wf.harnesses,
                workflows=base_wf.workflows,
                prompts=base_wf.prompts,
            )

            def runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                exec_plan = cwd / plan_path.relative_to(repo_root)
                rc, branch_name, _ = _run_git_in_test(['branch', '--show-current'], cwd=cwd)
                assert rc == 0
                text = exec_plan.read_text(encoding='utf-8')
                assert f'- Plan Branch: `{branch_name}`' in text
                assert '- Plan Branch: `main`' not in text
                updated = text.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                updated = updated.replace('- [ ] step one', '- [x] step one')
                _write_plan(exec_plan, updated)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )

            done_path = repo_root / 'plans' / 'done' / 'plan.md'
            primary_text = done_path.read_text(encoding='utf-8')
            assert '- Plan Branch: `main`' not in primary_text

    def test_worktree_syncs_original_plan_back_after_successful_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{current_head}`'),
            )
            base_wf = _make_worktree_no_merge_wf_config(worktree_root=str(worktree_root))
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(worktree_root=str(worktree_root)),
                roles=base_wf.roles,
                harnesses=base_wf.harnesses,
                workflows=base_wf.workflows,
                prompts=base_wf.prompts,
            )
            call_count = [0]
            marker = '  - synced marker\n'

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                exec_plan = cwd / plan_path.relative_to(repo_root)
                text = exec_plan.read_text(encoding='utf-8')
                if call_count[0] == 1:
                    updated = text.replace('  - None yet.\n', '  - None yet.\n' + marker)
                    _write_plan(exec_plan, updated)
                else:
                    assert marker in text
                    updated = text.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                    updated = updated.replace('- [ ] step one', '- [x] step one')
                    _write_plan(exec_plan, updated)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )

            assert call_count[0] == 2
            done_path = repo_root / 'plans' / 'done' / 'plan.md'
            assert marker in done_path.read_text(encoding='utf-8')

    def test_worktree_merge_restores_untracked_original_plan_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            exclude_path = repo_root / '.git' / 'info' / 'exclude'
            exclude_path.write_text(
                exclude_path.read_text(encoding='utf-8') + '\n/plans\n',
                encoding='utf-8',
            )
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{current_head}`'),
            )
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            call_count = [0]
            marker = '  - merged marker\n'

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    text = exec_plan.read_text(encoding='utf-8')
                    updated = text.replace('  - None yet.\n', '  - None yet.\n' + marker)
                    updated = updated.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                    updated = updated.replace('- [ ] step one', '- [x] step one')
                    _write_plan(exec_plan, updated)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )

            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert call_count[0] == 1
            done_path = repo_root / 'plans' / 'done' / 'plan.md'
            assert marker in done_path.read_text(encoding='utf-8')
            rc, _, _ = _run_git_in_test(['merge-base', '--is-ancestor', run_json['feature_branch'], 'main'], cwd=repo_root)
            assert rc == 0

    def test_worktree_merge_preserves_tracked_original_plan_sync_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{current_head}`'),
            )
            _git_force_commit_file(repo_root, plan_path)
            rc, committed_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{committed_head}`'),
            )
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            call_count = [0]
            marker = '  - tracked merge marker\n'

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    text = exec_plan.read_text(encoding='utf-8')
                    updated = text.replace('  - None yet.\n', '  - None yet.\n' + marker)
                    updated = updated.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                    updated = updated.replace('- [ ] step one', '- [x] step one')
                    _write_plan(exec_plan, updated)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )

            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert call_count[0] == 1
            done_path = repo_root / 'plans' / 'done' / 'plan.md'
            assert marker in done_path.read_text(encoding='utf-8')
            rc, _, _ = _run_git_in_test(['merge-base', '--is-ancestor', run_json['feature_branch'], 'main'], cwd=repo_root)
            assert rc == 0

    def test_worktree_syncs_plan_back_even_on_harness_failure(self) -> None:
        """Verify plan edits are synced back from worktree even when harness returns non-zero."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            exclude_path = repo_root / '.git' / 'info' / 'exclude'
            exclude_path.write_text(
                exclude_path.read_text(encoding='utf-8') + '\n/plans\n',
                encoding='utf-8',
            )
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))

            def runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                # Simulate harness that edits the plan but exits non-zero.
                exec_plan = cwd / plan_path.relative_to(repo_root)
                _write_plan(exec_plan, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 1, 'failed', 'error')

            with pytest.raises(WorkflowError):
                # First turn exits non-zero, plan is synced back before the exception.
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=runner,
                )

            # Plan edits were synced back to primary for restart correctness.
            assert _COMPLETE_PLAN in plan_path.read_text(encoding='utf-8')

    def test_worktree_sync_creates_parent_directories_in_worktree(self) -> None:
        """Verify sync-to-worktree creates parent directories if they don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            # Plan in deeply nested directory under plans/
            plan_path = repo_root / 'plans' / 'in-progress' / 'nested' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, _VALID_PLAN)
            _git_force_commit_file(repo_root, plan_path)  # Commit plan so merge doesn't fail on untracked file
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    # In worktree: write to the translated plan path
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    assert exec_plan.parent.exists(), "Parent directories should be created by sync-to-worktree"
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    # In primary root: do the merge
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            # Workflow succeeded with nested plan directories, proving sync creates parent dirs
            assert call_count[0] >= 1

    def test_preflight_fails_when_working_tree_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            (repo_root / 'dirty.txt').write_text('uncommitted\n', encoding='utf-8')
            wf_config = _make_branch_only_wf_config(main_branch='main')
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                    wf_config, 'branch_wf', config_dir=repo_root,
                )
            assert 'uncommitted changes' in str(ctx.value)

    def test_branch_only_setup_creates_feature_branch_and_uses_primary_as_exec_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            workflow_step_cwd: list[str] = []
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    workflow_step_cwd.append(str(cwd))
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            assert len(workflow_step_cwd) == 1
            assert workflow_step_cwd[0] == str(repo_root)
            rc, branches, _ = _run_git_in_test(['branch', '--list', 'aflow-*'], cwd=repo_root)
            assert rc == 0
            feature_branches = [b.strip().lstrip('+* ') for b in branches.splitlines() if b.strip()]
            assert len(feature_branches) == 1

    def test_branch_only_run_json_records_lifecycle_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['execution_repo_root'] == str(repo_root)
            assert run_json['main_branch'] == 'main'
            assert 'feature_branch' in run_json
            assert run_json['feature_branch'].startswith('aflow-')
            assert run_json['lifecycle_setup'] == ['branch']
            assert run_json['lifecycle_teardown'] == ['merge']
            assert 'worktree_path' not in run_json

    def test_worktree_setup_creates_worktree_and_uses_it_as_exec_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            workflow_step_cwd: list[str] = []
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    workflow_step_cwd.append(str(cwd))
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            assert len(workflow_step_cwd) == 1
            exec_root_path = Path(workflow_step_cwd[0])
            assert exec_root_path.resolve() != repo_root.resolve()
            assert exec_root_path.parent.resolve() == worktree_root.resolve()
            rc, _, _ = _run_git_in_test(['worktree', 'list', '--porcelain'], cwd=repo_root)
            assert rc == 0

    def test_worktree_run_json_records_lifecycle_context_with_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['repo_root'] == str(repo_root)
            assert 'execution_repo_root' in run_json
            assert run_json['execution_repo_root'] != str(repo_root)
            assert run_json['main_branch'] == 'main'
            assert 'feature_branch' in run_json
            assert 'worktree_path' in run_json
            assert run_json['worktree_path'] == run_json['execution_repo_root']
            assert run_json['lifecycle_setup'] == ['worktree', 'branch']
            assert run_json['lifecycle_teardown'] == ['merge', 'rm_worktree']

    def test_branch_only_adapter_invocation_uses_primary_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            captured_repo_roots: list[str] = []
            call_count: list[int] = [0]

            class TrackingAdapter:
                name = 'codex'
                supports_effort = True

                def build_invocation(self, *, repo_root, model, system_prompt, user_prompt, effort=None):
                    captured_repo_roots.append(str(repo_root))
                    from aflow.harnesses.codex import CodexAdapter as CA
                    return CA().build_invocation(
                        repo_root=repo_root, model=model,
                        system_prompt=system_prompt, user_prompt=user_prompt, effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=TrackingAdapter(), runner=runner,
            )
            assert len(captured_repo_roots) >= 1
            assert captured_repo_roots[0] == str(repo_root)

    def test_worktree_adapter_invocation_uses_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            captured_repo_roots: list[str] = []
            call_count: list[int] = [0]

            class TrackingAdapter:
                name = 'codex'
                supports_effort = True

                def build_invocation(self, *, repo_root, model, system_prompt, user_prompt, effort=None):
                    captured_repo_roots.append(str(repo_root))
                    from aflow.harnesses.codex import CodexAdapter as CA
                    return CA().build_invocation(
                        repo_root=repo_root, model=model,
                        system_prompt=system_prompt, user_prompt=user_prompt, effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=TrackingAdapter(), runner=runner,
            )
            assert len(captured_repo_roots) >= 1
            assert Path(captured_repo_roots[0]).resolve() != repo_root.resolve()
            assert Path(captured_repo_roots[0]).parent.resolve() == worktree_root.resolve()

    def test_run_artifacts_stay_under_primary_repo_root_in_worktree_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            assert result.run_dir.is_relative_to(repo_root)
            assert not result.run_dir.is_relative_to(worktree_root)

    def test_branch_name_does_not_contain_literal_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_lifecycle_git_repo(repo_root, branch='main')
            plan_path = repo_root / 'my-test-plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            feature_branch = run_json['feature_branch']
            assert '{' not in feature_branch
            assert '}' not in feature_branch
            assert feature_branch.startswith('aflow-my-test-plan-')

    def test_worktree_dir_uses_worktree_prefix_not_branch_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(
                    team_lead='senior_architect',
                    worktree_root=str(worktree_root),
                    branch_prefix='br',
                    worktree_prefix='wt',
                ),
                roles={
                    'architect': 'codex.default',
                    'senior_architect': 'codex.default',
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m')})},
                workflows={'wt_wf': WorkflowConfig(
                    steps={'impl': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='impl')),
                    )},
                    first_step='impl',
                    setup=('worktree', 'branch'),
                    teardown=('merge', 'rm_worktree'),
                    main_branch='main',
                )},
                prompts={'p': 'Work from {ACTIVE_PLAN_PATH}.'},
            )
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(exec_plan)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                else:
                    rc, branches_out, _ = _run_git_in_test(['branch', '--list', 'br-*'], cwd=cwd)
                    assert rc == 0 and branches_out.strip(), 'no br- feature branch found'
                    feature = branches_out.strip().lstrip('+* ').strip()
                    _run_git_in_test(['checkout', 'main'], cwd=cwd)
                    _run_git_in_test(['merge', '--ff-only', feature], cwd=cwd)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))
            feature_branch = run_json['feature_branch']
            worktree_path = run_json['worktree_path']
            worktree_dir_name = Path(worktree_path).name
            assert feature_branch.startswith('br-')
            assert worktree_dir_name.startswith('wt-')
            assert not worktree_dir_name.startswith('br-')

    def test_resume_reuses_existing_worktree_and_branch(self) -> None:
        """Test that accepted resume reuses the same feature branch and worktree path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            base_wf = _make_worktree_wf_config(worktree_root=str(worktree_root))
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(worktree_root=str(worktree_root)),
                roles=base_wf.roles,
                harnesses=base_wf.harnesses,
                workflows=base_wf.workflows,
                prompts=base_wf.prompts,
            )

            first_run_cwd: list[str] = []
            first_run_branch: list[str] = []

            def first_runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                first_run_cwd.append(str(cwd))
                rc, branch_name, _ = _run_git_in_test(['branch', '--show-current'], cwd=cwd)
                first_run_branch.append(branch_name)
                return subprocess.CompletedProcess(argv, 1, 'failed', 'first run failed')

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=first_runner,
                )

            run_json1 = json.loads((first_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            original_feature_branch = run_json1['feature_branch']
            original_worktree_path = run_json1['worktree_path']

            second_run_cwd: list[str] = []
            second_run_branch: list[str] = []

            def second_runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                second_run_cwd.append(str(cwd))
                rc, branch_name, _ = _run_git_in_test(['branch', '--show-current'], cwd=cwd)
                second_run_branch.append(branch_name)
                return subprocess.CompletedProcess(argv, 1, 'failed', 'resumed run failed')

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=original_feature_branch,
                worktree_path=Path(original_worktree_path),
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
            )

            with pytest.raises(WorkflowError) as second_ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=second_runner, resume=resume_ctx,
                )

            assert len(second_run_cwd) == 1
            assert second_run_cwd[0] == original_worktree_path
            assert second_run_branch[0] == original_feature_branch

            run_json2 = json.loads((second_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json2['resumed_from_run_id'] == first_ctx.value.run_dir.name
            assert run_json2['feature_branch'] == original_feature_branch
            assert run_json2['worktree_path'] == original_worktree_path

    def test_resume_does_not_create_second_worktree(self) -> None:
        """Test that accepted resume does not create a second linked worktree."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))

            def first_runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                return subprocess.CompletedProcess(argv, 1, 'failed', 'first run failed')

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=first_runner,
                )

            run_json1 = json.loads((first_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            original_worktree_path = run_json1['worktree_path']

            rc, wt_list_before, _ = _run_git_in_test(['worktree', 'list', '--porcelain'], cwd=repo_root)
            wt_count_before = wt_list_before.count('worktree ')

            def second_runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                return subprocess.CompletedProcess(argv, 1, 'failed', 'resumed run failed')

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=run_json1['feature_branch'],
                worktree_path=Path(original_worktree_path),
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
            )

            with pytest.raises(WorkflowError):
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=second_runner, resume=resume_ctx,
                )

            rc, wt_list_after, _ = _run_git_in_test(['worktree', 'list', '--porcelain'], cwd=repo_root)
            wt_count_after = wt_list_after.count('worktree ')
            assert wt_count_after == wt_count_before, 'No new worktree should be created on resume'

    def test_resume_syncs_plan_back_to_primary_checkout(self) -> None:
        """Test that resumed runs still sync the original plan back to the primary checkout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))

            resume_marker = '  - resumed run marker\n'
            first_runner_calls = {'count': 0}

            def first_runner(argv, **kwargs):
                first_runner_calls['count'] += 1
                cwd = Path(kwargs['cwd'])
                exec_plan = cwd / plan_path.relative_to(repo_root)
                if first_runner_calls['count'] == 1:
                    text = exec_plan.read_text(encoding='utf-8')
                    updated = text.replace('- [ ] step one\n', '- [ ] step one\n' + resume_marker)
                    _write_plan(exec_plan, updated)
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                return subprocess.CompletedProcess(argv, 1, 'failed', 'error')

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=first_runner,
                )

            primary_plan_text = plan_path.read_text(encoding='utf-8')
            assert resume_marker in primary_plan_text

            def second_runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                return subprocess.CompletedProcess(argv, 1, 'failed', 'resumed run failed')

            run_json = json.loads((first_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=run_json['feature_branch'],
                worktree_path=Path(run_json['worktree_path']),
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
            )

            with pytest.raises(WorkflowError):
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=second_runner, resume=resume_ctx,
                )

            assert resume_marker in plan_path.read_text(encoding='utf-8')

    def test_resume_goes_through_normal_merge_and_worktree_removal(self) -> None:
        """Test that resumed runs still go through normal merge teardown and worktree removal on success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))

            def first_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, 'failed', 'first run failed')

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=first_runner,
                )

            run_json1 = json.loads((first_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            original_worktree_path = Path(run_json1['worktree_path'])
            assert original_worktree_path.exists()

            def second_runner(argv, **kwargs):
                cwd = Path(kwargs['cwd'])
                if 'merge' in str(argv):
                    _git_merge_feature_into_main(cwd, 'main')
                else:
                    exec_plan = cwd / plan_path.relative_to(repo_root)
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=run_json1['feature_branch'],
                worktree_path=original_worktree_path,
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
            )

            result2 = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=second_runner, resume=resume_ctx,
            )

            assert not original_worktree_path.exists(), 'Worktree should be removed after successful merge'

            rc, wt_list, _ = _run_git_in_test(['worktree', 'list', '--porcelain'], cwd=repo_root)
            assert str(original_worktree_path) not in wt_list, 'Worktree should not be registered after removal'

    def test_resume_fast_forward_merge_does_not_depend_on_merge_handoff_runner(self) -> None:
        """Test that fast-forward merge teardown is performed by the engine, not the model runner."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))

            def first_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, 'failed', 'first run failed')

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=first_runner,
                )

            run_json1 = json.loads((first_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            original_worktree_path = Path(run_json1['worktree_path'])
            assert original_worktree_path.exists()

            second_runner_calls = {'count': 0}

            def second_runner(argv, **kwargs):
                second_runner_calls['count'] += 1
                cwd = Path(kwargs['cwd'])
                exec_plan = cwd / plan_path.relative_to(repo_root)
                if second_runner_calls['count'] == 1:
                    _write_plan(exec_plan, _COMPLETE_PLAN)
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                raise AssertionError('fast-forward merge teardown should not invoke the runner')

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=run_json1['feature_branch'],
                worktree_path=original_worktree_path,
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
            )

            result2 = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'wt_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=second_runner, resume=resume_ctx,
            )

            assert second_runner_calls['count'] == 1
            assert not original_worktree_path.exists(), 'Worktree should be removed after successful merge'

            run_json2 = json.loads((result2.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json2['merge_status'] == 'success'
            feature_branch = run_json2['feature_branch']
            rc, _, _ = _run_git_in_test(['merge-base', '--is-ancestor', feature_branch, 'main'], cwd=repo_root)
            assert rc == 0, 'main should contain the fast-forward merged feature branch'

    def test_resume_rejects_worktree_with_in_progress_merge(self) -> None:
        """Test that validation rejects a worktree with an in-progress git operation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            wf_config = _make_worktree_wf_config(worktree_root=str(worktree_root))

            def first_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, 'failed', 'first run failed')

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=first_runner,
                )

            run_json1 = json.loads((first_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            original_worktree_path = Path(run_json1['worktree_path'])

            rc, git_dir, _ = _run_git_in_test(['rev-parse', '--git-dir'], cwd=original_worktree_path)
            if rc == 0:
                worktree_git_dir = Path(git_dir)
                if not worktree_git_dir.is_absolute():
                    worktree_git_dir = original_worktree_path / worktree_git_dir
                (worktree_git_dir / 'MERGE_HEAD').write_text('test', encoding='utf-8')

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=run_json1['feature_branch'],
                worktree_path=original_worktree_path,
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
            )

            def second_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'wt_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=second_runner, resume=resume_ctx,
                )

            assert 'in-progress merge' in str(ctx.value).lower()


class WorkflowMaxTurnsEndToEndTests(unittest.TestCase):

    def test_review_implement_review_ends_via_max_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.multi.steps.review_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "MAX_TURNS_REACHED" },\n  { to = "implement_plan" },\n]\n\n[workflow.multi.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "MAX_TURNS_REACHED" },\n  { to = "review_implementation", when = "DONE" },\n  { to = "implement_plan" },\n]\n\n[workflow.multi.steps.review_implementation]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "MAX_TURNS_REACHED" },\n  { to = "implement_plan", when = "NEW_PLAN_EXISTS" },\n  { to = "END" },\n]\n\n[prompts]\np = "Work."\n')
            plan_path = tmp_path / 'plan.md'
            count_file = tmp_path / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            result = _run_workflow_launcher(repo_root, '--max-turns', '1', '--start-step', 'review_plan', str(plan_path), env=_workflow_test_env(repo_root, scenario='noop', plan_path=plan_path, count_file=count_file, home_dir=home_dir))
            assert result.returncode == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['end_reason'] == 'max_turns_reached'


class StopMarkerTests(unittest.TestCase):

    def _make_wf_config(self) -> WorkflowUserConfig:
        return _make_simple_wf_config()

    def test_stop_marker_in_stdout_fails_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='some output\nAFLOW_STOP: dirty worktree blocks verification\nmore output\n',
                    stderr='',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            assert 'dirty worktree blocks verification' in str(ctx.value)
            assert 'AFLOW_STOP' in str(ctx.value)

    def test_stop_marker_in_stderr_fails_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='',
                    stderr='AFLOW_STOP: unrelated changes block this step\n',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            assert 'unrelated changes block this step' in str(ctx.value)

    def test_stop_marker_writes_run_json_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='AFLOW_STOP: cannot continue\n',
                    stderr='',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            run_dir = ctx.value.run_dir
            assert run_dir is not None
            run_json = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json['status'] == 'failed'
            assert 'cannot continue' in run_json['failure_reason']
            assert 'AFLOW_STOP' in run_json['failure_reason']

    def test_stop_marker_writes_turn_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='AFLOW_STOP: fatal blocker\n',
                    stderr='',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            turn_dir = ctx.value.run_dir / 'turns' / 'turn-001'
            result_json = json.loads((turn_dir / 'result.json').read_text(encoding='utf-8'))
            assert result_json['status'] == 'harness-failed'
            assert 'fatal blocker' in result_json['error']
            assert result_json['stdout'] == 'AFLOW_STOP: fatal blocker\n'

    def test_no_stop_marker_does_not_fail_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, stdout='all good', stderr='')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            assert result.turns_completed == 1
            assert result.final_snapshot.is_complete

    def test_stop_marker_stdout_takes_priority_over_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='AFLOW_STOP: stdout reason\n',
                    stderr='AFLOW_STOP: stderr reason\n',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            assert 'stdout reason' in str(ctx.value)

    def test_stop_marker_blank_reason_uses_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='AFLOW_STOP:\n',
                    stderr='',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            assert 'implementer requested stop without a reason' in str(ctx.value)

    def test_stop_marker_example_inside_fenced_transcript_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = self._make_wf_config()

            def runner(argv, **kwargs):
                _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='checkpoint completed\n',
                    stderr=(
                        'tool output\n'
                        '```\n'
                        'AFLOW_STOP: <reason>\n'
                        '```\n'
                        'more transcript\n'
                    ),
                )

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            assert result.turns_completed == 1
            assert result.final_snapshot.is_complete


class LifecycleBootstrapTests(unittest.TestCase):
    """Runtime tests for the team-lead repo-init bootstrap handoff."""

    def test_init_repo_handoff_invoked_for_no_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    subprocess.run(['git', 'init', '-b', 'main'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=str(cwd), check=True, capture_output=True)
                    (cwd / 'README.md').write_text('# Plan\n\nBootstrapped.\n', encoding='utf-8')
                    subprocess.run(['git', 'add', 'README.md'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(cwd), check=True, capture_output=True)
                    return subprocess.CompletedProcess(argv, 0, 'bootstrap ok', '')
                elif call_count[0] == 2:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                    return subprocess.CompletedProcess(argv, 0, 'merged', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            assert call_count[0] >= 2, 'at least init + workflow step should be called'
            assert result.final_snapshot.is_complete

    def test_init_repo_handoff_for_unborn_repo_on_mismatched_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _make_unborn_git_repo(repo_root, branch='other-branch')
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/main'], cwd=str(cwd), check=True, capture_output=True)
                    (cwd / 'README.md').write_text('# Plan\n\nBootstrapped.\n', encoding='utf-8')
                    subprocess.run(['git', 'add', 'README.md'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(cwd), check=True, capture_output=True)
                    return subprocess.CompletedProcess(argv, 0, 'bootstrap ok', '')
                elif call_count[0] == 2:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                    return subprocess.CompletedProcess(argv, 0, 'merged', '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            assert call_count[0] >= 2
            assert result.final_snapshot.is_complete
            rc, branch, _ = _run_git_in_test(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_root)
            assert branch == 'main'

    def test_team_lead_resolved_through_team_override_for_init_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(team_lead='senior_architect'),
                roles={
                    'architect': 'codex.default',
                    'senior_architect': 'codex.default',
                },
                teams={
                    'elite': TeamConfig(roles={'senior_architect': 'codex.override'}),
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'default': HarnessProfileConfig(model='m'),
                    'override': HarnessProfileConfig(model='override-model'),
                })},
                workflows={'branch_wf': WorkflowConfig(
                    steps={'impl': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'), GoTransition(to='impl')),
                    )},
                    first_step='impl',
                    setup=('branch',),
                    teardown=('merge',),
                    main_branch='main',
                    team='elite',
                )},
                prompts={'p': 'Work from {ACTIVE_PLAN_PATH}.'},
            )
            call_count: list[int] = [0]
            bootstrap_invocation_model: list[str] = []

            class TrackingAdapter:
                name = 'codex'
                supports_effort = True

                def build_invocation(self, *, repo_root, model, system_prompt, user_prompt, effort=None):
                    if call_count[0] == 0:
                        bootstrap_invocation_model.append(model or '')
                    from aflow.harnesses.codex import CodexAdapter as CA
                    return CA().build_invocation(
                        repo_root=repo_root, model=model,
                        system_prompt=system_prompt, user_prompt=user_prompt, effort=effort,
                    )

            def runner(argv, **kwargs):
                call_count[0] += 1
                cwd = Path(kwargs['cwd'])
                if call_count[0] == 1:
                    subprocess.run(['git', 'init', '-b', 'main'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=str(cwd), check=True, capture_output=True)
                    (cwd / 'README.md').write_text('# Plan\n\nBootstrapped.\n', encoding='utf-8')
                    subprocess.run(['git', 'add', 'README.md'], cwd=str(cwd), check=True, capture_output=True)
                    subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(cwd), check=True, capture_output=True)
                    return subprocess.CompletedProcess(argv, 0, 'bootstrap ok', '')
                elif call_count[0] == 2:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    _run_git_in_test(['add', str(plan_path)], cwd=cwd)
                    _run_git_in_test(['commit', '-m', 'complete'], cwd=cwd)
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                else:
                    _git_merge_feature_into_main(cwd, 'main')
                    return subprocess.CompletedProcess(argv, 0, 'merged', '')

            run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config, 'branch_wf', config_dir=repo_root,
                adapter=TrackingAdapter(), runner=runner,
            )
            assert bootstrap_invocation_model, 'bootstrap agent build_invocation was not called'
            assert bootstrap_invocation_model[0] == 'override-model'

    def test_init_repo_AFLOW_STOP_fails_without_creating_feature_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_branch_only_wf_config(main_branch='main')
            call_count: list[int] = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                return subprocess.CompletedProcess(argv, 0, 'AFLOW_STOP: repo init failed', '')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                    wf_config, 'branch_wf', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=runner,
                )
            assert 'AFLOW_STOP' in str(ctx.value) or 'repo init failed' in str(ctx.value)
            assert call_count[0] == 1, 'only init call should be made; no feature branch creation'
            rc, branches, _ = _run_git_in_test(['branch', '--list', 'aflow-*'], cwd=repo_root)
            assert not branches.strip(), 'no feature branch should be created after bootstrap AFLOW_STOP'

    def test_git_missing_lifecycle_fails_with_clear_bootstrap_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_branch_only_wf_config(main_branch='main')

            with patch('aflow.git_status.shutil.which', return_value=None):
                with pytest.raises(WorkflowError) as ctx:
                    run_workflow(
                        ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                        wf_config, 'branch_wf', config_dir=repo_root,
                    )
            assert 'git' in str(ctx.value).lower()
            assert 'install' in str(ctx.value).lower() or 'installed' in str(ctx.value).lower() or 'PATH' in str(ctx.value)

    def test_non_lifecycle_workflow_in_no_git_dir_does_not_trigger_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _COMPLETE_PLAN)
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m')})},
                workflows={'simple': WorkflowConfig(
                    steps={'impl': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END'),),
                    )},
                    first_step='impl',
                )},
                prompts={'p': 'Work.'},
            )
            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1),
                wf_config, 'simple', config_dir=repo_root,
            )
            assert result.end_reason == 'already_complete'
            assert not (repo_root / '.git').exists(), 'no git repo should be initialized for non-lifecycle workflows'

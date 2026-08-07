from aflow._test_support import *  # noqa: F401,F403
from dataclasses import replace

class RetryInconsistentCheckpointPlanTests(unittest.TestCase):

    def test_error_kind_set_for_inconsistent_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n')
            with pytest.raises(PlanParseError) as exc_info:
                load_plan(plan_path)
            assert exc_info.value.error_kind == 'inconsistent_checkpoint_state'

    def test_error_kind_none_for_missing_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# No checkpoints here\n')
            with pytest.raises(PlanParseError) as exc_info:
                load_plan(plan_path)
            assert exc_info.value.error_kind is None


class RetryInconsistentCheckpointStartupTests(unittest.TestCase):

    def test_startup_retry_role_prompt_and_inconsistent_checkpoint_appendix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            broken_plan = '# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n'
            complete_plan = '# Plan\n\n### [x] Checkpoint 1: Broken\n- [x] step one\n'
            _write_plan(plan_path, broken_plan)
            wf_config = replace(
                _make_simple_wf_config(global_retry=1),
                role_prompts={'architect': 'Retry role guidance.'},
            )
            recovery = load_plan_tolerant(plan_path)
            assert recovery.parse_error is not None
            captured_user_prompts: list[str] = []
            captured_system_prompts: list[str] = []

            class CapturingAdapter:
                name = 'codex'
                supports_effort = False

                def build_invocation(self, *, repo_root, model, system_prompt, user_prompt, effort=None):
                    captured_system_prompts.append(system_prompt)
                    captured_user_prompts.append(user_prompt)
                    return HarnessInvocation(
                        label='codex',
                        argv=('codex', 'run', user_prompt),
                        env={},
                        prompt_mode='prefix-system-into-user-prompt',
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        effective_prompt=f'{system_prompt}\n\n{user_prompt}' if system_prompt else user_prompt,
                    )

            def runner(argv, **kwargs):
                _write_plan(plan_path, complete_plan)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            step = wf_config.workflows['simple'].steps['implement_plan']
            base_user_prompt = render_step_prompts(
                step,
                wf_config,
                config_dir=repo_root,
                working_dir=repo_root,
                original_plan_path=plan_path,
                new_plan_path=generate_new_plan_path(plan_path, checkpoint_index=1),
                active_plan_path=plan_path,
            )
            retry_ctx = RetryContext(
                step_name='implement_plan',
                step_role='architect',
                resolved_selector='codex.default',
                resolved_harness_name='codex',
                resolved_model='gpt-5.4',
                resolved_effort=None,
                snapshot_before=recovery.parsed_plan.snapshot,
                active_plan_path=plan_path,
                new_plan_path=generate_new_plan_path(plan_path, checkpoint_index=1),
                base_user_prompt=base_user_prompt,
                parse_error_str=str(recovery.parse_error),
                attempt=1,
                retry_limit=1,
            )
            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2)
            result = run_workflow(
                controller_config,
                wf_config,
                'simple',
                config_dir=repo_root,
                adapter=CapturingAdapter(),
                runner=runner,
                parsed_plan=recovery.parsed_plan,
                startup_retry=retry_ctx,
            )

            assert captured_user_prompts
            assert captured_system_prompts == ['Retry role guidance.']
            assert base_user_prompt in captured_user_prompts[0]
            assert 'inconsistent checkpoint state' in captured_user_prompts[0].lower()
            assert str(recovery.parse_error) in captured_user_prompts[0]
            turn1 = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1['was_retry'] is True
            assert turn1['retry_attempt'] == 1


class RetryInconsistentCheckpointWorkflowTests(unittest.TestCase):

    def test_retry_disabled_fails_immediately_on_invalid_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=0)

            def runner(argv, **kwargs):
                _write_plan(plan_path, _BROKEN_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            assert 'inconsistent checkpoint state' in str(ctx.value).lower()

    def test_role_prompt_is_retained_when_retry_fixes_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = replace(
                _make_simple_wf_config(global_retry=1),
                role_prompts={'architect': 'Retry role guidance.'},
            )
            call_count = [0]
            captured_system_prompts: list[str] = []

            class CapturingAdapter:
                name = 'codex'
                supports_effort = False

                def build_invocation(self, *, repo_root, model, system_prompt, user_prompt, effort=None):
                    captured_system_prompts.append(system_prompt)
                    return HarnessInvocation(
                        label='codex', argv=('codex', 'run', user_prompt), env={},
                        prompt_mode='prefix-system-into-user-prompt',
                        system_prompt=system_prompt, user_prompt=user_prompt,
                        effective_prompt=f'{system_prompt}\n\n{user_prompt}' if system_prompt else user_prompt,
                    )

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    _write_plan(plan_path, _BROKEN_PLAN)
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CapturingAdapter(), runner=runner)
            assert result.turns_completed == 2
            assert result.final_snapshot.is_complete
            assert captured_system_prompts == ['Retry role guidance.', 'Retry role guidance.']

    def test_retry_turn_reuses_same_new_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=1)
            captured_new_plan_paths: list[str] = []
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                prompt = ' '.join(argv)
                import re as re_mod
                match = re_mod.search(r'new_plan_path=(\S+)', prompt)
                if not match:
                    for tok in argv:
                        if 'cp01' in tok:
                            captured_new_plan_paths.append(tok)
                            break
                if call_count[0] == 1:
                    _write_plan(plan_path, _BROKEN_PLAN)
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)

            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            turn1_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            turn2_result = json.loads((run_dirs[0] / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn1_result['new_plan_path'] == turn2_result['new_plan_path']

    def test_retry_turn_prompt_includes_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=1)
            captured_prompts: list[str] = []
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                captured_prompts.append(' '.join(argv))
                if call_count[0] == 1:
                    _write_plan(plan_path, _BROKEN_PLAN)
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            assert len(captured_prompts) == 2
            assert 'inconsistent checkpoint state' in captured_prompts[1].lower()

    def test_workflow_override_zero_disables_retry_when_global_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=1, workflow_retry=0)

            def runner(argv, **kwargs):
                _write_plan(plan_path, _BROKEN_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            with pytest.raises(WorkflowError):
                run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)

    def test_workflow_override_enables_retry_when_global_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=0, workflow_retry=1)
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    _write_plan(plan_path, _BROKEN_PLAN)
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            assert result.final_snapshot.is_complete

    def test_retry_exhaustion_fails_on_latest_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=2)

            def runner(argv, **kwargs):
                _write_plan(plan_path, _BROKEN_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=10)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            assert 'inconsistent checkpoint state' in str(ctx.value).lower()

    def test_max_turn_on_failed_turn_does_not_schedule_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=3)

            def runner(argv, **kwargs):
                _write_plan(plan_path, _BROKEN_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1)
            with pytest.raises(WorkflowError):
                run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)


class RetryInconsistentCheckpointArtifactTests(unittest.TestCase):

    def test_failed_turn_writes_retry_scheduled_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=1)
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    _write_plan(plan_path, _BROKEN_PLAN)
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            turn1 = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn1['status'] == 'retry-scheduled'
            assert turn1['retry_attempt'] == 1
            assert turn1['retry_limit'] == 1
            assert turn1['retry_reason'] == 'inconsistent_checkpoint_state'
            assert turn1['retry_next_turn'] is True
            assert turn1['snapshot_after'] is None
            assert 'error' in turn1

    def test_run_json_exposes_pending_retry_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=1)

            def runner(argv, **kwargs):
                _write_plan(plan_path, _BROKEN_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=1)
            with pytest.raises(WorkflowError) as ctx:
                run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            run_json = json.loads((ctx.value.run_dir / 'run.json').read_text(encoding='utf-8'))
            assert run_json.get('pending_retry_step_name') is None

    def test_successful_retry_turn_marks_was_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=1)
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    _write_plan(plan_path, _BROKEN_PLAN)
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            turn2 = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            assert turn2['was_retry'] is True
            assert turn2['retry_attempt'] == 1

    def test_retry_issue_link_stays_on_issue_turn_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config(global_retry=1)
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    _write_plan(plan_path, _BROKEN_PLAN)
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            controller_config = ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5)
            result = run_workflow(controller_config, wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner)
            turn1 = json.loads((result.run_dir / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            turn2 = json.loads((result.run_dir / 'turns' / 'turn-002' / 'result.json').read_text(encoding='utf-8'))
            run_json = json.loads((result.run_dir / 'run.json').read_text(encoding='utf-8'))

            assert turn1['issues_summary_path'] == run_json['issues_summary_path']
            assert 'issues_summary_path' not in turn2
            assert run_json['issues_summary_path'] == '.aflow/runs/' + result.run_dir.name + '/issues.md'
            assert (result.run_dir / 'issues.md').is_file()


class SameStepCapWorkflowTests(unittest.TestCase):

    def test_multi_step_same_step_cap_fails_before_sixth_consecutive_visit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_multistep_wf_config(max_same_step_turns=5)
            call_steps: list[str] = []

            def runner(argv, **kwargs):
                step = argv[0] if argv else 'unknown'
                call_steps.append(step)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=20),
                    wf_config, 'loop', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            assert 'same-step cap' in str(ctx.value).lower()
            assert 'implement' in str(ctx.value)
            assert '5' in str(ctx.value)
            implement_count = sum(1 for s in call_steps if 'codex' in s or True)
            assert len(call_steps) < 20

    def test_multi_step_same_step_cap_fails_before_exceeding_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_multistep_wf_config(max_same_step_turns=3)
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=20),
                    wf_config, 'loop', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
                )
            assert 'same-step cap' in str(ctx.value).lower()
            assert '3' in str(ctx.value)
            assert call_count[0] <= 4

    def test_multi_step_streak_resets_after_different_step_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)

            from aflow.config import WorkflowConfig
            wf = WorkflowConfig(
                steps={
                    'review': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(
                            GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'),
                            GoTransition(to='implement'),
                        ),
                    ),
                    'implement': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(
                            GoTransition(to='END', when='DONE || MAX_TURNS_REACHED'),
                            GoTransition(to='review'),
                        ),
                    ),
                },
                first_step='review',
            )
            wf_config = WorkflowUserConfig(
                aflow=AflowSection(max_same_step_turns=2),
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='gpt-5.4')})},
                workflows={'alternating': wf},
                prompts={'p': 'Work.'},
            )
            turn_count = [0]

            def runner(argv, **kwargs):
                turn_count[0] += 1
                if turn_count[0] >= 8:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=20),
                wf_config, 'alternating', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            assert result.final_snapshot.is_complete

    def test_single_step_workflow_ignores_same_step_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_simple_wf_config()
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] >= 6:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=20),
                wf_config, 'simple', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            assert result.final_snapshot.is_complete
            assert call_count[0] >= 6

    def test_same_step_cap_zero_disables_guardrail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            wf_config = _make_multistep_wf_config(max_same_step_turns=0)
            call_count = [0]

            def runner(argv, **kwargs):
                call_count[0] += 1
                if call_count[0] >= 8:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, stdout='ok', stderr='')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=20),
                wf_config, 'loop', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            assert result.final_snapshot.is_complete

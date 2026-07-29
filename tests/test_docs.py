from aflow._test_support import *  # noqa: F401,F403

class SkillDocsTests(unittest.TestCase):

    def test_skill_files_do_not_contain_workflow_placeholders(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        placeholders = ('{ORIGINAL_PLAN_PATH}', '{ACTIVE_PLAN_PATH}', '{NEW_PLAN_PATH}')
        for skill_name in ('aflow-plan', 'aflow-execute-plan', 'aflow-execute-checkpoint', 'aflow-review-squash', 'aflow-review-checkpoint', 'aflow-review-final', 'aflow-merge', 'aflow-harness-recovery-lead', 'aflow-assistant'):
            skill_path = repo_root / 'aflow' / 'bundled_skills' / skill_name / 'SKILL.md'
            text = skill_path.read_text(encoding='utf-8')
            for placeholder in placeholders:
                assert placeholder not in text

    def test_bundled_skill_files_exist(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for skill_name in (
            'aflow-plan',
            'aflow-execute-plan',
            'aflow-execute-checkpoint',
            'aflow-review-squash',
            'aflow-review-checkpoint',
            'aflow-review-final',
            'aflow-merge',
            'aflow-harness-recovery-lead',
            'aflow-assistant',
        ):
            skill_path = repo_root / 'aflow' / 'bundled_skills' / skill_name / 'SKILL.md'
            assert skill_path.exists()

    def test_aflow_assistant_bundled_resources_exist(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        skill_root = repo_root / 'aflow' / 'bundled_skills' / 'aflow-assistant'
        assert (skill_root / 'SKILL.md').exists()
        assert (skill_root / 'references' / 'engine-map.md').exists()
        assert (skill_root / 'scripts' / 'analyze_runs.py').exists()

    def test_aflow_assistant_skill_prefers_bundled_resources_for_installed_use(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-assistant' / 'SKILL.md').read_text(encoding='utf-8')
        assert 'references/engine-map.md' in text
        assert 'aflow analyze' in text
        assert '--all' in text
        assert 'Do not assume the original `aflow` repo checkout exists' in text
        assert '## Bundled Engine Map First' in text

    def test_final_review_skill_is_distinct_and_no_squash(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-review-final' / 'SKILL.md').read_text(encoding='utf-8')
        assert 'name: aflow-review-final' in text
        assert 'Do nothing.' not in text
        assert 'Do not squash' in text or 'Do not squash,' in text
        assert 'non-checkpoint' in text

    def test_example_plan_uses_review_squash_spelling(self) -> None:
        example_text = textwrap.dedent('''
            [workflow.my_wf1.steps.review]
            role = "architect"
            prompts = ["review_prompt"]
            go = [
              { to = "implement_plan" },
              { to = "END", when = "MAX_TURNS_REACHED" },
            ]

            # The example plan references the checkpoint review skills by name.
            review_squash = "aflow-review-squash"
            review_checkpoint = "aflow-review-checkpoint"
            execute_checkpoint = "aflow-execute-checkpoint"
            execute_plan = "aflow-execute-plan"
            review_final = "aflow-review-final"
        ''')
        assert 'aflow-review-squash' in example_text
        assert 'aflow-review-checkpoint' in example_text
        assert 'aflow-execute-checkpoint' in example_text
        assert 'aflow-execute-plan' in example_text
        assert 'aflow-review-final' in example_text
        typo = '-'.join(('revive', 'squash'))
        assert typo not in example_text

    def test_review_checkpoint_skill_has_pre_handoff_selection_rule(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-review-checkpoint' / 'SKILL.md').read_text(encoding='utf-8')
        assert 'Pre-Handoff Base HEAD' in text
        # The selection rule about searching plans/in-progress/ must be present
        assert 'plans/in-progress/' in text

    def test_plan_skill_uses_semantic_checkpoint_shaping_without_hard_stop(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-plan' / 'SKILL.md').read_text(encoding='utf-8')
        section = text.split('## Checkpoint Shaping', 1)[1].split('\n## ', 1)[0]

        assert len(text.splitlines()) <= 190
        assert 'reviewable change surface' in section
        assert 'no more than two tightly coupled production layers' in section
        assert 'heuristic, not a quota' in section
        assert 'secondary scope-pressure signals' in section
        assert 'generated volume alone must not force a split' in section
        assert 'Re-audit the entire remaining plan' in text
        assert 'Confirm risky assumptions and record safe defaults explicitly' in text
        assert 'do not rely on another skill or workflow role' in text
        assert 'external artifacts explicitly supplied by the user or environment' in text
        assert '`git rev-parse --show-toplevel`' in text
        assert '`git diff --name-only`' in text
        assert 'unrelated dirty files make change ownership ambiguous' in text
        assert '25 changed files or 3,000 changed lines' not in text
        assert 'AFLOW_STOP' not in text

    def test_manager_skill_documents_lite_then_full_worker_upgrade_sequence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-manager' / 'SKILL.md').read_text(encoding='utf-8')
        normalized = ' '.join(text.split())

        assert 'first reviewer rejection' in normalized
        assert 'choose that upgrade at Lite level' in normalized
        assert 'invokes Full directly for the second rejection' in normalized
        assert 'controller validates all routing and decides the concrete target' in normalized
        assert "next checkpoint's initial worker" in normalized

    def test_bundled_config_review_implement_review_max_turns_transitions(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = load_workflow_config(repo_root / 'aflow' / 'aflow.toml')
        wf = config.workflows['review_implement_review']
        for step_name, step in wf.steps.items():
            assert step.go[0].to == 'END', f"step {step_name} first transition must be END"
            assert step.go[0].when == 'MAX_TURNS_REACHED', f"step {step_name} first transition must be MAX_TURNS_REACHED"
        assert wf.steps['implement_plan'].prompts == ('implementation_plans', 'cp_loop_implementation')

    def test_bundled_skills_keep_plan_worker_facing_and_commit_ownership_in_execution_roles(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        review_text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-review-squash' / 'SKILL.md').read_text(encoding='utf-8')
        review_cp_text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-review-checkpoint' / 'SKILL.md').read_text(encoding='utf-8')
        review_final_text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-review-final' / 'SKILL.md').read_text(encoding='utf-8')
        exec_cp_text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-execute-checkpoint' / 'SKILL.md').read_text(encoding='utf-8')
        exec_plan_text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-execute-plan' / 'SKILL.md').read_text(encoding='utf-8')
        plan_text = (repo_root / 'aflow' / 'bundled_skills' / 'aflow-plan' / 'SKILL.md').read_text(encoding='utf-8')
        config_text = (repo_root / 'aflow' / 'aflow.toml').read_text(encoding='utf-8')
        assert 'workflow engine finalizes the original plan location after terminal success' in review_text
        assert 'move that original plan to `plans/done/`' not in review_text
        assert 'workflow engine owns the final move to `plans/done/`' in plan_text
        assert '**Done When:**' in plan_text
        assert '**Review Acceptance Boundary:**' not in plan_text
        assert 'AFLOW_STOP' not in plan_text
        assert 'Do not add manager selection, retry or upgrade routing' in plan_text
        assert 'Reviewer workflows own all commit creation and approval-grade git bookkeeping' in exec_cp_text
        assert 'Reviewer workflows own all commit creation and approval-grade git bookkeeping' in exec_plan_text
        assert 'git diff --name-only' in exec_cp_text
        assert 'git diff --name-only' in exec_plan_text
        assert 'Create the checkpoint approval commit for the reviewed work in this review turn.' in review_cp_text
        assert 'Do not create checkpoint commits; leave verified scoped changes uncommitted for review.' in config_text
        assert 'all approval-grade git/tracking chores were completed by the reviewer in the same turn' in review_final_text
        assert 'all approval-grade git/tracking chores were completed by the reviewer in the same turn' in review_text

    def test_review_skills_require_new_plan_path_for_followup_files(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for skill_name in ('aflow-review-squash', 'aflow-review-checkpoint', 'aflow-review-final'):
            text = (repo_root / 'aflow' / 'bundled_skills' / skill_name / 'SKILL.md').read_text(encoding='utf-8')
            assert 'write it exactly to `NEW_PLAN_PATH`' in text
            assert 'Do not invent a different filename.' in text
            assert 'Use the filename format' not in text

    def test_bundled_config_review_implement_cp_review_max_turns_transitions(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = load_workflow_config(repo_root / 'aflow' / 'aflow.toml')
        wf = config.workflows['review_implement_cp_review']
        for step_name, step in wf.steps.items():
            assert step.go[0].to == 'END', f"step {step_name} first transition must be END"
            assert step.go[0].when == 'MAX_TURNS_REACHED', f"step {step_name} first transition must be MAX_TURNS_REACHED"

    def test_bundled_config_ralph_uses_checkpoint_prompt(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = load_workflow_config(repo_root / 'aflow' / 'aflow.toml')
        step = config.workflows['ralph'].steps['implement_plan']
        assert 'cp_loop_implementation' in step.prompts

    def test_bundled_config_typos_are_fixed(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        text = (repo_root / 'aflow' / 'aflow.toml').read_text(encoding='utf-8')
        assert 'undispituble' not in text
        assert 'improvementns' not in text

    def test_example_plan_max_turns_transitions(self) -> None:
        import tomllib
        raw = tomllib.loads(textwrap.dedent('''
            [workflow.my_wf1.steps.review]
            role = "architect"
            prompts = ["p"]
            go = [
              { to = "END", when = "MAX_TURNS_REACHED" },
              { to = "implement" },
            ]

            [workflow.my_wf1.steps.implement]
            role = "worker"
            prompts = ["p"]
            go = [
              { to = "END", when = "MAX_TURNS_REACHED" },
              { to = "review" },
            ]

            [workflow.checkpoint_loop.steps.review]
            role = "architect"
            prompts = ["p"]
            go = [
              { to = "END", when = "MAX_TURNS_REACHED" },
              { to = "implement" },
            ]

            [workflow.checkpoint_loop.steps.implement]
            role = "worker"
            prompts = ["p"]
            go = [
              { to = "END", when = "MAX_TURNS_REACHED" },
              { to = "review" },
            ]
        '''))
        for wf_name in ('my_wf1', 'checkpoint_loop'):
            for step_name, step_table in raw['workflow'][wf_name]['steps'].items():
                first_go = step_table['go'][0]
                assert first_go.get('when') == 'MAX_TURNS_REACHED', (
                    f"{wf_name}.{step_name} first transition must be MAX_TURNS_REACHED"
                )
                assert first_go['to'] == 'END'

    def test_library_api_exports_match_architecture_docs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        architecture_text = (repo_root / 'ARCHITECTURE.md').read_text(encoding='utf-8')

        import aflow

        # Verify all names in __all__ are importable
        for name in aflow.__all__:
            assert hasattr(aflow, name), f"Exported name '{name}' not found in aflow module"

        # Verify key public types are documented in ARCHITECTURE.md
        documented_types = [
            'StartupRequest',
            'StartupQuestion',
            'PreparedRun',
            'ExecutionObserver',
            'CallbackObserver',
            'CollectingObserver',
            'ExecutionEvent',
            'WorkflowRunner',
            'RunnerConfig',
            'prepare_startup',
            'prepare_startup_with_answer',
            'execute_workflow',
        ]
        for type_name in documented_types:
            assert type_name in architecture_text, f"Public type '{type_name}' not documented in ARCHITECTURE.md"
            assert hasattr(aflow, type_name), f"Documented type '{type_name}' not found in aflow module"

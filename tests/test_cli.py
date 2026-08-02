from aflow._test_support import *  # noqa: F401,F403
import hashlib
from aflow.api.models import PreparedRun
from aflow.config import ManagerConfig
from aflow.status import BannerRenderer as RealBannerRenderer


def test_cli_analysis_adds_safe_controller_state_without_notes(
    tmp_path: Path,
) -> None:
    import aflow.cli as cli_module

    run_dir = tmp_path / ".aflow" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "frozen_config": {
                    "workflow_name": "test",
                    "config_path": "/config",
                    "config_fingerprint": "abc",
                },
                "override_file_present": True,
                "override_result": {
                    "status": "rejected",
                    "digest": "digest",
                    "message": "unknown team",
                    "source_text": 'notes = ["private prompt note"]',
                },
                "pending_override_notes": ["private prompt note"],
            }
        ),
        encoding="utf-8",
    )
    payload = {"run": {"run_id": "run-1"}}

    cli_module._add_controller_state_to_analysis(
        payload,
        repo_root=tmp_path,
    )

    controller = payload["run"]["controller_state"]
    assert controller["schema_version"] == 1
    assert controller["corrected_override_required"] is True
    assert "pending_override_notes" not in controller
    assert "private prompt note" not in json.dumps(controller)


def _run_manager_report_cli_case(
    tmp_path: Path,
    *,
    invalid_managers: bool,
) -> tuple[int, str, Path]:
    from rich.console import Console

    import aflow.cli as cli_module

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plan_path = repo_root / "plan.md"
    _write_plan(plan_path, _VALID_PLAN)
    config_path = repo_root / "aflow.toml"
    config_path.write_text("", encoding="utf-8")
    workflow = WorkflowConfig(
        steps={"impl": WorkflowStepConfig(
            role="architect",
            prompts=("p",),
            go=(GoTransition(to="END", when="DONE"),),
        )},
        first_step="impl",
    )
    workflow_config = WorkflowUserConfig(
        roles={
            "architect": "codex.worker",
            "manager_lite": "codex.lite",
            "manager_full": "codex.full",
        },
        harnesses={"codex": WorkflowHarnessConfig(profiles={
            "worker": HarnessProfileConfig(model="worker"),
            "lite": HarnessProfileConfig(model="lite"),
            "full": HarnessProfileConfig(model="full"),
        })},
        workflows={"managed": workflow},
        prompts={"p": "Work."},
        manager=ManagerConfig(
            enabled=True,
            lite_role="manager_lite",
            full_role="manager_full",
        ),
    )
    prepared = PreparedRun(
        workflow_name="managed",
        repo_root=repo_root,
        plan_path=plan_path,
        config_path=config_path,
        max_turns=2,
        team=None,
        extra_instructions=(),
        start_step="impl",
    )
    stderr = io.StringIO()

    def runner(argv, **kwargs):
        model = argv[argv.index("--model") + 1]
        if model == "worker":
            _write_plan(plan_path, _COMPLETE_PLAN)
            return subprocess.CompletedProcess(argv, 0, "work complete", "")
        if invalid_managers:
            return subprocess.CompletedProcess(argv, 0, "not valid JSON", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "schema_version": 1,
            "action": "stop",
            "reason": "The manager stopped the run.",
            "next_step_notes": [],
            "stop_report": {
                "summary": "The manager stopped the run.",
                "root_cause": "Synthetic manager stop.",
                "evidence": ["The manager selected stop."],
                "attempts": "One workflow turn ran.",
                "workspace_state": "The plan is complete.",
                "next_actions": ["Inspect the manager report."],
            },
        }), "")

    def execute(prepared_run, *, banner, resume, observer):
        return run_workflow(
            ControllerConfig(
                repo_root=repo_root,
                plan_path=plan_path,
                max_turns=prepared_run.max_turns,
            ),
            workflow_config,
            "managed",
            config_dir=repo_root,
            runner=runner,
            banner=banner,
            observer=observer,
        )

    def banner_factory(**kwargs):
        return RealBannerRenderer(
            **kwargs,
            console=Console(
                file=stderr,
                force_terminal=True,
                color_system=None,
                width=120,
            ),
            refresh_interval_seconds=0.01,
        )

    with redirect_stderr(stderr), \
         patch.object(cli_module, "_bootstrap_config_files", return_value=(config_path, ())), \
         patch.object(cli_module, "load_workflow_config", return_value=workflow_config), \
         patch.object(cli_module, "validate_workflow_config", return_value=[]), \
         patch.object(cli_module, "_resolve_repo_root", return_value=repo_root), \
         patch.object(cli_module, "_resolve_run_arguments", return_value=("managed", str(plan_path), ())), \
         patch.object(cli_module, "_handle_startup_questions", return_value=prepared), \
         patch.object(cli_module, "_detect_resume_candidate", return_value=None), \
         patch.object(cli_module, "BannerRenderer", side_effect=banner_factory), \
         patch.object(cli_module, "execute_workflow", side_effect=execute):
        status = cli_module.main(["run", str(plan_path)])
        print("Aflow exited with status 1. This Screen shell will remain available.", file=sys.stderr)

    run_dirs = sorted((repo_root / ".aflow" / "runs").iterdir())
    return status, stderr.getvalue(), run_dirs[-1]


@pytest.mark.parametrize("invalid_managers", [False, True])
def test_manager_report_remains_visible_once_after_real_banner_and_cli(
    tmp_path: Path,
    invalid_managers: bool,
) -> None:
    status, stderr, run_dir = _run_manager_report_cli_case(
        tmp_path,
        invalid_managers=invalid_managers,
    )
    report = (run_dir / "manager-report.md").read_text(encoding="utf-8")
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    assert status == 1
    assert stderr.count("# AFlow manager report") == 1
    assert report in stderr
    assert "## Likely root cause" in stderr
    assert "## Evidence" in stderr
    assert "## Next actions" in stderr
    assert "## Artifact references" in stderr
    assert "Manager Report" in stderr
    assert stderr.index("Manager Report") < stderr.index(report)
    assert stderr.index(report) < stderr.index("Aflow exited with status 1.")
    assert run_json["failure_reason"] == report
    assert run_json["last_manager_report_path"] == "manager-report.md"
    decisions = sorted((run_dir / "manager").iterdir())
    assert len(decisions) == (2 if invalid_managers else 1)

class WorkflowCliTests(unittest.TestCase):

    def test_prog_name_is_aflow(self) -> None:
        parser = build_parser()
        assert parser.prog == 'aflow'

    def test_analyze_parser_accepts_manager_context_and_turn(self) -> None:
        args = build_parser().parse_args([
            'analyze', 'run-123', '--manager-context', 'full', '--turn', '4',
        ])
        assert args.manager_context == 'full'
        assert args.turn == 4

    def test_run_args_workflow_and_plan(self) -> None:
        workflow, plan, extra = _parse_run_args(['ralph', 'plan.md'])
        assert workflow == 'ralph'
        assert plan == 'plan.md'
        assert extra == ()

    def test_run_args_plan_only(self) -> None:
        workflow, plan, extra = _parse_run_args(['plan.md'])
        assert workflow is None
        assert plan == 'plan.md'
        assert extra == ()

    def test_run_args_extra_instructions(self) -> None:
        workflow, plan, extra = _parse_run_args(['plan.md', '--', 'keep edits small'])
        assert workflow is None
        assert plan == 'plan.md'
        assert extra == ('keep edits small',)

    def test_run_args_workflow_plan_extra(self) -> None:
        workflow, plan, extra = _parse_run_args(['ralph', 'plan.md', '--', 'be careful'])
        assert workflow == 'ralph'
        assert plan == 'plan.md'
        assert extra == ('be careful',)

    def test_run_args_empty(self) -> None:
        workflow, plan, extra = _parse_run_args([])
        assert workflow is None
        assert plan is None
        assert extra == ()

    def test_run_parser_max_turns_short_flag(self) -> None:
        args = build_parser().parse_args(['run', '-mt', '5', 'plan.md'])
        assert args.max_turns == 5

    def test_run_parser_max_turns_long_flag(self) -> None:
        args = build_parser().parse_args(['run', '--max-turns', '10', 'plan.md'])
        assert args.max_turns == 10

    def test_run_parser_max_turns_defaults_to_none(self) -> None:
        args = build_parser().parse_args(['run', 'plan.md'])
        assert args.max_turns is None

    def test_run_parser_team_flag(self) -> None:
        args = build_parser().parse_args(['run', '--team', '7teen', 'plan.md'])
        assert args.team == '7teen'

    def test_run_parser_team_flag_short(self) -> None:
        args = build_parser().parse_args(['run', '-t', '7teen', 'plan.md'])
        assert args.team == '7teen'

    def test_run_parser_plan_flag(self) -> None:
        args = build_parser().parse_args(['run', '--plan', 'my_plan.md'])
        assert args.plan == 'my_plan.md'

    def test_run_parser_plan_flag_short(self) -> None:
        args = build_parser().parse_args(['run', '-p', 'my_plan.md'])
        assert args.plan == 'my_plan.md'

    def test_run_parser_workflow_flag(self) -> None:
        args = build_parser().parse_args(['run', '--workflow', 'my_workflow', 'plan.md'])
        assert args.workflow == 'my_workflow'

    def test_run_parser_workflow_flag_short(self) -> None:
        args = build_parser().parse_args(['run', '-w', 'my_workflow', 'plan.md'])
        assert args.workflow == 'my_workflow'

    def test_run_parser_start_step_short_flag(self) -> None:
        args = build_parser().parse_args(['run', '-ss', 'implement_plan', 'plan.md'])
        assert args.start_step == 'implement_plan'

    def test_run_parser_accepts_resume_reset_scope(self) -> None:
        args = build_parser().parse_args([
            'run',
            '--resume',
            '20260101T000000Z-abc123',
            '--resume-reset-scope',
            'plan.md',
        ])
        assert args.resume == '20260101T000000Z-abc123'
        assert args.resume_reset_scope is True

    def test_resume_reset_scope_requires_explicit_run_id(self) -> None:
        stderr = io.StringIO()
        with patch(
            'aflow.cli._bootstrap_config_files',
            side_effect=AssertionError('validation should run before bootstrap'),
        ), redirect_stderr(stderr):
            result = main([
                'run',
                '--resume-reset-scope',
                'plan.md',
            ])

        assert result == 1
        assert (
            '--resume-reset-scope requires an explicit --resume RUN_ID'
            in stderr.getvalue()
        )

    def test_show_parser_accepts_optional_workflow_name(self) -> None:
        args = build_parser().parse_args(['show', 'alpha'])
        assert args.command == 'show'
        assert args.workflow_name == 'alpha'

    def test_show_parser_defaults_workflow_name_to_none(self) -> None:
        args = build_parser().parse_args(['show'])
        assert args.command == 'show'
        assert args.workflow_name is None

    def test_parser_no_legacy_flags(self) -> None:
        parser = build_parser()
        subparsers_action = next(a for a in parser._actions if hasattr(a, 'choices') and isinstance(a.choices, dict))
        run_subparser = subparsers_action.choices['run']
        run_actions = {a.dest for a in run_subparser._actions}
        assert 'harness' not in run_actions
        assert 'model' not in run_actions
        assert 'effort' not in run_actions
        assert 'profile' not in run_actions
        assert 'stagnation_limit' not in run_actions
        assert 'keep_runs' not in run_actions

    def test_resolve_run_args_plan_only_positional(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(None, None, [str(plan_file)], config)
            assert workflow is None
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_workflow_and_plan_positional(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(None, None, ['myworkflow', str(plan_file)], config)
            assert workflow == 'myworkflow'
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_plan_and_workflow_positional_reversed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(None, None, [str(plan_file), 'myworkflow'], config)
            assert workflow == 'myworkflow'
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_explicit_plan_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(str(plan_file), None, [], config)
            assert workflow is None
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_explicit_workflow_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(None, 'myworkflow', [str(plan_file)], config)
            assert workflow == 'myworkflow'
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_both_flags_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(str(plan_file), 'myworkflow', [], config)
            assert workflow == 'myworkflow'
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_duplicate_identical_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(str(plan_file), None, [str(plan_file)], config)
            assert workflow is None
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_duplicate_conflicting_plan_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file1 = Path(tmpdir) / 'plan1.md'
            plan_file1.write_text('# Plan\n')
            plan_file2 = Path(tmpdir) / 'plan2.md'
            plan_file2.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            with pytest.raises(ValueError, match="conflicting plan"):
                _resolve_run_arguments(str(plan_file1), None, [str(plan_file2)], config)

    def test_resolve_run_args_duplicate_identical_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(None, 'myworkflow', ['myworkflow', str(plan_file)], config)
            assert workflow == 'myworkflow'
            assert plan == str(plan_file)
            assert extra == ()

    def test_resolve_run_args_duplicate_conflicting_workflow_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.wf1.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[workflow.wf2.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            with pytest.raises(ValueError, match="conflicting workflow"):
                _resolve_run_arguments(None, 'wf1', ['wf2', str(plan_file)], config)

    def test_resolve_run_args_ambiguous_both_workflows_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_text = '[aflow]\n\n[workflow.wf1.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[workflow.wf2.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            with pytest.raises(ValueError, match="cannot determine"):
                _resolve_run_arguments(None, None, ['wf1', 'wf2'], config)

    def test_resolve_run_args_extra_instructions_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            workflow, plan, extra = _resolve_run_arguments(None, None, [str(plan_file), '--', 'be careful'], config)
            assert workflow is None
            assert plan == str(plan_file)
            assert extra == ('be careful',)

    def test_resolve_run_args_existing_plan_and_unknown_token_raises_error(self) -> None:
        """Reject existing-plan + unknown-token (token that is neither workflow nor file)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            with pytest.raises(ValueError, match="neither a configured workflow name nor an existing file"):
                _resolve_run_arguments(None, None, [str(plan_file), 'nonsense'], config)

    def test_resolve_run_args_unknown_token_and_existing_plan_raises_error(self) -> None:
        """Reject unknown-token + existing-plan (token that is neither workflow nor file)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            with pytest.raises(ValueError, match="neither a configured workflow name nor an existing file"):
                _resolve_run_arguments(None, None, ['nonsense', str(plan_file)], config)

    def test_resolve_run_args_both_existing_files_one_is_workflow_raises_error(self) -> None:
        """Reject when both tokens are existing files, even if one token is also a configured workflow name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file1 = Path(tmpdir) / 'plan1.md'
            plan_file1.write_text('# Plan\n')
            plan_file2_name = str(Path(tmpdir) / 'plan2.md')
            plan_file2 = Path(plan_file2_name)
            plan_file2.write_text('# Plan\n')
            config_text = f'[aflow]\n\n[workflow."{plan_file2_name}".steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{{ to = "END" }}]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            # Both tokens are existing files, and one also has a name matching a configured workflow
            # This should fail with ambiguity because both are file candidates
            with pytest.raises(ValueError, match="is a configured workflow and also resolves to an existing file"):
                _resolve_run_arguments(None, None, [str(plan_file1), plan_file2_name], config)

    def test_resolve_numeric_start_step_non_ascii_digit_treated_as_step_name(self) -> None:
        """Non-ASCII digit strings are treated as step names, not numeric indexes."""
        from aflow.cli import _resolve_numeric_start_step
        config_text = '''\
[aflow]
default_workflow = "simple"

[workflow.simple.steps.non_ascii_digit_step]
role = "architect"
prompts = ["p"]
go = [{ to = "END" }]

[harness.opencode.profiles.default]
model = "m"

[roles]
architect = "opencode.default"

[prompts]
p = "do it"
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)
            workflow = config.workflows['simple']

            # Test that non-ASCII digit characters are treated as step names
            # '١' is Arabic-Indic digit one
            resolved_name, error = _resolve_numeric_start_step('١', workflow)
            # Should not match any step, but be treated as a step name lookup (error handled at CLI level)
            assert resolved_name == '١'

    def test_resolve_numeric_start_step_underscored_digit_treated_as_step_name(self) -> None:
        """Underscored digit strings are treated as step names, not numeric indexes."""
        from aflow.cli import _resolve_numeric_start_step
        config_text = '''\
[aflow]
default_workflow = "simple"

[workflow.simple.steps.1_0]
role = "architect"
prompts = ["p"]
go = [{ to = "END" }]

[harness.opencode.profiles.default]
model = "m"

[roles]
architect = "opencode.default"

[prompts]
p = "do it"
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)
            workflow = config.workflows['simple']

            # '1_0' should be treated as step name, not numeric index 10
            resolved_name, error = _resolve_numeric_start_step('1_0', workflow)
            assert error is None
            assert resolved_name == '1_0'

    def test_install_subcommand_exposes_destination_and_yes(self) -> None:
        args = build_parser().parse_args(['install-skills', '--yes'])
        assert args.destination is None
        assert args.yes is True

    def test_root_help_mentions_install_skills_command(self) -> None:
        help_text = build_parser().format_help()
        assert "install-skills" in help_text

    def test_cli_bootstraps_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            stderr = io.StringIO()
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                with redirect_stderr(stderr):
                    result = main([])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            config_file = home_dir / '.config' / 'aflow' / 'aflow.toml'
            workflows_file = home_dir / '.config' / 'aflow' / 'workflows.toml'
            assert config_file.exists()
            assert workflows_file.exists()
            assert result == 0
            output = stderr.getvalue()
            assert str(config_file) in output
            assert str(workflows_file) in output

    def test_cli_run_bootstraps_missing_config_and_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            stderr = io.StringIO()
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                with redirect_stderr(stderr):
                    result = main(['run', 'plan.md'])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            config_file = home_dir / '.config' / 'aflow' / 'aflow.toml'
            workflows_file = home_dir / '.config' / 'aflow' / 'workflows.toml'
            assert config_file.exists()
            assert workflows_file.exists()
            assert result == 0
            output = stderr.getvalue()
            assert str(config_file) in output
            assert str(workflows_file) in output
            assert 'plan file does not exist' not in output

    def test_cli_rejects_missing_default_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', 'plan.md'])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_cli_workflow_override(self) -> None:
        import aflow.cli as cli_module
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[workflow.other.steps.review]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                with patch('aflow.api.startup.probe_worktree', return_value=None):
                    result = main(['run', 'other', str(plan_path)])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 0

    def test_cli_install_skills_runs_without_config_bootstrap(self) -> None:
        import aflow.cli as cli_module

        calls: list[tuple[str | None, bool, tuple[str, ...] | None, bool]] = []
        original = cli_module.install_skills
        try:
            def fake_install_skills(
                destination: str | None = None,
                *,
                yes: bool = False,
                only_skills: tuple[str, ...] | None = None,
                include_optional: bool = False,
            ) -> None:
                calls.append((destination, yes, only_skills, include_optional))

            cli_module.install_skills = fake_install_skills
            result = main(['install-skills', '/tmp/dest', '--yes'])
        finally:
            cli_module.install_skills = original
        assert result == 0
        assert calls == [('/tmp/dest', True, None, False)]

    def test_cli_rejects_unknown_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', 'nonexistent', 'plan.md'])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_cli_show_all_workflows_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from contextlib import redirect_stdout
            home_dir = Path(tmpdir)
            _write_config(
                home_dir,
                '[workflow.alpha.steps.review]\nrole = "reviewer"\nprompts = ["p"]\ngo = [{ to = "implement" }]\n\n'
                '[workflow.alpha.steps.implement]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.alpha]\nexclude = ["review"]\nteam = "7teen"\n\n'
                '[workflow.beta.steps.ship]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.claude.profiles.opus]\nmodel = "m"\n\n'
                '[harness.codex.profiles.default]\nmodel = "m"\n\n'
                '[roles]\nreviewer = "claude.opus"\narchitect = "codex.default"\n\n'
                '[teams.7teen.roles]\narchitect = "codex.default"\n\n'
                '[teams.reviewers.roles]\nreviewer = "claude.opus"\n\n'
                '[prompts]\np = "do it"\n',
            )
            stdout = io.StringIO()
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                with redirect_stdout(stdout):
                    result = main(['show'])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 0
            output = stdout.getvalue()
            assert 'Roles / Teams' in output
            assert 'reviewer' in output
            assert 'architect' in output
            assert '7teen' in output
            assert 'reviewers' in output
            assert 'alpha' in output
            assert 'beta' in output

    def test_cli_show_single_workflow_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from contextlib import redirect_stdout
            home_dir = Path(tmpdir)
            _write_config(
                home_dir,
                '[workflow.alpha.steps.review]\nrole = "reviewer"\nprompts = ["p"]\ngo = [{ to = "implement" }]\n\n'
                '[workflow.alpha.steps.implement]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.alpha]\nexclude = ["review"]\nteam = "7teen"\n\n'
                '[workflow.beta.steps.ship]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.claude.profiles.opus]\nmodel = "m"\n\n'
                '[harness.codex.profiles.default]\nmodel = "m"\n\n'
                '[roles]\nreviewer = "claude.opus"\narchitect = "codex.default"\n\n'
                '[teams.7teen.roles]\narchitect = "codex.default"\n\n'
                '[teams.reviewers.roles]\nreviewer = "claude.opus"\n\n'
                '[prompts]\np = "do it"\n',
            )
            stdout = io.StringIO()
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                with redirect_stdout(stdout):
                    result = main(['show', 'alpha'])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 0
            output = stdout.getvalue()
            assert 'Roles / Teams' in output
            assert 'architect' in output
            assert '7teen' in output
            assert 'reviewers' not in output
            assert 'review' in output
            assert 'implement' in output

    def test_cli_rejects_unknown_show_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(
                home_dir,
                '[workflow.alpha.steps.review]\nrole = "reviewer"\nprompts = ["p"]\ngo = [{ to = "implement" }]\n\n'
                '[workflow.alpha.steps.implement]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.claude.profiles.opus]\nmodel = "m"\n\n'
                '[harness.codex.profiles.default]\nmodel = "m"\n\n'
                '[roles]\nreviewer = "claude.opus"\narchitect = "codex.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            stderr = io.StringIO()
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                with redirect_stderr(stderr):
                    result = main(['show', 'missing'])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1
            assert "unknown workflow 'missing'" in stderr.getvalue()

    def test_run_parser_accepts_start_step(self) -> None:
        args = build_parser().parse_args(['run', '--start-step', 'implement_plan', 'plan.md'])
        assert args.start_step == 'implement_plan'

    def test_run_parser_start_step_defaults_to_none(self) -> None:
        args = build_parser().parse_args(['run', 'plan.md'])
        assert args.start_step is None

    def test_run_parser_start_step_with_workflow_name_and_plan(self) -> None:
        args = build_parser().parse_args(['run', '--start-step', 'implement_plan', 'my_workflow', 'plan.md'])
        assert args.start_step == 'implement_plan'
        assert 'my_workflow' in args.run_args
        assert 'plan.md' in args.run_args

    def test_run_parser_start_step_with_extra_instructions(self) -> None:
        args = build_parser().parse_args(['run', '--start-step', 'implement_plan', 'plan.md', '--', 'be careful'])
        assert args.start_step == 'implement_plan'
        assert 'plan.md' in args.run_args
        assert '--' in args.run_args
        assert 'be careful' in args.run_args

    def test_cli_start_step_must_be_valid_workflow_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi_step"\n\n[workflow.multi_step.steps.review_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.multi_step.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', '--start-step', 'nonexistent', str(plan_path)])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_run_parser_accepts_numeric_start_step_short_flag(self) -> None:
        args = build_parser().parse_args(['run', '-ss', '2', 'plan.md'])
        assert args.start_step == '2'

    def test_run_parser_accepts_numeric_start_step_long_flag(self) -> None:
        args = build_parser().parse_args(['run', '--start-step', '2', 'plan.md'])
        assert args.start_step == '2'

    def test_cli_resolves_numeric_start_step_to_second_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi_step"\n\n[workflow.multi_step.steps.review_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.multi_step.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', '--start-step', '2', str(plan_path)])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_cli_numeric_start_step_zero_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi_step"\n\n[workflow.multi_step.steps.review_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.multi_step.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', '--start-step', '0', str(plan_path)])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_cli_numeric_start_step_out_of_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi_step"\n\n[workflow.multi_step.steps.review_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.multi_step.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', '--start-step', '99', str(plan_path)])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_cli_named_start_step_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi_step"\n\n[workflow.multi_step.steps.review_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.multi_step.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', '--start-step', 'implement_plan', str(plan_path)])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_cli_rejects_numeric_start_step_on_complete_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "multi_step"\n\n[workflow.multi_step.steps.review_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.multi_step.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n')
            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                result = main(['run', '--start-step', '2', str(plan_path)])
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home
            assert result == 1

    def test_resolve_run_args_workflow_and_missing_plan_positional(self) -> None:
        """Preserve missing-plan-file behavior when workflow + non-existent plan are positionals."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_text = '[aflow]\n\n[workflow.simple.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            # Pass a workflow name and a non-existent plan path as positionals
            workflow, plan, extra = _resolve_run_arguments(None, None, ['simple', 'missing-plan.md'], config)
            assert workflow == 'simple'
            assert plan == 'missing-plan.md'
            assert extra == ()

    def test_resolve_run_args_equivalent_plan_paths_different_spelling(self) -> None:
        """Accept equivalent plan paths with different spellings (e.g., /abs/path vs ~/path)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / 'plan.md'
            plan_file.write_text('# Plan\n')
            config_text = '[aflow]\n\n[workflow.myworkflow.steps.impl]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n'
            config_path = _write_config(Path(tmpdir), config_text)
            config = load_workflow_config(config_path)

            # Use the same file with explicit path and canonical path
            abs_path = str(plan_file)
            # Pass the same absolute path but via positional and flag - should be accepted
            workflow, plan, extra = _resolve_run_arguments(abs_path, None, [abs_path], config)
            assert workflow is None
            assert plan == abs_path
            assert extra == ()

    def test_resolve_run_args_digit_like_step_name_not_numeric_index(self) -> None:
        """Treat digit-like but non-plain step names as step names, not numeric indexes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            config_text = '''\
[aflow]
default_workflow = "simple"

[workflow.simple.steps.1_0]
role = "architect"
prompts = ["p"]
go = [{ to = "other" }]

[workflow.simple.steps.other]
role = "architect"
prompts = ["p"]
go = [{ to = "END" }]

[harness.opencode.profiles.default]
model = "m"

[roles]
architect = "opencode.default"

[prompts]
p = "do it"
'''
            config_path = _write_config(home_dir, config_text)
            plan_path = Path(tmpdir) / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: One\n- [ ] step\n')

            resolved_step_from_cli: list[str | None] = []

            def capture_runner(*args, **kwargs):
                config_arg = kwargs.get('config') or args[0]
                resolved_step_from_cli.append(config_arg.start_step)
                return type(
                    "RunResult",
                    (),
                    {"turns_completed": 0, "end_reason": "already_complete"},
                )()

            original_home = os.environ.get('HOME')
            try:
                os.environ['HOME'] = str(home_dir)
                with patch('aflow.api.startup.probe_worktree', return_value=None), \
                     patch('aflow.api.runner.run_workflow', side_effect=capture_runner):
                    result = main(['run', '--start-step', '1_0', str(plan_path)])
                assert result == 0
                assert resolved_step_from_cli == ['1_0']
            finally:
                if original_home is None:
                    os.environ.pop('HOME', None)
                else:
                    os.environ['HOME'] = original_home

    def test_run_help_text_includes_flag_aliases(self) -> None:
        """Verify --start-step/-ss, --plan/-p, --workflow/-w are documented in help."""
        # Test that RUN_HELP contains the key information
        assert '--plan/-p' in RUN_HELP
        assert '--workflow/-w' in RUN_HELP
        assert '--start-step/-ss' in RUN_HELP
        assert '--team/-t' in RUN_HELP
        assert '--max-turns/-mt' in RUN_HELP


class WorkflowStartupFlowTests(unittest.TestCase):

    def _write_workflow_config(self, home_dir: Path, *, workflow_name: str, multi_step: bool) -> None:
        if multi_step:
            workflow_block = (
                f'[workflow.{workflow_name}.steps.review_plan]\n'
                'role = "architect"\n'
                'prompts = ["review_prompt"]\n'
                'go = [{ to = "implement_plan" }]\n\n'
                f'[workflow.{workflow_name}.steps.implement_plan]\n'
                'role = "architect"\n'
                'prompts = ["impl_prompt"]\n'
                'go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }, { to = "review_plan" }]\n'
            )
        else:
            workflow_block = (
                f'[workflow.{workflow_name}.steps.implement_plan]\n'
                'role = "architect"\n'
                'prompts = ["impl_prompt"]\n'
                'go = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }, { to = "implement_plan" }]\n'
            )
        _write_config(
            home_dir,
            (
                f'[aflow]\n'
                f'default_workflow = "{workflow_name}"\n\n'
                '[roles]\n'
                'architect = "codex.default"\n\n'
                '[harness.codex.profiles.default]\n'
                'model = "gpt-5.4"\n\n'
                f'{workflow_block}'
                '[prompts]\n'
                'review_prompt = "Review {ACTIVE_PLAN_PATH}."\n'
                'impl_prompt = "Implement from {ACTIVE_PLAN_PATH}."\n'
            ),
        )

    def test_pick_workflow_step_reprompts_on_invalid_input(self) -> None:
        steps = {
            'review_plan': WorkflowStepConfig(role='architect', prompts=('review_prompt',), go=(GoTransition(to='implement_plan'),)),
            'implement_plan': WorkflowStepConfig(role='architect', prompts=('impl_prompt',), go=(GoTransition(to='END'),)),
        }
        with patch('builtins.input', side_effect=['abc', '2']) as mock_input:
            chosen = _pick_workflow_step(steps)
        assert chosen == 'implement_plan'
        assert mock_input.call_count == 2

    def test_confirm_startup_recovery_accepts_yes_and_rejects_no(self) -> None:
        with patch('builtins.input', return_value='yes'):
            assert _confirm_startup_recovery('error: boom') is True
        with patch('builtins.input', return_value='n'):
            assert _confirm_startup_recovery('error: boom') is False

    def test_maybe_move_completed_plan_to_done_defaults_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n')

            with patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), \
                 patch('builtins.input', return_value=''):
                moved_path = _maybe_move_completed_plan_to_done(
                    repo_root,
                    plan_path,
                    is_complete=True,
                )

            expected_path = repo_root / 'plans' / 'done' / 'plan.md'
            assert moved_path.resolve() == expected_path.resolve()
            assert expected_path.read_text(encoding='utf-8') == '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n'
            assert not plan_path.exists()

    def test_cli_rejects_start_step_on_complete_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='multi_step', multi_step=True)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Done\n- [x] step one\n')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=repo_root / 'count.txt',
                home_dir=home_dir,
                completed_plan_path=repo_root / 'completed.md',
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('builtins.input', side_effect=AssertionError('unexpected input')), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', '--start-step', 'implement_plan', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 1
            assert 'plan is already complete, --start-step has no effect' in stderr_capture.getvalue()
            assert not (repo_root / '.aflow').exists()

    def test_cli_prompts_for_start_step_on_half_done_multi_step_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='multi_step', multi_step=True)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = repo_root / 'count.txt'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: First\n'
                '- [x] step one\n\n'
                '### [ ] Checkpoint 2: Second\n'
                '- [ ] step two\n',
            )
            _write_plan(
                completed_plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: First\n'
                '- [x] step one\n\n'
                '### [x] Checkpoint 2: Second\n'
                '- [x] step two\n',
            )
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=True), \
                         patch('sys.stdout.isatty', return_value=True), \
                         patch('builtins.input', side_effect=['2']), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['selected_start_step'] == 'implement_plan'
            assert run_json['startup_recovery_used'] is False
            assert run_json['startup_recovery_reason'] is None

    def test_cli_moves_completed_in_progress_plan_to_done_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='simple', multi_step=False)
            plan_path = repo_root / 'plans' / 'in-progress' / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = repo_root / 'count.txt'
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_plan(completed_plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()
            stdout_capture.isatty = lambda: True  # type: ignore[attr-defined]
            stderr_capture.isatty = lambda: True  # type: ignore[attr-defined]
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=True), \
                         patch('builtins.input', return_value=''), \
                         patch('sys.stdout', stdout_capture), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe

            assert result == 0
            moved_path = repo_root / 'plans' / 'done' / 'plan.md'
            assert moved_path.resolve().exists()
            assert not plan_path.exists()
            assert "Workflow 'simple' completed after 1 turn because DONE evaluated true." in stdout_capture.getvalue()
            assert 'error:' not in stderr_capture.getvalue().lower()
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['step_name'] == 'implement_plan'
            assert turn_result['status'] == 'completed'

    def test_cli_skips_start_step_picker_on_fresh_multi_step_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='multi_step', multi_step=True)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = repo_root / 'count.txt'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [ ] Checkpoint 1: First\n'
                '- [ ] step one\n\n'
                '### [ ] Checkpoint 2: Second\n'
                '- [ ] step two\n',
            )
            _write_plan(
                completed_plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: First\n'
                '- [x] step one\n\n'
                '### [x] Checkpoint 2: Second\n'
                '- [x] step two\n',
            )
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('builtins.input', side_effect=AssertionError('unexpected input')):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['selected_start_step'] == 'review_plan'
            assert run_json['startup_recovery_used'] is False
            assert run_json['startup_recovery_reason'] is None
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['step_name'] == 'review_plan'
            assert turn_result['status'] == 'completed'

    def test_cli_startup_recovery_prompts_and_seeds_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='multi_step', multi_step=True)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = tmp_path / 'completed.md'
            count_file = repo_root / 'count.txt'
            broken_plan = '# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n'
            complete_plan = '# Plan\n\n### [x] Checkpoint 1: Broken\n- [x] step one\n'
            _write_plan(plan_path, broken_plan)
            _write_plan(completed_plan_path, complete_plan)
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=True), \
                         patch('sys.stdout.isatty', return_value=True), \
                         patch('builtins.input', side_effect=['y', '2']), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['selected_start_step'] == 'implement_plan'
            assert run_json['startup_recovery_used'] is True
            assert 'inconsistent checkpoint state' in run_json['startup_recovery_reason']
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['was_retry'] is True
            assert turn_result['retry_attempt'] == 1
            user_prompt = (run_dirs[0] / 'turns' / 'turn-001' / 'user-prompt.txt').read_text(encoding='utf-8')
            assert 'inconsistent checkpoint state' in user_prompt.lower()

    def test_cli_declining_startup_recovery_exits_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='multi_step', multi_step=True)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=repo_root / 'count.txt',
                home_dir=home_dir,
                completed_plan_path=repo_root / 'completed.md',
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=True), \
                         patch('sys.stdout.isatty', return_value=True), \
                         patch('builtins.input', side_effect=['n']), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 1
            assert 'startup aborted' in stderr_capture.getvalue().lower()
            assert not (repo_root / '.aflow').exists()

    def test_cli_requires_tty_for_multi_step_start_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='multi_step', multi_step=True)
            plan_path = repo_root / 'plan.md'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: First\n'
                '- [x] step one\n\n'
                '### [ ] Checkpoint 2: Second\n'
                '- [ ] step two\n',
            )
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=repo_root / 'count.txt',
                home_dir=home_dir,
                completed_plan_path=repo_root / 'completed.md',
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=False), \
                         patch('sys.stdout.isatty', return_value=False), \
                         patch('builtins.input', side_effect=AssertionError('unexpected input')), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 1
            stderr_output = stderr_capture.getvalue().lower()
            assert 're-run with --start-step' in stderr_output
            assert 'available steps' in stderr_output

    def test_cli_requires_tty_for_startup_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='multi_step', multi_step=True)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [x] Checkpoint 1: Broken\n- [ ] step one\n')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=repo_root / 'count.txt',
                home_dir=home_dir,
                completed_plan_path=repo_root / 'completed.md',
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=False), \
                         patch('sys.stdout.isatty', return_value=False), \
                         patch('builtins.input', side_effect=AssertionError('unexpected input')), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 1
            stderr_output = stderr_capture.getvalue().lower()
            assert 'interactive confirmation is required' in stderr_output
            assert 'inconsistent checkpoint state' in stderr_output

    def test_cli_pre_handoff_prompts_and_accepts_pristine_base_head_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='single_step', multi_step=False)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = repo_root / 'completed.md'
            count_file = repo_root / 'count.txt'
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`'),
            )
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'follow-up\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            _write_plan(
                completed_plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{current_head}`').replace(
                    '### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First'
                ).replace('- [ ] step one', '- [x] step one'),
            )
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            import aflow.api.startup as startup_module
            original_probe = cli_module.probe_worktree
            original_startup_probe = startup_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    startup_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=True), \
                         patch('sys.stdout.isatty', return_value=True), \
                         patch('builtins.input', side_effect=['y']), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
                startup_module.probe_worktree = original_startup_probe
            assert result == 0
            assert 'Pre-Handoff Base HEAD' in plan_path.read_text(encoding='utf-8')
            assert f'`{current_head}`' in plan_path.read_text(encoding='utf-8')
            assert 'startup aborted' not in stderr_capture.getvalue().lower()

    def test_cli_pre_handoff_prompts_and_accepts_pristine_empty_base_head_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='single_step', multi_step=False)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = repo_root / 'completed.md'
            count_file = repo_root / 'count.txt'
            plan_text = _VALID_GIT_TRACKING_PLAN.replace('`base`', '``')
            _write_plan(plan_path, plan_text)
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'follow-up\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')
            rc, current_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            _write_plan(
                completed_plan_path,
                plan_text.replace('### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First')
                .replace('- [ ] step one', '- [x] step one')
                .replace('``', f'`{current_head}`', 1),
            )
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            import aflow.api.startup as startup_module
            original_probe = cli_module.probe_worktree
            original_startup_probe = startup_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    startup_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=True), \
                         patch('sys.stdout.isatty', return_value=True), \
                         patch('builtins.input', side_effect=['y']), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
                startup_module.probe_worktree = original_startup_probe
            assert result == 0
            assert f'`{current_head}`' in plan_path.read_text(encoding='utf-8')
            assert 'startup aborted' not in stderr_capture.getvalue().lower()

    def test_cli_pre_handoff_declines_pristine_base_head_refresh_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='single_step', multi_step=False)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = repo_root / 'completed.md'
            count_file = repo_root / 'count.txt'
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`'),
            )
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'follow-up\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')
            _write_plan(
                completed_plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`').replace(
                    '### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First'
                ).replace('- [ ] step one', '- [x] step one'),
            )
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            import aflow.api.startup as startup_module
            original_probe = cli_module.probe_worktree
            original_startup_probe = startup_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    startup_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=True), \
                         patch('sys.stdout.isatty', return_value=True), \
                         patch('builtins.input', side_effect=['n']), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
                startup_module.probe_worktree = original_startup_probe
            assert result == 1
            assert 'startup aborted' in stderr_capture.getvalue().lower()
            assert f'`{initial_head}`' in plan_path.read_text(encoding='utf-8')
            assert not (repo_root / '.aflow').exists()

    def test_cli_pre_handoff_refuses_pristine_base_head_refresh_non_interactively(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, initial_head, _ = _run_git_in_test(['rev-parse', 'HEAD'], cwd=repo_root)
            assert rc == 0
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='single_step', multi_step=False)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = repo_root / 'completed.md'
            count_file = repo_root / 'count.txt'
            _write_plan(
                plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`'),
            )
            _git_commit_file(repo_root, plan_path)
            _write_plan(repo_root / 'notes.txt', 'follow-up\n')
            _git_commit_file(repo_root, repo_root / 'notes.txt')
            _write_plan(
                completed_plan_path,
                _VALID_GIT_TRACKING_PLAN.replace('`base`', f'`{initial_head}`').replace(
                    '### [ ] Checkpoint 1: First', '### [x] Checkpoint 1: First'
                ).replace('- [ ] step one', '- [x] step one'),
            )
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            import aflow.api.startup as startup_module
            original_probe = cli_module.probe_worktree
            original_startup_probe = startup_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    startup_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=False), \
                         patch('sys.stdout.isatty', return_value=False), \
                         patch('builtins.input', side_effect=AssertionError('unexpected input')), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
                startup_module.probe_worktree = original_startup_probe
            assert result == 1
            stderr_output = stderr_capture.getvalue().lower()
            assert 'interactive confirmation is required' in stderr_output
            assert 'pre-handoff base head' in stderr_output

    def test_cli_one_step_workflow_skips_picker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(home_dir, workflow_name='single_step', multi_step=False)
            plan_path = repo_root / 'plan.md'
            completed_plan_path = repo_root / 'completed.md'
            count_file = repo_root / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_plan(completed_plan_path, '# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n')
            _write_workflow_harness_script(repo_root, 'codex')
            env = _workflow_test_env(
                repo_root,
                scenario='complete',
                plan_path=plan_path,
                count_file=count_file,
                home_dir=home_dir,
                completed_plan_path=completed_plan_path,
            )
            original_cwd = Path.cwd()
            import io
            import aflow.cli as cli_module
            original_probe = cli_module.probe_worktree
            stderr_capture = io.StringIO()
            try:
                with patch.dict(os.environ, env, clear=True):
                    cli_module.probe_worktree = lambda _: None
                    os.chdir(repo_root)
                    with patch('sys.stdin.isatty', return_value=False), \
                         patch('sys.stdout.isatty', return_value=False), \
                         patch('builtins.input', side_effect=AssertionError('picker should not run')), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
            assert result == 0
            run_dirs = sorted((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            run_json = json.loads((run_dirs[0] / 'run.json').read_text(encoding='utf-8'))
            assert run_json['selected_start_step'] == 'implement_plan'
            assert run_json['startup_recovery_used'] is False
            turn_result = json.loads((run_dirs[0] / 'turns' / 'turn-001' / 'result.json').read_text(encoding='utf-8'))
            assert turn_result['step_name'] == 'implement_plan'

    def test_resume_prompt_accepted_returns_resume_context(self) -> None:
        import aflow.cli as cli_module
        from aflow.run_state import ResumeContext

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "test_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "active_plan_path": str(Path("/fake/plan-cp01-v01.md")),
            "status": "failed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "last_run_id_file")), \
             patch('aflow.cli.load_run_json', return_value=prev_run), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             patch('builtins.input', return_value='y'):
            result = cli_module._detect_resume_candidate(
                repo_root=Path("/fake/repo").resolve(),
                workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
            )

        assert isinstance(result, ResumeContext)
        assert result.resumed_from_run_id == "20260101T000000Z-abc123"
        assert result.feature_branch == "feature/test-branch"
        assert result.worktree_path == Path("/fake/repo/.git/worktrees/test")
        assert result.active_plan_path == Path("/fake/plan-cp01-v01.md")

    def test_resume_restores_waiting_override_state(self) -> None:
        import aflow.cli as cli_module

        run_id = "20260101T000000Z-abc123"
        repo_root = Path("/fake/repo").resolve()
        prev_run = {
            "repo_root": str(repo_root),
            "workflow_name": "test_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "effective_max_turns": 9,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/worktree")),
            "main_branch": "main",
            "status": "waiting_for_valid_override",
            "last_snapshot": {"is_complete": False},
            "override_file_present": True,
            "override_result": {
                "status": "rejected",
                "digest": "abc",
                "message": "unknown team",
                "applied": False,
            },
        }

        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_id), "explicit_run_id"),
        ), patch(
            "aflow.cli.load_run_json",
            return_value=prev_run,
        ), patch(
            "sys.stdin.isatty",
            return_value=True,
        ), patch(
            "sys.stdout.isatty",
            return_value=True,
        ), patch(
            "builtins.input",
            return_value="y",
        ):
            result = cli_module._detect_resume_candidate(
                repo_root=repo_root,
                workflow_config=type(
                    "obj",
                    (object,),
                    {"setup": ("worktree", "branch")},
                )(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
            )

        assert result is not None
        assert result.override_result is not None
        assert result.override_result.status == "rejected"
        assert result.effective_max_turns == 9
        assert result.override_file_present is False
        assert result.override_source_run_dir == repo_root / ".aflow" / "runs" / run_id

    def test_resume_override_source_resolution_matrix(self) -> None:
        import aflow.cli as cli_module

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            plan_path = repo_root / "plan.md"
            run_id = "20260101T000000Z-abc123"
            run_dir = repo_root / ".aflow" / "runs" / run_id
            run_dir.mkdir(parents=True)
            base_run = {
                "repo_root": str(repo_root),
                "workflow_name": "test_workflow",
                "plan_path": str(plan_path),
                "team": "base",
                "selected_start_step": None,
                "max_turns": 15,
                "extra_instructions": [],
                "lifecycle_setup": ["worktree", "branch"],
                "lifecycle_teardown": [],
                "feature_branch": "feature/test-branch",
                "worktree_path": str(repo_root / "worktree"),
                "main_branch": "main",
                "status": "failed",
                "last_snapshot": {"is_complete": False},
            }
            accepted_text = 'team = "strong"\n'
            accepted_digest = hashlib.sha256(
                accepted_text.encode("utf-8")
            ).hexdigest()
            cases = (
                ("no_prior_no_file", None, None, False, False),
                (
                    "post_stop_valid",
                    None,
                    accepted_text,
                    True,
                    True,
                ),
                (
                    "post_stop_malformed",
                    None,
                    'team = "strong"\ninvalid = [\n',
                    True,
                    True,
                ),
                (
                    "accepted_applied_unchanged",
                    {
                        "status": "accepted",
                        "digest": accepted_digest,
                        "message": "accepted",
                        "team": "strong",
                        "applied": True,
                    },
                    accepted_text,
                    False,
                    True,
                ),
                (
                    "accepted_applied_changed",
                    {
                        "status": "accepted",
                        "digest": accepted_digest,
                        "message": "accepted",
                        "team": "strong",
                        "applied": True,
                    },
                    'team = "base"\n',
                    True,
                    True,
                ),
                (
                    "accepted_not_applied",
                    {
                        "status": "accepted",
                        "digest": accepted_digest,
                        "message": "accepted",
                        "team": "strong",
                        "applied": False,
                    },
                    None,
                    True,
                    False,
                ),
                (
                    "rejected_present",
                    {
                        "status": "rejected",
                        "digest": accepted_digest,
                        "message": "unknown team",
                        "applied": False,
                    },
                    'team = "strong"\n',
                    True,
                    True,
                ),
                (
                    "rejected_missing",
                    {
                        "status": "rejected",
                        "digest": accepted_digest,
                        "message": "unknown team",
                        "applied": False,
                    },
                    None,
                    True,
                    False,
                ),
            )

            for (
                name,
                override_result,
                override_text,
                expect_source,
                expect_present,
            ) in cases:
                with self.subTest(name=name):
                    override_path = run_dir / "overrides.toml"
                    override_path.unlink(missing_ok=True)
                    if override_text is not None:
                        override_path.write_text(
                            override_text,
                            encoding="utf-8",
                        )
                    prev_run = dict(base_run)
                    if override_result is not None:
                        prev_run["override_result"] = override_result

                    with patch(
                        "aflow.cli.resolve_run_id",
                        return_value=(run_dir, "explicit_run_id"),
                    ), patch(
                        "aflow.cli.load_run_json",
                        return_value=prev_run,
                    ):
                        result = cli_module._detect_resume_candidate(
                            repo_root=repo_root,
                            workflow_config=type(
                                "obj",
                                (object,),
                                {"setup": ("worktree", "branch")},
                            )(),
                            workflow_name="test_workflow",
                            plan_path=plan_path,
                            team="base",
                            selected_start_step=None,
                            max_turns=15,
                            extra_instructions=(),
                            requested_run_id=run_id,
                            require_resume=True,
                        )

                    assert result is not None
                    assert (
                        result.override_source_run_dir == run_dir
                    ) is expect_source
                    assert result.override_file_present is expect_present
                    assert (
                        result.override_result is not None
                    ) is (override_result is not None)

    def test_resume_restores_step_from_unfinished_active_turn(self) -> None:
        import aflow.cli as cli_module

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            plan_path = repo_root / "plan.md"
            run_dir = repo_root / ".aflow" / "runs" / "20260101T000000Z-abc123"
            turn_dir = run_dir / "turns" / "turn-006"
            turn_dir.mkdir(parents=True)
            (turn_dir / "result.json").write_text(
                json.dumps({
                    "status": "starting",
                    "step_name": "review_cp_implementation",
                }),
                encoding="utf-8",
            )
            prev_run = {
                "repo_root": str(repo_root),
                "workflow_name": "test_workflow",
                "plan_path": str(plan_path),
                "team": "ds4_pro",
                "selected_start_step": "implement_plan",
                "max_turns": 60,
                "extra_instructions": [],
                "lifecycle_setup": ["worktree", "branch"],
                "lifecycle_teardown": ["merge", "rm_worktree"],
                "feature_branch": "feature/test-branch",
                "worktree_path": str(repo_root / "worktree"),
                "main_branch": "main",
                "status": "running",
                "active_turn": 6,
                "current_step_name": "review_cp_implementation",
                "last_snapshot": {"is_complete": False},
            }

            with patch(
                "aflow.cli.resolve_run_id",
                return_value=(run_dir, "explicit_run_id"),
            ), patch("aflow.cli.load_run_json", return_value=prev_run):
                result = cli_module._detect_resume_candidate(
                    repo_root=repo_root,
                    workflow_config=type(
                        "obj",
                        (object,),
                        {"setup": ("worktree", "branch")},
                    )(),
                    workflow_name="test_workflow",
                    plan_path=plan_path,
                    team="ds4_pro",
                    selected_start_step="implement_plan",
                    max_turns=60,
                    extra_instructions=(),
                    requested_run_id=run_dir.name,
                    require_resume=True,
                )

        assert result is not None
        assert result.interrupted_step_name == "review_cp_implementation"

    def test_resume_reset_scope_preserves_history_and_lifecycle_only(self) -> None:
        import aflow.cli as cli_module

        repo_root = Path("/fake/repo").resolve()
        plan_path = Path("/fake/plan.md").resolve()
        previous_attempt = {
            "turn_number": 25,
            "step_name": "implement_plan",
            "role": "worker",
            "team": "terra_xhigh",
            "selector": "codex.terraxhigh",
            "outcome": "review_rejected",
            "manager_decision_number": 37,
        }
        prev_run = {
            "repo_root": str(repo_root),
            "workflow_name": "test_workflow",
            "plan_path": str(plan_path),
            "team": "ds4_pro",
            "selected_start_step": "implement_plan",
            "max_turns": 60,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(repo_root / "worktree"),
            "main_branch": "main",
            "active_plan_path": str(Path("/fake/plan-cp09-v02.md")),
            "status": "failed",
            "last_snapshot": {"is_complete": False},
            "manager_decision_number": 38,
            "manager_history": [{
                "decision_number": 38,
                "level": "full",
                "trigger": "reviewer_rejection",
                "action": "stop",
                "reason": "Owner repartition is required.",
                "artifact_path": "manager/decision-038",
            }],
            "semantic_stall_count": 3,
            "reviewer_rejection_count": 3,
            "implementation_attempts": {"old-scope": [previous_attempt]},
            "active_implementation_scope": {
                "scope_id": "old-scope",
                "original_plan_path": str(plan_path),
                "checkpoint_index": 9,
                "checkpoint_name": "Oversized checkpoint",
                "opened_turn_number": 1,
                "awaiting_review": True,
                "carried_reviewer_rejection_count": 3,
            },
            "pending_manager_notes": {
                "target_step": "implement_plan",
                "notes": ["Split the checkpoint."],
                "decision_number": 38,
            },
            "pending_step_team_override": {
                "target_step": "implement_plan",
                "role": "worker",
                "source_team": "terra_xhigh",
                "target_team": "sol_medium",
                "selector": "codex.sol56medium",
                "decision_number": 38,
            },
            "pending_boundary_decision": {
                "finalized_turn_number": 28,
                "decision_number": 38,
                "action": "stop",
                "proposed_action": "implement_plan",
            },
            "pending_repartition": {
                "schema_version": 1,
                "decision_number": 38,
                "scope_id": "old-scope",
                "stage": "semantically_validated",
                "envelope_sha256": "e" * 64,
                "source_plan_sha256": "s" * 64,
            },
            "last_manager_report_path": "manager-report.md",
        }

        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path("20260101T000000Z-abc123"), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run):
            result = cli_module._detect_resume_candidate(
                repo_root=repo_root,
                workflow_config=type(
                    "obj",
                    (object,),
                    {"setup": ("worktree", "branch")},
                )(),
                workflow_name="test_workflow",
                plan_path=plan_path,
                team="ds4_pro",
                selected_start_step="implement_plan",
                max_turns=60,
                extra_instructions=(),
                requested_run_id="20260101T000000Z-abc123",
                require_resume=True,
                reset_scope=True,
            )

        assert result is not None
        assert result.feature_branch == "feature/test-branch"
        assert result.worktree_path == repo_root / "worktree"
        assert result.setup == ("worktree", "branch")
        assert result.teardown == ("merge", "rm_worktree")
        assert result.manager_decision_number == 38
        assert len(result.manager_history) == 1
        assert result.implementation_attempts == {}
        assert result.active_plan_path is None
        assert result.interrupted_step_name is None
        assert result.semantic_stall_count == 0
        assert result.reviewer_rejection_count == 0
        assert result.active_implementation_scope is None
        assert result.pending_manager_notes is None
        assert result.pending_step_team_override is None
        assert result.pending_boundary_decision is None
        assert result.pending_repartition is None
        assert result.repartition_artifact_bytes == {}
        assert result.last_manager_report_path is None

    def test_resume_carries_pending_repartition_artifacts_before_pruning(self) -> None:
        import aflow.cli as cli_module

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            plan_path = repo_root / "plan.md"
            plan_path.write_text(_VALID_PLAN, encoding="utf-8")
            run_dir = repo_root / ".aflow" / "runs" / "prior-run"
            attempt = (
                run_dir / "manager" / "decision-001"
                / "repartition" / "attempt-001"
            )
            attempt.mkdir(parents=True)
            candidate_rel = (
                "manager/decision-001/repartition/attempt-001/candidate-plan.md"
            )
            candidate_bytes = b"# validated candidate\n"
            (run_dir / candidate_rel).write_bytes(candidate_bytes)
            prev_run = {
                "repo_root": str(repo_root),
                "workflow_name": "test_workflow",
                "plan_path": str(plan_path),
                "team": None,
                "selected_start_step": None,
                "max_turns": 15,
                "extra_instructions": [],
                "lifecycle_setup": ["worktree", "branch"],
                "lifecycle_teardown": [],
                "feature_branch": "feature/repartition",
                "worktree_path": str(repo_root / "worktree"),
                "main_branch": "main",
                "status": "failed",
                "last_snapshot": {"is_complete": False},
                "pending_repartition": {
                    "schema_version": 1,
                    "decision_number": 1,
                    "scope_id": "scope",
                    "stage": "semantically_validated",
                    "envelope_sha256": "e" * 64,
                    "source_plan_sha256": "s" * 64,
                    "candidate_plan_sha256": hashlib.sha256(
                        candidate_bytes
                    ).hexdigest(),
                    "latest_attempt_path": (
                        "manager/decision-001/repartition/attempt-001"
                    ),
                    "candidate_artifact_path": candidate_rel,
                },
            }
            with patch(
                "aflow.cli.resolve_run_id",
                return_value=(run_dir, "explicit_run_id"),
            ), patch("aflow.cli.load_run_json", return_value=prev_run):
                result = cli_module._detect_resume_candidate(
                    repo_root=repo_root,
                    workflow_config=type(
                        "obj", (object,), {"setup": ("worktree", "branch")}
                    )(),
                    workflow_name="test_workflow",
                    plan_path=plan_path,
                    team=None,
                    selected_start_step=None,
                    max_turns=15,
                    extra_instructions=(),
                    requested_run_id=run_dir.name,
                    require_resume=True,
                )

            assert result is not None
            assert result.repartition_artifact_bytes[candidate_rel] == candidate_bytes

    def test_resume_recomputes_active_scope_rejections_from_turn_artifacts(self) -> None:
        import aflow.cli as cli_module

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            plan_path = repo_root / "plan.md"
            run_dir = repo_root / ".aflow" / "runs" / "20260101T000000Z-abc123"
            turn_dir = run_dir / "turns" / "turn-002"
            turn_dir.mkdir(parents=True)
            snapshot_before = {
                "current_checkpoint_index": 2,
                "current_checkpoint_name": "Checkpoint 2: Next",
                "current_checkpoint_unchecked_step_count": 4,
                "is_complete": False,
                "total_checkpoint_count": 2,
                "unchecked_checkpoint_count": 1,
            }
            snapshot_after = {
                "current_checkpoint_index": 1,
                "current_checkpoint_name": "Checkpoint 1: Reopened",
                "current_checkpoint_unchecked_step_count": 3,
                "is_complete": False,
                "total_checkpoint_count": 2,
                "unchecked_checkpoint_count": 2,
            }
            (turn_dir / "result.json").write_text(json.dumps({
                "turn_number": 2,
                "status": "completed",
                "step_name": "review_cp_implementation",
                "step_role": "reviewer",
                "snapshot_before": snapshot_before,
                "snapshot_after": snapshot_after,
                "conditions": {"NEW_PLAN_EXISTS": True},
            }), encoding="utf-8")
            (turn_dir / "stdout.txt").write_text("rejected", encoding="utf-8")
            (turn_dir / "stderr.txt").write_text("", encoding="utf-8")
            prev_run = {
                "repo_root": str(repo_root),
                "workflow_name": "test_workflow",
                "plan_path": str(plan_path),
                "team": "ds4_pro",
                "selected_start_step": "implement_plan",
                "max_turns": 60,
                "extra_instructions": [],
                "lifecycle_setup": ["worktree", "branch"],
                "lifecycle_teardown": ["merge", "rm_worktree"],
                "feature_branch": "feature/test-branch",
                "worktree_path": str(repo_root / "worktree"),
                "main_branch": "main",
                "status": "failed",
                "last_snapshot": {"is_complete": False},
                "reviewer_rejection_count": 0,
                "active_implementation_scope": {
                    "scope_id": "scope-1",
                    "original_plan_path": str(plan_path),
                    "checkpoint_index": 1,
                    "checkpoint_name": "Checkpoint 1: Reopened",
                    "opened_turn_number": 1,
                    "awaiting_review": False,
                    "carried_reviewer_rejection_count": 0,
                },
            }

            with patch(
                "aflow.cli.resolve_run_id",
                return_value=(run_dir, "explicit_run_id"),
            ), patch("aflow.cli.load_run_json", return_value=prev_run):
                result = cli_module._detect_resume_candidate(
                    repo_root=repo_root,
                    workflow_config=type(
                        "obj",
                        (object,),
                        {"setup": ("worktree", "branch")},
                    )(),
                    workflow_name="test_workflow",
                    plan_path=plan_path,
                    team="ds4_pro",
                    selected_start_step="implement_plan",
                    max_turns=60,
                    extra_instructions=(),
                    requested_run_id=run_dir.name,
                    require_resume=True,
                )

        assert result is not None
        assert result.reviewer_rejection_count == 1

    def test_resume_recovers_completed_turn_before_manager_boundary(self) -> None:
        import aflow.cli as cli_module

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            plan_path = repo_root / "plan.md"
            repair_path = repo_root / "plan-cp02-v01.md"
            run_dir = (
                repo_root
                / ".aflow"
                / "runs"
                / "20260101T000000Z-abc123"
            )
            turn_dir = run_dir / "turns" / "turn-002"
            turn_dir.mkdir(parents=True)
            snapshot = {
                "current_checkpoint_index": 2,
                "current_checkpoint_name": "Checkpoint 2: Next",
                "current_checkpoint_unchecked_step_count": 4,
                "is_complete": False,
                "total_checkpoint_count": 2,
                "unchecked_checkpoint_count": 1,
            }
            (turn_dir / "result.json").write_text(
                json.dumps({
                    "turn_number": 2,
                    "status": "completed",
                    "step_name": "review_cp_implementation",
                    "step_role": "reviewer",
                    "selector": "codex.reviewer",
                    "returncode": 0,
                    "active_plan_path": str(plan_path),
                    "new_plan_path": str(repair_path),
                    "snapshot_before": snapshot,
                    "snapshot_after": snapshot,
                    "conditions": {
                        "DONE": False,
                        "NEW_PLAN_EXISTS": True,
                        "MAX_TURNS_REACHED": False,
                    },
                    "chosen_transition": "implement_plan",
                    "chosen_transition_condition": "NEW_PLAN_EXISTS || !DONE",
                }),
                encoding="utf-8",
            )
            (turn_dir / "stdout.txt").write_text("rejected", encoding="utf-8")
            (turn_dir / "stderr.txt").write_text("", encoding="utf-8")
            prev_run = {
                "repo_root": str(repo_root),
                "workflow_name": "test_workflow",
                "plan_path": str(plan_path),
                "team": "ds4_pro",
                "selected_start_step": "implement_plan",
                "max_turns": 60,
                "extra_instructions": [],
                "lifecycle_setup": ["worktree", "branch"],
                "lifecycle_teardown": ["merge", "rm_worktree"],
                "feature_branch": "feature/test-branch",
                "worktree_path": str(repo_root / "worktree"),
                "main_branch": "main",
                "status": "running",
                "active_turn": 2,
                "turns_completed": 1,
                "current_step_name": "review_cp_implementation",
                "active_plan_path": str(plan_path),
                "last_snapshot": snapshot,
                "reviewer_rejection_count": 0,
                "active_implementation_scope": {
                    "scope_id": "scope-1",
                    "original_plan_path": str(plan_path),
                    "checkpoint_index": 1,
                    "checkpoint_name": "Checkpoint 1: Reopened",
                    "opened_turn_number": 1,
                    "awaiting_review": True,
                    "carried_reviewer_rejection_count": 0,
                },
                "pending_boundary_decision": {
                    "finalized_turn_number": 1,
                    "decision_number": 7,
                    "action": "continue",
                    "proposed_action": "transition",
                    "proposed_transition": "review_cp_implementation",
                    "resolved_next_step": "review_cp_implementation",
                    "consumed": True,
                },
            }

            with patch(
                "aflow.cli.resolve_run_id",
                return_value=(run_dir, "explicit_run_id"),
            ), patch("aflow.cli.load_run_json", return_value=prev_run):
                result = cli_module._detect_resume_candidate(
                    repo_root=repo_root,
                    workflow_config=type(
                        "obj",
                        (object,),
                        {"setup": ("worktree", "branch")},
                    )(),
                    workflow_name="test_workflow",
                    plan_path=plan_path,
                    team="ds4_pro",
                    selected_start_step="implement_plan",
                    max_turns=60,
                    extra_instructions=(),
                    requested_run_id=run_dir.name,
                    require_resume=True,
                )

        assert result is not None
        assert result.active_plan_path == repair_path
        assert result.interrupted_step_name is None
        assert result.pending_boundary_decision is None
        assert result.reviewer_rejection_count == 1
        assert result.pending_finalized_turn is not None
        assert result.pending_finalized_turn.turn_number == 2
        assert result.pending_finalized_turn.step_name == "review_cp_implementation"
        assert result.pending_finalized_turn.chosen_transition == "implement_plan"
        assert result.pending_finalized_turn.new_plan_path == repair_path

    def test_resume_prompt_declined_returns_none(self) -> None:
        import aflow.cli as cli_module

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "test_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "status": "failed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "last_run_id_file")), \
             patch('aflow.cli.load_run_json', return_value=prev_run), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             patch('builtins.input', return_value='n'):
            result = cli_module._detect_resume_candidate(
                repo_root=Path("/fake/repo").resolve(),
                workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
            )

        assert result is None

    def test_resume_mismatch_suppresses_prompt(self) -> None:
        import aflow.cli as cli_module

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "different_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "status": "failed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "last_run_id_file")), \
             patch('aflow.cli.load_run_json', return_value=prev_run), \
             patch('builtins.input', side_effect=AssertionError('should not prompt')):
            result = cli_module._detect_resume_candidate(
                repo_root=Path("/fake/repo").resolve(),
                workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
            )

        assert result is None

    def test_resume_non_tty_skips_prompt(self) -> None:
        import aflow.cli as cli_module

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "test_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "status": "failed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "last_run_id_file")), \
             patch('aflow.cli.load_run_json', return_value=prev_run), \
             patch('sys.stdin.isatty', return_value=False), \
             patch('sys.stdout.isatty', return_value=False), \
             patch('builtins.input', side_effect=AssertionError('should not prompt')):
            result = cli_module._detect_resume_candidate(
                repo_root=Path("/fake/repo").resolve(),
                workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
            )

        assert result is None

    def test_resume_complete_prior_run_suppresses_prompt(self) -> None:
        import aflow.cli as cli_module

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "test_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "status": "completed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "last_run_id_file")), \
             patch('aflow.cli.load_run_json', return_value=prev_run), \
             patch('builtins.input', side_effect=AssertionError('should not prompt')):
            result = cli_module._detect_resume_candidate(
                repo_root=Path("/fake/repo").resolve(),
                workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
            )

        assert result is None

    def test_resume_lifecycle_setup_mismatch_suppresses_prompt(self) -> None:
        import aflow.cli as cli_module

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "test_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "status": "failed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "last_run_id_file")), \
             patch('aflow.cli.load_run_json', return_value=prev_run), \
             patch('builtins.input', side_effect=AssertionError('should not prompt')):
            result = cli_module._detect_resume_candidate(
                repo_root=Path("/fake/repo").resolve(),
                workflow_config=type('obj', (object,), {'setup': ('branch',)})(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
            )

        assert result is None

    def test_resume_flag_with_explicit_run_id_skips_prompt_and_returns_context(self) -> None:
        import aflow.cli as cli_module
        from aflow.run_state import ResumeContext

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "test_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "status": "failed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "explicit_run_id")), \
             patch('aflow.cli.load_run_json', return_value=prev_run), \
             patch('builtins.input', side_effect=AssertionError('should not prompt')):
            result = cli_module._detect_resume_candidate(
                repo_root=Path("/fake/repo").resolve(),
                workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                workflow_name="test_workflow",
                plan_path=Path("/fake/plan.md").resolve(),
                team=None,
                selected_start_step=None,
                max_turns=15,
                extra_instructions=(),
                requested_run_id="20260101T000000Z-abc123",
                require_resume=True,
            )

        assert isinstance(result, ResumeContext)
        assert result.resumed_from_run_id == "20260101T000000Z-abc123"

    def test_resume_accepts_failed_terminal_merge_without_rerun_scope(self) -> None:
        import aflow.cli as cli_module

        repo_root = Path("/fake/repo").resolve()
        plan_path = repo_root / "plan.md"
        run_id = "20260101T000000Z-abc123"
        prev_run = {
            "repo_root": str(repo_root),
            "workflow_name": "test_workflow",
            "plan_path": str(plan_path),
            "team": "sol_medium",
            "selected_start_step": "implement_plan",
            "max_turns": 60,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(repo_root / "worktree"),
            "main_branch": "main",
            "active_plan_path": str(repo_root / "missing-repair.md"),
            "current_step_name": "final_review",
            "status": "failed",
            "end_reason": "transition_end",
            "last_snapshot": {"is_complete": True},
            "merge_status": "failed",
            "merge_failure_reason": "feature branch is checked out elsewhere",
        }
        workflow = type(
            "obj",
            (object,),
            {
                "setup": ("worktree", "branch"),
                "teardown": ("merge", "rm_worktree"),
            },
        )()

        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_id), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run):
            result = cli_module._detect_resume_candidate(
                repo_root=repo_root,
                workflow_config=workflow,
                workflow_name="test_workflow",
                plan_path=plan_path,
                team="sol_medium",
                selected_start_step="implement_plan",
                max_turns=60,
                extra_instructions=(),
                requested_run_id=run_id,
                require_resume=True,
            )

        assert result is not None
        assert result.terminal_integration_only is True
        assert result.active_plan_path == plan_path
        assert result.interrupted_step_name == "final_review"

        invalid = dict(prev_run)
        invalid.pop("merge_failure_reason")
        assert (
            cli_module._resume_candidate_mismatch_reason(
                invalid,
                workflow,
                repo_root,
                "test_workflow",
                plan_path,
                "sol_medium",
                "implement_plan",
                60,
                (),
            )
            == "its last saved plan snapshot was already complete"
        )

    def test_resume_rejects_tampered_scope_envelope_before_workflow_start(self) -> None:
        import hashlib

        import aflow.cli as cli_module
        from aflow.repartition import create_envelope, write_envelope_atomic

        cases = (
            "missing_reference",
            "missing_artifact",
            "unsafe_path",
            "symlink_escape",
            "corrupt_bytes",
            "wrong_scope",
            "wrong_checkpoint",
            "wrong_stored_hash",
            "wrong_canonical_hash",
            "wrong_expected_location",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                repo_root = Path(tmpdir).resolve()
                plan_path = repo_root / "plan.md"
                run_id = f"run-{case}"
                run_dir = repo_root / ".aflow" / "runs" / run_id
                scope_id = f"{plan_path}::checkpoint-1::first"
                envelope = create_envelope(
                    scope_id=scope_id,
                    original_plan_path="plan.md",
                    plan_text=_VALID_PLAN,
                    checkpoint_index=1,
                )
                artifact = write_envelope_atomic(
                    envelope,
                    run_dir / "scopes" / envelope.scope_digest,
                )
                artifact_bytes = artifact.read_bytes()
                scope = {
                    "scope_id": scope_id,
                    "original_plan_path": str(plan_path),
                    "checkpoint_index": 1,
                    "checkpoint_name": "Checkpoint 1: First",
                    "opened_turn_number": 1,
                    "awaiting_review": False,
                    "carried_reviewer_rejection_count": 0,
                    "envelope_artifact_path": (
                        f"scopes/{envelope.scope_digest}/envelope.json"
                    ),
                    "envelope_artifact_sha256": hashlib.sha256(
                        artifact_bytes
                    ).hexdigest(),
                    "envelope_canonical_sha256": (
                        envelope.canonical_envelope_sha256
                    ),
                }
                if case == "missing_reference":
                    scope["envelope_artifact_path"] = None
                elif case == "missing_artifact":
                    artifact.unlink()
                elif case == "unsafe_path":
                    scope["envelope_artifact_path"] = "../../envelope.json"
                elif case == "symlink_escape":
                    outside = repo_root / "outside-envelope.json"
                    outside.write_bytes(artifact_bytes)
                    artifact.unlink()
                    artifact.symlink_to(outside)
                elif case == "corrupt_bytes":
                    artifact.write_bytes(b"{not json")
                    scope["envelope_artifact_sha256"] = hashlib.sha256(
                        b"{not json"
                    ).hexdigest()
                elif case == "wrong_scope":
                    scope["scope_id"] = "different-scope"
                    different_digest = hashlib.sha256(
                        b"different-scope"
                    ).hexdigest()
                    different_artifact = (
                        run_dir
                        / "scopes"
                        / different_digest
                        / "envelope.json"
                    )
                    different_artifact.parent.mkdir(parents=True)
                    different_artifact.write_bytes(artifact_bytes)
                    scope["envelope_artifact_path"] = (
                        f"scopes/{different_digest}/envelope.json"
                    )
                elif case == "wrong_checkpoint":
                    scope["checkpoint_index"] = 2
                elif case == "wrong_stored_hash":
                    scope["envelope_artifact_sha256"] = "a" * 64
                elif case == "wrong_canonical_hash":
                    scope["envelope_canonical_sha256"] = "b" * 64
                elif case == "wrong_expected_location":
                    wrong_digest = "f" * 64
                    wrong_artifact = (
                        run_dir / "scopes" / wrong_digest / "envelope.json"
                    )
                    wrong_artifact.parent.mkdir(parents=True)
                    wrong_artifact.write_bytes(artifact_bytes)
                    scope["envelope_artifact_path"] = (
                        f"scopes/{wrong_digest}/envelope.json"
                    )

                run_payload = {
                    "repo_root": str(repo_root),
                    "workflow_name": "test_workflow",
                    "plan_path": str(plan_path),
                    "team": None,
                    "selected_start_step": None,
                    "max_turns": 15,
                    "extra_instructions": [],
                    "lifecycle_setup": ["worktree", "branch"],
                    "lifecycle_teardown": ["merge", "rm_worktree"],
                    "feature_branch": "feature/test-branch",
                    "worktree_path": str(repo_root / "worktree"),
                    "main_branch": "main",
                    "status": "failed",
                    "last_snapshot": {"is_complete": False},
                    "active_implementation_scope": scope,
                }
                (run_dir / "run.json").write_text(
                    json.dumps(run_payload),
                    encoding="utf-8",
                )

                with patch(
                    "aflow.cli.resolve_run_id",
                    return_value=(run_dir, "explicit"),
                ), patch("aflow.cli.execute_workflow") as workflow_start:
                    with pytest.raises(
                        ValueError,
                        match="invalid scope envelope reference",
                    ):
                        cli_module._detect_resume_candidate(
                            repo_root=repo_root,
                            workflow_config=type(
                                "obj",
                                (object,),
                                {"setup": ("worktree", "branch")},
                            )(),
                            workflow_name="test_workflow",
                            plan_path=plan_path,
                            team=None,
                            selected_start_step=None,
                            max_turns=15,
                            extra_instructions=(),
                            requested_run_id=run_id,
                            require_resume=True,
                        )

                workflow_start.assert_not_called()
                assert run_dir.is_dir()
                assert (run_dir / "run.json").is_file()

    def test_resume_flag_without_id_errors_when_no_prior_run_can_be_resolved(self) -> None:
        import aflow.cli as cli_module

        with patch('aflow.cli.resolve_run_id', return_value=(None, None)):
            with pytest.raises(ValueError, match="Pass --resume RUN_ID"):
                cli_module._detect_resume_candidate(
                    repo_root=Path("/fake/repo").resolve(),
                    workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                    workflow_name="test_workflow",
                    plan_path=Path("/fake/plan.md").resolve(),
                    team=None,
                    selected_start_step=None,
                    max_turns=15,
                    extra_instructions=(),
                    require_resume=True,
                )

    def test_resume_flag_errors_when_run_is_not_resumable(self) -> None:
        import aflow.cli as cli_module

        prev_run = {
            "repo_root": str(Path("/fake/repo").resolve()),
            "workflow_name": "different_workflow",
            "plan_path": str(Path("/fake/plan.md").resolve()),
            "team": None,
            "selected_start_step": None,
            "max_turns": 15,
            "extra_instructions": [],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/test-branch",
            "worktree_path": str(Path("/fake/repo/.git/worktrees/test")),
            "main_branch": "main",
            "status": "failed",
            "last_snapshot": {"is_complete": False},
        }

        with patch('aflow.cli.resolve_run_id', return_value=(Path("20260101T000000Z-abc123"), "explicit_run_id")), \
             patch('aflow.cli.load_run_json', return_value=prev_run):
            with pytest.raises(ValueError, match="is not resumable"):
                cli_module._detect_resume_candidate(
                    repo_root=Path("/fake/repo").resolve(),
                    workflow_config=type('obj', (object,), {'setup': ('worktree', 'branch')})(),
                    workflow_name="test_workflow",
                    plan_path=Path("/fake/plan.md").resolve(),
                    team=None,
                    selected_start_step=None,
                    max_turns=15,
                    extra_instructions=(),
                    requested_run_id="20260101T000000Z-abc123",
                    require_resume=True,
                )


class RepoRootTests(unittest.TestCase):

    def test_resolve_repo_root_outside_git_uses_cwd(self) -> None:
        import unittest.mock as mock
        from aflow.cli import _resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_cwd = Path(tmpdir)
            with mock.patch('subprocess.run') as mock_run, \
                 mock.patch('pathlib.Path.cwd', return_value=fake_cwd):
                mock_run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr='fatal: not a git repo\n')
                result = _resolve_repo_root()
                assert result == fake_cwd.resolve()

    def test_resolve_repo_root_cwd_equals_git_root_uses_cwd(self) -> None:
        import unittest.mock as mock
        from aflow.cli import _resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_cwd = Path(tmpdir).resolve()
            with mock.patch('subprocess.run') as mock_run, \
                 mock.patch('pathlib.Path.cwd', return_value=fake_cwd):
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout=str(fake_cwd) + '\n', stderr='')
                result = _resolve_repo_root()
                assert result == fake_cwd

    def test_resolve_repo_root_nested_no_tty_returns_none(self) -> None:
        import unittest.mock as mock
        from aflow.cli import _resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            git_root = Path(tmpdir) / 'repo'
            git_root.mkdir()
            subdir = git_root / 'subdir'
            subdir.mkdir()
            fake_cwd = subdir.resolve()
            git_root_resolved = git_root.resolve()
            with mock.patch('subprocess.run') as mock_run, \
                 mock.patch('pathlib.Path.cwd', return_value=fake_cwd), \
                 mock.patch('sys.stdin') as mock_stdin, \
                 mock.patch('sys.stdout') as mock_stdout:
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout=str(git_root_resolved) + '\n', stderr='')
                mock_stdin.isatty.return_value = False
                mock_stdout.isatty.return_value = False
                result = _resolve_repo_root()
                assert result is None

    def test_resolve_repo_root_nested_tty_accepts_git_root(self) -> None:
        import unittest.mock as mock
        from aflow.cli import _resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            git_root = Path(tmpdir) / 'repo'
            git_root.mkdir()
            subdir = git_root / 'subdir'
            subdir.mkdir()
            fake_cwd = subdir.resolve()
            git_root_resolved = git_root.resolve()
            with mock.patch('subprocess.run') as mock_run, \
                 mock.patch('pathlib.Path.cwd', return_value=fake_cwd), \
                 mock.patch('sys.stdin') as mock_stdin, \
                 mock.patch('sys.stdout') as mock_stdout, \
                 mock.patch('builtins.input', return_value='y'):
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout=str(git_root_resolved) + '\n', stderr='')
                mock_stdin.isatty.return_value = True
                mock_stdout.isatty.return_value = True
                result = _resolve_repo_root()
                assert result == git_root_resolved

    def test_resolve_repo_root_nested_tty_declines_uses_cwd(self) -> None:
        import unittest.mock as mock
        from aflow.cli import _resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            git_root = Path(tmpdir) / 'repo'
            git_root.mkdir()
            subdir = git_root / 'subdir'
            subdir.mkdir()
            fake_cwd = subdir.resolve()
            git_root_resolved = git_root.resolve()
            with mock.patch('subprocess.run') as mock_run, \
                 mock.patch('pathlib.Path.cwd', return_value=fake_cwd), \
                 mock.patch('sys.stdin') as mock_stdin, \
                 mock.patch('sys.stdout') as mock_stdout, \
                 mock.patch('builtins.input', return_value='n'):
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout=str(git_root_resolved) + '\n', stderr='')
                mock_stdin.isatty.return_value = True
                mock_stdout.isatty.return_value = True
                result = _resolve_repo_root()
                assert result == fake_cwd

    def test_nested_subdir_no_tty_run_exits_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = _copy_aflow_repo(tmp_path)
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n[roles]\narchitect = "codex.default"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END", when = "DONE || MAX_TURNS_REACHED" }]\n\n[prompts]\np = "Work."\n')
            # init a real git repo at repo_root and create a subdirectory
            subprocess.run(['git', 'init'], cwd=str(repo_root), check=True, capture_output=True)
            subdir = repo_root / 'nested'
            subdir.mkdir()
            plan_path = tmp_path / 'plan.md'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            env = os.environ.copy()
            env['HOME'] = str(home_dir)
            result = subprocess.run(
                [sys.executable, '-m', 'aflow', 'run', str(plan_path)],
                cwd=str(subdir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 1
            assert 'git' in result.stderr.lower() or 'Rerun' in result.stderr or 'nested' in result.stderr


class DirtyWorktreeCliTests(unittest.TestCase):

    def _make_clean_repo(self, path: Path) -> None:
        _make_git_repo(path)

    def test_dirty_interactive_yes_proceeds(self) -> None:
        import aflow.cli as cli_module
        from aflow.git_status import WorktreeProbe
        dirty_probe = WorktreeProbe(is_dirty=True, modified_count=2, added_count=1, removed_count=0, sample_paths=("a.py",))
        original_probe = cli_module.probe_worktree
        original_resolve = cli_module._resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = home_dir / "plan.md"
            _write_plan(plan_path, "# Plan\n\n### [x] Checkpoint 1\n- [x] done\n")
            original_home = os.environ.get("HOME")
            try:
                os.environ["HOME"] = str(home_dir)
                cli_module.probe_worktree = lambda _: dirty_probe
                cli_module._resolve_repo_root = lambda: home_dir
                with patch("builtins.input", return_value="y"), \
                     patch("sys.stdin.isatty", return_value=True), \
                     patch("sys.stdout.isatty", return_value=True):
                    result = cli_module.main(["run", str(plan_path)])
            finally:
                cli_module.probe_worktree = original_probe
                cli_module._resolve_repo_root = original_resolve
                if original_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = original_home
        assert result == 0

    def test_dirty_interactive_no_aborts(self) -> None:
        import aflow.cli as cli_module
        from aflow.git_status import WorktreeProbe
        dirty_probe = WorktreeProbe(is_dirty=True, modified_count=1, added_count=0, removed_count=0, sample_paths=())
        original_resolve = cli_module._resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = home_dir / "plan.md"
            _write_plan(plan_path, "# Plan\n\n### [x] Checkpoint 1\n- [x] done\n")
            original_home = os.environ.get("HOME")
            try:
                os.environ["HOME"] = str(home_dir)
                cli_module._resolve_repo_root = lambda: home_dir
                with patch("aflow.api.startup.probe_worktree", return_value=dirty_probe), \
                     patch("builtins.input", return_value=""), \
                     patch("sys.stdin.isatty", return_value=True), \
                     patch("sys.stdout.isatty", return_value=True):
                    result = cli_module.main(["run", str(plan_path)])
            finally:
                cli_module._resolve_repo_root = original_resolve
                if original_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = original_home
        assert result == 1

    def test_dirty_non_interactive_aborts_with_message(self) -> None:
        import aflow.cli as cli_module
        from aflow.git_status import WorktreeProbe
        dirty_probe = WorktreeProbe(is_dirty=True, modified_count=1, added_count=0, removed_count=0, sample_paths=())
        original_resolve = cli_module._resolve_repo_root
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            _write_config(home_dir, '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            plan_path = home_dir / "plan.md"
            _write_plan(plan_path, "# Plan\n\n### [x] Checkpoint 1\n- [x] done\n")
            original_home = os.environ.get("HOME")
            import io
            stderr_capture = io.StringIO()
            try:
                os.environ["HOME"] = str(home_dir)
                cli_module._resolve_repo_root = lambda: home_dir
                with patch("aflow.api.startup.probe_worktree", return_value=dirty_probe), \
                     patch("sys.stdin.isatty", return_value=False), \
                     patch("sys.stdout.isatty", return_value=False), \
                     patch("sys.stderr", stderr_capture):
                    result = cli_module.main(["run", str(plan_path)])
            finally:
                cli_module._resolve_repo_root = original_resolve
                if original_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = original_home
        assert result == 1
        assert "dirty" in stderr_capture.getvalue().lower()

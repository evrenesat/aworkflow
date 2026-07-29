from aflow._test_support import *  # noqa: F401,F403
from aflow.run_state import load_override_request


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("protected_state = true\n", "unsupported override keys"),
        ("max_turns = 0\n", "positive integer"),
        ('notes = [""]\n', "non-empty string"),
        ('next_step = ""\n', "non-empty string"),
        ("next_step = [\n", "malformed TOML"),
    ],
)
def test_override_loader_rejects_unsafe_or_invalid_schema(
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    path = tmp_path / "overrides.toml"
    path.write_text(source, encoding="utf-8")
    result = load_override_request(path)
    assert result.status == "invalid"
    assert result.message is not None
    assert message in result.message


def test_manager_config_is_optional_and_defaults_to_disabled() -> None:
    config = WorkflowUserConfig()
    assert config.manager.enabled is False
    assert config.manager.full_after_stalled_turns == 2


def test_manager_config_rejects_missing_role_and_upgrade_cycle() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = _write_config(
            Path(tmpdir),
            '[manager]\nenabled = true\nlite_role = "manager_lite"\nfull_role = "manager_full"\n\n'
            '[harness.codex.profiles.nano]\nmodel = "nano"\n\n'
            '[roles]\nmanager_lite = "codex.nano"\nmanager_full = "codex.nano"\n\n'
            '[teams.a]\nupgrade_to = "b"\n\n'
            '[teams.b]\nupgrade_to = "a"\n',
        )
        with pytest.raises(ConfigError) as ctx:
            load_workflow_config(config_path)
        assert "upgrade_to forms a cycle" in str(ctx.value)

    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = _write_config(
            Path(tmpdir),
            '[manager]\nenabled = true\nlite_role = "manager_lite"\n\n',
        )
        with pytest.raises(ConfigError) as ctx:
            load_workflow_config(config_path)
        assert "full_role is required" in str(ctx.value)

class AflowSectionConfigTests(unittest.TestCase):

    def _write_workflow_config(self, tmpdir: str, text: str) -> Path:
        home_dir = Path(tmpdir)
        return _write_config(home_dir, text)

    def test_keep_runs_defaults_to_twenty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            assert config.aflow.keep_runs == 20

    def test_keep_runs_reads_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\ndefault_workflow = "simple"\nkeep_runs = 5\n\n[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            assert config.aflow.keep_runs == 5

    def test_keep_runs_rejects_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\nkeep_runs = 0\n')
            with pytest.raises(ConfigError):
                load_workflow_config(config_path)

    def test_keep_runs_rejects_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\nkeep_runs = -1\n')
            with pytest.raises(ConfigError):
                load_workflow_config(config_path)

    def test_keep_runs_rejects_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\nkeep_runs = true\n')
            with pytest.raises(ConfigError):
                load_workflow_config(config_path)

    def test_banner_files_limit_defaults_to_ten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\ndefault_workflow = "simple"\n\n[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            assert config.aflow.banner_files_limit == 10

    def test_banner_files_limit_reads_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\nbanner_files_limit = 7\n\n[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            assert config.aflow.banner_files_limit == 7

    def test_banner_files_limit_rejects_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\nbanner_files_limit = 0\n')
            with pytest.raises(ConfigError):
                load_workflow_config(config_path)

    def test_banner_files_limit_rejects_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\nbanner_files_limit = -1\n')
            with pytest.raises(ConfigError):
                load_workflow_config(config_path)

    def test_banner_files_limit_rejects_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\nbanner_files_limit = true\n')
            with pytest.raises(ConfigError):
                load_workflow_config(config_path)


class WorkflowConfigTests(unittest.TestCase):

    def _write_workflow_config(self, tmpdir: str, text: str) -> Path:
        home_dir = Path(tmpdir)
        return _write_config(home_dir, text)

    def test_parse_canonical_workflow_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            config_path = home_dir / '.config' / 'aflow' / 'aflow.toml'
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('[aflow]\ndefault_workflow = "simple"\n\n[harness.opencode.profiles.default]\nmodel = "glm-5-turbo"\n\n[harness.codex.profiles.high]\nmodel = "gpt-5.4"\neffort = "high"\n\n[roles]\nworker = "opencode.default"\nreviewer = "codex.high"\n\n[prompts]\nimplementation_prompt = "Work from {ACTIVE_PLAN_PATH}."\n', encoding='utf-8')
            (config_path.parent / 'workflows.toml').write_text('[workflow.simple.steps.implement_plan]\nrole = "worker"\nprompts = ["implementation_prompt"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "implement_plan" },\n]\n', encoding='utf-8')
            config = load_workflow_config(config_path)
            assert config.aflow.default_workflow == 'simple'
            assert config.roles['worker'] == 'opencode.default'
            assert 'opencode' in config.harnesses
            assert config.harnesses['opencode'].profiles['default'].model == 'glm-5-turbo'
            assert config.harnesses['codex'].profiles['high'].effort == 'high'
            assert 'simple' in config.workflows
            assert config.workflows['simple'].first_step == 'implement_plan'
            step = config.workflows['simple'].steps['implement_plan']
            assert step.role == 'worker'
            assert step.prompts == ('implementation_prompt',)
            assert len(step.go) == 2
            assert step.go[0].to == 'END'
            assert step.go[0].when == 'DONE || MAX_TURNS_REACHED'
            assert step.go[1].to == 'implement_plan'
            assert step.go[1].when is None
            assert config.prompts['implementation_prompt'] == 'Work from {ACTIVE_PLAN_PATH}.'

    def test_parse_multi_step_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            config_path = home_dir / '.config' / 'aflow' / 'aflow.toml'
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('[aflow]\ndefault_workflow = "review_loop"\n\n[harness.claude.profiles.opus]\nmodel = "claude-opus-4"\n\n[harness.opencode.profiles.turbo]\nmodel = "glm-5-turbo"\n\n[harness.codex.profiles.high]\nmodel = "gpt-5.4"\neffort = "high"\n\n[roles]\narchitect = "claude.opus"\nworker = "opencode.turbo"\nreviewer = "codex.high"\n\n[teams.reviewers]\nbackup_team = "codex1"\n\n[teams.reviewers.roles]\nreviewer = "claude.opus"\n\n[teams.codex1.roles]\nreviewer = "codex.high"\n\n[prompts]\nreview_prompt = "Review the plan."\nimplementation_prompt = "Implement from {ACTIVE_PLAN_PATH}."\nfix_plan_prompt = "Write new plan to {NEW_PLAN_PATH}."\n', encoding='utf-8')
            (config_path.parent / 'workflows.toml').write_text('[workflow.review_loop.steps.review_plan]\nrole = "architect"\nprompts = ["review_prompt"]\ngo = [{ to = "implement_plan" }]\n\n[workflow.review_loop.steps.implement_plan]\nrole = "worker"\nprompts = ["implementation_prompt"]\ngo = [{ to = "review_implementation" }]\n\n[workflow.review_loop.steps.review_implementation]\nrole = "reviewer"\nprompts = ["review_prompt", "fix_plan_prompt"]\ngo = [\n  { to = "END", when = "DONE || MAX_TURNS_REACHED" },\n  { to = "implement_plan" },\n]\n', encoding='utf-8')
            config = load_workflow_config(config_path)
            wf = config.workflows['review_loop']
            assert wf.first_step == 'review_plan'
            assert len(wf.steps) == 3
            assert wf.steps['review_plan'].role == 'architect'
            assert wf.steps['review_implementation'].prompts == ('review_prompt', 'fix_plan_prompt')

    def test_parse_legacy_inline_team_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\ndefault_workflow = "simple"\n\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "gpt-5.4"\n\n'
                '[roles]\narchitect = "opencode.default"\nsenior_architect = "opencode.default"\n\n'
                '[teams.legacy]\narchitect = "opencode.default"\nsenior_architect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            assert config.teams['legacy'].roles == {
                'architect': 'opencode.default',
                'senior_architect': 'opencode.default',
            }
            assert config.teams['legacy'].backup_team is None

    def test_parse_nested_team_roles_and_backup_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\ndefault_workflow = "simple"\n\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "gpt-5.4"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[teams.primary]\nbackup_team = "backup"\n\n'
                '[teams.primary.roles]\narchitect = "opencode.default"\n\n'
                '[teams.backup.roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            assert config.teams['primary'].backup_team == 'backup'
            assert config.teams['primary'].roles == {'architect': 'opencode.default'}

    def test_parse_rejects_mixed_inline_and_nested_team_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\ndefault_workflow = "simple"\n\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "gpt-5.4"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[teams.mixed]\narchitect = "opencode.default"\n\n'
                '[teams.mixed.roles]\nsenior_architect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'mixed legacy inline role keys' in str(ctx.value)
            assert 'teams.mixed' in str(ctx.value)

    def test_parse_rejects_legacy_default_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, 'default_harness = "codex"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'default_harness' in str(ctx.value)

    def test_parse_rejects_legacy_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\ndefault_model = "gpt-5.4"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'default_model' in str(ctx.value)

    def test_parse_rejects_bare_harness_step_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.implement_plan]\nrole = "opencode.default"\nprompts = ["p1"]\ngo = [{ to = "END" }]\n\n[prompts]\np1 = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'workflow.simple.steps.implement_plan.role' in str(ctx.value)
            assert 'unknown role' in str(ctx.value)

    def test_parse_rejects_harness_level_model_and_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[harness.opencode]\nmodel = "glm-5-turbo"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'model' in str(ctx.value)

    def test_parse_rejects_invalid_condition_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "END", when = "NOT_DONE" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np1 = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'NOT_DONE' in str(ctx.value)

    def test_parse_rejects_invalid_condition_max_iterations_not_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "END", when = "MAX_ITERATIONS_NOT_REACHED" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np1 = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'MAX_ITERATIONS_NOT_REACHED' in str(ctx.value)

    def test_parse_rejects_invalid_transition_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "nonexistent_step" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np1 = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'nonexistent_step' in str(ctx.value)

    def test_parse_rejects_empty_prompts_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = []\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'prompts' in str(ctx.value)
            assert 'empty' in str(ctx.value)

    def test_parse_rejects_missing_go(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'go' in str(ctx.value)

    def test_parse_rejects_empty_go_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = []\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'go' in str(ctx.value)
            assert 'empty' in str(ctx.value)

    def test_parse_accepts_unconditional_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.implement_plan]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "implement_plan" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np1 = "do it"\n')
            config = load_workflow_config(config_path)
            step = config.workflows['simple'].steps['implement_plan']
            assert len(step.go) == 1
            assert step.go[0].to == 'implement_plan'
            assert step.go[0].when is None
            assert step.go[0].preserve_active_plan is False

    def test_parse_accepts_preserve_active_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.s1]\n'
                'role = "architect"\n'
                'prompts = ["p"]\n'
                'go = [{ to = "s1", preserve_active_plan = true }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            transition = load_workflow_config(
                config_path
            ).workflows['simple'].steps['s1'].go[0]
            assert transition.preserve_active_plan is True

    def test_parse_rejects_non_boolean_preserve_active_plan(self) -> None:
        for invalid_value in ('"true"', '1', '[true]'):
            with self.subTest(invalid_value=invalid_value):
                with tempfile.TemporaryDirectory() as tmpdir:
                    config_path = self._write_workflow_config(
                        tmpdir,
                        '[workflow.simple.steps.s1]\n'
                        'role = "architect"\n'
                        'prompts = ["p"]\n'
                        f'go = [{{ to = "s1", preserve_active_plan = {invalid_value} }}]\n\n'
                        '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                        '[roles]\narchitect = "opencode.default"\n\n'
                        '[prompts]\np = "do it"\n',
                    )
                    with pytest.raises(ConfigError) as ctx:
                        load_workflow_config(config_path)
                    assert (
                        'workflow.simple.steps.s1.go[0].preserve_active_plan'
                        in str(ctx.value)
                    )

    def test_parse_rejects_preserve_active_plan_on_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.s1]\n'
                'role = "architect"\n'
                'prompts = ["p"]\n'
                'go = [{ to = "END", preserve_active_plan = true }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert (
                'workflow.simple.steps.s1.go[0].preserve_active_plan'
                in str(ctx.value)
            )
            assert "'END'" in str(ctx.value)

    def test_parse_accepts_complex_condition_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p1"]\ngo = [\n  { to = "END", when = "(DONE || MAX_TURNS_REACHED) && NEW_PLAN_EXISTS" },\n  { to = "s1" },\n]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np1 = "do it"\n')
            config = load_workflow_config(config_path)
            step = config.workflows['simple'].steps['s1']
            assert step.go[0].when == '(DONE || MAX_TURNS_REACHED) && NEW_PLAN_EXISTS'

    def test_prompts_preserve_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["alpha", "beta", "gamma"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\ngamma = "third"\nalpha = "first"\nbeta = "second"\n')
            config = load_workflow_config(config_path)
            step = config.workflows['simple'].steps['s1']
            assert step.prompts == ('alpha', 'beta', 'gamma')

    def test_go_transitions_preserve_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "DONE" },\n  { to = "END", when = "MAX_TURNS_REACHED" },\n  { to = "s2" },\n]\n\n[workflow.simple.steps.s2]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            step = config.workflows['simple'].steps['s1']
            assert len(step.go) == 3
            assert step.go[0].to == 'END'
            assert step.go[0].when == 'DONE'
            assert step.go[1].to == 'END'
            assert step.go[1].when == 'MAX_TURNS_REACHED'
            assert step.go[2].to == 's2'
            assert step.go[2].when is None

    def test_placeholder_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\ndefault_workflow = "simple"\n\n[harness.opencode.profiles.default]\nmodel = "FILL_IN_MODEL"\n\n[harness.codex.profiles.high]\nmodel = "gpt-5.4"\neffort = "high"\n\n[roles]\narchitect = "opencode.default"\n\n[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            placeholders = find_placeholders(config)
            assert placeholders == ['harness.opencode.profiles.default.model']

    def test_placeholder_settings_report_exact_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[aflow]\ndefault_workflow = "simple"\n\n[harness.opencode.profiles.default]\nmodel = "FILL_IN_MODEL"\n\n[harness.codex.profiles.high]\nmodel = "FILL_IN_MODEL"\neffort = "high"\n\n[harness.claude.profiles.opus]\nmodel = "FILL_IN_MODEL"\neffort = "medium"\n\n[roles]\narchitect = "opencode.default"\n\n[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            placeholders = find_placeholders(config)
            assert len(placeholders) == 3
            assert 'harness.claude.profiles.opus.model' in placeholders
            assert 'harness.codex.profiles.high.model' in placeholders
            assert 'harness.opencode.profiles.default.model' in placeholders


    def test_bundled_config_validates_without_errors(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = load_workflow_config(repo_root / 'aflow' / 'aflow.toml')
        assert config.aflow.default_workflow == 'medium'
        assert config.aflow.max_turns == 15
        assert config.aflow.team_lead == 'senior_architect'
        assert config.roles['architect'] == 'claude.opus'
        assert config.teams['7teen'].roles['worker'] == 'codex.nano'
        assert config.teams['codex1'].backup_team == '7teen'
        assert config.workflows['ralph'].steps['implement_plan'].role == 'worker'
        assert config.workflows['ralph'].setup == ('worktree', 'branch')
        assert config.workflows['ralph'].teardown == ('merge', 'rm_worktree')
        assert config.workflows['ralph_jr'].team == '7teen'
        assert config.workflows['ralph_jr'].setup == ('branch',)
        assert config.workflows['ralph_jr'].teardown == ('merge',)
        assert config.workflows['ralph_jr'].merge_prompt == ('simple_merge',)
        assert 'hard' in config.workflows
        assert 'jr' in config.workflows
        assert validate_workflow_config(config) == []

    def test_bundled_workflows_preserve_repair_plans_on_review_edges(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = load_workflow_config(repo_root / 'aflow' / 'aflow.toml')
        review_impl = config.workflows[
            'review_implement_review'
        ].steps['implement_plan'].go
        assert review_impl[1].preserve_active_plan is True
        assert review_impl[2].preserve_active_plan is True
        checkpoint_impl = config.workflows[
            'review_implement_cp_review'
        ].steps['implement_plan'].go
        assert checkpoint_impl[1].preserve_active_plan is True
        followup = config.workflows[
            'review_implement_cp_review'
        ].steps['followup'].go
        assert followup[1].preserve_active_plan is True

    def test_bundled_docs_and_configs_reflect_split_schema(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        readme = (repo_root / 'README.md').read_text(encoding='utf-8')
        architecture = (repo_root / 'ARCHITECTURE.md').read_text(encoding='utf-8')
        devlog = (repo_root / 'devlog' / 'DEVLOG.md').read_text(encoding='utf-8')
        aflow_text = (repo_root / 'aflow' / 'aflow.toml').read_text(encoding='utf-8')
        workflows_text = (repo_root / 'aflow' / 'workflows.toml').read_text(encoding='utf-8')

        assert 'aflow/workflows.toml' in readme
        assert '--team' in readme
        assert 'max_turns' in readme
        assert 'extends = "ralph"' in readme
        assert 'Config is split across two TOML files' in readme
        assert 'resolve_role_selector' in architecture
        assert 'workflows.toml' in architecture
        assert '# Default workflow used when the CLI does not name one.' in aflow_text
        assert '# Harness profiles map a harness name to the model and effort values it should run.' in aflow_text
        assert '# Team-specific role overrides live under [teams.<name>.roles]. Missing roles fall back' in aflow_text
        assert "backup_team = '7teen'" in aflow_text
        assert '# [error_handling.harness_error_recovery]' in aflow_text
        assert 'retry_same_team_after_delay' in aflow_text
        assert '# Named prompt templates that workflow steps reference by key.' in aflow_text
        assert '# Alias workflow, inherits `ralph` steps but runs branch-only lifecycle with a per-workflow merge prompt.' in workflows_text
        assert '# The step runs under a role. The selected team decides which selector that role maps to at runtime.' in workflows_text
        assert '# Transition rules are checked top to bottom. Each rule uses a `to` target and an optional `when` condition.' in workflows_text

        # lifecycle config docs parity
        assert 'team_lead' in readme
        assert 'worktree_root' in readme
        assert 'branch_prefix' in readme
        assert 'setup' in readme
        assert 'teardown' in readme
        assert 'merge_prompt' in readme
        assert 'aflow show' in readme
        assert 'exclude = ["step_name"]' in readme
        assert 'aflow-merge' in readme
        assert 'aflow-assistant' in readme
        assert '--include-optional' in readme
        assert '--only' in readme
        assert 'aflow analyze' in readme
        assert 'AnalyzeRequest' in readme
        assert 'error_handling.harness_error_recovery' in readme
        assert 'backup_team' in readme
        assert 'aflow-harness-recovery-lead' in readme
        assert 'AFLOW_LAST_RUN_ID' in readme
        assert 'last_run_ids' in readme
        assert 'resume' in readme
        assert 'reuses the recorded feature branch and worktree path' in readme
        assert 'Issues' in readme

        assert 'team_lead' in architecture
        assert 'worktree_root' in architecture
        assert 'ExecutionContext' in architecture
        assert 'aflow show' in architecture
        assert 'aflow-merge' in architecture
        assert 'aflow-assistant' in architecture
        assert 'aflow analyze' in architecture
        assert 'aflow.api.analyze' in architecture
        assert 'AnalyzeRequest' in architecture
        assert 'exclude = ["step_name"]' in architecture
        assert 'backup_team' in architecture
        assert 'aflow-harness-recovery-lead' in architecture
        assert 'AFLOW_LAST_RUN_ID' in architecture
        assert '.aflow/last_run_id' in architecture
        assert 'last_run_ids' in architecture
        assert 'resume' in architecture
        assert 'reused execution context' in architecture
        assert 'optional bundled' in architecture

        assert 'resume' in devlog
        assert 'Harness recovery docs and public analyze API' in devlog
        assert 'aflow show' in devlog
        assert 'exclude = ["step_name"]' in devlog
        assert 'AnalyzeRequest' in devlog
        assert 'AFLOW_LAST_RUN_ID' in devlog
        assert 'last_run_ids' in devlog

        # lifecycle defaults table is documented in workflows.toml
        assert '[workflow]' in workflows_text
        assert 'setup' in workflows_text
        assert 'teardown' in workflows_text
        assert 'main_branch' in workflows_text
        assert 'exclude = ["step_name"]' in workflows_text

        # merge-only placeholders are documented in README
        assert '{MAIN_BRANCH}' in readme
        assert '{FEATURE_BRANCH}' in readme
        assert '{PRIMARY_REPO_ROOT}' in readme
        assert '{EXECUTION_REPO_ROOT}' in readme

    def test_bootstrap_creates_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / 'aflow' / 'aflow.toml'
            result = bootstrap_config(config_path)
            assert result.exists()
            assert result == config_path
            packaged_aflow = resources.files('aflow').joinpath('aflow.toml').read_text(encoding='utf-8')
            packaged_workflows = resources.files('aflow').joinpath('workflows.toml').read_text(encoding='utf-8')
            assert result.read_text(encoding='utf-8') == packaged_aflow
            assert (config_path.parent / 'workflows.toml').read_text(encoding='utf-8') == packaged_workflows

    def test_bootstrap_does_not_overwrite_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / 'aflow' / 'aflow.toml'
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('existing', encoding='utf-8')
            result = bootstrap_config(config_path)
            assert result.read_text(encoding='utf-8') == 'existing'
            assert (config_path.parent / 'workflows.toml').exists()

    def test_parse_rejects_unsupported_workflow_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple]\nstart = "review"\n\n[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p"]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "x"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'workflow.simple' in str(ctx.value)
            assert 'start' in str(ctx.value)

    def test_parse_rejects_invalid_condition_operator_eq(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END", when = "DONE == NEW_PLAN_EXISTS" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "x"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert '==' in str(ctx.value)

    def test_parse_rejects_invalid_condition_operator_plus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END", when = "DONE + NEW_PLAN_EXISTS" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "x"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert '+' in str(ctx.value)

    def test_validate_workflow_config_default_workflow_missing_reports_exact_path(self) -> None:
        config = WorkflowUserConfig(aflow=AflowSection(default_workflow='nonexistent'), workflows={'simple': WorkflowConfig()})
        errors = validate_workflow_config(config)
        assert any(('aflow.default_workflow' in e for e in errors))
        assert any(('nonexistent' in e for e in errors))

    def test_validate_workflow_config_unknown_harness_reports_exact_path(self) -> None:
        wf = WorkflowConfig(steps={'s1': WorkflowStepConfig(role='unknown_harness.p1', prompts=('p1',))})
        config = WorkflowUserConfig(workflows={'w': wf}, prompts={'p1': 'text'})
        errors = validate_workflow_config(config)
        assert any(('workflow.w.steps.s1.role' in e for e in errors))
        assert any(('unknown role' in e for e in errors))

    def test_validate_workflow_config_unknown_profile_reports_exact_path(self) -> None:
        wf = WorkflowConfig(steps={'s1': WorkflowStepConfig(role='opencode.missing', prompts=('p1',))})
        config = WorkflowUserConfig(harnesses={'opencode': WorkflowHarnessConfig(profiles={})}, workflows={'w': wf}, prompts={'p1': 'text'})
        errors = validate_workflow_config(config)
        assert any(('workflow.w.steps.s1.role' in e for e in errors))
        assert any(('unknown role' in e for e in errors))

    def test_validate_workflow_config_unknown_prompt_reports_exact_path(self) -> None:
        wf = WorkflowConfig(steps={'s1': WorkflowStepConfig(role='architect', prompts=('missing_prompt',))})
        config = WorkflowUserConfig(harnesses={'opencode': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m')})}, workflows={'w': wf})
        errors = validate_workflow_config(config)
        assert any(('workflow.w.steps.s1.prompts[0]' in e for e in errors))

    def test_parse_accepts_complex_condition_with_negation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [\n  { to = "END", when = "!(DONE || MAX_TURNS_REACHED) && NEW_PLAN_EXISTS" },\n  { to = "s1" },\n]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            config = load_workflow_config(config_path)
            step = config.workflows['simple'].steps['s1']
            assert step.go[0].when == '!(DONE || MAX_TURNS_REACHED) && NEW_PLAN_EXISTS'

    def test_validate_workflow_config_default_workflow_missing(self) -> None:
        config = WorkflowUserConfig(aflow=AflowSection(default_workflow='nonexistent'), workflows={'simple': WorkflowConfig()})
        errors = validate_workflow_config(config)
        assert any(('nonexistent' in e for e in errors))

    def test_validate_workflow_config_passes_for_valid_config(self) -> None:
        wf = WorkflowConfig(steps={'s1': WorkflowStepConfig(role='architect', prompts=('p1',))})
        config = WorkflowUserConfig(
            aflow=AflowSection(default_workflow='w'),
            roles={'architect': 'opencode.default'},
            harnesses={'opencode': WorkflowHarnessConfig(profiles={'default': HarnessProfileConfig(model='m')})},
            workflows={'w': wf},
            prompts={'p1': 'text'},
        )
        errors = validate_workflow_config(config)
        assert errors == []

    def test_load_returns_empty_config_for_missing_file(self) -> None:
        config = load_workflow_config(Path('/nonexistent/aflow.toml'))
        assert config.aflow.default_workflow is None
        assert config.harnesses == {}
        assert config.workflows == {}
        assert config.prompts == {}

    def test_parse_rejects_unsupported_top_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[server]\nport = 8080\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'server' in str(ctx.value)

    def test_parse_rejects_unsupported_condition_in_when(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END", when = "DONE && STALEMATE" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'STALEMATE' in str(ctx.value)

    def test_first_step_is_first_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.review]\nrole = "reviewer"\nprompts = ["p1"]\ngo = [{ to = "implement" }]\n\n[workflow.simple.steps.implement]\nrole = "architect"\nprompts = ["p2"]\ngo = [{ to = "END" }]\n\n[harness.claude.profiles.opus]\nmodel = "m"\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\nreviewer = "claude.opus"\narchitect = "opencode.default"\n\n[prompts]\np1 = "review"\np2 = "implement"\n')
            config = load_workflow_config(config_path)
            wf = config.workflows['simple']
            assert wf.first_step == 'review'

    def test_missing_steps_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple]\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'steps' in str(ctx.value)

    def test_step_missing_profile_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nprompts = ["p"]\n\n[prompts]\np = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'role' in str(ctx.value)

    def test_step_missing_prompts_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\n\n[harness.opencode.profiles.default]\nmodel = "m"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'prompts' in str(ctx.value)

    def test_go_missing_to_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ when = "DONE" }]\n\n[harness.opencode.profiles.default]\nmodel = "m"\n\n[roles]\narchitect = "opencode.default"\n\n[prompts]\np = "do it"\n')
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'to' in str(ctx.value)

    def test_lifecycle_defaults_inherited_by_concrete_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow]\nsetup = ["branch"]\nteardown = ["merge"]\nmain_branch = "main"\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[aflow]\nteam_lead = "architect"\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            wf = config.workflows['simple']
            assert wf.setup == ('branch',)
            assert wf.teardown == ('merge',)
            assert wf.main_branch == 'main'
            assert wf.merge_prompt == ()

    def test_lifecycle_alias_can_override_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow]\nsetup = ["worktree", "branch"]\nteardown = ["merge", "rm_worktree"]\nmain_branch = "main"\n\n'
                '[workflow.base.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.alias]\nextends = "base"\nsetup = ["branch"]\nteardown = ["merge"]\n\n'
                '[aflow]\nteam_lead = "architect"\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            base = config.workflows['base']
            alias = config.workflows['alias']
            assert base.setup == ('worktree', 'branch')
            assert base.teardown == ('merge', 'rm_worktree')
            assert alias.setup == ('branch',)
            assert alias.teardown == ('merge',)
            assert alias.main_branch == 'main'

    def test_workflow_exclude_keeps_declared_graph_and_retargets_first_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "implement" }]\n\n'
                '[workflow.simple.steps.implement]\nrole = "architect"\nprompts = ["p2"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.simple]\nexclude = ["review"]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np1 = "review"\np2 = "implement"\n',
            )
            config = load_workflow_config(config_path)
            wf = config.workflows['simple']
            assert list(wf.declared_steps) == ['review', 'implement']
            assert list(wf.steps) == ['implement']
            assert wf.first_step == 'implement'
            assert wf.excluded_steps == ('review',)

    def test_alias_workflow_exclude_applies_after_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.base.steps.review]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "implement" }]\n\n'
                '[workflow.base.steps.implement]\nrole = "architect"\nprompts = ["p2"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.alias]\nextends = "base"\nexclude = ["review"]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np1 = "review"\np2 = "implement"\n',
            )
            config = load_workflow_config(config_path)
            base = config.workflows['base']
            alias = config.workflows['alias']
            assert list(base.declared_steps) == ['review', 'implement']
            assert list(base.steps) == ['review', 'implement']
            assert list(alias.declared_steps) == ['review', 'implement']
            assert list(alias.steps) == ['implement']
            assert alias.first_step == 'implement'
            assert alias.excluded_steps == ('review',)

    def test_exclude_rejects_unknown_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.simple]\nexclude = ["missing"]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "review"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'workflow.simple.exclude[0]' in str(ctx.value)
            assert 'missing' in str(ctx.value)

    def test_exclude_rejects_empty_step_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.simple]\nexclude = [""]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "review"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'workflow.simple.exclude[0]' in str(ctx.value)
            assert 'empty' in str(ctx.value)

    def test_exclude_rejects_duplicate_step_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.simple]\nexclude = ["review", "review"]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "review"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert "duplicate step 'review'" in str(ctx.value)
            assert 'workflow.simple.exclude' in str(ctx.value)

    def test_exclude_rejects_non_array_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.simple]\nexclude = "review"\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "review"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'workflow.simple.exclude' in str(ctx.value)
            assert 'array' in str(ctx.value)

    def test_exclude_rejects_dangling_transition_after_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "implement" }]\n\n'
                '[workflow.simple.steps.implement]\nrole = "architect"\nprompts = ["p2"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.simple]\nexclude = ["implement"]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np1 = "review"\np2 = "implement"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'workflow.simple.steps.review.go[0]' in str(ctx.value)
            assert 'implement' in str(ctx.value)

    def test_exclude_rejects_removing_all_executable_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple.steps.review]\nrole = "architect"\nprompts = ["p1"]\ngo = [{ to = "implement" }]\n\n'
                '[workflow.simple.steps.implement]\nrole = "architect"\nprompts = ["p2"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.simple]\nexclude = ["review", "implement"]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np1 = "review"\np2 = "implement"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'workflow.simple.first_step' in str(ctx.value)
            assert 'exclusions' in str(ctx.value)

    def test_lifecycle_alias_cannot_redefine_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.base.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[workflow.alias]\nextends = "base"\nsetup = ["branch"]\nteardown = ["merge"]\n\n'
                '[workflow.alias.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'steps' in str(ctx.value)

    def test_lifecycle_invalid_combo_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple]\nsetup = ["branch"]\nteardown = ["rm_worktree"]\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'unsupported lifecycle combination' in str(ctx.value)

    def test_lifecycle_wrong_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple]\nsetup = ["branch", "worktree"]\nteardown = ["merge", "rm_worktree"]\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'unsupported lifecycle combination' in str(ctx.value)

    def test_lifecycle_merge_requires_team_lead(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple]\nsetup = ["branch"]\nteardown = ["merge"]\nmain_branch = "main"\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'team_lead' in str(ctx.value)

    def test_lifecycle_team_lead_unresolvable_for_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\nteam_lead = "senior_architect"\n\n'
                '[workflow.simple]\nsetup = ["branch"]\nteardown = ["merge"]\nmain_branch = "main"\nteam = "7teen"\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[teams.7teen]\n\n[teams.7teen.roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'team_lead' in str(ctx.value)
            assert 'senior_architect' in str(ctx.value)

    def test_lifecycle_team_lead_resolves_via_global_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\nteam_lead = "senior_architect"\n\n'
                '[workflow.simple]\nsetup = ["branch"]\nteardown = ["merge"]\nmain_branch = "main"\nteam = "7teen"\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\nsenior_architect = "opencode.default"\n\n'
                '[teams.7teen]\n\n[teams.7teen.roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            assert validate_workflow_config(config) == []
            assert config.workflows['simple'].setup == ('branch',)

    def test_lifecycle_merge_prompt_unknown_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\nteam_lead = "architect"\n\n'
                '[workflow.simple]\nsetup = ["branch"]\nteardown = ["merge"]\nmain_branch = "main"\nmerge_prompt = ["nonexistent_prompt"]\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'nonexistent_prompt' in str(ctx.value)

    def test_lifecycle_no_lifecycle_combo_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple]\nsetup = []\nteardown = []\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            wf = config.workflows['simple']
            assert wf.setup == ()
            assert wf.teardown == ()

    def test_lifecycle_worktree_branch_combo_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\nteam_lead = "architect"\n\n'
                '[workflow.simple]\nsetup = ["worktree", "branch"]\nteardown = ["merge", "rm_worktree"]\nmain_branch = "main"\n\n'
                '[workflow.simple.steps.s1]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            wf = config.workflows['simple']
            assert wf.setup == ('worktree', 'branch')
            assert wf.teardown == ('merge', 'rm_worktree')
            assert validate_workflow_config(config) == []


class HarnessErrorRecoveryConfigTests(unittest.TestCase):

    def _write_workflow_config(self, tmpdir: str, text: str) -> Path:
        home_dir = Path(tmpdir)
        return _write_config(home_dir, text)

    def _minimal_config(self, extra: str = '') -> str:
        return (
            '[aflow]\ndefault_workflow = "simple"\n\n'
            '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
            '[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n'
            '[roles]\narchitect = "codex.default"\n\n'
            '[teams.primary]\nbackup_team = "backup"\n\n'
            '[teams.primary.roles]\narchitect = "codex.default"\n\n'
            '[teams.backup.roles]\narchitect = "codex.default"\n\n'
            f'{extra}'
            '[prompts]\np = "do it"\n'
        )

    def test_parse_harness_error_recovery_rules_and_backup_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                self._minimal_config(
                    '[error_handling.harness_error_recovery]\n'
                    'max_consecutive_recoveries = 4\n'
                    'team_lead_skill = "aflow-harness-recovery-lead"\n\n'
                    '[[error_handling.harness_error_recovery.rules]]\n'
                    'action = "retry_same_team_after_delay"\n'
                    'match = ["throttled", "rate limit"]\n'
                    'delay_seconds = 30\n\n'
                    '[[error_handling.harness_error_recovery.rules]]\n'
                    'action = "switch_to_backup_team_and_retry"\n'
                    'match = ["quota exceeded", "account exhausted"]\n\n'
                    '[[error_handling.harness_error_recovery.rules]]\n'
                    'action = "fail_immediately"\n'
                    'match = ["invalid prompt payload"]\n'
                ),
            )
            config = load_workflow_config(config_path)
            recovery = config.error_handling.harness_error_recovery
            assert config.teams['primary'].backup_team == 'backup'
            assert recovery.max_consecutive_recoveries == 4
            assert recovery.team_lead_skill == 'aflow-harness-recovery-lead'
            assert len(recovery.rules) == 3
            assert recovery.rules[0].action == 'retry_same_team_after_delay'
            assert recovery.rules[0].match == ('throttled', 'rate limit')
            assert recovery.rules[0].delay_seconds == 30
            assert recovery.rules[1].action == 'switch_to_backup_team_and_retry'
            assert recovery.rules[1].delay_seconds == 0
            assert recovery.rules[2].action == 'fail_immediately'

    def test_rejects_invalid_recovery_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                self._minimal_config(
                    '[error_handling.harness_error_recovery]\n'
                    '[[error_handling.harness_error_recovery.rules]]\n'
                    'action = "not_an_action"\n'
                    'match = ["throttled"]\n'
                ),
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'retry_same_team_after_delay' in str(ctx.value)
            assert 'switch_to_backup_team_and_retry' in str(ctx.value)
            assert 'fail_immediately' in str(ctx.value)

    def test_rejects_unknown_backup_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\ndefault_workflow = "simple"\n\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n'
                '[roles]\narchitect = "codex.default"\n\n'
                '[teams.primary]\nbackup_team = "missing"\n\n'
                '[teams.primary.roles]\narchitect = "codex.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'backup_team' in str(ctx.value)
            assert 'missing' in str(ctx.value)

    def test_rejects_backup_team_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\ndefault_workflow = "simple"\n\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
                '[harness.codex.profiles.default]\nmodel = "gpt-5.4"\n\n'
                '[roles]\narchitect = "codex.default"\n\n'
                '[teams.a]\nbackup_team = "b"\n\n'
                '[teams.a.roles]\narchitect = "codex.default"\n\n'
                '[teams.b]\nbackup_team = "a"\n\n'
                '[teams.b.roles]\narchitect = "codex.default"\n\n'
                '[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'cycle' in str(ctx.value)
            assert 'teams.a.backup_team' in str(ctx.value) or 'teams.b.backup_team' in str(ctx.value)

    def test_rejects_delay_seconds_for_fail_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                self._minimal_config(
                    '[error_handling.harness_error_recovery]\n'
                    '[[error_handling.harness_error_recovery.rules]]\n'
                    'action = "fail_immediately"\n'
                    'match = ["throttled"]\n'
                    'delay_seconds = 1\n'
                ),
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'delay_seconds' in str(ctx.value)


class RetryInconsistentCheckpointConfigTests(unittest.TestCase):

    def _write_workflow_config(self, tmpdir: str, text: str) -> Path:
        home_dir = Path(tmpdir)
        return _write_config(home_dir, text)

    def _minimal_config(self, extra_aflow: str = '', extra_workflow: str = '') -> str:
        return (
            f'[aflow]\n{extra_aflow}\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n'
            f'{extra_workflow}'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n[prompts]\np = "do it"\n'
        )

    def test_global_retry_defaults_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, self._minimal_config())
            config = load_workflow_config(config_path)
            assert config.aflow.retry_inconsistent_checkpoint_state == 0

    def test_global_retry_reads_positive_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='retry_inconsistent_checkpoint_state = 3')
            )
            config = load_workflow_config(config_path)
            assert config.aflow.retry_inconsistent_checkpoint_state == 3

    def test_global_retry_accepts_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='retry_inconsistent_checkpoint_state = 0')
            )
            config = load_workflow_config(config_path)
            assert config.aflow.retry_inconsistent_checkpoint_state == 0

    def test_global_retry_rejects_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='retry_inconsistent_checkpoint_state = -1')
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'retry_inconsistent_checkpoint_state' in str(ctx.value)

    def test_global_retry_rejects_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='retry_inconsistent_checkpoint_state = true')
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'retry_inconsistent_checkpoint_state' in str(ctx.value)

    def test_workflow_retry_override_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[aflow]\nretry_inconsistent_checkpoint_state = 1\n'
                '[roles]\narchitect = "opencode.default"\n\n'
                '[workflow.simple]\nretry_inconsistent_checkpoint_state = 2\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n[prompts]\np = "do it"\n',
            )
            config = load_workflow_config(config_path)
            assert config.aflow.retry_inconsistent_checkpoint_state == 1
            assert config.workflows['simple'].retry_inconsistent_checkpoint_state == 2

    def test_workflow_retry_override_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, self._minimal_config())
            config = load_workflow_config(config_path)
            assert config.workflows['simple'].retry_inconsistent_checkpoint_state is None

    def test_workflow_retry_override_rejects_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple]\nretry_inconsistent_checkpoint_state = -1\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'retry_inconsistent_checkpoint_state' in str(ctx.value)

    def test_workflow_retry_override_rejects_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir,
                '[workflow.simple]\nretry_inconsistent_checkpoint_state = true\n'
                '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n'
                '[harness.opencode.profiles.default]\nmodel = "m"\n\n[prompts]\np = "do it"\n',
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'retry_inconsistent_checkpoint_state' in str(ctx.value)


class SameStepCapConfigTests(unittest.TestCase):

    def _write_workflow_config(self, tmpdir: str, text: str) -> Path:
        home_dir = Path(tmpdir)
        return _write_config(home_dir, text)

    def _minimal_config(self, extra_aflow: str = '') -> str:
        return (
            f'[aflow]\n{extra_aflow}\n'
            '[roles]\narchitect = "opencode.default"\n\n'
            '[workflow.simple.steps.s]\nrole = "architect"\nprompts = ["p"]\ngo = [{ to = "END" }]\n'
            '[harness.opencode.profiles.default]\nmodel = "m"\n\n[prompts]\np = "do it"\n'
        )

    def test_max_same_step_turns_defaults_to_five(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(tmpdir, self._minimal_config())
            config = load_workflow_config(config_path)
            assert config.aflow.max_same_step_turns == 5

    def test_max_same_step_turns_reads_positive_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='max_same_step_turns = 3')
            )
            config = load_workflow_config(config_path)
            assert config.aflow.max_same_step_turns == 3

    def test_max_same_step_turns_accepts_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='max_same_step_turns = 0')
            )
            config = load_workflow_config(config_path)
            assert config.aflow.max_same_step_turns == 0

    def test_max_same_step_turns_rejects_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='max_same_step_turns = -1')
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'max_same_step_turns' in str(ctx.value)

    def test_max_same_step_turns_rejects_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_workflow_config(
                tmpdir, self._minimal_config(extra_aflow='max_same_step_turns = true')
            )
            with pytest.raises(ConfigError) as ctx:
                load_workflow_config(config_path)
            assert 'max_same_step_turns' in str(ctx.value)

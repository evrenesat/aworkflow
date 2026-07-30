from aflow._test_support import *  # noqa: F401,F403
from dataclasses import replace
import hashlib
from aflow.api import AnalyzeRequest, analyze_runs
from aflow.config import ErrorHandlingConfig, HarnessErrorRecoveryConfig, HarnessErrorRecoveryRuleConfig, ManagerConfig
from aflow.api.events import ExecutionEventType
from aflow.run_state import (
    ActiveImplementationScope,
    FrozenRunIdentity,
    ImplementationAttempt,
    PendingBoundaryDecision,
    PendingFinalizedTurn,
    PendingManagerNotes,
    PendingRepartitionV1,
    PendingTeamOverride,
    load_override_request,
    manager_resume_fields,
)
from aflow.runlog import create_run_paths, write_run_metadata
from aflow.workflow import (
    _pending_matches_scope_and_plan,
    _reconcile_repartition_plan_copies,
)


def _scope_envelope_observation(run_dir: Path) -> tuple[object, ...]:
    payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    scope = payload["active_implementation_scope"]
    assert scope is not None
    artifact_path = run_dir / scope["envelope_artifact_path"]
    artifacts = list((run_dir / "scopes").glob("*/envelope.json"))
    assert artifacts.count(artifact_path) == 1
    artifact_bytes = artifact_path.read_bytes()
    artifact_payload = json.loads(artifact_bytes)
    assert (
        scope["envelope_artifact_sha256"]
        == hashlib.sha256(artifact_bytes).hexdigest()
    )
    assert (
        scope["envelope_canonical_sha256"]
        == artifact_payload["canonical_envelope_sha256"]
    )
    return (
        scope["envelope_artifact_path"],
        artifact_bytes,
        scope["envelope_artifact_sha256"],
        scope["envelope_canonical_sha256"],
        scope["scope_id"],
        scope["checkpoint_index"],
        scope["checkpoint_name"],
        len(artifacts),
    )


def _pressure_workflow_config(*, role: str) -> WorkflowUserConfig:
    """Build a minimal supervised workflow for pressure-boundary regressions."""
    workflow = WorkflowConfig(
        steps={
            "step": WorkflowStepConfig(
                role=role,
                prompts=("p",),
                go=(
                    GoTransition(to="END", when="DONE"),
                    GoTransition(to="step"),
                ),
            ),
        },
        first_step="step",
    )
    return WorkflowUserConfig(
        roles={
            role: "codex.default",
            "manager_lite": "codex.manager-lite",
            "manager_full": "codex.manager-full",
        },
        harnesses={"codex": WorkflowHarnessConfig(profiles={
            "default": HarnessProfileConfig(model="default"),
            "manager-lite": HarnessProfileConfig(model="manager-lite"),
            "manager-full": HarnessProfileConfig(model="manager-full"),
        })},
        workflows={"managed": workflow},
        prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
        manager=ManagerConfig(
            enabled=True,
            lite_role="manager_lite",
            full_role="manager_full",
        ),
    )


class WorkflowRuntimeTests(unittest.TestCase):

    def test_repartition_route_requires_generation_candidate_and_partition(self) -> None:
        state = ControllerState(
            last_snapshot=PlanSnapshot("First child", 1, 1, False)
        )
        state.active_implementation_scope = ActiveImplementationScope(
            scope_id="scope",
            original_plan_path="/repo/plan.md",
            checkpoint_index=1,
            checkpoint_name="Parent",
            opened_turn_number=1,
            current_partition_generation_id="generation",
            current_partition_candidate_sha256="c" * 64,
            current_partition_id="partition-1",
        )
        pending_boundary = PendingBoundaryDecision(
            finalized_turn_number=1,
            decision_number=1,
            action="repartition_current_checkpoint",
            proposed_action="transition",
            proposed_transition="review",
            resolved_next_step="review",
            scope_id="scope",
            target_plan_identity="/repo/plan.md::checkpoint-1",
            repartition_generation_id="generation",
            repartition_candidate_sha256="c" * 64,
            repartition_partition_id="partition-1",
        )
        pending_notes = PendingManagerNotes(
            target_step="implement",
            notes=("Keep the retained worker.",),
            decision_number=1,
            scope_id="scope",
            target_plan_identity="/repo/plan.md::checkpoint-1",
            repartition_generation_id="generation",
            repartition_candidate_sha256="c" * 64,
            repartition_partition_id="partition-1",
        )
        pending_override = PendingTeamOverride(
            target_step="implement",
            role="worker",
            source_team="high",
            target_team="high",
            selector="codex.worker-high",
            checkpoint_identity="/repo/plan.md::checkpoint-1",
            decision_number=1,
            scope_id="scope",
            target_plan_identity="/repo/plan.md::checkpoint-1",
            repartition_generation_id="generation",
            repartition_candidate_sha256="c" * 64,
            repartition_partition_id="partition-1",
        )
        for pending in (pending_boundary, pending_notes, pending_override):
            assert _pending_matches_scope_and_plan(
                pending, state, "/repo/plan.md::checkpoint-1"
            )
            for stale_pending in (
                replace(
                    pending,
                    repartition_generation_id=None,
                    repartition_candidate_sha256=None,
                    repartition_partition_id=None,
                ),
                replace(pending, repartition_generation_id=None),
                replace(pending, repartition_candidate_sha256=None),
                replace(pending, repartition_partition_id=None),
                replace(pending, repartition_generation_id="stale-generation"),
                replace(pending, repartition_candidate_sha256="d" * 64),
                replace(pending, repartition_partition_id="stale-parent"),
            ):
                assert not _pending_matches_scope_and_plan(
                    stale_pending,
                    state,
                    "/repo/plan.md::checkpoint-1",
                )

        restored = manager_resume_fields({
            "pending_manager_notes": {
                **pending_notes.__dict__,
                "notes": list(pending_notes.notes),
            },
            "pending_step_team_override": pending_override.__dict__,
        })
        assert restored["pending_manager_notes"] == pending_notes
        assert restored["pending_step_team_override"] == pending_override

        state.active_implementation_scope = replace(
            state.active_implementation_scope,
            current_partition_generation_id=None,
            current_partition_candidate_sha256=None,
            current_partition_id=None,
        )
        for pending in (pending_boundary, pending_notes, pending_override):
            assert _pending_matches_scope_and_plan(
                replace(
                    pending,
                    repartition_generation_id=None,
                    repartition_candidate_sha256=None,
                    repartition_partition_id=None,
                ),
                state,
                "/repo/plan.md::checkpoint-1",
            )

    def test_repartition_copy_transaction_recovers_mixed_source_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            execution = root / "worktree" / "plan.md"
            primary = root / "primary" / "plan.md"
            execution.parent.mkdir()
            primary.parent.mkdir()
            source = b"# source\n"
            candidate = b"# candidate\n"
            execution.write_bytes(source)
            primary.write_bytes(source)
            pending = PendingRepartitionV1(
                schema_version=1,
                decision_number=1,
                scope_id="scope",
                stage="semantically_validated",
                envelope_sha256="e" * 64,
                source_plan_sha256=hashlib.sha256(source).hexdigest(),
                candidate_plan_sha256=hashlib.sha256(candidate).hexdigest(),
            )

            def interrupt_after_execution(value):
                assert value.stage == "execution_plan_applied"
                raise RuntimeError("interrupted")

            with pytest.raises(RuntimeError, match="interrupted"):
                _reconcile_repartition_plan_copies(
                    pending,
                    candidate_bytes=candidate,
                    execution_path=execution,
                    primary_path=primary,
                    persist=interrupt_after_execution,
                )
            assert execution.read_bytes() == candidate
            assert primary.read_bytes() == source

            stages = []
            recovered = _reconcile_repartition_plan_copies(
                pending,
                candidate_bytes=candidate,
                execution_path=execution,
                primary_path=primary,
                persist=lambda value: stages.append(value.stage),
            )
            assert execution.read_bytes() == candidate
            assert primary.read_bytes() == candidate
            assert recovered.stage == "primary_plan_applied"
            assert stages == ["execution_plan_applied", "primary_plan_applied"]

    def test_repartition_copy_transaction_preserves_unknown_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            execution = root / "execution.md"
            primary = root / "primary.md"
            source = b"# source\n"
            candidate = b"# candidate\n"
            unknown = b"# owner edit\n"
            execution.write_bytes(source)
            primary.write_bytes(unknown)
            pending = PendingRepartitionV1(
                schema_version=1,
                decision_number=1,
                scope_id="scope",
                stage="semantically_validated",
                envelope_sha256="e" * 64,
                source_plan_sha256=hashlib.sha256(source).hexdigest(),
                candidate_plan_sha256=hashlib.sha256(candidate).hexdigest(),
            )
            with pytest.raises(
                WorkflowError,
                match="no copy was overwritten",
            ) as error:
                _reconcile_repartition_plan_copies(
                    pending,
                    candidate_bytes=candidate,
                    execution_path=execution,
                    primary_path=primary,
                    persist=lambda _value: None,
                )
            assert "expected source=" in str(error.value)
            assert "observed=" in str(error.value)
            assert execution.read_bytes() == source
            assert primary.read_bytes() == unknown

    def test_repartition_copy_transaction_resumes_each_persisted_stage(self) -> None:
        source = b"# source\n"
        candidate = b"# candidate\n"
        cases = (
            ("semantically_validated", source, source, [
                "execution_plan_applied", "primary_plan_applied",
            ]),
            ("execution_plan_applied", candidate, source, [
                "primary_plan_applied",
            ]),
            ("primary_plan_applied", candidate, candidate, []),
            ("applied", candidate, candidate, []),
        )
        for stage, execution_bytes, primary_bytes, expected_stages in cases:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                execution = root / "execution.md"
                primary = root / "primary.md"
                execution.write_bytes(execution_bytes)
                primary.write_bytes(primary_bytes)
                pending = PendingRepartitionV1(
                    schema_version=1,
                    decision_number=1,
                    scope_id="scope",
                    stage=stage,
                    envelope_sha256="e" * 64,
                    source_plan_sha256=hashlib.sha256(source).hexdigest(),
                    candidate_plan_sha256=hashlib.sha256(candidate).hexdigest(),
                )
                stages = []
                recovered = _reconcile_repartition_plan_copies(
                    pending,
                    candidate_bytes=candidate,
                    execution_path=execution,
                    primary_path=primary,
                    persist=lambda value: stages.append(value.stage),
                )
                assert execution.read_bytes() == candidate
                assert primary.read_bytes() == candidate
                assert stages == expected_stages
                assert recovered.stage == (
                    expected_stages[-1] if expected_stages else stage
                )

    def test_override_loader_accepts_exact_grammar_and_digest_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "overrides.toml"
            path.write_text(
                'next_step = "review"\nteam = "strong"\nmax_turns = 7\n'
                'notes = ["focus on the failing test"]\n',
                encoding="utf-8",
            )
            loaded = load_override_request(path)
            assert loaded.status == "valid"
            assert loaded.request is not None
            assert loaded.request.next_step == "review"
            assert loaded.request.team == "strong"
            assert loaded.request.max_turns == 7
            assert loaded.request.notes == ("focus on the failing test",)

            consumed = load_override_request(
                path,
                consumed_digest=loaded.request.digest,
            )
            assert consumed.status == "already_consumed"
            path.write_text('max_turns = 8\n', encoding="utf-8")
            changed = load_override_request(
                path,
                consumed_digest=loaded.request.digest,
            )
            assert changed.status == "valid"
            assert changed.request is not None
            assert changed.request.digest != loaded.request.digest

    def test_atomic_run_metadata_failure_preserves_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            config = ControllerConfig(repo_root=repo_root, plan_path=plan_path)
            paths = create_run_paths(config)
            state = ControllerState(
                last_snapshot=PlanSnapshot("Checkpoint 1: First", 1, 0, False)
            )
            state.frozen_run_identity = FrozenRunIdentity(
                workflow_name="simple",
                config_path=str(repo_root),
                config_fingerprint="abc123",
            )
            write_run_metadata(paths, config, state, status="running")
            previous = paths.run_json.read_text(encoding="utf-8")

            state.status_message = "new state"
            with patch("aflow.runlog.os.replace", side_effect=OSError("interrupted")):
                with pytest.raises(OSError, match="interrupted"):
                    write_run_metadata(paths, config, state, status="running")

            assert paths.run_json.read_text(encoding="utf-8") == previous
            payload = json.loads(previous)
            assert payload["schema_version"] == 1
            assert payload["frozen_config"]["config_fingerprint"] == "abc123"

    def test_valid_boundary_override_routes_once_and_appends_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="END", when="DONE"),),
                    ),
                    "review": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="END", when="DONE"),),
                    ),
                },
                first_step="implement",
                team="base",
            )
            workflow_config = WorkflowUserConfig(
                harnesses={
                    "codex": WorkflowHarnessConfig(
                        profiles={
                            "base": HarnessProfileConfig(model="base"),
                            "strong": HarnessProfileConfig(model="strong"),
                        }
                    )
                },
                roles={"worker": "codex.base"},
                teams={
                    "base": TeamConfig(roles={"worker": "codex.base"}),
                    "strong": TeamConfig(roles={"worker": "codex.strong"}),
                },
                workflows={"test": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
            )
            actual_create = create_run_paths
            created_paths = []

            def create_with_override(config):
                paths = actual_create(config)
                created_paths.append(paths)
                (paths.run_dir / "overrides.toml").write_text(
                    'next_step = "review"\nteam = "strong"\nmax_turns = 3\n'
                    'notes = ["focus on boundary behavior"]\n',
                    encoding="utf-8",
                )
                return paths

            invocations: list[tuple[str, ...]] = []

            def runner(argv, **kwargs):
                invocations.append(tuple(argv))
                _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            with patch(
                "aflow.workflow.create_run_paths",
                side_effect=create_with_override,
            ):
                result = run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=2,
                    ),
                    workflow_config,
                    "test",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            assert result.turns_completed == 1
            assert len(invocations) == 1
            invocation_text = " ".join(invocations[0])
            assert "strong" in invocation_text
            assert "focus on boundary behavior" in invocation_text
            payload = json.loads(
                created_paths[0].run_json.read_text(encoding="utf-8")
            )
            assert payload["current_step_name"] == "review"
            assert payload["team"] == "strong"
            assert payload["effective_max_turns"] == 3
            assert payload["override_result"]["status"] == "accepted"
            assert payload["override_result"]["applied"] is True
            assert (
                'notes = ["focus on boundary behavior"]'
                in payload["override_result"]["source_text"]
            )

    def test_invalid_boundary_override_waits_without_launching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow_config = _make_simple_wf_config()
            actual_create = create_run_paths
            created_paths = []

            def create_with_override(config):
                paths = actual_create(config)
                created_paths.append(paths)
                (paths.run_dir / "overrides.toml").write_text(
                    'next_step = "missing"\n',
                    encoding="utf-8",
                )
                return paths

            runner = unittest.mock.Mock()
            with patch(
                "aflow.workflow.create_run_paths",
                side_effect=create_with_override,
            ):
                with pytest.raises(
                    WorkflowError,
                    match="waiting_for_valid_override",
                ):
                    run_workflow(
                        ControllerConfig(
                            repo_root=repo_root,
                            plan_path=plan_path,
                            max_turns=2,
                        ),
                        workflow_config,
                        "simple",
                        config_dir=repo_root,
                        adapter=CodexAdapter(),
                        runner=runner,
                    )

            runner.assert_not_called()
            payload = json.loads(
                created_paths[0].run_json.read_text(encoding="utf-8")
            )
            assert payload["status"] == "waiting_for_valid_override"
            assert payload["override_result"]["status"] == "rejected"
            assert "not an executable step" in payload["status_message"]

    def test_boundary_override_rejects_unknown_team_without_launching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow_config = _make_simple_wf_config()
            actual_create = create_run_paths

            def create_with_override(config):
                paths = actual_create(config)
                (paths.run_dir / "overrides.toml").write_text(
                    'team = "missing"\n',
                    encoding="utf-8",
                )
                return paths

            runner = unittest.mock.Mock()
            with patch(
                "aflow.workflow.create_run_paths",
                side_effect=create_with_override,
            ):
                with pytest.raises(WorkflowError, match="not configured"):
                    run_workflow(
                        ControllerConfig(
                            repo_root=repo_root,
                            plan_path=plan_path,
                            max_turns=2,
                        ),
                        workflow_config,
                        "simple",
                        config_dir=repo_root,
                        adapter=CodexAdapter(),
                        runner=runner,
                    )
            runner.assert_not_called()

    def test_override_written_during_turn_applies_only_to_following_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement"),
                        ),
                    )
                },
                first_step="implement",
                team="base",
            )
            workflow_config = WorkflowUserConfig(
                harnesses={
                    "codex": WorkflowHarnessConfig(
                        profiles={
                            "base": HarnessProfileConfig(model="base"),
                            "strong": HarnessProfileConfig(model="strong"),
                        }
                    )
                },
                roles={"worker": "codex.base"},
                teams={
                    "base": TeamConfig(roles={"worker": "codex.base"}),
                    "strong": TeamConfig(roles={"worker": "codex.strong"}),
                },
                workflows={"test": workflow},
                prompts={"p": "Work."},
            )
            actual_create = create_run_paths
            created_paths = []

            def capture_paths(config):
                paths = actual_create(config)
                created_paths.append(paths)
                return paths

            invocations: list[tuple[str, ...]] = []

            def runner(argv, **kwargs):
                invocations.append(tuple(argv))
                if len(invocations) == 1:
                    (created_paths[0].run_dir / "overrides.toml").write_text(
                        'team = "strong"\nnotes = ["second turn only"]\n',
                        encoding="utf-8",
                    )
                else:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            with patch(
                "aflow.workflow.create_run_paths",
                side_effect=capture_paths,
            ):
                result = run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                    ),
                    workflow_config,
                    "test",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            assert result.turns_completed == 2
            first = " ".join(invocations[0])
            second = " ".join(invocations[1])
            assert "second turn only" not in first
            assert "strong" not in first
            assert "second turn only" in second
            assert "strong" in second

    def test_boundary_override_can_raise_initial_turn_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow_config = _make_simple_wf_config()
            actual_create = create_run_paths

            def create_with_override(config):
                paths = actual_create(config)
                (paths.run_dir / "overrides.toml").write_text(
                    "max_turns = 2\n",
                    encoding="utf-8",
                )
                return paths

            calls = 0

            def runner(argv, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            with patch(
                "aflow.workflow.create_run_paths",
                side_effect=create_with_override,
            ):
                result = run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=1,
                    ),
                    workflow_config,
                    "simple",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            assert calls == 2
            assert result.turns_completed == 2
            assert result.end_reason == "done"

    def test_boundary_override_rejects_limit_below_completed_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow_config = _make_simple_wf_config()
            actual_create = create_run_paths
            created_paths = []
            calls = 0

            def capture_paths(config):
                paths = actual_create(config)
                created_paths.append(paths)
                return paths

            def runner(argv, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    (created_paths[0].run_dir / "overrides.toml").write_text(
                        "max_turns = 1\n",
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            with patch(
                "aflow.workflow.create_run_paths",
                side_effect=capture_paths,
            ):
                with pytest.raises(WorkflowError, match="below completed turns"):
                    run_workflow(
                        ControllerConfig(
                            repo_root=repo_root,
                            plan_path=plan_path,
                            max_turns=3,
                        ),
                        workflow_config,
                        "simple",
                        config_dir=repo_root,
                        adapter=CodexAdapter(),
                        runner=runner,
                    )

            assert calls == 2
            payload = json.loads(
                created_paths[0].run_json.read_text(encoding="utf-8")
            )
            assert payload["status"] == "waiting_for_valid_override"

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
            assert turn_result['status'] == 'completed'
            assert turn_result['duration_seconds'] >= 0

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
            assert turn1_result['status'] == 'completed'
            assert turn1_result['duration_seconds'] >= 0
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
            assert turn_result['status'] == 'completed'
            assert turn_result['duration_seconds'] >= 0

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
                    live_run_dirs = list((repo_root / '.aflow' / 'runs').iterdir())
                    assert len(live_run_dirs) == 1
                    live_run = json.loads(
                        (live_run_dirs[0] / 'run.json').read_text(encoding='utf-8')
                    )
                    assert live_run['worktree_path'] == str(cwd)
                    assert live_run['feature_branch']
                    assert live_run['lifecycle_setup'] == ['worktree', 'branch']
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

    def test_resume_retries_unfinished_step_instead_of_cli_start_step(self) -> None:
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
            base = _make_worktree_wf_config(worktree_root=str(worktree_root))
            workflow = WorkflowConfig(
                steps={
                    'implement': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='review'),),
                    ),
                    'review': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='END'),),
                    ),
                },
                first_step='implement',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
                main_branch='main',
            )
            wf_config = WorkflowUserConfig(
                aflow=base.aflow,
                roles=base.roles,
                harnesses=base.harnesses,
                workflows={'wt_wf': workflow},
                prompts=base.prompts,
            )

            def failing_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, 'failed', 'failed')

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                        start_step='implement',
                    ),
                    wf_config,
                    'wt_wf',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=failing_runner,
                )

            first_run = json.loads(
                (first_ctx.value.run_dir / 'run.json').read_text(encoding='utf-8')
            )
            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=first_run['feature_branch'],
                worktree_path=Path(first_run['worktree_path']),
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
                interrupted_step_name='review',
            )

            with pytest.raises(WorkflowError) as resumed_ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                        start_step='implement',
                    ),
                    wf_config,
                    'wt_wf',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=failing_runner,
                    resume=resume_ctx,
                )

            turn = json.loads(
                (
                    resumed_ctx.value.run_dir
                    / 'turns'
                    / 'turn-001'
                    / 'result.json'
                ).read_text(encoding='utf-8')
            )
            assert turn['step_name'] == 'review'

    def test_resume_restores_active_plan_from_reused_worktree(self) -> None:
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
            wf_config = _make_worktree_wf_config(
                worktree_root=str(worktree_root)
            )

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                    ),
                    wf_config,
                    'wt_wf',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                        argv, 1, 'failed', 'first run failed'
                    ),
                )

            first_run = json.loads(
                (first_ctx.value.run_dir / 'run.json').read_text(
                    encoding='utf-8'
                )
            )
            worktree_path = Path(first_run['worktree_path'])
            logical_repair = repo_root / 'plan-cp01-v01.md'
            execution_repair = worktree_path / logical_repair.relative_to(
                repo_root
            )
            execution_repair.write_text('# Repair\n', encoding='utf-8')
            captured_prompt: list[str] = []

            def resumed_runner(argv, **kwargs):
                captured_prompt.append(' '.join(argv))
                return subprocess.CompletedProcess(
                    argv, 1, 'failed', 'resumed run failed'
                )

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=first_run['feature_branch'],
                worktree_path=worktree_path,
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
                active_plan_path=logical_repair,
            )

            with pytest.raises(WorkflowError):
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                    ),
                    wf_config,
                    'wt_wf',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=resumed_runner,
                    resume=resume_ctx,
                )

            assert captured_prompt
            assert str(execution_repair) in captured_prompt[0]

    def test_resumed_repair_approval_returns_next_worker_to_original_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_path = root / 'worktree'
            plan_path = repo_root / 'plan.md'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [ ] Checkpoint 1: First\n'
                '- [ ] step one\n\n'
                '### [ ] Checkpoint 2: Second\n'
                '- [ ] step two\n',
            )
            _git_commit_file(repo_root, plan_path)
            subprocess.run(
                [
                    'git',
                    'worktree',
                    'add',
                    '-b',
                    'resume-feature',
                    str(worktree_path),
                    'main',
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            logical_repair = repo_root / 'plan-cp01-v01.md'
            execution_repair = (
                worktree_path / logical_repair.relative_to(repo_root)
            )
            _write_plan(
                execution_repair,
                '# Repair\n\n'
                '## Required Repair Steps\n'
                '- [x] repair step\n\n'
                '## Done When\n'
                '- [x] verified\n',
            )
            workflow = WorkflowConfig(
                steps={
                    'review': WorkflowStepConfig(
                        role='architect',
                        prompts=('review_prompt',),
                        go=(
                            GoTransition(
                                to='implement',
                                when='NEW_PLAN_EXISTS || !DONE',
                            ),
                        ),
                    ),
                    'implement': WorkflowStepConfig(
                        role='architect',
                        prompts=('implement_prompt',),
                        go=(GoTransition(to='END'),),
                    ),
                },
                first_step='review',
                setup=('worktree', 'branch'),
                teardown=(),
                main_branch='main',
            )
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'default': HarnessProfileConfig(model='gpt-5.4'),
                })},
                workflows={'repair_loop': workflow},
                prompts={
                    'review_prompt': (
                        'Review active {ACTIVE_PLAN_PATH}; '
                        'write findings to {NEW_PLAN_PATH}.'
                    ),
                    'implement_prompt': 'Implement {ACTIVE_PLAN_PATH}.',
                },
            )
            prompts: list[str] = []

            def runner(argv, **kwargs):
                prompt = ' '.join(argv)
                prompts.append(prompt)
                if len(prompts) == 1:
                    execution_plan = (
                        Path(kwargs['cwd'])
                        / plan_path.relative_to(repo_root)
                    )
                    _write_plan(
                        execution_plan,
                        '# Plan\n\n'
                        '### [x] Checkpoint 1: First\n'
                        '- [x] step one\n\n'
                        '### [ ] Checkpoint 2: Second\n'
                        '- [ ] step two\n',
                    )
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=2,
                    start_step='review',
                ),
                wf_config,
                'repair_loop',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
                resume=ResumeContext(
                    resumed_from_run_id='prior-run',
                    feature_branch='resume-feature',
                    worktree_path=worktree_path,
                    main_branch='main',
                    setup=('worktree', 'branch'),
                    teardown=(),
                    active_plan_path=logical_repair,
                    interrupted_step_name='review',
                ),
            )

            assert len(prompts) == 2
            execution_plan = worktree_path / plan_path.relative_to(repo_root)
            expected_v2 = worktree_path / 'plan-cp01-v02.md'
            assert str(execution_repair) in prompts[0]
            assert str(expected_v2) in prompts[0]
            assert str(execution_plan) in prompts[1]
            assert str(execution_repair) not in prompts[1]
            turn_one = json.loads(
                (
                    result.run_dir
                    / 'turns'
                    / 'turn-001'
                    / 'result.json'
                ).read_text(encoding='utf-8')
            )
            assert turn_one['conditions']['NEW_PLAN_EXISTS'] is False
            assert turn_one['new_plan_path'] == str(repo_root / 'plan-cp01-v02.md')

    def test_resume_repairs_legacy_approved_overlay_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_path = root / 'worktree'
            plan_path = repo_root / 'plan.md'
            _write_plan(
                plan_path,
                '# Plan\n\n'
                '### [x] Checkpoint 1: First\n'
                '- [x] step one\n\n'
                '### [ ] Checkpoint 2: Second\n'
                '- [ ] step two\n',
            )
            _git_commit_file(repo_root, plan_path)
            subprocess.run(
                [
                    'git',
                    'worktree',
                    'add',
                    '-b',
                    'resume-feature',
                    str(worktree_path),
                    'main',
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            logical_repair = repo_root / 'plan-cp01-v01.md'
            execution_repair = (
                worktree_path / logical_repair.relative_to(repo_root)
            )
            _write_plan(
                execution_repair,
                '# Repair\n\n'
                '## Required Repair Steps\n'
                '- [x] repair step\n\n'
                '## Done When\n'
                '- [x] verified\n',
            )
            workflow = WorkflowConfig(
                steps={
                    'implement': WorkflowStepConfig(
                        role='worker',
                        prompts=('p',),
                        go=(GoTransition(to='END'),),
                    ),
                },
                first_step='implement',
                setup=('worktree', 'branch'),
                teardown=(),
                main_branch='main',
            )
            wf_config = WorkflowUserConfig(
                roles={'worker': 'codex.default'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'default': HarnessProfileConfig(model='gpt-5.4'),
                })},
                workflows={'repair_loop': workflow},
                prompts={'p': 'Implement {ACTIVE_PLAN_PATH}.'},
            )
            scope = ActiveImplementationScope(
                scope_id=f'{plan_path}::checkpoint-1::first',
                original_plan_path=str(plan_path),
                checkpoint_index=1,
                checkpoint_name='First',
                opened_turn_number=1,
                awaiting_review=False,
            )
            captured_prompt: list[str] = []
            next_scope_captured_before_worker: list[bool] = []

            def runner(argv, **kwargs):
                captured_prompt.append(' '.join(argv))
                run_dir = next((repo_root / '.aflow' / 'runs').iterdir())
                payload = json.loads((run_dir / 'run.json').read_text())
                resumed_scope = payload['active_implementation_scope']
                next_scope_captured_before_worker.append(
                    resumed_scope['checkpoint_index'] == 2
                    and resumed_scope['envelope_artifact_path'] is not None
                    and resumed_scope['envelope_artifact_sha256'] is not None
                    and resumed_scope['envelope_canonical_sha256'] is not None
                )
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=2,
                    start_step='implement',
                ),
                wf_config,
                'repair_loop',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
                resume=ResumeContext(
                    resumed_from_run_id='prior-run',
                    feature_branch='resume-feature',
                    worktree_path=worktree_path,
                    main_branch='main',
                    setup=('worktree', 'branch'),
                    teardown=(),
                    active_plan_path=logical_repair,
                    interrupted_step_name='implement',
                    active_implementation_scope=scope,
                    pending_boundary_decision=PendingBoundaryDecision(
                        finalized_turn_number=3,
                        decision_number=9,
                        action='continue',
                        proposed_action='transition',
                        proposed_transition='implement',
                        resolved_next_step='implement',
                        target_role='worker',
                        target_selector='codex.default',
                        checkpoint_identity=(
                            f'{logical_repair}::checkpoint-complete'
                        ),
                        post_transition_active_plan_path=str(logical_repair),
                        post_transition_checkpoint_identity=(
                            f'{logical_repair}::checkpoint-complete'
                        ),
                        applied=True,
                        consumed=True,
                        scope_id=scope.scope_id,
                        target_plan_identity=(
                            f'{logical_repair}::checkpoint-complete'
                        ),
                    ),
                ),
            )

            assert len(captured_prompt) == 1
            assert next_scope_captured_before_worker == [True]
            execution_plan = worktree_path / plan_path.relative_to(repo_root)
            assert str(execution_plan) in captured_prompt[0]
            assert str(execution_repair) not in captured_prompt[0]
            run_payload = json.loads(
                (result.run_dir / 'run.json').read_text(encoding='utf-8')
            )
            assert run_payload['active_plan_path'] == str(plan_path)
            assert run_payload['active_implementation_scope'] is not None
            assert run_payload['active_implementation_scope']['checkpoint_index'] == 2
            assert run_payload['active_implementation_scope']['envelope_artifact_path'] is not None

    def test_resume_replays_completed_reviewer_boundary_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_path = root / 'worktree'
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            _git_commit_file(repo_root, plan_path)
            subprocess.run(
                [
                    'git',
                    'worktree',
                    'add',
                    '-b',
                    'resume-feature',
                    str(worktree_path),
                    'main',
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            repair_path = repo_root / 'plan-cp01-v01.md'
            execution_repair = (
                worktree_path / repair_path.relative_to(repo_root)
            )
            _write_plan(
                execution_repair,
                '# Repair\n\n'
                '### [ ] Checkpoint 1: Repair\n'
                '- [ ] fix reviewer finding\n',
            )
            source_run = repo_root / '.aflow' / 'runs' / 'prior-run'
            prior_turn = source_run / 'turns' / 'turn-001'
            prior_turn.mkdir(parents=True)
            (prior_turn / 'result.json').write_text(
                json.dumps({
                    'turn_number': 1,
                    'status': 'completed',
                    'step_name': 'review',
                    'step_role': 'reviewer',
                    'selector': 'codex.reviewer',
                    'returncode': 0,
                    'snapshot_before': PlanSnapshot(
                        'Earlier', 2, 1, False, 2, 1
                    ).to_dict(),
                    'snapshot_after': PlanSnapshot(
                        'Earlier', 2, 1, False, 2, 1
                    ).to_dict(),
                    'conditions': {
                        'DONE': False,
                        'NEW_PLAN_EXISTS': True,
                        'MAX_TURNS_REACHED': False,
                    },
                    'chosen_transition': 'implement',
                }),
                encoding='utf-8',
            )
            (prior_turn / 'stdout.txt').write_text(
                'Earlier checkpoint rejection.',
                encoding='utf-8',
            )
            (prior_turn / 'stderr.txt').write_text('', encoding='utf-8')
            source_turn = source_run / 'turns' / 'turn-004'
            source_turn.mkdir(parents=True)
            snapshot = PlanSnapshot('First', 1, 1, False, 1, 1)
            source_result = {
                'turn_number': 4,
                'status': 'completed',
                'step_name': 'review',
                'step_role': 'reviewer',
                'selector': 'codex.reviewer',
                'returncode': 0,
                'active_plan_path': str(plan_path),
                'new_plan_path': str(repair_path),
                'snapshot_before': snapshot.to_dict(),
                'snapshot_after': snapshot.to_dict(),
                'conditions': {
                    'DONE': False,
                    'NEW_PLAN_EXISTS': True,
                    'MAX_TURNS_REACHED': False,
                },
                'chosen_transition': 'implement',
                'chosen_transition_condition': 'NEW_PLAN_EXISTS || !DONE',
            }
            (source_turn / 'result.json').write_text(
                json.dumps(source_result),
                encoding='utf-8',
            )
            (source_turn / 'stdout.txt').write_text(
                'Reviewer rejected and created a repair plan.',
                encoding='utf-8',
            )
            (source_turn / 'stderr.txt').write_text('', encoding='utf-8')

            workflow = WorkflowConfig(
                steps={
                    'review': WorkflowStepConfig(
                        role='reviewer',
                        prompts=('p',),
                        go=(
                            GoTransition(
                                to='implement',
                                when='NEW_PLAN_EXISTS || !DONE',
                            ),
                            GoTransition(to='END'),
                        ),
                    ),
                    'implement': WorkflowStepConfig(
                        role='worker',
                        prompts=('p',),
                        go=(GoTransition(to='END'),),
                    ),
                },
                first_step='review',
                setup=('worktree', 'branch'),
                teardown=(),
                main_branch='main',
            )
            wf_config = WorkflowUserConfig(
                roles={
                    'manager_lite': 'codex.manager-lite',
                    'manager_full': 'codex.manager-full',
                    'reviewer': 'codex.reviewer',
                    'worker': 'codex.worker-low',
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'reviewer': HarnessProfileConfig(model='reviewer'),
                    'worker-low': HarnessProfileConfig(model='worker-low'),
                    'worker-high': HarnessProfileConfig(model='worker-high'),
                    'manager-lite': HarnessProfileConfig(model='manager-lite'),
                    'manager-full': HarnessProfileConfig(model='manager-full'),
                })},
                teams={
                    'default': TeamConfig(
                        roles={
                            'reviewer': 'codex.reviewer',
                            'worker': 'codex.worker-low',
                        },
                        upgrade_to='high',
                    ),
                    'high': TeamConfig(roles={
                        'reviewer': 'codex.reviewer',
                        'worker': 'codex.worker-high',
                    }),
                },
                workflows={'managed': workflow},
                prompts={'p': 'Work from {ACTIVE_PLAN_PATH}.'},
                manager=ManagerConfig(
                    enabled=True,
                    lite_role='manager_lite',
                    full_role='manager_full',
                    full_after_stalled_turns=99,
                ),
            )
            scope = ActiveImplementationScope(
                scope_id=f'{plan_path}::checkpoint-1::first',
                original_plan_path=str(plan_path),
                checkpoint_index=1,
                checkpoint_name='First',
                opened_turn_number=3,
                awaiting_review=True,
            )
            calls: list[str] = []
            prompts: list[str] = []

            def runner(argv, **kwargs):
                model = argv[argv.index('--model') + 1]
                calls.append(model)
                prompts.append(argv[-1])
                if model.startswith('manager-'):
                    action = (
                        'upgrade_next_implementation'
                        if calls.count('manager-lite') == 1
                        else 'continue'
                    )
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps({
                            'schema_version': 1,
                            'action': action,
                            'reason': 'Synthetic resume boundary decision.',
                            'next_step_notes': [],
                            'stop_report': None,
                        }),
                        '',
                    )
                assert model == 'worker-high', calls
                _write_plan(
                    execution_repair,
                    '# Repair\n\n'
                    '### [x] Checkpoint 1: Repair\n'
                    '- [x] fix reviewer finding\n',
                )
                _write_plan(
                    worktree_path / plan_path.relative_to(repo_root),
                    _VALID_PLAN.replace('[ ]', '[x]'),
                )
                return subprocess.CompletedProcess(argv, 0, 'repaired', '')

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=1,
                    start_step='implement',
                    team='default',
                ),
                wf_config,
                'managed',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
                resume=ResumeContext(
                    resumed_from_run_id='prior-run',
                    feature_branch='resume-feature',
                    worktree_path=worktree_path,
                    main_branch='main',
                    setup=('worktree', 'branch'),
                    teardown=(),
                    active_plan_path=repair_path,
                    reviewer_rejection_count=1,
                    implementation_attempts={
                        scope.scope_id: (
                            ImplementationAttempt(
                                3,
                                'implement',
                                'worker',
                                'default',
                                'codex.worker-low',
                                'progress',
                            ),
                        ),
                    },
                    active_implementation_scope=scope,
                    pending_finalized_turn=PendingFinalizedTurn(
                        source_run_dir=source_run,
                        turn_number=4,
                        step_name='review',
                        step_role='reviewer',
                        selector='codex.reviewer',
                        active_plan_path=plan_path,
                        new_plan_path=repair_path,
                        snapshot_after=snapshot,
                        conditions=source_result['conditions'],
                        chosen_transition='implement',
                        chosen_transition_condition=(
                            'NEW_PLAN_EXISTS || !DONE'
                        ),
                    ),
                ),
            )

            assert calls[:2] == ['manager-lite', 'worker-high']
            assert str(execution_repair) in prompts[1]
            boundary = json.loads(
                (
                    result.run_dir
                    / 'manager'
                    / 'decision-001'
                    / 'boundary.json'
                ).read_text(encoding='utf-8')
            )
            assert (
                boundary['boundary']['artifact_path']
                == 'resumed-from/prior-run/turns/turn-004'
            )

    def test_preserved_active_plan_uses_worktree_execution_path(self) -> None:
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
            base = _make_worktree_wf_config(
                worktree_root=str(worktree_root)
            )
            workflow = WorkflowConfig(
                steps={
                    'review': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(GoTransition(to='rework'),),
                    ),
                    'rework': WorkflowStepConfig(
                        role='architect',
                        prompts=('p',),
                        go=(
                            GoTransition(
                                to='review',
                                preserve_active_plan=True,
                            ),
                        ),
                    ),
                },
                first_step='review',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
                main_branch='main',
            )
            wf_config = WorkflowUserConfig(
                aflow=base.aflow,
                roles=base.roles,
                harnesses=base.harnesses,
                workflows={'repair_loop': workflow},
                prompts={'p': 'Active: {ACTIVE_PLAN_PATH}.'},
            )
            captured_active: list[str] = []
            logical_repair = repo_root / 'plan-cp01-v01.md'

            def runner(argv, **kwargs):
                if len(captured_active) >= 3:
                    return subprocess.CompletedProcess(
                        argv, 1, 'recovery failed', ''
                    )
                prompt = ' '.join(argv)
                import re as _re
                match = _re.search(r'Active: (\S+)', prompt)
                assert match is not None
                captured_active.append(match.group(1).rstrip('.'))
                if len(captured_active) == 1:
                    execution_repair = (
                        Path(kwargs['cwd'])
                        / logical_repair.relative_to(repo_root)
                    )
                    execution_repair.write_text(
                        '# Repair\n', encoding='utf-8'
                    )
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                if len(captured_active) == 2:
                    return subprocess.CompletedProcess(argv, 0, 'ok', '')
                return subprocess.CompletedProcess(
                    argv, 1, 'stop after assertion', ''
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=4,
                    ),
                    wf_config,
                    'repair_loop',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            run_payload = json.loads(
                (ctx.value.run_dir / 'run.json').read_text(encoding='utf-8')
            )
            execution_root = Path(run_payload['worktree_path'])
            execution_plan = execution_root / plan_path.relative_to(repo_root)
            execution_repair = execution_root / logical_repair.relative_to(
                repo_root
            )
            assert captured_active[:3] == [
                str(execution_plan),
                str(execution_repair),
                str(execution_repair),
            ]
            assert run_payload['active_plan_path'] == str(logical_repair)

    def test_resume_rejects_missing_saved_active_plan(self) -> None:
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
            wf_config = _make_worktree_wf_config(
                worktree_root=str(worktree_root)
            )

            with pytest.raises(WorkflowError) as first_ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                    ),
                    wf_config,
                    'wt_wf',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                        argv, 1, 'failed', 'first run failed'
                    ),
                )

            first_run = json.loads(
                (first_ctx.value.run_dir / 'run.json').read_text(
                    encoding='utf-8'
                )
            )
            runner_called = [False]

            def unexpected_runner(argv, **kwargs):
                runner_called[0] = True
                return subprocess.CompletedProcess(argv, 0, 'ok', '')

            resume_ctx = ResumeContext(
                resumed_from_run_id=first_ctx.value.run_dir.name,
                feature_branch=first_run['feature_branch'],
                worktree_path=Path(first_run['worktree_path']),
                main_branch='main',
                setup=('worktree', 'branch'),
                teardown=('merge', 'rm_worktree'),
                active_plan_path=repo_root / 'missing-repair.md',
            )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                    ),
                    wf_config,
                    'wt_wf',
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=unexpected_runner,
                    resume=resume_ctx,
                )

            assert runner_called[0] is False
            assert 'cannot resume with the saved active plan' in str(ctx.value)

    def test_resume_replays_completed_worker_after_removed_active_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_path = root / 'worktree'
            plan_path = repo_root / 'plan.md'
            advanced_plan = (
                '# Plan\n\n'
                '### [x] Checkpoint 1: First\n'
                '- [x] step one\n\n'
                '### [ ] Checkpoint 2: Second\n'
                '- [ ] step two\n'
            )
            _write_plan(plan_path, advanced_plan)
            _git_commit_file(repo_root, plan_path)
            subprocess.run(
                [
                    'git',
                    'worktree',
                    'add',
                    '-b',
                    'resume-feature',
                    str(worktree_path),
                    'main',
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            logical_repair = repo_root / 'plan-cp01-v01.md'
            execution_plan = worktree_path / plan_path.relative_to(repo_root)
            workflow = WorkflowConfig(
                steps={
                    'implement': WorkflowStepConfig(
                        role='worker',
                        prompts=('implement_prompt',),
                        go=(
                            GoTransition(
                                to='review',
                                preserve_active_plan=True,
                            ),
                        ),
                    ),
                    'review': WorkflowStepConfig(
                        role='reviewer',
                        prompts=('review_prompt',),
                        go=(GoTransition(to='END'),),
                    ),
                },
                first_step='implement',
                setup=('worktree', 'branch'),
                teardown=(),
                main_branch='main',
            )
            wf_config = WorkflowUserConfig(
                roles={
                    'worker': 'codex.low',
                    'reviewer': 'codex.review',
                },
                teams={
                    'sol_medium': TeamConfig(
                        roles={'worker': 'codex.sol'},
                    ),
                },
                harnesses={
                    'codex': WorkflowHarnessConfig(
                        profiles={
                            'low': HarnessProfileConfig(model='low'),
                            'sol': HarnessProfileConfig(model='sol'),
                            'review': HarnessProfileConfig(model='review'),
                        },
                    ),
                },
                workflows={'repair_loop': workflow},
                prompts={
                    'implement_prompt': 'Implement {ACTIVE_PLAN_PATH}.',
                    'review_prompt': 'Review {ACTIVE_PLAN_PATH}.',
                },
            )
            scope = ActiveImplementationScope(
                scope_id=f'{plan_path}::checkpoint-1::first',
                original_plan_path=str(plan_path),
                checkpoint_index=1,
                checkpoint_name='First',
                opened_turn_number=1,
                awaiting_review=False,
            )
            calls: list[tuple[str, str]] = []

            def runner(argv, **kwargs):
                calls.append((argv[argv.index('--model') + 1], ' '.join(argv)))
                return subprocess.CompletedProcess(argv, 0, 'reviewed', '')

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=1,
                    start_step='implement',
                    team='sol_medium',
                ),
                wf_config,
                'repair_loop',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
                resume=ResumeContext(
                    resumed_from_run_id='prior-run',
                    feature_branch='resume-feature',
                    worktree_path=worktree_path,
                    main_branch='main',
                    setup=('worktree', 'branch'),
                    teardown=(),
                    active_plan_path=logical_repair,
                    active_implementation_scope=scope,
                    pending_finalized_turn=PendingFinalizedTurn(
                        source_run_dir=repo_root / '.aflow' / 'runs' / 'prior-run',
                        turn_number=7,
                        step_name='implement',
                        step_role='worker',
                        selector='codex.sol',
                        active_plan_path=logical_repair,
                        new_plan_path=repo_root / 'plan-cp01-v02.md',
                        snapshot_after=PlanSnapshot(
                            current_checkpoint_name='Checkpoint 2: Second',
                            unchecked_checkpoint_count=1,
                            current_checkpoint_unchecked_step_count=1,
                            is_complete=False,
                            total_checkpoint_count=2,
                            current_checkpoint_index=2,
                        ),
                        conditions={
                            'DONE': False,
                            'NEW_PLAN_EXISTS': False,
                            'MAX_TURNS_REACHED': False,
                        },
                        chosen_transition='review',
                    ),
                ),
            )

            assert [model for model, _ in calls] == ['review']
            assert str(execution_plan) in calls[0][1]
            assert str(logical_repair) not in calls[0][1]
            turn = json.loads(
                (
                    result.run_dir
                    / 'turns'
                    / 'turn-001'
                    / 'result.json'
                ).read_text(encoding='utf-8')
            )
            assert turn['step_name'] == 'review'
            payload = json.loads(
                (result.run_dir / 'run.json').read_text(encoding='utf-8')
            )
            assert payload['team'] == 'sol_medium'
            assert payload['active_plan_path'] == str(plan_path)

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

    def test_resume_failed_terminal_merge_retries_integration_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            worktree_root = root / 'worktrees'
            worktree_root.mkdir()
            worktree_path = worktree_root / 'feature'
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _COMPLETE_PLAN)
            _git_commit_file(repo_root, plan_path)
            subprocess.run(
                [
                    'git',
                    'worktree',
                    'add',
                    '-b',
                    'feature/terminal-integration',
                    str(worktree_path),
                    'main',
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            feature_file = worktree_path / 'feature.txt'
            feature_file.write_text('approved\n', encoding='utf-8')
            _run_git_in_test(['add', 'feature.txt'], cwd=worktree_path)
            _run_git_in_test(
                ['commit', '-m', 'approved feature'],
                cwd=worktree_path,
            )
            feature_head = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=worktree_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            wf_config = _make_worktree_wf_config(
                worktree_root=str(worktree_root),
            )

            def runner(argv, **kwargs):
                raise AssertionError(
                    'terminal integration retry must not launch a workflow harness'
                )

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=3,
                    start_step='impl',
                ),
                wf_config,
                'wt_wf',
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
                resume=ResumeContext(
                    resumed_from_run_id='failed-merge-run',
                    feature_branch='feature/terminal-integration',
                    worktree_path=worktree_path,
                    main_branch='main',
                    setup=('worktree', 'branch'),
                    teardown=('merge', 'rm_worktree'),
                    active_plan_path=plan_path,
                    interrupted_step_name='impl',
                    terminal_integration_only=True,
                ),
            )

            assert result.turns_completed == 0
            assert not worktree_path.exists()
            payload = json.loads(
                (result.run_dir / 'run.json').read_text(encoding='utf-8')
            )
            assert payload['status'] == 'completed'
            assert payload['merge_status'] == 'success'
            assert payload['resumed_from_run_id'] == 'failed-merge-run'
            assert not list((result.run_dir / 'turns').iterdir())
            assert (
                subprocess.run(
                    ['git', 'rev-parse', 'HEAD'],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                == feature_head
            )

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

    def test_scope_envelope_is_identical_across_repair_overlay_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="review", preserve_active_plan=True),
                        ),
                    ),
                    "review": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("p",),
                        go=(GoTransition(to="implement", when="NEW_PLAN_EXISTS"),),
                    ),
                },
                first_step="implement",
            )
            wf_config = WorkflowUserConfig(
                roles={"worker": "codex.worker", "reviewer": "codex.reviewer"},
                harnesses={"codex": WorkflowHarnessConfig(profiles={
                    "worker": HarnessProfileConfig(model="worker"),
                    "reviewer": HarnessProfileConfig(model="reviewer"),
                })},
                workflows={"repair": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
            )
            observations: list[tuple[object, ...]] = []

            def runner(argv, **kwargs):
                model = argv[argv.index("--model") + 1]
                if model == "worker":
                    run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                    observations.append(_scope_envelope_observation(run_dir))
                    if len(observations) == 2:
                        _write_plan(
                            repo_root / "plan-cp01-v01.md",
                            "# Repair\n\n### [x] Checkpoint 1: Repair\n"
                            "- [x] fix finding\n",
                        )
                    return subprocess.CompletedProcess(argv, 0, "worked", "")
                _write_plan(
                    repo_root / "plan-cp01-v01.md",
                    "# Repair\n\n### [ ] Checkpoint 1: Repair\n"
                    "- [ ] fix finding\n",
                )
                return subprocess.CompletedProcess(argv, 0, "rejected", "")

            with pytest.raises(WorkflowError, match="reached max turns limit"):
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=3,
                    ),
                    wf_config,
                    "repair",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            assert len(observations) == 2
            assert observations[0] == observations[1]
            assert observations[0][-1] == 1

    def test_scope_envelope_is_identical_across_inconsistent_plan_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="END", when="DONE"),),
                    ),
                },
                first_step="implement",
                retry_inconsistent_checkpoint_state=1,
            )
            wf_config = WorkflowUserConfig(
                roles={"worker": "codex.worker"},
                harnesses={"codex": WorkflowHarnessConfig(profiles={
                    "worker": HarnessProfileConfig(model="worker"),
                })},
                workflows={"retry": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
            )
            observations: list[tuple[object, ...]] = []

            def runner(argv, **kwargs):
                run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                observations.append(_scope_envelope_observation(run_dir))
                _write_plan(
                    plan_path,
                    (
                        "# Plan\n\n### [x] Checkpoint 1: First\n"
                        "- [ ] step one\n"
                        if len(observations) == 1
                        else _COMPLETE_PLAN
                    ),
                )
                return subprocess.CompletedProcess(argv, 0, "worked", "")

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=3),
                wf_config,
                "retry",
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
            )

            assert result.final_snapshot.is_complete
            assert len(observations) == 2
            assert observations[0] == observations[1]
            assert observations[0][-1] == 1
            first_turn = json.loads(
                (
                    result.run_dir / "turns" / "turn-001" / "result.json"
                ).read_text(encoding="utf-8")
            )
            assert first_turn["status"] == "retry-scheduled"

    def test_legacy_resume_is_not_backfilled_until_next_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch="main")
            worktree_path = root / "worktree"
            plan_path = repo_root / "plan.md"
            _write_plan(
                plan_path,
                "# Plan\n\n"
                "### [ ] Checkpoint 1: First\n- [ ] step one\n\n"
                "### [ ] Checkpoint 2: Second\n- [ ] step two\n",
            )
            _git_commit_file(repo_root, plan_path)
            subprocess.run(
                [
                    "git", "worktree", "add", "-b", "resume-feature",
                    str(worktree_path), "main",
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement"),
                        ),
                    ),
                },
                first_step="implement",
                setup=("worktree", "branch"),
                teardown=(),
                main_branch="main",
            )
            wf_config = WorkflowUserConfig(
                roles={"worker": "codex.worker"},
                harnesses={"codex": WorkflowHarnessConfig(profiles={
                    "worker": HarnessProfileConfig(model="worker"),
                })},
                workflows={"legacy": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
            )
            legacy_scope_id = f"{plan_path}::checkpoint-1::first"
            legacy_payload = {
                "manager_decision_number": 2,
                "semantic_stall_count": 1,
                "reviewer_rejection_count": 0,
                "implementation_attempts": {
                    legacy_scope_id: [
                        {
                            "turn_number": 1,
                            "step_name": "implement",
                            "role": "worker",
                            "team": None,
                            "selector": "codex.worker",
                            "outcome": "completed",
                            "manager_decision_number": 1,
                        },
                    ],
                },
                "active_implementation_scope": {
                    "scope_id": legacy_scope_id,
                    "original_plan_path": str(plan_path),
                    "checkpoint_index": 1,
                    "checkpoint_name": "First",
                    "opened_turn_number": 1,
                    "awaiting_review": False,
                    "carried_reviewer_rejection_count": 0,
                },
            }
            assert not any(
                key in legacy_payload["active_implementation_scope"]
                for key in (
                    "envelope_artifact_path",
                    "envelope_artifact_sha256",
                    "envelope_canonical_sha256",
                )
            )
            restored_manager_fields = manager_resume_fields(legacy_payload)
            restored_scope = restored_manager_fields["active_implementation_scope"]
            assert isinstance(restored_scope, ActiveImplementationScope)
            assert not restored_scope.has_envelope
            observations: list[object] = []

            def runner(argv, **kwargs):
                run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                payload = json.loads(
                    (run_dir / "run.json").read_text(encoding="utf-8")
                )
                scope = payload["active_implementation_scope"]
                execution_plan = Path(kwargs["cwd"]) / "plan.md"
                if not observations:
                    envelope_values = (
                        scope.get("envelope_artifact_path"),
                        scope.get("envelope_artifact_sha256"),
                        scope.get("envelope_canonical_sha256"),
                    )
                    observations.append(
                        (
                            scope["checkpoint_index"],
                            *envelope_values,
                            not all(envelope_values),
                            list((run_dir / "scopes").glob("*/envelope.json")),
                        )
                    )
                    _write_plan(
                        execution_plan,
                        "# Plan\n\n"
                        "### [x] Checkpoint 1: First\n- [x] step one\n\n"
                        "### [ ] Checkpoint 2: Second\n- [ ] step two\n",
                    )
                else:
                    observations.append(_scope_envelope_observation(run_dir))
                    _write_plan(
                        execution_plan,
                        "# Plan\n\n"
                        "### [x] Checkpoint 1: First\n- [x] step one\n\n"
                        "### [x] Checkpoint 2: Second\n- [x] step two\n",
                    )
                return subprocess.CompletedProcess(argv, 0, "worked", "")

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                wf_config,
                "legacy",
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
                resume=ResumeContext(
                    resumed_from_run_id="prior-run",
                    feature_branch="resume-feature",
                    worktree_path=worktree_path,
                    main_branch="main",
                    setup=("worktree", "branch"),
                    teardown=(),
                    interrupted_step_name="implement",
                    **restored_manager_fields,
                ),
            )

            assert result.final_snapshot.is_complete
            assert observations[0] == (1, None, None, None, True, [])
            next_scope = observations[1]
            assert isinstance(next_scope, tuple)
            assert next_scope[5:7] == (2, "Checkpoint 2: Second")
            assert next_scope[-1] == 1
            assert next_scope[4] != legacy_scope_id


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

    def test_stop_marker_precedes_scope_pressure_without_a_valid_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=(
                        'AFLOW_SCOPE_PRESSURE: oversized checkpoint\n'
                        'AFLOW_STOP: terminal owner boundary\n'
                    ),
                    stderr='',
                )

            with pytest.raises(WorkflowError) as ctx:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=5),
                    self._make_wf_config(), 'simple', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=runner,
                )
            assert 'terminal owner boundary' in str(ctx.value)
            assert 'validated immutable envelope' not in str(ctx.value)

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

    def test_resume_envelope_survives_keep_runs_pruning_before_worker(self) -> None:
        """A real keep_runs=1 resume carries validated bytes before pruning."""
        from aflow.repartition import read_envelope
        from aflow.workflow import load_scope_envelope_for_resume

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch="main")
            worktree_root = root / "worktrees"
            worktree_root.mkdir()
            plan_path = repo_root / "plan.md"
            _write_plan(
                plan_path,
                "# Plan\r\n\r\n### [ ] Checkpoint 1: Café\r\n- [ ] preserve 🎉\r\n",
            )
            original_plan_bytes = plan_path.read_bytes()
            _git_commit_file(repo_root, plan_path)
            base = _make_worktree_wf_config(
                worktree_root=str(worktree_root),
            )
            workflow_config = WorkflowUserConfig(
                aflow=base.aflow,
                roles={"worker": base.roles["architect"]},
                harnesses=base.harnesses,
                workflows={
                    "wt_wf": WorkflowConfig(
                        steps={
                            "impl": WorkflowStepConfig(
                                role="worker",
                                prompts=("p",),
                                go=(GoTransition(to="END", when="DONE"),),
                            ),
                        },
                        first_step="impl",
                        setup=("worktree", "branch"),
                        teardown=("merge", "rm_worktree"),
                        main_branch="main",
                    ),
                },
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
            )

            observed_first_worker: list[bool] = []

            def fail_runner(argv, **kwargs):
                run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                payload = json.loads((run_dir / "run.json").read_text())
                captured_scope = payload["active_implementation_scope"]
                artifact = run_dir / captured_scope["envelope_artifact_path"]
                envelope = read_envelope(artifact)
                observed_first_worker.append(
                    envelope is not None
                    and envelope.original_plan_path == "plan.md"
                    and envelope.plan_text.encode("utf-8") == original_plan_bytes
                    and captured_scope["envelope_artifact_sha256"]
                    == hashlib.sha256(artifact.read_bytes()).hexdigest()
                )
                return subprocess.CompletedProcess(argv, 1, "failed", "failed")

            with pytest.raises(WorkflowError) as failed:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=2,
                        keep_runs=1,
                    ),
                    workflow_config,
                    "wt_wf",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=fail_runner,
                )

            source_run = next((repo_root / ".aflow" / "runs").iterdir())
            assert observed_first_worker == [True]
            source_payload = json.loads((source_run / "run.json").read_text())
            fields = manager_resume_fields(source_payload)
            scope = fields["active_implementation_scope"]
            assert scope is not None and scope.has_envelope
            source_bytes = load_scope_envelope_for_resume(source_run, scope)
            assert source_bytes is not None
            observed_before_worker: list[bool] = []

            def complete_runner(argv, **kwargs):
                run_dirs = list((repo_root / ".aflow" / "runs").iterdir())
                assert len(run_dirs) == 1
                resumed_payload = json.loads((run_dirs[0] / "run.json").read_text())
                resumed_scope = resumed_payload["active_implementation_scope"]
                artifact = run_dirs[0] / resumed_scope["envelope_artifact_path"]
                observed_before_worker.append(
                    artifact.read_bytes() == source_bytes
                    and resumed_scope["envelope_artifact_sha256"]
                    == hashlib.sha256(source_bytes).hexdigest()
                )
                _write_plan(Path(kwargs["cwd"]) / "plan.md", _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, "complete", "")

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=2,
                    keep_runs=1,
                ),
                workflow_config,
                "wt_wf",
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=complete_runner,
                resume=ResumeContext(
                    resumed_from_run_id=source_run.name,
                    feature_branch=source_payload["feature_branch"],
                    worktree_path=Path(source_payload["worktree_path"]),
                    main_branch=source_payload["main_branch"],
                    setup=tuple(source_payload["lifecycle_setup"]),
                    teardown=tuple(source_payload["lifecycle_teardown"]),
                    active_implementation_scope=scope,
                    scope_envelope_bytes=source_bytes,
                ),
            )
            assert not source_run.exists()
            assert observed_before_worker == [True]
            artifact = result.run_dir / scope.envelope_artifact_path
            assert artifact.read_bytes() == source_bytes
            assert plan_path.read_bytes() != original_plan_bytes


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

    def test_manager_gates_end_and_persists_its_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={'impl': WorkflowStepConfig(
                    role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE'),),
                )},
                first_step='impl',
            )
            wf_config = WorkflowUserConfig(
                roles={
                    'architect': 'codex.default',
                    'manager_lite': 'codex.nano',
                    'manager_full': 'codex.high',
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'default': HarnessProfileConfig(model='default'),
                    'nano': HarnessProfileConfig(model='nano'),
                    'high': HarnessProfileConfig(model='high'),
                })},
                workflows={'managed': workflow},
                prompts={'p': 'Work from {ACTIVE_PLAN_PATH}.'},
                manager=ManagerConfig(enabled=True, lite_role='manager_lite', full_role='manager_full'),
            )
            calls = 0

            def runner(argv, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    return subprocess.CompletedProcess(argv, 0, 'work complete', '')
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'schema_version': 1,
                    'action': 'continue',
                    'reason': 'The completed plan permits END.',
                    'next_step_notes': [],
                    'stop_report': None,
                }), '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                wf_config, 'managed', config_dir=repo_root,
                adapter=CodexAdapter(), runner=runner,
            )
            assert result.turns_completed == 1
            decision_dir = result.run_dir / 'manager' / 'decision-001'
            assert (decision_dir / 'context.json').is_file()
            assert (decision_dir / 'boundary.json').is_file()
            boundary_payload = json.loads(
                (decision_dir / 'boundary.json').read_text(encoding='utf-8')
            )
            assert boundary_payload['boundary']['context_schema_version'] == 3
            assert boundary_payload['boundary']['captured_plan_state'] == json.loads(
                (decision_dir / 'context.json').read_text(encoding='utf-8')
            )['plan_state']
            result_payload = json.loads((decision_dir / 'result.json').read_text(encoding='utf-8'))
            assert result_payload['action'] == 'continue'
            assert result_payload['status'] == 'accepted'

            from aflow.api import AnalyzeRequest, analyze_runs
            _write_plan(
                plan_path,
                "# Mutated after decision\n\n"
                "### [ ] Checkpoint 1: Later state\n- [ ] changed\n",
            )
            rebuilt = analyze_runs(AnalyzeRequest(
                repo_root=repo_root, run_id=result.run_dir.name,
                manager_context='lite', turn=1,
            ))
            stored = json.loads((decision_dir / 'context.json').read_text(encoding='utf-8'))
            assert rebuilt == stored

    def test_manager_chains_worker_upgrades_within_one_review_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            original_plan_text = (
                "# Plan\n\n"
                "### [ ] Checkpoint 1: First\n- [ ] step one\n\n"
                "### [ ] Checkpoint 2: Second\n- [ ] step two\n"
            )
            _write_plan(plan_path, original_plan_text)
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="review", preserve_active_plan=True),
                        ),
                    ),
                    "review": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement"),
                        ),
                    ),
                },
                first_step="implement",
            )
            wf_config = WorkflowUserConfig(
                roles={
                    "worker": "codex.worker-default",
                    "reviewer": "codex.reviewer-default",
                    "manager_lite": "codex.manager-lite",
                    "manager_full": "codex.manager-full",
                },
                harnesses={"codex": WorkflowHarnessConfig(profiles={
                    "worker-default": HarnessProfileConfig(model="worker-default"),
                    "worker-high": HarnessProfileConfig(model="worker-high"),
                    "worker-max": HarnessProfileConfig(model="worker-max"),
                    "reviewer-default": HarnessProfileConfig(model="reviewer-default"),
                    "reviewer-high": HarnessProfileConfig(model="reviewer-high"),
                    "reviewer-max": HarnessProfileConfig(model="reviewer-max"),
                    "manager-lite": HarnessProfileConfig(model="manager-lite"),
                    "manager-full": HarnessProfileConfig(model="manager-full"),
                })},
                teams={
                    "default": TeamConfig(
                        roles={"worker": "codex.worker-default"},
                        upgrade_to="high",
                    ),
                    "high": TeamConfig(
                        roles={
                            "worker": "codex.worker-high",
                            "reviewer": "codex.reviewer-high",
                        },
                        upgrade_to="max",
                    ),
                    "max": TeamConfig(roles={
                        "worker": "codex.worker-max",
                        "reviewer": "codex.reviewer-max",
                    }),
                },
                workflows={"managed": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
                manager=ManagerConfig(
                    enabled=True,
                    lite_role="manager_lite",
                    full_role="manager_full",
                    full_after_stalled_turns=99,
                ),
            )
            workflow_models: list[str] = []
            worker_envelopes: list[tuple[str, tuple[object, ...]]] = []
            manager_calls = 0
            reviewer_calls = 0

            def runner(argv, **kwargs):
                nonlocal manager_calls, reviewer_calls
                model = argv[argv.index("--model") + 1]
                if model.startswith("manager-"):
                    manager_calls += 1
                    action = (
                        "upgrade_next_implementation"
                        if manager_calls in {2, 4}
                        else "continue"
                    )
                    notes = (
                        [f"CP repair note {index}." for index in range(9)]
                        if manager_calls == 2
                        else []
                    )
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "schema_version": 1,
                        "action": action,
                        "reason": "Synthetic manager decision.",
                        "next_step_notes": notes,
                        "stop_report": None,
                    }), "")

                workflow_models.append(model)
                if model.startswith("worker-"):
                    run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                    worker_envelopes.append(
                        (model, _scope_envelope_observation(run_dir))
                    )
                if model == "worker-default":
                    if workflow_models.count("worker-default") == 1:
                        _write_plan(
                            plan_path,
                            "# Plan\n\n"
                            "### [x] Checkpoint 1: First\n- [x] step one\n\n"
                            "### [ ] Checkpoint 2: Second\n- [ ] step two\n",
                        )
                    else:
                        _write_plan(
                            plan_path,
                            "# Plan\n\n"
                            "### [x] Checkpoint 1: First\n- [x] step one\n\n"
                            "### [x] Checkpoint 2: Second\n- [x] step two\n",
                        )
                elif model == "reviewer-default":
                    reviewer_calls += 1
                    if reviewer_calls <= 3:
                        repair = repo_root / f"plan-repair-{reviewer_calls}.md"
                        _write_plan(
                            repair,
                            "# Repair\n\n"
                            f"### [ ] Checkpoint 1: Repair {reviewer_calls}\n"
                            "- [ ] repair step\n",
                        )
                elif (
                    model == "worker-max"
                    and workflow_models.count("worker-max") == 2
                ):
                    _write_plan(
                        repo_root / "plan-repair-3.md",
                        "# Repair\n\n### [x] Checkpoint 1: Repair 3\n- [x] repair step\n",
                    )
                return subprocess.CompletedProcess(argv, 0, "synthetic workflow result", "")

            result = run_workflow(
                ControllerConfig(
                    repo_root=repo_root,
                    plan_path=plan_path,
                    max_turns=9,
                    team="default",
                ),
                wf_config,
                "managed",
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
            )

            assert workflow_models == [
                "worker-default",
                "reviewer-default",
                "worker-high",
                "reviewer-default",
                "worker-max",
                "reviewer-default",
                "worker-max",
                "reviewer-default",
                "worker-default",
            ]
            assert [model for model, _ in worker_envelopes[:4]] == [
                "worker-default",
                "worker-high",
                "worker-max",
                "worker-max",
            ]
            assert len({observation for _, observation in worker_envelopes[:4]}) == 1
            assert worker_envelopes[0][1][-1] == 1
            assert worker_envelopes[4][1][4] != worker_envelopes[0][1][4]
            second = json.loads(
                (result.run_dir / "manager" / "decision-002" / "context.json").read_text()
            )
            fourth = json.loads(
                (result.run_dir / "manager" / "decision-004" / "context.json").read_text()
            )
            sixth = json.loads(
                (result.run_dir / "manager" / "decision-006" / "context.json").read_text()
            )
            eighth = json.loads(
                (result.run_dir / "manager" / "decision-008" / "context.json").read_text()
            )
            assert second["controller_state"]["eligible_upgrade"]["source_team"] == "default"
            assert second["controller_state"]["eligible_upgrade"]["target_team"] == "high"
            assert fourth["controller_state"]["eligible_upgrade"]["source_team"] == "high"
            assert fourth["controller_state"]["eligible_upgrade"]["target_team"] == "max"
            fourth_result = json.loads(
                (result.run_dir / "manager" / "decision-004" / "result.json").read_text()
            )
            second_result = json.loads(
                (result.run_dir / "manager" / "decision-002" / "result.json").read_text()
            )
            assert manager_calls == 9
            assert second_result["level"] == "lite"
            assert second_result["status"] == "accepted"
            assert len(second_result["next_step_notes"]) == 8
            assert fourth_result["level"] == "full"
            assert fourth["schema_version"] == 2
            envelope = fourth["envelope"]
            assert envelope["validated"] is True
            assert envelope["available"] is True
            assert envelope["plan_text"] == original_plan_text
            assert envelope["plan_sha256"] == hashlib.sha256(
                original_plan_text.encode("utf-8")
            ).hexdigest()
            assert envelope["checkpoint_text"] == (
                "### [ ] Checkpoint 1: First\n- [ ] step one\n\n"
            )
            assert envelope["checkpoint_line_start"] == 3
            assert envelope["checkpoint_line_end"] == 6
            assert envelope["checkpoint_byte_start"] < envelope["checkpoint_byte_end"]
            assert envelope["heading_prefix"] == "### [ ] Checkpoint 1: First\n"
            assert envelope["source_blocks"]
            assert fourth["original_plan_content"] == original_plan_text
            first_rejection = json.loads(
                (result.run_dir / "turns" / "turn-002" / "result.json").read_text()
            )["review_rejection"]
            second_rejection = json.loads(
                (result.run_dir / "turns" / "turn-004" / "result.json").read_text()
            )["review_rejection"]
            assert first_rejection["rejection_number"] == 1
            assert second_rejection["rejection_number"] == 2
            assert second_rejection["reviewed_worker_team"] == "high"
            third_rejection = json.loads(
                (result.run_dir / "turns" / "turn-006" / "result.json").read_text()
            )["review_rejection"]
            assert third_rejection["rejection_number"] == 3
            assert third_rejection["reviewed_worker_team"] == "max"
            assert fourth["controller_state"]["reviewer_rejection_count"] == 2
            assert fourth["controller_state"]["active_implementation_scope"]["attempt_teams"] == [
                "default",
                "high",
            ]
            assert fourth["controller_state"]["active_implementation_scope"]["upgrade_depth"] == 1
            assert [
                rejection["rejection_number"]
                for rejection in fourth["active_scope_rejection_ledger"]
            ] == [1, 2]
            latest_rejection = fourth["controller_state"]["latest_full_rejection"]
            assert latest_rejection["rejection_number"] == 2
            assert latest_rejection["exact_reviewer_output"] == "synthetic workflow result"
            assert [
                attempt["turn_number"]
                for attempt in fourth["implementation_attempts"]["attempts"]
            ] == [1, 3]
            assert [
                decision["decision_number"]
                for decision in fourth["manager_decisions"]
            ] == [1, 2, 3]
            assert "upgrade_next_implementation" not in sixth["controller_state"]["eligible_actions"]
            assert sixth["controller_state"]["eligible_upgrade"]["available"] is False
            assert (
                sixth["controller_state"]["eligible_upgrade"]["reason"]
                == "source team does not configure upgrade_to"
            )
            assert (
                eighth["controller_state"]["eligible_upgrade"]["reason"]
                == "next worker has no prior attempt in an active implementation scope"
            )

            run_json = json.loads((result.run_dir / "run.json").read_text())
            histories = list(run_json["implementation_attempts"].values())
            assert [attempt["team"] for attempt in histories[0]] == [
                "default",
                "high",
                "max",
                "max",
            ]
            assert run_json["active_implementation_scope"] is None
            assert run_json["pending_manager_notes"] is None
            assert run_json["pending_step_team_override"] is None
            assert run_json["pending_boundary_decision"]["scope_id"] is None

            fourth_context_path = result.run_dir / "manager" / "decision-004" / "context.json"
            stored_fourth_context = fourth_context_path.read_text(encoding="utf-8")
            _write_plan(
                plan_path,
                "# Mutated after selector-3 capture\n\n"
                "### [ ] Checkpoint 1: Later\n- [ ] changed\n",
            )
            run_json["review_rejection_history"] = []
            run_json["implementation_attempts"] = {}
            run_json["manager_history"] = []
            (result.run_dir / "run.json").write_text(
                json.dumps(run_json, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            later_result_path = result.run_dir / "manager" / "decision-009" / "result.json"
            later_result = json.loads(later_result_path.read_text(encoding="utf-8"))
            later_result["reason"] = "mutated later manager history"
            later_result_path.write_text(
                json.dumps(later_result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            rebuilt = analyze_runs(AnalyzeRequest(
                repo_root=repo_root,
                run_id=result.run_dir.name,
                manager_context="full",
                turn=4,
            ))
            assert json.dumps(rebuilt, indent=2, sort_keys=True) + "\n" == stored_fourth_context

    def test_scope_pressure_without_active_scope_fails_before_manager_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            manager_calls = 0

            def runner(argv, **kwargs):
                nonlocal manager_calls
                model = argv[argv.index("--model") + 1]
                if model.startswith("manager-"):
                    manager_calls += 1
                return subprocess.CompletedProcess(
                    argv, 0, "AFLOW_SCOPE_PRESSURE: no active scope", ""
                )

            with pytest.raises(WorkflowError, match="no active implementation scope is open"):
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                    _pressure_workflow_config(role="reviewer"),
                    "managed",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            run_dir = next((repo_root / ".aflow" / "runs").iterdir())
            assert manager_calls == 0
            assert not list((run_dir / "manager").glob("decision-*"))

    def test_scope_pressure_with_disabled_manager_fails_before_rerouting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0, "AFLOW_SCOPE_PRESSURE: manager required", ""
                )

            with pytest.raises(WorkflowError, match="manager supervision is disabled"):
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                    _make_simple_wf_config(),
                    "simple",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            run_dir = next((repo_root / ".aflow" / "runs").iterdir())
            assert not list((run_dir / "manager").glob("decision-*"))

    def test_scope_pressure_with_valid_envelope_forces_full_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            worker_calls = 0

            def runner(argv, **kwargs):
                nonlocal worker_calls
                model = argv[argv.index("--model") + 1]
                if model.startswith("manager-"):
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "schema_version": 1,
                        "action": "continue",
                        "reason": "Pressure requires a Full decision.",
                        "next_step_notes": [],
                        "stop_report": None,
                    }), "")
                worker_calls += 1
                if worker_calls == 1:
                    return subprocess.CompletedProcess(
                        argv, 0, "AFLOW_SCOPE_PRESSURE: scope needs Full review", ""
                    )
                _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, "completed", "")

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                _pressure_workflow_config(role="worker"),
                "managed",
                config_dir=repo_root,
                adapter=CodexAdapter(),
                runner=runner,
            )

            first_result = json.loads(
                (result.run_dir / "manager" / "decision-001" / "result.json").read_text()
            )
            first_context = json.loads(
                (result.run_dir / "manager" / "decision-001" / "context.json").read_text()
            )
            assert first_result["level"] == "full"
            assert first_context["controller_state"]["scope_pressure_reason"] == (
                "scope needs Full review"
            )
            assert first_context["envelope"]["validated"] is True

    def test_repartition_full_cycle_applies_routes_review_and_resets_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            call_kinds: list[str] = []
            proposal_calls = 0
            validation_calls = 0
            worker_calls = 0
            decision_calls = 0
            boundary_observations: list[str] = []
            parent_scope_ids: list[str] = []
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="review"),
                        ),
                    ),
                    "review": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="implement"),
                        ),
                    ),
                },
                first_step="implement",
                team="base",
            )
            workflow_config = WorkflowUserConfig(
                roles={
                    "worker": "codex.default",
                    "reviewer": "codex.reviewer",
                    "manager_lite": "codex.manager-lite",
                    "manager_full": "codex.manager-full",
                },
                teams={
                    "base": TeamConfig(roles={
                        "worker": "codex.default",
                        "reviewer": "codex.reviewer",
                    }),
                },
                harnesses={"codex": WorkflowHarnessConfig(profiles={
                    "default": HarnessProfileConfig(model="default"),
                    "reviewer": HarnessProfileConfig(model="reviewer"),
                    "manager-lite": HarnessProfileConfig(model="manager-lite"),
                    "manager-full": HarnessProfileConfig(model="manager-full"),
                })},
                workflows={"managed": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
                manager=ManagerConfig(
                    enabled=True,
                    lite_role="manager_lite",
                    full_role="manager_full",
                ),
            )

            def runner(argv, **kwargs):
                nonlocal proposal_calls, validation_calls, worker_calls, decision_calls
                prompt = argv[-1]
                model = argv[argv.index("--model") + 1]
                if model == "default":
                    call_kinds.append("worker")
                    worker_calls += 1
                    if worker_calls == 2:
                        run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                        live = json.loads((run_dir / "run.json").read_text())
                        next_scope = live["active_implementation_scope"]
                        assert next_scope["checkpoint_index"] == 2
                        assert next_scope["scope_id"] != parent_scope_ids[0]
                        assert next_scope["current_partition_id"] is None
                        text = plan_path.read_text(encoding="utf-8")
                        text = text.replace(
                            "### [ ] Checkpoint 1: First / Partition 2/2: Revised part 2",
                            "### [x] Checkpoint 1: First / Partition 2/2: Revised part 2",
                            1,
                        ).replace("- [ ] Implement part 2.", "- [x] Implement part 2.", 1)
                        plan_path.write_text(text, encoding="utf-8")
                        return subprocess.CompletedProcess(argv, 0, "second child complete", "")
                    return subprocess.CompletedProcess(
                        argv, 0,
                        "AFLOW_SCOPE_PRESSURE: split this checkpoint", "",
                    )
                if model == "reviewer":
                    call_kinds.append("reviewer")
                    run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                    live = json.loads((run_dir / "run.json").read_text())
                    assert live["pending_repartition"] is None
                    assert live["pending_boundary_decision"]["consumed"] is True
                    parent_scope = live["active_implementation_scope"]
                    assert parent_scope["checkpoint_index"] == 1
                    assert parent_scope["current_partition_id"]
                    parent_scope_ids.append(parent_scope["scope_id"])
                    assert json.loads(
                        (run_dir / "turns" / "turn-002" / "result.json").read_text()
                    )["status"] == "starting"
                    boundary_observations.append("consumed-after-starting")
                    text = plan_path.read_text(encoding="utf-8")
                    text = text.replace(
                        "### [ ] Checkpoint 1: First / Partition 1/2: Revised part 1",
                        "### [x] Checkpoint 1: First / Partition 1/2: Revised part 1",
                        1,
                    ).replace("- [ ] Implement part 1.", "- [x] Implement part 1.", 1)
                    plan_path.write_text(text, encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 0, "first child approved", "")
                if "REPARTITION_PROPOSE_CONTEXT_JSON:\n" in prompt:
                    call_kinds.append("propose")
                    proposal_calls += 1
                    payload = json.loads(
                        prompt.split("REPARTITION_PROPOSE_CONTEXT_JSON:\n", 1)[1]
                    )
                    if proposal_calls == 2:
                        assert payload["correction_findings"] == [
                            "Make the seam between children explicit."
                        ]
                    envelope = payload["envelope"]
                    source_ids = [
                        block["block_id"] for block in envelope["source_blocks"]
                    ]
                    repair_ids = [
                        block["block_id"]
                        for block in payload["repair_evidence_blocks"]
                    ]
                    children = []
                    for ordinal in (1, 2):
                        children.append({
                            "title": (
                                f"Revised part {ordinal}"
                                if proposal_calls == 2
                                else f"Part {ordinal}"
                            ),
                            "narrow_goal": f"Implement part {ordinal}.",
                            "source_block_ids": source_ids,
                            "repair_evidence_ids": repair_ids,
                            "implementation_steps": [f"Implement part {ordinal}."],
                            "verification_commands": ["uv run pytest -q"],
                            "done_criteria": [f"Part {ordinal} is observable."],
                        })
                    proposal = {
                        "schema_version": 1,
                        "envelope_sha256": envelope[
                            "canonical_envelope_sha256"
                        ],
                        "source_plan_sha256": payload["source_plan_sha256"],
                        "rationale": "Two independently reviewable slices.",
                        "children": children,
                        "current_disposition": "review_current_partition",
                        "cross_cutting_source_reasons": {
                            block_id: "The obligation constrains both slices."
                            for block_id in source_ids
                        },
                    }
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps(proposal), "",
                    )
                if "REPARTITION_VALIDATE_CONTEXT_JSON:\n" in prompt:
                    call_kinds.append("validate")
                    validation_calls += 1
                    payload = json.loads(
                        prompt.split("REPARTITION_VALIDATE_CONTEXT_JSON:\n", 1)[1]
                    )
                    if validation_calls == 1:
                        return subprocess.CompletedProcess(argv, 0, json.dumps({
                            "schema_version": 1,
                            "proposal_sha256": payload["proposal_sha256"],
                            "candidate_sha256": payload["candidate_plan_sha256"],
                            "verdict": "reject",
                            "reason": "The child seam is unclear.",
                            "findings": [
                                "Make the seam between children explicit."
                            ],
                        }), "")
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "schema_version": 1,
                        "proposal_sha256": payload["proposal_sha256"],
                        "candidate_sha256": payload["candidate_plan_sha256"],
                        "verdict": "accept",
                        "reason": "The split preserves all exact obligations.",
                        "findings": [],
                    }), "")
                call_kinds.append("decision")
                decision_calls += 1
                action = (
                    "repartition_current_checkpoint"
                    if decision_calls == 1
                    else "continue"
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "schema_version": 1,
                    "action": action,
                    "reason": "The scope has two independently reviewable slices.",
                    "next_step_notes": [],
                    "stop_report": None,
                }), "")

            from aflow.runlog import write_turn_artifacts_start as actual_start

            def observe_start(*args, **kwargs):
                if kwargs["turn_number"] == 2:
                    run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                    live = json.loads((run_dir / "run.json").read_text())
                    assert live["pending_repartition"]["stage"] == "applied"
                    assert live["pending_boundary_decision"]["consumed"] is False
                    boundary_observations.append("pending-before-starting")
                return actual_start(*args, **kwargs)

            with patch(
                "aflow.workflow.write_turn_artifacts_start",
                side_effect=observe_start,
            ):
                result = run_workflow(
                    ControllerConfig(
                        repo_root=repo_root, plan_path=plan_path, max_turns=3,
                    ),
                    workflow_config,
                    "managed",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            run_dir = next((repo_root / ".aflow" / "runs").iterdir())
            payload = json.loads((run_dir / "run.json").read_text())
            first_attempt = (
                run_dir / "manager" / "decision-001"
                / "repartition" / "attempt-001"
            )
            attempt = (
                run_dir / "manager" / "decision-001"
                / "repartition" / "attempt-002"
            )
            assert call_kinds == [
                "worker", "decision", "propose", "validate",
                "propose", "validate", "reviewer", "decision", "worker",
                "decision",
            ]
            assert result.final_snapshot.is_complete
            assert payload["turns_completed"] == 3
            assert payload["pending_repartition"] is None
            assert boundary_observations == [
                "pending-before-starting", "consumed-after-starting",
            ]
            assert json.loads(
                (first_attempt / "result.json").read_text()
            )["status"] == "rejected"
            assert hashlib.sha256(
                (attempt / "candidate-plan.md").read_bytes()
            ).hexdigest() != hashlib.sha256(_VALID_PLAN.encode()).hexdigest()
            assert json.loads(
                (attempt / "result.json").read_text()
            )["status"] == "accepted"
            assert "Partition 1/2: Revised part 1" in plan_path.read_text(
                encoding="utf-8"
            )

    def test_repartition_implement_current_partition_retains_upgraded_worker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            call_kinds: list[str] = []
            decision_calls = 0
            high_worker_calls = 0
            boundary_observations: list[str] = []
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(
                            GoTransition(to="END", when="DONE"),
                            GoTransition(to="review"),
                        ),
                    ),
                    "review": WorkflowStepConfig(
                        role="reviewer",
                        prompts=("p",),
                        go=(GoTransition(to="implement"),),
                    ),
                },
                first_step="implement",
                team="base",
            )
            workflow_config = WorkflowUserConfig(
                roles={
                    "worker": "codex.worker-default",
                    "reviewer": "codex.reviewer",
                    "manager_lite": "codex.manager-lite",
                    "manager_full": "codex.manager-full",
                },
                teams={
                    "base": TeamConfig(
                        roles={
                            "worker": "codex.worker-default",
                            "reviewer": "codex.reviewer",
                        },
                        upgrade_to="high",
                    ),
                    "high": TeamConfig(
                        roles={
                            "worker": "codex.worker-high",
                            "reviewer": "codex.reviewer",
                        },
                    ),
                },
                harnesses={"codex": WorkflowHarnessConfig(profiles={
                    "worker-default": HarnessProfileConfig(model="worker-default"),
                    "worker-high": HarnessProfileConfig(model="worker-high"),
                    "reviewer": HarnessProfileConfig(model="reviewer"),
                    "manager-lite": HarnessProfileConfig(model="manager-lite"),
                    "manager-full": HarnessProfileConfig(model="manager-full"),
                })},
                workflows={"managed": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
                manager=ManagerConfig(
                    enabled=True,
                    lite_role="manager_lite",
                    full_role="manager_full",
                ),
            )

            def runner(argv, **kwargs):
                nonlocal decision_calls, high_worker_calls
                prompt = argv[-1]
                model = argv[argv.index("--model") + 1]
                if model == "worker-default":
                    call_kinds.append("worker-default")
                    return subprocess.CompletedProcess(
                        argv, 0, "implementation ready for review", ""
                    )
                if model == "reviewer":
                    call_kinds.append("reviewer")
                    return subprocess.CompletedProcess(
                        argv, 0, "bounded repair required", ""
                    )
                if model == "worker-high":
                    call_kinds.append("worker-high")
                    high_worker_calls += 1
                    if high_worker_calls == 1:
                        return subprocess.CompletedProcess(
                            argv,
                            0,
                            "AFLOW_SCOPE_PRESSURE: split this checkpoint",
                            "",
                        )
                    run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                    live = json.loads((run_dir / "run.json").read_text())
                    assert live["team"] == "base"
                    assert live["pending_repartition"] is None
                    assert live["pending_step_team_override"] is None
                    assert live["pending_boundary_decision"]["consumed"] is True
                    boundary_observations.append("consumed-after-starting")
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    return subprocess.CompletedProcess(
                        argv, 0, "retained worker completed child", ""
                    )
                if "REPARTITION_PROPOSE_CONTEXT_JSON:\n" in prompt:
                    call_kinds.append("propose")
                    payload = json.loads(
                        prompt.split("REPARTITION_PROPOSE_CONTEXT_JSON:\n", 1)[1]
                    )
                    envelope = payload["envelope"]
                    source_ids = [
                        block["block_id"] for block in envelope["source_blocks"]
                    ]
                    repair_ids = [
                        block["block_id"]
                        for block in payload["repair_evidence_blocks"]
                    ]
                    proposal = {
                        "schema_version": 1,
                        "envelope_sha256": envelope[
                            "canonical_envelope_sha256"
                        ],
                        "source_plan_sha256": payload["source_plan_sha256"],
                        "rationale": "Two independently implementable slices.",
                        "children": [
                            {
                                "title": f"Part {ordinal}",
                                "narrow_goal": f"Implement part {ordinal}.",
                                "source_block_ids": source_ids,
                                "repair_evidence_ids": repair_ids,
                                "implementation_steps": [
                                    f"Implement part {ordinal}."
                                ],
                                "verification_commands": ["uv run pytest -q"],
                                "done_criteria": [
                                    f"Part {ordinal} is observable."
                                ],
                            }
                            for ordinal in (1, 2)
                        ],
                        "current_disposition": "implement_current_partition",
                        "cross_cutting_source_reasons": {
                            block_id: "The obligation constrains both slices."
                            for block_id in source_ids
                        },
                    }
                    return subprocess.CompletedProcess(
                        argv, 0, json.dumps(proposal), ""
                    )
                if "REPARTITION_VALIDATE_CONTEXT_JSON:\n" in prompt:
                    call_kinds.append("validate")
                    payload = json.loads(
                        prompt.split("REPARTITION_VALIDATE_CONTEXT_JSON:\n", 1)[1]
                    )
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "schema_version": 1,
                        "proposal_sha256": payload["proposal_sha256"],
                        "candidate_sha256": payload["candidate_plan_sha256"],
                        "verdict": "accept",
                        "reason": "The split preserves exact obligations.",
                        "findings": [],
                    }), "")

                call_kinds.append("decision")
                decision_calls += 1
                action = {
                    1: "continue",
                    2: "upgrade_next_implementation",
                    3: "repartition_current_checkpoint",
                }.get(decision_calls, "continue")
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "schema_version": 1,
                    "action": action,
                    "reason": "Synthetic lifecycle routing decision.",
                    "next_step_notes": [],
                    "stop_report": None,
                }), "")

            from aflow.runlog import write_turn_artifacts_start as actual_start

            def observe_start(*args, **kwargs):
                if kwargs["turn_number"] == 4:
                    run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                    live = json.loads((run_dir / "run.json").read_text())
                    assert live["pending_repartition"]["stage"] == "applied"
                    assert live["pending_boundary_decision"]["consumed"] is False
                    override = live["pending_step_team_override"]
                    scope = live["active_implementation_scope"]
                    assert override["target_team"] == "high"
                    assert override["selector"] == "codex.worker-high"
                    assert (
                        override["repartition_generation_id"],
                        override["repartition_candidate_sha256"],
                        override["repartition_partition_id"],
                    ) == (
                        scope["current_partition_generation_id"],
                        scope["current_partition_candidate_sha256"],
                        scope["current_partition_id"],
                    )
                    boundary_observations.append("pending-before-starting")
                return actual_start(*args, **kwargs)

            with patch(
                "aflow.workflow.write_turn_artifacts_start",
                side_effect=observe_start,
            ):
                result = run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=4,
                    ),
                    workflow_config,
                    "managed",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            payload = json.loads((result.run_dir / "run.json").read_text())
            assert result.final_snapshot.is_complete
            assert payload["pending_repartition"] is None
            assert boundary_observations == [
                "pending-before-starting",
                "consumed-after-starting",
            ]
            assert call_kinds == [
                "worker-default",
                "decision",
                "reviewer",
                "decision",
                "worker-high",
                "decision",
                "propose",
                "validate",
                "worker-high",
                "decision",
            ]

    def test_repartition_full_rejects_protected_run_artifact_mutation(self) -> None:
        for mutate_stage in ("propose", "validate"):
            with self.subTest(mutate_stage=mutate_stage):
                with tempfile.TemporaryDirectory() as tmpdir:
                    repo_root = Path(tmpdir)
                    plan_path = repo_root / "plan.md"
                    _write_plan(plan_path, _VALID_PLAN)

                    def runner(argv, **kwargs):
                        prompt = argv[-1]
                        model = argv[argv.index("--model") + 1]
                        if model == "default":
                            return subprocess.CompletedProcess(
                                argv, 0,
                                "AFLOW_SCOPE_PRESSURE: split this checkpoint", "",
                            )
                        if "REPARTITION_PROPOSE_CONTEXT_JSON:\n" in prompt:
                            payload = json.loads(
                                prompt.split(
                                    "REPARTITION_PROPOSE_CONTEXT_JSON:\n", 1,
                                )[1]
                            )
                            envelope = payload["envelope"]
                            source_ids = [
                                block["block_id"]
                                for block in envelope["source_blocks"]
                            ]
                            proposal = {
                                "schema_version": 1,
                                "envelope_sha256": envelope[
                                    "canonical_envelope_sha256"
                                ],
                                "source_plan_sha256": payload[
                                    "source_plan_sha256"
                                ],
                                "rationale": "Two reviewable slices.",
                                "children": [
                                    {
                                        "title": f"Part {ordinal}",
                                        "narrow_goal": f"Implement part {ordinal}.",
                                        "source_block_ids": source_ids,
                                        "repair_evidence_ids": [],
                                        "implementation_steps": [
                                            f"Implement part {ordinal}."
                                        ],
                                        "verification_commands": [
                                            "uv run pytest -q"
                                        ],
                                        "done_criteria": [
                                            f"Part {ordinal} is observable."
                                        ],
                                    }
                                    for ordinal in (1, 2)
                                ],
                                "current_disposition": (
                                    "implement_current_partition"
                                ),
                                "cross_cutting_source_reasons": {
                                    block_id: (
                                        "The obligation constrains both slices."
                                    )
                                    for block_id in source_ids
                                },
                            }
                            if mutate_stage == "propose":
                                run_dir = next(
                                    (repo_root / ".aflow" / "runs").iterdir()
                                )
                                source_artifact = (
                                    run_dir / "manager" / "decision-001"
                                    / "repartition" / "attempt-001"
                                    / "source-plan.md"
                                )
                                source_artifact.write_bytes(
                                    source_artifact.read_bytes() + b"tampered"
                                )
                            return subprocess.CompletedProcess(
                                argv, 0, json.dumps(proposal), "",
                            )
                        if "REPARTITION_VALIDATE_CONTEXT_JSON:\n" in prompt:
                            payload = json.loads(
                                prompt.split(
                                    "REPARTITION_VALIDATE_CONTEXT_JSON:\n", 1,
                                )[1]
                            )
                            if mutate_stage == "validate":
                                run_dir = next(
                                    (repo_root / ".aflow" / "runs").iterdir()
                                )
                                candidate_artifact = (
                                    run_dir / "manager" / "decision-001"
                                    / "repartition" / "attempt-001"
                                    / "candidate-plan.md"
                                )
                                candidate_artifact.write_bytes(
                                    candidate_artifact.read_bytes() + b"tampered"
                                )
                            verdict = {
                                "schema_version": 1,
                                "proposal_sha256": payload["proposal_sha256"],
                                "candidate_sha256": payload[
                                    "candidate_plan_sha256"
                                ],
                                "verdict": "accept",
                                "reason": "The split preserves exact obligations.",
                                "findings": [],
                            }
                            return subprocess.CompletedProcess(
                                argv, 0, json.dumps(verdict), "",
                            )
                        return subprocess.CompletedProcess(argv, 0, json.dumps({
                            "schema_version": 1,
                            "action": "repartition_current_checkpoint",
                            "reason": "The scope has two reviewable slices.",
                            "next_step_notes": [],
                            "stop_report": None,
                        }), "")

                    with pytest.raises(
                        WorkflowError, match="protected run-artifact state",
                    ):
                        run_workflow(
                            ControllerConfig(
                                repo_root=repo_root,
                                plan_path=plan_path,
                                max_turns=2,
                            ),
                            _pressure_workflow_config(role="worker"),
                            "managed",
                            config_dir=repo_root,
                            adapter=CodexAdapter(),
                            runner=runner,
                        )

                    run_dir = next(
                        (repo_root / ".aflow" / "runs").iterdir()
                    )
                    payload = json.loads(
                        (run_dir / "run.json").read_text(encoding="utf-8")
                    )
                    pending = payload["pending_repartition"]
                    result = json.loads(
                        (
                            run_dir / pending["latest_attempt_path"]
                            / "result.json"
                        ).read_text(encoding="utf-8")
                    )
                    assert pending["stage"] == "failed"
                    assert pending["failed_stage"] == mutate_stage
                    assert pending["semantic_verdict_artifact_path"] is None
                    assert result == {
                        "reason": (
                            "repartition Full call mutated repository, plan, "
                            "or protected run-artifact state"
                        ),
                        "stage": mutate_stage,
                        "status": "failed",
                    }
                    assert plan_path.read_text(encoding="utf-8") == _VALID_PLAN

    def test_scope_pressure_with_tampered_envelope_fails_before_manager_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            manager_calls = 0

            def runner(argv, **kwargs):
                nonlocal manager_calls
                model = argv[argv.index("--model") + 1]
                if model.startswith("manager-"):
                    manager_calls += 1
                    return subprocess.CompletedProcess(argv, 0, "", "")
                run_dir = next((repo_root / ".aflow" / "runs").iterdir())
                payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
                scope = payload["active_implementation_scope"]
                assert scope is not None
                (run_dir / scope["envelope_artifact_path"]).write_bytes(b"tampered")
                return subprocess.CompletedProcess(
                    argv, 0, "AFLOW_SCOPE_PRESSURE: envelope changed", ""
                )

            with pytest.raises(WorkflowError, match="artifact bytes hash mismatch"):
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                    _pressure_workflow_config(role="worker"),
                    "managed",
                    config_dir=repo_root,
                    adapter=CodexAdapter(),
                    runner=runner,
                )

            run_dir = next((repo_root / ".aflow" / "runs").iterdir())
            assert manager_calls == 0
            assert not list((run_dir / "manager").glob("decision-*"))

    def test_invalid_lite_manager_response_escalates_once_to_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={'impl': WorkflowStepConfig(
                    role='architect', prompts=('p',), go=(GoTransition(to='END', when='DONE'),),
                )}, first_step='impl',
            )
            wf_config = WorkflowUserConfig(
                roles={'architect': 'codex.default', 'manager_lite': 'codex.nano', 'manager_full': 'codex.high'},
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'default': HarnessProfileConfig(model='default'), 'nano': HarnessProfileConfig(model='nano'), 'high': HarnessProfileConfig(model='high'),
                })},
                workflows={'managed': workflow}, prompts={'p': 'Work from {ACTIVE_PLAN_PATH}.'},
                manager=ManagerConfig(enabled=True, lite_role='manager_lite', full_role='manager_full'),
            )
            calls = 0

            def runner(argv, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    return subprocess.CompletedProcess(argv, 0, 'work complete', '')
                if calls == 2:
                    return subprocess.CompletedProcess(argv, 0, 'not JSON', '')
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'schema_version': 1, 'action': 'continue', 'reason': 'Full supervision accepts END.',
                    'next_step_notes': [], 'stop_report': None,
                }), '')

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                wf_config, 'managed', config_dir=repo_root, adapter=CodexAdapter(), runner=runner,
            )
            assert (result.run_dir / 'manager' / 'decision-001' / 'result.json').is_file()
            full_result = json.loads((result.run_dir / 'manager' / 'decision-002' / 'result.json').read_text(encoding='utf-8'))
            assert full_result['level'] == 'full'
            assert full_result['action'] == 'continue'

    def test_reasonix_lite_manager_accepts_fenced_final_output_without_full_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={"impl": WorkflowStepConfig(
                    role="architect",
                    prompts=("p",),
                    go=(GoTransition(to="END", when="DONE"),),
                )},
                first_step="impl",
            )
            wf_config = WorkflowUserConfig(
                roles={
                    "architect": "codex.worker",
                    "manager_lite": "reasonix.lite",
                    "manager_full": "codex.full",
                },
                harnesses={
                    "codex": WorkflowHarnessConfig(profiles={
                        "worker": HarnessProfileConfig(model="worker"),
                        "full": HarnessProfileConfig(model="full"),
                    }),
                    "reasonix": WorkflowHarnessConfig(profiles={
                        "lite": HarnessProfileConfig(model="deepseek-flash"),
                    }),
                },
                workflows={"managed": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
                manager=ManagerConfig(
                    enabled=True,
                    lite_role="manager_lite",
                    full_role="manager_full",
                ),
            )
            calls: list[list[str]] = []

            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "reasonix":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        "```json\n" + json.dumps({
                            "schema_version": 1,
                            "action": "continue",
                            "reason": "Lite accepts the completed turn.",
                            "next_step_notes": [],
                            "stop_report": None,
                        }) + "\n```\n",
                        "",
                    )
                _write_plan(plan_path, _COMPLETE_PLAN)
                return subprocess.CompletedProcess(argv, 0, "work complete", "")

            result = run_workflow(
                ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                wf_config,
                "managed",
                config_dir=repo_root,
                runner=runner,
            )

            assert [argv[0] for argv in calls] == ["codex", "reasonix"]
            manager_argv = calls[1]
            assert "--dir" in manager_argv
            assert "--print" in manager_argv
            assert manager_argv.index("--print") < len(manager_argv) - 1
            decision = json.loads(
                (result.run_dir / "manager" / "decision-001" / "result.json").read_text()
            )
            assert decision["level"] == "lite"
            assert decision["status"] == "accepted"
            assert decision["invocation"]["argv"] == manager_argv

    def test_manager_stop_emits_report_then_stops_banner_before_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={"impl": WorkflowStepConfig(
                    role="architect",
                    prompts=("p",),
                    go=(GoTransition(to="END", when="DONE"),),
                )},
                first_step="impl",
            )
            wf_config = WorkflowUserConfig(
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
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
                manager=ManagerConfig(
                    enabled=True,
                    lite_role="manager_lite",
                    full_role="manager_full",
                ),
            )
            order: list[str] = []
            failed_events: list[object] = []

            class RecordingBanner:
                def start(self, state):
                    pass

                def update(self, state):
                    pass

                def set_context(self, **kwargs):
                    pass

                def stop(self, state):
                    order.append("banner_stopped")

            class RecordingObserver:
                def on_event(self, event):
                    if event.event_type == ExecutionEventType.RUN_FAILED:
                        order.append("failure_emitted")
                        failed_events.append(event)

            def runner(argv, **kwargs):
                model = argv[argv.index("--model") + 1]
                if model == "worker":
                    _write_plan(plan_path, _COMPLETE_PLAN)
                    return subprocess.CompletedProcess(argv, 0, "work complete", "")
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "schema_version": 1,
                    "action": "stop",
                    "reason": "Stop after the completed turn.",
                    "next_step_notes": [],
                    "stop_report": {
                        "summary": "The manager stopped the run.",
                        "root_cause": "Synthetic terminal decision.",
                        "evidence": ["The manager selected stop."],
                        "attempts": "One worker turn ran.",
                        "workspace_state": "The plan is complete.",
                        "next_actions": ["Inspect the manager artifacts."],
                    },
                }), "")

            with pytest.raises(WorkflowError) as raised:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                    wf_config,
                    "managed",
                    config_dir=repo_root,
                    runner=runner,
                    banner=RecordingBanner(),  # type: ignore[arg-type]
                    observer=RecordingObserver(),  # type: ignore[arg-type]
                )

            assert order[-2:] == ["failure_emitted", "banner_stopped"]
            assert len(failed_events) == 1
            report = str(raised.value)
            assert failed_events[0].failure_reason == report  # type: ignore[attr-defined]
            run_dir = raised.value.run_dir
            assert run_dir is not None
            assert (run_dir / "manager-report.md").read_text(encoding="utf-8") == report
            assert json.loads((run_dir / "run.json").read_text())["failure_reason"] == report

    def test_invalid_terminal_manager_response_preserves_harness_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / "plan.md"
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={
                    "implement": WorkflowStepConfig(
                        role="worker",
                        prompts=("p",),
                        go=(GoTransition(to="END", when="DONE"),),
                    ),
                },
                first_step="implement",
            )
            wf_config = WorkflowUserConfig(
                roles={
                    "worker": "reasonix.ds4lite",
                    "manager_lite": "codex.manager-lite",
                    "manager_full": "codex.manager-full",
                },
                harnesses={
                    "reasonix": WorkflowHarnessConfig(profiles={
                        "ds4lite": HarnessProfileConfig(model="deepseek-flash"),
                    }),
                    "codex": WorkflowHarnessConfig(profiles={
                        "manager-lite": HarnessProfileConfig(model="manager-lite"),
                        "manager-full": HarnessProfileConfig(model="manager-full"),
                    }),
                },
                workflows={"managed": workflow},
                prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
                manager=ManagerConfig(
                    enabled=True,
                    lite_role="manager_lite",
                    full_role="manager_full",
                ),
            )
            calls: list[str] = []

            def runner(argv, **kwargs):
                calls.append(argv[0])
                if argv[0] == "reasonix":
                    assert "--dir" in argv
                    assert "-dir" not in argv
                    return subprocess.CompletedProcess(argv, 2, "", "")
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    "schema_version": 1,
                    "action": "stop",
                    "reason": "The harness failed before implementation.",
                    "next_step_notes": "Investigate the harness failure.",
                    "stop_report": "AFLOW_STOP: harness failed.",
                }), "")

            with pytest.raises(WorkflowError) as error:
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=2,
                    ),
                    wf_config,
                    "managed",
                    config_dir=repo_root,
                    runner=runner,
                )

            report = str(error.value)
            assert calls == ["reasonix", "codex"]
            assert "## Summary\nharness 'reasonix' exited with code 2" in report
            assert "Manager decision error: next_step_notes must be an array" in report
            run_dir = error.value.run_dir
            assert run_dir is not None
            run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            assert run_json["failure_reason"] == report

    def test_same_step_cap_calls_full_once_without_a_lite_transition_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            plan_path = repo_root / 'plan.md'
            _write_plan(plan_path, _VALID_PLAN)
            workflow = WorkflowConfig(
                steps={
                    'impl': WorkflowStepConfig(
                        role='architect', prompts=('p',), go=(GoTransition(to='impl'),),
                    ),
                    'review': WorkflowStepConfig(
                        role='architect', prompts=('p',), go=(GoTransition(to='END'),),
                    ),
                },
                first_step='impl',
            )
            wf_config = WorkflowUserConfig(
                roles={
                    'architect': 'codex.default',
                    'manager_lite': 'codex.nano',
                    'manager_full': 'codex.high',
                },
                harnesses={'codex': WorkflowHarnessConfig(profiles={
                    'default': HarnessProfileConfig(model='default'),
                    'nano': HarnessProfileConfig(model='nano'),
                    'high': HarnessProfileConfig(model='high'),
                })},
                workflows={'managed': workflow}, prompts={'p': 'Work from {ACTIVE_PLAN_PATH}.'},
                aflow=AflowSection(max_same_step_turns=1),
                manager=ManagerConfig(enabled=True, lite_role='manager_lite', full_role='manager_full'),
            )

            def runner(argv, **kwargs):
                if 'high' in ' '.join(argv):
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        'schema_version': 1, 'action': 'stop', 'reason': 'Cap reached.',
                        'next_step_notes': [],
                        'stop_report': {
                            'summary': 'The same-step cap was reached.',
                            'root_cause': 'No transition progressed the workflow.',
                            'evidence': ['The controller selected impl again.'],
                            'attempts': 'One implementation turn ran.',
                            'workspace_state': 'The plan remains in progress.',
                            'next_actions': ['Inspect the manager report.'],
                        },
                    }), '')
                return subprocess.CompletedProcess(argv, 0, 'work complete', '')

            with pytest.raises(WorkflowError) as error:
                run_workflow(
                    ControllerConfig(repo_root=repo_root, plan_path=plan_path, max_turns=2),
                    wf_config, 'managed', config_dir=repo_root,
                    adapter=CodexAdapter(), runner=runner,
                )
            assert '# AFlow manager report' in str(error.value)
            run_dir = error.value.run_dir
            assert run_dir is not None
            decisions = sorted((run_dir / 'manager').iterdir())
            assert len(decisions) == 1
            result = json.loads((decisions[0] / 'result.json').read_text(encoding='utf-8'))
            assert result['level'] == 'full'
            assert result['trigger'] == 'same_step_cap'


def _run_upgrade_resume_scenario(
    tmp_path: Path,
    *,
    interruption: str,
    prior_reviewer_rejections: int = 0,
    legacy_scope: bool = False,
    pressure_on_first_reviewer: bool = False,
) -> tuple[list[str], list[int], dict[str, object], Path]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _make_lifecycle_git_repo(repo_root, branch="main")
    plan_path = repo_root / "plan.md"
    _write_plan(
        plan_path,
        "# Plan\n\n"
        "### [x] Checkpoint 1: First\n- [x] step one\n\n"
        "### [ ] Checkpoint 2: Second\n- [ ] step two\n",
    )
    _git_commit_file(repo_root, plan_path)
    worktree_path = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "resume-feature", str(worktree_path), "main"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    repair_path = repo_root / "repair.md"
    repair_text = "# Repair\n\n### [ ] Checkpoint 1: Repair\n- [ ] repair step\n"
    _write_plan(repair_path, repair_text)
    _write_plan(worktree_path / "repair.md", repair_text)

    workflow = WorkflowConfig(
        steps={
            "implement": WorkflowStepConfig(
                role="worker",
                prompts=("p",),
                go=(
                    GoTransition(to="END", when="DONE"),
                    GoTransition(to="review", preserve_active_plan=True),
                ),
            ),
            "review": WorkflowStepConfig(
                role="reviewer",
                prompts=("p",),
                go=(GoTransition(to="implement"),),
            ),
        },
        first_step="implement",
        setup=("worktree", "branch"),
        teardown=(),
        main_branch="main",
    )
    wf_config = WorkflowUserConfig(
        aflow=AflowSection(worktree_root=str(tmp_path)),
        roles={
            "worker": "codex.worker-default",
            "reviewer": "codex.reviewer-default",
            "manager_lite": "codex.manager-lite",
            "manager_full": "codex.manager-full",
        },
        harnesses={"codex": WorkflowHarnessConfig(profiles={
            "worker-default": HarnessProfileConfig(model="worker-default"),
            "worker-high": HarnessProfileConfig(model="worker-high"),
            "worker-max": HarnessProfileConfig(model="worker-max"),
            "reviewer-default": HarnessProfileConfig(model="reviewer-default"),
            "manager-lite": HarnessProfileConfig(model="manager-lite"),
            "manager-full": HarnessProfileConfig(model="manager-full"),
        })},
        teams={
            "default": TeamConfig(
                roles={"worker": "codex.worker-default"},
                upgrade_to="high",
            ),
            "high": TeamConfig(
                roles={"worker": "codex.worker-high"},
                upgrade_to="max",
            ),
            "max": TeamConfig(roles={"worker": "codex.worker-max"}),
        },
        workflows={"managed": workflow},
        prompts={"p": "Work from {ACTIVE_PLAN_PATH}."},
        manager=ManagerConfig(
            enabled=True,
            lite_role="manager_lite",
            full_role="manager_full",
            full_after_stalled_turns=99,
        ),
    )
    scope = ActiveImplementationScope(
        f"{plan_path}::checkpoint-1::first",
        str(plan_path),
        1,
        "First",
        1,
        awaiting_review=interruption != "before_worker",
    )
    attempts = [
        ImplementationAttempt(
            1, "implement", "worker", "default", "codex.worker-default", "progress"
        ),
        ImplementationAttempt(
            3, "implement", "worker", "high", "codex.worker-high", "progress", 2
        ),
    ]
    if interruption == "after_consumption":
        attempts.append(ImplementationAttempt(
            5, "implement", "worker", "max", "codex.worker-max", "progress", 3
        ))

    target_identity = f"{repair_path}::checkpoint-1"
    pending_override = None
    pending_boundary = None
    decision_number = 2 if interruption == "before_review" else 3
    if interruption == "before_worker":
        pending_override = PendingTeamOverride(
            "implement",
            "worker",
            "high",
            "max",
            "codex.worker-max",
            target_identity,
            3,
            scope_id=scope.scope_id,
            target_plan_identity=target_identity,
        )
        pending_boundary = PendingBoundaryDecision(
            finalized_turn_number=4,
            decision_number=3,
            action="upgrade_next_implementation",
            proposed_action="transition",
            proposed_transition="implement",
            resolved_next_step="implement",
            target_role="worker",
            target_team="max",
            target_selector="codex.worker-max",
            checkpoint_identity=target_identity,
            scope_id=scope.scope_id,
            target_plan_identity=target_identity,
        )
    elif interruption == "after_consumption":
        pending_boundary = PendingBoundaryDecision(
            finalized_turn_number=4,
            decision_number=3,
            action="upgrade_next_implementation",
            proposed_action="transition",
            proposed_transition="implement",
            resolved_next_step="implement",
            target_role="worker",
            target_team="max",
            target_selector="codex.worker-max",
            checkpoint_identity=target_identity,
            applied=True,
            consumed=True,
            scope_id=scope.scope_id,
            target_plan_identity=target_identity,
        )

    resume_manager_fields: dict[str, object] = {
        "manager_decision_number": decision_number,
        "reviewer_rejection_count": prior_reviewer_rejections,
        "implementation_attempts": {scope.scope_id: tuple(attempts)},
        "active_implementation_scope": scope,
        "pending_step_team_override": pending_override,
        "pending_boundary_decision": pending_boundary,
    }
    if legacy_scope:
        assert pending_override is None
        assert pending_boundary is None
        resume_manager_fields = manager_resume_fields({
            "manager_decision_number": decision_number,
            "reviewer_rejection_count": prior_reviewer_rejections,
            "implementation_attempts": {
                scope.scope_id: [
                    {
                        "turn_number": attempt.turn_number,
                        "step_name": attempt.step_name,
                        "role": attempt.role,
                        "team": attempt.team,
                        "selector": attempt.selector,
                        "outcome": attempt.outcome,
                        "manager_decision_number": attempt.manager_decision_number,
                    }
                    for attempt in attempts
                ],
            },
            "active_implementation_scope": {
                "scope_id": scope.scope_id,
                "original_plan_path": scope.original_plan_path,
                "checkpoint_index": scope.checkpoint_index,
                "checkpoint_name": scope.checkpoint_name,
                "opened_turn_number": 5,
                "awaiting_review": scope.awaiting_review,
            },
        })
    resume = ResumeContext(
        resumed_from_run_id=f"prior-{interruption}",
        feature_branch="resume-feature",
        worktree_path=worktree_path,
        main_branch="main",
        setup=("worktree", "branch"),
        teardown=(),
        active_plan_path=repair_path,
        **resume_manager_fields,
    )
    workflow_models: list[str] = []
    manager_numbers: list[int] = []
    reviewer_calls = 0

    def runner(argv, **kwargs):
        nonlocal reviewer_calls
        model = argv[argv.index("--model") + 1]
        if model.startswith("manager-"):
            prompt = argv[-1]
            context = json.loads(prompt.split("MANAGER_CONTEXT_JSON:\n", 1)[1])
            manager_numbers.append(context["decision_number"])
            action = (
                "upgrade_next_implementation"
                if interruption == "before_review" and len(manager_numbers) == 1
                else "continue"
            )
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "schema_version": 1,
                "action": action,
                "reason": "Synthetic resumed manager decision.",
                "next_step_notes": [],
                "stop_report": None,
            }), "")

        workflow_models.append(model)
        if model == "reviewer-default":
            reviewer_calls += 1
            replacement = Path(kwargs["cwd"]) / f"plan-resume-{interruption}-{reviewer_calls}.md"
            _write_plan(
                replacement,
                "# Repair\n\n### [ ] Checkpoint 1: Replacement\n- [ ] repair step\n",
            )
            if pressure_on_first_reviewer and reviewer_calls == 1:
                return subprocess.CompletedProcess(
                    argv, 0, "AFLOW_SCOPE_PRESSURE: legacy scope", ""
                )
        else:
            _write_plan(
                Path(kwargs["cwd"]) / "plan.md",
                "# Plan\n\n"
                "### [x] Checkpoint 1: First\n- [x] step one\n\n"
                "### [x] Checkpoint 2: Second\n- [x] step two\n",
            )
        return subprocess.CompletedProcess(argv, 0, "resumed workflow result", "")

    result = run_workflow(
        ControllerConfig(
            repo_root=repo_root,
            plan_path=plan_path,
            max_turns=2,
            team="default",
            start_step=(
                "implement" if interruption == "before_worker" else "review"
            ),
        ),
        wf_config,
        "managed",
        config_dir=repo_root,
        adapter=CodexAdapter(),
        runner=runner,
        resume=resume,
    )
    run_json = json.loads((result.run_dir / "run.json").read_text())
    return workflow_models, manager_numbers, run_json, result.run_dir


def test_manager_resume_after_upgraded_worker_uses_it_as_next_upgrade_source(
    tmp_path: Path,
) -> None:
    models, decisions, _, run_dir = _run_upgrade_resume_scenario(
        tmp_path, interruption="before_review"
    )
    assert models == ["reviewer-default", "worker-max"]
    assert decisions == [3, 4]
    context = json.loads(
        (run_dir / "manager" / "decision-003" / "context.json").read_text()
    )
    assert context["controller_state"]["eligible_upgrade"]["source_team"] == "high"
    assert context["controller_state"]["eligible_upgrade"]["target_team"] == "max"


def test_manager_resume_carries_scope_rejections_into_first_boundary(
    tmp_path: Path,
) -> None:
    _, _, _, run_dir = _run_upgrade_resume_scenario(
        tmp_path,
        interruption="before_review",
        prior_reviewer_rejections=2,
    )
    result = json.loads(
        (run_dir / "manager" / "decision-003" / "result.json").read_text()
    )
    context = json.loads(
        (run_dir / "manager" / "decision-003" / "context.json").read_text()
    )
    scope = context["controller_state"]["active_implementation_scope"]
    assert result["level"] == "full"
    assert context["controller_state"]["reviewer_rejection_count"] == 3
    assert scope["opened_turn_number"] == 1
    assert scope["carried_reviewer_rejection_count"] == 2


def test_manager_legacy_resume_discards_prior_checkpoint_rejections(
    tmp_path: Path,
) -> None:
    _, _, _, run_dir = _run_upgrade_resume_scenario(
        tmp_path,
        interruption="before_review",
        prior_reviewer_rejections=2,
        legacy_scope=True,
    )
    result = json.loads(
        (run_dir / "manager" / "decision-003" / "result.json").read_text()
    )
    context = json.loads(
        (run_dir / "manager" / "decision-003" / "context.json").read_text()
    )
    scope = context["controller_state"]["active_implementation_scope"]
    assert result["level"] == "lite"
    assert context["controller_state"]["reviewer_rejection_count"] == 1
    assert scope["opened_turn_number"] == 1
    assert scope["carried_reviewer_rejection_count"] == 0


def test_scope_pressure_with_legacy_resume_scope_fails_before_manager_decision(
    tmp_path: Path,
) -> None:
    with pytest.raises(WorkflowError, match="legacy scope has no artifact"):
        _run_upgrade_resume_scenario(
            tmp_path,
            interruption="before_review",
            legacy_scope=True,
            pressure_on_first_reviewer=True,
        )

    run_dir = next((tmp_path / "repo" / ".aflow" / "runs").iterdir())
    assert not list((run_dir / "manager").glob("decision-*"))


def test_manager_resume_before_stronger_worker_consumes_override_once(tmp_path: Path) -> None:
    models, decisions, run_json, _ = _run_upgrade_resume_scenario(
        tmp_path, interruption="before_worker"
    )
    assert models == ["worker-max"]
    assert decisions == [4]
    assert run_json["pending_step_team_override"] is None


def test_manager_resume_after_consumption_retains_scope_team_without_replaying_boundary(
    tmp_path: Path,
) -> None:
    models, decisions, _, run_dir = _run_upgrade_resume_scenario(
        tmp_path, interruption="after_consumption"
    )
    assert models == ["reviewer-default", "worker-max"]
    assert decisions == [4, 5]
    context = json.loads(
        (run_dir / "manager" / "decision-004" / "context.json").read_text()
    )
    assert context["controller_state"]["eligible_upgrade"]["source_team"] == "max"
    assert context["controller_state"]["eligible_upgrade"]["available"] is False

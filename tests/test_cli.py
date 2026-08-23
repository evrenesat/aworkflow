from aflow._test_support import *  # noqa: F401,F403
import hashlib
import re
from typing import Mapping
from unittest.mock import Mock
from aflow.api.models import PreparedRun
from aflow.config import ErrorHandlingConfig, ManagerConfig
from aflow.repartition import create_envelope, derive_generation_id, write_envelope_atomic
from aflow.status import BannerRenderer as RealBannerRenderer
from aflow.workflow import _freeze_run_identity


def _current_resume_payload(payload: dict[str, object]) -> dict[str, object]:
    """Complete a small fixture with the schema-v2 controller envelope."""
    payload = dict(payload)
    payload.setdefault("schema_version", 2)
    payload.setdefault("original_plan_path", payload.get("plan_path", "/fake/plan.md"))
    payload.setdefault("effective_max_turns", payload.get("max_turns", 1))
    payload.setdefault("lifecycle_setup", [])
    payload.setdefault("lifecycle_teardown", [])
    payload.setdefault(
        "frozen_config",
        {
            "workflow_name": str(payload.get("workflow_name", "test_workflow")),
            "config_path": "/fake/aflow.toml",
            "config_fingerprint": "fixture-fingerprint",
        },
    )
    payload.setdefault("manager_decision_number", 0)
    payload.setdefault("manager_history", [])
    payload.setdefault("semantic_stall_count", 0)
    payload.setdefault("reviewer_rejection_count", 0)
    payload.setdefault("implementation_attempts", {})
    payload.setdefault("active_implementation_scope", None)
    active_scope = payload.get("active_implementation_scope")
    if isinstance(active_scope, Mapping):
        active_scope = dict(active_scope)
        active_scope.setdefault("current_partition_generation_id", None)
        active_scope.setdefault("current_partition_candidate_sha256", None)
        active_scope.setdefault("current_partition_id", None)
        payload["active_implementation_scope"] = active_scope
    payload.setdefault("review_rejection_history", [])
    payload.setdefault("pending_manager_notes", None)
    pending_notes = payload.get("pending_manager_notes")
    if isinstance(pending_notes, Mapping):
        pending_notes = dict(pending_notes)
        for field in (
            "target_role",
            "target_selector",
            "checkpoint_identity",
            "scope_id",
            "target_plan_identity",
            "repartition_generation_id",
            "repartition_candidate_sha256",
            "repartition_partition_id",
        ):
            pending_notes.setdefault(field, None)
        pending_notes.setdefault("consumed", False)
        pending_notes.setdefault("correction_attempted", False)
        payload["pending_manager_notes"] = pending_notes
    payload.setdefault("pending_step_team_override", None)
    pending_override = payload.get("pending_step_team_override")
    if isinstance(pending_override, Mapping):
        pending_override = dict(pending_override)
        for field in (
            "checkpoint_identity",
            "scope_id",
            "target_plan_identity",
            "repartition_generation_id",
            "repartition_candidate_sha256",
            "repartition_partition_id",
        ):
            pending_override.setdefault(field, None)
        pending_override.setdefault("consumed", False)
        payload["pending_step_team_override"] = pending_override
    payload.setdefault("pending_boundary_decision", None)
    pending_boundary = payload.get("pending_boundary_decision")
    if isinstance(pending_boundary, Mapping):
        pending_boundary = dict(pending_boundary)
        for field in (
            "proposed_transition",
            "resolved_next_step",
            "target_role",
            "target_team",
            "target_selector",
            "checkpoint_identity",
            "post_transition_active_plan_path",
            "post_transition_checkpoint_identity",
            "notes_reference",
            "scope_id",
            "target_plan_identity",
            "repartition_generation_id",
            "repartition_candidate_sha256",
            "repartition_partition_id",
        ):
            pending_boundary.setdefault(field, None)
        pending_boundary.setdefault("applied", False)
        pending_boundary.setdefault("consumed", False)
        payload["pending_boundary_decision"] = pending_boundary
    payload.setdefault("pending_repartition", None)
    payload.setdefault("repartition_history", [])
    payload.setdefault("scope_pressure_reason", None)
    payload.setdefault("last_manager_report_path", None)
    payload.setdefault("hotplug_schema_version", 1)
    payload.setdefault("role_selectors", {})
    payload.setdefault("current_hotplug_transaction", None)
    payload.setdefault("pending_hotplug_transaction", None)
    payload.setdefault("active_role_sessions", [])
    payload.setdefault("hotplug_transaction_number", 0)
    payload.setdefault("hotplug_history", [])
    return payload


def _complete_pending_repartition_fixture(
    run_dir: Path,
    *,
    write_artifacts: bool = True,
    scope: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, bytes]]:
    attempt_rel = "manager/decision-001/repartition/attempt-001"
    prefix = f"{attempt_rel}/"
    artifacts = {
        f"{prefix}proposal.json": b"proposal-artifact\n",
        f"{prefix}candidate-plan.md": b"# candidate\n",
        f"{prefix}mechanical-validation.json": b"mechanical-artifact\n",
        f"{prefix}semantic-verdict.json": b"semantic-artifact\n",
    }
    if write_artifacts:
        for relative, content in artifacts.items():
            artifact_path = run_dir / relative
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(content)
    proposal_path = f"{prefix}proposal.json"
    candidate_path = f"{prefix}candidate-plan.md"
    pending = {
        "schema_version": 1,
        "decision_number": 1,
        "scope_id": (
            str(scope["scope_id"]) if scope is not None else "saved-scope"
        ),
        "stage": "semantically_validated",
        "envelope_sha256": (
            str(scope["envelope_canonical_sha256"])
            if scope is not None
            else "e" * 64
        ),
        "source_plan_sha256": (
            str(scope["source_plan_sha256"])
            if scope is not None
            else "s" * 64
        ),
        "attempt_count": 1,
        "generation_id": (
            derive_generation_id(
                scope_id=str(scope["scope_id"]),
                decision_number=1,
                envelope_sha256=str(scope["envelope_canonical_sha256"]),
                source_plan_sha256=str(scope["source_plan_sha256"]),
            )
            if scope is not None
            else "generation-1"
        ),
        "partition_ids": ["partition-1"],
        "child_summaries": ["Child 1: narrow goal"],
        "proposal_sha256": hashlib.sha256(artifacts[proposal_path]).hexdigest(),
        "candidate_plan_sha256": hashlib.sha256(artifacts[candidate_path]).hexdigest(),
        "current_disposition": "implement_current_partition",
        "resolved_target_step": "implement_plan",
        "resolved_target_role": "worker",
        "latest_attempt_path": attempt_rel,
        "proposal_artifact_path": proposal_path,
        "candidate_artifact_path": candidate_path,
        "mechanical_validation_artifact_path": (
            f"{prefix}mechanical-validation.json"
        ),
        "semantic_verdict_artifact_path": f"{prefix}semantic-verdict.json",
        "failed_stage": None,
        "failure_reason": None,
    }
    return pending, artifacts


def _pending_scope_fixture(
    run_dir: Path,
    original_plan_path: Path,
) -> dict[str, object]:
    scope_id = f"{original_plan_path}::checkpoint-1::first"
    plan_text = original_plan_path.read_text(encoding="utf-8")
    envelope = create_envelope(
        scope_id=scope_id,
        original_plan_path=original_plan_path,
        plan_text=plan_text,
        checkpoint_index=1,
        repo_root=original_plan_path.parent.parent.parent,
    )
    artifact_path = write_envelope_atomic(
        envelope,
        run_dir / "scopes" / envelope.scope_digest,
    )
    artifact_bytes = artifact_path.read_bytes()
    return {
        "scope_id": scope_id,
        "original_plan_path": str(original_plan_path),
        "checkpoint_index": 1,
        "checkpoint_name": "Checkpoint 1: First",
        "opened_turn_number": 1,
        "awaiting_review": False,
        "carried_reviewer_rejection_count": 0,
        "envelope_artifact_path": artifact_path.resolve()
        .relative_to(run_dir.resolve())
        .as_posix(),
        "envelope_artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "envelope_canonical_sha256": envelope.canonical_envelope_sha256,
        "source_plan_sha256": hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
        "current_partition_generation_id": None,
        "current_partition_candidate_sha256": None,
        "current_partition_id": None,
    }


def _bound_pending_repartition_fixture(
    run_dir: Path,
    original_plan_path: Path,
) -> tuple[dict[str, object], dict[str, bytes], dict[str, object]]:
    scope = _pending_scope_fixture(run_dir, original_plan_path)
    pending, artifacts = _complete_pending_repartition_fixture(
        run_dir,
        scope=scope,
    )
    return pending, artifacts, scope


def test_cli_analysis_adds_safe_controller_state_without_notes(
    tmp_path: Path,
) -> None:
    import aflow.cli as cli_module

    run_dir = tmp_path / ".aflow" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
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
    assert controller["schema_version"] == 2
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
    assert len(decisions) == 2
    lite_result = json.loads((decisions[0] / "result.json").read_text(encoding="utf-8"))
    full_result = json.loads((decisions[1] / "result.json").read_text(encoding="utf-8"))
    assert lite_result["level"] == "lite"
    assert lite_result["status"] == "invalid"
    assert full_result["level"] == "full"
    assert full_result["trigger"] == "lite_invalid"
    if not invalid_managers:
        assert "action 'stop' is not eligible" in lite_result["error"]
        assert full_result["status"] == "accepted"
        assert full_result["action"] == "stop"

class WorkflowCliTests(unittest.TestCase):

    def _new_temp_path(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def _resume_bootstrap_fixture(self, tmp_path: Path) -> tuple[Path, Path, object, dict[str, object]]:
        repo_root = tmp_path.resolve()
        plan_path = repo_root / "plans" / "in-progress" / "saved-plan.md"
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text(_VALID_PLAN, encoding="utf-8")
        step = WorkflowStepConfig(
            role="worker",
            prompts=("p",),
            go=(GoTransition(to="END", when="DONE"),),
        )
        workflow_spec = WorkflowConfig(
            declared_steps={"implement_plan": step},
            steps={"implement_plan": step},
            first_step="implement_plan",
            team="base",
            setup=("worktree", "branch"),
            teardown=("merge", "rm_worktree"),
        )
        workflow_config = type(
            "WorkflowConfig",
            (),
            {
                "workflows": {"saved_workflow": workflow_spec},
                "aflow": AflowSection(default_workflow="saved_workflow"),
                "harnesses": {
                    "codex": WorkflowHarnessConfig(
                        profiles={"worker": HarnessProfileConfig(model="worker")}
                    )
                },
                "roles": {"worker": "codex.worker"},
                "teams": {
                    "base": TeamConfig(roles={"worker": "codex.worker"})
                },
                "manager": ManagerConfig(),
                "error_handling": ErrorHandlingConfig(),
            },
        )()
        run_dir = repo_root / ".aflow" / "runs" / "saved-run"
        config_path = repo_root / "aflow.toml"
        config_path.write_text("", encoding="utf-8")
        identity = _freeze_run_identity(
            "saved_workflow",
            workflow_config,
            config_dir=config_path,
        )
        prev_run = {
            "schema_version": 2,
            "repo_root": str(repo_root),
            "workflow_name": "saved_workflow",
            "plan_path": str(plan_path),
            "original_plan_path": str(plan_path),
            "team": "base",
            "selected_start_step": "implement_plan",
            "max_turns": 15,
            "effective_max_turns": 15,
            "extra_instructions": ["keep the patch focused"],
            "lifecycle_setup": ["worktree", "branch"],
            "lifecycle_teardown": ["merge", "rm_worktree"],
            "feature_branch": "feature/saved-run",
            "worktree_path": str(repo_root / "worktree"),
            "main_branch": "main",
            "status": "failed",
            "last_snapshot": {"is_complete": False},
            "frozen_config": {
                "workflow_name": identity.workflow_name,
                "config_path": identity.config_path,
                "config_fingerprint": identity.config_fingerprint,
            },
            "manager_decision_number": 0,
            "manager_history": [],
            "semantic_stall_count": 0,
            "reviewer_rejection_count": 0,
            "implementation_attempts": {},
            "active_implementation_scope": None,
            "review_rejection_history": [],
            "pending_manager_notes": None,
            "pending_step_team_override": None,
            "pending_boundary_decision": None,
            "pending_repartition": None,
            "repartition_history": [],
            "scope_pressure_reason": None,
            "last_manager_report_path": None,
            "hotplug_schema_version": 1,
            "role_selectors": {},
            "current_hotplug_transaction": None,
            "pending_hotplug_transaction": None,
            "active_role_sessions": [],
            "hotplug_transaction_number": 0,
            "hotplug_history": [],
        }
        return repo_root, run_dir, workflow_config, prev_run

    def _modern_resume_fixture(
        self,
        tmp_path: Path,
    ) -> tuple[Path, Path, Path, object, dict[str, object]]:
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(
            tmp_path
        )
        config_path = repo_root / "aflow.toml"
        config_path.write_text("", encoding="utf-8")
        identity = _freeze_run_identity(
            "saved_workflow",
            workflow_config,
            config_dir=config_path,
        )
        modern_run = dict(prev_run)
        modern_run.update(
            {
                "schema_version": 2,
                "frozen_config": {
                    "workflow_name": identity.workflow_name,
                    "config_path": identity.config_path,
                    "config_fingerprint": identity.config_fingerprint,
                },
            }
        )
        return repo_root, run_dir, config_path, workflow_config, modern_run

    def _invoke_resume_command(
        self,
        tmp_path: Path,
        prev_run: dict[str, object],
        run_dir: Path,
        workflow_config: object,
        *,
        reset_scope: bool = False,
        auto: bool = False,
        startup_result: PreparedRun | None = None,
    ) -> tuple[int, str, Mock, Mock, Mock, Mock, object | None]:
        import aflow.cli as cli_module

        repo_root = tmp_path.resolve()
        config_path = repo_root / "aflow.toml"
        config_path.write_text("", encoding="utf-8")
        if startup_result is None:
            startup_result = PreparedRun(
                workflow_name="saved_workflow",
                repo_root=repo_root,
                plan_path=Path(prev_run["original_plan_path"]),
                config_path=config_path,
                max_turns=15,
                team="base",
                extra_instructions=("keep the patch focused",),
                start_step="implement_plan",
            )
        startup = Mock(return_value=startup_result)
        execute = Mock(
            return_value=type(
                "RunResult",
                (),
                {"turns_completed": 0, "end_reason": "done"},
            )()
        )
        stderr = io.StringIO()
        resolve_run_id = Mock(
            return_value=(Path(run_dir.name), "shell_last_run_id_file" if auto else "explicit_run_id")
        )
        load_run_json = Mock(return_value=prev_run)
        argv = ["run", "--resume"]
        if not auto:
            argv.append(run_dir.name)
        if reset_scope:
            argv.append("--resume-reset-scope")

        with patch.object(
            cli_module,
            "_bootstrap_config_files",
            return_value=(config_path, ()),
        ), patch.object(
            cli_module,
            "_resolve_repo_root",
            return_value=repo_root,
        ), patch.object(
            cli_module,
            "load_workflow_config",
            return_value=workflow_config,
        ), patch.object(
            cli_module,
            "validate_workflow_config",
            return_value=[],
        ), patch.object(
            cli_module,
            "resolve_run_id",
            resolve_run_id,
        ), patch.object(
            cli_module,
            "load_run_json",
            load_run_json,
        ), patch.object(
            cli_module,
            "_handle_startup_questions",
            startup,
        ), patch.object(
            cli_module,
            "execute_workflow",
            execute,
        ), patch.object(cli_module, "BannerRenderer"):
            with redirect_stderr(stderr):
                status = cli_module.main(argv)

        captured_resume = None
        if execute.call_args is not None:
            captured_resume = execute.call_args.kwargs.get("resume")
        return (
            status,
            stderr.getvalue(),
            startup,
            execute,
            resolve_run_id,
            load_run_json,
            captured_resume,
        )

    def test_resume_schema_admission_rejects_missing_legacy_bool_string_and_future(
        self,
    ) -> None:
        for observed in (None, 1, True, "2", 3):
            with self.subTest(observed=observed):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, current_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                prev_run = dict(current_run)
                if observed is None:
                    prev_run.pop("schema_version")
                else:
                    prev_run["schema_version"] = observed

                for auto in (False, True):
                    with self.subTest(auto=auto):
                        status, stderr, startup, execute, *_ = self._invoke_resume_command(
                            tmp_path,
                            prev_run,
                            run_dir,
                            workflow_config,
                            auto=auto,
                        )
                        assert status == 1
                        assert f"run '{run_dir.name}'" in stderr
                        assert "expected integer 2" in stderr
                        startup.assert_not_called()
                        execute.assert_not_called()
                        assert not (repo_root / ".aflow" / "runs").exists()

    def test_plain_auto_resume_ignores_unsupported_schema_without_prompt(self) -> None:
        import aflow.cli as cli_module

        for observed in (None, 1, True, "2", 3):
            with self.subTest(observed=observed):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, current_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                prev_run = dict(current_run)
                if observed is None:
                    prev_run.pop("schema_version")
                else:
                    prev_run["schema_version"] = observed
                with patch(
                    "aflow.cli.resolve_run_id",
                    return_value=(Path(run_dir.name), "shell_last_run_id_file"),
                ), patch("aflow.cli.load_run_json", return_value=prev_run):
                    result = cli_module._detect_resume_candidate(
                        repo_root=repo_root,
                        workflow_config=workflow_config.workflows["saved_workflow"],
                        workflow_name="saved_workflow",
                        plan_path=Path(prev_run["original_plan_path"]),
                        team="base",
                        selected_start_step="implement_plan",
                        max_turns=15,
                        extra_instructions=("keep the patch focused",),
                        requested_run_id=run_dir.name,
                        require_resume=False,
                    )
                assert result is None

    def test_resume_bootstrap_reconstructs_omitted_identity_from_exact_run(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run):
            result = cli_module._bootstrap_resume_invocation(
                repo_root=repo_root,
                workflow_config=workflow_config,
                requested_run_id=None,
                workflow_arg=None,
                plan_file_arg=None,
                team_arg=None,
                start_step_arg=None,
                max_turns_arg=None,
                extra_instructions_arg=(),
                extra_instructions_provided=False,
            )

        assert result.resolved_run_id == Path(run_dir.name)
        assert result.plan_path == Path(prev_run["original_plan_path"])
        assert result.workflow_name == "saved_workflow"
        assert result.team == "base"
        assert result.start_step == "implement_plan"
        assert result.max_turns == 15
        assert result.extra_instructions == ("keep the patch focused",)
        assert result.frozen_run_identity is not None

    def test_resume_bootstrap_accepts_exact_modern_frozen_identity(self) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, config_path, workflow_config, modern_run = (
            self._modern_resume_fixture(tmp_path)
        )
        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=modern_run):
            result = cli_module._bootstrap_resume_invocation(
                repo_root=repo_root,
                config_path=config_path,
                workflow_config=workflow_config,
                requested_run_id=run_dir.name,
                workflow_arg=None,
                plan_file_arg=None,
                team_arg=None,
                start_step_arg=None,
                max_turns_arg=None,
                extra_instructions_arg=(),
                extra_instructions_provided=False,
            )

        assert result.frozen_run_identity is not None
        assert result.frozen_run_identity.config_path == str(config_path.resolve())
        assert result.frozen_run_identity.config_fingerprint == modern_run[
            "frozen_config"
        ]["config_fingerprint"]

    def test_resume_bootstrap_rejects_frozen_identity_path_and_fingerprint_drift(
        self,
    ) -> None:
        import aflow.cli as cli_module

        for drift in ("config_path", "config_fingerprint"):
            with self.subTest(drift=drift):
                tmp_path = self._new_temp_path()
                (
                    repo_root,
                    run_dir,
                    config_path,
                    workflow_config,
                    modern_run,
                ) = self._modern_resume_fixture(tmp_path)
                modern_run = dict(modern_run)
                frozen_config = dict(modern_run["frozen_config"])
                if drift == "config_path":
                    current_config_path = repo_root / "other-aflow.toml"
                    frozen_config["config_path"] = str(config_path.resolve())
                else:
                    current_config_path = config_path
                    frozen_config["config_fingerprint"] = "different-fingerprint"
                modern_run["frozen_config"] = frozen_config

                with patch(
                    "aflow.cli.resolve_run_id",
                    return_value=(Path(run_dir.name), "explicit_run_id"),
                ), patch("aflow.cli.load_run_json", return_value=modern_run):
                    with pytest.raises(ValueError, match="frozen configuration mismatch"):
                        cli_module._bootstrap_resume_invocation(
                            repo_root=repo_root,
                            config_path=current_config_path,
                            workflow_config=workflow_config,
                            requested_run_id=run_dir.name,
                            workflow_arg=None,
                            plan_file_arg=None,
                            team_arg=None,
                            start_step_arg=None,
                            max_turns_arg=None,
                            extra_instructions_arg=(),
                            extra_instructions_provided=False,
                        )

    def test_modern_resume_requires_complete_frozen_identity_before_startup(
        self,
    ) -> None:
        import aflow.cli as cli_module

        cases = (
            (None, "expected a mapping"),
            ({"workflow_name": "saved_workflow"}, "frozen_config.config_path"),
            (
                {
                    "workflow_name": "saved_workflow",
                    "config_path": "/tmp/config",
                    "config_fingerprint": "",
                },
                "frozen_config.config_fingerprint",
            ),
        )
        for frozen_config, message in cases:
            with self.subTest(message=message):
                tmp_path = self._new_temp_path()
                (
                    repo_root,
                    run_dir,
                    config_path,
                    workflow_config,
                    modern_run,
                ) = self._modern_resume_fixture(tmp_path)
                modern_run = dict(modern_run)
                modern_run["frozen_config"] = frozen_config
                with patch(
                    "aflow.cli.resolve_run_id",
                    return_value=(Path(run_dir.name), "explicit_run_id"),
                ), patch("aflow.cli.load_run_json", return_value=modern_run):
                    with pytest.raises(ValueError, match=message):
                        cli_module._bootstrap_resume_invocation(
                            repo_root=repo_root,
                            config_path=config_path,
                            workflow_config=workflow_config,
                            requested_run_id=run_dir.name,
                            workflow_arg=None,
                            plan_file_arg=None,
                            team_arg=None,
                            start_step_arg=None,
                            max_turns_arg=None,
                            extra_instructions_arg=(),
                            extra_instructions_provided=False,
                        )

    def test_modern_plan_free_resume_rejects_identity_drift_before_startup(
        self,
    ) -> None:
        import aflow.cli as cli_module

        for drift in ("config_path", "config_fingerprint"):
            with self.subTest(drift=drift):
                tmp_path = self._new_temp_path()
                (
                    repo_root,
                    run_dir,
                    config_path,
                    workflow_config,
                    modern_run,
                ) = self._modern_resume_fixture(tmp_path)
                current_config_path = (
                    repo_root / "other-aflow.toml"
                    if drift == "config_path"
                    else config_path
                )
                current_config_path.write_text("", encoding="utf-8")
                if drift == "config_fingerprint":
                    modern_run = dict(modern_run)
                    modern_run["frozen_config"] = dict(modern_run["frozen_config"])
                    modern_run["frozen_config"]["config_fingerprint"] = "drifted"
                startup = Mock()
                stderr = io.StringIO()
                with patch.object(
                    cli_module,
                    "_bootstrap_config_files",
                    return_value=(current_config_path, ()),
                ), patch.object(
                    cli_module,
                    "_resolve_repo_root",
                    return_value=repo_root,
                ), patch.object(
                    cli_module,
                    "load_workflow_config",
                    return_value=workflow_config,
                ), patch.object(
                    cli_module,
                    "validate_workflow_config",
                    return_value=[],
                ), patch.object(
                    cli_module,
                    "resolve_run_id",
                    return_value=(Path(run_dir.name), "explicit_run_id"),
                ), patch.object(
                    cli_module,
                    "load_run_json",
                    return_value=modern_run,
                ), patch.object(
                    cli_module,
                    "_handle_startup_questions",
                    startup,
                ), redirect_stderr(stderr):
                    status = cli_module.main(["run", "--resume", run_dir.name])

                assert status == 1
                assert "frozen configuration mismatch" in stderr.getvalue()
                startup.assert_not_called()
                assert not (repo_root / ".aflow" / "runs").exists()

    def test_modern_plan_free_resume_rejects_malformed_identity_before_startup(
        self,
    ) -> None:
        import aflow.cli as cli_module

        cases = (
            None,
            {"workflow_name": "saved_workflow"},
            {
                "workflow_name": "saved_workflow",
                "config_path": "/tmp/config",
                "config_fingerprint": "",
            },
        )
        for frozen_config in cases:
            with self.subTest(frozen_config=frozen_config):
                tmp_path = self._new_temp_path()
                (
                    repo_root,
                    run_dir,
                    config_path,
                    workflow_config,
                    modern_run,
                ) = self._modern_resume_fixture(tmp_path)
                modern_run = dict(modern_run)
                modern_run["frozen_config"] = frozen_config
                startup = Mock()
                execute = Mock()
                stderr = io.StringIO()
                with patch.object(
                    cli_module,
                    "_bootstrap_config_files",
                    return_value=(config_path, ()),
                ), patch.object(
                    cli_module,
                    "_resolve_repo_root",
                    return_value=repo_root,
                ), patch.object(
                    cli_module,
                    "load_workflow_config",
                    return_value=workflow_config,
                ), patch.object(
                    cli_module,
                    "validate_workflow_config",
                    return_value=[],
                ), patch.object(
                    cli_module,
                    "resolve_run_id",
                    return_value=(Path(run_dir.name), "explicit_run_id"),
                ), patch.object(
                    cli_module,
                    "load_run_json",
                    return_value=modern_run,
                ), patch.object(
                    cli_module,
                    "_handle_startup_questions",
                    startup,
                ), patch.object(
                    cli_module,
                    "execute_workflow",
                    execute,
                ), redirect_stderr(stderr):
                    status = cli_module.main(["run", "--resume", run_dir.name])

                assert status == 1
                assert "invalid frozen_config" in stderr.getvalue()
                startup.assert_not_called()
                execute.assert_not_called()
                assert not (repo_root / ".aflow" / "runs").exists()

    def test_plan_free_resume_bootstrap_rejects_malformed_saved_metadata(
        self,
    ) -> None:
        import aflow.cli as cli_module

        cases = (
            ("original_plan_path", 17, "invalid original_plan_path"),
            ("extra_instructions", "not-a-list", "invalid extra_instructions"),
            ("max_turns", True, "invalid max_turns"),
            ("selected_start_step", "missing_step", "invalid selected_start_step"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
                prev_run = dict(prev_run)
                prev_run[field] = value
                with patch(
                    "aflow.cli.resolve_run_id",
                    return_value=(Path(run_dir.name), "explicit_run_id"),
                ), patch("aflow.cli.load_run_json", return_value=prev_run):
                    with pytest.raises(ValueError, match=message):
                        cli_module._bootstrap_resume_invocation(
                            repo_root=repo_root,
                            workflow_config=workflow_config,
                            requested_run_id=run_dir.name,
                            workflow_arg=None,
                            plan_file_arg=None,
                            team_arg=None,
                            start_step_arg=None,
                            max_turns_arg=None,
                            extra_instructions_arg=(),
                            extra_instructions_provided=False,
                        )

    def test_plan_free_resume_rejects_malformed_current_manager_authority(
        self,
    ) -> None:
        import aflow.cli as cli_module

        cases = (
            (
                "empty pending manager notes",
                lambda run: run.__setitem__("pending_manager_notes", {}),
                "pending_manager_notes",
            ),
            (
                "incomplete active scope",
                lambda run: run.__setitem__(
                    "active_implementation_scope",
                    {
                        "scope_id": "saved-scope",
                        "original_plan_path": "/saved/plan.md",
                        "checkpoint_index": 1,
                        "checkpoint_name": "First",
                        "opened_turn_number": 1,
                        "carried_reviewer_rejection_count": 0,
                        "envelope_artifact_path": "scopes/scope/envelope.json",
                        "envelope_artifact_sha256": "a" * 64,
                        "envelope_canonical_sha256": "b" * 64,
                    },
                ),
                "active_implementation_scope is missing required fields: awaiting_review",
            ),
            (
                "incomplete implementation attempt",
                lambda run: run.__setitem__(
                    "implementation_attempts",
                    {
                        "saved-scope": [{
                            "turn_number": 1,
                            "step_name": "implement",
                            "role": "worker",
                            "outcome": "progress",
                        }],
                    },
                ),
                "implementation_attempts[saved-scope][0] is missing required fields",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(case=name):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, prev_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                prev_run = dict(prev_run)
                mutate(prev_run)
                with pytest.raises(ValueError, match=re.escape(expected)):
                    cli_module._validate_current_resume_metadata(
                        prev_run,
                        Path(run_dir.name),
                    )

    def test_resume_bootstrap_accepts_compatible_repeated_identity(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run):
            result = cli_module._bootstrap_resume_invocation(
                repo_root=repo_root,
                workflow_config=workflow_config,
                requested_run_id=run_dir.name,
                workflow_arg="saved_workflow",
                plan_file_arg=str(Path(prev_run["plan_path"])),
                team_arg="base",
                start_step_arg="1",
                max_turns_arg=15,
                extra_instructions_arg=("keep the patch focused",),
                extra_instructions_provided=True,
            )

        assert result.workflow_name == "saved_workflow"
        assert result.start_step == "implement_plan"

    def test_resume_bootstrap_accepts_explicit_empty_extra_instructions_when_saved_empty(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        prev_run = dict(prev_run)
        prev_run["extra_instructions"] = []
        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run):
            result = cli_module._bootstrap_resume_invocation(
                repo_root=repo_root,
                workflow_config=workflow_config,
                requested_run_id=run_dir.name,
                workflow_arg=None,
                plan_file_arg=None,
                team_arg=None,
                start_step_arg=None,
                max_turns_arg=None,
                extra_instructions_arg=(),
                extra_instructions_provided=True,
            )

        assert result.extra_instructions == ()

    def test_resume_bootstrap_rejects_explicit_empty_extra_instructions_when_saved_nonempty(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run):
            with pytest.raises(ValueError, match="resume extra-instructions mismatch"):
                cli_module._bootstrap_resume_invocation(
                    repo_root=repo_root,
                    workflow_config=workflow_config,
                    requested_run_id=run_dir.name,
                    workflow_arg=None,
                    plan_file_arg=None,
                    team_arg=None,
                    start_step_arg=None,
                    max_turns_arg=None,
                    extra_instructions_arg=(),
                    extra_instructions_provided=True,
                )

    def test_plan_free_resume_explicit_empty_extra_instructions_reaches_bootstrap_conflict(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        config_path = repo_root / "aflow.toml"
        stderr = io.StringIO()
        startup = Mock()
        with patch.object(
            cli_module,
            "_bootstrap_config_files",
            return_value=(config_path, ()),
        ), patch.object(cli_module, "_resolve_repo_root", return_value=repo_root), patch.object(
            cli_module,
            "load_workflow_config",
            return_value=workflow_config,
        ), patch.object(cli_module, "validate_workflow_config", return_value=[]), patch.object(
            cli_module,
            "resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch.object(cli_module, "load_run_json", return_value=prev_run) as load_run_json, patch.object(
            cli_module,
            "_handle_startup_questions",
            startup,
        ), redirect_stderr(stderr):
            status = cli_module.main(["run", "--resume", run_dir.name, "--"])

        assert status == 1
        assert "resume extra-instructions mismatch" in stderr.getvalue()
        load_run_json.assert_called_once_with(run_dir)
        startup.assert_not_called()

    def test_resume_explicit_and_auto_reject_path_shaped_ids_before_loading_or_startup(
        self,
    ) -> None:
        import aflow.cli as cli_module

        repo_root = self._new_temp_path().resolve()
        config_path = repo_root / "aflow.toml"
        workflow_config = type(
            "WorkflowConfig",
            (),
            {"workflows": {}, "harnesses": {}},
        )()
        invalid_ids = (
            "../saved-run",
            "nested/saved-run",
            str(repo_root / "saved-run"),
            ".",
            "..",
        )
        for source in ("explicit_run_id", "shell_last_run_id_file"):
            for invalid_id in invalid_ids:
                with self.subTest(source=source, run_id=invalid_id):
                    requested_run_id = invalid_id if source == "explicit_run_id" else None
                    argv = ["run", "--resume"]
                    if requested_run_id is not None:
                        argv.append(requested_run_id)
                    stderr = io.StringIO()
                    startup = Mock()
                    controller = Mock()
                    with patch.object(
                        cli_module,
                        "_bootstrap_config_files",
                        return_value=(config_path, ()),
                    ), patch.object(
                        cli_module,
                        "_resolve_repo_root",
                        return_value=repo_root,
                    ), patch.object(
                        cli_module,
                        "load_workflow_config",
                        return_value=workflow_config,
                    ), patch.object(
                        cli_module,
                        "validate_workflow_config",
                        return_value=[],
                    ), patch.object(
                        cli_module,
                        "resolve_run_id",
                        return_value=(Path(invalid_id), source),
                    ) as resolve_run_id, patch.object(
                        cli_module,
                        "load_run_json",
                    ) as load_run_json, patch.object(
                        cli_module,
                        "_handle_startup_questions",
                        startup,
                    ), patch.object(cli_module, "execute_workflow", controller), redirect_stderr(
                        stderr
                    ):
                        status = cli_module.main(argv)

                    assert status == 1
                    assert "error: invalid resume run id" in stderr.getvalue()
                    resolve_run_id.assert_called_once_with(requested_run_id, repo_root)
                    load_run_json.assert_not_called()
                    startup.assert_not_called()
                    controller.assert_not_called()

    def test_plan_free_resume_bootstrap_rejects_missing_saved_plan_before_startup(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        prev_run = dict(prev_run)
        prev_run["original_plan_path"] = str(repo_root / "missing-plan.md")
        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run), \
             patch.object(cli_module, "_bootstrap_config_files", return_value=(repo_root / "aflow.toml", ())), \
             patch.object(cli_module, "_resolve_repo_root", return_value=repo_root), \
             patch.object(cli_module, "load_workflow_config", return_value=workflow_config), \
             patch.object(cli_module, "validate_workflow_config", return_value=[]), \
             patch.object(cli_module, "_handle_startup_questions") as startup:
            status = cli_module.main(["run", "--resume", run_dir.name])

        assert status == 1
        startup.assert_not_called()

    def test_plan_free_resume_rejects_missing_run_metadata_before_startup(
        self,
    ) -> None:
        import aflow.cli as cli_module
        from unittest.mock import Mock

        tmp_path = self._new_temp_path()
        repo_root = tmp_path.resolve()
        run_dir = repo_root / ".aflow" / "runs" / "missing-metadata"
        config_path = repo_root / "aflow.toml"
        workflow_config = type(
            "WorkflowConfig",
            (),
            {"workflows": {}, "harnesses": {}},
        )()
        startup = Mock()
        stderr = io.StringIO()
        with patch.object(
            cli_module,
            "_bootstrap_config_files",
            return_value=(config_path, ()),
        ), patch.object(cli_module, "_resolve_repo_root", return_value=repo_root), patch.object(
            cli_module,
            "load_workflow_config",
            return_value=workflow_config,
        ), patch.object(cli_module, "validate_workflow_config", return_value=[]), patch.object(
            cli_module,
            "resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch.object(cli_module, "load_run_json", return_value=None), patch.object(
            cli_module,
            "_handle_startup_questions",
            startup,
        ), redirect_stderr(stderr):
            status = cli_module.main(["run", "--resume", run_dir.name])

        assert status == 1
        assert "does not contain readable or valid run metadata" in stderr.getvalue()
        startup.assert_not_called()

    def test_plan_free_resume_rejects_complete_context_metadata_before_startup(
        self,
    ) -> None:
        import aflow.cli as cli_module

        cases = (
            (
                "missing main branch",
                lambda run: run.pop("main_branch"),
                "no recorded main branch",
            ),
            (
                "invalid lifecycle teardown",
                lambda run: run.__setitem__("lifecycle_teardown", "merge"),
                "invalid lifecycle_teardown",
            ),
            (
                "unsafe scope envelope reference",
                lambda run: run.__setitem__(
                    "active_implementation_scope",
                    {
                        "scope_id": "saved-scope",
                        "original_plan_path": "saved-plan.md",
                        "checkpoint_index": 1,
                        "checkpoint_name": "Checkpoint 1: Saved",
                        "opened_turn_number": 1,
                        "awaiting_review": False,
                        "carried_reviewer_rejection_count": 0,
                        "envelope_artifact_path": "../../outside-envelope.json",
                        "envelope_artifact_sha256": "a" * 64,
                        "envelope_canonical_sha256": "b" * 64,
                        "current_partition_generation_id": None,
                        "current_partition_candidate_sha256": None,
                        "current_partition_id": None,
                    },
                ),
                "invalid scope envelope reference",
            ),
        )

        for name, mutate, expected in cases:
            with self.subTest(case=name):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                prev_run = dict(original_run)
                mutate(prev_run)
                config_path = repo_root / "aflow.toml"
                config_path.write_text("", encoding="utf-8")
                stderr = io.StringIO()
                startup = Mock()
                execute = Mock()

                with patch.object(
                    cli_module,
                    "_bootstrap_config_files",
                    return_value=(config_path, ()),
                ), patch.object(
                    cli_module,
                    "_resolve_repo_root",
                    return_value=repo_root,
                ), patch.object(
                    cli_module,
                    "load_workflow_config",
                    return_value=workflow_config,
                ), patch.object(
                    cli_module,
                    "validate_workflow_config",
                    return_value=[],
                ), patch.object(
                    cli_module,
                    "resolve_run_id",
                    return_value=(Path(run_dir.name), "explicit_run_id"),
                ), patch.object(
                    cli_module,
                    "load_run_json",
                    return_value=prev_run,
                ), patch.object(
                    cli_module,
                    "_handle_startup_questions",
                    startup,
                ), patch.object(
                    cli_module,
                    "execute_workflow",
                    execute,
                ), redirect_stderr(stderr):
                    status = cli_module.main(["run", "--resume", run_dir.name])

                assert status == 1
                assert expected in stderr.getvalue()
                startup.assert_not_called()
                execute.assert_not_called()
                assert not (repo_root / ".aflow" / "runs").exists()

    def test_resume_rejects_present_pending_repartition_before_startup(self) -> None:
        cases = (
            ("scalar", lambda pending: "not-a-transaction", "pending_repartition"),
            (
                "tolerant decoder drop",
                lambda pending: {"schema_version": 1},
                "pending_repartition",
            ),
            (
                "missing generation identity",
                lambda pending: pending.pop("generation_id"),
                "generation_id",
            ),
            (
                "missing routing identity",
                lambda pending: pending.__setitem__("resolved_target_role", None),
                "resolved_target_role",
            ),
        )
        for name, mutate, expected_field in cases:
            with self.subTest(case=name):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                prev_run = dict(original_run)
                pending, _artifacts, scope = _bound_pending_repartition_fixture(
                    run_dir,
                    Path(original_run["original_plan_path"]),
                )
                prev_run["active_implementation_scope"] = scope
                prev_run["manager_decision_number"] = pending["decision_number"]
                if name in {"scalar", "tolerant decoder drop"}:
                    prev_run["pending_repartition"] = mutate(pending)
                else:
                    mutate(pending)
                    prev_run["pending_repartition"] = pending

                status, stderr, startup, execute, _resolve, _load, _resume = (
                    self._invoke_resume_command(
                        tmp_path,
                        prev_run,
                        run_dir,
                        workflow_config,
                    )
                )

                assert status == 1
                assert expected_field in stderr
                startup.assert_not_called()
                execute.assert_not_called()
                assert sorted(
                    path.name for path in (repo_root / ".aflow" / "runs").iterdir()
                ) == [run_dir.name]

    def test_resume_rejects_pending_identity_without_restored_authority(self) -> None:
        cases = (
            (
                "absent active scope",
                lambda pending, scope, run: run.pop("active_implementation_scope"),
                "active_implementation_scope",
            ),
            (
                "legacy scope authority",
                lambda pending, scope, run: (
                    scope.pop("envelope_artifact_path"),
                    scope.pop("envelope_artifact_sha256"),
                    scope.pop("envelope_canonical_sha256"),
                ),
                "active_implementation_scope",
            ),
            (
                "partial scope authority",
                lambda pending, scope, run: scope.__setitem__(
                    "envelope_artifact_sha256", None
                ),
                "invalid scope envelope reference",
            ),
            (
                "scope mismatch",
                lambda pending, scope, run: pending.__setitem__(
                    "scope_id", "different-scope"
                ),
                "scope_id",
            ),
            (
                "envelope identity mismatch",
                lambda pending, scope, run: pending.__setitem__(
                    "envelope_sha256", "0" * 64
                ),
                "envelope_sha256",
            ),
            (
                "malformed envelope digest",
                lambda pending, scope, run: pending.__setitem__(
                    "envelope_sha256", "E" * 64
                ),
                "envelope_sha256",
            ),
            (
                "malformed source digest",
                lambda pending, scope, run: pending.__setitem__(
                    "source_plan_sha256", "z" * 64
                ),
                "source_plan_sha256",
            ),
            (
                "forged generation identity",
                lambda pending, scope, run: pending.__setitem__(
                    "generation_id", "gen-forged"
                ),
                "generation_id",
            ),
            (
                "manager decision mismatch",
                lambda pending, scope, run: pending.__setitem__(
                    "decision_number", 2
                ),
                "decision_number",
            ),
            (
                "missing manager decision boundary",
                lambda pending, scope, run: run.pop("manager_decision_number"),
                "decision_number",
            ),
            (
                "missing workflow target",
                lambda pending, scope, run: pending.__setitem__(
                    "resolved_target_step", "missing-step"
                ),
                "resolved_target_step",
            ),
            (
                "workflow role mismatch",
                lambda pending, scope, run: pending.__setitem__(
                    "resolved_target_role", "reviewer"
                ),
                "resolved_target_role",
            ),
        )
        for auto in (False, True):
            for name, mutate, expected in cases:
                with self.subTest(auto=auto, case=name):
                    tmp_path = self._new_temp_path()
                    repo_root, run_dir, workflow_config, original_run = (
                        self._resume_bootstrap_fixture(tmp_path)
                    )
                    pending, _artifacts, scope = _bound_pending_repartition_fixture(
                        run_dir,
                        Path(original_run["original_plan_path"]),
                    )
                    prev_run = dict(original_run)
                    prev_run["active_implementation_scope"] = scope
                    prev_run["manager_decision_number"] = pending["decision_number"]
                    prev_run["pending_repartition"] = pending
                    mutate(pending, scope, prev_run)

                    status, stderr, startup, execute, _resolve, _load, _resume = (
                        self._invoke_resume_command(
                            tmp_path,
                            prev_run,
                            run_dir,
                            workflow_config,
                            auto=auto,
                        )
                    )

                    assert status == 1
                    assert expected in stderr
                    startup.assert_not_called()
                    execute.assert_not_called()
                    assert sorted(
                        path.name
                        for path in (repo_root / ".aflow" / "runs").iterdir()
                    ) == [run_dir.name]

    def test_resume_rejects_each_missing_pending_repartition_artifact(self) -> None:
        artifact_fields = (
            "proposal_artifact_path",
            "candidate_artifact_path",
            "mechanical_validation_artifact_path",
            "semantic_verdict_artifact_path",
        )
        for field in artifact_fields:
            with self.subTest(field=field):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                pending, _artifacts, scope = _bound_pending_repartition_fixture(
                    run_dir,
                    Path(original_run["original_plan_path"]),
                )
                pending.pop(field)
                prev_run = dict(original_run)
                prev_run["active_implementation_scope"] = scope
                prev_run["manager_decision_number"] = pending["decision_number"]
                prev_run["pending_repartition"] = pending

                status, stderr, startup, execute, _resolve, _load, _resume = (
                    self._invoke_resume_command(
                        tmp_path,
                        prev_run,
                        run_dir,
                        workflow_config,
                    )
                )

                assert status == 1
                assert field in stderr
                startup.assert_not_called()
                execute.assert_not_called()
                assert sorted(
                    path.name for path in (repo_root / ".aflow" / "runs").iterdir()
                ) == [run_dir.name]

    def test_resume_rejects_unsafe_missing_wrong_kind_and_unreadable_pending_artifacts(
        self,
    ) -> None:
        for case in ("unsafe", "symlink", "missing", "wrong_kind", "unreadable"):
            with self.subTest(case=case):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                pending, _artifacts, scope = _bound_pending_repartition_fixture(
                    run_dir,
                    Path(original_run["original_plan_path"]),
                )
                proposal_path = run_dir / pending["proposal_artifact_path"]
                if case == "unsafe":
                    pending["proposal_artifact_path"] = "../../outside-proposal.json"
                elif case == "symlink":
                    outside = repo_root / "outside-proposal.json"
                    outside.write_bytes(b"outside")
                    proposal_path.unlink()
                    proposal_path.symlink_to(outside)
                elif case == "missing":
                    proposal_path.unlink()
                elif case == "wrong_kind":
                    pending["proposal_artifact_path"] = pending["latest_attempt_path"]
                prev_run = dict(original_run)
                prev_run["active_implementation_scope"] = scope
                prev_run["manager_decision_number"] = pending["decision_number"]
                prev_run["pending_repartition"] = pending

                if case == "unreadable":
                    real_read_bytes = Path.read_bytes

                    def deny_proposal(path: Path) -> bytes:
                        if path == proposal_path:
                            raise PermissionError("denied for resume test")
                        return real_read_bytes(path)

                    with patch.object(Path, "read_bytes", deny_proposal):
                        result = self._invoke_resume_command(
                            tmp_path,
                            prev_run,
                            run_dir,
                            workflow_config,
                        )
                else:
                    result = self._invoke_resume_command(
                        tmp_path,
                        prev_run,
                        run_dir,
                        workflow_config,
                    )
                status, stderr, startup, execute, _resolve, _load, _resume = result

                assert status == 1
                assert "proposal_artifact_path" in stderr
                startup.assert_not_called()
                execute.assert_not_called()
                assert sorted(
                    path.name for path in (repo_root / ".aflow" / "runs").iterdir()
                ) == [run_dir.name]

    def test_resume_rejects_noncanonical_pending_artifact_paths(self) -> None:
        cases = (
            (
                "missing latest attempt path",
                lambda pending, run_dir: pending.__setitem__(
                    "latest_attempt_path", None
                ),
                "latest_attempt_path",
            ),
            (
                "redundant path spelling",
                lambda pending, run_dir: pending.__setitem__(
                    "candidate_artifact_path",
                    pending["candidate_artifact_path"].replace(
                        "/candidate-plan.md", "/./candidate-plan.md"
                    ),
                ),
                "candidate_artifact_path",
            ),
            (
                "internal symlink alias",
                lambda pending, run_dir: (
                    (run_dir / pending["candidate_artifact_path"])
                    .with_name("candidate-alias.md")
                    .symlink_to(run_dir / pending["candidate_artifact_path"]),
                    pending.__setitem__(
                        "candidate_artifact_path",
                        str(
                            (run_dir / pending["candidate_artifact_path"])
                            .with_name("candidate-alias.md")
                            .relative_to(run_dir)
                        ).replace("\\", "/"),
                    ),
                ),
                "candidate_artifact_path",
            ),
        )
        for auto in (False, True):
            for name, mutate, expected in cases:
                with self.subTest(auto=auto, case=name):
                    tmp_path = self._new_temp_path()
                    repo_root, run_dir, workflow_config, original_run = (
                        self._resume_bootstrap_fixture(tmp_path)
                    )
                    pending, _artifacts, scope = _bound_pending_repartition_fixture(
                        run_dir,
                        Path(original_run["original_plan_path"]),
                    )
                    prev_run = dict(original_run)
                    prev_run["active_implementation_scope"] = scope
                    prev_run["manager_decision_number"] = pending["decision_number"]
                    prev_run["pending_repartition"] = pending
                    mutate(pending, run_dir)

                    status, stderr, startup, execute, _resolve, _load, _resume = (
                        self._invoke_resume_command(
                            tmp_path,
                            prev_run,
                            run_dir,
                            workflow_config,
                            auto=auto,
                        )
                    )

                    assert status == 1
                    assert expected in stderr
                    startup.assert_not_called()
                    execute.assert_not_called()
                    assert sorted(
                        path.name
                        for path in (repo_root / ".aflow" / "runs").iterdir()
                    ) == [run_dir.name]

    def test_complete_pending_repartition_resume_binds_exact_bytes_before_startup(
        self,
    ) -> None:
        for auto in (False, True):
            with self.subTest(auto=auto):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                pending, artifacts, scope = _bound_pending_repartition_fixture(
                    run_dir,
                    Path(original_run["original_plan_path"]),
                )
                prev_run = dict(original_run)
                prev_run["active_implementation_scope"] = scope
                prev_run["manager_decision_number"] = pending["decision_number"]
                prev_run["pending_repartition"] = pending

                status, stderr, startup, execute, resolve, load, resume = (
                    self._invoke_resume_command(
                        tmp_path,
                        prev_run,
                        run_dir,
                        workflow_config,
                        auto=auto,
                    )
                )

                assert status == 0, stderr
                startup.assert_called_once()
                execute.assert_called_once()
                resolve.assert_called_once_with(
                    None if auto else run_dir.name,
                    repo_root,
                )
                load.assert_called_once_with(run_dir)
                assert isinstance(resume, ResumeContext)
                assert resume.pending_repartition is not None
                assert dict(resume.repartition_artifact_bytes) == artifacts

    def test_environment_preflight_repartition_block_replays_finalized_boundary(
        self,
    ) -> None:
        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, original_run = (
            self._resume_bootstrap_fixture(tmp_path)
        )
        plan_path = Path(original_run["original_plan_path"])
        pending, _artifacts, scope = _bound_pending_repartition_fixture(
            run_dir,
            plan_path,
        )
        turn_dir = run_dir / "turns" / "turn-001"
        turn_dir.mkdir(parents=True)
        snapshot = {
            "current_checkpoint_index": 1,
            "current_checkpoint_name": "Checkpoint 1: First",
            "current_checkpoint_unchecked_step_count": 1,
            "is_complete": False,
            "total_checkpoint_count": 1,
            "unchecked_checkpoint_count": 1,
        }
        (turn_dir / "result.json").write_text(
            json.dumps({
                "turn_number": 1,
                "status": "running",
                "step_name": "implement_plan",
                "step_role": "worker",
                "selector": "codex.worker",
                "returncode": 0,
                "active_plan_path": str(plan_path),
                "new_plan_path": str(plan_path),
                "snapshot_after": snapshot,
                "conditions": {
                    "DONE": False,
                    "NEW_PLAN_EXISTS": False,
                    "MAX_TURNS_REACHED": False,
                },
                "chosen_transition": "implement_plan",
                "chosen_transition_condition": None,
            }),
            encoding="utf-8",
        )
        prev_run = dict(original_run)
        prev_run.update({
            "active_turn": 1,
            "turns_completed": 1,
            "current_step_name": "implement_plan",
            "failure_kind": "environment_preflight",
            "environment_preflight": {
                "classification": "harness_environment_preflight",
                "reason_code": "harness_executable_missing",
                "harness": "reasonix",
                "invocation_kind": "checkpoint_repartition",
                "required_executable": "reasonix",
                "checked_command": ["reasonix"],
                "remediation": "Install Reasonix.",
                "safe_diagnostics": {},
                "step_name": "implement_plan",
            },
            "active_implementation_scope": scope,
            "manager_decision_number": pending["decision_number"],
            "pending_repartition": pending,
        })

        status, stderr, startup, execute, _resolve, _load, resume = (
            self._invoke_resume_command(
                tmp_path,
                prev_run,
                run_dir,
                workflow_config,
            )
        )

        assert status == 0, stderr
        startup.assert_called_once()
        execute.assert_called_once()
        assert isinstance(resume, ResumeContext)
        assert resume.pending_repartition is None
        assert resume.repartition_artifact_bytes == {}
        assert resume.pending_finalized_turn is not None
        assert resume.pending_finalized_turn.turn_number == 1
        assert resume.pending_finalized_turn.step_name == "implement_plan"

    def test_resume_absent_or_null_pending_repartition_remains_empty(self) -> None:
        for raw_value in ("absent", None):
            with self.subTest(raw_value=raw_value):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                prev_run = dict(original_run)
                if raw_value != "absent":
                    prev_run["pending_repartition"] = raw_value

                status, stderr, startup, execute, _resolve, _load, resume = (
                    self._invoke_resume_command(
                        tmp_path,
                        prev_run,
                        run_dir,
                        workflow_config,
                    )
                )

                assert status == 0, stderr
                startup.assert_called_once()
                execute.assert_called_once()
                assert isinstance(resume, ResumeContext)
                assert resume.pending_repartition is None
                assert resume.repartition_artifact_bytes == {}

    def test_resume_surfaces_authoritative_pending_repartition_stage_errors_before_startup(
        self,
    ) -> None:
        for stage, expected in (
            (
                "decided",
                "must be reconciled before a harness can start",
            ),
            (
                "failed",
                "without explicit scope reset",
            ),
        ):
            with self.subTest(stage=stage):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                pending, _artifacts, scope = _bound_pending_repartition_fixture(
                    run_dir,
                    Path(original_run["original_plan_path"]),
                )
                pending["stage"] = stage
                if stage == "failed":
                    pending["failed_stage"] = "semantic"
                    pending["failure_reason"] = "synthetic failure"
                else:
                    pending["failed_stage"] = None
                    pending["failure_reason"] = None
                prev_run = dict(original_run)
                prev_run["active_implementation_scope"] = scope
                prev_run["manager_decision_number"] = pending["decision_number"]
                prev_run["pending_repartition"] = pending

                status, stderr, startup, execute, _resolve, _load, _resume = (
                    self._invoke_resume_command(
                        tmp_path,
                        prev_run,
                        run_dir,
                        workflow_config,
                    )
                )

                assert status == 1
                assert expected in stderr
                startup.assert_not_called()
                execute.assert_not_called()
                assert sorted(
                    path.name for path in (repo_root / ".aflow" / "runs").iterdir()
                ) == [run_dir.name]

    def test_resume_reset_scope_ignores_malformed_pending_repartition_without_reading_it(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, original_run = (
            self._resume_bootstrap_fixture(tmp_path)
        )
        outside = repo_root / "outside-pending.json"
        outside.write_bytes(b"must not be read")
        prev_run = dict(original_run)
        prev_run["pending_repartition"] = {
            "schema_version": 1,
            "stage": "semantically_validated",
            "latest_attempt_path": "../../outside-pending.json",
            "proposal_artifact_path": "../../outside-pending.json",
        }
        original_manager_resume_fields = cli_module.manager_resume_fields
        decoder_payloads: list[object] = []

        def observe_decoder(payload: object) -> dict[str, object]:
            decoder_payloads.append(payload)
            return original_manager_resume_fields(payload)

        with patch.object(
            cli_module,
            "manager_resume_fields",
            side_effect=observe_decoder,
        ), patch.object(
            cli_module,
            "load_scope_envelope_for_resume",
            side_effect=AssertionError("reset scope must not load envelope paths"),
        ), patch.object(
            cli_module,
            "derive_generation_id",
            side_effect=AssertionError("reset scope must not derive pending identity"),
        ):
            status, stderr, startup, execute, _resolve, _load, resume = (
                self._invoke_resume_command(
                    tmp_path,
                    prev_run,
                    run_dir,
                    workflow_config,
                    reset_scope=True,
                )
            )

        assert status == 0, stderr
        startup.assert_called_once()
        execute.assert_called_once()
        assert decoder_payloads
        assert "pending_repartition" not in decoder_payloads[0]
        assert isinstance(resume, ResumeContext)
        assert resume.pending_repartition is None
        assert resume.repartition_artifact_bytes == {}
        assert outside.read_bytes() == b"must not be read"

    def test_resume_reset_scope_still_rejects_non_scoped_metadata(self) -> None:
        cases = (
            (
                "missing worktree metadata",
                lambda run: run.pop("feature_branch"),
                "no recorded feature branch",
            ),
            (
                "malformed lifecycle metadata",
                lambda run: run.__setitem__("lifecycle_setup", "worktree"),
                "invalid lifecycle_setup",
            ),
            (
                "mismatched frozen identity",
                lambda run: run.update(
                    {
                        "schema_version": 2,
                        "frozen_config": {
                            "workflow_name": "other_workflow",
                            "config_path": str(Path(run["repo_root"]) / "aflow.toml"),
                            "config_fingerprint": "0" * 64,
                        },
                    }
                ),
                "frozen configuration mismatch",
            ),
            (
                "incomplete frozen identity",
                lambda run: run.update(
                    {
                        "schema_version": 2,
                        "frozen_config": {
                            "workflow_name": "saved_workflow",
                            "config_path": str(Path(run["repo_root"]) / "aflow.toml"),
                        },
                    }
                ),
                "invalid frozen_config",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(case=name):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, original_run = (
                    self._resume_bootstrap_fixture(tmp_path)
                )
                prev_run = dict(original_run)
                prev_run["pending_repartition"] = {
                    "latest_attempt_path": "../../outside-pending.json",
                    "proposal_artifact_path": "../../outside-pending.json",
                }
                mutate(prev_run)

                status, stderr, startup, execute, _resolve, _load, _resume = (
                    self._invoke_resume_command(
                        tmp_path,
                        prev_run,
                        run_dir,
                        workflow_config,
                        reset_scope=True,
                    )
                )

                assert status == 1
                assert expected in stderr
                startup.assert_not_called()
                execute.assert_not_called()

    def test_fresh_run_without_plan_still_requires_plan_file(self) -> None:
        import aflow.cli as cli_module

        repo_root = self._new_temp_path()
        config_path = repo_root / "aflow.toml"
        workflow_config = type("WorkflowConfig", (), {"workflows": {}, "harnesses": {}})()
        stderr = io.StringIO()
        with patch.object(cli_module, "_bootstrap_config_files", return_value=(config_path, ())), \
             patch.object(cli_module, "_resolve_repo_root", return_value=repo_root), \
             patch.object(cli_module, "load_workflow_config", return_value=workflow_config), \
             redirect_stderr(stderr):
            status = cli_module.main(["run"])

        assert status == 1
        assert stderr.getvalue().strip() == "error: plan_file is required"

    def test_resume_bootstrap_rejects_conflicting_repeated_identity(
        self,
    ) -> None:
        import aflow.cli as cli_module

        cases = (
            (
                {"plan_file_arg": "/different/plan.md"},
                "resume plan mismatch",
            ),
            ({"workflow_arg": "other_workflow"}, "resume workflow mismatch"),
            ({"team_arg": "other-team"}, "resume team mismatch"),
            ({"max_turns_arg": 30}, "resume max-turns mismatch"),
            (
                {"extra_instructions_arg": ("override",)},
                "resume extra-instructions mismatch",
            ),
        )
        for kwargs, message in cases:
            with self.subTest(message=message):
                tmp_path = self._new_temp_path()
                repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
                arguments: dict[str, object] = {
                    "workflow_arg": None,
                    "plan_file_arg": None,
                    "team_arg": None,
                    "start_step_arg": None,
                    "max_turns_arg": None,
                    "extra_instructions_arg": (),
                    "extra_instructions_provided": False,
                }
                if "extra_instructions_arg" in kwargs:
                    arguments["extra_instructions_provided"] = True
                arguments.update(kwargs)
                with patch(
                    "aflow.cli.resolve_run_id",
                    return_value=(Path(run_dir.name), "explicit_run_id"),
                ), patch("aflow.cli.load_run_json", return_value=prev_run):
                    with pytest.raises(ValueError, match=message):
                        cli_module._bootstrap_resume_invocation(
                            repo_root=repo_root,
                            workflow_config=workflow_config,
                            requested_run_id=run_dir.name,
                            **arguments,
                        )

    def test_resume_bootstrap_rejects_missing_original_plan_path_without_fallback(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        prev_run = dict(prev_run)
        prev_run.pop("original_plan_path")
        with patch(
            "aflow.cli.resolve_run_id",
            return_value=(Path(run_dir.name), "explicit_run_id"),
        ), patch("aflow.cli.load_run_json", return_value=prev_run):
            with pytest.raises(ValueError, match="original_plan_path"):
                cli_module._bootstrap_resume_invocation(
                    repo_root=repo_root,
                    workflow_config=workflow_config,
                    requested_run_id=run_dir.name,
                    workflow_arg=None,
                    plan_file_arg=None,
                    team_arg=None,
                    start_step_arg=None,
                    max_turns_arg=None,
                    extra_instructions_arg=(),
                    extra_instructions_provided=False,
                )

    def test_plan_free_resume_bootstrap_runs_before_startup_and_detector_uses_same_run(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        config_path = repo_root / "aflow.toml"
        config_path.write_text("", encoding="utf-8")
        workflow_config.aflow = type(
            "AflowConfig",
            (),
            {"default_workflow": "different_default", "banner_files_limit": 10},
        )()
        prepared = PreparedRun(
            workflow_name="saved_workflow",
            repo_root=repo_root,
            plan_path=Path(prev_run["original_plan_path"]),
            config_path=config_path,
            max_turns=15,
            team="base",
            extra_instructions=("keep the patch focused",),
            start_step="implement_plan",
        )
        detector_kwargs: dict[str, object] = {}
        captured: dict[str, object] = {}
        real_bootstrap = cli_module._bootstrap_resume_invocation
        real_detect = cli_module._detect_resume_candidate

        def detect(**kwargs: object) -> object:
            detector_kwargs.update(kwargs)
            return real_detect(**kwargs)

        def bootstrap(**kwargs: object) -> object:
            result = real_bootstrap(**kwargs)
            captured["bootstrap"] = result
            return result

        def execute(prepared_run, *, banner, resume, observer):
            captured["resume"] = resume
            return result

        result = type("RunResult", (), {"turns_completed": 0, "end_reason": "done"})()
        with patch.object(cli_module, "_bootstrap_config_files", return_value=(config_path, ())), \
             patch.object(cli_module, "_resolve_repo_root", return_value=repo_root), \
             patch.object(cli_module, "load_workflow_config", return_value=workflow_config), \
             patch.object(cli_module, "validate_workflow_config", return_value=[]), \
             patch.object(cli_module, "resolve_run_id", return_value=(Path(run_dir.name), "explicit_run_id")) as resolve_run_id, \
             patch.object(cli_module, "load_run_json", return_value=prev_run) as load_run_json, \
             patch.object(cli_module, "_handle_startup_questions", return_value=prepared) as startup, \
             patch.object(cli_module, "_bootstrap_resume_invocation", side_effect=bootstrap), \
             patch.object(cli_module, "_detect_resume_candidate", side_effect=detect), \
             patch.object(cli_module, "BannerRenderer"), \
             patch.object(cli_module, "execute_workflow", side_effect=execute):
            status = cli_module.main(["run", "--resume", run_dir.name])

        assert status == 0
        startup.assert_called_once()
        request = startup.call_args.args[0]
        assert request.plan_path == Path(prev_run["original_plan_path"])
        assert request.workflow_name == "saved_workflow"
        assert request.max_turns == 15
        assert request.team == "base"
        assert request.start_step == "implement_plan"
        assert request.extra_instructions == ("keep the patch focused",)
        assert detector_kwargs["resume_bootstrap"].run_dir == run_dir
        assert captured["resume"] is captured["bootstrap"].resume_context
        assert captured["resume"].main_branch == "main"
        resolve_run_id.assert_called_once_with(run_dir.name, repo_root)
        load_run_json.assert_called_once_with(run_dir)

    def test_plan_free_auto_resume_uses_same_resolved_run_without_reselecting(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        config_path = repo_root / "aflow.toml"
        config_path.write_text("", encoding="utf-8")
        workflow_config.aflow = type(
            "AflowConfig",
            (),
            {"default_workflow": "different_default", "banner_files_limit": 10},
        )()
        prepared = PreparedRun(
            workflow_name="saved_workflow",
            repo_root=repo_root,
            plan_path=Path(prev_run["original_plan_path"]),
            config_path=config_path,
            max_turns=15,
            team="base",
            extra_instructions=("keep the patch focused",),
            start_step="implement_plan",
        )
        detector_kwargs: dict[str, object] = {}
        captured: dict[str, object] = {}
        real_bootstrap = cli_module._bootstrap_resume_invocation
        real_detect = cli_module._detect_resume_candidate

        def detect(**kwargs: object) -> object:
            detector_kwargs.update(kwargs)
            return real_detect(**kwargs)

        def bootstrap(**kwargs: object) -> object:
            result = real_bootstrap(**kwargs)
            captured["bootstrap"] = result
            return result

        def execute(prepared_run, *, banner, resume, observer):
            captured["resume"] = resume
            return result

        result = type("RunResult", (), {"turns_completed": 0, "end_reason": "done"})()
        with patch.object(cli_module, "_bootstrap_config_files", return_value=(config_path, ())), \
             patch.object(cli_module, "_resolve_repo_root", return_value=repo_root), \
             patch.object(cli_module, "load_workflow_config", return_value=workflow_config), \
             patch.object(cli_module, "validate_workflow_config", return_value=[]), \
             patch.object(cli_module, "resolve_run_id", return_value=(Path(run_dir.name), "shell_last_run_id_file")) as resolve_run_id, \
             patch.object(cli_module, "load_run_json", return_value=prev_run) as load_run_json, \
             patch.object(cli_module, "_handle_startup_questions", return_value=prepared), \
             patch.object(cli_module, "_bootstrap_resume_invocation", side_effect=bootstrap), \
             patch.object(cli_module, "_detect_resume_candidate", side_effect=detect), \
             patch.object(cli_module, "BannerRenderer"), \
             patch.object(cli_module, "execute_workflow", side_effect=execute):
            status = cli_module.main(["run", "--resume"])

        assert status == 0
        assert detector_kwargs["resume_bootstrap"].run_dir == run_dir
        assert captured["resume"] is captured["bootstrap"].resume_context
        resolve_run_id.assert_called_once_with(None, repo_root)
        load_run_json.assert_called_once_with(run_dir)

    def test_plan_free_resume_carries_pending_finalized_turn_to_executor(
        self,
    ) -> None:
        import aflow.cli as cli_module

        tmp_path = self._new_temp_path()
        repo_root, run_dir, workflow_config, prev_run = self._resume_bootstrap_fixture(tmp_path)
        config_path = repo_root / "aflow.toml"
        config_path.write_text("", encoding="utf-8")
        frozen_identity = _freeze_run_identity(
            "saved_workflow",
            workflow_config,
            config_dir=config_path,
        )
        repair_path = repo_root / "plans" / "in-progress" / "repair-plan.md"
        repair_path.write_text(_VALID_PLAN, encoding="utf-8")
        workflow_config.aflow = type(
            "AflowConfig",
            (),
            {"default_workflow": "different_default", "banner_files_limit": 10},
        )()

        snapshot = {
            "current_checkpoint_index": 2,
            "current_checkpoint_name": "Checkpoint 2: Repair",
            "current_checkpoint_unchecked_step_count": 4,
            "is_complete": False,
            "total_checkpoint_count": 2,
            "unchecked_checkpoint_count": 1,
        }
        result_payload = {
            "turn_number": 2,
            "status": "completed",
            "step_name": "review_cp_implementation",
            "step_role": "reviewer",
            "selector": "codex.reviewer",
            "returncode": 0,
            "active_plan_path": str(prev_run["original_plan_path"]),
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
        }
        turn_dir = run_dir / "turns" / "turn-002"
        turn_dir.mkdir(parents=True)
        result_path = turn_dir / "result.json"
        result_path.write_text(json.dumps(result_payload), encoding="utf-8")
        result_bytes = result_path.read_bytes()
        (turn_dir / "stdout.txt").write_text("review rejected", encoding="utf-8")
        (turn_dir / "stderr.txt").write_text("", encoding="utf-8")

        durable_run = dict(prev_run)
        durable_run.update(
            {
                "status": "running",
                "active_turn": 2,
                "turns_completed": 1,
                "current_step_name": "review_cp_implementation",
                "active_plan_path": str(prev_run["original_plan_path"]),
                "last_snapshot": snapshot,
                "schema_version": 2,
                "frozen_config": {
                    "workflow_name": frozen_identity.workflow_name,
                    "config_path": frozen_identity.config_path,
                    "config_fingerprint": frozen_identity.config_fingerprint,
                },
                "pending_manager_notes": {
                    "target_step": "review_cp_implementation",
                    "notes": ["stale manager note"],
                    "decision_number": 1,
                },
                "pending_step_team_override": {
                    "target_step": "review_cp_implementation",
                    "role": "reviewer",
                    "source_team": "base",
                    "target_team": "base",
                    "selector": "codex.reviewer",
                    "decision_number": 1,
                },
                "pending_boundary_decision": {
                    "finalized_turn_number": 1,
                    "decision_number": 1,
                    "action": "continue",
                    "proposed_action": "transition",
                    "proposed_transition": "review_cp_implementation",
                    "resolved_next_step": "review_cp_implementation",
                    "consumed": True,
                },
            }
        )
        durable_run = _current_resume_payload(durable_run)
        (run_dir / "run.json").write_text(
            json.dumps(durable_run),
            encoding="utf-8",
        )

        prepared = PreparedRun(
            workflow_name="saved_workflow",
            repo_root=repo_root,
            plan_path=Path(prev_run["original_plan_path"]),
            config_path=config_path,
            max_turns=15,
            team="base",
            extra_instructions=("keep the patch focused",),
            start_step="implement_plan",
        )
        captured: dict[str, object] = {}

        def execute(prepared_run, *, banner, resume, observer):
            captured["prepared_run"] = prepared_run
            captured["resume"] = resume
            captured["banner"] = banner
            captured["observer"] = observer
            return type("RunResult", (), {"turns_completed": 0, "end_reason": "done"})()

        with patch.object(
            cli_module,
            "_bootstrap_config_files",
            return_value=(config_path, ()),
        ), patch.object(
            cli_module,
            "_resolve_repo_root",
            return_value=repo_root,
        ), patch.object(
            cli_module,
            "load_workflow_config",
            return_value=workflow_config,
        ), patch.object(
            cli_module,
            "validate_workflow_config",
            return_value=[],
        ), patch.object(
            cli_module,
            "resolve_run_id",
            wraps=cli_module.resolve_run_id,
        ) as resolve_run_id, patch.object(
            cli_module,
            "load_run_json",
            wraps=cli_module.load_run_json,
        ) as load_run_json, patch.object(
            cli_module,
            "_handle_startup_questions",
            return_value=prepared,
        ) as startup, patch.object(
            cli_module,
            "BannerRenderer",
        ), patch.object(
            cli_module,
            "execute_workflow",
            side_effect=execute,
        ) as execute_workflow:
            status = cli_module.main(["run", "--resume", run_dir.name])

        assert status == 0
        startup.assert_called_once()
        request = startup.call_args.args[0]
        assert request.plan_path == Path(prev_run["original_plan_path"])
        assert request.workflow_name == "saved_workflow"
        assert request.max_turns == 15
        assert request.team == "base"
        assert request.start_step == "implement_plan"
        assert request.extra_instructions == ("keep the patch focused",)
        assert request.resume_requested is True
        resolve_run_id.assert_called_once_with(run_dir.name, repo_root)
        load_run_json.assert_called_once_with(run_dir)
        execute_workflow.assert_called_once()
        assert captured["prepared_run"] is prepared

        resume = captured["resume"]
        assert isinstance(resume, ResumeContext)
        assert resume.resumed_from_run_id == run_dir.name
        assert resume.feature_branch == "feature/saved-run"
        assert resume.worktree_path == repo_root / "worktree"
        assert resume.main_branch == "main"
        assert resume.setup == ("worktree", "branch")
        assert resume.teardown == ("merge", "rm_worktree")
        assert resume.active_plan_path == repair_path
        assert resume.frozen_run_identity is not None
        assert resume.frozen_run_identity.workflow_name == "saved_workflow"
        assert resume.frozen_run_identity.config_path == str(config_path)
        assert (
            resume.frozen_run_identity.config_fingerprint
            == frozen_identity.config_fingerprint
        )
        assert resume.pending_manager_notes is None
        assert resume.pending_step_team_override is None
        assert resume.pending_boundary_decision is None

        pending = resume.pending_finalized_turn
        assert pending is not None
        assert pending.source_run_dir == run_dir
        assert pending.turn_number == 2
        assert pending.step_name == "review_cp_implementation"
        assert pending.step_role == "reviewer"
        assert pending.selector == "codex.reviewer"
        assert pending.active_plan_path == Path(prev_run["original_plan_path"])
        assert pending.new_plan_path == repair_path
        assert pending.conditions == {
            "DONE": False,
            "NEW_PLAN_EXISTS": True,
            "MAX_TURNS_REACHED": False,
        }
        assert pending.chosen_transition == "implement_plan"
        assert pending.chosen_transition_condition == "NEW_PLAN_EXISTS || !DONE"
        assert pending.snapshot_after.current_checkpoint_name == "Checkpoint 2: Repair"
        assert pending.snapshot_after.current_checkpoint_index == 2
        assert pending.snapshot_after.unchecked_checkpoint_count == 1
        assert pending.snapshot_after.current_checkpoint_unchecked_step_count == 4
        assert pending.snapshot_after.total_checkpoint_count == 2
        assert pending.snapshot_after.is_complete is False
        assert result_path.read_bytes() == result_bytes
        assert sorted(path.name for path in run_dir.parent.iterdir()) == [run_dir.name]

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

    def test_run_parser_resume_without_id_selects_auto(self) -> None:
        args = build_parser().parse_args(['run', '--resume'])
        assert args.resume == 'AUTO'

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

    def _write_workflow_config(
        self,
        home_dir: Path,
        *,
        workflow_name: str,
        multi_step: bool,
        review_skill: bool = False,
    ) -> None:
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
        implementation_prompt = (
            'Use aflow-review-checkpoint with {ACTIVE_PLAN_PATH}.'
            if review_skill
            else 'Implement from {ACTIVE_PLAN_PATH}.'
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
                f'impl_prompt = "{implementation_prompt}"\n'
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

    def test_cli_pre_handoff_auto_refreshes_pristine_stale_base_without_input(self) -> None:
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
                         patch('builtins.input', side_effect=AssertionError('unexpected input')), \
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

    def test_cli_pre_handoff_auto_refreshes_pristine_empty_base_noninteractively(self) -> None:
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
                    with patch('sys.stdin.isatty', return_value=False), \
                         patch('sys.stdout.isatty', return_value=False), \
                         patch('builtins.input', side_effect=AssertionError('unexpected input')), \
                         patch('sys.stderr', stderr_capture):
                        result = main(['run', str(plan_path)])
            finally:
                os.chdir(original_cwd)
                cli_module.probe_worktree = original_probe
                startup_module.probe_worktree = original_startup_probe
            assert result == 0
            assert f'`{current_head}`' in plan_path.read_text(encoding='utf-8')
            assert 'startup aborted' not in stderr_capture.getvalue().lower()

    def test_cli_git_tracking_readme_minimal_review_plan_runs_noninteractively(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_root = tmp_path / 'repo'
            repo_root.mkdir()
            _make_lifecycle_git_repo(repo_root, branch='main')
            rc, current_head, _ = _run_git_in_test(['rev-parse', '--verify', 'HEAD'], cwd=repo_root)
            assert rc == 0
            home_dir = tmp_path / 'home'
            home_dir.mkdir()
            self._write_workflow_config(
                home_dir,
                workflow_name='single_step',
                multi_step=False,
                review_skill=True,
            )
            plan_path = repo_root / 'plan.md'
            completed_plan_path = repo_root / 'completed.md'
            count_file = repo_root / 'count.txt'
            _write_plan(plan_path, '# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n')
            _write_plan(
                completed_plan_path,
                (
                    '# Plan\n\n'
                    '## Git Tracking\n\n'
                    '- Plan Branch: ``\n'
                    f'- Pre-Handoff Base HEAD: `{current_head}`\n\n'
                    '### [x] Checkpoint 1: First\n'
                    '- [x] step one\n'
                ),
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

            assert result == 0
            final_text = plan_path.read_text(encoding='utf-8')
            assert final_text.count('## Git Tracking') == 1
            assert final_text.count('- Plan Branch: ``') == 1
            assert final_text.count(f'- Pre-Handoff Base HEAD: `{current_head}`') == 1
            run_dirs = list((repo_root / '.aflow' / 'runs').iterdir())
            assert len(run_dirs) == 1
            assert (run_dirs[0] / 'turns' / 'turn-001' / 'result.json').is_file()
            assert not (run_dirs[0] / 'turns' / 'turn-002').exists()
            assert 'startup aborted' not in stderr_capture.getvalue().lower()

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
        prev_run = _current_resume_payload(prev_run)

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
        prev_run = _current_resume_payload(prev_run)

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
            base_run = _current_resume_payload(base_run)
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
            prev_run = _current_resume_payload(prev_run)

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
                "envelope_artifact_path": "scopes/old-scope/envelope.json",
                "envelope_artifact_sha256": "a" * 64,
                "envelope_canonical_sha256": "b" * 64,
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
        prev_run = _current_resume_payload(prev_run)

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
            pending, artifacts, scope = _bound_pending_repartition_fixture(
                run_dir,
                plan_path,
            )
            candidate_rel = pending["candidate_artifact_path"]
            candidate_bytes = artifacts[candidate_rel]
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
                "manager_decision_number": pending["decision_number"],
                "active_implementation_scope": scope,
                "pending_repartition": pending,
            }
            prev_run = _current_resume_payload(prev_run)
            with patch(
                "aflow.cli.resolve_run_id",
                return_value=(run_dir, "explicit_run_id"),
            ), patch("aflow.cli.load_run_json", return_value=prev_run):
                result = cli_module._detect_resume_candidate(
                    repo_root=repo_root,
                    workflow_config=type(
                        "obj",
                        (object,),
                        {
                            "setup": ("worktree", "branch"),
                            "steps": {
                                "implement_plan": type(
                                    "Step", (), {"role": "worker"}
                                )()
                            },
                        },
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
            for relative, content in artifacts.items():
                assert result.repartition_artifact_bytes[relative] == content

    def test_resume_restores_pending_artifacts_after_source_pruning(self) -> None:
        import aflow.cli as cli_module
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            plan_path = repo_root / "plan.md"
            plan_path.write_text(_VALID_PLAN, encoding="utf-8")
            config_path = repo_root / "aflow.toml"
            config_path.write_text("", encoding="utf-8")
            run_dir = repo_root / ".aflow" / "runs" / "prior-run"
            pending, artifacts, scope = _bound_pending_repartition_fixture(
                run_dir,
                plan_path,
            )
            prev_run = {
                "repo_root": str(repo_root),
                "workflow_name": "test_workflow",
                "plan_path": str(plan_path),
                "team": None,
                "selected_start_step": None,
                "max_turns": 1,
                "extra_instructions": [],
                "lifecycle_setup": ["worktree", "branch"],
                "lifecycle_teardown": ["merge", "rm_worktree"],
                "feature_branch": "feature/repartition",
                "worktree_path": str(repo_root),
                "main_branch": "main",
                "status": "failed",
                "last_snapshot": {"is_complete": False},
                "manager_decision_number": pending["decision_number"],
                "active_implementation_scope": scope,
                "pending_repartition": pending,
            }
            prev_run = _current_resume_payload(prev_run)
            workflow_config = type(
                "WorkflowConfig",
                (),
                {
                    "workflows": {
                        "test_workflow": WorkflowConfig(
                            declared_steps={
                                "implement_plan": WorkflowStepConfig(
                                    role="worker",
                                    prompts=("p",),
                                    go=(GoTransition(to="END", when="DONE"),),
                                )
                            },
                            steps={
                                "implement_plan": WorkflowStepConfig(
                                    role="worker",
                                    prompts=("p",),
                                    go=(GoTransition(to="END", when="DONE"),),
                                )
                            },
                            first_step="implement_plan",
                            setup=("worktree", "branch"),
                            teardown=("merge", "rm_worktree"),
                        )
                    },
                    "aflow": AflowSection(default_workflow="test_workflow"),
                    "harnesses": {},
                    "roles": {},
                    "teams": {},
                    "prompts": {"p": "Work from {ACTIVE_PLAN_PATH}."},
                    "manager": ManagerConfig(),
                    "error_handling": ErrorHandlingConfig(),
                },
            )()

            with patch(
                "aflow.cli.resolve_run_id",
                return_value=(run_dir, "explicit_run_id"),
            ), patch("aflow.cli.load_run_json", return_value=prev_run):
                resume = cli_module._detect_resume_candidate(
                    repo_root=repo_root,
                    workflow_config=workflow_config.workflows["test_workflow"],
                    workflow_name="test_workflow",
                    plan_path=plan_path,
                    team=None,
                    selected_start_step=None,
                    max_turns=1,
                    extra_instructions=(),
                    requested_run_id=run_dir.name,
                    require_resume=True,
                )

            assert resume is not None
            assert resume.pending_repartition is not None
            resume = replace(
                resume,
                worktree_path=repo_root,
                setup=(),
                teardown=(),
                frozen_run_identity=_freeze_run_identity(
                    "test_workflow",
                    workflow_config,
                    config_dir=config_path,
                ),
                pending_repartition=replace(
                    resume.pending_repartition,
                    stage="proposed",
                ),
            )
            with patch(
                "aflow.workflow._validate_worktree_resume_context",
                return_value=None,
            ), pytest.raises(
                WorkflowError,
                match="pending repartition proposal/validation transaction",
            ):
                run_workflow(
                    ControllerConfig(
                        repo_root=repo_root,
                        plan_path=plan_path,
                        max_turns=1,
                        keep_runs=1,
                    ),
                    workflow_config,
                    "test_workflow",
                    config_dir=config_path,
                    adapter=CodexAdapter(),
                    resume=resume,
                )

            restored_runs = [
                path
                for path in (repo_root / ".aflow" / "runs").iterdir()
                if path.is_dir()
            ]
            assert len(restored_runs) == 1
            restored_run = restored_runs[0]
            assert not run_dir.exists()
            for relative, content in artifacts.items():
                assert (restored_run / relative).read_bytes() == content
            assert (restored_run / pending["candidate_artifact_path"]).read_bytes() == (
                artifacts[pending["candidate_artifact_path"]]
            )

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
                "reviewer_rejection_count": 1,
                "active_implementation_scope": None,
            }
            prev_run = _current_resume_payload(prev_run)

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
                "reviewer_rejection_count": 1,
                "active_implementation_scope": None,
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
            prev_run = _current_resume_payload(prev_run)

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

    def test_environment_preflight_resume_restores_blocked_workflow_step(
        self,
    ) -> None:
        import aflow.cli as cli_module

        prev_run = {
            "status": "failed",
            "failure_kind": "environment_preflight",
            "current_step_name": "review_cp_implementation",
            "active_turn": 1,
            "turns_completed": 1,
            "environment_preflight": {
                "classification": "harness_environment_preflight",
                "reason_code": "harness_executable_missing",
                "harness": "reasonix",
                "invocation_kind": "workflow_turn",
                "required_executable": "reasonix",
                "checked_command": ["reasonix"],
                "remediation": "Install Reasonix.",
                "safe_diagnostics": {},
                "step_name": "review_cp_implementation",
            },
        }

        assert cli_module._interrupted_resume_step(Path("unused"), prev_run) == (
            "review_cp_implementation"
        )

    def test_environment_preflight_resume_accepts_non_worktree_lifecycles(
        self,
    ) -> None:
        import aflow.cli as cli_module

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            plan_path = repo_root / "plan.md"
            plan_path.write_text(_VALID_PLAN, encoding="utf-8")
            run_dir = repo_root / ".aflow" / "runs" / "blocked-run"
            run_dir.mkdir(parents=True)
            base_run = {
                "repo_root": str(repo_root),
                "workflow_name": "test_workflow",
                "plan_path": str(plan_path),
                "team": None,
                "selected_start_step": None,
                "max_turns": 5,
                "extra_instructions": [],
                "status": "failed",
                "failure_kind": "environment_preflight",
                "current_step_name": "implement",
                "active_turn": 1,
                "turns_completed": 0,
                "last_snapshot": {"is_complete": False},
                "environment_preflight": {
                    "classification": "harness_environment_preflight",
                    "reason_code": "harness_executable_missing",
                    "harness": "reasonix",
                    "invocation_kind": "workflow_turn",
                    "required_executable": "reasonix",
                    "checked_command": ["reasonix"],
                    "remediation": "Install Reasonix.",
                    "safe_diagnostics": {},
                    "step_name": "implement",
                },
            }
            cases = (
                ("no_lifecycle", (), {}, None, None, None),
                (
                    "branch_only",
                    ("branch",),
                    {
                        "lifecycle_setup": ["branch"],
                        "lifecycle_teardown": ["merge"],
                        "feature_branch": "feature/resume",
                        "main_branch": "main",
                    },
                    "feature/resume",
                    "main",
                    None,
                ),
            )

            for name, setup, lifecycle_fields, feature, main, worktree in cases:
                with self.subTest(name=name):
                    prev_run = dict(base_run)
                    prev_run.update(lifecycle_fields)
                    prev_run = _current_resume_payload(prev_run)
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
                                "WorkflowConfig",
                                (),
                                {"setup": setup, "steps": {}},
                            )(),
                            workflow_name="test_workflow",
                            plan_path=plan_path,
                            team=None,
                            selected_start_step=None,
                            max_turns=5,
                            extra_instructions=(),
                            requested_run_id=run_dir.name,
                            require_resume=True,
                        )

                    assert result is not None
                    assert result.interrupted_step_name == "implement"
                    assert result.setup == setup
                    assert result.feature_branch == feature
                    assert result.main_branch == main
                    assert result.worktree_path == worktree

    def test_environment_preflight_manager_block_restores_finalized_boundary(
        self,
    ) -> None:
        import aflow.cli as cli_module

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            turn_dir = run_dir / "turns" / "turn-001"
            turn_dir.mkdir(parents=True)
            snapshot = {
                "current_checkpoint_index": 1,
                "current_checkpoint_name": "Checkpoint 1: Test",
                "current_checkpoint_unchecked_step_count": 1,
                "is_complete": False,
                "total_checkpoint_count": 1,
                "unchecked_checkpoint_count": 1,
            }
            (turn_dir / "result.json").write_text(
                json.dumps({
                    "turn_number": 1,
                    "status": "running",
                    "step_name": "implement",
                    "step_role": "worker",
                    "selector": "codex.worker",
                    "returncode": 0,
                    "active_plan_path": "/repo/plan.md",
                    "new_plan_path": "/repo/plan-cp1.md",
                    "snapshot_after": snapshot,
                    "conditions": {
                        "DONE": False,
                        "NEW_PLAN_EXISTS": False,
                        "MAX_TURNS_REACHED": False,
                    },
                    "chosen_transition": "review",
                    "chosen_transition_condition": None,
                }),
                encoding="utf-8",
            )
            prev_run = {
                "status": "failed",
                "failure_kind": "environment_preflight",
                "active_turn": 1,
                "turns_completed": 1,
                "current_step_name": "implement",
                "environment_preflight": {
                    "classification": "harness_environment_preflight",
                    "reason_code": "harness_executable_missing",
                    "harness": "reasonix",
                    "invocation_kind": "manager",
                    "required_executable": "reasonix",
                    "checked_command": ["reasonix"],
                    "remediation": "Install Reasonix.",
                    "safe_diagnostics": {},
                    "step_name": "implement",
                },
            }

            pending = cli_module._pending_finalized_resume_turn(run_dir, prev_run)

        assert pending is not None
        assert pending.turn_number == 1
        assert pending.step_name == "implement"
        assert pending.chosen_transition == "review"

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
        prev_run = _current_resume_payload(prev_run)

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
        prev_run = _current_resume_payload(prev_run)

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
        prev_run = _current_resume_payload(prev_run)

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
        prev_run = _current_resume_payload(prev_run)

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
        prev_run = _current_resume_payload(prev_run)

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
        prev_run = _current_resume_payload(prev_run)

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
        prev_run = _current_resume_payload(prev_run)
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
                run_payload = _current_resume_payload(run_payload)
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
        prev_run = _current_resume_payload(prev_run)

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

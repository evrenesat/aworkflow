"""Atomic, revision-checked project configuration text service tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from threading import Barrier, Event, Thread

import pytest

from aflow.api.models import PreparedRun, StartupQuestion, StartupQuestionKind
from aflow.control_plane import StartRunResult
from aflow.control_plane.units import InMemoryUnitManager
from aflow.daemon import AflowDaemon

from aflow_app_server import project_config_service
from aflow_app_server.control_plane_service import ControlPlaneService
from aflow_app_server.project_config_service import (
    ProjectConfigError,
    ProjectConfigRevisionConflict,
    ProjectConfigService,
    combined_revision,
    document_path,
    validate_candidate_pair,
)
from aflow_app_server.project_registry import ProjectRegistry

PROJECT_ID = "alpha"


def _release_inputs(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    environment_file = tmp_path / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    return executable, environment_file


def _git(target: Path, *argv: str) -> None:
    subprocess.run(
        ("git", "-C", str(target), *argv),
        check=True,
        capture_output=True,
    )


def _valid_pair(
    *, model: str = "test-model", workflow: str = "deliver"
) -> tuple[str, str]:
    aflow = f"""[aflow]
default_workflow = "{workflow}"

[harness.codex.profiles.fast]
model = "{model}"

[roles]
worker = "codex.fast"

[prompts]
p = "Work."
"""
    workflows = f"""[workflow.{workflow}.steps.implement]
role = "worker"
prompts = ["p"]
go = [{{ to = "END", when = "DONE" }}]
"""
    return aflow, workflows


def _write_valid_config(root: Path, **kwargs: str) -> tuple[str, str]:
    aflow_text, workflows_text = _valid_pair(**kwargs)
    config_dir = root / ".aflow" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "aflow.toml").write_text(aflow_text, encoding="utf-8")
    (config_dir / "workflows.toml").write_text(workflows_text, encoding="utf-8")
    return aflow_text, workflows_text


def _env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial: str | None = "starter",
) -> tuple[
    ProjectConfigService,
    ProjectRegistry,
    ControlPlaneService,
    Path,
    InMemoryUnitManager,
]:
    managed = tmp_path / "managed"
    managed.mkdir()
    registry = ProjectRegistry(managed, tmp_path / "registry.json")
    root = registry.managed_root / PROJECT_ID
    root.mkdir()
    _git(root, "init", "-q")
    (root / "keep.txt").write_text("history")
    _git(root, "add", "keep.txt")
    _git(
        root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "prior"
    )
    if initial == "starter":
        (root / ".aflow" / "config").mkdir(parents=True)
        from aflow.config import bootstrap_project_config

        bootstrap_project_config(root / ".aflow" / "config" / "aflow.toml")
    elif initial == "valid":
        _write_valid_config(root)
    registry.register(PROJECT_ID, "Alpha", PROJECT_ID)
    executable, environment_file = _release_inputs(tmp_path)
    units = InMemoryUnitManager()
    control = ControlPlaneService(
        registry,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
        daemon_factory=lambda config: AflowDaemon(config, units=units),
        workflow_config_path=root / ".aflow" / "config" / "aflow.toml",
    )
    control.start()
    service = ProjectConfigService(
        registry,
        control,
        audit_path=tmp_path / "state" / "config_audit.jsonl",
    )
    return service, registry, control, root, units


def _audit_records(tmp_path: Path) -> list[dict[str, object]]:
    audit_path = tmp_path / "state" / "config_audit.jsonl"
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestRead:
    def test_read_returns_exact_texts_revision_and_ready_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        aflow_text, workflows_text = _valid_pair()

        snapshot = service.read(PROJECT_ID)

        assert snapshot.project_id == PROJECT_ID
        assert snapshot.documents == ("aflow.toml", "workflows.toml")
        assert snapshot.aflow_toml == aflow_text
        assert snapshot.workflows_toml == workflows_text
        assert snapshot.revision == combined_revision(
            aflow_text.encode("utf-8"), workflows_text.encode("utf-8")
        )
        assert snapshot.validation.state == "ready"
        assert snapshot.validation.workflows == ("deliver",)
        assert snapshot.validation.roles == ("worker",)

    def test_read_missing_documents_reports_configuration_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, _, _ = _env(tmp_path, monkeypatch, initial=None)

        snapshot = service.read(PROJECT_ID)

        assert snapshot.aflow_toml == ""
        assert snapshot.workflows_toml == ""
        assert snapshot.revision == combined_revision(b"", b"")
        assert snapshot.validation.state == "configuration_required"

    def test_read_rejects_symlinked_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        config_dir = root / ".aflow" / "config"
        outside = tmp_path / "outside.toml"
        outside.write_text("[aflow]\n", encoding="utf-8")
        (config_dir / "aflow.toml").unlink()
        (config_dir / "aflow.toml").symlink_to(outside)

        with pytest.raises(ProjectConfigError, match="symlink"):
            service.read(PROJECT_ID)

    def test_read_rejects_hard_linked_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        config_dir = root / ".aflow" / "config"
        (config_dir / "aflow.toml").unlink()
        source = tmp_path / "linked.toml"
        source.write_text("[aflow]\n", encoding="utf-8")
        os.link(source, config_dir / "aflow.toml")

        with pytest.raises(ProjectConfigError, match="hard-linked"):
            service.read(PROJECT_ID)

    def test_read_rejects_non_regular_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        config_dir = root / ".aflow" / "config"
        (config_dir / "workflows.toml").unlink()
        os.mkfifo(config_dir / "workflows.toml")

        with pytest.raises(ProjectConfigError, match="regular file"):
            service.read(PROJECT_ID)

    def test_read_rejects_non_directory_config_component(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial=None)
        (root / ".aflow").write_text("not a directory", encoding="utf-8")

        with pytest.raises(ProjectConfigError, match="path component"):
            service.read(PROJECT_ID)

    def test_read_rejects_invalid_utf8(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        (root / ".aflow" / "config" / "aflow.toml").write_bytes(b"\xff\xfe\x00bad")

        with pytest.raises(ProjectConfigError, match="UTF-8"):
            service.read(PROJECT_ID)

    def test_read_rejects_oversized_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        monkeypatch.setattr(project_config_service, "MAX_CONFIG_DOCUMENT_BYTES", 16)
        (root / ".aflow" / "config" / "aflow.toml").write_text(
            "x" * 64, encoding="utf-8"
        )

        with pytest.raises(ProjectConfigError, match="maximum supported size"):
            service.read(PROJECT_ID)

    def test_unsupported_document_names_are_rejected(self) -> None:
        with pytest.raises(
            ProjectConfigError, match="unsupported configuration document"
        ):
            document_path(Path("/anywhere"), "secrets.env")


class TestValidateCandidate:
    def test_validate_reports_syntax_line_diagnostics_per_document(self) -> None:
        report = validate_candidate_pair("[aflow\ndefault_workflow = ", "")

        assert report.state == "invalid"
        assert report.issues[0].document == "aflow.toml"
        assert report.issues[0].line == 1
        assert "/tmp" not in report.issues[0].message
        assert "<candidate>" not in report.issues[0].message

    def test_validate_reports_cross_document_semantic_errors(
        self, tmp_path: Path
    ) -> None:
        aflow_text, _ = _valid_pair()
        broken_workflows = """[workflow.deliver.steps.implement]
role = "missing-role"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]
"""

        report = validate_candidate_pair(aflow_text, broken_workflows)

        assert report.state == "invalid"
        assert any("missing-role" in issue.message for issue in report.issues)

    def test_validate_reports_placeholders_as_configuration_required(
        self, tmp_path: Path
    ) -> None:
        from aflow.config import bootstrap_project_config

        scratch = tmp_path / "starter-scratch"
        scratch.mkdir()
        bootstrap_project_config(scratch / "aflow.toml")
        report = validate_candidate_pair(
            (scratch / "aflow.toml").read_text(encoding="utf-8"),
            (scratch / "workflows.toml").read_text(encoding="utf-8"),
        )

        assert report.state == "configuration_required"
        assert "harness.starter.profiles.default.model" in report.placeholders
        assert report.workflows == ("implement",)

    def test_validate_accepts_ready_pair_and_reports_parsed_names(self) -> None:
        aflow_text, workflows_text = _valid_pair()

        report = validate_candidate_pair(aflow_text, workflows_text)

        assert report.state == "ready"
        assert report.issues == ()
        assert report.workflows == ("deliver",)
        assert report.teams == ()
        assert report.roles == ("worker",)


class TestSave:
    def test_save_commits_pair_atomically_and_returns_new_revision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="starter")
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair()

        snapshot = service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)

        config_dir = root / ".aflow" / "config"
        assert (config_dir / "aflow.toml").read_text(encoding="utf-8") == aflow_text
        assert (config_dir / "workflows.toml").read_text(
            encoding="utf-8"
        ) == workflows_text
        assert snapshot.revision == combined_revision(
            aflow_text.encode("utf-8"), workflows_text.encode("utf-8")
        )
        assert snapshot.validation.state == "ready"
        assert list(config_dir.glob("*.tmp")) == []

    def test_save_creates_missing_documents_for_unconfigured_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, control, root, _ = _env(tmp_path, monkeypatch, initial=None)
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair()

        snapshot = service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)

        assert snapshot.revision != before.revision
        assert control.capabilities(PROJECT_ID).workflows == ("deliver",)
        assert (root / ".aflow" / "config" / "workflows.toml").exists()

    def test_save_ignores_legacy_history_when_daemon_cannot_compose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial=None)
        invalid_aflow = root / ".aflow" / "config" / "aflow.toml"
        invalid_aflow.parent.mkdir(parents=True)
        invalid_aflow.write_text("[aflow\\n", encoding="utf-8")
        legacy_run = root / ".aflow" / "runs" / "20260905T120000Z-01234567"
        legacy_run.mkdir(parents=True)
        (legacy_run / "run.json").write_text(
            json.dumps({"status": "running", "workflow_name": "legacy"}),
            encoding="utf-8",
        )
        aflow_text, workflows_text = _valid_pair()

        snapshot = service.save(
            PROJECT_ID,
            aflow_text,
            workflows_text,
            combined_revision(invalid_aflow.read_bytes(), b""),
        )

        assert snapshot.validation.state == "ready"

    def test_save_rejects_stale_revision_and_preserves_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair(model="changed-model")

        with pytest.raises(ProjectConfigRevisionConflict) as exc_info:
            service.save(
                PROJECT_ID,
                aflow_text,
                workflows_text,
                "0" * 64,
            )

        assert exc_info.value.current_revision == before.revision
        config_dir = root / ".aflow" / "config"
        assert (config_dir / "aflow.toml").read_text(
            encoding="utf-8"
        ) == before.aflow_toml
        assert (config_dir / "workflows.toml").read_text(
            encoding="utf-8"
        ) == before.workflows_toml

    def test_save_rejects_paired_semantic_failure_and_preserves_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        _, workflows_text = _valid_pair()
        broken_workflows = workflows_text.replace('role = "worker"', 'role = "ghost"')

        with pytest.raises(ProjectConfigError, match="invalid"):
            service.save(
                PROJECT_ID, before.aflow_toml, broken_workflows, before.revision
            )

        config_dir = root / ".aflow" / "config"
        assert (config_dir / "workflows.toml").read_text(
            encoding="utf-8"
        ) == before.workflows_toml
        assert service.read(PROJECT_ID).revision == before.revision

    def test_save_rejects_placeholder_configuration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        from aflow.config import bootstrap_project_config

        scratch = tmp_path / "starter-scratch"
        scratch.mkdir()
        bootstrap_project_config(scratch / "aflow.toml")
        starter_aflow = (scratch / "aflow.toml").read_text(encoding="utf-8")
        starter_workflows = (scratch / "workflows.toml").read_text(encoding="utf-8")

        with pytest.raises(ProjectConfigError, match="placeholder"):
            service.save(PROJECT_ID, starter_aflow, starter_workflows, before.revision)

        config_dir = root / ".aflow" / "config"
        assert (config_dir / "aflow.toml").read_text(
            encoding="utf-8"
        ) == before.aflow_toml

    def test_save_rejects_malformed_expected_revision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, _, _ = _env(tmp_path, monkeypatch, initial="valid")
        aflow_text, workflows_text = _valid_pair()

        with pytest.raises(ProjectConfigError, match="expected_revision"):
            service.save(PROJECT_ID, aflow_text, workflows_text, "not-a-revision")

    def test_save_allowed_while_run_awaits_startup_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, control, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        plan = root / "plans" / "todo" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n### [ ] Checkpoint 1: Do\n- [ ] step\n")
        monkeypatch.setattr(
            "aflow.daemon.prepare_startup",
            lambda request: StartupQuestion(
                kind=StartupQuestionKind.PICK_STEP,
                message="Choose a step",
                choices=["implement"],
            ),
        )
        pending = control.start_run(
            PROJECT_ID,
            plan_path="plans/todo/plan.md",
            workflow_name="deliver",
            team=None,
            start_step=None,
            max_turns=None,
            idempotency_key="start-await",
        )
        assert hasattr(pending, "question_id")
        assert pending.run_id is not None
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair(model="updated-model")

        snapshot = service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)

        assert snapshot.revision != before.revision
        assert service.read(PROJECT_ID).aflow_toml == aflow_text

    def test_save_allowed_while_running_then_owner_stop_remains_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, control, root, units = _env(tmp_path, monkeypatch, initial="valid")
        plan = root / "plans" / "todo" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n\n### [ ] Checkpoint 1: Do\n- [ ] step\n")

        def prepared(request: PreparedRun) -> PreparedRun:
            return PreparedRun(
                workflow_name="deliver",
                repo_root=request.repo_root,
                plan_path=request.plan_path,
                config_path=request.config_path,
                max_turns=request.max_turns or 15,
                team=request.team,
                extra_instructions=(),
                start_step=request.start_step or "implement",
            )

        monkeypatch.setattr(
            "aflow.daemon.prepare_startup",
            lambda request: StartupQuestion(
                kind=StartupQuestionKind.PICK_STEP,
                message="Choose a step",
                choices=["implement"],
            ),
        )
        monkeypatch.setattr(
            "aflow.daemon.prepare_startup_with_answer",
            lambda _question, request, _answer: prepared(request),
        )
        pending = control.start_run(
            PROJECT_ID,
            plan_path="plans/todo/plan.md",
            workflow_name="deliver",
            team=None,
            start_step=None,
            max_turns=None,
            idempotency_key="start-running",
        )
        assert hasattr(pending, "question_id")
        assert pending.run_id is not None
        run_id = pending.run_id
        answered = control.answer_startup(
            PROJECT_ID,
            pending.question_id,
            "implement",
            idempotency_key="answer-running",
        )
        assert isinstance(answered, StartRunResult)
        assert answered.status == "running"
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair(model="post-run-model")

        snapshot = service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)
        assert snapshot.revision != before.revision

        control.owner_stop(
            PROJECT_ID, run_id, expected_revision=0, idempotency_key="stop-running"
        )

    def test_second_file_failure_restores_previous_bytes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair(model="rolled-back-model")
        real_replace = os.replace

        def failing_replace(src: object, dst: object, *args: object) -> None:
            if Path(str(dst)).name == "workflows.toml":
                raise OSError("simulated second-file replacement failure")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(project_config_service.os, "replace", failing_replace)

        with pytest.raises(ProjectConfigError, match="restored"):
            service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)

        monkeypatch.undo()
        config_dir = root / ".aflow" / "config"
        assert (config_dir / "aflow.toml").read_text(
            encoding="utf-8"
        ) == before.aflow_toml
        assert (config_dir / "workflows.toml").read_text(
            encoding="utf-8"
        ) == before.workflows_toml
        assert service.read(PROJECT_ID).revision == before.revision
        assert list(config_dir.glob("*.tmp")) == []

    def test_second_file_failure_restores_absent_workflows_document(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        config_dir = root / ".aflow" / "config"
        (config_dir / "workflows.toml").unlink()
        before = service.read(PROJECT_ID)
        assert before.workflows_toml == ""
        aflow_text, workflows_text = _valid_pair(model="half-saved-model")
        real_replace = os.replace

        def failing_replace(src: object, dst: object, *args: object) -> None:
            if Path(str(dst)).name == "workflows.toml":
                raise OSError("simulated second-file replacement failure")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(project_config_service.os, "replace", failing_replace)

        with pytest.raises(ProjectConfigError, match="restored"):
            service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)

        monkeypatch.undo()
        assert not (config_dir / "workflows.toml").exists()
        assert (config_dir / "aflow.toml").read_text(
            encoding="utf-8"
        ) == before.aflow_toml
        assert service.read(PROJECT_ID).revision == before.revision

    def test_concurrent_writers_only_one_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, root, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        barrier = Barrier(2)
        outcomes: dict[str, object] = {}

        def attempt(tag: str, model: str) -> None:
            aflow_text, workflows_text = _valid_pair(model=model)
            barrier.wait()
            try:
                service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)
            except ProjectConfigRevisionConflict:
                outcomes[tag] = "conflict"
            else:
                outcomes[tag] = "saved"

        first = Thread(target=attempt, args=("first", "winner-model"))
        second = Thread(target=attempt, args=("second", "loser-model"))
        first.start()
        second.start()
        first.join(timeout=30)
        second.join(timeout=30)

        assert sorted(outcomes.values()) == ["conflict", "saved"]
        winner_model = "winner-model" if outcomes["first"] == "saved" else "loser-model"
        winner_aflow, winner_workflows = _valid_pair(model=winner_model)
        expected_revision = combined_revision(
            winner_aflow.encode("utf-8"), winner_workflows.encode("utf-8")
        )
        config_dir = root / ".aflow" / "config"
        assert (config_dir / "aflow.toml").read_text(encoding="utf-8") == winner_aflow
        assert (config_dir / "workflows.toml").read_text(
            encoding="utf-8"
        ) == winner_workflows
        assert service.read(PROJECT_ID).revision == expected_revision

    def test_save_is_serialized_per_project_with_distinct_locks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, control, _, _ = _env(tmp_path, monkeypatch, initial="valid")
        first = control.project_lock(PROJECT_ID)
        second = control.project_lock(PROJECT_ID)
        other = control.project_lock("beta")
        assert first is second
        assert first is not other

    def test_read_never_observes_torn_pair_during_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, _, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair(model="paired-model")
        first_replaced, release_save, read_finished = Event(), Event(), Event()
        outcomes: dict[str, object] = {}
        real_replace = os.replace

        def pausing_replace(src: object, dst: object, *args: object) -> None:
            real_replace(src, dst)
            if Path(str(dst)).name == "aflow.toml":
                first_replaced.set()
                assert release_save.wait(timeout=30)

        monkeypatch.setattr(project_config_service.os, "replace", pausing_replace)
        saver = Thread(
            target=lambda: outcomes.setdefault(
                "save",
                service.save(PROJECT_ID, aflow_text, workflows_text, before.revision),
            )
        )
        saver.start()
        assert first_replaced.wait(timeout=30)
        reader = Thread(
            target=lambda: (
                outcomes.setdefault("read", service.read(PROJECT_ID)),
                read_finished.set(),
            )
        )
        reader.start()
        assert not read_finished.wait(timeout=0.1)
        release_save.set()
        saver.join(timeout=30)
        reader.join(timeout=30)
        assert not saver.is_alive() and not reader.is_alive()
        snapshot = outcomes["read"]
        assert isinstance(snapshot, type(before))
        assert (snapshot.aflow_toml, snapshot.workflows_toml) in {
            (before.aflow_toml, before.workflows_toml),
            (aflow_text, workflows_text),
        }

class TestAuditAndReload:
    def test_audit_records_are_bounded_and_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, _, _, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        marker_model = "secret-model-value"
        marker_prompt = "CONFIDENTIAL-PROMPT-MARKER"
        aflow_text, workflows_text = _valid_pair(model=marker_model)
        aflow_text = aflow_text.replace("Work.", marker_prompt)

        service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)
        with pytest.raises(ProjectConfigRevisionConflict):
            service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)

        records = _audit_records(tmp_path)
        assert [record["outcome"] for record in records] == [
            "saved",
            "revision_conflict",
        ]
        audit_path = tmp_path / "state" / "config_audit.jsonl"
        audit_bytes = audit_path.read_text(encoding="utf-8")
        assert marker_model not in audit_bytes
        assert marker_prompt not in audit_bytes
        assert "test-model" not in audit_bytes
        assert before.aflow_toml not in audit_bytes
        for record in records:
            assert set(record) == {
                "schema_version",
                "timestamp",
                "project_id",
                "outcome",
                "old_revision",
                "new_revision",
                "caller_scope",
            }
            assert record["project_id"] == PROJECT_ID
            assert record["caller_scope"] == "rest"
        saved = records[0]
        assert saved["old_revision"] == before.revision
        assert saved["new_revision"] != before.revision
        assert records[1]["new_revision"] is None

    def test_failed_saves_do_not_alter_capabilities(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, control, _, _ = _env(tmp_path, monkeypatch, initial="valid")
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair(workflow="revised")
        service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)
        assert control.capabilities(PROJECT_ID).workflows == ("revised",)

        stale = service.read(PROJECT_ID)
        broken_workflows = workflows_text.replace('role = "worker"', 'role = "ghost"')
        with pytest.raises(ProjectConfigError, match="invalid"):
            service.save(PROJECT_ID, aflow_text, broken_workflows, stale.revision)

        assert control.capabilities(PROJECT_ID).workflows == ("revised",)

    def test_successful_save_reloads_capabilities_from_committed_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, control, _, _ = _env(tmp_path, monkeypatch, initial="valid")
        assert control.capabilities(PROJECT_ID).workflows == ("deliver",)
        cached_before = control._projects[PROJECT_ID].daemon
        before = service.read(PROJECT_ID)
        aflow_text, workflows_text = _valid_pair(workflow="ship")

        service.save(PROJECT_ID, aflow_text, workflows_text, before.revision)

        assert control.capabilities(PROJECT_ID).workflows == ("ship",)
        cached_after = control._projects[PROJECT_ID].daemon
        assert cached_after is not cached_before

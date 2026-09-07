"""Safe project creation, registration, and unregister tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from aflow.config import load_workflow_config, project_configuration_state

from aflow_app_server.config import ServerConfig
from aflow_app_server.control_plane_service import ControlPlaneService
from aflow_app_server import main as main_module
from aflow_app_server.project_registry import ProjectRegistry
from aflow_app_server.project_service import (
    ProjectRequest,
    ProjectService,
    ProjectServiceError,
)


def _release_inputs(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    environment_file = tmp_path / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    return executable, environment_file


def _service(tmp_path: Path, control_plane: ControlPlaneService | None = None) -> tuple[ProjectService, ProjectRegistry, Path]:
    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    return ProjectService(registry, control_plane), registry, managed


def _create(
    service: ProjectService,
    path: str = "alpha",
    *,
    mode: str = "create",
    display_name: str | None = None,
    main_branch: str = "trunk",
    initial_workflow: str | None = None,
    initial_team: str | None = None,
    initialize_git: bool = False,
    initialize_config: bool = False,
) -> dict[str, object]:
    return service.create_or_register(
        ProjectRequest(
            mode=mode,  # type: ignore[arg-type]
            relative_path=path,
            display_name=display_name,
            main_branch=main_branch,
            initial_workflow=initial_workflow,
            initial_team=initial_team,
            initialize_git=initialize_git,
            initialize_config=initialize_config,
        )
    )


def _git(target: Path, *argv: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(target), *argv),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _committed_repo(root: Path, name: str, file_body: str = "history") -> Path:
    project = root / name
    project.mkdir(parents=True)
    subprocess.run(("git", "init", "-q", str(project)), check=True)
    (project / "keep.txt").write_text(file_body)
    _git(project, "add", "keep.txt")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "prior")
    return project


def _failing_register(self, project_id, display_name, relative_root):  # type: ignore[no-untyped-def]
    from aflow_app_server.project_registry import ProjectRegistryError

    raise ProjectRegistryError("registry rejected")


class TestCreate:
    def test_create_builds_verified_repository_and_starter_config(
        self, tmp_path: Path
    ) -> None:
        service, registry, managed = _service(tmp_path)
        result = _create(
            service,
            "alpha",
            display_name="Alpha",
            initial_workflow="build",
            initial_team="crew",
        )

        root = managed / "alpha"
        assert result["id"] == "alpha"
        assert result["readiness"] == "configuration_required"
        assert registry.get("alpha") is not None
        assert _git(root, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"
        assert _git(root, "log", "--oneline").count("\n") == 0
        assert _git(root, "status", "--porcelain") == ""
        assert (root / ".gitignore").read_text() == ".aflow/\n"
        assert sorted(_git(root, "ls-files").splitlines()) == [".gitignore", "README.md"]

        config_dir = root / ".aflow" / "config"
        aflow_text = (config_dir / "aflow.toml").read_text()
        workflows_text = (config_dir / "workflows.toml").read_text()
        assert 'default_workflow = "build"' in aflow_text
        assert '[teams."crew".roles]' in aflow_text
        assert '[workflow.build]' in workflows_text
        assert 'team = "crew"' in workflows_text
        assert "codex" not in aflow_text.lower()
        workflows_config = load_workflow_config(config_dir / "aflow.toml")
        assert workflows_config.workflows["build"].main_branch == "trunk"
        assert project_configuration_state(config_dir / "aflow.toml") == "configuration_required"

    def test_create_uses_starter_default_workflow_and_registers_immediately(
        self, tmp_path: Path
    ) -> None:
        service, registry, _ = _service(tmp_path)
        result = _create(service, "beta")

        assert result["readiness"] == "configuration_required"
        config_dir = (tmp_path / "managed" / "beta" / ".aflow" / "config")
        assert 'default_workflow = "implement"' in (config_dir / "aflow.toml").read_text()
        assert registry.get("beta") is not None
        assert service.readiness("beta") == "configuration_required"

    def test_create_nested_path_builds_in_existing_parent(self, tmp_path: Path) -> None:
        service, registry, managed = _service(tmp_path)
        (managed / "clients").mkdir()

        result = _create(service, "clients/acme")

        root = managed / "clients" / "acme"
        assert result["id"] == "acme"
        assert registry.get("acme") is not None
        assert _git(root, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"
        assert _git(root, "status", "--porcelain") == ""
        workflows_config = load_workflow_config(
            root / ".aflow" / "config" / "aflow.toml"
        )
        assert workflows_config.workflows["implement"].main_branch == "trunk"
        assert sorted(p.name for p in (managed / "clients").iterdir()) == ["acme"]

    def test_create_nested_missing_parent_is_rejected_without_tree_creation(
        self, tmp_path: Path
    ) -> None:
        service, _, managed = _service(tmp_path)

        with pytest.raises(ProjectServiceError, match="parent"):
            _create(service, "clients/deep/acme")
        assert not (managed / "clients").exists()
        assert list(managed.glob(".aflow-create-*")) == []


class TestRegister:
    def test_register_existing_repository_preserves_history_and_config(
        self, tmp_path: Path
    ) -> None:
        service, registry, managed = _service(tmp_path)
        project = _committed_repo(managed, "gamma", "precious")
        (project / ".aflow" / "config").mkdir(parents=True)
        (project / ".aflow" / "config" / "aflow.toml").write_text("# kept\n")
        before = subprocess.run(
            ("git", "-C", str(project), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        result = _create(service, "gamma", mode="register")

        assert result["id"] == "gamma"
        assert (project / ".aflow" / "config" / "aflow.toml").read_text() == "# kept\n"
        assert (project / ".aflow" / "config" / "workflows.toml").exists() is False
        assert _git(project, "rev-parse", "HEAD") == before.strip()
        assert _git(project, "status", "--porcelain") == ""
        assert registry.get("gamma") is not None

    def test_register_initialize_config_refuses_existing_documents(
        self, tmp_path: Path
    ) -> None:
        service, registry, managed = _service(tmp_path)
        project = _committed_repo(managed, "gamma")
        (project / ".aflow" / "config").mkdir(parents=True)
        (project / ".aflow" / "config" / "aflow.toml").write_text("# kept\n")

        with pytest.raises(ProjectServiceError, match="refusing overwrite"):
            _create(service, "gamma", mode="register", initialize_config=True)
        assert registry.get("gamma") is None
        assert (project / ".aflow" / "config" / "aflow.toml").read_text() == "# kept\n"

    def test_register_non_git_requires_explicit_initialize_git(
        self, tmp_path: Path
    ) -> None:
        service, _, managed = _service(tmp_path)
        (managed / "plain").mkdir()

        with pytest.raises(ProjectServiceError, match="initialize_git"):
            _create(service, "plain", mode="register")
        assert not (managed / "plain" / ".git").exists()

    def test_register_opt_in_git_init_requires_empty_directory(
        self, tmp_path: Path
    ) -> None:
        service, _, managed = _service(tmp_path)
        (managed / "hollow").mkdir()
        (managed / "hollow" / "existing.txt").write_text("keep")

        with pytest.raises(ProjectServiceError, match="empty"):
            _create(service, "hollow", mode="register", initialize_git=True)
        assert (managed / "hollow" / "existing.txt").read_text() == "keep"
        assert not (managed / "hollow" / ".git").exists()

    def test_register_opt_in_git_init_on_empty_directory(
        self, tmp_path: Path
    ) -> None:
        service, _, managed = _service(tmp_path)
        (managed / "fresh").mkdir()
        result = _create(
            service,
            "fresh",
            mode="register",
            initialize_git=True,
            initialize_config=True,
            initial_workflow="build",
        )

        root = managed / "fresh"
        assert result["readiness"] == "configuration_required"
        assert _git(root, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"
        assert _git(root, "status", "--porcelain") == ""
        assert (root / ".aflow" / "config" / "aflow.toml").exists()

    def test_register_unborn_head_is_rejected_and_repository_preserved(
        self, tmp_path: Path
    ) -> None:
        service, registry, managed = _service(tmp_path)
        project = managed / "unborn"
        project.mkdir()
        subprocess.run(("git", "init", "-q", str(project)), check=True)
        (project / "keep.txt").write_text("untouched")

        def snapshot() -> tuple[list[str], str]:
            files = sorted(
                str(path.relative_to(project)) for path in project.rglob("*")
            )
            status = subprocess.run(
                ("git", "-C", str(project), "status", "--porcelain"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            return files, status

        before = snapshot()
        with pytest.raises(ProjectServiceError, match="commit HEAD"):
            _create(service, "unborn", mode="register")

        assert snapshot() == before
        head = subprocess.run(
            ("git", "-C", str(project), "rev-parse", "--verify", "--quiet", "HEAD"),
            capture_output=True,
            text=True,
        )
        assert head.returncode != 0
        assert registry.get("unborn") is None


class TestRejectionsAndRollback:
    def test_create_collision_preserves_existing_target(self, tmp_path: Path) -> None:
        service, _, managed = _service(tmp_path)
        target = managed / "alpha"
        target.mkdir()
        (target / "existing.txt").write_bytes(b"precious")

        with pytest.raises(ProjectServiceError, match="already exists"):
            _create(service, "alpha")
        assert (target / "existing.txt").read_bytes() == b"precious"
        assert list(managed.glob(".aflow-create-*")) == []

    @pytest.mark.parametrize("path", ["../outside", "/absolute", "./alpha", "a//b", ""])
    def test_traversal_and_noncanonical_paths_are_rejected(
        self, tmp_path: Path, path: str
    ) -> None:
        service, _, _ = _service(tmp_path)
        with pytest.raises(ProjectServiceError):
            _create(service, path)

    def test_symlink_component_is_rejected(self, tmp_path: Path) -> None:
        service, _, managed = _service(tmp_path)
        real = _committed_repo(managed, "real")
        (managed / "alias").symlink_to(real, target_is_directory=True)

        with pytest.raises(ProjectServiceError, match="symlink"):
            _create(service, "alias", mode="register")

    def test_registered_roots_conflict_before_any_work(self, tmp_path: Path) -> None:
        service, _, managed = _service(tmp_path)
        _create(service, "alpha")
        (managed / "alpha" / "nested").mkdir()

        with pytest.raises(ProjectServiceError, match="already registered"):
            _create(service, "alpha")
        with pytest.raises(ProjectServiceError, match="overlapping"):
            _create(service, "alpha/nested", mode="register")

    def test_git_failure_rolls_back_new_directory_only(self, tmp_path: Path) -> None:
        service, _, managed = _service(tmp_path)
        original_run = subprocess.run

        def failing_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
            if argv[0:2] == ("git", "init"):
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            return original_run(argv, *args, **kwargs)

        subprocess.run = failing_run  # type: ignore[assignment]
        try:
            with pytest.raises(ProjectServiceError):
                _create(service, "doomed")
        finally:
            subprocess.run = original_run  # type: ignore[assignment]
        assert not (managed / "doomed").exists()
        assert list(managed.glob(".aflow-create-*")) == []

    def test_git_timeout_is_bounded_and_reported(self, tmp_path: Path) -> None:
        service, _, _ = _service(tmp_path)
        original_run = subprocess.run

        def slow_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=15)

        subprocess.run = slow_run  # type: ignore[assignment]
        try:
            with pytest.raises(ProjectServiceError):
                _create(service, "slow")
        finally:
            subprocess.run = original_run  # type: ignore[assignment]

    def test_config_failure_leaves_no_record_or_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aflow_app_server import project_service as project_service_module

        service, registry, managed = _service(tmp_path)

        def failing_bootstrap(*args, **kwargs):  # type: ignore[no-untyped-def]
            from aflow.config import ConfigError

            raise ConfigError("starter unavailable")

        monkeypatch.setattr(
            project_service_module, "bootstrap_project_config", failing_bootstrap
        )
        with pytest.raises(ProjectServiceError):
            _create(service, "noconfig")
        assert registry.get("noconfig") is None
        assert not (managed / "noconfig").exists()
        assert list(managed.glob(".aflow-create-*")) == []

    def test_registry_failure_after_rename_removes_only_new_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, registry, managed = _service(tmp_path)
        monkeypatch.setattr(ProjectRegistry, "register", _failing_register)
        with pytest.raises(Exception, match="registry rejected"):
            _create(service, "ghost")
        assert registry.get("ghost") is None
        assert not (managed / "ghost").exists()
        assert list(managed.glob(".aflow-create-*")) == []

    def test_registry_failure_after_initialize_config_restores_existing_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, registry, managed = _service(tmp_path)
        project = _committed_repo(managed, "gamma")
        head = _git(project, "rev-parse", "HEAD")
        monkeypatch.setattr(ProjectRegistry, "register", _failing_register)

        with pytest.raises(Exception, match="registry rejected"):
            _create(service, "gamma", mode="register", initialize_config=True)

        assert registry.get("gamma") is None
        assert not (project / ".aflow").exists()
        assert _git(project, "rev-parse", "HEAD") == head
        assert _git(project, "status", "--porcelain") == ""

    def test_registry_failure_after_initialize_config_keeps_preexisting_aflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, registry, managed = _service(tmp_path)
        project = _committed_repo(managed, "delta")
        (project / ".aflow" / "runs").mkdir(parents=True)
        (project / ".aflow" / "runs" / "marker").write_text("keep")
        monkeypatch.setattr(ProjectRegistry, "register", _failing_register)

        with pytest.raises(Exception, match="registry rejected"):
            _create(service, "delta", mode="register", initialize_config=True)

        assert registry.get("delta") is None
        assert (project / ".aflow" / "runs" / "marker").read_text() == "keep"
        assert not (project / ".aflow" / "config").exists()
        assert not (project / ".aflow" / "aflow.toml").exists()

    def test_config_failure_after_empty_git_bootstrap_restores_empty_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aflow_app_server import project_service as project_service_module

        service, _, managed = _service(tmp_path)
        (managed / "fresh").mkdir()

        def failing_bootstrap(*args, **kwargs):  # type: ignore[no-untyped-def]
            from aflow.config import ConfigError

            raise ConfigError("starter unavailable")

        monkeypatch.setattr(
            project_service_module, "bootstrap_project_config", failing_bootstrap
        )
        with pytest.raises(ProjectServiceError):
            _create(
                service,
                "fresh",
                mode="register",
                initialize_git=True,
                initialize_config=True,
            )
        assert list((managed / "fresh").iterdir()) == []

    def test_registry_failure_after_empty_git_bootstrap_restores_empty_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, registry, managed = _service(tmp_path)
        (managed / "hollow").mkdir()
        monkeypatch.setattr(ProjectRegistry, "register", _failing_register)

        with pytest.raises(Exception, match="registry rejected"):
            _create(service, "hollow", mode="register", initialize_git=True)

        assert registry.get("hollow") is None
        assert list((managed / "hollow").iterdir()) == []

    def test_invalid_display_name_rejected_before_mutation(
        self, tmp_path: Path
    ) -> None:
        service, registry, managed = _service(tmp_path)
        with pytest.raises(ProjectServiceError, match="display_name"):
            _create(service, "alpha", display_name="bad\nname")
        assert not (managed / "alpha").exists()
        assert list(managed.glob(".aflow-create-*")) == []
        assert registry.list_records() == ()

    def test_invalid_initial_workflow_rejected_before_mutation(
        self, tmp_path: Path
    ) -> None:
        service, _, managed = _service(tmp_path)
        with pytest.raises(ProjectServiceError, match="workflow-safe"):
            _create(service, "alpha", initial_workflow="../escape")
        assert not (managed / "alpha").exists()
        assert list(managed.glob(".aflow-create-*")) == []

    def test_pre_existing_target_byte_identical_after_failed_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _, managed = _service(tmp_path)
        before = _create(service, "alpha")
        assert before["id"] == "alpha"
        # A second create at the same path must not touch the first project.
        with pytest.raises(ProjectServiceError, match="already registered"):
            _create(service, "alpha")
        assert _git(managed / "alpha", "status", "--porcelain") == ""


class TestUnsafeFolderNames:
    """Existing folders register without renaming; IDs stay path-safe."""

    def test_real_world_basenames_register_with_stable_generated_ids(
        self, tmp_path: Path
    ) -> None:
        from aflow_app_server.project_ids import deterministic_project_id

        service, registry, managed = _service(tmp_path)
        for name in ("agent_flow", "codHex", "My Project", "prøjekt"):
            _committed_repo(managed, name)
            head = _git(managed / name, "rev-parse", "HEAD")

            result = _create(service, name, mode="register")

            assert result["id"] == deterministic_project_id(name)
            assert registry.get(result["id"]) is not None
            record, root = registry.resolve(result["id"])
            assert root == managed / name
            assert record.relative_root == name
            # Registration is read-only for an existing repository.
            assert _git(managed / name, "rev-parse", "HEAD") == head
            assert _git(managed / name, "status", "--porcelain") == ""
            assert not (managed / name / ".aflow").exists()

    def test_generated_ids_are_path_safe_and_repeatable(self, tmp_path: Path) -> None:
        from aflow_app_server.project_ids import (
            PROJECT_ID_RE,
            deterministic_project_id,
        )

        for name in ("agent_flow", "codHex", "My Project", "prøjekt", "folder with spaces"):
            project_id = deterministic_project_id(name)
            assert PROJECT_ID_RE.fullmatch(project_id) is not None
            assert deterministic_project_id(name) == project_id
        # The base truncates to 48 characters; a fully non-ASCII basename
        # falls back to "project"; the digest distinguishes both cases.
        long_id = deterministic_project_id("a" * 60)
        assert long_id.startswith("a" * 48 + "-")
        assert deterministic_project_id("øø").startswith("project-")
        assert long_id != deterministic_project_id("a" * 60 + "b")

    def test_consecutive_unsafe_runs_collapse_to_single_hyphens(
        self, tmp_path: Path
    ) -> None:
        from aflow_app_server.project_ids import (
            PROJECT_ID_RE,
            deterministic_project_id,
        )

        service, registry, managed = _service(tmp_path)
        for name in ("a__b", "folder  with spaces", "mix - _ .!run"):
            project_id = deterministic_project_id(name)
            assert PROJECT_ID_RE.fullmatch(project_id) is not None
            assert "--" not in project_id

            _committed_repo(managed, name)
            result = _create(service, name, mode="register")

            assert result["id"] == project_id
            assert registry.get(project_id) is not None

    def test_truncation_boundary_landing_on_unsafe_run_keeps_id_safe(
        self, tmp_path: Path
    ) -> None:
        from aflow_app_server.project_ids import (
            PROJECT_ID_RE,
            deterministic_project_id,
        )

        service, registry, managed = _service(tmp_path)
        for name, base in (
            ("a" * 47 + " b", "a" * 47),
            ("a" * 46 + " _b", "a" * 46),
        ):
            project_id = deterministic_project_id(name)
            assert project_id.startswith(base + "-")
            assert PROJECT_ID_RE.fullmatch(project_id) is not None

            _committed_repo(managed, name)
            result = _create(service, name, mode="register")

            assert result["id"] == project_id
            assert registry.get(project_id) is not None

    def test_default_display_name_uses_actual_basename_for_unsafe_names(
        self, tmp_path: Path
    ) -> None:
        service, _, managed = _service(tmp_path)
        _committed_repo(managed, "codHex")

        result = _create(service, "codHex", mode="register")

        assert result["display_name"] == "codHex"

    def test_safe_slug_default_display_behavior_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        service, _, _ = _service(tmp_path)
        result = _create(service, "beta-service")
        assert result["display_name"] == "Beta Service"

    def test_two_roots_with_same_basename_get_distinct_stable_ids(
        self, tmp_path: Path
    ) -> None:
        service, registry, managed = _service(tmp_path)
        _committed_repo(managed, "one/agent_flow")
        _committed_repo(managed, "two/agent_flow")

        first = _create(service, "one/agent_flow", mode="register")
        second = _create(service, "two/agent_flow", mode="register")

        assert first["id"] != second["id"]
        assert registry.resolve(first["id"])[1] == managed / "one" / "agent_flow"
        assert registry.resolve(second["id"])[1] == managed / "two" / "agent_flow"

    @pytest.mark.skipif(
        os.path.normcase("A") != "A",
        reason="case-distinct roots require case-sensitive path identity",
    )
    def test_case_distinct_basenames_stay_distinct(self, tmp_path: Path) -> None:
        service, registry, managed = _service(tmp_path)
        _committed_repo(managed, "case")
        _committed_repo(managed, "Case")

        lower = _create(service, "case", mode="register")
        upper = _create(service, "Case", mode="register")

        # The safe-slug basename keeps its exact ID; the unsafe case gets a
        # deterministic one. Lookup remains case-sensitive on both.
        assert lower["id"] == "case"
        assert upper["id"] != "case"
        assert registry.resolve(lower["id"])[1] == managed / "case"
        assert registry.resolve(upper["id"])[1] == managed / "Case"

    def test_existing_registry_ids_are_unchanged_by_new_registrations(
        self, tmp_path: Path
    ) -> None:
        service, registry, managed = _service(tmp_path)
        _create(service, "alpha")
        _committed_repo(managed, "agent_flow")
        before = {record.id: record for record in registry.list_records()}

        _create(service, "agent_flow", mode="register")

        after = {record.id: record for record in registry.list_records()}
        for record_id, record in before.items():
            assert after[record_id] == record

    def test_unallocatable_id_is_rejected_without_mutation(
        self, tmp_path: Path
    ) -> None:
        from aflow_app_server.project_ids import deterministic_project_id

        service, registry, managed = _service(tmp_path)
        project = _committed_repo(managed, "agent_flow")
        head = _git(project, "rev-parse", "HEAD")
        _committed_repo(managed, "other")
        registry.register(deterministic_project_id("agent_flow"), "Other", "other")

        with pytest.raises(ProjectServiceError, match="project id is already registered"):
            _create(service, "agent_flow", mode="register")

        assert registry.get("agent_flow") is None
        assert _git(project, "rev-parse", "HEAD") == head
        assert _git(project, "status", "--porcelain") == ""
        assert len(registry.list_records()) == 1

    def test_create_mode_allocates_safe_id_for_unsafe_basename(
        self, tmp_path: Path
    ) -> None:
        from aflow_app_server.project_ids import deterministic_project_id

        service, registry, managed = _service(tmp_path)

        result = _create(service, "My Project")

        assert result["id"] == deterministic_project_id("My Project")
        assert result["display_name"] == "My Project"
        assert (managed / "My Project" / ".git").exists()
        assert registry.get(result["id"]) is not None

    def test_traversal_symlink_and_overlap_rejection_is_retained(
        self, tmp_path: Path
    ) -> None:
        service, _, managed = _service(tmp_path)
        real = _committed_repo(managed, "real_root")
        (managed / "alias").symlink_to(real, target_is_directory=True)

        with pytest.raises(ProjectServiceError, match="symlink"):
            _create(service, "alias", mode="register")
        _create(service, "real_root", mode="register")
        with pytest.raises(ProjectServiceError, match="already registered"):
            _create(service, "real_root", mode="register")
        with pytest.raises(ProjectServiceError, match="relative"):
            _create(service, "../outside", mode="register")


class TestUnregister:
    def test_unregister_removes_record_but_not_files(self, tmp_path: Path) -> None:
        from aflow.control_plane.units import InMemoryUnitManager
        from aflow.daemon import AflowDaemon

        service, registry, managed = _service(tmp_path)
        _create(service, "alpha")
        executable, environment_file = _release_inputs(tmp_path)
        control_plane = ControlPlaneService(
            registry,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="test-release",
            daemon_factory=lambda config: AflowDaemon(config, units=InMemoryUnitManager()),
        )
        service = ProjectService(registry, control_plane)

        assert service.unregister("alpha") is True
        assert registry.get("alpha") is None
        assert (managed / "alpha" / ".git").exists()
        assert service.unregister("alpha") is False

    def test_unregister_rejected_while_units_active(self, tmp_path: Path) -> None:
        service, registry, _ = _service(tmp_path)
        _create(service, "alpha")

        class FailingControlPlane:
            def unregister(self, project_id: str) -> bool:
                from aflow_app_server.project_registry import ProjectRegistryError

                raise ProjectRegistryError("project owns an active workflow unit")

        failing = ProjectService(registry, FailingControlPlane())  # type: ignore[arg-type]
        with pytest.raises(Exception, match="active workflow unit"):
            failing.unregister("alpha")
        assert registry.get("alpha") is not None


class TestAuthenticatedApi:
    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        managed = tmp_path / "managed"
        managed.mkdir()
        executable, environment_file = _release_inputs(tmp_path)
        config = ServerConfig(
            bind_host="127.0.0.1",
            bind_port=8765,
            auth_token="test-token",
            managed_projects_root=managed,
            project_registry_path=tmp_path / "projects.json",
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="test-release",
        )
        registry = ProjectRegistry(config.managed_projects_root, config.project_registry_path)
        main_module._config = config
        main_module._project_registry = registry
        main_module._control_plane_service = ControlPlaneService(
            registry,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity="test-release",
        )
        main_module._control_plane_service.start()
        client = TestClient(main_module.app, raise_server_exceptions=False)
        client.headers["Authorization"] = "Bearer test-token"
        try:
            yield client
        finally:
            main_module._config = None
            main_module._project_registry = None
            main_module._control_plane_service = None

    def test_create_register_unregister_end_to_end(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        managed = tmp_path / "managed"

        # Create
        response = client.post(
            "/api/projects",
            json={
                "mode": "create",
                "path": "alpha",
                "display_name": "Alpha",
                "main_branch": "trunk",
                "initial_workflow": "build",
                "initial_team": "crew",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["readiness"] == "configuration_required"

        listing = client.get("/api/projects")
        assert listing.status_code == 200
        assert [entry["id"] for entry in listing.json()] == ["alpha"]
        assert listing.json()[0]["readiness"] == "configuration_required"

        # Register an existing repository (must hold a valid commit HEAD)
        project = managed / "beta"
        project.mkdir()
        subprocess.run(("git", "init", "-q", str(project)), check=True)
        (project / "README.md").write_text("# beta\n")
        subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
        subprocess.run((
            "git", "-C", str(project),
            "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "-m", "init",
        ), check=True)
        response = client.post(
            "/api/projects",
            json={"mode": "register", "path": "beta", "initialize_config": True},
        )
        assert response.status_code == 201, response.text
        assert response.json()["id"] == "beta"

        # Unregister is non-destructive
        response = client.delete("/api/projects/alpha")
        assert response.status_code == 204
        assert (managed / "alpha" / ".git").exists()
        assert [e["id"] for e in client.get("/api/projects").json()] == ["beta"]

        response = client.delete("/api/projects/alpha")
        assert response.status_code == 404

    def test_unauthenticated_requests_are_rejected(self, client: TestClient) -> None:
        anon = TestClient(main_module.app)
        assert anon.get("/api/projects").status_code == 401
        assert anon.post("/api/projects", json={"mode": "create", "path": "x"}).status_code == 401
        assert anon.delete("/api/projects/x").status_code == 401

    def test_api_rejects_unsafe_payloads(self, client: TestClient) -> None:
        response = client.post(
            "/api/projects",
            json={"mode": "explore", "path": "alpha"},
        )
        assert response.status_code == 422
        response = client.post(
            "/api/projects",
            json={"mode": "create", "path": "../escape"},
        )
        assert response.status_code == 422

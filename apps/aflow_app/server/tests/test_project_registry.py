"""Safety and dynamic-composition tests for the canonical project registry."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from aflow.control_plane.units import InMemoryUnitManager
from aflow.daemon import AflowDaemon
from aflow_app_server.config import ServerConfig
from aflow_app_server.control_plane_service import (
    ControlPlaneService,
    ControlPlaneUnavailableError,
    ProjectNotAllowedError,
)
from aflow_app_server.project_registry import (
    ProjectRegistry,
    ProjectRegistryError,
)


def _registry_pair_path(registry):
    """Resolve the single registered project's pair path for these tests."""
    records = registry.list_records()
    if not records:
        return registry.managed_root / "alpha" / ".aflow" / "config" / "aflow.toml"
    record = records[0]
    return registry.declared_root(record.id) / ".aflow" / "config" / "aflow.toml"


def _git_project(root: Path, name: str, *, valid_config: bool = True) -> Path:
    project = root / name
    project.mkdir(parents=True)
    subprocess.run(("git", "init", "-q", str(project)), check=True)
    config = project / ".aflow" / "config" / "aflow.toml"
    config.parent.mkdir(parents=True)
    if valid_config:
        config.write_text(
            '[aflow]\ndefault_workflow = "managed"\n\n'
            '[harness.codex.profiles.test]\nmodel = "test"\n\n'
            '[roles]\nworker = "codex.test"\n\n[prompts]\np = "Work."\n'
        )
        config.with_name("workflows.toml").write_text(
            '[workflow.managed.steps.implement]\nrole = "worker"\n'
            'prompts = ["p"]\ngo = [{ to = "END", when = "DONE" }]\n'
        )
    return project


def _release_inputs(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "release" / "bin" / "aflow"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    environment_file = tmp_path / "aflowd.env"
    environment_file.write_text("AFLOWD_MODE=test\n")
    return executable, environment_file


def test_registry_persists_versioned_relative_records_atomically(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    project = _git_project(managed, "alpha")
    path = tmp_path / "state" / "projects.json"
    registry = ProjectRegistry(managed, path)

    record = registry.register("alpha", "Alpha", "alpha")

    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert payload["projects"] == [record.to_dict()]
    assert payload["projects"][0]["relative_root"] == "alpha"
    assert "executable" not in json.dumps(payload)
    assert registry.resolve("alpha") == (record, project.resolve())
    assert list(path.parent.glob("*.tmp")) == []


def test_server_config_uses_shared_release_inputs_and_rejects_static_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    executable, environment_file = _release_inputs(tmp_path)
    (config_dir / "config.toml").write_text(
        f'''[server]
auth_token = "test-token"

[control_plane]
managed_projects_root = "{managed}"
project_registry_path = "{tmp_path / 'projects.json'}"
aflow_executable = "{executable}"
environment_file = "{environment_file}"
release_identity = "release-1"
environment = {{ HOME = "/srv/aflow" }}
'''
    )
    monkeypatch.setenv("AFLOW_APP_CONFIG_DIR", str(config_dir))

    config = ServerConfig.from_env()

    assert config.managed_projects_root == managed
    assert config.project_registry_path == tmp_path / "projects.json"
    assert config.aflow_executable == executable
    assert config.environment_file == environment_file
    assert config.release_identity == "release-1"
    assert config.control_plane_environment == {"HOME": "/srv/aflow"}
    assert config.control_plane_projects == ()

    with (config_dir / "config.toml").open("a") as handle:
        handle.write(
            '\n[[control_plane.projects]]\nid = "must-not-load"\nroot = "/outside"\n'
        )
    with pytest.raises(ValueError, match="no longer supported"):
        ServerConfig.from_env()


@pytest.mark.parametrize(
    "relative_root",
    ["../outside", "/absolute", "alpha/../beta", "./alpha", "alpha//nested"],
)
def test_registry_rejects_traversal_and_noncanonical_roots(
    tmp_path: Path, relative_root: str
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    _git_project(managed, "alpha")
    registry = ProjectRegistry(managed, tmp_path / "projects.json")

    with pytest.raises(ProjectRegistryError):
        registry.register("alpha", "Alpha", relative_root)


def test_registry_rejects_symlinks_non_git_duplicates_and_overlap(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    alpha = _git_project(managed, "alpha")
    _git_project(alpha, "nested")
    (managed / "plain").mkdir()
    (managed / "linked").symlink_to(alpha, target_is_directory=True)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")

    with pytest.raises(ProjectRegistryError, match="Git"):
        registry.register("plain", "Plain", "plain")
    with pytest.raises(ProjectRegistryError, match="symlink"):
        registry.register("linked", "Linked", "linked")

    registry.register("alpha", "Alpha", "alpha")
    with pytest.raises(ProjectRegistryError, match="id"):
        registry.register("alpha", "Again", "alpha/nested")
    with pytest.raises(ProjectRegistryError, match="overlapping"):
        registry.register("nested", "Nested", "alpha/nested")


def test_registry_rejects_symlinked_managed_root_and_corrupt_store(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    linked = tmp_path / "managed-link"
    linked.symlink_to(managed, target_is_directory=True)
    with pytest.raises(ProjectRegistryError, match="symlink"):
        ProjectRegistry(linked, tmp_path / "projects.json")

    path = tmp_path / "corrupt.json"
    path.write_text('{"schema_version": 1, "projects": [')
    with pytest.raises(ProjectRegistryError, match="corrupt"):
        ProjectRegistry(managed, path)
    assert path.read_text() == '{"schema_version": 1, "projects": ['


def test_external_valid_record_is_immediately_addressable_and_daemon_is_cached(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    _git_project(managed, "alpha")
    executable, environment_file = _release_inputs(tmp_path)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    factories: list[Path] = []

    def factory(config):
        factories.append(config.repo_root)
        return AflowDaemon(config, units=InMemoryUnitManager())

    service = ControlPlaneService(
        registry,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
        daemon_factory=factory,
        workflow_config_path=registry.managed_root / "alpha" / ".aflow" / "config" / "aflow.toml",
    )
    service.start()
    assert service.projects() == ()

    registry.register("alpha", "Alpha", "alpha")
    assert service.projects()[0].project_id == "alpha"
    assert service.capabilities("alpha").workflows == ("managed",)
    assert service.capabilities("alpha").workflows == ("managed",)
    assert factories == [(managed / "alpha").resolve()]


def test_all_projects_share_one_global_pair(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    _git_project(managed, "healthy")
    _git_project(managed, "second")
    executable, environment_file = _release_inputs(tmp_path)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    registry.register("healthy", "Healthy", "healthy")
    registry.register("second", "Second", "second")
    # The single shared global pair lives outside any project.
    global_pair = tmp_path / "global" / "aflow.toml"
    global_pair.parent.mkdir(parents=True)
    global_pair.write_text(
        '[aflow]\ndefault_workflow = "managed"\n\n'
        '[harness.codex.profiles.test]\nmodel = "test"\n\n'
        '[roles]\nworker = "codex.test"\n\n[prompts]\np = "Work."\n'
    )
    global_pair.with_name("workflows.toml").write_text(
        '[workflow.managed.steps.implement]\nrole = "worker"\n'
        'prompts = ["p"]\ngo = [{ to = "END", when = "DONE" }]\n'
    )
    service = ControlPlaneService(
        registry,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
        daemon_factory=lambda config: AflowDaemon(config, units=InMemoryUnitManager()),
        workflow_config_path=global_pair,
    )
    service.start()

    assert {project.project_id for project in service.projects()} == {"healthy", "second"}
    assert service.readiness() == {"healthy": None, "second": None}
    assert service.capabilities("healthy").workflows == ("managed",)
    assert service.capabilities("second").workflows == ("managed",)
    assert service.ready_capabilities().keys() == {"healthy", "second"}


def test_unregistered_project_is_rejected_before_daemon_composition(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    _git_project(managed, "candidate")
    executable, environment_file = _release_inputs(tmp_path)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    calls = 0

    def factory(config):
        nonlocal calls
        calls += 1
        return AflowDaemon(config, units=InMemoryUnitManager())

    service = ControlPlaneService(
        registry,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
        daemon_factory=factory,
        workflow_config_path=registry.managed_root / "alpha" / ".aflow" / "config" / "aflow.toml",
    )
    with pytest.raises(ProjectNotAllowedError):
        service.capabilities("candidate")
    assert calls == 0


def test_two_projects_keep_independent_run_roots_and_unregister_cache(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    first = _git_project(managed, "first")
    second = _git_project(managed, "second")
    (first / ".aflow" / "runs" / "first-run").mkdir(parents=True)
    (second / ".aflow" / "runs" / "second-run").mkdir(parents=True)
    executable, environment_file = _release_inputs(tmp_path)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    registry.register("first", "First", "first")
    registry.register("second", "Second", "second")
    service = ControlPlaneService(
        registry,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
        daemon_factory=lambda config: AflowDaemon(config, units=InMemoryUnitManager()),
        workflow_config_path=_registry_pair_path(registry),
    )

    first_daemon = service._project("first").daemon
    second_daemon = service._project("second").daemon
    assert first_daemon.application.repository.repo_root == first.resolve()
    assert second_daemon.application.repository.repo_root == second.resolve()
    assert service.unregister("first") is True
    with pytest.raises(ProjectNotAllowedError):
        service.capabilities("first")
    assert service.capabilities("second").workflows == ("managed",)


def test_registration_ignores_unavailable_peers_but_keeps_identity_strict(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    for name in ("alpha", "beta", "gamma", "delta"):
        _git_project(managed, name)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    registry.register("alpha", "Alpha", "alpha")
    registry.register("beta", "Beta", "beta")

    # The peer root disappears: an unrelated healthy project still registers.
    shutil.rmtree(managed / "beta")
    registry.register("gamma", "Gamma", "gamma")

    before_failures = (tmp_path / "projects.json").read_bytes()

    # A nested candidate beneath the unavailable record remains rejected, and
    # the failed attempts leave the durable registry bytes untouched.
    _git_project(managed / "beta", "nested")
    with pytest.raises(ProjectRegistryError, match="overlapping"):
        registry.register("beta-nested", "Beta Nested", "beta/nested")

    # Recreating the same root with a fresh repository remains rejected.
    subprocess.run(("git", "init", "-q", str(managed / "beta")), check=True)
    with pytest.raises(ProjectRegistryError, match="already registered"):
        registry.register("beta-again", "Beta Again", "beta")
    assert (tmp_path / "projects.json").read_bytes() == before_failures

    # A peer replaced by an invalid path still does not block registration.
    shutil.rmtree(managed / "gamma")
    (managed / "gamma").write_text("not a directory")
    registry.register("delta", "Delta", "delta")

    payload = json.loads((tmp_path / "projects.json").read_text())
    assert {entry["id"] for entry in payload["projects"]} == {
        "alpha", "beta", "gamma", "delta"
    }
    assert registry.resolve("delta")[1] == (managed / "delta").resolve()

def test_cached_project_recovers_after_exact_root_is_restored(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    root = _git_project(managed, "alpha")
    executable, environment_file = _release_inputs(tmp_path)
    registry = ProjectRegistry(managed, tmp_path / "projects.json")
    registry.register("alpha", "Alpha", "alpha")
    service = ControlPlaneService(
        registry,
        aflow_executable=executable,
        environment_file=environment_file,
        release_identity="test-release",
        daemon_factory=lambda config: AflowDaemon(config, units=InMemoryUnitManager()),
        workflow_config_path=_registry_pair_path(registry),
    )
    service.start()
    assert service.capabilities("alpha").workflows == ("managed",)
    git_dir = root / ".git"
    saved = root / ".git-temporarily-unavailable"
    git_dir.rename(saved)
    try:
        assert service.readiness()["alpha"] == "project_registration_invalid"
        with pytest.raises(ControlPlaneUnavailableError):
            service.capabilities("alpha")
    finally:
        saved.rename(git_dir)
    assert service.readiness() == {"alpha": None}
    assert service.capabilities("alpha").workflows == ("managed",)

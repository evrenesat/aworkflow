"""Bounded read-only project discovery tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from aflow_app_server.config import ServerConfig
from aflow_app_server.control_plane_service import ControlPlaneService
from aflow_app_server import main as main_module
from aflow_app_server.project_discovery import (
    MAX_CANDIDATES,
    MAX_VISITED_ENTRIES,
    ProjectDiscoveryUnavailable,
    discover_projects,
)
from aflow_app_server.project_registry import ProjectRegistry


def _git(target: Path, *argv: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(target), *argv),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _committed_repo(root: Path, name: str) -> Path:
    project = root / name
    project.mkdir(parents=True)
    subprocess.run(("git", "init", "-q", str(project)), check=True)
    (project / "keep.txt").write_text("history")
    _git(project, "add", "keep.txt")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "prior")
    return project


def _registry(tmp_path: Path, managed_name: str = "managed") -> tuple[ProjectRegistry, Path]:
    managed = tmp_path / managed_name
    managed.mkdir()
    return ProjectRegistry(managed, tmp_path / f"{managed_name}-projects.json"), managed


def _candidates(payload: dict) -> dict[str, dict]:
    return {entry["relative_path"]: entry for entry in payload["candidates"]}


class TestTraversal:
    def test_finds_direct_and_nested_roots_but_not_deeper(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "direct")
        _committed_repo(managed, "team/nested")
        _committed_repo(managed, "team/deeper/leaf")

        payload = discover_projects(registry)

        found = _candidates(payload)
        assert set(found) == {"direct", "team/nested"}
        assert payload["managed_root"] == str(managed)
        assert payload["schema_version"] == 1
        assert payload["truncated"] is False
        assert payload["limits"] == {
            "max_visited_entries": MAX_VISITED_ENTRIES,
            "max_candidates": MAX_CANDIDATES,
        }

    def test_git_file_worktree_entry_counts_as_root(self, tmp_path: Path) -> None:
        registry, managed = _registry(tmp_path)
        main_repo = _committed_repo(managed, "main-repo")
        worktree = managed / "linked"
        worktree.mkdir()
        _git(main_repo, "worktree", "add", "-q", str(worktree), "-b", "wt")

        payload = discover_projects(registry)

        found = _candidates(payload)
        assert "linked" in found
        assert (worktree / ".git").is_file()

    def test_unborn_head_is_found_but_not_addable(self, tmp_path: Path) -> None:
        registry, managed = _registry(tmp_path)
        project = managed / "unborn"
        project.mkdir()
        subprocess.run(("git", "init", "-q", str(project)), check=True)

        payload = discover_projects(registry)

        entry = _candidates(payload)["unborn"]
        assert entry["addable"] is False
        assert entry["add_blocker"] == "repository HEAD does not point to a commit"

    def test_non_root_directories_descend_only_to_grandchildren(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        (managed / "plain").mkdir()
        _committed_repo(managed, "plain/inner")

        payload = discover_projects(registry)

        assert set(_candidates(payload)) == {"plain/inner"}

    def test_skips_exact_hidden_and_archive_names(self, tmp_path: Path) -> None:
        registry, managed = _registry(tmp_path)
        for name in ("worktrees", "node_modules", ".venv", "evidence", ".hidden"):
            _committed_repo(managed, name)
        _committed_repo(managed, "_archive-old")

        payload = discover_projects(registry)

        assert payload["candidates"] == []

    def test_symlinked_directories_are_never_followed(self, tmp_path: Path) -> None:
        registry, managed = _registry(tmp_path)
        outside = _committed_repo(tmp_path / "outside-repo", "secret")
        (managed / "alias").symlink_to(outside, target_is_directory=True)

        payload = discover_projects(registry)

        assert payload["candidates"] == []

    def test_symlinked_git_entry_disqualifies_a_root(self, tmp_path: Path) -> None:
        registry, managed = _registry(tmp_path)
        real = _committed_repo(managed, "real")
        fake = managed / "fake"
        fake.mkdir()
        (fake / ".git").symlink_to(real / ".git", target_is_directory=False)

        payload = discover_projects(registry)

        assert set(_candidates(payload)) == {"real"}

    def test_unsafe_basename_is_addable_with_actual_display_name(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "My_Project")

        payload = discover_projects(registry)

        entry = _candidates(payload)["My_Project"]
        assert entry == {
            "relative_path": "My_Project",
            "display_name": "My_Project",
            "registered_project_id": None,
            "addable": True,
            "add_blocker": None,
        }


class TestRegistryInteraction:
    def test_registered_candidate_is_flagged_not_addable(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        project = _committed_repo(managed, "alpha")
        registry.register("alpha", "Alpha", "alpha")

        payload = discover_projects(registry)

        entry = _candidates(payload)["alpha"]
        assert entry["registered_project_id"] == "alpha"
        assert entry["display_name"] == "Alpha"
        assert entry["addable"] is False
        assert entry["add_blocker"] == "already registered"
        assert _git(project, "status", "--porcelain") == ""

    def test_overlap_with_registered_root_reports_blocker(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "team/nested")
        registry.register("nested", "Nested", "team/nested")
        # A Git root that contains the registered root overlaps it.
        team = managed / "team"
        subprocess.run(("git", "init", "-q", str(team)), check=True)
        _git(team, "add", "nested")
        _git(team, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "wrap")

        payload = discover_projects(registry)

        entry = _candidates(payload)["team"]
        assert entry["addable"] is False
        assert entry["add_blocker"] == "overlaps a registered project root"

    def test_matching_uses_normalized_relative_root_without_config(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "team/nested")
        registry.register("nested", "Nested", "team/nested")

        payload = discover_projects(registry)

        entry = _candidates(payload)["team/nested"]
        assert entry["registered_project_id"] == "nested"

    def test_registered_project_with_invalid_config_does_not_hide_results(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        broken = _committed_repo(managed, "broken")
        (broken / ".aflow" / "config").mkdir(parents=True)
        (broken / ".aflow" / "config" / "aflow.toml").write_text("not = [valid toml\n")
        registry.register("broken", "Broken", "broken")
        _committed_repo(managed, "healthy")

        payload = discover_projects(registry)

        found = _candidates(payload)
        assert found["broken"]["registered_project_id"] == "broken"
        assert found["healthy"]["addable"] is True

    @pytest.mark.skipif(
        os.path.normcase("A") != "A",
        reason="distinct-cased roots require case-sensitive path identity",
    )
    def test_case_distinct_relative_roots_stay_distinct(self, tmp_path: Path) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "Team/shared")
        _committed_repo(managed, "team/shared")
        registry.register("shared", "Shared", "Team/shared")

        payload = discover_projects(registry)

        found = _candidates(payload)
        assert set(found) == {"Team/shared", "team/shared"}
        assert found["Team/shared"]["registered_project_id"] == "shared"
        # The case-distinct unregistered root gets a deterministic distinct ID.
        unregistered = found["team/shared"]
        assert unregistered["registered_project_id"] is None
        assert unregistered["addable"] is True
        assert unregistered["add_blocker"] is None

    def test_duplicate_basename_registered_elsewhere_is_addable(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "one/shared")
        _committed_repo(managed, "two/shared")
        registry.register("shared", "Shared", "one/shared")

        payload = discover_projects(registry)

        found = _candidates(payload)
        assert found["one/shared"]["registered_project_id"] == "shared"
        entry = found["two/shared"]
        assert entry["registered_project_id"] is None
        assert entry["addable"] is True
        assert entry["add_blocker"] is None

    def test_add_pre_check_rejects_when_no_safe_id_is_allocatable(
        self, tmp_path: Path
    ) -> None:
        from aflow_app_server.project_ids import deterministic_project_id

        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "agent_flow")
        # Occupy the exact deterministic ID the candidate would receive.
        _committed_repo(managed, "other")
        colliding = deterministic_project_id("agent_flow")
        registry.register(colliding, "Colliding", "other")

        payload = discover_projects(registry)

        entry = _candidates(payload)["agent_flow"]
        assert entry["addable"] is False
        assert entry["add_blocker"] == "project id is already registered"

    def test_addable_candidate_has_no_blocker_and_clean_display_name(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "beta-service")

        payload = discover_projects(registry)

        entry = _candidates(payload)["beta-service"]
        assert entry == {
            "relative_path": "beta-service",
            "display_name": "Beta Service",
            "registered_project_id": None,
            "addable": True,
            "add_blocker": None,
        }


class TestLimitsAndFailures:
    def test_visited_entry_cap_stops_enumeration_and_truncates(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        bulk = managed / "bulk"
        bulk.mkdir()
        for index in range(MAX_VISITED_ENTRIES):
            (bulk / f"entry-{index:04d}").mkdir()
        _committed_repo(managed, "real")

        payload = discover_projects(registry)

        assert payload["truncated"] is True
        assert payload["visited_entries"] == MAX_VISITED_ENTRIES
        # The depth-1 root scanned first is still reported.
        assert "real" in _candidates(payload)

    def test_candidate_cap_truncates_beyond_one_hundred_roots(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        for index in range(MAX_CANDIDATES + 5):
            _committed_repo(managed, f"repo-{index:03d}")

        payload = discover_projects(registry)

        assert len(payload["candidates"]) == MAX_CANDIDATES
        assert payload["truncated"] is True

    def test_exactly_one_hundred_roots_is_not_truncated(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        for index in range(MAX_CANDIDATES):
            _committed_repo(managed, f"repo-{index:03d}")

        payload = discover_projects(registry)

        assert len(payload["candidates"]) == MAX_CANDIDATES
        assert payload["truncated"] is False

    def test_partial_read_failures_are_counted_and_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "healthy")
        locked = managed / "locked"
        locked.mkdir()
        real_scandir = os.scandir

        def failing_scandir(path):
            if Path(path) == locked:
                raise PermissionError("denied")
            return real_scandir(path)

        monkeypatch.setattr(
            "aflow_app_server.project_discovery.os.scandir", failing_scandir
        )

        payload = discover_projects(registry)

        assert payload["skipped_unreadable"] == 1
        assert set(_candidates(payload)) == {"healthy"}

    def test_root_scan_failure_raises_discovery_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, managed = _registry(tmp_path)
        _committed_repo(managed, "healthy")
        real_scandir = os.scandir

        def failing_scandir(path):
            if Path(path) == managed:
                raise PermissionError("denied")
            return real_scandir(path)

        monkeypatch.setattr(
            "aflow_app_server.project_discovery.os.scandir", failing_scandir
        )

        with pytest.raises(ProjectDiscoveryUnavailable):
            discover_projects(registry)

    def test_registry_corruption_remains_an_error(self, tmp_path: Path) -> None:
        registry, managed = _registry(tmp_path)
        registry.path.write_text("{ not json")
        _committed_repo(managed, "alpha")

        from aflow_app_server.project_registry import ProjectRegistryError

        with pytest.raises(ProjectRegistryError):
            discover_projects(registry)

    def test_unavailable_managed_root_raises_discovery_unavailable(
        self, tmp_path: Path
    ) -> None:
        registry, managed = _registry(tmp_path)
        managed.rmdir()

        with pytest.raises(ProjectDiscoveryUnavailable):
            discover_projects(registry)


class TestAuthenticatedApi:
    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        managed = tmp_path / "managed"
        managed.mkdir()
        executable = tmp_path / "release" / "bin" / "aflow"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        environment_file = tmp_path / "aflowd.env"
        environment_file.write_text("AFLOWD_MODE=test\n")
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
        yield client
        main_module._config = None
        main_module._project_registry = None
        main_module._control_plane_service = None

    def test_discovery_requires_authentication(self, client: TestClient) -> None:
        anon = TestClient(main_module.app)
        assert anon.get("/api/project-discovery").status_code == 401

    def test_discovery_endpoint_returns_bounded_candidates(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        managed = tmp_path / "managed"
        _committed_repo(managed, "alpha")
        _committed_repo(managed, "team/beta")

        response = client.get("/api/project-discovery")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert [entry["relative_path"] for entry in payload["candidates"]] == [
            "alpha",
            "team/beta",
        ]
        assert payload["managed_root"] == str(managed)

    def test_unavailable_root_maps_to_503_without_partial_candidates(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        managed = tmp_path / "managed"
        _committed_repo(managed, "alpha")
        import shutil

        shutil.rmtree(managed)

        response = client.get("/api/project-discovery")

        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "discovery_unavailable"}}

    def test_root_scan_failure_maps_to_503_without_partial_candidates(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        managed = tmp_path / "managed"
        _committed_repo(managed, "alpha")
        real_scandir = os.scandir

        def failing_scandir(path):
            if Path(path) == managed:
                raise PermissionError("denied")
            return real_scandir(path)

        monkeypatch.setattr(
            "aflow_app_server.project_discovery.os.scandir", failing_scandir
        )

        response = client.get("/api/project-discovery")

        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "discovery_unavailable"}}

    def test_add_from_discovery_changes_only_the_registry(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        managed = tmp_path / "managed"
        _committed_repo(managed, "gamma")
        head = _git(managed / "gamma", "rev-parse", "HEAD")
        status = _git(managed / "gamma", "status", "--porcelain")

        listing = client.get("/api/project-discovery")
        candidate = next(
            entry for entry in listing.json()["candidates"]
            if entry["relative_path"] == "gamma"
        )
        assert candidate["addable"] is True

        added = client.post("/api/projects", json={
            "mode": "register",
            "path": candidate["relative_path"],
            "display_name": candidate["display_name"],
            "initialize_git": False,
            "initialize_config": False,
        })
        assert added.status_code == 201, added.text

        assert _git(managed / "gamma", "rev-parse", "HEAD") == head
        assert _git(managed / "gamma", "status", "--porcelain") == status
        assert not (managed / "gamma" / ".aflow").exists()
        refreshed = _candidates(client.get("/api/project-discovery").json())["gamma"]
        assert refreshed["registered_project_id"] == "gamma"
        assert refreshed["addable"] is False

    def test_add_race_rejection_is_reported_without_partial_change(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        managed = tmp_path / "managed"
        _committed_repo(managed, "gamma")
        registry = main_module._project_registry
        assert registry is not None
        registry.register("gamma", "Gamma", "gamma")

        raced = client.post("/api/projects", json={
            "mode": "register",
            "path": "gamma",
            "initialize_git": False,
            "initialize_config": False,
        })

        assert raced.status_code == 422
        assert raced.json() == {"detail": {"code": "operation_rejected"}}

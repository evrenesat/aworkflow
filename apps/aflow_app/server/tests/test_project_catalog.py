"""Tests for project discovery, overrides, and planning-session association."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from aflow_app_server.planning import Session, SessionKey
from aflow_app_server.project_catalog import ProjectCatalog
from aflow_app_server.project_overrides import ProjectOverridesStore


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _run_git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _run_git(["init"], cwd=path)
    (path / "README.md").write_text("# test\n")
    _run_git(["add", "README.md"], cwd=path)
    _run_git(["commit", "-m", "init"], cwd=path)
    return path


def _make_session(session_id: str, cwd: Path) -> Session:
    return Session(
        key=SessionKey(provider_id="codex", provider_session_id=session_id),
        project_id=None,
        preview="preview",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        cwd=str(cwd),
        title=session_id,
    )


def test_discovers_local_git_roots_and_stabilizes_ids(tmp_path: Path) -> None:
    projects_home = tmp_path / "code"
    alpha = _make_git_repo(projects_home / "alpha")
    bravo = _make_git_repo(projects_home / "bravo")
    (projects_home / "notes").mkdir(parents=True)

    store_path = tmp_path / "project_overrides.json"
    catalog = ProjectCatalog(projects_home, store_path)

    first_snapshot = catalog.list_projects()
    assert [project.current_path for project in first_snapshot] == [alpha, bravo]
    assert all(project.detection_source == "local_git_root" for project in first_snapshot)
    assert all(project.linked_thread_count == 0 for project in first_snapshot)

    first_ids = {project.current_path: project.id for project in first_snapshot}
    second_snapshot = ProjectCatalog(projects_home, store_path).list_projects()
    assert {project.current_path: project.id for project in second_snapshot} == first_ids


def test_merges_thread_cwds_into_catalog(tmp_path: Path) -> None:
    projects_home = tmp_path / "code"
    alpha = _make_git_repo(projects_home / "alpha")
    thread_only = tmp_path / "outside" / "thread-only"
    thread_only.mkdir(parents=True)

    sessions = [_make_session("thread-1", alpha), _make_session("thread-2", thread_only)]
    catalog = ProjectCatalog(projects_home, tmp_path / "project_overrides.json")

    projects = catalog.list_projects(sessions=sessions)
    by_path = {project.current_path: project for project in projects}

    assert by_path[alpha].linked_thread_count == 1
    assert by_path[alpha].detection_source == "local_git_root+planning_session_cwd"
    assert by_path[thread_only].linked_thread_count == 1
    assert by_path[thread_only].detection_source == "planning_session_cwd"
    assert by_path[thread_only].is_git_root is False


def test_catalog_without_provider_enrichment_remains_available(tmp_path: Path) -> None:
    projects_home = tmp_path / "code"
    alpha = _make_git_repo(projects_home / "alpha")
    store_path = tmp_path / "project_overrides.json"
    catalog = ProjectCatalog(projects_home, store_path)

    projects = catalog.list_projects(sessions=None)

    assert [project.current_path for project in projects] == [alpha]
    assert projects[0].linked_thread_count == 0
    assert projects[0].detection_source == "local_git_root"


def test_moved_projects_keep_old_threads_visible(tmp_path: Path) -> None:
    projects_home = tmp_path / "code"
    old_path = _make_git_repo(projects_home / "legacy")
    new_path = projects_home / "renamed"
    session = _make_session("thread-1", old_path)

    catalog = ProjectCatalog(projects_home, tmp_path / "project_overrides.json")
    initial = catalog.list_projects(sessions=[session])
    project = initial[0]

    old_path.rename(new_path)
    updated = catalog.update_project(project.id, display_name="Renamed project", current_path=new_path)
    assert updated is not None
    assert updated.current_path == new_path
    assert old_path in updated.historical_aliases

    refreshed = ProjectCatalog(projects_home, tmp_path / "project_overrides.json").list_projects(
        sessions=[session]
    )
    project_by_id = {project.id: project for project in refreshed}[project.id]
    assert project_by_id.current_path == new_path
    assert old_path in project_by_id.historical_aliases
    assert project_by_id.linked_thread_count == 1

    by_alias = ProjectCatalog(projects_home, tmp_path / "project_overrides.json").resolve_project_for_path(
        old_path,
        sessions=[session],
    )
    assert by_alias is not None
    assert by_alias.id == project.id


def test_reused_historical_path_creates_a_distinct_project(tmp_path: Path) -> None:
    """A new repo at an old alias path should not get collapsed into the moved project."""
    projects_home = tmp_path / "code"
    legacy_path = _make_git_repo(projects_home / "legacy")
    store_path = tmp_path / "project_overrides.json"
    catalog = ProjectCatalog(projects_home, store_path)

    initial = catalog.list_projects()
    assert len(initial) == 1
    moved_project = initial[0]

    renamed_path = projects_home / "renamed"
    legacy_path.rename(renamed_path)
    updated = catalog.update_project(
        moved_project.id,
        display_name="Renamed project",
        current_path=renamed_path,
    )
    assert updated is not None

    _make_git_repo(legacy_path)
    refreshed = ProjectCatalog(projects_home, store_path).list_projects()

    assert {project.current_path for project in refreshed} == {renamed_path, legacy_path}
    assert len({project.id for project in refreshed}) == 2

    by_path = {project.current_path: project for project in refreshed}
    assert by_path[renamed_path].id == moved_project.id
    assert by_path[legacy_path].id != moved_project.id
    assert legacy_path in by_path[renamed_path].historical_aliases


def test_reused_historical_path_prefers_alias_owner_for_threads(tmp_path: Path) -> None:
    projects_home = tmp_path / "code"
    legacy_path = _make_git_repo(projects_home / "legacy")
    store_path = tmp_path / "project_overrides.json"
    catalog = ProjectCatalog(projects_home, store_path)

    initial = catalog.list_projects()
    moved_project = initial[0]

    renamed_path = projects_home / "renamed"
    legacy_path.rename(renamed_path)
    updated = catalog.update_project(
        moved_project.id,
        display_name="Renamed project",
        current_path=renamed_path,
    )
    assert updated is not None

    _make_git_repo(legacy_path)
    session = _make_session("thread-1", legacy_path)
    refreshed_catalog = ProjectCatalog(projects_home, store_path)

    resolved = refreshed_catalog.resolve_project_for_path(legacy_path, sessions=[session])
    assert resolved is not None
    assert resolved.id == moved_project.id

    projects = {
        project.current_path: project
        for project in refreshed_catalog.list_projects(sessions=[session])
    }
    assert projects[renamed_path].linked_thread_count == 1
    assert projects[legacy_path].linked_thread_count == 0


def test_project_ownership_helper_prefers_alias_owner(tmp_path: Path) -> None:
    projects_home = tmp_path / "code"
    legacy_path = _make_git_repo(projects_home / "legacy")
    store_path = tmp_path / "project_overrides.json"
    catalog = ProjectCatalog(projects_home, store_path)

    initial = catalog.list_projects()
    moved_project = initial[0]

    renamed_path = projects_home / "renamed"
    legacy_path.rename(renamed_path)
    updated = catalog.update_project(
        moved_project.id,
        display_name="Renamed project",
        current_path=renamed_path,
    )
    assert updated is not None

    _make_git_repo(legacy_path)
    session = _make_session("thread-1", legacy_path)
    refreshed_catalog = ProjectCatalog(projects_home, store_path)
    projects = refreshed_catalog.list_projects(sessions=[session])
    by_path = {project.current_path: project for project in projects}

    assert refreshed_catalog.project_owns_path(
        by_path[renamed_path],
        legacy_path,
        projects=projects,
    ) is True
    assert refreshed_catalog.project_owns_path(
        by_path[legacy_path],
        legacy_path,
        projects=projects,
    ) is False


def test_worktree_projects_collapse_to_primary_checkout(tmp_path: Path) -> None:
    projects_home = tmp_path / "code"
    primary = _init_git_repo(projects_home / "alpha")
    worktree = projects_home / "worktrees" / "alpha-feature"
    _run_git(["worktree", "add", str(worktree)], cwd=primary)

    sessions = [_make_session("thread-1", worktree)]
    catalog = ProjectCatalog(projects_home, tmp_path / "project_overrides.json")

    projects = catalog.list_projects(sessions=sessions)
    assert [project.current_path for project in projects] == [primary]
    assert projects[0].linked_thread_count == 1
    assert projects[0].detection_source == "local_git_root+planning_session_cwd"

    resolved = catalog.resolve_project_for_path(worktree, sessions=sessions)
    assert resolved is not None
    assert resolved.current_path == primary


def test_migrates_legacy_repo_registry_once(tmp_path: Path) -> None:
    legacy_registry_path = tmp_path / "repos.json"
    project_path = _make_git_repo(tmp_path / "code" / "legacy")
    legacy_registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repos": [
                    {
                        "id": "repo-legacy",
                        "name": "Legacy project",
                        "path": str(project_path),
                        "is_git_root": True,
                        "registered_at": "2024-01-01T00:00:00+00:00",
                    }
                ],
            }
        )
    )

    overrides_path = tmp_path / "project_overrides.json"
    store = ProjectOverridesStore(overrides_path, legacy_registry_path=legacy_registry_path)

    record = store.get("repo-legacy")
    assert record is not None
    assert record.display_name == "Legacy project"
    assert record.current_path == project_path
    assert record.registered_at.isoformat() == "2024-01-01T00:00:00+00:00"
    assert overrides_path.exists()

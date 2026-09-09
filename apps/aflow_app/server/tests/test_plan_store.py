"""Plan filesystem and authenticated route contracts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from aflow_app_server.config import ServerConfig
from aflow_app_server.main import app
from aflow_app_server.plan_service import PlanRevisionConflict, PlanService, PlanServiceError
from aflow_app_server.project_registry import ProjectRegistry

TOKEN = "plan-test-token"

@pytest.fixture
def plan_fixture(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    registry = ProjectRegistry(tmp_path, tmp_path / "projects.json")
    registry.register("project", "Project", "project")
    return PlanService(registry), root, registry

def test_plan_lifecycle_preserves_exact_bytes_and_revisions(plan_fixture) -> None:
    service, root, _ = plan_fixture
    original = "# Plan\n\n- [ ] first\n"
    created = service.create("project", "demo.md", original)
    assert created.path == "plans/todo/demo.md"
    assert created.revision == hashlib.sha256(original.encode()).hexdigest()
    assert (root / created.path).read_bytes() == original.encode()

    loaded = service.read("project", "todo", "demo.md")
    updated = service.update("project", "todo", "demo.md", original + "- [ ] second\n", loaded.revision)
    promoted = service.promote("project", "todo", "demo.md", updated.revision)
    done = service.promote("project", "in_progress", "demo.md", promoted.revision)
    assert done.status == "done"
    assert done.path == "plans/done/demo.md"
    assert service.list("project") == (service.list("project", "done")[0],)
    assert (root / done.path).read_text() == original + "- [ ] second\n"

def test_stale_update_and_failed_promotion_preserve_source_bytes(plan_fixture) -> None:
    service, root, _ = plan_fixture
    created = service.create("project", "demo.md", "original")
    with pytest.raises(PlanRevisionConflict) as caught:
        service.update("project", "todo", "demo.md", "replacement", "0" * 64)
    assert caught.value.current_revision == created.revision
    assert (root / "plans/todo/demo.md").read_bytes() == b"original"

    target = root / "plans/in-progress/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing target")
    with pytest.raises(PlanServiceError, match="target already exists"):
        service.promote("project", "todo", "demo.md", created.revision)
    assert (root / "plans/todo/demo.md").read_bytes() == b"original"
    assert target.read_bytes() == b"existing target"

def test_rejects_unsafe_names_content_and_non_regular_files(plan_fixture) -> None:
    service, root, _ = plan_fixture
    for name in ("../escape.md", "nested/escape.md", "not-markdown.txt", ".hidden.md"):
        with pytest.raises(PlanServiceError, match="Markdown filename"):
            service.create("project", name, "x")
    with pytest.raises(PlanServiceError, match="UTF-8"):
        service.create("project", "nul.md", "bad\x00text")
    with pytest.raises(PlanServiceError, match="size limit"):
        service.create("project", "large.md", "x" * (256 * 1024 + 1))

    directory = root / "plans/todo"
    directory.mkdir(parents=True, exist_ok=True)
    outside = root / "outside.md"
    outside.write_text("outside")
    (directory / "linked.md").symlink_to(outside)
    assert service.list("project", "todo") == ()
    with pytest.raises(PlanServiceError, match="regular file"):
        service.read("project", "todo", "linked.md")
    hard = directory / "hard.md"
    os.link(outside, hard)
    with pytest.raises(PlanServiceError, match="regular file"):
        service.read("project", "todo", "hard.md")

def test_project_registry_scope_is_required(plan_fixture, tmp_path: Path) -> None:
    service, _, _ = plan_fixture
    with pytest.raises(PlanServiceError, match="not registered"):
        service.create("missing", "demo.md", "x")
    assert not (tmp_path / "plans").exists()

@pytest.fixture
def plan_client(plan_fixture):
    from aflow_app_server import main
    service, root, registry = plan_fixture
    config = ServerConfig(
        bind_host="127.0.0.1", bind_port=8765, auth_token=TOKEN,
        managed_projects_root=root.parent,
        project_registry_path=registry.path,
    )
    main._config = config
    main._project_registry = registry
    main._plan_service = service
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        main._config = None
        main._project_registry = None
        main._plan_service = None

def test_omitted_and_null_content_create_packaged_template(plan_fixture) -> None:
    """Only null/omission loads the packaged draft skeleton."""
    from importlib.resources import files as resource_files
    from pathlib import Path as _Path

    from aflow.plan import parse_git_tracking_metadata, parse_plan_text

    service, root, _ = plan_fixture
    expected = resource_files("aflow").joinpath("templates/draft-plan.md").read_text(encoding="utf-8")
    omitted = service.create("project", "omitted.md")
    explicit_null = service.create("project", "null.md", None)
    assert omitted.content == expected
    assert explicit_null.content == expected
    assert (root / omitted.path).read_text(encoding="utf-8") == expected

    parsed = parse_plan_text(omitted.content, source_path=_Path("omitted.md"))
    assert len(parsed.sections) == 1
    section = parsed.sections[0]
    assert section.heading_checked is False
    assert section.unchecked_step_count == 2
    assert section.checked_step_count == 0
    metadata = parse_git_tracking_metadata(omitted.content)
    assert metadata is not None
    assert metadata.plan_branch == ""
    assert metadata.pre_handoff_base_head == ""

def test_explicit_content_including_empty_string_is_preserved(plan_fixture) -> None:
    service, root, _ = plan_fixture
    for name, content in (("explicit.md", "# Custom\n"), ("empty.md", "")):
        created = service.create("project", name, content)
        assert created.content == content
        assert (root / created.path).read_text(encoding="utf-8") == content
        assert service.read("project", "todo", name).content == content

def test_template_draft_keeps_normal_lifecycle(plan_fixture) -> None:
    service, _, _ = plan_fixture
    created = service.create("project", "draft.md")
    updated = service.update(
        "project", "todo", "draft.md", created.content + "\nExtra note.\n", created.revision,
    )
    promoted = service.promote("project", "todo", "draft.md", updated.revision)
    assert promoted.status == "in_progress"
    assert promoted.content == updated.content

def test_missing_template_fails_before_writing_any_file(plan_fixture, monkeypatch) -> None:
    import aflow_app_server.plan_service as plan_service_module

    service, root, _ = plan_fixture
    def _missing(*args, **kwargs):
        raise OSError("package resources are unavailable")
    monkeypatch.setattr(plan_service_module, "files", _missing)
    with pytest.raises(PlanServiceError, match="template is unavailable"):
        service.create("project", "broken.md", None)
    assert not (root / "plans/todo/broken.md").exists()

def test_create_routes_accept_omitted_and_null_content(plan_client: TestClient) -> None:
    from importlib.resources import files as resource_files
    from pathlib import Path as _Path

    from aflow.plan import parse_git_tracking_metadata, parse_plan_text

    headers = {"Authorization": f"Bearer {TOKEN}"}
    path = "/api/projects/project/plans"
    expected = resource_files("aflow").joinpath("templates/draft-plan.md").read_text(encoding="utf-8")

    omitted = plan_client.post(path, headers=headers, json={"name": "omitted.md"})
    assert omitted.status_code == 201
    assert omitted.json()["content"] == expected
    parsed = parse_plan_text(omitted.json()["content"], source_path=_Path("omitted.md"))
    assert len(parsed.sections) == 1
    assert parsed.sections[0].unchecked_step_count == 2
    assert parse_git_tracking_metadata(omitted.json()["content"]) is not None

    explicit_null = plan_client.post(path, headers=headers, json={"name": "null.md", "content": None})
    assert explicit_null.status_code == 201
    assert explicit_null.json()["content"] == expected

    explicit_empty = plan_client.post(path, headers=headers, json={"name": "empty.md", "content": ""})
    assert explicit_empty.status_code == 201
    assert explicit_empty.json()["content"] == ""

    explicit = plan_client.post(path, headers=headers, json={"name": "custom.md", "content": "# Custom\n"})
    assert explicit.status_code == 201
    assert explicit.json()["content"] == "# Custom\n"

def test_authenticated_plan_routes_and_removed_remote_routes(plan_client: TestClient) -> None:
    path = "/api/projects/project/plans"
    assert plan_client.get(path).status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    created = plan_client.post(path, headers=headers, json={"name": "route.md", "content": "# route"})
    assert created.status_code == 201
    body = created.json()
    assert body["path"] == "plans/todo/route.md"
    assert plan_client.get(f"{path}/todo/route.md", headers=headers).json()["content"] == "# route"
    updated = plan_client.put(
        f"{path}/todo/route.md", headers=headers,
        json={"content": "# updated", "expected_revision": body["revision"]},
    )
    assert updated.status_code == 200
    promoted = plan_client.post(
        f"{path}/todo/route.md/promote", headers=headers,
        json={"expected_revision": updated.json()["revision"]},
    )
    assert promoted.json()["path"] == "plans/in-progress/route.md"
    assert plan_client.get(path, headers=headers).json()[0]["status"] == "in_progress"

    for removed in (
        "/api/planning/" + "providers",
        "/api/projects/project/planning/" + "sessions",
        "/api/transcribe",
    ):
        assert plan_client.get(removed, headers=headers).status_code == 404

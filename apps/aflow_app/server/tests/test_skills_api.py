"""Authenticated Skills API tests: contract, revisions, shared services.

All HOME/PATH fixtures are disposable: stub harness executables stand in
for the eleven supported harnesses and every canonical store, destination,
and link lives beneath a temporary HOME. No live home is ever mutated.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aflow.skill_catalog import BUNDLED_SKILL_NAMES, DEFAULT_BUNDLED_SKILL_NAMES
from aflow.skill_installer import SUPPORTED_HARNESS_INSTALL_SPECS
from aflow_app_server.config import ServerConfig
from aflow_app_server.main import app


TOKEN = "skills-api-test-token"

HARNESS_EXECUTABLES = (
    "claude",
    "codex",
    "copilot",
    "dsh",
    "gemini",
    "kiro-cli",
    "muse",
    "opencode",
    "pi",
    "reasonix",
    "zcode",
)


@pytest.fixture
def skills_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from aflow_app_server import main

    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for executable in HARNESS_EXECUTABLES:
        stub = bindir / executable
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bindir))
    main._config = ServerConfig(
        bind_host="127.0.0.1",
        bind_port=8765,
        auth_token=TOKEN,
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        yield client, home, monkeypatch
    finally:
        main._config = None


def _store_snapshot(home: Path) -> dict[str, bytes]:
    root = home / ".config" / "aflow" / "skills"
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _edited_content(content: str) -> str:
    return content.rstrip("\n") + "\n\nSaved via API test.\n"


def test_list_returns_exact_bundled_registry(skills_client) -> None:
    client, home, _ = skills_client
    response = client.get("/api/skills")
    assert response.status_code == 200, response.text
    assert TOKEN not in response.text
    entries = response.json()
    assert [entry["name"] for entry in entries] == list(BUNDLED_SKILL_NAMES)
    for entry in entries:
        assert entry["default"] is (entry["name"] != "aflow-assistant")
        assert len(entry["revision"]) == 64
        assert entry["source"] == "bundled"
        assert entry["edited"] is False
        assert entry["installed"] is False
        assert entry["detected_harnesses"] == [
            spec.harness for spec in SUPPORTED_HARNESS_INSTALL_SPECS
        ]
        destinations = [link["destination"] for link in entry["links"]]
        assert destinations == [
            str(home / ".claude" / "skills"),
            str(home / ".agents" / "skills"),
            str(home / ".kiro" / "skills"),
            str(home / ".zcode" / "skills"),
        ]
        assert all(link["linked"] is False for link in entry["links"])
    # Pure read: no store directory is initialized by listing.
    assert not (home / ".config").exists()


def test_protected_skills_routes_require_authentication(skills_client) -> None:
    client, _, _ = skills_client
    from fastapi.testclient import TestClient as _TestClient

    anonymous = _TestClient(app)
    assert anonymous.get("/api/skills").status_code == 401
    assert anonymous.get("/api/skills/aflow-plan").status_code == 401
    assert anonymous.put("/api/skills/aflow-plan", json={}).status_code == 401
    assert anonymous.post("/api/skills/validate", json={}).status_code == 401
    assert anonymous.post("/api/skills/install").status_code == 401
    wrong = _TestClient(app)
    wrong.headers["Authorization"] = "Bearer wrong-token"
    assert wrong.get("/api/skills").status_code == 401


def test_unknown_skill_names_are_404(skills_client) -> None:
    client, _, _ = skills_client
    assert client.get("/api/skills/not-a-skill").status_code == 404
    assert client.get("/api/skills/not-a-skill").json() == {
        "detail": {"code": "skill_not_found"}
    }
    detail = client.get("/api/skills/aflow-plan").json()
    revision = detail["revision"]
    unknown = client.put(
        "/api/skills/not-a-skill",
        json={"content": "x", "expected_revision": revision},
    )
    assert unknown.status_code == 404


def test_save_roundtrip_and_noop(skills_client) -> None:
    client, _, _ = skills_client
    detail = client.get("/api/skills/aflow-plan").json()
    assert detail["content"].startswith("---\n")
    noop = client.put(
        "/api/skills/aflow-plan",
        json={"content": detail["content"], "expected_revision": detail["revision"]},
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["revision"] == detail["revision"]
    assert noop.json()["source"] == "bundled"

    updated = _edited_content(detail["content"])
    saved = client.put(
        "/api/skills/aflow-plan",
        json={"content": updated, "expected_revision": detail["revision"]},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["content"] == updated
    assert body["revision"] == hashlib.sha256(updated.encode()).hexdigest()
    assert body["source"] == "saved"
    assert body["edited"] is True
    assert TOKEN not in saved.text
    reread = client.get("/api/skills/aflow-plan").json()
    assert reread["content"] == updated
    assert reread["revision"] == body["revision"]


def test_save_conflict_preserves_bytes(skills_client) -> None:
    client, _, _ = skills_client
    detail = client.get("/api/skills/aflow-plan").json()
    first = _edited_content(detail["content"])
    saved = client.put(
        "/api/skills/aflow-plan",
        json={"content": first, "expected_revision": detail["revision"]},
    )
    assert saved.status_code == 200
    current_revision = saved.json()["revision"]
    conflict = client.put(
        "/api/skills/aflow-plan",
        json={"content": first + "\nMore.\n", "expected_revision": detail["revision"]},
    )
    assert conflict.status_code == 409
    assert conflict.json() == {
        "detail": {"code": "revision_conflict", "current_revision": current_revision}
    }
    assert client.get("/api/skills/aflow-plan").json()["content"] == first


def test_save_rejects_invalid_content_shape_and_revision(skills_client) -> None:
    client, _, _ = skills_client
    detail = client.get("/api/skills/aflow-manager").json()
    bad_content = client.put(
        "/api/skills/aflow-manager",
        json={"content": "no frontmatter here", "expected_revision": detail["revision"]},
    )
    assert bad_content.status_code == 422
    bad_shape = client.put(
        "/api/skills/aflow-manager",
        json={
            "content": detail["content"],
            "expected_revision": detail["revision"],
            "path": "/tmp/evil.md",
        },
    )
    assert bad_shape.status_code == 422
    bad_revision = client.put(
        "/api/skills/aflow-manager",
        json={"content": detail["content"], "expected_revision": "not-a-digest"},
    )
    assert bad_revision.status_code == 422
    missing_field = client.put(
        "/api/skills/aflow-manager",
        json={"content": detail["content"]},
    )
    assert missing_field.status_code == 422
    # Failed writes preserve prior content byte-for-byte.
    assert client.get("/api/skills/aflow-manager").json()["content"] == detail["content"]


def test_validate_batch_reports_per_entry_without_writes(skills_client) -> None:
    client, home, _ = skills_client
    plan = client.get("/api/skills/aflow-plan").json()
    manager = client.get("/api/skills/aflow-manager").json()
    merge = client.get("/api/skills/aflow-merge").json()
    before = _store_snapshot(home)
    dupes = client.post(
        "/api/skills/validate",
        json={
            "entries": [
                {
                    "name": "aflow-plan",
                    "content": _edited_content(plan["content"]),
                    "expected_revision": plan["revision"],
                },
                {
                    "name": "aflow-plan",
                    "content": "x",
                    "expected_revision": plan["revision"],
                },
            ]
        },
    )
    assert dupes.status_code == 422

    mixed = client.post(
        "/api/skills/validate",
        json={
            "entries": [
                {
                    "name": "aflow-plan",
                    "content": _edited_content(plan["content"]),
                    "expected_revision": plan["revision"],
                },
                {
                    "name": "aflow-manager",
                    "content": "no frontmatter",
                    "expected_revision": manager["revision"],
                },
                {
                    "name": "aflow-merge",
                    "content": merge["content"],
                    "expected_revision": "0" * 64,
                },
                {"name": "not-a-skill", "content": "x", "expected_revision": "0" * 64},
            ]
        },
    )
    assert mixed.status_code == 200, mixed.text
    assert TOKEN not in mixed.text
    entries = mixed.json()["entries"]
    assert entries[0]["ok"] is True
    assert entries[0]["current_revision"] == plan["revision"]
    assert entries[1]["ok"] is False
    assert entries[1]["error_code"] == "skill_invalid"
    assert entries[1]["current_revision"] == manager["revision"]
    assert entries[2]["ok"] is False
    assert entries[2]["error_code"] == "revision_conflict"
    assert entries[2]["current_revision"] == merge["revision"]
    assert entries[3]["ok"] is False
    assert entries[3]["error_code"] == "skill_not_found"
    # Prevalidation never mutates the store.
    assert _store_snapshot(home) == before
    assert client.get("/api/skills/aflow-plan").json()["content"] == plan["content"]

    empty = client.post("/api/skills/validate", json={"entries": []})
    assert empty.status_code == 422


def test_validate_batch_reports_lone_surrogate_as_skill_invalid(skills_client) -> None:
    client, _, _ = skills_client
    manager = client.get("/api/skills/aflow-manager").json()
    # The test client serializes json= bodies with ensure_ascii=False, which
    # cannot encode a lone surrogate; send an ASCII-escaped body instead so
    # the server decodes the JSON \ud800 escape back to a lone surrogate.
    body = json.dumps(
        {
            "entries": [
                {
                    "name": "aflow-manager",
                    "content": "\ud800",
                    "expected_revision": manager["revision"],
                }
            ]
        },
        ensure_ascii=True,
    )
    response = client.post(
        "/api/skills/validate",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    (entry,) = response.json()["entries"]
    assert entry["ok"] is False
    assert entry["error_code"] == "skill_invalid"
    assert entry["current_revision"] == manager["revision"]


def test_install_links_default_skills_and_reinstall_reports_linked(skills_client) -> None:
    client, home, _ = skills_client
    first = client.post("/api/skills/install")
    assert first.status_code == 200, first.text
    assert TOKEN not in first.text
    body = first.json()
    assert body["mode"] == "auto"
    assert body["succeeded"] is True
    assert body["cancelled"] is False
    assert {item["name"] for item in body["refresh"]} == set(DEFAULT_BUNDLED_SKILL_NAMES)
    assert all(item["status"] == "initialized" for item in body["refresh"])
    assert "aflow-assistant" not in {op["skill"] for op in body["operations"]}
    assert len(body["operations"]) == len(DEFAULT_BUNDLED_SKILL_NAMES) * 4
    assert all(op["status"] == "linked" for op in body["operations"])
    link = home / ".agents" / "skills" / "aflow-plan"
    assert link.is_symlink()
    assert os.readlink(link) == str(home / ".config" / "aflow" / "skills" / "aflow-plan")

    second = client.post("/api/skills/install", json={})
    assert second.status_code == 200
    assert all(op["status"] == "already_linked" for op in second.json()["operations"])


def test_save_does_not_install_and_reports_saved_but_uninstalled(skills_client) -> None:
    client, home, _ = skills_client
    detail = client.get("/api/skills/aflow-assistant").json()
    saved = client.put(
        "/api/skills/aflow-assistant",
        json={
            "content": _edited_content(detail["content"]),
            "expected_revision": detail["revision"],
        },
    )
    assert saved.status_code == 200
    listing = {entry["name"]: entry for entry in client.get("/api/skills").json()}
    assert listing["aflow-assistant"]["source"] == "saved"
    assert listing["aflow-assistant"]["installed"] is False

    installed = client.post("/api/skills/install").json()
    assert installed["succeeded"] is True
    listing = {entry["name"]: entry for entry in client.get("/api/skills").json()}
    # Optional skills install only through CLI flags; the default action
    # leaves the saved optional skill uninstalled.
    assert listing["aflow-assistant"]["installed"] is False

    # Saved Markdown propagates through every installed link without reinstall.
    plan = client.get("/api/skills/aflow-plan").json()
    updated = _edited_content(plan["content"])
    assert client.put(
        "/api/skills/aflow-plan",
        json={"content": updated, "expected_revision": plan["revision"]},
    ).status_code == 200
    for destination in (
        home / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".kiro" / "skills",
        home / ".zcode" / "skills",
    ):
        assert (destination / "aflow-plan" / "SKILL.md").read_text() == updated


def test_install_without_targets_is_actionable(skills_client) -> None:
    client, _, monkeypatch = skills_client
    empty = Path(os.environ["HOME"]).parent / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    response = client.post("/api/skills/install")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_install_targets"


def test_install_partial_failure_reports_failed_and_unattempted(skills_client) -> None:
    client, home, _ = skills_client
    claude_dest = home / ".claude" / "skills"
    claude_dest.mkdir(parents=True)
    (claude_dest / "aflow-plan").write_text("legacy real directory blocker")
    response = client.post("/api/skills/install")
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] is False
    first, rest = body["operations"][0], body["operations"][1:]
    assert (first["harness"], first["skill"]) == ("claude", "aflow-plan")
    assert first["status"] == "failed"
    assert first["error_code"] == "file_collision"
    assert all(op["status"] == "unattempted" for op in rest)
    # The canonical refresh already ran before linking stopped the batch.
    assert body["refresh"][0]["name"] == "aflow-plan"


def test_cookie_mutations_require_exact_same_origin(skills_client) -> None:
    client, home, monkeypatch = skills_client
    from fastapi.testclient import TestClient as _TestClient

    secure = _TestClient(app, base_url="https://testserver")
    login = secure.post(
        "/api/session",
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://testserver"},
    )
    assert login.status_code == 200
    detail = client.get("/api/skills/aflow-plan").json()
    payload = {"content": detail["content"], "expected_revision": detail["revision"]}
    assert secure.put("/api/skills/aflow-plan", json=payload).status_code == 403
    assert (
        secure.post(
            "/api/skills/validate",
            json={"entries": [{"name": "aflow-plan", **payload}]},
        ).status_code
        == 403
    )
    assert secure.post("/api/skills/install").status_code == 403
    allowed = secure.put(
        "/api/skills/aflow-plan", json=payload, headers={"Origin": "https://testserver"}
    )
    assert allowed.status_code == 200
    assert home is not None and monkeypatch is not None


def test_reads_and_prevalidation_leave_store_bytes_intact(skills_client) -> None:
    client, home, _ = skills_client
    client.post("/api/skills/install")
    before = _store_snapshot(home)
    assert client.get("/api/skills").status_code == 200
    detail = client.get("/api/skills/aflow-plan").json()
    valid = client.post(
        "/api/skills/validate",
        json={
            "entries": [
                {
                    "name": "aflow-plan",
                    "content": detail["content"],
                    "expected_revision": detail["revision"],
                }
            ]
        },
    )
    assert valid.json()["entries"][0]["ok"] is True
    assert _store_snapshot(home) == before

"""Checkpoint 10 integrated regression: links, live skill saves, supervision paths.

Disposable end-to-end journey across the shared services with no external
model call: install directory links into fake harness homes, save manager
Markdown through the revisioned store (the same service the Skills API and
web UI use), prove the next fake manager invocation reads the new bytes,
then set workflow defaults true plus a disabled override, save/reload the
TOML pair, and prove the enabled/disabled run paths diverge.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from aflow.config import load_workflow_config
from aflow.manager import build_manager_prompts, resolve_manager_role
from aflow.skill_installer import install_skills
from aflow.skill_store import SkillStore
from tests._support import _write_split_config

_AFLOW_TEXT = (
    '[harness.codex.profiles.nano]\nmodel = "nano"\n\n'
    '[roles]\nworker = "codex.nano"\nmanager_lite = "codex.nano"\nmanager_full = "codex.nano"\n\n'
    '[manager]\nlite_role = "manager_lite"\nfull_role = "manager_full"\n\n'
    '[prompts]\np = "do it"\n'
)

_WORKFLOWS_TEXT = (
    '[workflow]\nmanager_enabled = true\n\n'
    '[workflow.managed.steps.s]\nrole = "worker"\nprompts = ["p"]\ngo = [{ to = "END" }]\n\n'
    '[workflow.quiet]\nmanager_enabled = false\n\n'
    '[workflow.quiet.steps.s]\nrole = "worker"\nprompts = ["p"]\ngo = [{ to = "END" }]\n'
)

_MARKER = "\nIntegration regression marker: supervision journey.\n"


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_links_live_skill_save_and_supervision_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for executable in ("claude", "codex"):
        _write_executable(bin_dir / executable)
    monkeypatch.setenv("PATH", str(bin_dir))

    store = SkillStore()
    assert store.root == home / ".config" / "aflow" / "skills"

    result = install_skills(yes=True, store=store, stdout=io.StringIO())
    assert result.cancelled is False
    assert result.succeeded is True
    canonical = store.root / "aflow-manager"
    for destination in (home / ".claude" / "skills", home / ".agents" / "skills"):
        link = destination / "aflow-manager"
        assert link.is_symlink()
        assert os.readlink(link) == str(canonical)

    before = store.read("aflow-manager")
    # Installation already materialized the canonical tree from the package.
    assert before.source == "saved"
    edited = before.content + _MARKER
    ack = store.save("aflow-manager", edited, expected_revision=before.revision)
    assert ack.changed is True
    after = store.read("aflow-manager")
    assert after.source == "saved"
    assert after.content == edited
    # Saved bytes are immediately visible through every installed link.
    for destination in (home / ".claude" / "skills", home / ".agents" / "skills"):
        assert (destination / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == edited

    # The next fake manager invocation uses the saved body; no model call.
    context = {
        "level": "lite",
        "run_id": "integration-run",
        "controller_state": {"eligible_actions": ["continue"]},
    }
    system, _ = build_manager_prompts(context, store=store)
    assert _MARKER.strip() in system
    assert not system.startswith("---")

    # Workflow supervision: defaults true with a disabled override, saved and
    # reloaded from the TOML pair like the Settings save path persists them.
    aflow_path, workflows_path = _write_split_config(
        tmp_path / "project", _AFLOW_TEXT, _WORKFLOWS_TEXT
    )
    config = load_workflow_config(aflow_path)
    assert config.workflows["managed"].manager_enabled is True
    assert config.workflows["quiet"].manager_enabled is False

    resolved = resolve_manager_role(
        config, level="lite", baseline_team=None, workflow_name="managed"
    )
    assert resolved.selector == "codex.nano"
    with pytest.raises(ValueError, match="disabled for workflow 'quiet'"):
        resolve_manager_role(
            config, level="lite", baseline_team=None, workflow_name="quiet"
        )

    # Removing the override re-inherits the default after a save/reload cycle.
    workflows_path.write_text(
        _WORKFLOWS_TEXT.replace(
            '[workflow.quiet]\nmanager_enabled = false\n\n', "[workflow.quiet]\n"
        ),
        encoding="utf-8",
    )
    reloaded = load_workflow_config(aflow_path)
    assert reloaded.workflows["quiet"].manager_enabled is True
    assert (
        resolve_manager_role(
            reloaded, level="lite", baseline_team=None, workflow_name="quiet"
        ).selector
        == "codex.nano"
    )

"""Tests for explicit refresh of unedited bundled skill trees.

Covers package v1→v2 refresh, whole-tree preservation of edited skills,
exact reversion, missing metadata adoption/protection, changed and removed
support resources, refresh/save lock contention, failure rollback, and
interrupted-refresh recovery. The store is exercised with an injected
package loader so "v1" and "v2" upgrades are fully deterministic; one test
refreshes the real bundled package through the default loader.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading
import time

import pytest

import aflow.skill_store as skill_store_module
from aflow.skill_store import (
    SkillRefreshIncomplete,
    SkillRevisionConflict,
    SkillStore,
    SkillStoreError,
    sha256_revision,
)

SKILL = "aflow-manager"


def _doc(body: str) -> str:
    return (
        "---\n"
        f"name: {SKILL}\n"
        'description: "Refresh fixture skill."\n'
        "---\n\n" + body
    )


V1_SKILL_MD = _doc("# V1 Body\n\nOriginal packaged instructions.\n")
V2_SKILL_MD = _doc("# V2 Body\n\nUpgraded packaged instructions.\n")
V1_NOTES = b"v1 reference notes\n"
V2_NOTES = b"v2 reference notes\n"
V1_OLD = b"obsolete v1 sidecar\n"
V2_SCRIPT = b"#!/bin/sh\nexit 0\n"
EDITED_SKILL_MD = _doc("# Edited Body\n\nLocally edited instructions.\n")


def _tree(entries: list[tuple[str, bytes | str, int]]) -> list[tuple[str, bytes, int]]:
    return [
        (relative, payload.encode("utf-8") if isinstance(payload, str) else payload, mode)
        for relative, payload, mode in entries
    ]


def _v1_tree() -> list[tuple[str, bytes, int]]:
    return _tree(
        [
            ("SKILL.md", V1_SKILL_MD, 0o644),
            ("references/notes.md", V1_NOTES, 0o644),
            ("references/old.md", V1_OLD, 0o644),
        ]
    )


def _v2_tree() -> list[tuple[str, bytes, int]]:
    return _tree(
        [
            ("SKILL.md", V2_SKILL_MD, 0o644),
            ("references/notes.md", V2_NOTES, 0o644),
            ("scripts/new.sh", V2_SCRIPT, 0o755),
        ]
    )


class _FakePackages:
    """Injectable package loader whose available version can be switched."""

    def __init__(self) -> None:
        self._trees = {SKILL: _v1_tree()}
        self.lock = threading.Lock()

    def set_version(self, version: str) -> None:
        with self.lock:
            self._trees[SKILL] = _v1_tree() if version == "v1" else _v2_tree()

    def __call__(self, name: str) -> list[tuple[str, bytes, int]]:
        with self.lock:
            if name in self._trees:
                return list(self._trees[name])
        fallback = (
            "---\n"
            f"name: {name}\n"
            'description: "Fallback fixture skill."\n'
            "---\n\nFallback body.\n"
        )
        return [("SKILL.md", fallback.encode("utf-8"), 0o644)]


def _store(tmp_path: Path, packages: _FakePackages | None = None) -> SkillStore:
    return SkillStore(tmp_path / "skills", package_tree_loader=packages)


def _tree_state(root: Path, name: str) -> dict[str, bytes]:
    state: dict[str, bytes] = {}
    for path in sorted((root / name).rglob("*")):
        if path.is_file():
            state[str(path.relative_to(root / name))] = path.read_bytes()
    return state


def _baseline(root: Path, name: str) -> dict[str, str]:
    metadata = json.loads((root / ".metadata" / f"{name}.json").read_text("utf-8"))
    return metadata


def test_missing_canonical_tree_is_initialized_by_refresh(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "initialized"
    assert result.changed is True
    assert _tree_state(root, SKILL)["SKILL.md"].decode("utf-8") == V1_SKILL_MD
    baseline = _baseline(root, SKILL)
    assert baseline["files"]["SKILL.md"] == sha256_revision(V1_SKILL_MD.encode("utf-8"))
    document = store.read(SKILL)
    assert document.source == "saved"


def test_unedited_v1_tree_becomes_v2_with_added_and_removed_files(
    tmp_path: Path,
) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    packages.set_version("v2")

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "refreshed"
    assert result.changed is True
    assert result.edited is False
    state = _tree_state(root, SKILL)
    assert state["SKILL.md"].decode("utf-8") == V2_SKILL_MD
    assert state["references/notes.md"] == V2_NOTES
    assert state["scripts/new.sh"] == V2_SCRIPT
    assert "references/old.md" not in state, "obsolete packaged file must be removed"
    mode = stat.S_IMODE(os.stat(root / SKILL / "scripts" / "new.sh").st_mode)
    assert mode == 0o755, "added files keep their packaged permission intent"
    baseline = _baseline(root, SKILL)
    assert baseline["files"]["SKILL.md"] == sha256_revision(V2_SKILL_MD.encode("utf-8"))
    assert baseline["files"]["references/notes.md"] == sha256_revision(V2_NOTES)
    assert "references/old.md" not in baseline["files"]
    assert not list((root / ".metadata").glob(f"*{'.refresh.json'}"))
    assert not (root / ".metadata" / "staging").exists()


def test_refresh_is_idempotent_for_an_identical_package(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    before = _tree_state(root, SKILL)

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "refreshed"
    assert result.changed is False
    assert _tree_state(root, SKILL) == before


def test_edited_tree_is_preserved_whole_and_reported(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    store.save(SKILL, EDITED_SKILL_MD, store.read(SKILL).revision)
    packages.set_version("v2")

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "preserved_edited"
    assert result.edited is True
    assert result.changed is False
    state = _tree_state(root, SKILL)
    assert state["SKILL.md"].decode("utf-8") == EDITED_SKILL_MD
    assert state["references/notes.md"] == V1_NOTES, "the whole tree is preserved"
    assert state["references/old.md"] == V1_OLD
    baseline = _baseline(root, SKILL)
    assert baseline["files"]["SKILL.md"] == sha256_revision(V1_SKILL_MD.encode("utf-8")), (
        "a preserved refresh never advances the baseline"
    )


def test_edited_support_resource_is_preserved(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    (root / SKILL / "references" / "notes.md").write_bytes(b"user customized notes\n")
    packages.set_version("v2")

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "preserved_edited"
    assert (
        root / SKILL / "references" / "notes.md"
    ).read_bytes() == b"user customized notes\n"


def test_extra_user_file_makes_the_tree_edited(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    (root / SKILL / "user-notes.txt").write_bytes(b"user added\n")

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "preserved_edited"
    assert result.edited is True
    assert (root / SKILL / "user-notes.txt").read_bytes() == b"user added\n"
    assert (root / SKILL / "SKILL.md").read_bytes() == V1_SKILL_MD.encode("utf-8")


def test_missing_user_file_makes_the_tree_edited_and_unrepaired(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    (root / SKILL / "references" / "notes.md").unlink()

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "preserved_edited"
    assert result.edited is True
    assert not (root / SKILL / "references" / "notes.md").exists(), (
        "preserving an edited tree never repairs or rewrites it"
    )
    assert (root / SKILL / "SKILL.md").read_bytes() == V1_SKILL_MD.encode("utf-8")


def test_symlink_inside_the_tree_makes_it_edited_without_traversal(
    tmp_path: Path,
) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside bytes\n")
    link = root / SKILL / "references" / "notes.md"
    link.unlink()
    link.symlink_to(outside)

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "preserved_edited"
    assert outside.read_bytes() == b"outside bytes\n"
    assert link.is_symlink()


def test_multi_skill_refresh_reports_each_skill_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL, "aflow-plan"])
    packages.set_version("v2")
    real_atomic_write = skill_store_module._atomic_write

    def crashing_write(path, payload, *args, **kwargs):
        if Path(path).name == "notes.md" and payload == V2_NOTES:
            raise OSError("simulated I/O failure")
        return real_atomic_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(skill_store_module, "_atomic_write", crashing_write)
    results = store.refresh_skills([SKILL, "aflow-plan"])
    monkeypatch.undo()

    by_name = {result.name: result for result in results}
    assert by_name[SKILL].status == "failed"
    assert by_name["aflow-plan"].status == "refreshed"
    assert _tree_state(root, SKILL)["SKILL.md"].decode("utf-8") == V1_SKILL_MD
    assert _tree_state(root, "aflow-plan")["SKILL.md"].decode("utf-8") != V1_SKILL_MD
    assert store.read(SKILL).content == V1_SKILL_MD


def test_exact_reversion_makes_the_tree_unedited_again(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    store.save(SKILL, EDITED_SKILL_MD, store.read(SKILL).revision)
    store.save(SKILL, V1_SKILL_MD, store.read(SKILL).revision)
    packages.set_version("v2")

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "refreshed"
    assert result.changed is True
    assert _tree_state(root, SKILL)["SKILL.md"].decode("utf-8") == V2_SKILL_MD


def test_missing_metadata_is_adopted_only_on_exact_package_match(
    tmp_path: Path,
) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    (root / ".metadata" / f"{SKILL}.json").unlink()

    (adopted,) = store.refresh_skills([SKILL])

    assert adopted.status == "refreshed"
    assert adopted.changed is False
    assert _baseline(root, SKILL)["files"]["SKILL.md"] == sha256_revision(
        V1_SKILL_MD.encode("utf-8")
    )


def test_missing_metadata_on_mismatch_is_protected_and_preserved(
    tmp_path: Path,
) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    (root / ".metadata" / f"{SKILL}.json").unlink()
    packages.set_version("v2")

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "preserved_edited"
    assert result.edited is True
    metadata = _baseline(root, SKILL)
    assert metadata["files"] is None
    assert metadata["protected"] is True
    assert _tree_state(root, SKILL)["SKILL.md"].decode("utf-8") == V1_SKILL_MD

    (again,) = store.refresh_skills([SKILL])
    assert again.status == "preserved_edited", "unknown baselines are never refreshed"


def test_failure_rollback_restores_prior_tree_and_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    packages.set_version("v2")
    real_atomic_write = skill_store_module._atomic_write

    def crashing_write(path, payload, *args, **kwargs):
        if Path(path).name == "notes.md" and payload == V2_NOTES:
            raise OSError("simulated I/O failure")
        return real_atomic_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(skill_store_module, "_atomic_write", crashing_write)
    (result,) = store.refresh_skills([SKILL])
    monkeypatch.undo()

    assert result.status == "failed"
    assert result.error is not None
    assert _tree_state(root, SKILL) == {
        "SKILL.md": V1_SKILL_MD.encode("utf-8"),
        "references/notes.md": V1_NOTES,
        "references/old.md": V1_OLD,
    }, "an ordinary I/O error restores the prior per-skill tree"
    baseline = _baseline(root, SKILL)
    assert baseline["files"]["SKILL.md"] == sha256_revision(V1_SKILL_MD.encode("utf-8")), (
        "failed refreshes never advance the baseline"
    )
    assert not (root / ".metadata" / f"{SKILL}.refresh.json").exists()
    assert not (root / ".metadata" / "staging").exists()
    assert store.read(SKILL).content == V1_SKILL_MD


class _SimulatedCrash(BaseException):
    """Escapes ordinary exception handling exactly like a process crash."""


def test_interrupted_refresh_blocks_reads_until_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    packages.set_version("v2")
    real_atomic_write = skill_store_module._atomic_write

    def crashing_write(path, payload, *args, **kwargs):
        if Path(path).name == "notes.md" and payload == V2_NOTES:
            raise _SimulatedCrash("process died mid-refresh")
        return real_atomic_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(skill_store_module, "_atomic_write", crashing_write)
    with pytest.raises(_SimulatedCrash):
        store.refresh_skills([SKILL])
    monkeypatch.undo()

    marker = root / ".metadata" / f"{SKILL}.refresh.json"
    assert marker.is_file(), "the interrupted transaction is detectable"
    with pytest.raises(SkillRefreshIncomplete):
        store.read(SKILL)
    with pytest.raises(SkillRefreshIncomplete):
        store.list_skills()
    with pytest.raises(SkillRefreshIncomplete):
        store.save(SKILL, EDITED_SKILL_MD, "0" * 64)

    (result,) = store.refresh_skills([SKILL])

    assert result.status == "refreshed"
    assert _tree_state(root, SKILL)["SKILL.md"].decode("utf-8") == V2_SKILL_MD
    assert _baseline(root, SKILL)["files"]["SKILL.md"] == sha256_revision(
        V2_SKILL_MD.encode("utf-8")
    )
    assert not marker.exists()
    assert not (root / ".metadata" / "staging").exists()
    assert store.read(SKILL).content == V2_SKILL_MD


def test_uncertain_rollback_keeps_the_refresh_error_visible(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    packages.set_version("v2")
    store.refresh_skills([SKILL])
    # Forge a crash state whose tree no longer matches the transaction: the
    # rollback cannot be verified, so content must stay protected and loud.
    marker_path = root / ".metadata" / f"{SKILL}.refresh.json"
    marker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skill": SKILL,
                "metadata_sha256": "0" * 64,
                "files": {
                    "SKILL.md": {"old": sha256_revision(V1_SKILL_MD.encode("utf-8")), "new": None}
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SkillRefreshIncomplete):
        store.read(SKILL)

    (result,) = store.refresh_skills([SKILL])
    assert result.status == "failed"
    assert marker_path.exists(), "uncertain markers are never silently retired"


def test_refresh_and_save_contend_on_the_same_store_lock(tmp_path: Path) -> None:
    packages = _FakePackages()
    root = tmp_path / "skills"
    store = _store(tmp_path, packages)
    store.refresh_skills([SKILL])
    packages.set_version("v2")

    # Direction 1: refresh waits for a save-held store lock.
    outcomes: list[object] = []
    with store._write_lock():
        worker = threading.Thread(
            target=lambda: outcomes.append(store.refresh_skills([SKILL])[0])
        )
        worker.start()
        time.sleep(0.2)
        assert worker.is_alive(), "refresh must wait for the store lock"
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcomes and outcomes[0].status == "refreshed"

    # Direction 2: a save waits for an in-flight refresh, then CASes against
    # the refreshed bytes — its stale v1 revision loses deterministically.
    stale_revision = sha256_revision(V1_SKILL_MD.encode("utf-8"))
    gate = threading.Event()
    plain_loader = _FakePackages.__call__

    def gated_loader(self, name: str):
        tree = plain_loader(self, name)
        gate.wait(timeout=10)
        return tree

    gated_store = SkillStore(root, package_tree_loader=lambda name: gated_loader(packages, name))
    refresh_outcomes: list[object] = []
    save_outcomes: list[object] = []
    refresh_thread = threading.Thread(
        target=lambda: refresh_outcomes.append(gated_store.refresh_skills([SKILL])[0])
    )
    def _contending_save() -> None:
        try:
            save_outcomes.append(gated_store.save(SKILL, EDITED_SKILL_MD, stale_revision))
        except Exception as exc:  # the conflict itself is the expected outcome
            save_outcomes.append(exc)

    save_thread = threading.Thread(target=_contending_save)
    refresh_thread.start()
    time.sleep(0.2)
    assert refresh_thread.is_alive(), "the gated refresh should still be in flight"
    save_thread.start()
    time.sleep(0.2)
    assert save_thread.is_alive(), "save must wait for the in-flight refresh"
    gate.set()
    refresh_thread.join(timeout=5)
    save_thread.join(timeout=5)
    assert refresh_outcomes[0].status == "refreshed"
    assert isinstance(save_outcomes[0], SkillRevisionConflict)
    assert save_outcomes[0].current_revision == sha256_revision(V2_SKILL_MD.encode("utf-8"))
    assert store.read(SKILL).content == V2_SKILL_MD


def test_refresh_rejects_unknown_and_unregistered_names(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(SkillStoreError, match="Unknown bundled skill"):
        store.refresh_skills(["not-a-skill"])


def test_real_bundled_package_refresh_keeps_edits_and_idempotence(
    tmp_path: Path,
) -> None:
    from tests.test_skill_store import _package_skill_bytes

    root = tmp_path / "skills"
    store = SkillStore(root)  # default loader over the real package resources
    (first,) = store.refresh_skills([SKILL])
    assert first.status == "initialized"
    store.save(SKILL, EDITED_SKILL_MD, store.read(SKILL).revision)

    (preserved,) = store.refresh_skills([SKILL])
    assert preserved.status == "preserved_edited"
    assert store.read(SKILL).content == EDITED_SKILL_MD

    store.save(SKILL, _package_skill_bytes(SKILL).decode("utf-8"), store.read(SKILL).revision)
    (refreshed,) = store.refresh_skills([SKILL])
    assert refreshed.status == "refreshed"
    assert refreshed.changed is False

    (repeat,) = store.refresh_skills([SKILL])
    assert repeat.status == "refreshed"
    assert repeat.changed is False

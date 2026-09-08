"""Tests for the canonical skill document store.

Covers pure package-fallback reads without writes, first-save complete
materialization with baseline metadata, revision compare-and-swap conflicts,
external modification, document validation, symlink/path rejection, failed
writes, and concurrent saves. Ownership model under test: the store owns only
``<root>/<name>/`` trees it initialized plus ``<root>/.metadata/``; reads
never create, refresh, or reinstall anything, and a failed save always leaves
the previously saved bytes intact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading

import pytest
from importlib import resources

from aflow.skill_store import (
    MAX_SKILL_DOCUMENT_BYTES,
    SkillDocument,
    SkillRevisionConflict,
    SkillStore,
    SkillStoreError,
    SkillValidationError,
    parse_skill_document,
    sha256_revision,
    validate_skill_document,
)


def _package_skill_bytes(skill_name: str, relative: str = "SKILL.md") -> bytes:
    resource = resources.files("aflow").joinpath("bundled_skills", skill_name, relative)
    with resource.open("rb") as handle:
        return handle.read()


def _document(
    skill_name: str = "aflow-manager",
    body: str = "# Edited Body\n\nSaved instructions live outside the package.\n",
    extra_frontmatter: str = "",
) -> str:
    lines = [
        "---",
        f"name: {skill_name}",
        'description: "Test skill used by the store suite."',
    ]
    if extra_frontmatter:
        lines.append(extra_frontmatter)
    lines.extend(["---", "", body])
    return "\n".join(lines)


def _tree_digest(root) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            entries.append((str(path.relative_to(root)), path.read_bytes()))
    return entries


def test_default_root_uses_the_executing_account_home() -> None:
    store = SkillStore()
    assert store.root == Path("~/.config/aflow/skills").expanduser()


def test_pure_read_falls_back_to_package_without_creating_the_store(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)

    document = store.read("aflow-manager")

    assert isinstance(document, SkillDocument)
    assert document.source == "bundled"
    assert document.content == _package_skill_bytes("aflow-manager").decode("utf-8")
    assert document.revision == sha256_revision(_package_skill_bytes("aflow-manager"))
    assert not root.exists(), "pure reads must not create the store"


def test_list_skills_is_pure_and_covers_the_registered_inventory(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)

    documents = store.list_skills()

    assert {document.name for document in documents} >= {"aflow-manager", "aflow-assistant"}
    assert all(document.source == "bundled" for document in documents)
    assert not root.exists(), "pure list must not create the store"


def test_read_of_unknown_nonbundled_name_is_a_bounded_error(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")

    with pytest.raises(SkillStoreError, match="no saved canonical document"):
        store.read("not-a-skill")


def test_first_save_materializes_the_complete_bundled_directory(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    packaged_revision = store.read("aflow-assistant").revision
    package_root = Path(resources.files("aflow")).joinpath(
        "bundled_skills", "aflow-assistant"
    )
    packaged_tree = _tree_digest(package_root)
    saved = _document("aflow-assistant")

    result = store.save("aflow-assistant", saved, packaged_revision)

    assert result.materialized is True
    assert result.changed is True
    assert result.revision == sha256_revision(saved.encode("utf-8"))
    skill_dir = root / "aflow-assistant"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == saved
    for relative, payload in packaged_tree:
        if relative == "SKILL.md":
            continue
        assert (skill_dir / relative).read_bytes() == payload
        source_mode = stat.S_IMODE(os.stat(package_root / relative).st_mode)
        materialized_mode = stat.S_IMODE(os.stat(skill_dir / relative).st_mode)
        assert materialized_mode == source_mode, relative


def test_first_initialization_records_version_1_package_baseline_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    packaged = _package_skill_bytes("aflow-manager")
    saved = _document("aflow-manager")

    store.save("aflow-manager", saved, sha256_revision(packaged))

    metadata_path = root / ".metadata" / "aflow-manager.json"
    assert metadata_path.is_file()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["schema_version"] == 1
    assert metadata["files"]["SKILL.md"] == sha256_revision(packaged), (
        "the baseline records the materialized package bytes, not the saved text"
    )
    assert (root / ".metadata" / "store.lock").is_file()


def test_second_save_replaces_only_skill_md_and_keeps_supporting_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    store.save(
        "aflow-assistant", _document("aflow-assistant"), store.read("aflow-assistant").revision
    )
    supporting = (root / "aflow-assistant" / "references" / "engine-map.md").read_bytes()

    second = _document("aflow-assistant", body="# Second Body\n\nMore edits.\n")
    result = store.save("aflow-assistant", second, store.read("aflow-assistant").revision)

    assert result.materialized is False
    assert result.changed is True
    assert (root / "aflow-assistant" / "SKILL.md").read_text(encoding="utf-8") == second
    assert (
        root / "aflow-assistant" / "references" / "engine-map.md"
    ).read_bytes() == supporting
    metadata = json.loads(
        (root / ".metadata" / "aflow-assistant.json").read_text(encoding="utf-8")
    )
    assert metadata["files"]["SKILL.md"] == sha256_revision(
        _package_skill_bytes("aflow-assistant")
    ), "user saves never advance the package baseline"


def test_conflicting_revision_is_rejected_and_bytes_survive(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    store.save("aflow-manager", _document(), store.read("aflow-manager").revision)
    before = (root / "aflow-manager" / "SKILL.md").read_bytes()

    with pytest.raises(SkillRevisionConflict) as conflict:
        store.save("aflow-manager", _document(body="# Different\n"), "0" * 64)

    assert conflict.value.current_revision == store.read("aflow-manager").revision
    assert (root / "aflow-manager" / "SKILL.md").read_bytes() == before


def test_first_save_conflict_reports_bundled_revision_without_materializing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)

    with pytest.raises(SkillRevisionConflict) as conflict:
        store.save("aflow-manager", _document(), "0" * 64)

    assert conflict.value.current_revision == sha256_revision(
        _package_skill_bytes("aflow-manager")
    )
    assert not (root / "aflow-manager").exists()
    assert not (root / ".metadata" / "aflow-manager.json").exists()


def test_external_modification_is_visible_and_invalidates_stale_revisions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    store.save("aflow-manager", _document(), store.read("aflow-manager").revision)
    stale_revision = store.read("aflow-manager").revision
    externally_edited = _document(body="# Externally Edited\n\nChanged on disk.\n")

    (root / "aflow-manager" / "SKILL.md").write_text(externally_edited, encoding="utf-8")

    document = store.read("aflow-manager")
    assert document.source == "saved"
    assert document.content == externally_edited
    assert document.revision != stale_revision
    with pytest.raises(SkillRevisionConflict):
        store.save("aflow-manager", _document(body="# Lost\n"), stale_revision)


def test_identical_bytes_are_a_no_op(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    saved = _document()
    store.save("aflow-manager", saved, store.read("aflow-manager").revision)
    before = os.stat(root / "aflow-manager" / "SKILL.md")

    result = store.save("aflow-manager", saved, store.read("aflow-manager").revision)

    assert result.changed is False
    after = os.stat(root / "aflow-manager" / "SKILL.md")
    assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)


@pytest.mark.parametrize(
    "content",
    [
        "no frontmatter at all\n",
        "name: aflow-manager\n",
        "---\nname: aflow-manager\ndescription: \"x\"\n",
        "---\nname: other-skill\ndescription: \"x\"\n---\n\nBody.\n",
        "---\nname: [aflow-manager]\ndescription: \"x\"\n---\n\nBody.\n",
        "---\nname: aflow-manager\ndescription: \"\"\n---\n\nBody.\n",
        "---\nname: aflow-manager\ndescription: 123\n---\n\nBody.\n",
        "---\nname: aflow-manager\n---\n\nBody.\n",
        "---\nname: aflow-manager\ndescription: \"x\"\n---\n",
        "---\nname: aflow-manager\ndescription: \"unterminated\n---\n\nBody.\n",
        "---\nname: aflow-manager\ndescription: \"x\"\n---\n\nBody.\n\x00",
    ],
)
def test_invalid_documents_are_rejected(tmp_path: Path, content: str) -> None:
    store = SkillStore(tmp_path / "skills")
    with pytest.raises(SkillValidationError):
        store.save(
            "aflow-manager",
            content,
            sha256_revision(_package_skill_bytes("aflow-manager")),
        )


def test_frontmatter_yaml_aliases_are_rejected(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    content = (
        "---\n"
        "name: aflow-manager\n"
        'description: "x"\n'
        "anchor: &a value\n"
        "copy: *a\n"
        "---\n\nBody.\n"
    )
    with pytest.raises(SkillValidationError, match="anchors or aliases"):
        store.save(
            "aflow-manager",
            content,
            sha256_revision(_package_skill_bytes("aflow-manager")),
        )


def test_oversized_documents_are_rejected(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    payload = _document(body="x" * (MAX_SKILL_DOCUMENT_BYTES + 1))
    with pytest.raises(SkillValidationError, match="too large"):
        store.save(
            "aflow-manager",
            payload,
            sha256_revision(_package_skill_bytes("aflow-manager")),
        )


def test_non_utf8_bytes_are_rejected() -> None:
    with pytest.raises(SkillValidationError, match="UTF-8"):
        validate_skill_document("aflow-manager", b"\xff\xfe body without NUL")


def test_extra_safe_frontmatter_fields_are_permitted(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    saved = _document(
        "aflow-manager",
        extra_frontmatter='metadata:\n  version: 2\n  tags: ["a", "b"]',
    )

    result = store.save(
        "aflow-manager", saved, sha256_revision(_package_skill_bytes("aflow-manager"))
    )

    assert result.changed is True
    assert (root / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == saved


def test_invalid_save_over_an_existing_skill_leaves_bytes_intact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    saved = _document()
    store.save("aflow-manager", saved, store.read("aflow-manager").revision)

    with pytest.raises(SkillValidationError):
        store.save(
            "aflow-manager",
            _document(body="# Broken\n\x00"),
            store.read("aflow-manager").revision,
        )

    assert (root / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == saved


def test_save_is_restricted_to_registered_bundled_names(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")

    with pytest.raises(SkillStoreError, match="Unknown bundled skill"):
        store.save("not-a-skill", _document("not-a-skill"), "0" * 64)


def test_canonical_skill_directory_symlink_is_rejected_without_touching_the_referent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside\n", encoding="utf-8")
    root.mkdir()
    (root / "aflow-manager").symlink_to(outside)
    store = SkillStore(root)

    with pytest.raises(SkillStoreError, match="symbolic link"):
        store.read("aflow-manager")
    with pytest.raises(SkillStoreError, match="symbolic link"):
        store.save(
            "aflow-manager",
            _document(),
            sha256_revision(_package_skill_bytes("aflow-manager")),
        )
    assert (outside / "SKILL.md").read_text(encoding="utf-8") == "# Outside\n"


def test_canonical_skill_md_symlink_is_rejected_without_touching_the_referent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    store.save("aflow-manager", _document(), store.read("aflow-manager").revision)
    outside = tmp_path / "target.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (root / "aflow-manager" / "SKILL.md").unlink()
    (root / "aflow-manager" / "SKILL.md").symlink_to(outside)

    with pytest.raises(SkillStoreError, match="symbolic link"):
        store.read("aflow-manager")
    with pytest.raises(SkillStoreError, match="symbolic link"):
        store.save("aflow-manager", _document(body="# New\n"), "0" * 64)
    assert outside.read_text(encoding="utf-8") == "# Outside\n"


def test_symlinked_baseline_metadata_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    store.save("aflow-manager", _document(), store.read("aflow-manager").revision)
    metadata_path = root / ".metadata" / "aflow-manager.json"
    metadata_path.unlink()
    metadata_path.symlink_to(tmp_path / "elsewhere.json")

    with pytest.raises(SkillStoreError, match="regular file"):
        store.save(
            "aflow-manager",
            _document(body="# Next\n"),
            store.read("aflow-manager").revision,
        )


def test_unsafe_store_root_is_a_bounded_error(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.write_text("not a directory", encoding="utf-8")
    store = SkillStore(root)

    with pytest.raises(SkillStoreError, match="not a directory"):
        store.save(
            "aflow-manager",
            _document(),
            sha256_revision(_package_skill_bytes("aflow-manager")),
        )
    assert root.read_text(encoding="utf-8") == "not a directory"


def test_corrupt_or_unsupported_baseline_metadata_is_a_bounded_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    store.save("aflow-manager", _document(), store.read("aflow-manager").revision)
    metadata_path = root / ".metadata" / "aflow-manager.json"
    revision = store.read("aflow-manager").revision

    metadata_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SkillStoreError, match="unreadable"):
        store.save("aflow-manager", _document(body="# Next\n"), revision)

    metadata_path.write_text(
        json.dumps({"schema_version": 99, "files": {"SKILL.md": "0" * 64}}),
        encoding="utf-8",
    )
    with pytest.raises(SkillStoreError, match="unsupported schema"):
        store.save("aflow-manager", _document(body="# Next\n"), revision)

    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": {"../escape.md": "0" * 64},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SkillStoreError, match="unsafe skill resource path"):
        store.save("aflow-manager", _document(body="# Next\n"), revision)
    assert (root / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == _document()


def test_hand_created_unmanaged_tree_is_not_silently_adopted(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    skill_dir = root / "aflow-manager"
    skill_dir.mkdir(parents=True)
    packaged = _package_skill_bytes("aflow-manager")
    (skill_dir / "SKILL.md").write_bytes(packaged)

    with pytest.raises(SkillStoreError, match="not store-managed"):
        store.save("aflow-manager", _document(), sha256_revision(packaged))


def test_unmanaged_nonempty_directory_blocks_initialization(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    skill_dir = root / "aflow-manager"
    skill_dir.mkdir(parents=True)
    (skill_dir / "user-file.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(SkillStoreError, match="unmanaged"):
        store.save(
            "aflow-manager",
            _document(),
            sha256_revision(_package_skill_bytes("aflow-manager")),
        )
    assert (skill_dir / "user-file.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (skill_dir / "SKILL.md").exists()


def test_malformed_canonical_document_is_an_error_not_a_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    store.save("aflow-manager", _document(), store.read("aflow-manager").revision)
    (root / "aflow-manager" / "SKILL.md").write_text("# just markdown\n", encoding="utf-8")

    with pytest.raises(SkillValidationError):
        store.read("aflow-manager")


def test_failed_atomic_write_leaves_saved_bytes_and_no_temporaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    saved = _document()
    store.save("aflow-manager", saved, store.read("aflow-manager").revision)

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(SkillStoreError, match="cannot replace skill document"):
        store.save(
            "aflow-manager",
            _document(body="# Unwritten\n"),
            store.read("aflow-manager").revision,
        )
    monkeypatch.undo()

    assert (root / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == saved
    assert list((root / "aflow-manager").glob(".*.tmp")) == []


def test_failed_materialization_cleans_staging_and_leaves_the_store_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)

    def failing_rename(src, dst, *args, **kwargs):
        raise OSError("rename refused")

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(SkillStoreError, match="cannot materialize"):
        store.save(
            "aflow-manager",
            _document(),
            sha256_revision(_package_skill_bytes("aflow-manager")),
        )
    monkeypatch.undo()

    assert not (root / "aflow-manager").exists()
    assert not (root / ".metadata" / "staging").exists()
    assert not (root / ".metadata" / "aflow-manager.json").exists()


def test_concurrent_saves_serialize_with_one_winner(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    start_revision = store.read("aflow-manager").revision
    first = _document(body="# Winner A\n")
    second = _document(body="# Winner B\n")
    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker(content: str) -> None:
        barrier.wait()
        try:
            result = store.save("aflow-manager", content, start_revision)
            with lock:
                outcomes.append(result)
        except SkillRevisionConflict as conflict:
            with lock:
                outcomes.append(conflict)

    threads = [threading.Thread(target=worker, args=(content,)) for content in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, SkillRevisionConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    winner_revision = successes[0].revision
    winner = (
        first
        if winner_revision == sha256_revision(first.encode("utf-8"))
        else second
    )
    assert (root / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == winner
    assert conflicts[0].current_revision == winner_revision
    loser = second if winner == first else first
    retry = store.save("aflow-manager", loser, store.read("aflow-manager").revision)
    assert retry.changed is True


def test_concurrent_reads_only_observe_complete_documents(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    initial = _document(body="# Round 0\n")
    store.save("aflow-manager", initial, store.read("aflow-manager").revision)
    stop = threading.Event()
    problems: list[str] = []

    def reader() -> None:
        seen: set[str] = set()
        while not stop.is_set():
            try:
                document = store.read("aflow-manager")
            except Exception as exc:
                problems.append(f"read failed: {exc}")
                return
            if document.source != "saved":
                problems.append(f"unexpected source {document.source}")
            seen.add(document.content)
        for content in seen:
            try:
                parse_skill_document("aflow-manager", content)
            except Exception:
                problems.append(f"unparseable snapshot observed: {content!r}")
            headers = [
                line for line in content.splitlines() if line.startswith("# Round ")
            ]
            if len(headers) != 1:
                problems.append(f"torn document observed: {content!r}")

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    for index in range(1, 6):
        content = _document(body=f"# Round {index}\n")
        store.save("aflow-manager", content, store.read("aflow-manager").revision)
    stop.set()
    reader_thread.join()

    assert problems == []
    assert (root / "aflow-manager" / "SKILL.md").read_text(encoding="utf-8") == _document(
        body="# Round 5\n"
    )


def test_store_operations_never_mutate_package_resources(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    package_root = Path(resources.files("aflow")).joinpath("bundled_skills", "aflow-manager")
    before = _tree_digest(package_root)

    store.read("aflow-manager")
    store.save(
        "aflow-manager",
        _document(),
        sha256_revision(_package_skill_bytes("aflow-manager")),
    )

    assert _tree_digest(package_root) == before

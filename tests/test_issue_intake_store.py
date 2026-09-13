from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest

from aflow.issue_intake_store import (
    IssueIntakeStore,
    IntakeStoreError,
    ReceiptCorruptionError,
)


def _receipt(repository_id: int = 123, issue_number: int = 7) -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "claimed",
        "claim_accepted": True,
        "repository_id": repository_id,
        "repository_full_name": "owner/repo",
        "issue_number": issue_number,
        "claim_sha256": "a" * 64,
        "source_sha256": "b" * 64,
    }


def test_receipts_are_atomic_private_and_path_addressed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    store = IssueIntakeStore(state_root)

    with store.issue_lock(123, 7):
        path = store.write(123, 7, _receipt())

    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.read(123, 7) == _receipt()
    assert path == state_root / "receipts" / "123" / "7.json"
    assert store.lock_path(123, 7) == state_root / "locks" / "123" / "7.lock"


def test_ignored_receipt_can_be_replaced_by_a_later_claim(tmp_path: Path) -> None:
    store = IssueIntakeStore(tmp_path / "state")
    ignored = _receipt()
    ignored.update({"state": "ignored", "claim_accepted": False})
    store.write(123, 7, ignored)

    claimed = _receipt()
    with store.issue_lock(123, 7):
        store.write(123, 7, claimed)

    assert store.read(123, 7) == claimed


def test_receipt_rejects_symlink_and_wrong_identity(tmp_path: Path) -> None:
    store = IssueIntakeStore(tmp_path / "state")
    path = store.receipt_path(123, 7)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(IntakeStoreError):
        store.read(123, 7)

    path.unlink()
    path.symlink_to(tmp_path / "missing.json")
    with pytest.raises(IntakeStoreError):
        store.read(123, 7)

    path.unlink()
    wrong = _receipt(repository_id=123, issue_number=8)
    with pytest.raises(ReceiptCorruptionError):
        store.write(123, 7, wrong)

    with pytest.raises(ReceiptCorruptionError):
        store.write(123, 7, {**_receipt(), "repository_id": True})


def test_receipt_rejects_corrupt_json_and_non_private_mode(tmp_path: Path) -> None:
    store = IssueIntakeStore(tmp_path / "state")
    path = store.receipt_path(123, 7)
    path.write_text('{"schema_version":1,"state":"claimed",', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ReceiptCorruptionError):
        store.read(123, 7)

    path.write_text(json.dumps(_receipt()), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ReceiptCorruptionError):
        store.read(123, 7)


def test_different_issues_have_distinct_lock_paths(tmp_path: Path) -> None:
    store = IssueIntakeStore(tmp_path / "state")
    assert store.lock_path(123, 7) != store.lock_path(123, 8)
    assert store.lock_path(123, 7) != store.lock_path(456, 7)


def test_state_root_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "state"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(IntakeStoreError):
        IssueIntakeStore(link)

"""Crash-recovery tests for the configuration pair transaction owner.

The commit and recovery paths are exercised through real child processes that
crash with ``os._exit`` at each durable boundary; every assertion is then
performed by a fresh reader process so recovery is always observed through a
clean startup, including when recovery itself was interrupted.
"""

from __future__ import annotations

import errno
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from aflow import config_pair
from aflow.config_pair import (
    MAX_CONFIG_DOCUMENT_BYTES,
    MAX_TRANSACTION_RECORD_BYTES,
    PAIR_LOCK_NAME,
    TRANSACTION_RECORD_NAME,
    ConfigPairError,
    ConfigPairRevisionConflict,
    commit_configuration_pair,
    configuration_pair_lock,
    configuration_pair_lock_nonblocking,
    pair_revision,
    parse_transaction_record,
    recover_pending_transaction,
)
from aflow.live_config import LiveConfigError, load_live_config

_CRASH_EXIT = 37

OLD_AFLOW = """\
[aflow]
default_workflow = "simple"

[roles]
architect = "codex.default"

[harness.codex.profiles.default]
model = "model-a"

[prompts]
p = "Work."
"""

NEW_AFLOW = OLD_AFLOW.replace("model-a", "model-b").replace(
    'default_workflow = "simple"', 'default_workflow = "other"'
)

OLD_WORKFLOWS = """\
[workflow.simple]
[workflow.simple.steps.implement]
role = "architect"
prompts = ["p"]
go = [{ to = "END" }]
"""

NEW_WORKFLOWS = """\
[workflow.other]
[workflow.other.steps.implement]
role = "architect"
prompts = ["p"]
go = [{ to = "END" }]
"""

COMMIT_SCRIPT = """\
import os, sys
from pathlib import Path
from aflow.config_pair import commit_configuration_pair

pair_dir = Path(sys.argv[1])
new_dir = Path(sys.argv[2])
payloads = {name: (new_dir / name).read_bytes() for name in ("aflow.toml", "workflows.toml")}
crash_point = sys.argv[4]

def barrier(point):
    if point == crash_point:
        os._exit(37)

commit_configuration_pair(
    pair_dir,
    payloads=payloads,
    expected_revision=sys.argv[3],
    barrier=barrier,
)
"""

RECOVERY_SCRIPT = """\
import os, sys
from pathlib import Path
from aflow.config_pair import recover_pending_transaction

crash_point = sys.argv[2]

def barrier(point):
    if point == crash_point:
        os._exit(37)

recover_pending_transaction(Path(sys.argv[1]), barrier=barrier)
"""

# A fresh reader process: recovers (through the live reader) and reports the
# exact pair bytes plus the model the loader interpreted.
READER_SCRIPT = """\
import hashlib, json, sys
from pathlib import Path
from aflow.live_config import load_live_config

pair_dir = Path(sys.argv[1])
loaded = load_live_config(pair_dir / "aflow.toml")
digests = {}
for name in ("aflow.toml", "workflows.toml"):
    path = pair_dir / name
    digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
model = loaded.workflow_config.harnesses["codex"].profiles["default"].model
print(json.dumps({"digests": digests, "model": model}))
"""

# A fresh reader for pairs the production loader does not require to be
# complete (for example a supported missing workflows.toml state).
RECOVER_READ_SCRIPT = """\
import hashlib, json, sys
from pathlib import Path
from aflow.config_pair import recover_pending_transaction

pair_dir = Path(sys.argv[1])
recover_pending_transaction(pair_dir)
digests = {}
for name in ("aflow.toml", "workflows.toml"):
    path = pair_dir / name
    digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
print(json.dumps(digests))
"""


def _run(script: str, *args: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *[str(arg) for arg in args]],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _write_pair(directory: Path, aflow: str, workflows: str | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "aflow.toml").write_text(aflow, encoding="utf-8")
    if workflows is not None:
        (directory / "workflows.toml").write_text(workflows, encoding="utf-8")


def _digests(directory: Path) -> dict[str, str | None]:
    result = {}
    for name in ("aflow.toml", "workflows.toml"):
        path = directory / name
        result[name] = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def _setup(tmp_path: Path, *, workflows: str | None = OLD_WORKFLOWS) -> tuple[Path, Path, str]:
    """Write the old pair plus a staging directory of new documents.

    Returns ``(pair_dir, new_dir, expected_revision)``.
    """
    pair_dir = tmp_path / "pair"
    new_dir = tmp_path / "new"
    _write_pair(pair_dir, OLD_AFLOW, workflows)
    _write_pair(new_dir, NEW_AFLOW, NEW_WORKFLOWS)
    old_aflow = (pair_dir / "aflow.toml").read_bytes()
    old_workflows = (pair_dir / "workflows.toml").read_bytes() if workflows is not None else b""
    expected = pair_revision(old_aflow, old_workflows)
    return pair_dir, new_dir, expected


def _crash_commit(
    tmp_path: Path, crash_point: str, *, workflows: str | None = OLD_WORKFLOWS
) -> Path:
    pair_dir, new_dir, expected = _setup(tmp_path, workflows=workflows)
    result = _run(COMMIT_SCRIPT, pair_dir, new_dir, expected, crash_point)
    assert result.returncode == _CRASH_EXIT, result.stderr
    return pair_dir


def test_successful_commit_leaves_no_record_and_new_pair(tmp_path: Path) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    result = _run(COMMIT_SCRIPT, pair_dir, new_dir, expected, "never")
    assert result.returncode == 0, result.stderr

    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()
    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-b"
    assert data["digests"] == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }


@pytest.mark.parametrize(
    ("crash_point", "aflow_state", "workflows_state"),
    [
        ("after_record", "old", "old"),
        ("after_replacement:aflow.toml", "new", "old"),
        ("after_replacement:workflows.toml", "new", "new"),
        ("after_directory_sync", "new", "new"),
    ],
)
def test_pre_commit_crash_restores_old_pair(
    tmp_path: Path, crash_point: str, aflow_state: str, workflows_state: str
) -> None:
    pair_dir = _crash_commit(tmp_path, crash_point)

    old_aflow = sha256(OLD_AFLOW.encode()).hexdigest()
    new_aflow = sha256(NEW_AFLOW.encode()).hexdigest()
    old_workflows = sha256(OLD_WORKFLOWS.encode()).hexdigest()
    new_workflows = sha256(NEW_WORKFLOWS.encode()).hexdigest()
    observed = _digests(pair_dir)
    assert observed["aflow.toml"] == (old_aflow if aflow_state == "old" else new_aflow)
    assert observed["workflows.toml"] == (
        old_workflows if workflows_state == "old" else new_workflows
    )
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()

    # A fresh reader must restore and interpret the complete old pair.
    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-a"
    assert data["digests"] == {"aflow.toml": old_aflow, "workflows.toml": old_workflows}
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_committed_marker_crash_recovers_new_pair(tmp_path: Path) -> None:
    pair_dir = _crash_commit(tmp_path, "after_committed_marker")

    observed = _digests(pair_dir)
    assert observed["aflow.toml"] == sha256(NEW_AFLOW.encode()).hexdigest()
    assert observed["workflows.toml"] == sha256(NEW_WORKFLOWS.encode()).hexdigest()
    record = pair_dir / TRANSACTION_RECORD_NAME
    assert record.is_file()
    assert parse_transaction_record(record.read_bytes()).phase == "committed"

    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-b"
    assert data["digests"] == observed
    assert not record.exists()


def test_crash_during_recovery_completes_on_next_read(tmp_path: Path) -> None:
    # Mixed state: aflow.toml replaced, workflows.toml not yet, prepared record.
    pair_dir = _crash_commit(tmp_path, "after_replacement:workflows.toml")

    result = _run(RECOVERY_SCRIPT, pair_dir, "after_document:aflow.toml")
    assert result.returncode == _CRASH_EXIT, result.stderr
    observed = _digests(pair_dir)
    assert observed["aflow.toml"] == sha256(OLD_AFLOW.encode()).hexdigest()
    assert observed["workflows.toml"] == sha256(NEW_WORKFLOWS.encode()).hexdigest()
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()

    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-a"
    assert data["digests"]["aflow.toml"] == sha256(OLD_AFLOW.encode()).hexdigest()
    assert data["digests"]["workflows.toml"] == sha256(OLD_WORKFLOWS.encode()).hexdigest()
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_unknown_edit_during_pending_is_preserved_and_rejected(tmp_path: Path) -> None:
    pair_dir = _crash_commit(tmp_path, "after_directory_sync")
    edited = b"operator bytes that match neither generation"
    (pair_dir / "aflow.toml").write_bytes(edited)

    result = _run(RECOVER_READ_SCRIPT, pair_dir)
    assert result.returncode != 0
    assert "changed while a configuration transaction was pending" in result.stderr
    assert (pair_dir / "aflow.toml").read_bytes() == edited
    assert (pair_dir / "workflows.toml").read_bytes() == NEW_WORKFLOWS.encode()
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()


def test_missing_document_during_pending_is_rejected(tmp_path: Path) -> None:
    pair_dir = _crash_commit(tmp_path, "after_directory_sync")
    (pair_dir / "workflows.toml").unlink()

    result = _run(RECOVER_READ_SCRIPT, pair_dir)
    assert result.returncode != 0
    assert "changed while a configuration transaction was pending" in result.stderr
    assert (pair_dir / "workflows.toml").exists() is False
    assert (pair_dir / "aflow.toml").read_bytes() == NEW_AFLOW.encode()
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()


def _tamper(record_bytes: bytes, mutate) -> bytes:
    data = json.loads(record_bytes)
    mutate(data)
    return json.dumps(data, sort_keys=True).encode("utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(schema_version=2),
        lambda data: data.update(extra_field="x"),
        lambda data: data["documents"].update({"other.toml": {}}),
        lambda data: data["documents"]["aflow.toml"]["new"].update(sha256="0" * 64),
        lambda data: data["documents"]["aflow.toml"].update(old="not-an-object"),
    ],
    ids=["schema-version", "unknown-field", "unknown-document", "bad-digest", "bad-shape"],
)
def test_invalid_record_is_rejected_and_preserved(
    tmp_path: Path, mutate: object
) -> None:
    pair_dir, new_dir, _ = _setup(tmp_path)
    old = {
        "aflow.toml": OLD_AFLOW.encode(),
        "workflows.toml": OLD_WORKFLOWS.encode(),
    }
    new = {
        "aflow.toml": NEW_AFLOW.encode(),
        "workflows.toml": NEW_WORKFLOWS.encode(),
    }
    from aflow.config_pair import _record_bytes

    record = _tamper(_record_bytes("prepared", old, new), mutate)  # type: ignore[operator]
    (pair_dir / TRANSACTION_RECORD_NAME).write_bytes(record)

    result = _run(RECOVER_READ_SCRIPT, pair_dir)
    assert result.returncode != 0
    assert "transaction record" in result.stderr
    assert (pair_dir / TRANSACTION_RECORD_NAME).read_bytes() == record
    assert (pair_dir / "aflow.toml").read_bytes() == OLD_AFLOW.encode()


def test_readonly_with_pending_record_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_dir = _crash_commit(tmp_path, "after_directory_sync")
    record_before = (pair_dir / TRANSACTION_RECORD_NAME).read_bytes()

    def deny_readonly(path: Path, payload: bytes, mode: int) -> None:
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(config_pair, "_atomic_write_file", deny_readonly)
    with pytest.raises(LiveConfigError):
        load_live_config(pair_dir / "aflow.toml")

    # The reader failed closed: no mixed pair was returned, and the record
    # and file bytes are preserved for a later recoverable read.
    assert (pair_dir / TRANSACTION_RECORD_NAME).read_bytes() == record_before
    assert (pair_dir / "aflow.toml").read_bytes() == NEW_AFLOW.encode()
    assert (pair_dir / "workflows.toml").read_bytes() == NEW_WORKFLOWS.encode()


def test_stale_revision_creates_no_transaction(tmp_path: Path) -> None:
    pair_dir, new_dir, _ = _setup(tmp_path)
    before = _digests(pair_dir)
    payloads = {
        "aflow.toml": NEW_AFLOW.encode(),
        "workflows.toml": NEW_WORKFLOWS.encode(),
    }

    with pytest.raises(ConfigPairRevisionConflict):
        commit_configuration_pair(
            pair_dir, payloads=payloads, expected_revision="0" * 64
        )

    assert _digests(pair_dir) == before
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_missing_workflows_is_supported_pre_save_state(tmp_path: Path) -> None:
    pair_dir = _crash_commit(tmp_path, "after_replacement:workflows.toml", workflows=None)

    # The formerly missing workflows.toml was created by the interrupted
    # commit; recovery must remove it and restore the old aflow.toml.
    assert (pair_dir / "workflows.toml").read_bytes() == NEW_WORKFLOWS.encode()
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()

    result = _run(RECOVER_READ_SCRIPT, pair_dir)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "aflow.toml": sha256(OLD_AFLOW.encode()).hexdigest(),
        "workflows.toml": None,
    }
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    pair_dir = _crash_commit(tmp_path, "after_committed_marker")

    recover_pending_transaction(pair_dir)
    recover_pending_transaction(pair_dir)

    assert _digests(pair_dir) == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_record_is_bounded_mode_0600_and_path_free(tmp_path: Path) -> None:
    pair_dir = _crash_commit(tmp_path, "after_record")
    record = pair_dir / TRANSACTION_RECORD_NAME
    assert record.stat().st_mode & 0o777 == 0o600
    raw = record.read_bytes()
    assert len(raw) <= MAX_TRANSACTION_RECORD_BYTES
    parsed = parse_transaction_record(raw)
    assert parsed.phase == "prepared"
    assert set(parsed.documents) == {"aflow.toml", "workflows.toml"}
    for name, entry in parsed.documents.items():
        assert entry.old_bytes is not None
        assert sha256(entry.new_bytes).hexdigest()
    assert str(tmp_path) not in raw.decode("utf-8")


def test_oversized_payload_is_rejected_before_any_write(tmp_path: Path) -> None:
    pair_dir, _, expected = _setup(tmp_path)
    before = _digests(pair_dir)
    payloads = {
        "aflow.toml": b"x" * (MAX_CONFIG_DOCUMENT_BYTES + 1),
        "workflows.toml": NEW_WORKFLOWS.encode(),
    }
    with pytest.raises(ConfigPairError):
        commit_configuration_pair(pair_dir, payloads=payloads, expected_revision=expected)
    assert _digests(pair_dir) == before
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_cleanup_unlink_failure_retains_committed_record_and_reader_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)

    def fail_unlink(record_path: Path, directory: Path) -> None:
        raise OSError(errno.EIO, "injected cleanup unlink failure")

    monkeypatch.setattr(config_pair, "_complete_cleanup", fail_unlink)
    # The acknowledged save outcome is returned, not an error.
    with caplog.at_level(logging.WARNING, logger="aflow.config_pair"):
        commit_configuration_pair(
            pair_dir,
            payloads={"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()},
            expected_revision=expected,
        )
    warnings = [r.getMessage() for r in caplog.records]
    # The diagnostic describes the retained journal evidence honestly.
    assert any(
        "cleanup is incomplete" in message and "a later read will complete cleanup" in message
        for message in warnings
    )

    record = pair_dir / TRANSACTION_RECORD_NAME
    assert record.is_file()
    assert parse_transaction_record(record.read_bytes()).phase == "committed"
    assert _digests(pair_dir) == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }

    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-b"
    assert data["digests"] == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }
    assert not record.exists()


def test_cleanup_dir_sync_failure_restores_committed_record_and_reader_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)

    def fail_dir_sync(record_path: Path, directory: Path) -> None:
        record_path.unlink()
        raise OSError(errno.EIO, "injected cleanup directory sync failure")

    monkeypatch.setattr(config_pair, "_complete_cleanup", fail_dir_sync)
    with caplog.at_level(logging.WARNING, logger="aflow.config_pair"):
        commit_configuration_pair(
            pair_dir,
            payloads={"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()},
            expected_revision=expected,
        )
    warnings = [r.getMessage() for r in caplog.records]
    # The journal was re-established durably, so the diagnostic may promise
    # that a later reader completes cleanup.
    assert any(
        "cleanup is incomplete" in message and "a later read will complete cleanup" in message
        for message in warnings
    )

    # The new generation stays and the committed journal is re-established
    # so a later reader can complete cleanup.
    record = pair_dir / TRANSACTION_RECORD_NAME
    assert record.is_file()
    assert parse_transaction_record(record.read_bytes()).phase == "committed"
    assert _digests(pair_dir) == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }

    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-b"
    assert data["digests"]["aflow.toml"] == sha256(NEW_AFLOW.encode()).hexdigest()
    assert not record.exists()


def test_cleanup_crash_leaves_complete_new_pair(tmp_path: Path) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    result = _run(COMMIT_SCRIPT, pair_dir, new_dir, expected, "after_cleanup")
    assert result.returncode == _CRASH_EXIT, result.stderr

    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()
    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-b"
    assert data["digests"] == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }


def test_committed_recovery_interrupted_at_document_boundary_completes(
    tmp_path: Path,
) -> None:
    pair_dir = _crash_commit(tmp_path, "after_committed_marker")

    result = _run(RECOVERY_SCRIPT, pair_dir, "after_document:aflow.toml")
    assert result.returncode == _CRASH_EXIT, result.stderr
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()

    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-b"
    assert data["digests"] == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def test_prepared_recovery_cleanup_interruption_completes(tmp_path: Path) -> None:
    pair_dir = _crash_commit(tmp_path, "after_directory_sync")

    # Crashing exactly at the cleanup completion boundary (after the record
    # unlink and its directory sync) leaves a complete old pair; the fresh
    # reader must finish with nothing pending.
    result = _run(RECOVERY_SCRIPT, pair_dir, "after_cleanup")
    assert result.returncode == _CRASH_EXIT, result.stderr
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()

    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-a"
    assert data["digests"] == {
        "aflow.toml": sha256(OLD_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(OLD_WORKFLOWS.encode()).hexdigest(),
    }
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def _leftover_temps(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_marker_publication_failure_restores_exact_old_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    before = _digests(pair_dir)
    payloads = {
        "aflow.toml": NEW_AFLOW.encode(),
        "workflows.toml": NEW_WORKFLOWS.encode(),
    }
    real = config_pair._write_record
    calls = {"n": 0}

    def fail_committed_marker(record_path: Path, phase: str, old: object, new: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            assert phase == "committed"
            raise OSError(errno.EIO, "injected marker failure")
        real(record_path, phase, old, new)

    monkeypatch.setattr(config_pair, "_write_record", fail_committed_marker)
    with pytest.raises(ConfigPairError) as excinfo:
        commit_configuration_pair(
            pair_dir, payloads=payloads, expected_revision=expected
        )

    # Bounded core error, honest restoration claim, exact old pair back in
    # place, journal removed, and no staging leftovers.
    assert "restored" in str(excinfo.value)
    assert _digests(pair_dir) == before
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()
    assert _leftover_temps(pair_dir) == []

    # A fresh, healthy reader completes with the restored old generation.
    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-a"
    assert data["digests"] == before
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


@pytest.mark.parametrize("fault", ["journal-read", "prepared-reset-write"])
def test_rollback_preparation_io_failure_is_bounded_and_evidence_preserved(
    tmp_path: Path, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker sync failure plus a rollback-preparation I/O fault.

    The commit must surface a bounded pending-recovery core error and
    preserve the state at the failure: new bytes in place, the committed
    journal visible, no staging leftovers, and no restoration claim.
    """
    pair_dir, new_dir, expected = _setup(tmp_path)
    payloads = {
        "aflow.toml": NEW_AFLOW.encode(),
        "workflows.toml": NEW_WORKFLOWS.encode(),
    }
    real_write = config_pair._write_record
    calls = {"n": 0}

    def fail_after_committed_marker(path: Path, phase: str, old: object, new: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            assert phase == "committed"
            real_write(path, phase, old, new)
            raise OSError(errno.EIO, "injected committed marker directory sync")
        if calls["n"] == 3 and fault == "prepared-reset-write":
            assert phase == "prepared"
            raise OSError(errno.EIO, "injected rollback prepared-record write")
        real_write(path, phase, old, new)

    monkeypatch.setattr(config_pair, "_write_record", fail_after_committed_marker)
    if fault != "prepared-reset-write":

        def fail_journal_read(raw: bytes) -> object:
            raise OSError(errno.EIO, "injected rollback journal read")

        monkeypatch.setattr(config_pair, "parse_transaction_record", fail_journal_read)

    with pytest.raises(ConfigPairError) as excinfo:
        commit_configuration_pair(
            pair_dir, payloads=payloads, expected_revision=expected
        )

    message = str(excinfo.value)
    assert "recovery is pending" in message
    assert "restored" not in message
    # The state at the failure is preserved: acknowledged new bytes in
    # place, committed journal visible, no staging leftovers.
    assert _digests(pair_dir) == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }
    record = pair_dir / TRANSACTION_RECORD_NAME
    assert record.is_file()
    assert parse_transaction_record(record.read_bytes()).phase == "committed"
    assert _leftover_temps(pair_dir) == []


def test_cleanup_retention_failure_reports_unconfirmed_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    new_digests = {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }

    def fail_dir_sync(record_path: Path, directory: Path) -> None:
        record_path.unlink()
        raise OSError(errno.EIO, "injected cleanup directory sync failure")

    real_write = config_pair._write_record
    calls = {"n": 0}

    def fail_retention(path: Path, phase: str, old: object, new: object) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(errno.ENOSPC, "injected journal retention write failure")
        real_write(path, phase, old, new)

    monkeypatch.setattr(config_pair, "_complete_cleanup", fail_dir_sync)
    monkeypatch.setattr(config_pair, "_write_record", fail_retention)
    with caplog.at_level(logging.WARNING, logger="aflow.config_pair"):
        # The acknowledged save outcome is returned, not an error.
        commit_configuration_pair(
            pair_dir,
            payloads={"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()},
            expected_revision=expected,
        )

    # Exact new bytes in place; no journal exists because retention failed.
    assert _digests(pair_dir) == new_digests
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()

    messages = [r.getMessage() for r in caplog.records]
    assert any("retention could not be confirmed" in m for m in messages)
    joined = "\n".join(messages)
    # Truthful and payload-free: no promise of future journal cleanup and
    # no document content in the diagnostic.
    assert "a later read will complete cleanup" not in joined
    assert "model-b" not in joined
    assert "default_workflow" not in joined

    # A fresh, healthy reader retains the new generation with nothing pending.
    reader = _run(READER_SCRIPT, pair_dir)
    assert reader.returncode == 0, reader.stderr
    data = json.loads(reader.stdout)
    assert data["model"] == "model-b"
    assert data["digests"] == new_digests


def test_stage_failure_restores_old_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    before = _digests(pair_dir)

    def fail_stage(*args: object, **kwargs: object) -> Path:
        raise OSError(errno.EIO, "injected stage failure")

    monkeypatch.setattr(config_pair, "_stage_document", fail_stage)
    with pytest.raises(ConfigPairError):
        commit_configuration_pair(
            pair_dir,
            payloads={"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()},
            expected_revision=expected,
        )
    assert _digests(pair_dir) == before
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()
    assert _leftover_temps(pair_dir) == []


def test_replacement_failure_restores_old_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    before = _digests(pair_dir)
    real_replace = os.replace
    calls = {"n": 0}

    def fail_workflows_replace(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(errno.EIO, "injected replacement failure")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", fail_workflows_replace)
    with pytest.raises(ConfigPairError):
        commit_configuration_pair(
            pair_dir,
            payloads={"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()},
            expected_revision=expected,
        )
    # The partial replacement (new aflow.toml) is rolled back to the exact
    # old pair under the lock; the journal is consumed by the rollback.
    assert _digests(pair_dir) == before
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()
    assert _leftover_temps(pair_dir) == []


def test_directory_sync_failure_restores_old_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    before = _digests(pair_dir)
    real = config_pair._fsync_directory
    calls = {"n": 0}

    def fail_document_sync(path: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EIO, "injected directory sync failure")
        real(path)

    monkeypatch.setattr(config_pair, "_fsync_directory", fail_document_sync)
    with pytest.raises(ConfigPairError):
        commit_configuration_pair(
            pair_dir,
            payloads={"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()},
            expected_revision=expected,
        )
    assert _digests(pair_dir) == before
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()
    assert _leftover_temps(pair_dir) == []


def test_prepared_record_failure_permits_same_process_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    before = _digests(pair_dir)
    payloads = {
        "aflow.toml": NEW_AFLOW.encode(),
        "workflows.toml": NEW_WORKFLOWS.encode(),
    }
    real = config_pair._write_record
    calls = {"n": 0}

    def fail_prepared_record(record_path: Path, phase: str, old: object, new: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            assert phase == "prepared"
            raise OSError(errno.EIO, "injected record fsync failure")
        real(record_path, phase, old, new)

    monkeypatch.setattr(config_pair, "_write_record", fail_prepared_record)
    with pytest.raises(ConfigPairError):
        commit_configuration_pair(
            pair_dir, payloads=payloads, expected_revision=expected
        )
    assert _digests(pair_dir) == before
    assert _leftover_temps(pair_dir) == []

    # The one-shot failure left no temporary that collides with the retry.
    commit_configuration_pair(
        pair_dir, payloads=payloads, expected_revision=expected
    )
    assert _digests(pair_dir) == {
        "aflow.toml": sha256(NEW_AFLOW.encode()).hexdigest(),
        "workflows.toml": sha256(NEW_WORKFLOWS.encode()).hexdigest(),
    }
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()
    assert _leftover_temps(pair_dir) == []


def test_atomic_write_failure_removes_only_own_temp_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "aflow.toml"
    target.write_bytes(b"old")

    def failing_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "injected")

    with monkeypatch.context() as m:
        m.setattr(os, "fsync", failing_fsync)
        with pytest.raises(OSError):
            config_pair._atomic_write_file(target, b"new", 0o644)
    assert target.read_bytes() == b"old"
    assert _leftover_temps(tmp_path) == []

    # A healthy retry in the same process succeeds.
    config_pair._atomic_write_file(target, b"new", 0o644)
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "edited",
    ["aflow.toml", "workflows.toml"],
    ids=["aflow-edit", "workflows-edit"],
)
def test_operator_edit_blocks_rollback_without_partial_restoration(
    tmp_path: Path, edited: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_dir, new_dir, expected = _setup(tmp_path)
    operator = b"operator edit that matches neither generation"
    real_replace = os.replace
    calls = {"n": 0}

    def fail_workflows_replace(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(errno.EIO, "injected replacement failure")
        real_replace(src, dst)  # type: ignore[arg-type]

    def barrier(point: str) -> None:
        if point == "after_replacement:aflow.toml":
            (pair_dir / edited).write_bytes(operator)

    monkeypatch.setattr(os, "replace", fail_workflows_replace)
    with pytest.raises(ConfigPairError) as excinfo:
        commit_configuration_pair(
            pair_dir,
            payloads={"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()},
            expected_revision=expected,
            barrier=barrier,
        )

    # The rollback must not claim a restoration it could not verify, must
    # preserve the operator edit byte-for-byte, and must not partially
    # restore: the untouched document stays exactly as it was.
    assert "recovery is pending" in str(excinfo.value)
    assert "restored" not in str(excinfo.value)
    if edited == "aflow.toml":
        assert (pair_dir / "aflow.toml").read_bytes() == operator
        assert (pair_dir / "workflows.toml").read_bytes() == OLD_WORKFLOWS.encode()
    else:
        assert (pair_dir / "workflows.toml").read_bytes() == operator
        assert (pair_dir / "aflow.toml").read_bytes() == NEW_AFLOW.encode()
    record = pair_dir / TRANSACTION_RECORD_NAME
    assert record.is_file()
    assert parse_transaction_record(record.read_bytes()).phase == "prepared"
    assert _leftover_temps(pair_dir) == []


def test_non_string_digest_types_are_bounded_core_errors(
    tmp_path: Path,
) -> None:
    old = {"aflow.toml": OLD_AFLOW.encode(), "workflows.toml": OLD_WORKFLOWS.encode()}
    new = {"aflow.toml": NEW_AFLOW.encode(), "workflows.toml": NEW_WORKFLOWS.encode()}
    from aflow.config_pair import _record_bytes

    for digest in (None, 1, [], {}):
        record = _tamper(
            _record_bytes("prepared", old, new),
            lambda data, d=digest: data["documents"]["aflow.toml"]["new"].update(
                sha256=d
            ),
        )
        with pytest.raises(ConfigPairError, match="malformed"):
            parse_transaction_record(record)


def test_nested_locks_do_not_leak_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Instrument the standard-library open/close calls (no procfs) so the
    # test proves balanced lock-descriptor lifetimes on every platform.
    open_events: list[int] = []
    close_events: list[int] = []
    real_open = os.open
    real_close = os.close

    def tracked_open(path: object, *args: object, **kwargs: object) -> int:
        fd = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if os.path.basename(str(path)) == PAIR_LOCK_NAME:
            open_events.append(fd)
        return fd

    def tracked_close(fd: int) -> None:
        close_events.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)

    for _ in range(64):
        open_events.clear()
        close_events.clear()
        with configuration_pair_lock(tmp_path) as outer:
            assert outer is not None
            # The outer acquisition owns exactly one effective descriptor.
            assert len(open_events) == 1
            outer_fd = open_events[0]
            with configuration_pair_lock(tmp_path) as inner:
                assert inner == outer
                # A re-entrant acquisition opens no descriptor of its own.
                assert len(open_events) == 1
            assert close_events == []
        # The owned descriptor is closed exactly once, after the operation.
        assert close_events == [outer_fd]

    for _ in range(64):
        open_events.clear()
        close_events.clear()
        with configuration_pair_lock_nonblocking(tmp_path) as outer:
            assert outer is not None
            assert len(open_events) == 1
            outer_fd = open_events[0]
            with configuration_pair_lock_nonblocking(tmp_path) as inner:
                assert inner == outer
                assert len(open_events) == 1
            assert close_events == []
        assert close_events == [outer_fd]


def test_lock_reentry_does_not_deadlock_and_still_excludes_others(
    tmp_path: Path,
) -> None:
    with configuration_pair_lock(tmp_path) as outer:
        assert outer is not None
        with configuration_pair_lock(tmp_path) as inner:
            assert inner == outer

        observed: list[bool] = []

        def other_thread() -> None:
            with configuration_pair_lock_nonblocking(tmp_path) as lock_path:
                observed.append(lock_path is None)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert observed == [True]

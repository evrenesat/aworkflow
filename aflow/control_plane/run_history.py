"""Atomic presentation metadata; workflow evidence is never removed."""

import hashlib
import json
from dataclasses import replace

from .models import utc_now
from .persistence import ControlConflictError, PersistenceError, _contained_directory, _locked_file, _write_atomic_bytes


class DeletedRunError(PersistenceError):
    """The external history record has been permanently deleted."""


class RunHistory:
    def __init__(self, repository):
        self.repository = repository

    def _path(self, run_id):
        valid, _ = self.repository._readable_run_id(run_id)
        key = hashlib.sha256(valid.encode()).hexdigest()
        path = self.repository.repo_root
        for part in (".aflow", "run-history", key + ".json"):
            path = path / part
            if path.is_symlink():
                raise PersistenceError("run history may not use symlinks")
        return path

    def read(self, run_id):
        path = self._path(run_id)
        if not path.exists():
            return {"schema_version": 1, "run_id": run_id, "state": "visible", "revision": 0, "changed_at": None, "requests": {}}
        if path.is_symlink() or path.stat().st_size > 1_048_576:
            raise PersistenceError("unsafe run history artifact")
        try:
            value = json.loads(path.read_text())
            if (
                value["schema_version"] != 1 or value["run_id"] != run_id
                or value["state"] not in {"visible", "archived", "deleted"}
                or type(value["revision"]) is not int or value["revision"] < 0
                or not isinstance(value["requests"], dict) or len(value["requests"]) > 1024
                or not isinstance(value["changed_at"], str) or len(value["changed_at"]) > 64
            ):
                raise ValueError("unsupported history payload")
        except (ValueError, KeyError, TypeError) as exc:
            raise PersistenceError("invalid run history artifact") from exc
        return value

    def project(self, run, *, external=False):
        value = self.read(run.run_id)
        if external and value["state"] == "deleted":
            raise DeletedRunError("Deleted record")
        return replace(run, history_state=value["state"], history_revision=value["revision"])

    def mutate(self, run_id, *, state, expected_revision, idempotency_key, active=False, acknowledge_active=False):
        self.repository.get_run_status(run_id)
        if state not in {"visible", "archived", "deleted"} or type(expected_revision) is not int or expected_revision < 0:
            raise PersistenceError("invalid history mutation")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise PersistenceError("history mutation requires an idempotency key")
        path = self._path(run_id)
        _contained_directory(self.repository.repo_root, ".aflow", "run-history")
        key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        intent = {"state": state, "expected_revision": expected_revision}
        lock = path.with_suffix(".lock")
        if lock.is_symlink():
            raise PersistenceError("run history lock may not use symlinks")
        with _locked_file(lock):
            value = self.read(run_id)
            previous = value["requests"].get(key)
            if value["state"] == "deleted" and not (previous and previous["intent"] == intent and state == "deleted"):
                raise DeletedRunError("Deleted record")
            if previous is not None:
                if previous["intent"] != intent:
                    raise ControlConflictError(value["revision"])
                return previous["result"]
            if value["state"] == "deleted":
                raise DeletedRunError("Deleted record")
            if value["revision"] != expected_revision:
                raise ControlConflictError(value["revision"])
            if state != "visible" and active and not acknowledge_active:
                raise PersistenceError("Acknowledge that hiding this record will not stop the workflow")
            if len(value["requests"]) >= 1024:
                raise PersistenceError("history request capacity reached")
            value.update(state=state, revision=value["revision"] + 1, changed_at=utc_now())
            result = {k: v for k, v in value.items() if k != "requests"}
            value["requests"][key] = {"intent": intent, "result": result}
            _write_atomic_bytes(path, json.dumps(value).encode())
            return result

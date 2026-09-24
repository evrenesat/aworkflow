"""Durable filename-series membership and receipt-backed predecessor checks.

Call ``require_ready`` only while holding the project's admission lock. The
inventory is additive: removing or renaming a file cannot remove a known
predecessor from the series.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from aflow.plan_backups import (
    BackupProvenanceError,
    _plan_identities,
    ensure_plan_identity,
)
from aflow.publication import _valid_published_receipt
from aflow.project_settings import resolve_project_identity


_SEQUENCE = re.compile(r"^(?P<series>.+)_P(?P<position>[0-9]{2,})_(?P<title>.+)\.md$")
_DIRECTORIES = ("todo", "in-progress", "done", "failed", "needs-plan-change")
_MAX_STATE = 4 * 1024 * 1024
_MAX_RECEIPT = 4 * 1024 * 1024


class PlanDependencyError(RuntimeError):
    """Dependency evidence is invalid or the series is not ready."""


class PlanDependencyBlocked(PlanDependencyError):
    """A known predecessor or duplicate position holds this series."""


def parse_sequence_name(name: str) -> tuple[str, int] | None:
    """Return a numbered member, rejecting names that resemble one badly."""
    if not isinstance(name, str) or not name.endswith(".md"):
        raise PlanDependencyError("plan filename is invalid")
    markers = name.count("_P")
    if markers == 0:
        return None
    match = _SEQUENCE.fullmatch(name)
    if markers != 1 or match is None or not int(match["position"]):
        raise PlanDependencyError("plan sequence filename needs correction")
    return match["series"], int(match["position"])


def _origin(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def _member(name: str, identity: str, origin: str) -> dict[str, object] | None:
    try:
        parsed = parse_sequence_name(name)
    except PlanDependencyError:
        if "_P" not in name:
            raise
        series = name.split("_P", 1)[0]
        return {"identity": identity, "origin": origin, "name": name, "series": series,
                "position": None, "delivered": False}
    if parsed is None:
        return None
    return {"identity": identity, "origin": origin, "name": name, "series": parsed[0],
            "position": parsed[1], "delivered": False}


def _regular_json(path: Path, limit: int) -> object | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanDependencyError("dependency evidence is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise PlanDependencyError("dependency evidence is unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise PlanDependencyError("dependency evidence is too large")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PlanDependencyError("dependency evidence is malformed") from exc


class PlanDependencies:
    """Inventory known members and check one requested plan under admission."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve(strict=True)
        self.path = self.root / ".aflow" / "plan-dependencies.json"

    def _load(self) -> dict[str, dict[str, object]]:
        payload = _regular_json(self.path, _MAX_STATE)
        if payload is None:
            return {}
        if not isinstance(payload, dict) or payload.get("schema") != 1 or not isinstance(payload.get("members"), list):
            raise PlanDependencyError("dependency inventory is malformed")
        members: dict[str, dict[str, object]] = {}
        for item in payload["members"]:
            if not isinstance(item, dict) or set(item) not in (
                {"identity", "name", "series", "position", "delivered"},
                {"identity", "origin", "name", "series", "position", "delivered"},
            ):
                raise PlanDependencyError("dependency inventory is malformed")
            identity, name = item["identity"], item["name"]
            origin = item.get("origin", _origin(self.root))
            if (
                not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None
                or not isinstance(origin, str) or re.fullmatch(r"[0-9a-f]{64}", origin) is None
                or not isinstance(name, str) or Path(name).name != name or len(name) > 203
            ):
                raise PlanDependencyError("dependency inventory is malformed")
            expected = _member(name, identity, origin)
            if expected is None or (item["series"], item["position"]) != (expected["series"], expected["position"]):
                raise PlanDependencyError("dependency inventory is malformed")
            if not isinstance(item["delivered"], bool):
                raise PlanDependencyError("dependency inventory is malformed")
            key = f"{identity}:{name}"
            if key in members:
                raise PlanDependencyError("dependency inventory has duplicate members")
            members[key] = {**item, "origin": origin}
        return members

    def _save(self, members: dict[str, dict[str, object]]) -> None:
        directory = self.path.parent
        if directory.is_symlink() or not directory.is_dir():
            raise PlanDependencyError("dependency directory is unsafe")
        payload = (json.dumps({"schema": 1, "members": sorted(
            members.values(), key=lambda item: (str(item["series"]), str(item["name"]), str(item["identity"])),
        )}, sort_keys=True) + "\n").encode("utf-8")
        if len(payload) > _MAX_STATE:
            raise PlanDependencyError("dependency inventory is too large")
        descriptor, temporary = tempfile.mkstemp(prefix=".plan-dependencies-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            dir_descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_descriptor)
            finally:
                os.close(dir_descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _add_member(
        self, members: dict[str, dict[str, object]], *,
        name: str, identity: str, origin: str,
    ) -> bool:
        key = f"{identity}:{name}"
        if key in members:
            return False
        # Git worktrees often contain the same logical plan with different
        # checkout-local identity sidecars. Coalesce only equal filenames
        # observed in different checkouts; distinct names or identities in
        # one checkout remain separate members and still detect duplicates.
        if any(item["name"] == name and item["origin"] != origin for item in members.values()):
            return False
        item = _member(name, identity, origin)
        assert item is not None
        members[key] = item
        return True

    def _discover(self, members: dict[str, dict[str, object]], roots: tuple[Path, ...]) -> bool:
        changed = False
        for root in roots:
            origin = _origin(root)
            plans = root / "plans"
            if plans.is_symlink():
                raise PlanDependencyError("plan directory is unsafe")
            for location in _DIRECTORIES:
                directory = plans / location
                if directory.is_symlink():
                    raise PlanDependencyError("plan directory is unsafe")
                if not directory.exists():
                    continue
                if not directory.is_dir():
                    raise PlanDependencyError("plan directory is invalid")
                for path in directory.iterdir():
                    if path.suffix != ".md":
                        continue
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise PlanDependencyError("plan entry is unsafe")
                    if "_P" not in path.name:
                        continue
                    try:
                        identity = ensure_plan_identity(root, path)
                    except BackupProvenanceError as exc:
                        raise PlanDependencyError("plan identity is unavailable") from exc
                    changed |= self._add_member(
                        members, name=path.name, identity=identity, origin=origin,
                    )
            try:
                identities = _plan_identities(root, strict=True)
            except BackupProvenanceError as exc:
                raise PlanDependencyError("plan identity history is unavailable") from exc
            for record in identities:
                for owned_path in record["owned_paths"]:
                    try:
                        path = Path(owned_path).relative_to(root)
                    except ValueError:
                        continue
                    if len(path.parts) != 3 or path.parts[:2] not in {
                        ("plans", location) for location in _DIRECTORIES
                    } or "_P" not in path.name:
                        continue
                    changed |= self._add_member(
                        members, name=path.name,
                        identity=str(record["plan_identity_id"]), origin=origin,
                    )
        return changed

    def _identity_has_done_owner(
        self, identity: str, name: str, origin: str, roots: tuple[Path, ...],
    ) -> bool:
        for root in roots:
            try:
                records = _plan_identities(root, strict=True)
            except BackupProvenanceError as exc:
                raise PlanDependencyError("plan identity history is unavailable") from exc
            source_path = str(root / "plans" / "in-progress" / name)
            done_path = str(root / "plans" / "done" / name)
            if any(
                done_path in record["owned_paths"]
                and record["current_path"] in {None, done_path}
                and (
                    record["plan_identity_id"] == identity
                    if _origin(root) == origin
                    else source_path in record["owned_paths"]
                )
                for record in records
            ):
                return True
        return False

    def _delivered(self, name: str, root: Path) -> bool:
        runs = root / ".aflow" / "runs"
        if runs.is_symlink():
            raise PlanDependencyError("run history is unsafe")
        if not runs.exists():
            return False
        for run in runs.iterdir():
            if run.is_symlink():
                raise PlanDependencyError("run history is unsafe")
            if not run.is_dir():
                continue
            receipt = _regular_json(run / "publication.json", _MAX_RECEIPT)
            if not isinstance(receipt, dict) or not _valid_published_receipt(receipt):
                continue
            lifecycle = receipt.get("plan_lifecycle")
            if (
                isinstance(lifecycle, dict)
                and lifecycle.get("phase") == "committed"
                and lifecycle.get("complete") is True
                and lifecycle.get("source") == f"plans/in-progress/{name}"
                and lifecycle.get("destination") == f"plans/done/{name}"
            ):
                return True
        return False

    def require_ready(
        self, plan_key: str, *, checkout_root: Path | None = None,
        evidence_roots: tuple[Path, ...] | None = None,
    ) -> None:
        path = Path(plan_key)
        if len(path.parts) != 3 or path.parts[:2] not in {
            ("plans", "todo"), ("plans", "in-progress"),
        }:
            return
        try:
            candidate = parse_sequence_name(path.name)
        except PlanDependencyError as exc:
            raise PlanDependencyBlocked(str(exc)) from exc
        if candidate is None:
            return
        discovery_roots = (self.root,)
        if checkout_root is not None:
            try:
                checkout = Path(checkout_root)
                project = resolve_project_identity(checkout)
            except (OSError, RuntimeError, ValueError) as exc:
                raise PlanDependencyError("dependency checkout is not verified") from exc
            if (
                not checkout.is_absolute()
                or project.primary_root != self.root
                or project.checkout_root != checkout
            ):
                raise PlanDependencyError("dependency checkout belongs to another project")
            if checkout != self.root:
                discovery_roots = (self.root, checkout)
        verified_roots: list[Path] = []
        for root in evidence_roots or discovery_roots:
            evidence_root = Path(root)
            try:
                project = resolve_project_identity(evidence_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise PlanDependencyError("dependency evidence root is not verified") from exc
            if (
                not evidence_root.is_absolute()
                or project.primary_root != self.root
                or project.checkout_root != evidence_root
            ):
                raise PlanDependencyError("dependency evidence belongs to another project")
            if evidence_root not in verified_roots:
                verified_roots.append(evidence_root)
        if any(root not in verified_roots for root in discovery_roots):
            raise PlanDependencyError("dependency checkout is absent from verified roots")
        members = self._load()
        changed = self._discover(members, discovery_roots)
        series, position = candidate
        for item in members.values():
            if item["series"] != series:
                continue
            name = str(item["name"])
            verified = (
                self._identity_has_done_owner(
                    str(item["identity"]), name, str(item["origin"]), tuple(verified_roots),
                )
                and any(self._delivered(name, root) for root in verified_roots)
            )
            if item["delivered"] != verified:
                item["delivered"] = verified
                changed = True
        if changed:
            self._save(members)
        peers = [item for item in members.values() if item["series"] == series]
        positions = [item["position"] for item in peers if item["position"] is not None]
        if any(item["position"] is None for item in peers) or len(positions) != len(set(positions)):
            raise PlanDependencyBlocked("plan sequence needs correction")
        if any(isinstance(item["position"], int) and item["position"] < position
               and not item["delivered"] for item in peers):
            raise PlanDependencyBlocked("a known predecessor has not been delivered")

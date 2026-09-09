"""Account-local bundled-skill API service for the remote app server.

This service is a thin read/edit/install facade over the two shared
core services owned by checkpoint 1–3: ``aflow.skill_store`` (the
revisioned canonical store at ``Path.home() / '.config' / 'aflow' /
'skills'``) and ``aflow.skill_installer`` (the one symlink installer
backing ``aflow install-skills``). It introduces no second persistence
or installer implementation: reads and saves delegate to ``SkillStore``,
and installation delegates to ``install_skills`` with the same default
auto arguments as ``install-skills --yes``.

Reads are pure. Package refresh happens only inside the explicit
install entry point, never on GET/save/validate. Saving updates the
canonical ``SKILL.md`` only; it never installs or broadens targets.

Link status is derived by inspecting the known mapped destinations on
the filesystem, even when the harness executable that owns a
destination has since disappeared from PATH. Harness detection itself
uses the installer's executable lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Callable, Iterable

from aflow.skill_catalog import (
    BUNDLED_SKILL_METADATA,
    BUNDLED_SKILL_NAMES,
    SkillCatalogError,
    is_bundled_skill_name,
)
from aflow.skill_installer import (
    SUPPORTED_HARNESS_INSTALL_SPECS,
    InstallResult,
    InstallerError,
    install_skills,
)
from aflow.skill_store import (
    SkillStore,
    SkillStoreError,
    default_store_root,
)


class SkillNotFound(SkillStoreError):
    """An unknown or non-bundled skill name was requested (HTTP 404)."""


class SkillInstallError(SkillStoreError):
    """An explicit install request was rejected before linking (HTTP 422)."""

    def __init__(self, message: str, *, code: str = "install_rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SkillLinkStatus:
    """Link state of one skill at one known mapped destination."""

    destination: str
    harnesses: tuple[str, ...]
    detected_harnesses: tuple[str, ...]
    linked: bool


@dataclass(frozen=True)
class SkillEntry:
    """Bounded registry entry: identity, revision, edit and link state."""

    name: str
    default: bool
    revision: str
    source: str  # "bundled" (package resource) or "saved" (canonical store)
    edited: bool
    installed: bool
    links: tuple[SkillLinkStatus, ...]
    detected_harnesses: tuple[str, ...]


@dataclass(frozen=True)
class SkillValidationEntry:
    """Read-only prevalidation verdict for one save candidate."""

    name: str
    ok: bool
    current_revision: str | None = None
    error_code: str | None = None
    error: str | None = None


_DEFAULT_BY_NAME = {meta.name: meta.default for meta in BUNDLED_SKILL_METADATA}


def _unique_destinations() -> list[tuple[Path, tuple[str, ...]]]:
    """Known mapped destinations in harness-map order with sharing harnesses."""
    seen: dict[Path, list[str]] = {}
    order: list[Path] = []
    for spec in SUPPORTED_HARNESS_INSTALL_SPECS:
        destination = Path(spec.destination_template).expanduser()
        if destination not in seen:
            seen[destination] = []
            order.append(destination)
        seen[destination].append(spec.harness)
    return [(destination, tuple(seen[destination])) for destination in order]


def _expected_link_target(store_root: Path, name: str) -> str:
    return str(store_root / name)


def _is_linked(destination: Path, name: str, expected_target: str) -> bool:
    """True only when <destination>/<name> links exactly at the canonical tree."""
    link_path = destination / name
    try:
        record = os.lstat(link_path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not stat.S_ISLNK(record.st_mode):
        return False
    try:
        return os.readlink(link_path) == expected_target
    except OSError:
        return False


def _read_baseline_files(store_root: Path, name: str) -> dict[str, str] | None | bool:
    """Return the recorded baseline inventory, None when protected, False when absent/broken."""
    metadata_path = store_root / ".metadata" / f"{name}.json"
    try:
        record = os.lstat(metadata_path)
    except OSError:
        return False
    if not stat.S_ISREG(record.st_mode):
        return False
    try:
        with open(metadata_path, "rb") as handle:
            metadata = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        return False
    files = metadata.get("files")
    if files is None:
        return None if metadata.get("protected") is True else False
    if not isinstance(files, dict) or not files:
        return False
    cleaned: dict[str, str] = {}
    for relative, digest in files.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            return False
        cleaned[relative] = digest
    return cleaned


def _scan_tree_state(directory: Path) -> dict[str, str | None] | None:
    """Hash every contained file; symlink/irregular entries map to None."""
    state: dict[str, str | None] = {}

    def walk(current: Path, prefix: str) -> None:
        for child in current.iterdir():
            relative = f"{prefix}{child.name}"
            try:
                record = os.lstat(child)
            except OSError:
                return None
            if stat.S_ISLNK(record.st_mode):
                state[relative] = None
            elif stat.S_ISDIR(record.st_mode):
                if walk(child, relative + "/") is None:
                    return None
            elif stat.S_ISREG(record.st_mode):
                try:
                    with open(child, "rb") as handle:
                        state[relative] = hashlib.sha256(handle.read()).hexdigest()
                except OSError:
                    return None
            else:
                state[relative] = None
        return state

    if walk(directory, "") is None:
        return None
    return state


class SkillService:
    """Bounded skill facade; the store root resolves per operation."""

    def __init__(
        self,
        *,
        store_root: Path | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store_root = store_root
        self._which = which or shutil.which

    def _root(self) -> Path:
        if self._store_root is not None:
            return self._store_root
        return default_store_root()

    def _store(self) -> SkillStore:
        return SkillStore(self._root())

    def _detected_harnesses(self) -> tuple[str, ...]:
        return tuple(
            spec.harness
            for spec in SUPPORTED_HARNESS_INSTALL_SPECS
            if self._which(spec.executable) is not None
        )

    def _links_for(self, name: str) -> tuple[tuple[SkillLinkStatus, ...], bool]:
        store_root = self._root()
        expected = _expected_link_target(store_root, name)
        links: list[SkillLinkStatus] = []
        installed = False
        for destination, harnesses in _unique_destinations():
            detected = tuple(
                harness
                for harness in harnesses
                if self._which(_executable_for(harness)) is not None
            )
            linked = _is_linked(destination, name, expected)
            installed = installed or linked
            links.append(
                SkillLinkStatus(
                    destination=str(destination),
                    harnesses=harnesses,
                    detected_harnesses=detected,
                    linked=linked,
                )
            )
        return tuple(links), installed

    def _edited(self, name: str, source: str) -> bool:
        """A saved tree is edited when it no longer matches its baseline."""
        if source != "saved":
            return False
        baseline = _read_baseline_files(self._root(), name)
        if baseline is False or baseline is None:
            # Missing/unreadable/protected metadata: preserve conservatively.
            return True
        state = _scan_tree_state(self._root() / name)
        if state is None:
            return True
        if any(digest is None for digest in state.values()):
            return True
        return {k: v for k, v in state.items() if v is not None} != baseline

    def list_entries(self) -> tuple[SkillEntry, ...]:
        """List exactly the registered bundled skills; never initialize stores."""
        store = self._store()
        detected = self._detected_harnesses()
        entries: list[SkillEntry] = []
        for name in BUNDLED_SKILL_NAMES:
            try:
                document = store.read(name)
            except SkillCatalogError as exc:
                raise SkillStoreError(str(exc)) from exc
            links, installed = self._links_for(name)
            entries.append(
                SkillEntry(
                    name=name,
                    default=bool(_DEFAULT_BY_NAME.get(name, False)),
                    revision=document.revision,
                    source=document.source,
                    edited=self._edited(name, document.source),
                    installed=installed,
                    links=links,
                    detected_harnesses=detected,
                )
            )
        return tuple(entries)

    def read_entry(self, name: str) -> tuple[SkillEntry, str]:
        """Return one entry plus its effective content; pure read."""
        canonical = self._require_bundled_name(name)
        store = self._store()
        try:
            document = store.read(canonical)
        except SkillCatalogError as exc:
            raise SkillStoreError(str(exc)) from exc
        links, installed = self._links_for(canonical)
        return (
            SkillEntry(
                name=canonical,
                default=bool(_DEFAULT_BY_NAME.get(canonical, False)),
                revision=document.revision,
                source=document.source,
                edited=self._edited(canonical, document.source),
                installed=installed,
                links=links,
                detected_harnesses=self._detected_harnesses(),
            ),
            document.content,
        )

    def save_entry(self, name: str, content: object, expected_revision: object) -> tuple[SkillEntry, str]:
        """Compare-and-swap one canonical SKILL.md; never installs anything."""
        canonical = self._require_bundled_name(name)
        if not isinstance(content, str):
            raise SkillStoreError("skill content must be a string")
        if not isinstance(expected_revision, str):
            raise SkillStoreError("expected_revision must be a SHA-256 hex digest")
        store = self._store()
        try:
            store.save(canonical, content, expected_revision)
        except SkillCatalogError as exc:
            raise SkillStoreError(str(exc)) from exc
        return self.read_entry(canonical)

    def validate_batch(self, candidates: Iterable[dict[str, object]]) -> tuple[SkillValidationEntry, ...]:
        """Validate save candidates read-only; never a concurrency guarantee."""
        store = self._store()
        results: list[SkillValidationEntry] = []
        for candidate in candidates:
            raw_name = candidate.get("name")
            content = candidate.get("content")
            expected_revision = candidate.get("expected_revision")
            if not isinstance(raw_name, str) or not is_bundled_skill_name(raw_name):
                results.append(
                    SkillValidationEntry(
                        name=str(raw_name),
                        ok=False,
                        error_code="skill_not_found",
                        error=f"unknown bundled skill: {raw_name!r}",
                    )
                )
                continue
            try:
                document = store.read(raw_name)
            except SkillStoreError as exc:
                results.append(
                    SkillValidationEntry(
                        name=raw_name,
                        ok=False,
                        error_code="skill_unreadable",
                        error=" ".join(str(exc).split())[:300],
                    )
                )
                continue
            if not isinstance(expected_revision, str) or not expected_revision:
                results.append(
                    SkillValidationEntry(
                        name=raw_name,
                        ok=False,
                        current_revision=document.revision,
                        error_code="invalid_revision",
                        error="expected_revision must be a SHA-256 hex digest",
                    )
                )
                continue
            if expected_revision != document.revision:
                results.append(
                    SkillValidationEntry(
                        name=raw_name,
                        ok=False,
                        current_revision=document.revision,
                        error_code="revision_conflict",
                        error="skill revision does not match the effective document",
                    )
                )
                continue
            if not isinstance(content, str):
                results.append(
                    SkillValidationEntry(
                        name=raw_name,
                        ok=False,
                        current_revision=document.revision,
                        error_code="skill_invalid",
                        error="skill content must be a string",
                    )
                )
                continue
            from aflow.skill_store import validate_skill_document

            try:
                encoded = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                results.append(
                    SkillValidationEntry(
                        name=raw_name,
                        ok=False,
                        current_revision=document.revision,
                        error_code="skill_invalid",
                        error=" ".join(str(exc).split())[:300],
                    )
                )
                continue
            try:
                validate_skill_document(raw_name, encoded)
            except SkillStoreError as exc:
                results.append(
                    SkillValidationEntry(
                        name=raw_name,
                        ok=False,
                        current_revision=document.revision,
                        error_code="skill_invalid",
                        error=" ".join(str(exc).split())[:300],
                    )
                )
                continue
            results.append(
                SkillValidationEntry(
                    name=raw_name, ok=True, current_revision=document.revision
                )
            )
        return tuple(results)

    def install_default(self) -> InstallResult:
        """Run the shared default auto installer (CLI ``install-skills --yes``)."""
        store = self._store()
        try:
            return install_skills(
                destination=None,
                yes=True,
                only_skills=None,
                include_optional=False,
                store=store,
                stdout=StringIO(),
            )
        except InstallerError as exc:
            message = " ".join(str(exc).split())
            if "No supported" in message:
                raise SkillInstallError(message[:300], code="no_install_targets") from exc
            raise SkillInstallError(message[:300]) from exc

    @staticmethod
    def _require_bundled_name(name: object) -> str:
        if not isinstance(name, str) or not name:
            raise SkillStoreError("skill name must be a short nonempty string")
        if not is_bundled_skill_name(name):
            raise SkillNotFound(f"Unknown bundled skill: {name!r}")
        return name


def _executable_for(harness: str) -> str:
    for spec in SUPPORTED_HARNESS_INSTALL_SPECS:
        if spec.harness == harness:
            return spec.executable
    return harness

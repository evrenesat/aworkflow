"""Canonical per-account skill document store.

One account-level store at ``Path.home() / '.config' / 'aflow' / 'skills'``
holds one directory per bundled skill: the editable ``SKILL.md`` plus the
supporting files materialized from the bundled package resources on first
save. Per-skill baseline metadata and the store lock live under
``<root>/.metadata/``, outside every installed skill directory, so skill
directories contain only the files a harness or agent reads.

Reads are pure: they never create, initialize, refresh, or reinstall anything.
The effective document is the saved canonical ``SKILL.md`` when it is a
regular file, otherwise that name's bundled package resource. A malformed or
unreadable canonical document is an error, never a silent fallback to the
package copy.

Revisions are the SHA-256 of the effective ``SKILL.md`` UTF-8 bytes. Saves are
compare-and-swap on that revision under one cross-process advisory lock, so
two writers serialize and a writer that observed bundled bytes still wins if
nobody materialized the tree first. Content replacement is atomic: concurrent
readers observe either the complete old or the complete new document. A save
never touches supporting files or the recorded package baseline after
initialization; refresh decisions compare against that baseline, not against
the user's saved text.

``SkillStore.refresh_skills(names)`` is the only explicit refresh entry point;
runtime reads and saves never refresh anything. A canonical tree is unedited
exactly when its complete relative-file inventory and content hashes match the
recorded baseline; unedited trees are replaced with the incoming package
files, while any changed, extra, missing, or irregular user file preserves the
entire tree (``preserved_edited``). An existing tree without metadata is
adopted only when it exactly matches the current package; otherwise it is
preserved and protected with an unknown baseline. Refreshes stage and validate
the full incoming tree first, mutate under a store-owned transaction marker
holding only bounded relative paths and hashes, keep rollback material until
the per-skill refresh succeeds, and roll back prior files plus metadata on
ordinary I/O errors. A crash leaves the marker behind: reads and saves of that
skill fail with an incomplete-refresh error until the next explicit refresh
verifiably rolls the transaction back. Each skill's result is independent;
there is no multi-skill batch transaction.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
try:
    from importlib.resources.abc import Traversable
except ImportError:
    from importlib.abc import Traversable
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Callable, Iterable, Iterator

import yaml

from .skill_catalog import (
    BUNDLED_SKILL_NAMES,
    SkillCatalogError,
    bundled_skill_resource,
    validate_bundled_skill_name,
)

SKILL_DOCUMENT_NAME = "SKILL.md"
METADATA_DIR_NAME = ".metadata"
METADATA_SCHEMA_VERSION = 1
STORE_LOCK_NAME = "store.lock"
REFRESH_MARKER_SUFFIX = ".refresh.json"
ROLLBACK_DIR_NAME = "rollback"
MAX_SKILL_DOCUMENT_BYTES = 1024 * 1024
_DEFAULT_ROOT = Path.home() / ".config" / "aflow" / "skills"
_REVISION_RE = re.compile(r"[0-9a-f]{64}")


class SkillStoreError(RuntimeError):
    """A bounded skill-store operation failure; saved bytes are unchanged."""


class SkillValidationError(SkillStoreError):
    """A ``SKILL.md`` document failed structural validation."""


class SkillRevisionConflict(SkillStoreError):
    """The submitted expected revision no longer matches the effective document."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("skill revision does not match the effective document")
        self.current_revision = current_revision


class SkillRefreshIncomplete(SkillStoreError):
    """An interrupted refresh transaction is pending for this skill."""


@dataclass(frozen=True)
class SkillMarkdownDocument:
    """Validated ``SKILL.md`` structure: frontmatter mapping and Markdown body."""

    frontmatter: dict
    body: str


@dataclass(frozen=True)
class SkillDocument:
    """One effective skill document observed through a pure read."""

    name: str
    content: str
    revision: str
    source: str  # "saved" (canonical store) or "bundled" (package resource)


@dataclass(frozen=True)
class SkillSaveResult:
    """Acknowledgement of one revisioned save."""

    name: str
    revision: str
    changed: bool
    materialized: bool


@dataclass(frozen=True)
class SkillRefreshResult:
    """Per-skill acknowledgement of one explicit refresh.

    ``status`` is ``initialized`` (missing canonical tree materialized from the
    package), ``refreshed`` (an unedited or adopted tree now matches the
    incoming package), ``preserved_edited`` (the whole tree was kept because it
    is edited or has an unknown baseline), or ``failed`` with a bounded
    ``error`` after any rollback.
    """

    name: str
    status: str
    changed: bool
    edited: bool
    error: str | None = None


def sha256_revision(payload: bytes) -> str:
    """Return the store's revision identifier for exact document bytes."""
    return hashlib.sha256(payload).hexdigest()


def _require_safe_name(name: object) -> str:
    if not isinstance(name, str) or not name or len(name) > 255 or "\x00" in name:
        raise SkillStoreError("skill name must be a short nonempty string")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise SkillStoreError(f"unsafe skill name: {name!r}")
    return name


def _require_revision_format(expected_revision: object) -> str:
    if (
        not isinstance(expected_revision, str)
        or _REVISION_RE.fullmatch(expected_revision) is None
    ):
        raise SkillStoreError("expected_revision must be a SHA-256 hex digest")
    return expected_revision


def _require_safe_relative_path(relative: object) -> str:
    """Validate one contained relative resource path used by store mutations."""
    if not isinstance(relative, str) or not relative or len(relative) > 1024:
        raise SkillStoreError(f"unsafe skill resource path: {relative!r}")
    if "\x00" in relative or relative.startswith("/"):
        raise SkillStoreError(f"unsafe skill resource path: {relative!r}")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SkillStoreError(f"unsafe skill resource path: {relative!r}")
    return relative


def validate_skill_document(skill_name: str, payload: bytes) -> SkillMarkdownDocument:
    """Validate complete ``SKILL.md`` bytes and return their parsed boundary.

    The document must be UTF-8 without NUL bytes, at most 1 MiB, carry YAML
    frontmatter whose ``name`` matches this skill and whose ``description`` is
    a nonempty string, and keep a nonempty Markdown body. Extra safe
    frontmatter fields are permitted; the Markdown body is never executed or
    templated.
    """
    if len(payload) > MAX_SKILL_DOCUMENT_BYTES:
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' is too large "
            f"({len(payload)} bytes; limit is {MAX_SKILL_DOCUMENT_BYTES})"
        )
    if b"\x00" in payload:
        raise SkillValidationError(f"SKILL.md for '{skill_name}' must not contain NUL bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' is not valid UTF-8: {exc}"
        ) from exc
    return parse_skill_document(skill_name, text)


def parse_skill_document(skill_name: str, text: str) -> SkillMarkdownDocument:
    """Validate skill Markdown text and return its frontmatter/body boundary."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' must start with a '---' frontmatter delimiter"
        )
    closing = None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r") == "---":
            closing = index
            break
    if closing is None:
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' has unterminated YAML frontmatter"
        )
    frontmatter_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])
    if not body.strip():
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' must keep a nonempty Markdown body"
        )
    try:
        composition = yaml.compose(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' frontmatter is not valid YAML: {exc}"
        ) from exc
    if composition is not None and _contains_yaml_aliases(composition):
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' frontmatter must not use YAML anchors or aliases"
        )
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' frontmatter is not valid YAML: {exc}"
        ) from exc
    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' frontmatter must be a YAML mapping"
        )
    declared_name = frontmatter.get("name")
    if not isinstance(declared_name, str) or declared_name != skill_name:
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' frontmatter must declare the matching name"
        )
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError(
            f"SKILL.md for '{skill_name}' frontmatter must keep a nonempty string description"
        )
    return SkillMarkdownDocument(frontmatter=frontmatter, body=body)


def _contains_yaml_aliases(node: object) -> bool:
    """Detect YAML alias nodes before safe_load expands anchor bombs.

    PyYAML's composer resolves aliases by sharing the anchored node object,
    so any node identity observed twice means the document uses aliases.
    """
    seen: set[int] = set()

    def visit(current: object) -> bool:
        if id(current) in seen:
            return True
        seen.add(id(current))
        if isinstance(current, yaml.nodes.SequenceNode):
            return any(visit(child) for child in current.value)
        if isinstance(current, yaml.nodes.MappingNode):
            return any(
                visit(key) or visit(value) for key, value in current.value
            )
        return False

    return visit(node)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillStoreError(f"cannot inspect skill store path {path}: {exc}") from exc


def _read_no_follow(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise SkillStoreError(f"skill document is missing: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SkillStoreError(
                f"skill document must not be a symbolic link: {path}"
            ) from exc
        raise SkillStoreError(f"cannot read skill document {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            return handle.read()
    except OSError as exc:
        raise SkillStoreError(f"cannot read skill document {path}: {exc}") from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace file bytes atomically; readers see the old or new complete file."""
    directory = path.parent
    try:
        existing = os.lstat(path)
        mode = stat.S_IMODE(existing.st_mode)
    except FileNotFoundError:
        mode = 0o644
    except OSError as exc:
        raise SkillStoreError(f"cannot inspect {path}: {exc}") from exc
    temp = directory / f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    try:
        descriptor = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
        )
    except OSError as exc:
        raise SkillStoreError(
            f"cannot stage skill document write in {directory}: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise SkillStoreError(f"cannot replace skill document {path}: {exc}") from exc
    _fsync_directory(directory)


def _package_file_mode(source: Traversable) -> int:
    try:
        return stat.S_IMODE(os.stat(source).st_mode)  # type: ignore[arg-type]
    except (OSError, TypeError):
        return 0o644


def _collect_package_files(source: Traversable, prefix: str = "") -> list[tuple[str, bytes, int]]:
    """Walk one bundled skill resource tree into contained relative paths."""
    collected: list[tuple[str, bytes, int]] = []
    for child in source.iterdir():
        relative = f"{prefix}{child.name}"
        _require_safe_relative_path(relative)
        if child.is_dir():
            collected.extend(_collect_package_files(child, prefix=f"{relative}/"))
            continue
        if not child.is_file():
            raise SkillStoreError(f"bundled resource is not a regular file: {relative}")
        try:
            with child.open("rb") as handle:
                payload = handle.read()
        except OSError as exc:
            raise SkillStoreError(f"cannot read bundled resource {relative}: {exc}") from exc
        collected.append((relative, payload, _package_file_mode(child)))
    return collected


def _cleanup_staging(staging: Path) -> None:
    """Remove one skill's staging directory and the per-skill staging parent."""
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.parent.rmdir()
    except OSError:
        pass


def _load_bundled_package_tree(name: str) -> list[tuple[str, bytes, int]]:
    """Default package loader: the real bundled resources for one skill."""
    return _collect_package_files(bundled_skill_resource(name))


def _tree_matches_baseline(tree: dict[str, str | None], baseline: dict[str, str]) -> bool:
    """A tree is unedited only with the exact baseline inventory and hashes.

    Any symlink/irregular entry (``None``), extra file, missing file, or
    changed content hash makes the tree edited.
    """
    if any(digest is None for digest in tree.values()):
        return False
    return {relative: digest for relative, digest in tree.items()} == baseline


class SkillStore:
    """Revisioned, cross-process safe store for canonical skill documents."""

    def __init__(
        self,
        root: str | Path | None = None,
        package_tree_loader: Callable[[str], list[tuple[str, bytes, int]]] | None = None,
    ) -> None:
        if root is None:
            root = _DEFAULT_ROOT
        self._root = Path(root).expanduser().resolve()
        if package_tree_loader is None:
            package_tree_loader = _load_bundled_package_tree
        self._package_tree_loader = package_tree_loader

    @property
    def root(self) -> Path:
        return self._root

    @property
    def metadata_root(self) -> Path:
        return self._root / METADATA_DIR_NAME

    # ------------------------------------------------------------------ reads

    def list_skills(self) -> tuple[SkillDocument, ...]:
        """List every registered bundled skill's effective document, purely.

        A skill with a pending interrupted-refresh marker makes its read fail
        with :class:`SkillRefreshIncomplete`; the listing is loud rather than
        silently skipping uncertain content.
        """
        return tuple(self.read(name) for name in BUNDLED_SKILL_NAMES)

    def read(self, name: str) -> SkillDocument:
        """Return the effective document for one skill without writing anything."""
        _require_safe_name(name)
        self._require_no_incomplete_refresh(name)
        payload, source = self._effective_payload(name)
        validate_skill_document(name, payload)
        return SkillDocument(
            name=name,
            content=payload.decode("utf-8"),
            revision=_sha256(payload),
            source=source,
        )

    # ------------------------------------------------------------------- save

    def save(self, name: str, content: str, expected_revision: str) -> SkillSaveResult:
        """Compare-and-swap the canonical ``SKILL.md`` for one bundled skill.

        ``expected_revision`` is the revision the caller last observed through
        a pure read. The comparison and the write both happen under the store
        lock, including when the effective source is still the bundled package
        resource. Identical bytes are a no-op. First initialization
        materializes the complete bundled directory into canonical storage and
        records the package baseline before replacing ``SKILL.md``.
        """
        _require_safe_name(name)
        _require_revision_format(expected_revision)
        if not isinstance(content, str):
            raise SkillValidationError("skill content must be a string")
        try:
            payload = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SkillValidationError(f"skill content must be UTF-8 text: {exc}") from exc
        validate_skill_document(name, payload)
        try:
            validate_bundled_skill_name(name)
        except SkillCatalogError as exc:
            raise SkillStoreError(str(exc)) from exc

        with self._write_lock():
            self._require_no_incomplete_refresh(name)
            current_payload, source = self._effective_payload(name)
            validate_skill_document(name, current_payload)
            current_revision = _sha256(current_payload)
            if current_revision != expected_revision:
                raise SkillRevisionConflict(current_revision)
            if current_payload == payload:
                return SkillSaveResult(
                    name=name, revision=current_revision, changed=False, materialized=False
                )
            if source == "saved":
                self._require_managed_tree(name)
                _atomic_write(self._skill_document_path(name), payload)
            else:
                self._initialize_tree(name, payload)
            return SkillSaveResult(
                name=name, revision=_sha256(payload), changed=True, materialized=source != "saved"
            )

    # -------------------------------------------------------------- internals

    def _skill_directory(self, name: str) -> Path:
        return self._root / name

    def _skill_document_path(self, name: str) -> Path:
        return self._skill_directory(name) / SKILL_DOCUMENT_NAME

    def _metadata_path(self, name: str) -> Path:
        return self.metadata_root / f"{name}.json"

    def _transaction_path(self, name: str) -> Path:
        return self.metadata_root / f"{name}{REFRESH_MARKER_SUFFIX}"

    def _rollback_root(self, name: str) -> Path:
        return self.metadata_root / "staging" / f"{name}.{ROLLBACK_DIR_NAME}"

    def _package_tree(self, name: str) -> list[tuple[str, bytes, int]]:
        """Load the incoming package resource tree for one bundled skill."""
        try:
            return self._package_tree_loader(name)
        except SkillCatalogError as exc:
            raise SkillStoreError(
                f"skill '{name}' has no saved canonical document and no bundled "
                f"resource: {exc}"
            ) from exc
        except OSError as exc:
            raise SkillStoreError(
                f"cannot read bundled resources for '{name}': {exc}"
            ) from exc

    def _require_no_incomplete_refresh(self, name: str) -> None:
        """Reject reads and saves while an interrupted refresh is pending."""
        if _lstat(self._transaction_path(name)) is not None:
            raise SkillRefreshIncomplete(
                f"skill '{name}' has an interrupted refresh transaction; "
                "run an explicit refresh to recover it before reading or saving"
            )

    def _require_safe_root(self) -> None:
        root_stat = _lstat(self._root)
        if root_stat is not None and not stat.S_ISDIR(root_stat.st_mode):
            raise SkillStoreError(f"skill store root is not a directory: {self._root}")

    def _effective_payload(self, name: str) -> tuple[bytes, str]:
        """Return the effective SKILL.md bytes and their source, read-only."""
        self._require_safe_root()
        directory_stat = _lstat(self._skill_directory(name))
        if directory_stat is not None and stat.S_ISLNK(directory_stat.st_mode):
            raise SkillStoreError(
                f"canonical skill directory must not be a symbolic link: {name}"
            )
        if directory_stat is not None and not stat.S_ISDIR(directory_stat.st_mode):
            raise SkillStoreError(f"canonical skill path is not a directory: {name}")
        document_stat = _lstat(self._skill_document_path(name))
        if document_stat is not None:
            if stat.S_ISLNK(document_stat.st_mode):
                raise SkillStoreError(
                    f"canonical SKILL.md must not be a symbolic link: {name}"
                )
            if not stat.S_ISREG(document_stat.st_mode):
                raise SkillStoreError(f"canonical SKILL.md is not a regular file: {name}")
            return _read_no_follow(self._skill_document_path(name)), "saved"
        package_files = self._package_tree(name)
        package_payloads = {relative: payload for relative, payload, _ in package_files}
        if SKILL_DOCUMENT_NAME not in package_payloads:
            raise SkillStoreError(f"bundled skill '{name}' is missing its SKILL.md resource")
        return package_payloads[SKILL_DOCUMENT_NAME], "bundled"

    def _load_metadata(self, name: str) -> dict:
        path = self._metadata_path(name)
        metadata_stat = _lstat(path)
        if metadata_stat is None:
            raise SkillStoreError(
                f"canonical skill '{name}' has no store baseline metadata; "
                "the tree is not store-managed"
            )
        if stat.S_ISLNK(metadata_stat.st_mode) or not stat.S_ISREG(metadata_stat.st_mode):
            raise SkillStoreError(f"skill baseline metadata must be a regular file: {name}")
        payload = _read_no_follow(path)
        try:
            metadata = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SkillStoreError(
                f"skill baseline metadata is unreadable for '{name}': {exc}"
            ) from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema_version") != METADATA_SCHEMA_VERSION
        ):
            raise SkillStoreError(
                f"skill baseline metadata for '{name}' has an unsupported schema"
            )
        files = metadata.get("files")
        if files is None:
            # An unknown/protected baseline: refresh must preserve the tree.
            if metadata.get("protected") is not True:
                raise SkillStoreError(
                    f"skill baseline metadata for '{name}' has an unknown baseline "
                    "without the protected marker"
                )
            return metadata
        if not isinstance(files, dict) or not files:
            raise SkillStoreError(
                f"skill baseline metadata for '{name}' lists no baseline files"
            )
        for relative, digest in files.items():
            _require_safe_relative_path(relative)
            if not isinstance(digest, str) or _REVISION_RE.fullmatch(digest) is None:
                raise SkillStoreError(
                    f"skill baseline metadata for '{name}' has an invalid digest for {relative!r}"
                )
        return metadata

    def _try_load_metadata(self, name: str) -> dict | None:
        """Return baseline metadata, or None when the tree has none yet."""
        if _lstat(self._metadata_path(name)) is None:
            return None
        return self._load_metadata(name)

    def _require_managed_tree(self, name: str) -> None:
        self._load_metadata(name)

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Serialize store mutations across processes with one advisory lock."""
        self._require_safe_root()
        try:
            self.metadata_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SkillStoreError(
                f"cannot create skill store metadata directory: {exc}"
            ) from exc
        lock_path = self.metadata_root / STORE_LOCK_NAME
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise SkillStoreError(f"cannot open skill store lock {lock_path}: {exc}") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _metadata_bytes(self, name: str, files: dict | None, protected: bool = False) -> bytes:
        metadata: dict = {
            "schema_version": METADATA_SCHEMA_VERSION,
            "skill": name,
            "files": files,
        }
        if protected:
            metadata["protected"] = True
        return json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    def _record_baseline_metadata_locked(self, name: str, baseline: dict[str, str]) -> None:
        _atomic_write(self._metadata_path(name), self._metadata_bytes(name, baseline))

    def _record_protected_metadata_locked(self, name: str) -> None:
        _atomic_write(self._metadata_path(name), self._metadata_bytes(name, None, protected=True))

    def _materialize_tree_locked(
        self,
        name: str,
        package_files: list[tuple[str, bytes, int]],
        baseline: dict[str, str],
    ) -> None:
        """Stage, validate, and rename the package tree into canonical storage.

        The staged tree is renamed into place (atomic for a missing or empty
        target) and the package baseline metadata is recorded before any
        ``SKILL.md`` replacement happens. A failure before the rename leaves
        the store unchanged.
        """
        canonical_dir = self._skill_directory(name)
        existing = _lstat(canonical_dir)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise SkillStoreError(
                    f"canonical skill directory must not be a symbolic link: {name}"
                )
            if not stat.S_ISDIR(existing.st_mode):
                raise SkillStoreError(f"canonical skill path is not a directory: {name}")
            if any(canonical_dir.iterdir()):
                raise SkillStoreError(
                    f"refusing to initialize skill '{name}' over an existing unmanaged directory"
                )

        staging = (
            self.metadata_root
            / "staging"
            / f"{name}.{os.getpid()}.{os.urandom(6).hex()}"
        )
        try:
            try:
                staging.mkdir(parents=True)
            except OSError as exc:
                raise SkillStoreError(
                    f"cannot stage skill '{name}' materialization: {exc}"
                ) from exc
            for relative, payload, mode in package_files:
                target = staging / _require_safe_relative_path(relative)
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise SkillStoreError(
                        f"cannot stage skill resource directory for '{name}': {exc}"
                    ) from exc
                _atomic_write(target, payload)
                try:
                    os.chmod(target, mode)
                except OSError as exc:
                    raise SkillStoreError(
                        f"cannot preserve permissions for staged {relative}: {exc}"
                    ) from exc
                if _sha256(_read_no_follow(target)) != baseline[relative]:
                    raise SkillStoreError(f"staged resource failed validation: {relative}")
            try:
                if existing is not None:
                    canonical_dir.rmdir()
                os.rename(staging, canonical_dir)
            except OSError as exc:
                raise SkillStoreError(
                    f"cannot materialize canonical skill directory for '{name}': {exc}"
                ) from exc
            _fsync_directory(self._root)
            self._record_baseline_metadata_locked(name, baseline)
        finally:
            _cleanup_staging(staging)

    def _initialize_tree(self, name: str, saved_payload: bytes) -> None:
        """Materialize the complete bundled directory, then replace SKILL.md."""
        package_files = self._package_tree(name)
        package_payloads = {relative: payload for relative, payload, _ in package_files}
        if SKILL_DOCUMENT_NAME not in package_payloads:
            raise SkillStoreError(f"bundled skill '{name}' is missing its SKILL.md resource")
        validate_skill_document(name, package_payloads[SKILL_DOCUMENT_NAME])
        baseline = {relative: _sha256(payload) for relative, payload, _ in package_files}
        self._materialize_tree_locked(name, package_files, baseline)
        _atomic_write(self._skill_document_path(name), saved_payload)

    # --------------------------------------------------------------- refresh

    def refresh_skills(self, names: Iterable[str]) -> tuple[SkillRefreshResult, ...]:
        """Explicitly prepare/refresh selected bundled names under the store lock.

        Missing canonical trees are initialized from the incoming package. A
        tree exactly matching its recorded baseline is unedited and is replaced
        with the incoming package files; its baseline advances only after the
        refresh succeeds. Edited or unknown-baseline trees are preserved whole.
        Interrupted refresh transactions are rolled back first. Each skill is
        independent: a per-skill failure is reported in its own result and
        never claims a whole-batch transaction.
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for name in names:
            _require_safe_name(name)
            try:
                validate_bundled_skill_name(name)
            except SkillCatalogError as exc:
                raise SkillStoreError(str(exc)) from exc
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        with self._write_lock():
            self._recover_interrupted_locked()
            results: list[SkillRefreshResult] = []
            for name in ordered:
                try:
                    self._require_no_incomplete_refresh(name)
                    results.append(self._refresh_one_locked(name))
                except (SkillStoreError, OSError) as exc:
                    results.append(
                        SkillRefreshResult(
                            name=name,
                            status="failed",
                            changed=False,
                            edited=False,
                            error=str(exc),
                        )
                    )
            return tuple(results)

    def _refresh_one_locked(self, name: str) -> SkillRefreshResult:
        self._require_safe_root()
        canonical_dir = self._skill_directory(name)
        directory_stat = _lstat(canonical_dir)
        if directory_stat is not None and stat.S_ISLNK(directory_stat.st_mode):
            raise SkillStoreError(
                f"canonical skill directory must not be a symbolic link: {name}"
            )
        if directory_stat is not None and not stat.S_ISDIR(directory_stat.st_mode):
            raise SkillStoreError(f"canonical skill path is not a directory: {name}")
        incoming = self._package_tree(name)
        incoming_payloads = {relative: payload for relative, payload, _ in incoming}
        if SKILL_DOCUMENT_NAME not in incoming_payloads:
            raise SkillStoreError(f"bundled skill '{name}' is missing its SKILL.md resource")
        validate_skill_document(name, incoming_payloads[SKILL_DOCUMENT_NAME])
        incoming_baseline = {relative: _sha256(payload) for relative, payload, _ in incoming}

        if directory_stat is None or not any(canonical_dir.iterdir()):
            self._materialize_tree_locked(name, incoming, incoming_baseline)
            return SkillRefreshResult(
                name=name, status="initialized", changed=True, edited=False
            )

        metadata = self._try_load_metadata(name)
        tree = self._scan_tree_state(canonical_dir)
        if metadata is None:
            # Adopt only an exact match with the current package; never guess
            # an earlier package version or import an installed copy.
            if _tree_matches_baseline(tree, incoming_baseline):
                self._record_baseline_metadata_locked(name, incoming_baseline)
                return SkillRefreshResult(
                    name=name, status="refreshed", changed=False, edited=False
                )
            self._record_protected_metadata_locked(name)
            return SkillRefreshResult(
                name=name, status="preserved_edited", changed=False, edited=True
            )
        baseline = metadata["files"]
        if baseline is None or not _tree_matches_baseline(tree, baseline):
            return SkillRefreshResult(
                name=name, status="preserved_edited", changed=False, edited=True
            )
        changed = self._apply_refresh_locked(name, baseline, tree, incoming, incoming_baseline)
        return SkillRefreshResult(name=name, status="refreshed", changed=changed, edited=False)

    def _scan_tree_state(self, directory: Path) -> dict[str, str | None]:
        """Hash every contained file; symlink/irregular entries map to None.

        Store-owned metadata lives outside the tree, so it is never scanned.
        """
        state: dict[str, str | None] = {}

        def walk(current: Path, prefix: str) -> None:
            for child in current.iterdir():
                relative = f"{prefix}{child.name}"
                _require_safe_relative_path(relative)
                try:
                    child_stat = os.lstat(child)
                except OSError as exc:
                    raise SkillStoreError(
                        f"cannot inspect skill tree entry {relative}: {exc}"
                    ) from exc
                if stat.S_ISLNK(child_stat.st_mode):
                    state[relative] = None
                elif stat.S_ISDIR(child_stat.st_mode):
                    walk(child, relative + "/")
                elif stat.S_ISREG(child_stat.st_mode):
                    state[relative] = _sha256(_read_no_follow(child))
                else:
                    state[relative] = None

        walk(directory, "")
        return state

    def _apply_refresh_locked(
        self,
        name: str,
        baseline: dict[str, str],
        tree: dict[str, str | None],
        incoming: list[tuple[str, bytes, int]],
        incoming_baseline: dict[str, str],
    ) -> bool:
        """Replace an unedited tree with the incoming package under a marker."""
        canonical_dir = self._skill_directory(name)
        incoming_relatives = {relative for relative, _, _ in incoming}
        changes = [
            (relative, payload, mode)
            for relative, payload, mode in incoming
            if tree.get(relative) != _sha256(payload)
        ]
        obsolete = [relative for relative in baseline if relative not in incoming_relatives]
        if not changes and not obsolete:
            return False

        metadata_path = self._metadata_path(name)
        metadata_bytes = _read_no_follow(metadata_path)
        rollback_root = self._rollback_root(name)
        rollback_files = rollback_root / "files"
        if _lstat(self._transaction_path(name)) is None:
            # No live transaction: any existing rollback material here is a
            # leftover from a crash before its marker was written.
            shutil.rmtree(rollback_root, ignore_errors=True)
        try:
            rollback_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SkillStoreError(
                f"cannot stage rollback material for '{name}': {exc}"
            ) from exc

        entries: dict[str, dict[str, str | None]] = {}

        def _backup(relative: str) -> str:
            prior = _read_no_follow(canonical_dir / relative)
            digest = _sha256(prior)
            if tree.get(relative) != digest:
                raise SkillStoreError(
                    f"skill '{name}' changed during refresh preparation: {relative}"
                )
            backup = rollback_files / _require_safe_relative_path(relative)
            backup.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(backup, prior)
            return digest

        try:
            for relative, payload, _mode in changes:
                new_digest = _sha256(payload)
                old_digest = _backup(relative) if tree.get(relative) is not None else None
                entries[relative] = {"old": old_digest, "new": new_digest}
            for relative in obsolete:
                entries[relative] = {"old": _backup(relative), "new": None}
            # Keep the prior baseline metadata with the rollback material.
            _atomic_write(rollback_root / "metadata.json", metadata_bytes)
            if _sha256(_read_no_follow(rollback_root / "metadata.json")) != _sha256(
                metadata_bytes
            ):
                raise SkillStoreError(
                    f"cannot stage rollback metadata for '{name}'"
                )
            marker = {
                "schema_version": METADATA_SCHEMA_VERSION,
                "skill": name,
                "metadata_sha256": _sha256(metadata_bytes),
                "files": entries,
            }
            _atomic_write(
                self._transaction_path(name),
                json.dumps(marker, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
            try:
                for relative, payload, mode in changes:
                    target = canonical_dir / _require_safe_relative_path(relative)
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        raise SkillStoreError(
                            f"cannot create skill resource directory for {relative}: {exc}"
                        ) from exc
                    _atomic_write(target, payload)
                    try:
                        os.chmod(target, mode)
                    except OSError as exc:
                        raise SkillStoreError(
                            f"cannot preserve permissions for {relative}: {exc}"
                        ) from exc
                for relative in obsolete:
                    target = canonical_dir / _require_safe_relative_path(relative)
                    try:
                        os.unlink(target)
                    except OSError as exc:
                        raise SkillStoreError(
                            f"cannot remove obsolete skill file {relative}: {exc}"
                        ) from exc
                    _fsync_directory(target.parent)
                self._record_baseline_metadata_locked(name, incoming_baseline)
            except (SkillStoreError, OSError):
                # Ordinary I/O error: restore the prior per-skill tree and
                # baseline before reporting the failure.
                self._rollback_transaction_locked(name)
                raise
        except (SkillStoreError, OSError):
            _cleanup_staging(rollback_root)
            raise
        # The per-skill refresh succeeded; retire its transaction marker.
        try:
            self._transaction_path(name).unlink()
        except OSError as exc:
            raise SkillStoreError(
                f"cannot complete refresh transaction for '{name}': {exc}"
            ) from exc
        _cleanup_staging(rollback_root)
        return True

    def _recover_interrupted_locked(self) -> None:
        """Finish rollback for every pending transaction marker, loudly.

        A marker whose rollback material verifies is rolled back and retired;
        an uncertain marker is left in place so reads of that skill keep
        failing with an incomplete-refresh error instead of silently
        overwriting uncertain content.
        """
        if not self.metadata_root.is_dir():
            return
        for marker_path in sorted(self.metadata_root.glob(f"*{REFRESH_MARKER_SUFFIX}")):
            candidate = marker_path.name[: -len(REFRESH_MARKER_SUFFIX)]
            try:
                _require_safe_name(candidate)
                self._rollback_transaction_locked(candidate)
            except (SkillStoreError, OSError, ValueError):
                continue

    def _rollback_transaction_locked(self, name: str) -> None:
        """Roll one pending transaction back; raise on any uncertain content."""
        marker_path = self._transaction_path(name)
        try:
            marker = json.loads(_read_no_follow(marker_path).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SkillStoreError(
                f"skill '{name}' has an unreadable refresh transaction marker"
            ) from exc
        if (
            not isinstance(marker, dict)
            or marker.get("schema_version") != METADATA_SCHEMA_VERSION
            or not isinstance(marker.get("files"), dict)
        ):
            raise SkillStoreError(
                f"skill '{name}' has a malformed refresh transaction marker"
            )
        metadata_digest = marker.get("metadata_sha256")
        if not isinstance(metadata_digest, str) or _REVISION_RE.fullmatch(metadata_digest) is None:
            raise SkillStoreError(
                f"skill '{name}' has a malformed refresh transaction marker"
            )
        canonical_dir = self._skill_directory(name)
        rollback_root = self._rollback_root(name)
        rollback_files = rollback_root / "files"
        for relative, entry in marker["files"].items():
            _require_safe_relative_path(relative)
            if not isinstance(entry, dict):
                raise SkillStoreError(
                    f"skill '{name}' has a malformed refresh transaction entry"
                )
            old_digest = entry.get("old")
            new_digest = entry.get("new")
            for digest in (old_digest, new_digest):
                if digest is not None and (
                    not isinstance(digest, str) or _REVISION_RE.fullmatch(digest) is None
                ):
                    raise SkillStoreError(
                        f"skill '{name}' has a malformed refresh transaction entry"
                    )
            target = canonical_dir / relative
            if old_digest is None:
                # The refresh added this file; remove it only if it still has
                # the exact bytes the transaction wrote.
                if _lstat(target) is None:
                    continue
                if new_digest is None or _sha256(_read_no_follow(target)) != new_digest:
                    raise SkillStoreError(
                        f"skill '{name}' rollback is uncertain for {relative!r}"
                    )
                try:
                    os.unlink(target)
                except OSError as exc:
                    raise SkillStoreError(
                        f"cannot roll back skill file {relative}: {exc}"
                    ) from exc
            else:
                backup_bytes = _read_no_follow(rollback_files / relative)
                if _sha256(backup_bytes) != old_digest:
                    raise SkillStoreError(
                        f"skill '{name}' rollback is uncertain for {relative!r}"
                    )
                _atomic_write(target, backup_bytes)
        metadata_backup = _read_no_follow(rollback_root / "metadata.json")
        if _sha256(metadata_backup) != metadata_digest:
            raise SkillStoreError(
                f"skill '{name}' rollback is uncertain for its baseline metadata"
            )
        _atomic_write(self._metadata_path(name), metadata_backup)
        # The rollback is verified: retire the marker and its staging.
        try:
            marker_path.unlink()
        except OSError as exc:
            raise SkillStoreError(
                f"cannot retire refresh transaction marker for '{name}': {exc}"
            ) from exc
        _cleanup_staging(rollback_root)

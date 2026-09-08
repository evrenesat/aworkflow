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
from typing import Iterator

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


class SkillStore:
    """Revisioned, cross-process safe store for canonical skill documents."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = _DEFAULT_ROOT
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def metadata_root(self) -> Path:
        return self._root / METADATA_DIR_NAME

    # ------------------------------------------------------------------ reads

    def list_skills(self) -> tuple[SkillDocument, ...]:
        """List every registered bundled skill's effective document, purely."""
        return tuple(self.read(name) for name in BUNDLED_SKILL_NAMES)

    def read(self, name: str) -> SkillDocument:
        """Return the effective document for one skill without writing anything."""
        _require_safe_name(name)
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
        try:
            resource = bundled_skill_resource(name)
        except SkillCatalogError as exc:
            raise SkillStoreError(
                f"skill '{name}' has no saved canonical document and no bundled resource: {exc}"
            ) from exc
        try:
            with resource.joinpath(SKILL_DOCUMENT_NAME).open("rb") as handle:
                return handle.read(), "bundled"
        except OSError as exc:
            raise SkillStoreError(
                f"cannot read bundled SKILL.md resource for '{name}': {exc}"
            ) from exc

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

    def _initialize_tree(self, name: str, saved_payload: bytes) -> None:
        """Materialize the complete bundled directory, then replace SKILL.md.

        Package resources are staged and validated first; the staged tree is
        renamed into canonical storage, the package baseline metadata is
        recorded, and only then is ``SKILL.md`` replaced with the saved bytes.
        Any failure before the rename leaves the store unchanged.
        """
        self._require_safe_root()
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
        package_files = _collect_package_files(bundled_skill_resource(name))
        package_payloads = {relative: payload for relative, payload, _ in package_files}
        if SKILL_DOCUMENT_NAME not in package_payloads:
            raise SkillStoreError(f"bundled skill '{name}' is missing its SKILL.md resource")
        validate_skill_document(name, package_payloads[SKILL_DOCUMENT_NAME])
        baseline = {relative: _sha256(payload) for relative, payload, _ in package_files}

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
            metadata = {
                "schema_version": METADATA_SCHEMA_VERSION,
                "skill": name,
                "files": baseline,
            }
            _atomic_write(
                self._metadata_path(name),
                json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
            _atomic_write(self._skill_document_path(name), saved_payload)
        finally:
            _cleanup_staging(staging)

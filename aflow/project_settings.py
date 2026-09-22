"""Durable, shared scheduling settings for one Git project.

The settings file belongs to the primary checkout of a repository. Linked
worktrees therefore share one settings document, while projects that are not
Git checkouts retain the direct-root behaviour used by low-level control-plane
callers.

Reads are intentionally pure when the settings file is absent. Updates use
one advisory filesystem lock, compare the exact current file bytes through a
SHA-256 revision, and publish complete JSON bytes with an atomic replacement.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Literal


PROJECT_SETTINGS_SCHEMA_VERSION = 1
PROJECT_SETTINGS_FILENAME = "project-settings.json"
PROJECT_SETTINGS_LOCK_FILENAME = "project-settings.lock"
DEFAULT_AUTO_CONSUME_PLANS = True
DEFAULT_MAX_CONCURRENT_IMPLEMENTATIONS = 2
# A positive limit still needs a safe upper bound so malformed or accidental
# values cannot request an unbounded number of implementations.
MAX_CONCURRENT_IMPLEMENTATIONS = 1_000
MAX_PROJECT_SETTINGS_BYTES = 16 * 1024
EMPTY_PROJECT_SETTINGS_REVISION = hashlib.sha256(b"").hexdigest()
_GIT_IDENTITY_TIMEOUT_SECONDS = 5.0

_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSET = object()
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ProjectSettingsError(ValueError):
    """Base class for safe project-settings read and update failures."""


class ProjectSettingsValidationError(ProjectSettingsError):
    """A settings value or request is outside the supported contract."""


class ProjectSettingsPathError(ProjectSettingsError):
    """The settings path or Git project identity is unsafe or unavailable."""


class ProjectSettingsCorruptionError(ProjectSettingsError):
    """An existing settings artifact is not a valid version-1 document."""


class ProjectSettingsRevisionConflict(ProjectSettingsError):
    """The expected SHA-256 revision is older than the committed document."""

    def __init__(self, current_revision: str) -> None:
        self.current_revision = current_revision
        super().__init__("project settings revision conflict")


@dataclass(frozen=True)
class ProjectSettings:
    """The two project-local scheduling controls."""

    auto_consume_plans: bool = DEFAULT_AUTO_CONSUME_PLANS
    max_concurrent_implementations: int = DEFAULT_MAX_CONCURRENT_IMPLEMENTATIONS

    def __post_init__(self) -> None:
        _validate_values(
            self.auto_consume_plans,
            self.max_concurrent_implementations,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROJECT_SETTINGS_SCHEMA_VERSION,
            "auto_consume_plans": self.auto_consume_plans,
            "max_concurrent_implementations": self.max_concurrent_implementations,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ProjectSettings":
        """Parse a complete version-1 persisted payload."""
        if set(payload) != {
            "schema_version",
            "auto_consume_plans",
            "max_concurrent_implementations",
        }:
            raise ProjectSettingsValidationError("project settings have an invalid shape")
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != PROJECT_SETTINGS_SCHEMA_VERSION
        ):
            raise ProjectSettingsValidationError(
                "project settings schema version is unsupported"
            )
        try:
            return cls(
                auto_consume_plans=payload["auto_consume_plans"],  # type: ignore[arg-type]
                max_concurrent_implementations=payload[
                    "max_concurrent_implementations"
                ],  # type: ignore[arg-type]
            )
        except KeyError as exc:  # pragma: no cover - guarded by the shape check
            raise ProjectSettingsValidationError(
                "project settings have an invalid shape"
            ) from exc


ProjectSchedulingSettings = ProjectSettings


@dataclass(frozen=True)
class ProjectSettingsSnapshot:
    """Typed settings plus the revision used for the next compare-and-swap."""

    auto_consume_plans: bool
    max_concurrent_implementations: int
    revision: str
    persisted: bool
    source: Literal["defaults", "file"]

    def __post_init__(self) -> None:
        _validate_values(
            self.auto_consume_plans,
            self.max_concurrent_implementations,
        )
        _validate_revision(self.revision)
        if self.source not in {"defaults", "file"}:
            raise ProjectSettingsValidationError("project settings source is invalid")
        if self.persisted != (self.source == "file"):
            raise ProjectSettingsValidationError(
                "project settings persistence state is invalid"
            )

    @property
    def settings(self) -> ProjectSettings:
        return ProjectSettings(
            auto_consume_plans=self.auto_consume_plans,
            max_concurrent_implementations=self.max_concurrent_implementations,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "auto_consume_plans": self.auto_consume_plans,
            "max_concurrent_implementations": self.max_concurrent_implementations,
            "revision": self.revision,
            "persisted": self.persisted,
            "source": self.source,
        }


def _validate_values(
    auto_consume_plans: object,
    max_concurrent_implementations: object,
) -> None:
    if type(auto_consume_plans) is not bool:
        raise ProjectSettingsValidationError("auto_consume_plans must be a boolean")
    if (
        type(max_concurrent_implementations) is not int
        or not 1 <= max_concurrent_implementations <= MAX_CONCURRENT_IMPLEMENTATIONS
    ):
        raise ProjectSettingsValidationError(
            "max_concurrent_implementations must be a positive bounded integer"
        )


def _validate_revision(revision: object) -> str:
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        raise ProjectSettingsValidationError(
            "expected_revision must be a SHA-256 hex digest"
        )
    return revision


def _canonical_bytes(settings: ProjectSettings) -> bytes:
    return (
        json.dumps(
            settings.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _revision(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_existing(payload: bytes) -> ProjectSettings:
    if len(payload) > MAX_PROJECT_SETTINGS_BYTES:
        raise ProjectSettingsCorruptionError("project settings exceed the size limit")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProjectSettingsCorruptionError("project settings are malformed") from exc
    if not isinstance(value, Mapping):
        raise ProjectSettingsCorruptionError("project settings must be a JSON object")
    try:
        return ProjectSettings.from_mapping(value)
    except ProjectSettingsValidationError as exc:
        raise ProjectSettingsCorruptionError("project settings are invalid") from exc


def _canonical_directory(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ProjectSettingsPathError("project root must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectSettingsPathError("project root is unavailable") from exc
    if not resolved.is_dir():
        raise ProjectSettingsPathError("project root must be a directory")
    return resolved


@dataclass(frozen=True)
class ProjectIdentity:
    """The checkout and primary-root identity used by settings persistence."""

    checkout_root: Path
    primary_root: Path
    common_git_dir: Path | None

    @property
    def is_worktree(self) -> bool:
        return self.checkout_root != self.primary_root


def _git_entry_exists(checkout_root: Path) -> bool:
    """Return whether the exact checkout has a Git entry, failing closed."""
    entry = checkout_root / ".git"
    try:
        metadata = entry.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProjectSettingsPathError("Git project entry is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ProjectSettingsPathError("Git project entry must not be a symlink")
    return True


def _git_identity(checkout_root: Path) -> tuple[Path, Path, Path] | None:
    """Read one exact checkout's verified Git identity.

    ``None`` is reserved for a directory with no ``.git`` entry.  An existing
    entry that Git cannot verify is an identity failure, not evidence that the
    directory is a plain project.
    """
    if not _git_entry_exists(checkout_root):
        return None
    try:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(checkout_root),
                "--no-optional-locks",
                "rev-parse",
                "--show-toplevel",
                "--absolute-git-dir",
                "--git-common-dir",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_IDENTITY_TIMEOUT_SECONDS,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ProjectSettingsPathError("Git project identity is unavailable") from exc
    if completed.returncode != 0:
        raise ProjectSettingsPathError("Git project identity is invalid")
    output = completed.stdout.splitlines()
    if len(output) != 3 or any(not item for item in output):
        raise ProjectSettingsPathError("Git project identity is invalid")

    def resolve_git_path(raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = checkout_root / path
        try:
            return path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectSettingsPathError("Git project identity is unavailable") from exc

    observed_root = resolve_git_path(output[0])
    git_dir = resolve_git_path(output[1])
    common_dir = resolve_git_path(output[2])
    if (
        observed_root != checkout_root
        or not git_dir.is_dir()
        or not common_dir.is_dir()
    ):
        raise ProjectSettingsPathError("Git project identity is invalid")
    return observed_root, git_dir, common_dir


def _configured_primary_root(checkout_root: Path, common_dir: Path) -> Path | None:
    """Read an explicit external-metadata primary checkout hint from Git.

    A separate Git directory does not encode the primary checkout in
    ``common_dir.parent``.  Git's local ``core.worktree`` is the only bounded
    path hint we accept in that layout; the caller verifies the resulting
    checkout and common-directory identity before using it.
    """
    try:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(checkout_root),
                "--no-optional-locks",
                "config",
                "--local",
                "--get-all",
                "core.worktree",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_IDENTITY_TIMEOUT_SECONDS,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ProjectSettingsPathError("Git primary project identity is unavailable") from exc
    if completed.returncode == 1 and not completed.stdout:
        return None
    if completed.returncode != 0:
        raise ProjectSettingsPathError("Git primary project identity is invalid")
    values = completed.stdout.splitlines()
    if len(values) != 1 or not values[0] or "\x00" in values[0]:
        raise ProjectSettingsPathError("Git primary project identity is ambiguous")
    candidate = Path(values[0]).expanduser()
    if not candidate.is_absolute():
        candidate = common_dir / candidate
    try:
        return _canonical_directory(candidate)
    except ProjectSettingsPathError as exc:
        raise ProjectSettingsPathError("Git primary project is unavailable") from exc


def _verified_primary_identity(
    candidate: Path,
    common_dir: Path,
) -> tuple[Path, Path, Path] | None:
    """Return a candidate only when it is the exact primary Git checkout."""
    identity = _git_identity(candidate)
    if identity is None:
        return None
    _, git_dir, candidate_common_dir = identity
    if (
        os.path.normcase(str(git_dir)) != os.path.normcase(str(common_dir))
        or os.path.normcase(str(candidate_common_dir))
        != os.path.normcase(str(common_dir))
    ):
        return None
    return identity


def resolve_project_identity(project_root: Path) -> ProjectIdentity:
    """Resolve a checkout to the primary root that owns its settings."""
    checkout_root = _canonical_directory(project_root)
    identity = _git_identity(checkout_root)
    if identity is None:
        return ProjectIdentity(
            checkout_root=checkout_root,
            primary_root=checkout_root,
            common_git_dir=None,
        )
    observed_root, git_dir, common_dir = identity
    if os.path.normcase(str(git_dir)) == os.path.normcase(str(common_dir)):
        return ProjectIdentity(
            checkout_root=observed_root,
            primary_root=observed_root,
            common_git_dir=common_dir,
        )

    # A conventional linked worktree's common Git directory is the ``.git``
    # directory of the primary checkout. Validate that candidate with a
    # second identity probe instead of trusting an unchecked path string.
    primary_candidate = common_dir.parent
    try:
        primary_identity = _verified_primary_identity(primary_candidate, common_dir)
    except ProjectSettingsPathError:
        primary_identity = None
    if primary_identity is None:
        # With an external/separate Git directory, ``common_dir.parent`` is
        # metadata, not the checkout. Use only Git's explicit bounded hint and
        # validate it against the same exact common-directory identity.
        configured_candidate = _configured_primary_root(checkout_root, common_dir)
        if configured_candidate is None:
            raise ProjectSettingsPathError("Git primary project is unavailable")
        primary_identity = _verified_primary_identity(
            configured_candidate,
            common_dir,
        )
        if primary_identity is None:
            raise ProjectSettingsPathError("Git primary project identity is ambiguous")
    primary_root, _, _ = primary_identity
    return ProjectIdentity(
        checkout_root=observed_root,
        primary_root=primary_root,
        common_git_dir=common_dir,
    )


def resolve_primary_project_root(project_root: Path) -> Path:
    """Return the primary Git checkout root, or the direct root for plain dirs."""
    return resolve_project_identity(project_root).primary_root


def _default_snapshot(defaults: ProjectSettings) -> ProjectSettingsSnapshot:
    return ProjectSettingsSnapshot(
        auto_consume_plans=defaults.auto_consume_plans,
        max_concurrent_implementations=defaults.max_concurrent_implementations,
        revision=EMPTY_PROJECT_SETTINGS_REVISION,
        persisted=False,
        source="defaults",
    )


def _read_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectSettingsPathError("project settings file disappeared") from exc
    except OSError as exc:
        raise ProjectSettingsPathError("project settings file is unavailable") from exc
    if path.is_symlink():
        raise ProjectSettingsPathError("project settings file must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ProjectSettingsPathError("project settings file must be a regular file")
    if metadata.st_size > MAX_PROJECT_SETTINGS_BYTES:
        raise ProjectSettingsCorruptionError("project settings exceed the size limit")
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ProjectSettingsPathError(
                "project settings file must not be a symlink"
            ) from exc
        raise ProjectSettingsPathError("project settings file is unreadable") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            payload = handle.read(MAX_PROJECT_SETTINGS_BYTES + 1)
    except OSError as exc:
        raise ProjectSettingsPathError("project settings file is unreadable") from exc
    if len(payload) > MAX_PROJECT_SETTINGS_BYTES:
        raise ProjectSettingsCorruptionError("project settings exceed the size limit")
    return payload


def _validate_settings_directory(
    primary_root: Path,
    *,
    create: bool,
) -> Path | None:
    directory = primary_root / ".aflow"
    if directory.is_symlink():
        raise ProjectSettingsPathError("project settings directory must not be a symlink")
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProjectSettingsPathError(
                "project settings directory is unavailable"
            ) from exc
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise ProjectSettingsPathError(
                "project settings directory is unavailable"
            ) from exc
    except OSError as exc:
        raise ProjectSettingsPathError("project settings directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
        raise ProjectSettingsPathError("project settings path must be a directory")
    try:
        resolved = directory.resolve(strict=True)
        resolved.relative_to(primary_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectSettingsPathError("project settings path escapes the project") from exc
    return directory


@contextmanager
def _settings_lock(directory: Path, *, create: bool) -> Iterator[None]:
    path = directory / PROJECT_SETTINGS_LOCK_FILENAME
    flags = os.O_RDWR | _NOFOLLOW | (os.O_CREAT if create else 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError as exc:
        raise ProjectSettingsPathError("project settings lock is unavailable") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ProjectSettingsPathError(
                "project settings lock must not be a symlink"
            ) from exc
        raise ProjectSettingsPathError("project settings lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ProjectSettingsPathError("project settings lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ProjectSettingsPathError("project settings file must not be a symlink")
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
    except OSError as exc:
        raise ProjectSettingsPathError("project settings write is unavailable") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError as exc:
            raise ProjectSettingsPathError("project settings directory is unavailable") from exc
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise ProjectSettingsPathError("project settings write failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class ProjectSettingsService:
    """Read and compare-and-swap the scheduling settings for one project."""

    def __init__(
        self,
        project_root: Path,
        *,
        defaults: ProjectSettings | None = None,
    ) -> None:
        self._identity = resolve_project_identity(project_root)
        self._defaults = defaults or ProjectSettings()

    @property
    def checkout_root(self) -> Path:
        return self._identity.checkout_root

    @property
    def primary_root(self) -> Path:
        return self._identity.primary_root

    @property
    def settings_path(self) -> Path:
        return self.primary_root / ".aflow" / PROJECT_SETTINGS_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.primary_root / ".aflow" / PROJECT_SETTINGS_LOCK_FILENAME

    @property
    def defaults(self) -> ProjectSettings:
        return self._defaults

    def read(self) -> ProjectSettingsSnapshot:
        """Return settings without creating the project settings artifact."""
        directory = _validate_settings_directory(self.primary_root, create=False)
        if directory is None:
            return _default_snapshot(self._defaults)
        lock = directory / PROJECT_SETTINGS_LOCK_FILENAME
        if lock.is_symlink():
            raise ProjectSettingsPathError("project settings lock must not be a symlink")
        try:
            lock.lstat()
        except FileNotFoundError:
            return self._read_snapshot(directory)
        except OSError as exc:
            raise ProjectSettingsPathError("project settings lock is unavailable") from exc
        with _settings_lock(directory, create=False):
            return self._read_snapshot(directory)

    def save(
        self,
        settings: ProjectSettings,
        *,
        expected_revision: str,
    ) -> ProjectSettingsSnapshot:
        """Save both fields after checking the exact expected revision."""
        return self.update(settings, expected_revision=expected_revision)

    def update(
        self,
        settings: ProjectSettings | Mapping[str, object] | None = None,
        *,
        expected_revision: str,
        auto_consume_plans: object = _UNSET,
        max_concurrent_implementations: object = _UNSET,
    ) -> ProjectSettingsSnapshot:
        """CAS-update the full settings or selected individual fields."""
        _validate_revision(expected_revision)
        if settings is not None and (
            auto_consume_plans is not _UNSET
            or max_concurrent_implementations is not _UNSET
        ):
            raise ProjectSettingsValidationError(
                "settings and individual fields cannot be combined"
            )
        candidate = None if settings is None else _coerce_settings(settings)
        if candidate is None:
            if (
                auto_consume_plans is _UNSET
                and max_concurrent_implementations is _UNSET
            ):
                raise ProjectSettingsValidationError(
                    "an updated project setting is required"
                )
            if auto_consume_plans is not _UNSET and type(auto_consume_plans) is not bool:
                raise ProjectSettingsValidationError(
                    "auto_consume_plans must be a boolean"
                )
            if max_concurrent_implementations is not _UNSET and (
                type(max_concurrent_implementations) is not int
                or not 1 <= max_concurrent_implementations <= MAX_CONCURRENT_IMPLEMENTATIONS
            ):
                raise ProjectSettingsValidationError(
                    "max_concurrent_implementations must be a positive bounded integer"
                )

        directory = _validate_settings_directory(self.primary_root, create=True)
        assert directory is not None
        with _settings_lock(directory, create=True):
            current = self._read_snapshot(directory)
            if current.revision != expected_revision:
                raise ProjectSettingsRevisionConflict(current.revision)
            if candidate is None:
                candidate = ProjectSettings(
                    auto_consume_plans=(
                        current.auto_consume_plans
                        if auto_consume_plans is _UNSET
                        else auto_consume_plans  # type: ignore[arg-type]
                    ),
                    max_concurrent_implementations=(
                        current.max_concurrent_implementations
                        if max_concurrent_implementations is _UNSET
                        else max_concurrent_implementations  # type: ignore[arg-type]
                    ),
                )
            payload = _canonical_bytes(candidate)
            _atomic_write(self.settings_path, payload)
            return ProjectSettingsSnapshot(
                auto_consume_plans=candidate.auto_consume_plans,
                max_concurrent_implementations=candidate.max_concurrent_implementations,
                revision=_revision(payload),
                persisted=True,
                source="file",
            )

    def _read_snapshot(self, directory: Path) -> ProjectSettingsSnapshot:
        path = directory / PROJECT_SETTINGS_FILENAME
        try:
            path.lstat()
        except FileNotFoundError:
            return _default_snapshot(self._defaults)
        except OSError as exc:
            raise ProjectSettingsPathError("project settings file is unavailable") from exc
        payload = _read_regular_file(path)
        settings = _decode_existing(payload)
        return ProjectSettingsSnapshot(
            auto_consume_plans=settings.auto_consume_plans,
            max_concurrent_implementations=settings.max_concurrent_implementations,
            revision=_revision(payload),
            persisted=True,
            source="file",
        )


ProjectSettingsStore = ProjectSettingsService


def _coerce_settings(value: ProjectSettings | Mapping[str, object]) -> ProjectSettings:
    if isinstance(value, ProjectSettings):
        return value
    if not isinstance(value, Mapping):
        raise ProjectSettingsValidationError("settings must be a typed settings value")
    payload = dict(value)
    payload.setdefault("schema_version", PROJECT_SETTINGS_SCHEMA_VERSION)
    try:
        return ProjectSettings.from_mapping(payload)
    except ProjectSettingsValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProjectSettingsValidationError("settings are invalid") from exc


def read_project_settings(
    project_root: Path,
    *,
    defaults: ProjectSettings | None = None,
) -> ProjectSettingsSnapshot:
    """Convenience read for CLI callers that do not need a retained service."""
    return ProjectSettingsService(project_root, defaults=defaults).read()

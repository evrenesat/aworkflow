"""Safe project creation, registration, and non-destructive unregister."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from typing import Literal

from aflow.config import (
    ConfigError,
    project_configuration_state,
    validate_starter_main_branch,
)

from .control_plane_service import ControlPlaneService
from .project_ids import allocate_project_id, default_display_name
from .project_registry import ProjectRegistry, ProjectRegistryError


_GIT_TIMEOUT_SECONDS = 15.0
_INITIAL_COMMIT_MESSAGE = "Initialize project"

READINESS_READY = "ready"
READINESS_CONFIGURATION_REQUIRED = "configuration_required"
READINESS_BLOCKED = "blocked"


def project_readiness(info_current_path: Path, *, is_git_root: bool = True) -> str:
    """Classify one project's readiness through the global engine classifier.

    All projects share the global workflow pair, so the per-project path is
    only consulted for the Git-root check; configuration readiness comes from
    the global configuration itself.
    """
    if not is_git_root:
        return READINESS_BLOCKED
    return project_configuration_state()


class ProjectServiceError(RuntimeError):
    """A bounded, actionable project create/register/unregister failure."""


@dataclass(frozen=True)
class ProjectRequest:
    """One validated create/register request with typed fields only.

    Project-level starter configuration fields were removed: registration
    never writes project-local workflow configuration because every project
    uses the shared global configuration.
    """

    mode: Literal["create", "register"]
    relative_path: str
    display_name: str | None
    main_branch: str
    initialize_git: bool


def _validated_relative_root(registry: ProjectRegistry, raw_path: str) -> PurePosixPath:
    """Accept one canonical relative directory path beneath the managed root."""
    if (
        not isinstance(raw_path, str)
        or not raw_path.strip()
        or len(raw_path) > 256
        or any(marker in raw_path for marker in ("\\", "\x00", "\n", "\r"))
    ):
        raise ProjectServiceError("path must be a relative directory path")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or str(relative) != raw_path
    ):
        raise ProjectServiceError("path must be a normalized relative directory path")
    managed_root = registry.managed_root
    candidate = managed_root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ProjectServiceError("path must not contain symlink components")
    return relative


def _validated_branch(branch: str) -> str:
    try:
        return validate_starter_main_branch(branch)
    except ConfigError as exc:
        raise ProjectServiceError(str(exc)) from exc


def _run_git(argv: tuple[str, ...], *, timeout: float = _GIT_TIMEOUT_SECONDS) -> str:
    """Run fixed Git argv with a bounded timeout and return trimmed stdout."""
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ProjectServiceError("project Git bootstrap step timed out or failed") from exc
    if completed.returncode != 0:
        raise ProjectServiceError("project Git bootstrap step failed")
    return completed.stdout.strip()


def _is_git_root(target: Path) -> bool:
    git_entry = target / ".git"
    if git_entry.is_symlink() or not git_entry.exists():
        return False
    try:
        completed = subprocess.run(
            ("git", "-C", str(target), "rev-parse", "--show-toplevel"),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if completed.returncode != 0:
        return False
    return Path(completed.stdout.strip()) == target


def _has_commit_head(target: Path) -> bool:
    """Prove the repository HEAD resolves to a real commit (not unborn)."""
    try:
        completed = subprocess.run(
            ("git", "-C", str(target), "rev-parse", "--verify", "--quiet", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0


def _bootstrap_repository(target: Path, branch: str) -> None:
    """Initialize a local Git repository with one minimal verified commit."""
    _run_git(("git", "init", "-q", "-b", branch, str(target)))
    (target / "README.md").write_text("# Project\n\nManaged by aflow.\n", encoding="utf-8")
    (target / ".gitignore").write_text(".aflow/\n", encoding="utf-8")
    for key, value in (
        ("user.name", "aflow"),
        ("user.email", "aflow@localhost"),
    ):
        probe = subprocess.run(
            ("git", "-C", str(target), "config", "--get", key),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            _run_git(("git", "-C", str(target), "config", key, value))
    _run_git(("git", "-C", str(target), "add", "README.md", ".gitignore"))
    _run_git((
        "git", "-C", str(target), "commit", "-q",
        "-m", _INITIAL_COMMIT_MESSAGE,
    ))
    observed_branch = _run_git((
        "git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD",
    ))
    if observed_branch != branch:
        raise ProjectServiceError("project initial branch could not be verified")
    status = _run_git(("git", "-C", str(target), "status", "--porcelain"))
    if status:
        raise ProjectServiceError("project initial commit left a dirty worktree")


def _discard_created(entries: list[Path], dirs: list[Path]) -> None:
    """Remove only entries and directories this request created."""
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    for directory in reversed(dirs):
        try:
            directory.rmdir()
        except OSError:
            pass


class ProjectService:
    """Create/register projects against the exact registry without shell use."""

    def __init__(
        self,
        registry: ProjectRegistry,
        control_plane: ControlPlaneService | None = None,
    ) -> None:
        self._registry = registry
        self._control_plane = control_plane

    def readiness(self, project_id: str) -> str:
        """Classify configuration readiness from the one global workflow pair.

        Every project uses the global configuration, so a registered project
        is ready exactly when the global pair is ready; local project
        configuration files are never consulted.
        """
        try:
            self._registry.resolve(project_id)
        except ProjectRegistryError:
            return READINESS_BLOCKED
        return project_configuration_state()

    def unregister(self, project_id: str) -> bool:
        """Remove only the registry record, after proving units inactive."""
        if self._control_plane is None:
            raise ProjectServiceError("project control plane is unavailable")
        if self._registry.get(project_id) is None:
            return False
        return self._control_plane.unregister(project_id)

    def create_or_register(self, request: ProjectRequest) -> dict[str, object]:
        """Create or register one project and return its bounded description.

        Registration never writes project-local workflow configuration: new
        projects immediately use the shared global configuration.
        """
        relative = _validated_relative_root(self._registry, request.relative_path)
        branch = _validated_branch(request.main_branch)
        self._reject_conflicts(relative)
        # Registry IDs are internal labels: an unsafe or collided basename gets
        # a deterministic safe ID instead of forcing a directory rename.
        occupied_ids = {record.id for record in self._registry.list_records()}
        project_id = allocate_project_id(str(relative), occupied_ids)
        if project_id is None:
            raise ProjectServiceError("project id is already registered")
        display_name = self._validated_display_name(
            request.display_name, default_display_name(str(relative))
        )
        target = self._registry.managed_root.joinpath(*relative.parts)
        if request.mode == "create":
            return self._create(
                target,
                relative,
                project_id=project_id,
                display_name=display_name,
                branch=branch,
                request=request,
            )
        return self._register(
            target,
            relative,
            project_id=project_id,
            display_name=display_name,
            branch=branch,
            request=request,
        )

    @staticmethod
    def _validated_display_name(raw: str | None, fallback: str) -> str:
        """Apply the registry display-name rules before any mutation."""
        if raw is None:
            return fallback
        if (
            not isinstance(raw, str)
            or not raw.strip()
            or len(raw) > 200
            or any(marker in raw for marker in ("\x00", "\n", "\r"))
        ):
            raise ProjectServiceError(
                "display_name must be printable text of at most 200 characters"
            )
        return raw

    def _reject_conflicts(self, relative: PurePosixPath) -> None:
        candidate = self._registry.managed_root.joinpath(*relative.parts)
        for record in self._registry.list_records():
            declared = self._registry.declared_root(record.id)
            if declared == candidate:
                raise ProjectServiceError("project root is already registered")
            if candidate in declared.parents or declared in candidate.parents:
                raise ProjectServiceError(
                    "nested or overlapping project roots are not allowed"
                )

    def _create(
        self,
        target: Path,
        relative: PurePosixPath,
        *,
        project_id: str,
        display_name: str,
        branch: str,
        request: ProjectRequest,
    ) -> dict[str, object]:
        if target.exists() or target.is_symlink():
            raise ProjectServiceError("project target path already exists")
        parent = target.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ProjectServiceError(
                "project parent directory must already exist beneath the managed root"
            )
        temporary = parent / f".aflow-create-{project_id}-{os.urandom(8).hex()}"
        temporary.mkdir(parents=False)
        renamed = False
        try:
            _bootstrap_repository(temporary, branch)
            if target.exists() or target.is_symlink():
                raise ProjectServiceError("project target path already exists")
            os.replace(temporary, target)
            renamed = True
            record = self._registry.register(project_id, display_name, str(relative))
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if renamed:
                shutil.rmtree(target, ignore_errors=True)
            raise
        return self._describe(record)

    def _register(
        self,
        target: Path,
        relative: PurePosixPath,
        *,
        project_id: str,
        display_name: str,
        branch: str,
        request: ProjectRequest,
    ) -> dict[str, object]:
        if not target.exists() or target.is_symlink() or not target.is_dir():
            raise ProjectServiceError("project root must be an existing directory")
        created_entries: list[Path] = []
        created_dirs: list[Path] = []
        try:
            if _is_git_root(target):
                # Existing repositories and their history are never modified;
                # an unborn HEAD is not a registered project yet.
                if not _has_commit_head(target):
                    raise ProjectServiceError(
                        "project root must have a valid Git commit HEAD"
                    )
            elif request.initialize_git:
                if any(target.iterdir()):
                    raise ProjectServiceError(
                        "initialize_git requires an empty directory"
                    )
                # The directory was empty, so every bootstrap entry is ours.
                created_entries.extend((
                    target / ".git",
                    target / "README.md",
                    target / ".gitignore",
                ))
                _bootstrap_repository(target, branch)
            else:
                raise ProjectServiceError(
                    "project root must be a Git repository or initialize_git must be set"
                )
            record = self._registry.register(project_id, display_name, str(relative))
        except Exception:
            _discard_created(created_entries, created_dirs)
            raise
        return self._describe(record)

    def _describe(self, record: object) -> dict[str, object]:
        return {
            "id": record.id,
            "display_name": record.display_name,
            "relative_root": record.relative_root,
            "root": str(self._registry.declared_root(record.id)),
            "created_at": record.created_at.isoformat(),
            "readiness": self.readiness(record.id),
        }

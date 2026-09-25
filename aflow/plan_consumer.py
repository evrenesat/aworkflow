"""Server-owned automatic admission of stable in-progress plans.

The consumer is deliberately a caller of the managed control plane.  Its
cross-process lock elects a scanner, while project admission remains the sole
authority for plan claims and implementation capacity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
from threading import Event, Lock, Thread
from typing import Any

from watchfiles import watch

from aflow.config import ConfigError, load_workflow_config
from aflow.live_config import load_live_config
from aflow.plan import GitTrackingMetadataError, PlanParseError, load_plan, parse_git_tracking_metadata
from aflow.plan_backups import BackupProvenanceError, ensure_plan_identity, plan_identity_for_path
from aflow.project_admission import ProjectAdmission, ProjectAdmissionError
from aflow.project_settings import ProjectSettingsError, ProjectSettingsService


_LOGGER = logging.getLogger(__name__)
_MAX_PLAN_BYTES = 256 * 1024
_INTERVAL_SECONDS = 5.0
_LOCK_NAME = "plan-consumer.lock"
_REASON_CODES = frozenset({
    "capacity", "dependency", "claimed", "configuration", "provider",
    "startup_input", "unsafe_plan", "unstable", "validation", "unavailable",
})


class PlanConsumer:
    """Elect and scan one owner per registered primary project."""

    def __init__(
        self,
        *,
        projects: Callable[[], Iterable[tuple[str, Path]]],
        launch: Callable[[str, str, str, str, str, str | None, str], Any],
        classify_invalid: Callable[[str, str, str], None],
        config_path: Path,
        interval_seconds: float = _INTERVAL_SECONDS,
    ) -> None:
        if not 0 < interval_seconds <= _INTERVAL_SECONDS:
            raise ValueError("consumer interval must be within five seconds")
        self._projects = projects
        self._launch = launch
        self._classify_invalid = classify_invalid
        self._config_path = Path(config_path)
        self._interval = interval_seconds
        self._wake = Event()
        self._stop = Event()
        self._root_locks_guard = Lock()
        self._root_locks: dict[Path, Lock] = {}
        self._thread: Thread | None = None
        self._workers: dict[Path, tuple[str, Event, Event, Thread]] = {}
        self._owners: dict[Path, int] = {}
        self._observed: dict[tuple[Path, str], tuple[int, int, int, int, str]] = {}
        self._held: dict[tuple[Path, str], str] = {}
        self._reasons: dict[tuple[Path, str], str] = {}

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("plan consumer is already started")
        self._stop.clear()
        self._thread = Thread(target=self._run, name="aflow-plan-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for _, stopped, waking, _ in tuple(self._workers.values()):
            stopped.set()
            waking.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None
        for _, _, _, worker in tuple(self._workers.values()):
            worker.join(timeout=0.5)
        self._workers.clear()
        for root in tuple(self._owners):
            lock = self._root_lock(root)
            if lock.acquire(blocking=False):
                try:
                    if root in self._owners:
                        self._release(root)
                finally:
                    lock.release()

    def wake(self, project_id: str | None = None) -> None:
        """Observe a mutation promptly; a periodic scan covers other processes."""
        self._wake.set()
        for worker_project, _, waking, _ in tuple(self._workers.values()):
            if project_id is None or worker_project == project_id:
                waking.set()

    def reason(self, root: Path, name: str) -> str | None:
        """Return a bounded current-process queue reason for later projections."""
        with self._root_lock(Path(root)):
            return self._reasons.get((Path(root), name))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_workers()
            except Exception:
                _LOGGER.exception("automatic plan registry scan failed")
            self._wake.wait(self._interval)
            self._wake.clear()

    def _root_lock(self, root: Path) -> Lock:
        with self._root_locks_guard:
            return self._root_locks.setdefault(root, Lock())

    def _refresh_workers(self) -> None:
        projects = tuple(self._projects())
        current: set[Path] = set()
        for project_id, declared_root in projects:
            if self._stop.is_set():
                break
            try:
                root = ProjectSettingsService(declared_root).primary_root
                if root != Path(declared_root):
                    raise ProjectSettingsError("registered root is not primary")
            except Exception:
                _LOGGER.exception("automatic plan project identity failed for %s", project_id)
                continue
            current.add(root)
            existing = self._workers.get(root)
            if existing is not None:
                if existing[3].is_alive():
                    continue
                self._workers.pop(root, None)
            stopped, waking = Event(), Event()
            worker = Thread(
                target=self._run_project,
                args=(project_id, root, stopped, waking),
                name=f"aflow-plan-consumer:{project_id}", daemon=True,
            )
            self._workers[root] = (project_id, stopped, waking, worker)
            worker.start()
        for root, (_, stopped, waking, worker) in tuple(self._workers.items()):
            if root not in current:
                stopped.set()
                waking.set()
                if not worker.is_alive():
                    self._workers.pop(root, None)

    def _run_project(
        self, project_id: str, root: Path, stopped: Event, waking: Event,
    ) -> None:
        watcher: Thread | None = None
        try:
            while not self._stop.is_set() and not stopped.is_set():
                try:
                    self._scan_registered(project_id, root)
                    if watcher is None and root in self._owners:
                        watcher = Thread(
                            target=self._watch_project,
                            args=(root, stopped, waking),
                            name=f"aflow-plan-watch:{project_id}", daemon=True,
                        )
                        watcher.start()
                except Exception:
                    _LOGGER.exception("automatic plan project scan failed for %s", project_id)
                waking.wait(self._interval)
                waking.clear()
        finally:
            stopped.set()
            if watcher is not None:
                watcher.join(timeout=2)
            with self._root_lock(root):
                if root in self._owners:
                    self._release(root)

    @staticmethod
    def _watch_project(root: Path, stopped: Event, waking: Event) -> None:
        """Wake on plan writes and controller completion artifacts."""
        watched = [root / ".aflow"]
        plans = root / "plans"
        if plans.is_dir() and not plans.is_symlink():
            watched.append(plans)
        plan_changes = plans / "in-progress"
        run_changes = root / ".aflow" / "runs"
        try:
            for changes in watch(*watched, stop_event=stopped, debounce=200):
                if any(
                    Path(path).is_relative_to(plan_changes)
                    or Path(path).is_relative_to(run_changes)
                    for _, path in changes
                ):
                    waking.set()
        except Exception:
            if not stopped.is_set():
                _LOGGER.exception("automatic plan completion watch failed")

    def _scan_registered(self, project_id: str, declared_root: Path) -> Path:
        settings = ProjectSettingsService(declared_root)
        root = settings.primary_root
        if root != Path(declared_root):
            raise ProjectSettingsError("registered root is not primary")
        with self._root_lock(root):
            if self._own(root):
                self._scan_project(project_id, root, settings)
        return root

    def scan_once(self) -> None:
        """Perform one bounded pass, isolating errors between projects."""
        try:
            projects = tuple(self._projects())
        except Exception:
            _LOGGER.exception("automatic plan registry read failed")
            return
        current: set[Path] = set()
        for project_id, declared_root in projects:
            try:
                current.add(self._scan_registered(project_id, declared_root))
            except Exception:
                _LOGGER.exception("automatic plan project scan failed for %s", project_id)
        for root in tuple(self._owners):
            if root not in current and root not in self._workers:
                with self._root_lock(root):
                    if root in self._owners:
                        self._release(root)

    def _own(self, root: Path) -> bool:
        if root in self._owners:
            return True
        directory = root / ".aflow"
        if directory.is_symlink():
            raise OSError("automatic plan state directory is unsafe")
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir() or directory.is_symlink():
            raise OSError("automatic plan state directory is unsafe")
        descriptor = os.open(
            directory / _LOCK_NAME,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("automatic plan lock is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self._owners[root] = descriptor
            descriptor = -1
            return True
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _release(self, root: Path) -> None:
        descriptor = self._owners.pop(root)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        for key in tuple(self._observed):
            if key[0] == root:
                self._observed.pop(key, None)
        for key in tuple(self._held):
            if key[0] == root:
                self._held.pop(key, None)
        for key in tuple(self._reasons):
            if key[0] == root:
                self._reasons.pop(key, None)

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int, int, int, str]:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_PLAN_BYTES:
                raise OSError("automatic plan entry is unsafe")
            content = stream.read(_MAX_PLAN_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (
            len(content) > _MAX_PLAN_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise OSError("automatic plan changed during scan")
        return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, hashlib.sha256(content).hexdigest())

    @staticmethod
    def _selection(path: Path) -> tuple[str, str | None]:
        config = load_live_config(path, loader=load_workflow_config).workflow_config
        name = config.aflow.default_workflow
        if name is None or name not in config.workflows:
            raise ConfigError("global default workflow is unavailable")
        workflow = config.workflows[name]
        if (
            tuple(workflow.setup or ()) != ("worktree", "branch")
            or tuple(workflow.teardown or ()) != ("merge", "rm_worktree")
            or workflow.main_branch is None
            or not config.aflow.worktree_root
        ):
            raise ConfigError("automatic workflow requires isolated worktree delivery")
        if workflow.team is not None and workflow.team not in config.teams:
            raise ConfigError("automatic workflow team is unavailable")
        return name, workflow.team

    def _scan_project(self, project_id: str, root: Path, settings: ProjectSettingsService) -> None:
        if not settings.read().auto_consume_plans:
            return
        plans = root / "plans"
        directory = plans / "in-progress"
        if (
            plans.is_symlink()
            or directory.is_symlink()
            or (plans.exists() and not plans.is_dir())
            or (directory.exists() and not directory.is_dir())
        ):
            raise OSError("automatic plan directory is unsafe")
        if not directory.exists():
            return
        seen: set[tuple[Path, str]] = set()
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if self._stop.is_set():
                break
            if path.suffix != ".md":
                continue
            key = (root, path.name)
            seen.add(key)
            if key in self._held:
                owner = plan_identity_for_path(root, path)
                if owner is None or owner == self._held[key]:
                    continue
                self._held.pop(key, None)
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.md", path.name) is None:
                self._set_reason(key, "unsafe_plan")
                continue
            try:
                current = self._fingerprint(path)
            except OSError:
                self._observed.pop(key, None)
                self._set_reason(key, "unsafe_plan")
                continue
            previous = self._observed.get(key)
            self._observed[key] = current
            if previous != current:
                self._set_reason(key, "unstable")
                continue
            revision = current[-1]
            try:
                parsed = load_plan(path)
                parse_git_tracking_metadata(path.read_text(encoding="utf-8"))
                if self._fingerprint(path) != current:
                    self._set_reason(key, "unstable")
                    continue
                if parsed.snapshot.is_complete:
                    self._held[key] = plan_identity_for_path(root, path) or "complete"
                    continue
            except (PlanParseError, GitTrackingMetadataError, UnicodeError):
                try:
                    self._classify_invalid(project_id, path.name, revision)
                except Exception:
                    self._set_reason(key, "validation")
                else:
                    self._observed.pop(key, None)
                    self._reasons.pop(key, None)
                continue
            except OSError:
                self._set_reason(key, "unstable")
                continue
            try:
                workflow_name, team = self._selection(self._config_path)
            except (ConfigError, OSError, ValueError):
                self._set_reason(key, "configuration")
                continue
            try:
                identity = ensure_plan_identity(root, path)
            except (BackupProvenanceError, OSError):
                self._set_reason(key, "unavailable")
                continue
            try:
                capacity = ProjectAdmission(root).snapshot()
            except ProjectAdmissionError:
                self._set_reason(key, "unavailable")
                continue
            if capacity.occupied_count >= capacity.limit:
                self._set_reason(key, "capacity")
                continue
            request_key = "automatic:" + hashlib.sha256(
                f"{root}\0{identity}".encode("utf-8")
            ).hexdigest()
            try:
                result = self._launch(
                    project_id, f"plans/in-progress/{path.name}", request_key,
                    revision, workflow_name, team, identity,
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code == "project_capacity_reached":
                    reason = "capacity"
                elif code in {"project_plan_dependency_blocked"}:
                    reason = "dependency"
                elif code in {"project_plan_claim_conflict"}:
                    reason = "claimed"
                elif code == "project_automatic_disabled":
                    reason = "configuration"
                elif code == "automatic_default_unavailable":
                    reason = "configuration"
                elif code == "project_automatic_plan_changed":
                    reason = "unstable"
                elif code == "plan_validation_failed":
                    try:
                        self._classify_invalid(project_id, path.name, revision)
                    except Exception:
                        reason = "validation"
                    else:
                        self._observed.pop(key, None)
                        self._reasons.pop(key, None)
                        continue
                else:
                    reason = "provider"
                self._set_reason(key, reason)
                continue
            self._held[key] = identity
            if getattr(result, "question_id", None) is not None:
                self._set_reason(key, "startup_input")
            else:
                self._reasons.pop(key, None)
        for key in tuple(self._observed):
            if key[0] == root and key not in seen:
                self._observed.pop(key, None)
                self._held.pop(key, None)
                self._reasons.pop(key, None)

    def _set_reason(self, key: tuple[Path, str], reason: str) -> None:
        assert reason in _REASON_CODES
        self._reasons[key] = reason

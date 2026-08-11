"""Local control-plane service and lifecycle helpers for ``aflow daemon``.

The ``aflow daemon`` command runs the daemon-owned control plane directly in
a terminal for one project (the repository root), without the FastAPI web
app, the production ``aflowd`` deployment, or systemd.  The MCP registry from
``aflow.mcp_control_plane`` is served over stdio or HTTP; workflow units run
as local subprocesses through ``SubprocessUnitManager``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Mapping

from aflow.config import bootstrap_config, load_workflow_config
from aflow.control_plane import (
    ControlWriteResult,
    PlanRecord,
    ProjectRecord,
    RunControlRequest,
    RunEvent,
    RunPage,
    RunStatus,
    StartRunResult,
    StartupQuestionRecord,
)
from aflow.control_plane.units import SubprocessUnitManager
from aflow.api.models import StartupRequest
from aflow.daemon import AflowDaemon, DaemonConfig, DaemonError

LOCAL_SCOPE_PREFIX = "bearer"


def daemon_release_identity() -> str:
    try:
        return importlib.metadata.version("aworkflow")
    except Exception:
        return "dev"


def _resolve_aflow_executable() -> Path:
    """Resolve an executable aflow entry point for child/daemon re-exec."""
    candidate = Path(sys.argv[0]).resolve()
    if os.access(candidate, os.X_OK):
        return candidate
    discovered = shutil.which("aflow")
    if discovered:
        return Path(discovered).resolve()
    return candidate


def resolve_daemon_config(
    *,
    repo_root: Path,
    config_path: Path | None,
    environment_file: Path | None,
    poll_interval_seconds: float,
    stop_timeout_seconds: float,
) -> DaemonConfig:
    """Build a validated ``DaemonConfig`` for a local ``aflow daemon`` run.

    Missing user config is bootstrapped from the packaged defaults (the same
    first-run behavior as ``aflow run``).  Without an explicit environment
    file, an empty engine-owned ``.aflow/daemon.env`` is used so the daemon
    contract holds without bearer material.
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise DaemonError(f"repository root does not exist: {root}")
    resolved_config = Path(config_path).resolve() if config_path is not None else None
    if resolved_config is None:
        resolved_config = Path.home() / ".config" / "aflow" / "aflow.toml"
    if not resolved_config.is_file():
        resolved_config = bootstrap_config(resolved_config)
    if environment_file is None:
        daemon_env = root / ".aflow" / "daemon.env"
        if not daemon_env.is_file():
            daemon_env.parent.mkdir(parents=True, exist_ok=True)
            daemon_env.write_text("# aflow daemon environment file (empty by default)\n", encoding="utf-8")
            try:
                os.chmod(daemon_env, 0o600)
            except OSError:
                pass
        environment_file = daemon_env
    executable = _resolve_aflow_executable()
    if not os.access(executable, os.X_OK):
        raise DaemonError(f"aflow executable is not executable: {executable}")
    return DaemonConfig(
        repo_root=root,
        config_path=resolved_config,
        aflow_executable=executable,
        environment_file=Path(environment_file).resolve(),
        release_identity=daemon_release_identity(),
        environment={},
        stop_timeout_seconds=stop_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    ).validated()


class LocalControlPlaneService:
    """Single-project control-plane routing for ``aflow daemon``.

    Mirrors the routing the web app's ``ControlPlaneService`` performs, but
    for exactly one locally owned project, so the shared MCP registry works
    unchanged.  All mutation authority stays in the daemon services.
    """

    def __init__(self, daemon: AflowDaemon, *, project_id: str, repo_root: Path) -> None:
        self._daemon = daemon
        self._project_id = project_id
        self._repo_root = Path(repo_root).resolve()

    @staticmethod
    def _caller_scope(transport: str) -> str:
        if transport not in {"rest", "mcp"}:
            raise ValueError("unsupported control-plane transport")
        return f"{LOCAL_SCOPE_PREFIX}:{transport}"

    def _plan_path(self, requested: str) -> Path:
        path = Path(requested)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise DaemonError("plan path must be a relative contained file")
        candidate = self._repo_root / path
        if candidate.is_symlink() or not candidate.is_file():
            raise DaemonError("plan path must name an existing regular file")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._repo_root)
        except (OSError, ValueError) as exc:
            raise DaemonError("plan path is outside the allowed project") from exc
        return resolved

    def projects(self) -> tuple[ProjectRecord, ...]:
        return (ProjectRecord(project_id=self._project_id, root=str(self._repo_root)),)

    def capabilities(self, project_id: str):
        self._assert_project(project_id)
        return self._daemon.application.capabilities.get()

    def list_plans(
        self, project_id: str, *, limit: int, cursor: str | None
    ) -> tuple[PlanRecord, ...]:
        self._assert_project(project_id)
        return self._daemon.application.repository.list_plans(limit=limit, cursor=cursor)

    def list_runs(
        self, project_id: str, *, limit: int, cursor: str | None
    ) -> RunPage:
        self._assert_project(project_id)
        page = self._daemon.application.repository.list_runs(limit=limit, cursor=cursor)
        return RunPage(
            runs=tuple(self._daemon.service.run_status(run.run_id) for run in page.runs),
            next_cursor=page.next_cursor,
        )

    def run_status(self, project_id: str, run_id: str) -> RunStatus:
        self._assert_project(project_id)
        return self._daemon.service.run_status(run_id)

    def events(
        self,
        project_id: str,
        run_id: str,
        *,
        after_sequence: int | None,
        limit: int,
    ) -> tuple[RunEvent, ...]:
        self._assert_project(project_id)
        return self._daemon.service.poll_events(
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            authorizer=lambda _action, _status: True,
        )

    def context(
        self,
        project_id: str,
        run_id: str,
        *,
        level: str,
        full_scope: bool,
    ):
        self._assert_project(project_id)
        return self._daemon.application.context.get(
            run_id,
            level=level,  # type: ignore[arg-type]
            full_scope=full_scope,
        )

    def start_run(
        self,
        project_id: str,
        *,
        plan_path: str,
        workflow_name: str | None,
        team: str | None,
        start_step: str | None,
        max_turns: int | None,
        idempotency_key: str | None,
        caller_scope: str = "mcp",
    ) -> StartRunResult | StartupQuestionRecord:
        self._assert_project(project_id)
        request = StartupRequest(
            repo_root=self._repo_root,
            plan_path=self._plan_path(plan_path),
            config_path=self._daemon_config_path(),
            workflow_config=load_workflow_config(self._daemon_config_path()),
            workflow_name=workflow_name,
            start_step=start_step,
            max_turns=max_turns,
            team=team,
        )
        return self._daemon.service.start(
            request,
            caller_scope=self._caller_scope(caller_scope),
            idempotency_key=idempotency_key,
        )

    def answer_startup(
        self,
        project_id: str,
        question_id: str,
        answer: str | int | bool,
        *,
        idempotency_key: str | None,
        caller_scope: str = "mcp",
    ) -> StartRunResult | StartupQuestionRecord:
        self._assert_project(project_id)
        return self._daemon.service.answer_startup(
            question_id,
            answer,
            caller_scope=self._caller_scope(caller_scope),
            idempotency_key=idempotency_key,
        )

    def control(
        self,
        project_id: str,
        run_id: str,
        request: RunControlRequest,
        *,
        idempotency_key: str | None,
        caller_scope: str = "mcp",
    ) -> tuple[ControlWriteResult, RunStatus]:
        self._assert_project(project_id)
        result = self._daemon.application.controls.apply(
            run_id,
            request,
            caller_scope=self._caller_scope(caller_scope),
            idempotency_key=idempotency_key,
        )
        return result, self._daemon.application.repository.get_run_status(run_id)

    def owner_stop(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_revision: int,
        idempotency_key: str | None,
        caller_scope: str = "mcp",
    ) -> RunStatus:
        self._assert_project(project_id)
        return self._daemon.service.owner_stop(
            run_id,
            expected_revision=expected_revision,
            caller_scope=self._caller_scope(caller_scope),
            idempotency_key=idempotency_key,
        )

    def resume(
        self,
        project_id: str,
        run_id: str,
        *,
        idempotency_key: str | None,
        caller_scope: str = "mcp",
    ) -> StartRunResult:
        self._assert_project(project_id)
        return self._daemon.service.resume(
            run_id,
            caller_scope=self._caller_scope(caller_scope),
            idempotency_key=idempotency_key,
        )

    def _assert_project(self, project_id: str) -> None:
        if project_id != self._project_id:
            raise DaemonError(
                f"project '{project_id}' is not allowed; use '{self._project_id}'"
            )

    def _daemon_config_path(self) -> Path:
        return self._daemon.config_path


def default_project_id(repo_root: Path) -> str:
    name = Path(repo_root).resolve().name
    return name or "default"


def pidfile_path(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / ".aflow" / "daemon.pid"


@dataclass(frozen=True)
class DaemonPidRecord:
    pid: int
    process_birth: str
    repo_root: str


def _process_birth_identity(pid: int) -> str | None:
    """Return a stable process-birth identity, not merely a reusable PID."""
    if pid < 1:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = stat[stat.rfind(")") + 2 :].split()
        if suffix[0] == "Z":
            return None
        return f"linux-start-ticks:{suffix[19]}"
    except (OSError, IndexError):
        pass
    completed = subprocess.run(
        ("ps", "-o", "lstart=", "-p", str(pid)),
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return f"ps-lstart:{value}" if completed.returncode == 0 and value else None


def _read_pidfile(repo_root: Path) -> DaemonPidRecord | None:
    path = pidfile_path(repo_root)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DaemonError(f"unable to read daemon pidfile: {exc}") from exc
    try:
        raw = json.loads(content)
        record = DaemonPidRecord(
            pid=raw["pid"],
            process_birth=raw["process_birth"],
            repo_root=raw["repo_root"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DaemonError("daemon pidfile is malformed; leaving it untouched") from exc
    expected_root = str(Path(repo_root).resolve())
    if (
        not isinstance(record.pid, int)
        or isinstance(record.pid, bool)
        or record.pid < 1
        or not isinstance(record.process_birth, str)
        or not record.process_birth
        or record.repo_root != expected_root
    ):
        raise DaemonError("daemon pidfile identity is invalid; leaving it untouched")
    return record


def _record_is_live(record: DaemonPidRecord) -> bool:
    return _process_birth_identity(record.pid) == record.process_birth


def _record_process_state(record: DaemonPidRecord) -> str:
    actual = _process_birth_identity(record.pid)
    if actual is None:
        return "dead"
    return "matching" if actual == record.process_birth else "mismatched"


def _current_pid_record(repo_root: Path) -> DaemonPidRecord:
    process_birth = _process_birth_identity(os.getpid())
    if process_birth is None:
        raise DaemonError("cannot establish daemon process-birth identity")
    return DaemonPidRecord(
        pid=os.getpid(),
        process_birth=process_birth,
        repo_root=str(Path(repo_root).resolve()),
    )


def _claim_pidfile(repo_root: Path) -> DaemonPidRecord:
    path = pidfile_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_pidfile(repo_root)
    if existing is not None:
        state = _record_process_state(existing)
        if state == "matching":
            raise DaemonError(f"daemon already running for this repository (pid {existing.pid})")
        if state == "mismatched":
            raise DaemonError("daemon pidfile refers to a reused PID; leaving it untouched")
        path.unlink()
    record = _current_pid_record(repo_root)
    payload = (json.dumps(asdict(record), sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DaemonError("another daemon claimed the repository pidfile") from exc
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return record


def _release_pidfile(repo_root: Path, record: DaemonPidRecord) -> bool:
    try:
        current = _read_pidfile(repo_root)
    except DaemonError:
        return False
    if current != record:
        return False
    pidfile_path(repo_root).unlink(missing_ok=True)
    return True


def _owned_run_processes(daemon_pid: int, repo_root: Path) -> tuple[tuple[str, int], ...]:
    """Inspect direct daemon-worker children without treating legacy runs as owned."""
    root = str(Path(repo_root).resolve())
    owned: list[tuple[str, int]] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return ()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
            parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
            if int(parent_line.split()[1]) != daemon_pid:
                continue
            argv = (entry / "cmdline").read_bytes().decode("utf-8").split("\x00")
            worker_index = argv.index("daemon-worker")
            repo_index = argv.index("--repo-root", worker_index)
            run_index = argv.index("--run-id", worker_index)
            if argv[repo_index + 1] != root:
                continue
            run_id = argv[run_index + 1]
            owned.append((run_id, int(entry.name)))
        except (OSError, StopIteration, ValueError, IndexError):
            continue
    return tuple(sorted(owned))


def daemon_status(repo_root: Path) -> int:
    """Report verified daemon liveness and only its direct owned workers."""
    root = Path(repo_root).resolve()
    try:
        record = _read_pidfile(root)
    except DaemonError as exc:
        print(f"daemon status is ambiguous: {exc}", file=sys.stderr)
        return 2
    if record is None:
        print(f"daemon not running for {root}", file=sys.stderr)
        return 1
    state = _record_process_state(record)
    if state == "mismatched":
        print("daemon status is ambiguous: pidfile PID was reused", file=sys.stderr)
        return 2
    if state == "dead":
        print(f"daemon not running for {root}", file=sys.stderr)
        return 1
    print(f"daemon running: pid {record.pid} for {root}")
    owned = _owned_run_processes(record.pid, root)
    print(f"owned runs: {len(owned)}")
    for run_id, pid in owned:
        print(f"  {run_id} pid={pid}")
    return 0


def daemon_stop(repo_root: Path, *, stop_timeout_seconds: float) -> int:
    """Terminate only the process matching the exact persisted birth identity."""
    if not math.isfinite(stop_timeout_seconds) or stop_timeout_seconds < 0:
        print("stop timeout must be non-negative", file=sys.stderr)
        return 2
    try:
        record = _read_pidfile(repo_root)
    except DaemonError as exc:
        print(f"refusing to stop daemon: {exc}", file=sys.stderr)
        return 2
    if record is None:
        print("daemon is not running", file=sys.stderr)
        return 1
    state = _record_process_state(record)
    if state == "mismatched":
        print("refusing to stop daemon: pidfile PID was reused", file=sys.stderr)
        return 2
    if state == "dead":
        print("daemon is not running (stale pidfile removed)", file=sys.stderr)
        _release_pidfile(repo_root, record)
        return 1
    print(f"stopping daemon pid {record.pid}...")
    os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + stop_timeout_seconds
    while _record_is_live(record) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _record_is_live(record):
        print(f"daemon did not stop within {stop_timeout_seconds}s; sending SIGKILL", file=sys.stderr)
        os.kill(record.pid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 2.0
        while _record_is_live(record) and time.monotonic() < kill_deadline:
            time.sleep(0.05)
    if _record_is_live(record):
        print("daemon process identity remains live; pidfile retained", file=sys.stderr)
        return 2
    _release_pidfile(repo_root, record)
    print("daemon stopped")
    return 0


def detach_daemon(
    repo_root: Path,
    *,
    config_path: Path | None,
    environment_file: Path | None,
    mcp_port: int,
    poll_interval_seconds: float,
    stop_timeout_seconds: float,
) -> int:
    """Re-exec in a new session and wait for exact pidfile ownership."""
    try:
        existing = _read_pidfile(repo_root)
    except DaemonError as exc:
        print(f"cannot start daemon: {exc}", file=sys.stderr)
        return 2
    if existing is not None:
        state = _record_process_state(existing)
        if state == "matching":
            print(f"daemon already running: pid {existing.pid}", file=sys.stderr)
            return 1
        if state == "mismatched":
            print("cannot start daemon: pidfile PID was reused", file=sys.stderr)
            return 2
        _release_pidfile(repo_root, existing)
    child_argv = [
        str(_resolve_aflow_executable()),
        "daemon",
        "start",
        "--foreground",
        "--repo-root",
        str(Path(repo_root).resolve()),
        "--mcp-transport",
        "http",
        "--mcp-port",
        str(mcp_port),
        "--poll-interval",
        str(poll_interval_seconds),
        "--stop-timeout",
        str(stop_timeout_seconds),
    ]
    if config_path is not None:
        child_argv.extend(["--config", str(config_path)])
    if environment_file is not None:
        child_argv.extend(["--environment-file", str(environment_file)])
    child = subprocess.Popen(
        child_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    record: DaemonPidRecord | None = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        try:
            candidate = _read_pidfile(repo_root)
        except DaemonError:
            candidate = None
        if candidate is not None and candidate.pid == child.pid and _record_is_live(candidate):
            record = candidate
            break
        time.sleep(0.05)
    if record is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=1.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        print("daemon failed to claim its pidfile and remain alive", file=sys.stderr)
        return 1
    time.sleep(0.25)
    if child.poll() is not None or not _record_is_live(record):
        _release_pidfile(repo_root, record)
        print("daemon exited during readiness verification", file=sys.stderr)
        return 1
    print(f"daemon started in background: pid {child.pid} (http port {mcp_port})")
    return 0


def run_daemon_foreground(
    daemon: AflowDaemon,
    *,
    transport: str,
    mcp_port: int,
    get_service: Any,
    repo_root: Path,
) -> int:
    """Serve stdio on the main thread and drain all owned local children."""
    from aflow.mcp_control_plane import create_control_plane_mcp

    mcp_server = create_control_plane_mcp(get_service)

    transport_errors: list[BaseException] = []
    shutdown_signals: list[int] = []

    def serve_http() -> None:
        try:
            mcp_server.run("http", host="127.0.0.1", port=mcp_port)
        except BaseException as exc:
            transport_errors.append(exc)
        finally:
            daemon.request_shutdown()

    def handle_shutdown(signum: int, frame: object) -> None:
        shutdown_signals.append(signum)
        daemon.request_shutdown()
        if transport == "stdio":
            raise KeyboardInterrupt

    try:
        pid_record = _claim_pidfile(repo_root)
    except DaemonError as exc:
        print(f"cannot start daemon: {exc}", file=sys.stderr)
        return 2
    previous_term = signal.signal(signal.SIGTERM, handle_shutdown)
    previous_int = signal.signal(signal.SIGINT, handle_shutdown)
    try:
        if transport == "stdio":
            poll_thread = threading.Thread(
                target=daemon.serve_forever,
                name="aflow-daemon-poll",
                daemon=True,
            )
            poll_thread.start()
            print("Daemon ready", file=sys.stderr, flush=True)
            try:
                mcp_server.run("stdio")
            except KeyboardInterrupt:
                if not shutdown_signals:
                    raise
            except BaseException as exc:
                transport_errors.append(exc)
        else:
            mcp_thread = threading.Thread(target=serve_http, name="aflow-mcp", daemon=True)
            mcp_thread.start()
            print("Daemon ready", file=sys.stderr, flush=True)
            daemon.serve_forever()
    finally:
        daemon.shutdown()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        _release_pidfile(repo_root, pid_record)
    if transport_errors:
        print(f"MCP transport failed: {type(transport_errors[0]).__name__}", file=sys.stderr)
        return 1
    return 0

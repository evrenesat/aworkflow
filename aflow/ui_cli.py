"""The ``aflow ui`` launcher: one command that serves the AFlow web UI.

The launcher owns the UI process lifecycle (foreground or background), the
per-user ownership record under ``~/.config/aflow/ui/``, first-run setup for
the shared global configuration, and the private ``aflow ui-worker`` wrapper
dispatch that lets workflow units outlive the UI on Linux and macOS.

It intentionally uses the default global configuration directory and the
installation it runs from; unrelated shells, virtual environments, or legacy
``AFLOW_APP_*`` environment variables cannot redirect it.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import getpass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from aflow.installation import detect_installation
from aflow.ui_assets import AssetBuildError, ensure_launch_assets

UI_LOG_MAX_BYTES = 1_000_000
UI_LOG_KEEP_BYTES = 256_000
READY_WAIT_SECONDS = 20.0
_STOP_TIMEOUT_SECONDS = 15.0
_PROBE_TIMEOUT_SECONDS = 1.5


class UIError(RuntimeError):
    """An actionable launcher failure."""


# ---------------------------------------------------------------------------
# Ownership record (foreground and background share it)


def ui_state_dir() -> Path:
    return Path.home() / ".config" / "aflow" / "ui"


def _record_path() -> Path:
    return ui_state_dir() / "ui.pid.json"


def _record_lock_path() -> Path:
    return ui_state_dir() / "ui.lock"


def _ready_path() -> Path:
    return ui_state_dir() / "ready.json"


def _log_path() -> Path:
    return ui_state_dir() / "ui.log"


def process_birth_identity(pid: int) -> str | None:
    """Stable process-birth identity, portable across Linux and macOS."""
    from aflow.daemon_cli import _process_birth_identity

    return _process_birth_identity(pid)


@dataclass(frozen=True)
class UIRecord:
    pid: int
    process_birth: str
    host: str
    port: int
    started_at: str
    mode: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": 1,
                "pid": self.pid,
                "process_birth": self.process_birth,
                "host": self.host,
                "port": self.port,
                "started_at": self.started_at,
                "mode": self.mode,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def current(cls, *, host: str, port: int, mode: str) -> "UIRecord":
        birth = process_birth_identity(os.getpid())
        if birth is None:
            raise UIError("cannot establish the UI process-birth identity")
        return cls(
            pid=os.getpid(),
            process_birth=birth,
            host=host,
            port=port,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            mode=mode,
        )


def read_ui_record() -> UIRecord | None:
    path = _record_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid, birth = raw.get("pid"), raw.get("process_birth")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return None
    if not isinstance(birth, str) or not birth:
        return None
    return UIRecord(
        pid=pid,
        process_birth=birth,
        host=str(raw.get("host", "")),
        port=int(raw.get("port", 0)),
        started_at=str(raw.get("started_at", "")),
        mode=str(raw.get("mode", "")),
    )


def _record_state(record: UIRecord) -> str:
    actual = process_birth_identity(record.pid)
    if actual is None:
        return "dead"
    return "matching" if actual == record.process_birth else "mismatched"


def claim_ui_record(record: UIRecord) -> None:
    """Exclusively claim the UI ownership record, refusing duplicates."""
    directory = ui_state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    existing = read_ui_record()
    if existing is not None:
        state = _record_state(existing)
        if state == "matching" and _probe_health(existing.host, existing.port):
            raise UIError(
                f"aflow ui is already running at {_local_url(existing.host, existing.port)} (pid {existing.pid})"
            )
        if state == "matching":
            raise UIError(
                f"a pid {existing.pid} matches the recorded aflow ui identity but is not "
                f"responding on port {existing.port}; stop it with `aflow ui --stop` "
                "or remove ~/.config/aflow/ui/ui.pid.json if it is stale"
            )
        # A dead or reused PID record must not survive a new owner.
        _record_path().unlink(missing_ok=True)
    fd = os.open(_record_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(record.to_json())
        handle.flush()
        os.fsync(handle.fileno())


def release_ui_record() -> None:
    _record_path().unlink(missing_ok=True)
    _ready_path().unlink(missing_ok=True)


def write_ready(payload: dict[str, object]) -> None:
    directory = ui_state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / ".ready.json.tmp"
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, _ready_path())


def read_ready() -> dict[str, object] | None:
    path = _ready_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def append_ui_log(message: str) -> None:
    path = _log_path()
    directory = ui_state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_file() and path.stat().st_size > UI_LOG_MAX_BYTES:
            retained = path.read_bytes()[-UI_LOG_KEEP_BYTES:]
            path.write_bytes(retained)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Networking helpers


def _probe_health(host: str, port: int) -> bool:
    if not isinstance(port, int) or port < 1:
        return False
    url = f"http://127.0.0.1:{port}/health"
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


def primary_local_ip() -> str | None:
    """Best-effort primary non-loopback IPv4 address of this host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if address.startswith("127.") or address.startswith("0."):
        return None
    return address


def _local_url(host: str, port: int) -> str:
    if host in ("0.0.0.0", "::", ""):
        return f"http://127.0.0.1:{port}"
    return f"http://{host}:{port}"


def _port_bound(port: int) -> bool:
    # BSD can permit wildcard bind/listen beside a specific-address listener.
    # Check a live loopback listener first without confusing TIME_WAIT with one.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
        # Reuse permits clean restarts after connections enter TIME_WAIT.
        sock.listen(1)
        return False
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            return True
        raise
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# First-run setup


def _config_root_key_present(config_dir: Path) -> bool:
    try:
        import tomllib

        path = config_dir / "config.toml"
        if not path.is_file():
            return False
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        control = raw.get("control_plane", {})
        return isinstance(control, dict) and "managed_projects_root" in control
    except (OSError, ValueError):
        return False


def prompt_first_run_setup(config_dir: Path, *, need_password: bool, need_root: bool, default_root: str) -> tuple[str | None, str | None]:
    """Interactively collect the missing (auth_token, managed_projects_root)."""
    print("First-run setup for the AFlow web UI")
    print(f"Settings are stored in {config_dir / 'config.toml'}")
    token: str | None = None
    root: str | None = None
    if need_password:
        while True:
            first = getpass.getpass("Set a UI password: ")
            if not first.strip():
                print("  the password must not be empty; try again")
                continue
            second = getpass.getpass("Repeat the UI password: ")
            if first != second:
                print("  the passwords did not match; try again")
                continue
            token = first
            break
    if need_root:
        entered = input(f"Projects root [{default_root}]: ").strip()
        root = entered or default_root
    return token, root


def run_first_run_setup(config_dir: Path, *, interactive: bool) -> None:
    """Fill only missing settings, writing ``bind_host`` explicitly (decision 4)."""
    from aflow_app_server.config import ServerConfig, update_global_settings

    try:
        config = ServerConfig.from_global_dir(config_dir)
    except ValueError as exc:
        raise UIError(f"invalid global configuration: {exc}") from exc
    has_token = False
    try:
        has_token = bool(config.current_auth_token())
    except ValueError:
        has_token = False
    root_present = _config_root_key_present(config_dir)
    if has_token and root_present:
        return
    if not interactive:
        missing: list[str] = []
        if not has_token:
            missing.append(
                '  [server] auth_token = "your-secret"   (or auth_token_file = "/path/to/file")'
            )
        if not root_present:
            missing.append(
                '  [control_plane] managed_projects_root = "~/code"'
            )
        raise UIError(
            "the AFlow UI is not fully configured and no terminal is attached "
            "for first-run setup.\nEdit "
            f"{config_dir / 'config.toml'} and set:\n" + "\n".join(missing) +
            "\nThen run `aflow ui` again."
        )
    token, root = prompt_first_run_setup(
        config_dir,
        need_password=not has_token,
        need_root=not root_present,
        default_root=str(config.managed_projects_root).replace(str(Path.home()), "~", 1)
        if str(config.managed_projects_root).startswith(str(Path.home()))
        else str(config.managed_projects_root),
    )
    update_global_settings(
        config_dir,
        auth_token=token,
        managed_projects_root=root,
        bind_host="0.0.0.0",
    )
    print(f"Wrote {config_dir / 'config.toml'} (mode 0600)")


# ---------------------------------------------------------------------------
# Server construction


def _build_server_config(*, host: str | None, port: int | None):
    """Assemble the resolved ServerConfig for this UI process."""
    from aflow_app_server.config import ServerConfig

    installation = detect_installation()
    config = ServerConfig.from_global_dir(bind_host=host, bind_port=port)
    errors = [e for e in config.validate() if "release identity" not in e]
    if errors:
        raise UIError("invalid global configuration:\n  " + "\n  ".join(errors))
    executable = installation.executable
    if not os.access(executable, os.X_OK):
        raise UIError(f"the aflow executable is not runnable: {executable}")
    worker_env = ui_state_dir() / "worker.env"
    worker_env.parent.mkdir(parents=True, exist_ok=True)
    if not worker_env.exists():
        worker_env.write_text(
            "# aflow ui worker environment (intentionally empty; no secrets)\n",
            encoding="utf-8",
        )
        try:
            os.chmod(worker_env, 0o600)
        except OSError:
            pass
    # A first-run projects root may not exist yet; an empty root is usable.
    try:
        config.managed_projects_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UIError(
            f"the configured projects root is unavailable: "
            f"{config.managed_projects_root} ({exc})"
        ) from exc
    from dataclasses import replace

    return replace(
        config,
        aflow_executable=executable,
        release_identity=installation.release_identity,
        environment_file=worker_env,
    )


def _prepare_assets() -> Path:
    try:
        return ensure_launch_assets(progress=lambda message: print(f"aflow ui: {message}"))
    except AssetBuildError as exc:
        raise UIError(str(exc)) from exc


def _print_startup_banner(config, assets: Path, *, foreground: bool) -> None:
    url = _local_url(config.bind_host, config.bind_port)
    mode = "foreground" if foreground else "background"
    print(f"AFlow UI configuration root: {config.managed_projects_root}")
    print(f"Mode: {mode}")
    print(f"Local URL: {url}")
    if config.bind_host in ("0.0.0.0", "::", ""):
        address = primary_local_ip()
        if address:
            print(f"Other devices on your network or Tailscale can try: http://{address}:{config.bind_port}")
        else:
            print("Other devices can connect to this host's non-loopback address on that port.")
        print("(Reachability depends on your firewall and VPN routing; nothing is configured automatically.)")
    print(f"Log: {_log_path()}")
    print(f"UI assets: {assets}")


def _print_ready(config, *, foreground: bool) -> None:
    url = _local_url(config.bind_host, config.bind_port)
    print(f"AFlow UI is ready at {url} (Ctrl+C to stop)" if foreground else f"AFlow UI is ready at {url}")


# ---------------------------------------------------------------------------
# Serving


def _serve(config, *, mode: str) -> int:
    """Serve the app in this process; shared by foreground and background child."""
    import uvicorn

    from aflow_app_server import main as server_main

    server_main.configure_server(config)
    record = UIRecord.current(
        host=config.bind_host, port=config.bind_port, mode=mode
    )
    claim_ui_record(record)
    write_ready({"schema": 1, "ready": False, "pid": record.pid, "port": config.bind_port})
    append_ui_log(f"ui serving {mode} on {config.bind_host}:{config.bind_port}")

    uv_config = uvicorn.Config(
        server_main.app,
        host=config.bind_host,
        port=config.bind_port,
        log_config=server_main._build_uvicorn_log_config(),
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)
    failure: list[str] = []

    def _run() -> None:
        try:
            server.run()
        except Exception as exc:  # pragma: no cover - defensive
            failure.append(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=_run, name="aflow-ui-uvicorn", daemon=True)
    thread.start()

    def _request_shutdown(signum, frame):  # noqa: ARG001 - signal signature
        server.should_exit = True

    signal.signal(signal.SIGTERM, _request_shutdown)
    deadline = time.monotonic() + 30.0
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        if not thread.is_alive():
            break
        time.sleep(0.05)
    if server.started:
        write_ready(
            {
                "schema": 1,
                "ready": True,
                "pid": record.pid,
                "host": config.bind_host,
                "port": config.bind_port,
                "local_url": _local_url(config.bind_host, config.bind_port),
            }
        )
        _print_ready(config, foreground=(mode == "foreground"))
    else:
        if failure:
            detail = "; ".join(failure)
        elif _port_bound(config.bind_port):
            detail = (
                f"port {config.bind_port} is occupied by another program; "
                "free it or choose another port with `aflow ui --port <port>`"
            )
        else:
            detail = "the server exited during startup; see the log"
        write_ready({"schema": 1, "ready": False, "pid": record.pid, "error": detail})
        append_ui_log(f"ui failed to start: {detail}")
        release_ui_record()
        print(f"aflow ui: {detail}", file=sys.stderr)
        return 1
    try:
        while thread.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=10)
    release_ui_record()
    append_ui_log("ui stopped")
    return 0


# ---------------------------------------------------------------------------
# Background launch


def _spawn_detached(config, *, host: str | None, port: int | None) -> tuple[subprocess.Popen, Path]:
    executable = detect_installation().executable
    argv = [str(executable), "ui", "--ui-internal-serve"]
    if host is not None:
        argv.extend(["--host", host])
    if port is not None:
        argv.extend(["--port", str(port)])
    ui_state_dir().mkdir(parents=True, exist_ok=True)
    log_handle = _log_path().open("ab")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            cwd=str(Path.home()),
        )
    finally:
        log_handle.close()
    return process, _log_path()


def _wait_for_ready(child: subprocess.Popen) -> tuple[bool, str]:
    deadline = time.monotonic() + READY_WAIT_SECONDS
    while time.monotonic() < deadline:
        ready = read_ready()
        ready_matches = (
            isinstance(ready, dict) and ready.get("pid") == child.pid
        )
        if ready_matches and ready.get("ready") is True:
            return True, ""
        if ready_matches and ready.get("error"):
            return False, str(ready["error"])
        if child.poll() is not None:
            detail = "the detached UI process exited before becoming ready"
            if ready_matches and isinstance(ready, dict) and ready.get("error"):
                detail = str(ready["error"])
            return False, detail
        time.sleep(0.1)
    return False, f"the detached UI process was not ready within {READY_WAIT_SECONDS:.0f}s"


# ---------------------------------------------------------------------------
# Status / stop


def _status() -> int:
    record = read_ui_record()
    if record is None:
        print("aflow ui is not running")
        return 0
    state = _record_state(record)
    if state == "dead":
        print("aflow ui is not running (stale ownership record present)")
        return 0
    if state == "mismatched":
        print(
            f"the recorded aflow ui pid {record.pid} was reused by another process; "
            "the ownership record is stale and can be removed"
        )
        return 1
    responding = _probe_health(record.host, record.port)
    url = _local_url(record.host, record.port)
    print(f"aflow ui is running: {url} (pid {record.pid}, started {record.started_at})")
    print(f"HTTP: {'responding' if responding else 'not responding on /health'}")
    print(f"Log: {_log_path()}")
    return 0


def stop_ui() -> int:
    record = read_ui_record()
    if record is None:
        print("aflow ui is not running")
        return 0
    state = _record_state(record)
    if state != "matching":
        _record_path().unlink(missing_ok=True)
        print(
            f"the recorded aflow ui pid {record.pid} is {state}; stale record removed "
            "(workflow units were never touched)"
        )
        return 0
    try:
        os.kill(record.pid, signal.SIGTERM)
    except ProcessLookupError:
        _record_path().unlink(missing_ok=True)
        print("aflow ui is not running")
        return 0
    except PermissionError as exc:
        raise UIError(f"cannot signal the recorded UI pid {record.pid}: {exc}") from exc
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while (
        time.monotonic() < deadline
        and process_birth_identity(record.pid) == record.process_birth
    ):
        time.sleep(0.1)
    if process_birth_identity(record.pid) == record.process_birth:
        os.kill(record.pid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 2.0
        while (
            time.monotonic() < kill_deadline
            and process_birth_identity(record.pid) == record.process_birth
        ):
            time.sleep(0.05)
    released = not _record_path().exists()
    if process_birth_identity(record.pid) == record.process_birth:
        print(f"error: the recorded UI pid {record.pid} did not terminate", file=sys.stderr)
        return 1
    if not released:
        _record_path().unlink(missing_ok=True)
    print("aflow ui stopped (running workflows were not signalled)")
    return 0


# ---------------------------------------------------------------------------
# Command entry points


def handle_ui_command(args) -> int:
    """Entry for `aflow ui`."""
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        if args.ui_internal_serve:
            config = _build_server_config(host=args.host, port=args.port)
            return _serve(config, mode="background")
        if args.stop:
            return stop_ui()
        if args.status:
            return _status()
        config_dir = Path.home() / ".config" / "aflow"
        run_first_run_setup(config_dir, interactive=interactive)
        from aflow.config import bootstrap_config

        bootstrap_config()  # packaged defaults for absent workflow files
        config = _build_server_config(host=args.host, port=args.port)
        assets = _prepare_assets()
        record = read_ui_record()
        if record is not None and _record_state(record) == "matching" and _probe_health(record.host, record.port):
            print(
                f"aflow ui is already running at {_local_url(record.host, record.port)} "
                f"(pid {record.pid}); not starting a second server"
            )
            return 0
        if _port_bound(config.bind_port):
            existing = read_ui_record()
            if (
                existing is not None
                and existing.port == config.bind_port
                and _record_state(existing) == "matching"
                and _probe_health(existing.host, existing.port)
            ):
                print(
                    f"aflow ui is already running at {_local_url(existing.host, existing.port)} "
                    f"(pid {existing.pid}); not starting a second server"
                )
                return 0
            raise UIError(
                f"port {config.bind_port} is occupied by another program; "
                "free it or choose another port with `aflow ui --port <port>`"
            )
        _print_startup_banner(config, assets, foreground=not args.daemon)
        if args.daemon:
            child, log = _spawn_detached(config, host=args.host, port=args.port)
            ok, detail = _wait_for_ready(child)
            if ok:
                print(f"aflow ui detached; ready at {_local_url(config.bind_host, config.bind_port)}")
                print(f"Stop it with `aflow ui --stop`. Log: {log}")
                return 0
            print(
                f"error: the background AFlow UI failed to start: {detail}\nLog: {log}",
                file=sys.stderr,
            )
            return 1
        return _serve(config, mode="foreground")
    except UIError as exc:
        print(f"aflow ui: {exc}", file=sys.stderr)
        return 1


def handle_ui_worker_command(args) -> int:
    """Private `aflow ui-worker` wrapper: owns one workflow process group."""
    from aflow.control_plane.persistent_units import _receipts_for
    from aflow.control_plane.models import startup_failure
    from threading import Thread

    receipt_dir = Path(args.receipt_dir).absolute()
    nonce = getattr(args, "nonce", None)
    start = _read_worker_receipt(receipt_dir, "start.json")
    try:
        receipts = _receipts_for(f"aflow-run-{receipt_dir.parent.name}.service", Path.cwd())
    except ValueError:
        receipts = None
    if nonce is None or receipts is None or receipts.directory != receipt_dir or not isinstance(start, dict) or start.get("nonce") != nonce:
        print(
            "aflow ui-worker: the launch claim is missing or its invocation "
            "nonce does not match; refusing to start a workflow worker",
            file=sys.stderr,
        )
        return 2
    inner = list(args.worker_argv)
    if not inner:
        _write_worker_receipt(
            receipt_dir,
            "error.json",
            {"schema": 1, "nonce": nonce, "error": "no worker argv supplied"},
        )
        return 2
    log_lines: list[str] = []
    try:
        child = subprocess.Popen(
            inner,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd=str(Path.cwd()),
            env={**os.environ, "AFLOW_WORKER_NONCE": nonce},
        )
    except OSError as exc:
        log_lines.append(startup_failure("wrapper_spawn", f"spawn failed: {exc}")["message"])
        _write_worker_receipt(
            receipt_dir,
            "error.json",
            {
                "schema": 1,
                "nonce": nonce,
                **startup_failure("wrapper_spawn", f"failed to start the workflow worker: {exc}"),
                "error": startup_failure("wrapper_spawn", str(exc))["message"],
            },
        )
        _write_wrapper_log(receipt_dir, log_lines)
        return 127
    birth = process_birth_identity(child.pid)
    try:
        _write_worker_receipt(
            receipt_dir,
            "child.json",
            {
                "schema": 1,
                "nonce": nonce,
                "pid": child.pid,
                "pgid": child.pid,
                "process_birth": birth,
                "argv0": inner[0],
            },
        )
    except OSError:
        # A child with no durable identity cannot safely outlive its wrapper.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()
        child.stderr.close()
        try:
            _write_worker_receipt(receipt_dir, "error.json", {"schema": 1, "nonce": nonce, **startup_failure("wrapper_receipt", "Could not persist worker process identity")})
            _write_worker_receipt(receipt_dir, "exit.json", {"schema": 1, "nonce": nonce, "returncode": child.returncode, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except OSError:
            print("aflow ui-worker: receipt persistence failed", file=sys.stderr)
        return 1
    # One reader per stream avoids pipe-buffer deadlocks. Keep only complete,
    # redacted lines; discard overlong lines rather than leaking a split secret.
    tails = {"stdout": "", "stderr": ""}
    def drain(stream, key):
        pending = b""
        dropping = False
        while chunk := stream.read1(4096):
            for piece in chunk.splitlines(keepends=True):
                complete = piece.endswith((b"\n", b"\r"))
                if not dropping:
                    pending += piece
                    if len(pending) > 8192:
                        pending = b""
                        dropping = True
                if complete:
                    line = "[overlong output line omitted]\n" if dropping else startup_failure("worker", pending.decode("utf-8", errors="replace"))["message"]
                    tails[key] = (tails[key] + line)[-4096:]
                    pending = b""
                    dropping = False
        if pending:
            tails[key] = (tails[key] + startup_failure("worker", pending.decode("utf-8", errors="replace"))["message"])[-4096:]
        stream.close()
    readers = [Thread(target=drain, args=(child.stdout, "stdout")), Thread(target=drain, args=(child.stderr, "stderr"))]
    for reader in readers:
        reader.start()
    persistence_error = None
    while child.poll() is None:
        try:
            _write_worker_receipt(receipt_dir, "diagnostic.json", {"schema": 1, "nonce": nonce, **tails})
        except OSError as exc:
            persistence_error = exc
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    code = child.returncode
    for reader in readers:
        reader.join()
    try:
        _write_worker_receipt(receipt_dir, "diagnostic.json", {"schema": 1, "nonce": nonce, **tails})
    except OSError as exc:
        persistence_error = exc
    _write_worker_receipt(
        receipt_dir,
        "exit.json",
        {
            "schema": 1,
            "nonce": nonce,
            "returncode": code,
            "diagnostic_write_failed": persistence_error is not None,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    _write_wrapper_log(receipt_dir, log_lines)
    if persistence_error is not None:
        print("aflow ui-worker: diagnostic persistence failed", file=sys.stderr)
        return 1
    if code < 0:
        return 128 + (-code)
    return code


def _read_worker_receipt(receipt_dir: Path, name: str) -> dict | None:
    from aflow.control_plane.persistent_units import _read_json

    return _read_json(receipt_dir / name)


def _write_worker_receipt(receipt_dir: Path, name: str, payload: dict[str, object]) -> None:
    from aflow.control_plane.persistent_units import _write_receipt

    _write_receipt(receipt_dir / name, payload, exclusive=False)


def _write_wrapper_log(receipt_dir: Path, lines: list[str]) -> None:
    if not lines:
        return
    try:
        with (receipt_dir / "wrapper.log").open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
    except OSError:
        pass

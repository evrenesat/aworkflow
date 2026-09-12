#!/usr/bin/env python3
"""End-to-end smoke test for the ``aflow ui`` product surface.

Modes
-----

``--verify-wheel WHEEL``
    Assert the distributable bundles the compiled UI assets and the server
    package, and print its identity.

``--wheel WHEEL [--login-only] [--browser]``
    Install the explicitly selected wheel into a task-private uv tool
    directory and HOME, then run ``aflow ui`` from an unrelated working
    directory with Node/npm removed from PATH.  Binds ``0.0.0.0`` and every
    HTTP check goes to the host's primary non-loopback address, so a
    Secure-cookie regression over plain HTTP fails loudly.  ``--login-only``
    performs the minimal HTTP login/cookie check; the default also registers
    two projects, runs deterministic fake workflows, restarts the UI, and
    verifies both project-scoped histories remain visible.  ``--browser`` drives
    the flow through Playwright Chromium instead of raw HTTP.

The script owns only its temporary homes, tools, processes, and files, never
touches live user configuration, and exits nonzero on any failure.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import uuid


def log(message: str) -> None:
    print(f"[smoke_ui] {message}", flush=True)


def fail(message: str):
    raise SystemExit(f"[smoke_ui] FAILURE: {message}")


def primary_non_loopback_address() -> str:
    """Return a usable non-loopback address of this host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        address = sock.getsockname()[0]
    except OSError:
        address = ""
    finally:
        sock.close()
    candidates = [address]
    try:
        candidates.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    for candidate in candidates:
        if candidate and not candidate.startswith("127.") and not candidate.startswith("0."):
            return candidate
    fail("no non-loopback address is available for the HTTP smoke check")


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def verify_wheel(wheel: Path) -> str:
    """Validate the distributable's identity and bundled assets."""
    if not wheel.is_file():
        fail(f"wheel not found: {wheel}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = [
            "aflow/ui_web/index.html",
            "aflow/cli.py",
            "aflow/ui_cli.py",
            "aflow_app_server/config.py",
            "aflow_app_server/main.py",
            "aflow_app_server/global_config_service.py",
            "aflow/control_plane/persistent_units.py",
        ]
        missing = [name for name in required if name not in names]
        if missing:
            fail(f"wheel {wheel.name} is missing required entries: {missing}")
        assets = [name for name in names if name.startswith("aflow/ui_web/assets/")]
        if not assets:
            fail("wheel does not bundle compiled UI assets")
        changelog_name = "aflow/ui_web/changelog.json"
        if changelog_name not in names:
            fail(f"wheel {wheel.name} is missing generated changelog asset")
        try:
            changelog = json.loads(archive.read(changelog_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(f"wheel {wheel.name} has invalid generated changelog JSON: {exc}")
        entries = changelog.get("entries") if isinstance(changelog, dict) else None
        if (
            not isinstance(changelog, dict)
            or changelog.get("schema_version") != 1
            or not isinstance(entries, list)
            or not entries
        ):
            fail(f"wheel {wheel.name} has invalid generated changelog schema")
        for index, entry in enumerate(entries):
            if (
                not isinstance(entry, dict)
                or set(entry) != {"date", "title"}
                or not isinstance(entry["date"], str)
                or not entry["date"]
                or not isinstance(entry["title"], str)
                or not entry["title"].strip()
            ):
                fail(f"wheel {wheel.name} has invalid changelog entry {index}")
        metadata = next(
            (name for name in names if name.endswith("METADATA")), None
        )
        version = "unknown"
        if metadata:
            for line in archive.read(metadata).decode("utf-8").splitlines():
                if line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
                    break
    identity = f"{wheel.name} version={version} sha256={digest[:16]}"
    log(f"verified wheel: {identity}")
    return identity


def http_request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    cookie: str | None = None,
    payload: dict | None = None,
    origin: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers: dict[str, str] = {}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@dataclass(frozen=True)
class OwnedRun:
    """A workflow run launched and therefore owned by this smoke invocation."""

    project_id: str
    run_id: str
    unit_nonce: str | None = None


class SmokeCleanupError(RuntimeError):
    """One or more invocation-owned smoke resources could not be released."""


class _SmokeUIUnavailable(RuntimeError):
    """The temporary UI cannot service an owner-stop request."""


_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "done", "failed", "interrupted", "owner_stopped"}
)
_UI_UNAVAILABLE_STATUSES = frozenset({502, 503, 504})
_RUN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


_INSTALLED_UNIT_CLEANUP = """\
from pathlib import Path
import sys

from aflow.control_plane.persistent_units import PersistentUnitManager, _receipts_for


project_root = Path(sys.argv[1]).resolve()
run_id = sys.argv[2]
expected_nonce = sys.argv[3]
executable = Path(sys.argv[4])
projects_root = Path(sys.argv[5]).resolve()
unit_name = f"aflow-run-{run_id}.service"
expected_directory = project_root / ".aflow" / "runs" / run_id / "units"
receipts = _receipts_for(unit_name, project_root)
if (
    receipts is None
    or receipts.directory != expected_directory
    or receipts.nonce != expected_nonce
):
    raise SystemExit("owned persistent unit receipt does not match this smoke invocation")

manager = PersistentUnitManager(
    executable=executable,
    projects_root=projects_root,
)
manager.register_project_root(project_root)
state = manager.stop(unit_name)
if state is None:
    raise SystemExit("owned persistent unit disappeared during cleanup")
if state.is_active:
    raise SystemExit("owned persistent unit remained active after cleanup")
"""


MCP_PROTOCOL_VERSION = "2025-11-25"


def mcp_request(
    base_url: str,
    token: str,
    method: str,
    params: dict | None = None,
) -> dict:
    """Call the stateless authenticated MCP transport over the UI port."""
    status, _, body = http_request(
        f"{base_url}/mcp",
        method="POST",
        token=token,
        origin=base_url,
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        },
        extra_headers={
            "Accept": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        },
    )
    if status != 200:
        fail(f"MCP {method} failed with status {status}: {body[:300]}")
    try:
        response = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"MCP {method} returned invalid JSON: {exc}")
    if "error" in response:
        fail(f"MCP {method} returned an error: {response['error']}")
    return response["result"]


def mcp_tool(
    base_url: str,
    token: str,
    name: str,
    arguments: dict | None = None,
) -> object:
    result = mcp_request(
        base_url,
        token,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
    )
    if result.get("isError") is True:
        content = result.get("content", [])
        detail = (
            content[0].get("text", "MCP tool error")
            if content
            else "MCP tool error"
        )
        fail(f"MCP tool {name} failed: {detail}")
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    content = result.get("content", [])
    if not content or not isinstance(content[0].get("text"), str):
        fail(f"MCP tool {name} returned no structured result")
    try:
        return json.loads(content[0]["text"])
    except json.JSONDecodeError as exc:
        fail(f"MCP tool {name} returned invalid JSON text: {exc}")


class InstalledWheel:
    """A wheel installed into a private uv tool environment."""

    def __init__(self, wheel: Path, home: Path, workdir: Path) -> None:
        self.wheel = wheel
        self.home = home
        self.workdir = workdir
        self.tool_dir = home / "uv-tools"
        self.token = f"smoke-token-{os.getpid()}"
        self.port = free_port()
        self.host = primary_non_loopback_address()
        self.process: subprocess.Popen | None = None
        self._owned_runs: dict[tuple[str, str], OwnedRun] = {}
        self._cleanup_session: str | None = None
        self._cleanup_invocation_token = uuid.uuid4().hex

    _NODE_TOOLS = frozenset({"node", "npm", "npx", "nodejs", "corepack"})

    def _node_free_path(self) -> str:
        """A PATH whose directories are reproduced without Node/npm executables.

        Whole directories cannot be dropped (they also carry git etc.); a
        shadow bin symlinks every other executable so the installed wheel is
        exercised exactly as an end user runs it: Node/npm unavailable.
        """
        shadow = self.home / "node-free-bin"
        shadow.mkdir(parents=True, exist_ok=True)
        for part in os.environ.get("PATH", "").split(os.pathsep):
            directory = Path(part) if part else None
            if directory is None or not directory.is_dir():
                continue
            for entry in directory.iterdir():
                if entry.name in self._NODE_TOOLS:
                    continue
                link = shadow / entry.name
                # Do not follow inaccessible system executables (macOS ships
                # protected entries such as weakpass_edit), or broken links.
                if not os.path.lexists(link):
                    try:
                        link.symlink_to(entry)
                    except OSError:
                        continue
        return str(shadow)

    def _env(self, *, node_free: bool = True) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["UV_TOOL_DIR"] = str(self.tool_dir)
        env["UV_TOOL_BIN_DIR"] = str(self.home / "bin")
        if node_free:
            env["PATH"] = self._node_free_path()
        return env

    def install(self) -> None:
        completed = subprocess.run(
            ["uv", "tool", "install", "--force", str(self.wheel)],
            capture_output=True, text=True, env=self._env(node_free=False),
            check=False,
        )
        if completed.returncode != 0:
            fail(f"uv tool install failed:\n{completed.stdout}\n{completed.stderr}")
        self.executable = self.home / "bin" / "aflow"
        if not self.executable.is_file():
            fail("the installed tool did not provide the aflow entry point")

    def write_config(self) -> None:
        # Stdlib-only: the smoke script never imports the repo it tests.
        config_dir = self.home / ".config" / "aflow"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.toml"
        config_file.write_text(
            "[server]\n"
            'bind_host = "0.0.0.0"\n'
            f"bind_port = {self.port}\n"
            f'auth_token = "{self.token}"\n\n'
            "[control_plane]\n"
            'managed_projects_root = "~/code"\n',
            encoding="utf-8",
        )
        os.chmod(config_file, 0o600)
        # Project creation commits through git; give the private HOME an identity.
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = smoke\n\temail = smoke@example.com\n"
            "[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
        # A deterministic workflow pair driven by the fake harness.
        (config_dir / "aflow.toml").write_text(
            '[aflow]\ndefault_workflow = "simple"\nmax_turns = 6\n\n'
            '[roles]\narchitect = "codex.default"\n\n'
            '[harness.codex.profiles.default]\nmodel = "smoke"\n\n'
            '[prompts]\np = "Work from {ACTIVE_PLAN_PATH}."\n',
            encoding="utf-8",
        )
        (config_dir / "workflows.toml").write_text(
            "[workflow.simple]\n"
            "[workflow.simple.steps.implement_plan]\nrole = \"architect\"\n"
            'prompts = ["p"]\ngo = [{ to = "END", when = "DONE" }]\n',
            encoding="utf-8",
        )

    def start(self) -> None:
        completed = subprocess.run(
            [
                str(self.executable), "ui", "--daemon",
                "--host", "0.0.0.0", "--port", str(self.port),
            ],
            cwd=str(self.workdir),
            capture_output=True, text=True, env=self._env(),
            timeout=120,
        )
        if completed.returncode != 0:
            fail(
                "aflow ui --daemon failed:\n"
                f"{completed.stdout}\n{completed.stderr}"
            )

    def stop(self) -> None:
        subprocess.run(
            [str(self.executable), "ui", "--stop"],
            cwd=str(self.workdir), capture_output=True, text=True,
            env=self._env(), timeout=60,
        )

    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def remember_session(self, session: str) -> None:
        """Retain the latest session for cleanup after a UI restart."""
        self._cleanup_session = session

    def _project_root(self, project_id: str) -> Path:
        if (
            not isinstance(project_id, str)
            or not project_id
            or Path(project_id).name != project_id
        ):
            raise SmokeCleanupError(
                f"cannot prove the smoke project identity for {project_id!r}"
            )
        projects_root = (self.home / "code").resolve()
        project_root = (projects_root / project_id).resolve()
        if project_root.parent != projects_root or not project_root.is_dir():
            raise SmokeCleanupError(
                f"cannot prove the smoke project root for {project_id!r}"
            )
        return project_root

    def _unit_nonce(self, project_id: str, run_id: str) -> str | None:
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            return None
        project_root = self._project_root(project_id)
        receipt_dir = project_root / ".aflow" / "runs" / run_id / "units"
        receipt_path = receipt_dir / "start.json"
        if any(
            path.is_symlink()
            for path in (
                project_root,
                project_root / ".aflow",
                project_root / ".aflow" / "runs",
                project_root / ".aflow" / "runs" / run_id,
                receipt_dir,
                receipt_path,
            )
        ):
            return None
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if (
            type(payload.get("schema")) is not int
            or payload.get("schema") != 1
            or payload.get("run_id") != run_id
            or payload.get("unit") != f"aflow-run-{run_id}.service"
        ):
            return None
        nonce = payload.get("nonce")
        return nonce if isinstance(nonce, str) and nonce else None

    def remember_owned_run(self, project_id: str, run_id: str) -> None:
        """Record a successful launch before any later smoke assertion runs."""
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            raise SmokeCleanupError(f"the server returned an unsafe run ID: {run_id!r}")
        self._owned_runs[(project_id, run_id)] = OwnedRun(
            project_id=project_id,
            run_id=run_id,
            unit_nonce=self._unit_nonce(project_id, run_id),
        )

    def _owner_stop_request(
        self,
        owned: OwnedRun,
        session: str,
        request,
    ) -> None:
        run_url = (
            f"{self.base_url()}/api/control-plane/projects/"
            f"{owned.project_id}/runs/{owned.run_id}"
        )
        try:
            status, _, body = request(run_url, cookie=session)
        except OSError as exc:
            raise _SmokeUIUnavailable(str(exc)) from exc
        if status in _UI_UNAVAILABLE_STATUSES:
            raise _SmokeUIUnavailable(f"run status returned HTTP {status}")
        if status != 200:
            raise SmokeCleanupError(
                f"could not inspect owned run {owned.project_id}/{owned.run_id}: "
                f"HTTP {status} {body[:300]}"
            )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeCleanupError(
                f"owned run {owned.project_id}/{owned.run_id} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SmokeCleanupError(
                f"owned run {owned.project_id}/{owned.run_id} returned a non-object"
            )
        run_status = payload.get("status")
        if run_status in _TERMINAL_RUN_STATUSES and payload.get("activity") != "active":
            return
        revision = payload.get("revision")
        if type(revision) is not int or revision < 0:
            raise SmokeCleanupError(
                f"owned run {owned.project_id}/{owned.run_id} has no valid revision"
            )
        cleanup_key = (
            f"smoke-cleanup-{self._cleanup_invocation_token}-"
            f"{owned.project_id}-{owned.run_id}-{uuid.uuid4().hex}"
        )
        try:
            status, _, body = request(
                f"{run_url}/owner-stop",
                method="POST",
                cookie=session,
                payload={"expected_revision": revision},
                origin=self.base_url(),
                extra_headers={"Idempotency-Key": cleanup_key},
            )
        except OSError as exc:
            raise _SmokeUIUnavailable(str(exc)) from exc
        if status in _UI_UNAVAILABLE_STATUSES:
            raise _SmokeUIUnavailable(f"owner-stop returned HTTP {status}")
        if status != 200:
            raise SmokeCleanupError(
                f"could not stop owned run {owned.project_id}/{owned.run_id}: "
                f"HTTP {status} {body[:300]}"
            )
        try:
            stopped = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeCleanupError(
                f"owner-stop for {owned.project_id}/{owned.run_id} returned invalid JSON"
            ) from exc
        if not isinstance(stopped, dict) or stopped.get("status") != "owner_stopped":
            raise SmokeCleanupError(
                f"owner-stop for {owned.project_id}/{owned.run_id} did not record cleanup"
            )

    def _installed_python(self) -> Path:
        """Find the interpreter belonging to this installed wheel tool."""
        entrypoint = self.executable.resolve()
        try:
            first_line = entrypoint.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError) as exc:
            raise SmokeCleanupError(
                f"cannot inspect the installed aflow entry point: {entrypoint}"
            ) from exc
        if first_line.startswith("#!"):
            interpreter = Path(first_line[2:].strip().split()[0])
            if interpreter.is_file():
                return interpreter
        for candidate in (
            entrypoint.parent / "python",
            entrypoint.parent / "python3",
        ):
            if candidate.is_file():
                return candidate
        raise SmokeCleanupError(
            f"cannot find the installed wheel interpreter for {entrypoint}"
        )

    def _fallback_stop_owned_run(self, owned: OwnedRun) -> None:
        """Stop one exact unit through the installed runtime if UI is down."""
        nonce = owned.unit_nonce or self._unit_nonce(owned.project_id, owned.run_id)
        if nonce is None:
            raise SmokeCleanupError(
                f"cannot prove invocation ownership for {owned.project_id}/{owned.run_id}"
            )
        project_root = self._project_root(owned.project_id)
        projects_root = (self.home / "code").resolve()
        environment = self._env(node_free=False)
        # Never let the source runner's import path cause fallback cleanup to
        # load the checkout instead of the installed wheel under test.
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        try:
            completed = subprocess.run(
                [
                    str(self._installed_python()),
                    "-c",
                    _INSTALLED_UNIT_CLEANUP,
                    str(project_root),
                    owned.run_id,
                    nonce,
                    str(self.executable),
                    str(projects_root),
                ],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                env=environment,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SmokeCleanupError(
                f"installed-runtime cleanup failed for {owned.project_id}/{owned.run_id}: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise SmokeCleanupError(
                f"installed-runtime cleanup failed for {owned.project_id}/{owned.run_id}: {detail}"
            )

    def cleanup_owned_runs(self, *, request=http_request) -> None:
        """Release every run launched by this invocation, including failures."""
        failures: list[str] = []
        for key, owned in tuple(self._owned_runs.items()):
            try:
                if self._cleanup_session is None:
                    raise _SmokeUIUnavailable("no live UI session is available")
                try:
                    self._owner_stop_request(owned, self._cleanup_session, request)
                except _SmokeUIUnavailable:
                    self._fallback_stop_owned_run(owned)
            except Exception as exc:
                failures.append(f"{owned.project_id}/{owned.run_id}: {exc}")
                log(f"cleanup warning for {owned.project_id}/{owned.run_id}: {exc}")
            else:
                del self._owned_runs[key]
        if failures:
            raise SmokeCleanupError("; ".join(failures))


FAKE_HARNESS = """\
#!/usr/bin/env python3
import os, shutil, sys, time
from pathlib import Path

plan = Path.cwd() / os.environ["AFLOW_TEST_PLAN_RELATIVE"]
scenario = os.environ.get("AFLOW_TEST_SCENARIO", "noop")
if Path.cwd().name == os.environ.get("AFLOW_TEST_HOLD_PROJECT"):
    scenario = "hold"
count_file = Path(os.environ["AFLOW_TEST_COUNT_FILE"])
count = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(count))
print(f"codex turn {count}")
if scenario == "complete":
    shutil.copyfile(os.environ["AFLOW_TEST_COMPLETED_PLAN"], plan)
    sys.exit(0)
if scenario == "hold":
    time.sleep(300)
sys.exit(0)
"""


def install_fake_harness(installed: InstalledWheel) -> None:
    """Provide the deterministic fake harness on the server's PATH."""
    bin_dir = installed.home / "fake-harness-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "codex"
    script.write_text(FAKE_HARNESS, encoding="utf-8")
    script.chmod(0o755)
    # The server resolves harness commands through PATH; the launcher is
    # started by this script so the fake harness directory is prepended.
    def start_with_fake_harness() -> None:
        env_path = installed._env()
        env_path["PATH"] = f"{bin_dir}:{env_path['PATH']}"
        env_path["AFLOW_TEST_SCENARIO"] = "complete"
        env_path["AFLOW_TEST_HOLD_PROJECT"] = "smoke-b"
        env_path["AFLOW_TEST_PLAN_RELATIVE"] = "plans/in-progress/smoke-plan.md"
        env_path["AFLOW_TEST_COUNT_FILE"] = str(installed.home / "turn-count")
        env_path["AFLOW_TEST_COMPLETED_PLAN"] = str(
            installed.home / "completed-plan.md"
        )
        (installed.home / "completed-plan.md").write_text(
            "# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n",
            encoding="utf-8",
        )
        # The second project remains active so a UI stop/start can prove that
        # persistent worker units are independent of the server process.
        env_path["AFLOW_TEST_SCENARIO"] = "complete"
        completed = subprocess.run(
            [
                str(installed.executable), "ui", "--daemon",
                "--host", "0.0.0.0", "--port", str(installed.port),
            ],
            cwd=str(installed.workdir), capture_output=True, text=True,
            env=env_path, timeout=120,
        )
        if completed.returncode != 0:
            fail(f"aflow ui --daemon failed:\n{completed.stdout}\n{completed.stderr}")

    installed.start = start_with_fake_harness  # type: ignore[method-assign]


def wait_for_health(base_url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: str = ""
    while time.monotonic() < deadline:
        try:
            status, _, _ = http_request(f"{base_url}/health")
            if status == 200:
                log(f"health check OK at {base_url}")
                return
            last_error = f"status {status}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    fail(f"the UI never became healthy at {base_url}: {last_error}")


def http_login_and_settings(base_url: str, token: str) -> str:
    """Login over non-loopback HTTP; return the session cookie."""
    status, headers, _ = http_request(
        f"{base_url}/api/session", method="POST", token=token,
        payload={}, origin=base_url,
    )
    if status != 200:
        fail(f"login failed with status {status}")
    cookies = headers.get("set-cookie", "")
    if not cookies.startswith("aflow_session="):
        fail("login did not set the aflow_session cookie")
    first_attribute = cookies.split(";", 1)[1] if ";" in cookies else ""
    if "Secure" in first_attribute:
        fail("the session cookie carries the Secure attribute over plain HTTP")
    session = cookies.split(";", 1)[0]

    status, _, body = http_request(f"{base_url}/api/session", cookie=session)
    if status != 200:
        fail("the session cookie was not accepted")
    status, _, body = http_request(f"{base_url}/api/config", cookie=session)
    if status != 200:
        fail("the global configuration endpoint rejected the session")
    if token.encode() in body:
        fail("the credential leaked into the configuration response")
    status, _, body = http_request(f"{base_url}/api/settings", cookie=session)
    if status != 200:
        fail("the settings endpoint rejected the session")
    if token.encode() in body:
        fail("the credential leaked into the settings response")
    payload = json.loads(body)
    if payload.get("password_set") is not True:
        fail("settings does not report the configured password state")
    log("HTTP login, session renewal surface, and redacted settings verified")
    return session


def _commit_project(installed: InstalledWheel, project_id: str, message: str) -> None:
    project_root = installed.home / "code" / project_id
    environment = installed._env(node_free=False)
    subprocess.run(
        ["git", "-C", str(project_root), "add", "-A"],
        check=True,
        capture_output=True,
        env=environment,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "-c",
            "user.name=smoke",
            "-c",
            "user.email=smoke@example.com",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        env=environment,
    )


def _mcp_project_history(
    base_url: str,
    token: str,
    project_id: str,
    run_id: str,
    plan_name: str,
) -> None:
    plans = mcp_tool(base_url, token, "list_plans", {"project_id": project_id})
    if not isinstance(plans, dict) or not any(
        plan.get("path") in {
            f"plans/in-progress/{plan_name}",
            f"plans/done/{plan_name}",
        }
        for plan in plans.get("plans", [])
        if isinstance(plan, dict)
    ):
        fail(f"MCP plan history for {project_id} is not project-scoped")
    runs = mcp_tool(base_url, token, "list_runs", {"project_id": project_id})
    if not isinstance(runs, dict) or run_id not in {
        run.get("run_id")
        for run in runs.get("runs", [])
        if isinstance(run, dict)
    }:
        fail(f"MCP run history for {project_id} lost {run_id}")
    detail = mcp_tool(
        base_url,
        token,
        "get_run",
        {"project_id": project_id, "run_id": run_id},
    )
    if not isinstance(detail, dict) or detail.get("run_id") != run_id:
        fail(f"MCP get_run returned the wrong project history for {project_id}")
    if not str(detail.get("plan_path", "")).endswith(f"/{plan_name}"):
        fail(f"MCP get_run returned the wrong plan for {project_id}")


def full_run_flow(
    installed: InstalledWheel, session: str
) -> tuple[list[str], dict[str, str]]:
    """Exercise two project roots, MCP, a restart, and independent workers."""
    base_url = installed.base_url()
    plan_name = "smoke-plan.md"
    project_specs = (
        ("smoke-a", "Smoke A", "SMOKE_A_ONLY"),
        ("smoke-b", "Smoke B", "SMOKE_B_ONLY"),
    )

    for project_id, display_name, marker in project_specs:
        status, _, body = http_request(
            f"{base_url}/api/projects",
            method="POST",
            cookie=session,
            payload={
                "mode": "create",
                "path": project_id,
                "display_name": display_name,
                "main_branch": "main",
            },
            origin=base_url,
        )
        if status != 201:
            fail(f"{project_id} creation failed with status {status}: {body[:300]}")
        created_project = json.loads(body)
        if created_project.get("id") != project_id:
            fail(f"project creation returned the wrong identity: {body[:300]}")

        plan_content = (
            f"# {display_name}\n\n"
            "### [ ] Checkpoint 1: First\n"
            f"- [ ] {marker}\n"
        )
        status, _, body = http_request(
            f"{base_url}/api/projects/{project_id}/plans",
            method="POST",
            cookie=session,
            payload={"name": plan_name, "content": plan_content},
            origin=base_url,
        )
        if status != 201:
            fail(f"{project_id} plan creation failed with status {status}: {body[:300]}")
        created_plan = json.loads(body)
        _commit_project(installed, project_id, f"{project_id} draft")

        status, _, body = http_request(
            f"{base_url}/api/projects/{project_id}/plans/todo/{plan_name}/promote",
            method="POST",
            cookie=session,
            payload={"expected_revision": created_plan["revision"]},
            origin=base_url,
        )
        if status != 200:
            fail(f"{project_id} plan promotion failed with status {status}: {body[:300]}")
        promoted = json.loads(body)
        if promoted.get("path") != f"plans/in-progress/{plan_name}":
            fail(f"{project_id} promotion returned the wrong path: {body[:300]}")
        _commit_project(installed, project_id, f"{project_id} promote")

    initialization = mcp_request(
        base_url,
        installed.token,
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aflow-issue-6-smoke", "version": "1"},
        },
    )
    if initialization.get("protocolVersion") != MCP_PROTOCOL_VERSION:
        fail("MCP negotiated an unexpected protocol version")
    tools = mcp_request(base_url, installed.token, "tools/list")
    tool_names = {tool.get("name") for tool in tools.get("tools", [])}
    if not {"list_projects", "list_plans", "list_runs", "read_plan", "get_run"}.issubset(tool_names):
        fail("the installed MCP registry is missing multi-project tools")
    projects = mcp_tool(base_url, installed.token, "list_projects")
    if not isinstance(projects, dict) or {
        item.get("project_id") for item in projects.get("projects", [])
        if isinstance(item, dict)
    } != {
        project_id for project_id, _, _ in project_specs
    }:
        fail(f"MCP project listing is not the exact registry: {projects}")
    for project_id, _, _ in project_specs:
        document = mcp_tool(
            base_url,
            installed.token,
            "read_plan",
            {
                "project_id": project_id,
                "plan_status": "in_progress",
                "name": plan_name,
            },
        )
        if not isinstance(document, dict) or document.get("project_id") != project_id:
            fail(f"MCP read_plan returned the wrong project for {project_id}")

    run_ids: dict[str, str] = {}
    for project_id, _, _ in project_specs:
        status, _, body = http_request(
            f"{base_url}/api/control-plane/projects/{project_id}/runs",
            method="POST",
            cookie=session,
            payload={"plan_path": f"plans/in-progress/{plan_name}"},
            origin=base_url,
            extra_headers={"Idempotency-Key": f"smoke-{project_id}"},
        )
        if status not in (200, 201, 202):
            fail(f"{project_id} run start failed with status {status}: {body[:300]}")
        payload = json.loads(body)
        if "result" not in payload:
            fail(f"{project_id} run start returned a startup question: {body[:300]}")
        run_ids[project_id] = payload["result"]["run_id"]
        installed.remember_owned_run(project_id, run_ids[project_id])
        log(f"started {project_id} smoke run {run_ids[project_id]}")

    def read_run(project_id: str, run_id: str, cookie: str) -> dict:
        status, _, body = http_request(
            f"{base_url}/api/control-plane/projects/{project_id}/runs/{run_id}",
            cookie=cookie,
        )
        if status != 200:
            fail(f"{project_id} run {run_id} read failed with status {status}")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(f"{project_id} run {run_id} returned invalid JSON: {exc}")

    deadline = time.monotonic() + 120
    final_a: dict = {}
    while time.monotonic() < deadline:
        final_a = read_run("smoke-a", run_ids["smoke-a"], session)
        if final_a.get("status") in {"completed", "done", "failed", "needs_attention"}:
            break
        time.sleep(1.0)
    if final_a.get("status") not in {"completed", "done"}:
        fail(f"smoke-a ended as '{final_a.get('status', '')}' instead of completing")

    deadline = time.monotonic() + 30
    active_b: dict = {}
    while time.monotonic() < deadline:
        active_b = read_run("smoke-b", run_ids["smoke-b"], session)
        if active_b.get("status") == "running" and active_b.get("activity") == "active":
            break
        time.sleep(0.5)
    if active_b.get("status") != "running" or active_b.get("activity") != "active":
        fail(f"smoke-b did not prove an active independent worker: {active_b}")

    for project_id, _, _ in project_specs:
        _mcp_project_history(
            base_url,
            installed.token,
            project_id,
            run_ids[project_id],
            plan_name,
        )

    # Stop only the UI server.  The held smoke-b worker must remain active and
    # discoverable after the new UI process starts.
    installed.stop()
    installed.start()
    wait_for_health(base_url)
    session2 = http_login_and_settings(base_url, installed.token)
    installed.remember_session(session2)
    status, _, body = http_request(f"{base_url}/api/projects", cookie=session2)
    if status != 200:
        fail("the registered project list disappeared after a UI restart")
    project_payloads = json.loads(body)
    if {
        (item.get("id"), item.get("display_name")) for item in project_payloads
    } != {(project_id, display_name) for project_id, display_name, _ in project_specs}:
        fail(f"the project registry changed after restart: {body[:500]}")

    restarted_a = read_run("smoke-a", run_ids["smoke-a"], session2)
    if restarted_a.get("status") not in {"completed", "done"}:
        fail("the completed project-a run lost its terminal status after restart")
    restarted_b = read_run("smoke-b", run_ids["smoke-b"], session2)
    if restarted_b.get("status") != "running" or restarted_b.get("activity") != "active":
        fail("the project-b worker was stopped or lost during UI restart")

    for project_id, _, _ in project_specs:
        status, _, body = http_request(
            f"{base_url}/api/control-plane/projects/{project_id}/runs",
            cookie=session2,
        )
        if status != 200:
            fail(f"REST history for {project_id} disappeared after restart")
        history = json.loads(body)
        if run_ids[project_id] not in {run.get("run_id") for run in history.get("runs", [])}:
            fail(f"REST history for {project_id} lost its run after restart")
        _mcp_project_history(
            base_url,
            installed.token,
            project_id,
            run_ids[project_id],
            plan_name,
        )

    run_json = (
        installed.home
        / "code"
        / "smoke-a"
        / ".aflow"
        / "runs"
        / run_ids["smoke-a"]
        / "run.json"
    )
    try:
        run_payload = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"the completed run has unreadable current-source metadata: {exc}")
    expected_live_config = (installed.home / ".config" / "aflow" / "aflow.toml").resolve()
    if run_payload.get("live_config_path") != str(expected_live_config):
        fail("the completed run did not retain the smoke configuration source")

    log("two project histories, MCP parity, and an independent worker survived a UI restart")
    return [display_name for _, display_name, _ in project_specs], run_ids


PLAYWRIGHT_DRIVER = """
import json, re, sys, time
from playwright.sync_api import expect, sync_playwright

base_url, token = sys.argv[1], sys.argv[2]
login_only = sys.argv[3] == "login-only"
project_names = json.loads(sys.argv[4])
run_ids = json.loads(sys.argv[5])
result = {"steps": []}
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context()
    page = context.new_page()
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_selector('input[type="password"]', timeout=20000)
    page.fill('input[type="password"]', token)
    page.click("text=Login")
    page.wait_for_selector("text=aflow", timeout=20000)
    time.sleep(1)
    cookies = context.cookies(base_url)
    session = [c for c in cookies if c["name"] == "aflow_session"]
    if not session:
        raise SystemExit("browser login did not set the aflow_session cookie")
    if session[0].get("secure"):
        raise SystemExit("browser received a Secure session cookie over HTTP")
    result["steps"].append("login+cookie")
    if not login_only:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("text=aflow", timeout=20000)
        result["steps"].append("reload")
        page.goto(base_url + "/?view=projects", wait_until="domcontentloaded")
        page.get_by_role("heading", name="Projects", exact=True).wait_for(timeout=20000)
        for project_name in project_names:
            page.get_by_title(project_name, exact=True).wait_for(timeout=20000)
        page.goto(base_url + "/?view=all-runs", wait_until="domcontentloaded")
        page.get_by_role("heading", name="All runs", exact=True).wait_for(timeout=20000)
        for run_id in run_ids:
            run_button = page.get_by_role("button", name=re.compile(r" · " + re.escape(run_id) + r"$"))
            expect(run_button).to_have_count(1, timeout=20000)
            run_button.wait_for(state="visible", timeout=20000)
        result["steps"].append("two-project-history")
        page.click("text=Logout")
        time.sleep(1)
        page.wait_for_selector('input[type="password"]', timeout=20000)
        result["steps"].append("logout")
    browser.close()
print("PLAYWRIGHT_OK " + json.dumps(result))
"""


def browser_flow(
    installed: InstalledWheel,
    *,
    login_only: bool,
    playwright_python: str | None,
    project_names: list[str] | None = None,
    run_ids: list[str] | None = None,
) -> None:
    if playwright_python is None:
        fail("--browser requires --playwright-python pointing at an interpreter with playwright installed")
    driver = installed.home / "playwright_driver.py"
    driver.write_text(PLAYWRIGHT_DRIVER, encoding="utf-8")
    completed = subprocess.run(
        [
            playwright_python, str(driver), installed.base_url(),
            installed.token, "login-only" if login_only else "full",
            json.dumps(project_names or []), json.dumps(run_ids or []),
        ],
        capture_output=True, text=True, timeout=180,
    )
    output = completed.stdout
    if completed.returncode != 0 or "PLAYWRIGHT_OK" not in output:
        fail(f"browser flow failed:\n{output}\n{completed.stderr}")
    log("browser flow OK: " + output.split("PLAYWRIGHT_OK", 1)[1].strip())


def finalize_invocation(installed: InstalledWheel) -> None:
    """Clean owned workers before stopping the disposable UI process."""
    failures: list[str] = []
    try:
        installed.cleanup_owned_runs()
    except Exception as exc:
        failures.append(f"worker cleanup: {exc}")
    try:
        installed.stop()
    except Exception as exc:
        failures.append(f"UI cleanup: {exc}")
    if failures:
        raise SmokeCleanupError("; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-wheel", metavar="WHEEL", help="only verify the wheel's bundled assets")
    parser.add_argument("--wheel", metavar="WHEEL", help="install and exercise this exact wheel")
    parser.add_argument("--login-only", action="store_true", help="minimal HTTP login/cookie check")
    parser.add_argument("--browser", action="store_true", help="drive the flow through Playwright Chromium")
    parser.add_argument("--playwright-python", metavar="EXE", help="interpreter with playwright installed")
    args = parser.parse_args()

    if args.verify_wheel:
        verify_wheel(Path(args.verify_wheel))
        return 0
    if not args.wheel:
        parser.error("either --verify-wheel or --wheel is required")
    wheel = Path(args.wheel)
    identity = verify_wheel(wheel)

    workdir = Path(tempfile.mkdtemp(prefix="smoke-ui-cwd-"))
    home = Path(tempfile.mkdtemp(prefix="smoke-ui-home-"))
    installed = InstalledWheel(wheel, home, workdir)
    log(f"tested artifact: {identity}")
    log(f"private HOME: {home}")
    passed = False
    try:
        installed.install()
        installed.write_config()
        install_fake_harness(installed)
        installed.start()
        base_url = installed.base_url()
        log(f"UI bound 0.0.0.0:{installed.port}; checking via non-loopback {base_url}")
        wait_for_health(base_url)
        session = http_login_and_settings(base_url, installed.token)
        installed.remember_session(session)
        project_names: list[str] = []
        run_ids: dict[str, str] = {}
        if not args.login_only:
            project_names, run_ids = full_run_flow(installed, session)
        if args.browser:
            browser_flow(
                installed,
                login_only=args.login_only,
                playwright_python=args.playwright_python,
                project_names=project_names,
                run_ids=list(run_ids.values()),
            )
        log("SMOKE PASSED")
        passed = True
        return 0
    finally:
        try:
            finalize_invocation(installed)
        except Exception as exc:
            if passed:
                passed = False
                log(f"failure evidence preserved at {home}")
                raise
            log(f"cleanup warning (original failure preserved): {exc}")
        if passed:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            log(f"failure evidence preserved at {home}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

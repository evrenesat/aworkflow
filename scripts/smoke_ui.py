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
    a project, runs a deterministic fake workflow to completion, restarts the
    UI, and verifies the finished run is still visible.  ``--browser`` drives
    the flow through Playwright Chromium instead of raw HTTP.

The script owns only its temporary homes, tools, processes, and files, never
touches live user configuration, and exits nonzero on any failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


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
) -> tuple[int, dict[str, str], bytes]:
    headers: dict[str, str] = {}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


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


FAKE_HARNESS = """\
#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path

plan = Path(os.environ["AFLOW_TEST_PLAN_PATH"])
scenario = os.environ.get("AFLOW_TEST_SCENARIO", "noop")
count_file = Path(os.environ["AFLOW_TEST_COUNT_FILE"])
count = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(count))
print(f"codex turn {count}")
if scenario == "complete":
    shutil.copyfile(os.environ["AFLOW_TEST_COMPLETED_PLAN"], plan)
    sys.exit(0)
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
        env_path["AFLOW_TEST_PLAN_PATH"] = str(
            installed.home / "code" / "smoke" / "plans" / "todo" / "smoke-plan.md"
        )
        env_path["AFLOW_TEST_COUNT_FILE"] = str(installed.home / "turn-count")
        env_path["AFLOW_TEST_COMPLETED_PLAN"] = str(
            installed.home / "completed-plan.md"
        )
        (installed.home / "completed-plan.md").write_text(
            "# Plan\n\n### [x] Checkpoint 1: First\n- [x] step one\n",
            encoding="utf-8",
        )
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


def full_run_flow(installed: InstalledWheel, session: str) -> None:
    """Register a project, run the fake workflow, restart, re-verify."""
    base_url = installed.base_url()
    status, _, body = http_request(
        f"{base_url}/api/projects", method="POST", cookie=session,
        payload={"mode": "create", "path": "smoke", "main_branch": "main"},
        origin=base_url,
    )
    if status != 201:
        fail(f"project creation failed with status {status}: {body[:300]}")
    plan_content = "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] step one\n"
    status, _, body = http_request(
        f"{base_url}/api/projects/smoke/plans", method="POST", cookie=session,
        payload={"name": "smoke-plan.md", "content": plan_content},
        origin=base_url,
    )
    if status != 201:
        fail(f"plan creation failed with status {status}: {body[:300]}")
    # Commit the plan so the worktree is clean and no startup question is
    # raised for dirtiness.
    subprocess.run(
        ["git", "-C", str(installed.home / "code" / "smoke"),
         "add", "-A"], check=True, capture_output=True,
        env={**os.environ, "HOME": str(installed.home)},
    )
    subprocess.run(
        ["git", "-C", str(installed.home / "code" / "smoke"),
         "commit", "-q", "-m", "smoke plan"], check=True, capture_output=True,
        env={**os.environ, "HOME": str(installed.home)},
    )
    status, _, body = http_request(
        f"{base_url}/api/control-plane/projects/smoke/runs",
        method="POST", cookie=session,
        payload={"plan_path": "plans/todo/smoke-plan.md"},
        origin=base_url,
    )
    payload = json.loads(body)
    if "result" not in payload:
        fail(f"run start returned a startup question or error: {body[:300]}")
    if status not in (200, 201, 202):
        fail(f"run start failed with status {status}: {body[:300]}")
    run_id = payload["result"]["run_id"]
    log(f"started smoke run {run_id}")

    deadline = time.monotonic() + 120
    final_status = ""
    while time.monotonic() < deadline:
        status, _, body = http_request(
            f"{base_url}/api/control-plane/projects/smoke/runs/{run_id}",
            cookie=session,
        )
        if status == 200:
            final_status = json.loads(body).get("status", "")
            if final_status in {"completed", "done", "failed", "needs_attention"}:
                break
        time.sleep(1.0)
    if final_status not in {"completed", "done"}:
        fail(f"the smoke run ended as '{final_status}' instead of completing")

    # Restart the UI; the finished run and its snapshot remain inspectable.
    installed.stop()
    installed.start()
    wait_for_health(base_url)
    session2 = http_login_and_settings(base_url, installed.token)
    status, _, body = http_request(
        f"{base_url}/api/control-plane/projects/smoke/runs/{run_id}",
        cookie=session2,
    )
    if status != 200:
        fail("the completed run disappeared after a UI restart")
    if json.loads(body).get("status") not in {"completed", "done"}:
        fail("the completed run lost its terminal status after restart")
    snapshot = (
        installed.home / "code" / "smoke" / ".aflow" / "runs" / run_id / "config"
    )
    if not (snapshot / "snapshot.json").is_file():
        fail("the run has no frozen configuration snapshot")
    log("full run flow survived a UI restart with its frozen snapshot intact")


PLAYWRIGHT_DRIVER = """
import json, sys, time
from playwright.sync_api import sync_playwright

base_url, token = sys.argv[1], sys.argv[2]
login_only = sys.argv[3] == "login-only"
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
        page.click("text=Logout")
        time.sleep(1)
        page.wait_for_selector('input[type="password"]', timeout=20000)
        result["steps"].append("logout")
    browser.close()
print("PLAYWRIGHT_OK " + json.dumps(result))
"""


def browser_flow(
    installed: InstalledWheel, *, login_only: bool, playwright_python: str | None
) -> None:
    if playwright_python is None:
        fail("--browser requires --playwright-python pointing at an interpreter with playwright installed")
    driver = installed.home / "playwright_driver.py"
    driver.write_text(PLAYWRIGHT_DRIVER, encoding="utf-8")
    completed = subprocess.run(
        [
            playwright_python, str(driver), installed.base_url(),
            installed.token, "login-only" if login_only else "full",
        ],
        capture_output=True, text=True, timeout=180,
    )
    output = completed.stdout
    if completed.returncode != 0 or "PLAYWRIGHT_OK" not in output:
        fail(f"browser flow failed:\n{output}\n{completed.stderr}")
    log("browser flow OK: " + output.split("PLAYWRIGHT_OK", 1)[1].strip())


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
        if not args.login_only:
            full_run_flow(installed, session)
        if args.browser:
            browser_flow(installed, login_only=args.login_only,
                         playwright_python=args.playwright_python)
        installed.stop()
        log("SMOKE PASSED")
        passed = True
        return 0
    finally:
        installed.stop()
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

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "aflowd"
MODULE_PATH = DEPLOY / "continuous-deploy.py"
CI_PATH = ".github/workflows/ci.yml"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="aflowd continuous deployment polling requires Linux and GNU git",
)

PREFLIGHT_FIXTURE = """#!/bin/sh
printf 'preflight\\n' >>"$DEPLOY_CD_MARK"
output_dir=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--output-dir" ]; then output_dir="$argument"; fi
  previous="$argument"
done
mkdir -p "$output_dir"
if [ -n "$DEPLOY_CD_PREFLIGHT_SLEEP" ]; then sleep "$DEPLOY_CD_PREFLIGHT_SLEEP"; fi
if [ -n "$DEPLOY_CD_PREFLIGHT_SNAPSHOT" ]; then
  printf '%s\\n' "$DEPLOY_CD_PREFLIGHT_SNAPSHOT" >"$output_dir/preflight.json"
elif [ -z "$DEPLOY_CD_PREFLIGHT_OMIT_SNAPSHOT" ]; then
  : >"$output_dir/preflight.json"
fi
if [ -n "$DEPLOY_CD_PREFLIGHT_FAIL" ]; then exit 1; fi
exit 0
"""

INSTALLER_FIXTURE = """#!/bin/sh
{
  printf 'install'
  for argument in "$@"; do printf ' %s' "$argument"; done
  printf '\\n'
} >>"$DEPLOY_CD_MARK"
if [ -n "$DEPLOY_CD_INSTALLER_HANG" ]; then
  sleep 300 &
  printf '%s\\n' "$!" >"$DEPLOY_CD_INSTALLER_CHILD"
  printf '%s\\n' "$$" >"$DEPLOY_CD_INSTALLER_PARENT"
  wait "$!"
  exit 0
fi
if [ -n "$DEPLOY_CD_INSTALLER_FAIL" ]; then exit 7; fi
exit 0
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("aflowd_continuous_deploy", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, env=env, check=False)


def _git(*args: str) -> None:
    result = _run("git", *args)
    assert result.returncode == 0, result.stderr


def _git_rev(path: Path, revision: str) -> str:
    result = _run("git", "-C", str(path), "rev-parse", revision)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _fake_origin(path: Path) -> None:
    deploy = path / "deploy" / "aflowd"
    deploy.mkdir(parents=True)
    (path / "README.md").write_text("fixture origin\n")
    (deploy / "preflight.sh").write_text(PREFLIGHT_FIXTURE)
    (deploy / "install.sh").write_text(INSTALLER_FIXTURE)
    for script in ("preflight.sh", "install.sh"):
        (deploy / script).chmod(0o755)
    _git("init", "-q", "-b", "main", str(path))
    _git("-C", str(path), "config", "user.email", "test@example.invalid")
    _git("-C", str(path), "config", "user.name", "AFlow Test")
    _git("-C", str(path), "add", ".")
    _git("-C", str(path), "commit", "-qm", "base")


def _advance_origin(path: Path, marker: str) -> str:
    (path / "marker.txt").write_text(marker + "\n")
    _git("-C", str(path), "add", ".")
    _git("-C", str(path), "commit", "-qm", marker)
    return _git_rev(path, "main")


@contextmanager
def _ci_api(runs: list[dict[str, object]]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self.path.startswith("/repos/evrenesat/aworkflow/actions/runs"):
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"total_count": len(runs), "workflow_runs": runs}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _ci_runs(kind: str, candidate: str) -> list[dict[str, object]]:
    good = {
        "head_sha": candidate,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "path": CI_PATH,
    }
    if kind == "success":
        return [good]
    if kind == "no_runs":
        return []
    if kind == "pending":
        return [{**good, "status": "in_progress", "conclusion": None}]
    if kind == "failed":
        return [{**good, "conclusion": "failure"}]
    if kind == "wrong_sha":
        return [{**good, "head_sha": "b" * 40}]
    if kind == "wrong_event":
        return [{**good, "event": "pull_request"}]
    if kind == "wrong_branch":
        return [{**good, "head_branch": "feature"}]
    raise AssertionError(f"unknown CI scenario: {kind}")


def _expected_ci_phase(kind: str) -> str:
    return "deferred" if kind == "failed" else "waiting_for_ci"


def _release_root(path: Path, commit: str) -> Path:
    release = path / "releases" / commit
    release.mkdir(parents=True)
    (release / "release-manifest.sha256").write_text(f"source_commit={commit}\n")
    current = path / "current"
    if current.is_symlink() or current.exists():
        current.unlink()
    current.symlink_to(release)
    return path


def _poll_args(env: SimpleNamespace) -> list[str]:
    return [
        "--state-dir", str(env.state_dir),
        "--release-root", str(env.tmp_path / "aflowd"),
        "--service-path", str(env.tmp_path / "aflowd.service"),
        "--environment-file", str(env.tmp_path / "aflowd.env"),
        "--managed-projects-root", str(env.tmp_path / "managed"),
        "--project-registry-path", str(env.tmp_path / "projects.json"),
        "--project-config-root", str(env.tmp_path / "project" / ".aflow" / "config"),
        "--backend-url", "http://127.0.0.1:8765",
    ]


def _read_status(state_dir: Path) -> dict[str, object]:
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


def _mark_lines(mark: Path) -> list[str]:
    if not mark.exists():
        return []
    return [line for line in mark.read_text(encoding="utf-8").splitlines() if line.strip()]


def _install_lines(mark: Path) -> list[str]:
    return [line for line in _mark_lines(mark) if line.startswith("install")]


def _process_terminated(pid: int) -> bool:
    """True once the process is gone or only an unreaped zombie remains."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def _assert_process_terminated(pid: int, description: str) -> None:
    deadline = time.monotonic() + 5.0
    while not _process_terminated(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _process_terminated(pid), f"{description} (pid {pid}) survived the installer timeout"


@pytest.fixture()
def poller():
    return _load_module()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poller) -> SimpleNamespace:
    origin = tmp_path / "origin"
    _fake_origin(origin)
    base = _git_rev(origin, "main")
    candidate = _advance_origin(origin, "candidate")
    mark = tmp_path / "invocations.txt"
    monkeypatch.setenv("DEPLOY_CD_MARK", str(mark))
    monkeypatch.delenv("DEPLOY_CD_PREFLIGHT_FAIL", raising=False)
    monkeypatch.delenv("DEPLOY_CD_PREFLIGHT_SNAPSHOT", raising=False)
    monkeypatch.delenv("DEPLOY_CD_PREFLIGHT_OMIT_SNAPSHOT", raising=False)
    monkeypatch.delenv("DEPLOY_CD_PREFLIGHT_SLEEP", raising=False)
    monkeypatch.delenv("DEPLOY_CD_INSTALLER_FAIL", raising=False)
    monkeypatch.delenv("DEPLOY_CD_INSTALLER_HANG", raising=False)
    monkeypatch.setattr(poller, "REMOTE_URL", str(origin))
    return SimpleNamespace(
        poller=poller,
        tmp_path=tmp_path,
        origin=origin,
        base=base,
        candidate=candidate,
        state_dir=tmp_path / "state",
        mark=mark,
    )


def test_equal_candidate_reports_up_to_date_without_ci_or_installer(env: SimpleNamespace) -> None:
    _release_root(env.tmp_path / "aflowd", env.candidate)
    env.poller.API_BASE = "http://127.0.0.1:1"
    result = env.poller.main(_poll_args(env))
    assert result == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "up_to_date"
    assert status["candidate_commit"] == env.candidate
    assert status["current_commit"] == env.candidate
    assert _mark_lines(env.mark) == []


def test_validated_candidate_deploys_once_with_preflight_snapshot_and_log(env: SimpleNamespace) -> None:
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        result = env.poller.main(_poll_args(env))
    assert result == 0
    status = _read_status(env.state_dir)
    assert status["schema_version"] == 1
    assert status["phase"] == "deployed"
    assert status["current_commit"] == env.candidate
    assert status["candidate_commit"] == env.candidate
    lines = _mark_lines(env.mark)
    assert len(lines) == 2 and lines[0] == "preflight"
    install = lines[1]
    assert install.startswith("install ")
    assert " --apply" in install
    assert " --preflight-snapshot" in install
    assert "--skip-service" not in install and "--skip-readiness" not in install and "--stage-only" not in install
    assert f" --commit {env.candidate}" in install
    attempts = list((env.state_dir / "attempts").iterdir())
    assert len(attempts) == 1
    assert (attempts[0] / "preflight.json").is_file()
    assert (attempts[0] / "installer.log").is_file()
    assert status["attempt_dir"] == str(attempts[0])
    assert status["installer_log"] == str(attempts[0] / "installer.log")
    assert list((env.state_dir / "tmp").iterdir()) == []


@pytest.mark.parametrize("kind", ["no_runs", "pending", "failed", "wrong_sha", "wrong_event", "wrong_branch"])
def test_unvalidated_ci_never_calls_the_installer(env: SimpleNamespace, kind: str) -> None:
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs(kind, env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        result = env.poller.main(_poll_args(env))
    assert result == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == _expected_ci_phase(kind)
    assert status["candidate_commit"] == env.candidate
    assert _install_lines(env.mark) == []
    assert not (env.state_dir / "attempts").exists()


def test_divergent_and_unknown_current_commit_defer_without_installer(env: SimpleNamespace) -> None:
    _release_root(env.tmp_path / "aflowd", "a" * 40)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        unknown = env.poller.main(_poll_args(env))
    assert unknown == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deferred"
    assert "absent from fetched history" in status["reason"]
    assert _install_lines(env.mark) == []

    _git("-C", str(env.origin), "checkout", "-q", "--detach", env.base)
    (env.origin / "rewritten.txt").write_text("rewritten\n")
    _git("-C", str(env.origin), "add", ".")
    _git("-C", str(env.origin), "commit", "-qm", "rewritten")
    rewritten = _git_rev(env.origin, "HEAD")
    _git("-C", str(env.origin), "update-ref", "refs/heads/main", rewritten)
    _git("-C", str(env.origin), "checkout", "-q", "main")

    # The old main tip is still an object in the poller clone but a rewritten
    # main no longer descends from it.
    _release_root(env.tmp_path / "aflowd", env.candidate)
    with _ci_api(_ci_runs("success", rewritten)) as api_base:
        env.poller.API_BASE = api_base
        not_descendant = env.poller.main(_poll_args(env))
    assert not_descendant == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deferred"
    assert "not a descendant" in status["reason"]
    assert _install_lines(env.mark) == []


UNSAFE_PREFLIGHT_SNAPSHOT = '{"schema_version": 1, "safe_to_rollout": false}'


def test_unsafe_preflight_snapshot_defers_discards_temp_and_never_installs(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEPLOY_CD_PREFLIGHT_SNAPSHOT", UNSAFE_PREFLIGHT_SNAPSHOT)
    monkeypatch.setenv("DEPLOY_CD_PREFLIGHT_FAIL", "1")
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        result = env.poller.main(_poll_args(env))
    assert result == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deferred"
    assert "preflight refused" in status["reason"]
    lines = _mark_lines(env.mark)
    assert lines == ["preflight"]
    assert not (env.state_dir / "attempts").exists()
    assert list((env.state_dir / "tmp").iterdir()) == []


OPERATIONAL_PREFLIGHT_CASES = {
    "missing": None,
    "malformed": "not json",
    "wrong_schema": '{"schema_version": 2, "safe_to_rollout": false}',
    "safe_but_failed": '{"schema_version": 1, "safe_to_rollout": true}',
}


@pytest.mark.parametrize("snapshot", OPERATIONAL_PREFLIGHT_CASES.values(), ids=OPERATIONAL_PREFLIGHT_CASES)
def test_operationally_failed_preflight_fails_poll_without_installer(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, snapshot: str | None
) -> None:
    monkeypatch.setenv("DEPLOY_CD_PREFLIGHT_FAIL", "1")
    if snapshot is None:
        monkeypatch.setenv("DEPLOY_CD_PREFLIGHT_OMIT_SNAPSHOT", "1")
    else:
        monkeypatch.setenv("DEPLOY_CD_PREFLIGHT_SNAPSHOT", snapshot)
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        result = env.poller.main(_poll_args(env))
    assert result == 1
    status = _read_status(env.state_dir)
    assert status["phase"] == "failed"
    assert "unsafe snapshot" in status["reason"]
    assert _mark_lines(env.mark) == ["preflight"]
    assert not (env.state_dir / "attempts").exists()
    assert list((env.state_dir / "tmp").iterdir()) == []


def test_preflight_timeout_fails_poll_and_discards_temp(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.poller, "PREFLIGHT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setenv("DEPLOY_CD_PREFLIGHT_SLEEP", "15")
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        result = env.poller.main(_poll_args(env))
    assert result == 1
    status = _read_status(env.state_dir)
    assert status["phase"] == "failed"
    assert "timed out" in status["reason"]
    assert _mark_lines(env.mark) == ["preflight"]
    assert not (env.state_dir / "attempts").exists()
    assert list((env.state_dir / "tmp").iterdir()) == []


def test_dirty_source_is_preserved_and_refused(env: SimpleNamespace) -> None:
    source = env.state_dir / "source"
    source.parent.mkdir(parents=True)
    _git("clone", "-q", str(env.origin), str(source))
    (source / "README.md").write_text("dirty tracked change\n")
    (source / "untracked.txt").write_text("dirty untracked file\n")
    result = env.poller.main(_poll_args(env))
    assert result == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deferred"
    assert "uncommitted changes" in status["reason"]
    assert (source / "README.md").read_text() == "dirty tracked change\n"
    assert (source / "untracked.txt").read_text() == "dirty untracked file\n"
    assert _mark_lines(env.mark) == []
    assert not (env.state_dir / "attempts").exists()


def test_unexpected_source_directories_defer_without_modification(env: SimpleNamespace) -> None:
    source = env.state_dir / "source"
    source.mkdir(parents=True)
    (source / "keep.txt").write_text("keep\n")
    result = env.poller.main(_poll_args(env))
    assert result == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deferred"
    assert "not a Git work tree" in status["reason"]
    assert (source / "keep.txt").read_text() == "keep\n"
    assert _mark_lines(env.mark) == []

    shutil.rmtree(source)
    _git("clone", "-q", str(env.origin), str(source))
    _git("-C", str(source), "remote", "set-url", "origin", "/nonexistent/foreign.git")
    foreign = env.poller.main(_poll_args(env))
    assert foreign == 0
    status = _read_status(env.state_dir)
    assert "origin is not the expected repository" in status["reason"]
    assert _mark_lines(env.mark) == []


def test_concurrent_invocation_is_skipped_without_status_change(
    env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded = {"schema_version": 1, "phase": "up_to_date", "reason": "seeded"}
    env.state_dir.mkdir(parents=True)
    (env.state_dir / "status.json").write_text(json.dumps(seeded))
    lock = (env.state_dir / "lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = env.poller.main(_poll_args(env))
    finally:
        lock.close()
    assert result == 0
    assert "another poll holds the deployment lock" in capsys.readouterr().out
    assert json.loads((env.state_dir / "status.json").read_text(encoding="utf-8")) == seeded


def test_installer_failure_records_truthful_status(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOY_CD_INSTALLER_FAIL", "1")
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        result = env.poller.main(_poll_args(env))
    assert result == 1
    status = _read_status(env.state_dir)
    assert status["phase"] == "failed"
    assert status["last_failed_commit"] == env.candidate
    assert status["candidate_commit"] == env.candidate
    assert Path(str(status["installer_log"])).is_file()
    assert Path(str(status["attempt_dir"])).is_dir()
    current = env.tmp_path / "aflowd" / "current"
    assert current.resolve().name == env.base


def test_failed_rollout_is_suppressed_until_retry_or_new_candidate(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEPLOY_CD_INSTALLER_FAIL", "1")
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        assert env.poller.main(_poll_args(env)) == 1

    monkeypatch.delenv("DEPLOY_CD_INSTALLER_FAIL", raising=False)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        suppressed = env.poller.main(_poll_args(env))
    assert suppressed == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deferred"
    assert "--retry-failed" in status["reason"]
    assert len(_install_lines(env.mark)) == 1

    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        retried = env.poller.main([*_poll_args(env), "--retry-failed"])
    assert retried == 0
    assert _read_status(env.state_dir)["phase"] == "deployed"
    assert len(_install_lines(env.mark)) == 2

    new_candidate = _advance_origin(env.origin, "next candidate")
    with _ci_api(_ci_runs("success", new_candidate)) as api_base:
        env.poller.API_BASE = api_base
        moved_on = env.poller.main(_poll_args(env))
    assert moved_on == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deployed"
    assert status["candidate_commit"] == new_candidate
    assert status["last_failed_commit"] == env.candidate
    assert len(_install_lines(env.mark)) == 3


def test_installer_timeout_terminates_whole_process_group_and_suppresses_candidate(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.poller, "INSTALLER_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setenv("DEPLOY_CD_INSTALLER_HANG", "1")
    child_file = env.tmp_path / "installer-child.pid"
    parent_file = env.tmp_path / "installer-parent.pid"
    monkeypatch.setenv("DEPLOY_CD_INSTALLER_CHILD", str(child_file))
    monkeypatch.setenv("DEPLOY_CD_INSTALLER_PARENT", str(parent_file))
    _release_root(env.tmp_path / "aflowd", env.base)
    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        result = env.poller.main(_poll_args(env))
    assert result == 1
    status = _read_status(env.state_dir)
    assert status["phase"] == "failed"
    assert status["last_failed_commit"] == env.candidate
    assert len(_install_lines(env.mark)) == 1
    assert Path(str(status["installer_log"])).is_file()
    assert Path(str(status["attempt_dir"])).is_dir()
    _assert_process_terminated(int(parent_file.read_text().strip()), "installer")
    _assert_process_terminated(int(child_file.read_text().strip()), "installer descendant")

    with _ci_api(_ci_runs("success", env.candidate)) as api_base:
        env.poller.API_BASE = api_base
        suppressed = env.poller.main(_poll_args(env))
    assert suppressed == 0
    status = _read_status(env.state_dir)
    assert status["phase"] == "deferred"
    assert "--retry-failed" in status["reason"]
    assert len(_install_lines(env.mark)) == 1


@pytest.mark.parametrize("final_phase", ["deployed", "waiting_for_ci"])
def test_final_status_write_failure_returns_nonzero(
    env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    final_phase: str,
) -> None:
    real_write_status = env.poller._write_status

    def failing_write(state_dir: Path, status: dict[str, object]) -> None:
        if status["phase"] == final_phase:
            raise OSError(28, "No space left on device")
        real_write_status(state_dir, status)

    monkeypatch.setattr(env.poller, "_write_status", failing_write)
    _release_root(env.tmp_path / "aflowd", env.base)
    if final_phase == "deployed":
        with _ci_api(_ci_runs("success", env.candidate)) as api_base:
            env.poller.API_BASE = api_base
            result = env.poller.main(_poll_args(env))
    else:
        with _ci_api(_ci_runs("pending", env.candidate)) as api_base:
            env.poller.API_BASE = api_base
            result = env.poller.main(_poll_args(env))
    assert result == 1
    output = capsys.readouterr().out
    assert "status write failed" in output
    assert f"phase={final_phase}" in output
    assert _read_status(env.state_dir)["phase"] != final_phase


def _unit_fixture_release(tmp_path: Path) -> Path:
    root = tmp_path / "aflowd"
    commit = "c" * 40
    release = root / "releases" / commit
    (release / "src" / "deploy" / "aflowd").mkdir(parents=True)
    shutil.copy(MODULE_PATH, release / "src" / "deploy" / "aflowd" / "continuous-deploy.py")
    manifest = [f"source_commit={commit}"]
    digest = hashlib.sha256((release / "src/deploy/aflowd/continuous-deploy.py").read_bytes()).hexdigest()
    manifest.append(f"{digest}  src/deploy/aflowd/continuous-deploy.py")
    (release / "release-manifest.sha256").write_text("\n".join(manifest) + "\n")
    (root / "current").symlink_to(release)
    return root


def _recorder_path(tmp_path: Path, name: str, body: str) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    script = tools / name
    script.write_text(body)
    script.chmod(0o755)
    return tools


def test_unit_files_pin_oneshot_poll_and_timer_schedule() -> None:
    service = (DEPLOY / "aflowd-deploy.service").read_text()
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/bin/python3 /opt/aflowd/current/src/deploy/aflowd/continuous-deploy.py" in service
    assert "Environment=PATH=" in service
    assert "After=network-online.target" in service
    timer = (DEPLOY / "aflowd-deploy.timer").read_text()
    assert "OnBootSec=2min" in timer
    assert "OnUnitInactiveSec=5min" in timer
    assert "Unit=aflowd-deploy.service" in timer
    assert "WantedBy=timers.target" in timer


@pytest.mark.parametrize("name", ["continuous-deploy.py", "install-continuous-deploy.sh"])
def test_continuous_deploy_scripts_are_executable(name: str) -> None:
    assert os.access(DEPLOY / name, os.X_OK)


def test_install_script_dry_run_and_apply_behaviour(tmp_path: Path) -> None:
    root = _unit_fixture_release(tmp_path)
    units = tmp_path / "units"
    calls = tmp_path / "systemctl-calls.txt"
    tools = _recorder_path(
        tmp_path,
        "systemctl",
        f"""#!/bin/sh
printf '%s\\n' "$*" >>{shlex.quote(str(calls))}
exit 0
""",
    )
    script = DEPLOY / "install-continuous-deploy.sh"
    base_env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}

    help_result = _run("bash", str(script), "--help")
    assert help_result.returncode == 0 and "dry-run by default" in help_result.stdout

    dry = _run("bash", str(script), "--root", str(root), "--unit-dir", str(units))
    assert dry.returncode == 0, dry.stderr
    assert "dry-run: no units were installed" in dry.stdout
    assert not units.exists()

    applied = _run("bash", str(script), "--root", str(root), "--unit-dir", str(units), "--apply", env=base_env)
    assert applied.returncode == 0, applied.stderr
    assert (units / "aflowd-deploy.service").is_file()
    assert (units / "aflowd-deploy.timer").is_file()
    recorded = calls.read_text()
    assert "daemon-reload" in recorded
    assert "enable --now aflowd-deploy.timer" in recorded
    assert "restart" not in recorded

    stale_root = tmp_path / "empty-root"
    missing = _run("bash", str(script), "--root", str(stale_root))
    assert missing.returncode != 0 and "current release link is missing" in missing.stderr


@pytest.mark.parametrize("parent_exits", [False, True])
@pytest.mark.parametrize("timeout_reason", [None, "fetching origin main timed out"])
def test_generic_timeout_stops_term_ignoring_child(
    tmp_path: Path, poller, parent_exits: bool, timeout_reason: str | None,
) -> None:
    child_file = tmp_path / "child.pid"
    child_code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"path=Path({str(child_file)!r}); "
        "\nwhile not path.exists(): time.sleep(0.01)\n"
        + ("sys.exit(0)" if parent_exits else "child.wait()")
    )
    error = poller.Deferral if timeout_reason else poller.PollerError
    started = time.monotonic()
    try:
        with pytest.raises(error, match="timed out"):
            poller._run([sys.executable, "-c", parent_code], 1.0, timeout_reason)
        assert time.monotonic() - started < 5
        assert child_file.exists(), "test child did not start"
        _assert_process_terminated(int(child_file.read_text()), "command descendant")
    finally:
        if child_file.exists():
            pid = int(child_file.read_text())
            if not _process_terminated(pid):
                os.kill(pid, signal.SIGKILL)

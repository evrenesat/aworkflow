#!/usr/bin/env python3
"""One-shot validated-main deployment poll for the p100 aflowd release.

The poller deploys an exact commit from the fixed public repository's main
branch only when every trusted boundary holds: the candidate is the exact
fetched main commit, it descends from the currently installed release, and
GitHub Actions recorded a completed successful push-to-main run of
.github/workflows/ci.yml for that exact SHA. Any other outcome defers with a
truthful status and leaves the running service untouched. The rollout itself
is performed by the candidate's own preflight.sh and install.sh; this script
owns no deployment or rollback logic of its own.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REMOTE_URL = "https://github.com/evrenesat/aworkflow.git"
OWNER_REPO = "evrenesat/aworkflow"
API_BASE = "https://api.github.com"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"

DEFAULT_STATE_DIR = Path("/var/lib/aflowd/deploy")
DEFAULT_RELEASE_ROOT = Path("/opt/aflowd")
DEFAULT_SERVICE_PATH = Path("/etc/systemd/system/aflowd.service")
DEFAULT_ENVIRONMENT_FILE = Path("/etc/aflowd/aflowd.env")
DEFAULT_MANAGED_PROJECTS_ROOT = Path("/root/code")
DEFAULT_PROJECT_REGISTRY_PATH = Path("/var/lib/aflowd/projects.json")
DEFAULT_PROJECT_CONFIG_ROOT = Path("/root/code/aflow-control-plane-proof-20260811/.aflow/config")
DEFAULT_BACKEND_URL = "http://127.0.0.1:8765"

GIT_TIMEOUT_SECONDS = 120.0
GIT_QUICK_TIMEOUT_SECONDS = 30.0
PREFLIGHT_TIMEOUT_SECONDS = 300.0
INSTALLER_TIMEOUT_SECONDS = 3600.0
INSTALLER_REAP_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 15.0
HTTP_MAX_BYTES = 1_048_576
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class Deferral(Exception):
    """An expected condition that leaves the current service untouched."""

    def __init__(self, phase: str, reason: str) -> None:
        super().__init__(reason)
        self.phase = phase
        self.reason = reason


class PollerError(Exception):
    """An actual poll error that should fail the systemd unit."""


class RolloutFailed(Exception):
    """The installer attempted a rollout and reported failure."""

    def __init__(self, attempt_dir: Path, installer_log: Path) -> None:
        super().__init__(str(installer_log))
        self.attempt_dir = attempt_dir
        self.installer_log = installer_log


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _journal(status: dict[str, object]) -> None:
    print(
        f"aflowd-deploy: phase={status['phase']} reason={status['reason']}",
        flush=True,
    )


def _write_status(state_dir: Path, status: dict[str, object]) -> None:
    path = state_dir / "status.json"
    pending = state_dir / "status.json.new"
    payload = json.dumps(status, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    os.replace(pending, path)


def _finish(state_dir: Path, status: dict[str, object], phase: str, reason: str) -> int:
    status["phase"] = phase
    status["reason"] = reason
    status["checked_at"] = _utc_now()
    persisted = True
    try:
        _write_status(state_dir, status)
    except OSError as exc:
        persisted = False
        print(f"aflowd-deploy: status write failed: {type(exc).__name__}", flush=True)
    _journal(status)
    if not persisted:
        # The durable status is stale; never report nominal success for it.
        return 1
    return 0 if phase in {"up_to_date", "waiting_for_ci", "deferred", "deployed"} else 1


def _load_previous_status(state_dir: Path) -> dict[str, object]:
    try:
        payload = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _bounded(text: str, limit: int = 200) -> str:
    """One printable line of subprocess output for the journal, never secrets."""
    for line in text.splitlines():
        if line.strip():
            cleaned = "".join(character if character.isprintable() else " " for character in line)
            return cleaned.strip()[:limit]
    return ""


def _run(command: list[str], timeout: float, timeout_reason: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except FileNotFoundError:
        raise PollerError(f"{command[0]} is unavailable") from None
    except subprocess.TimeoutExpired:
        if timeout_reason is not None:
            raise Deferral("deferred", timeout_reason) from None
        raise PollerError(f"{command[0]} timed out after {int(timeout)}s") from None


def _prepare_source(state_dir: Path) -> Path:
    """Ensure the poller-owned clone exists, matches origin, and is clean."""
    source = state_dir / "source"
    if os.path.lexists(source):
        inside = _run(
            ["git", "-C", str(source), "rev-parse", "--is-inside-work-tree"],
            GIT_QUICK_TIMEOUT_SECONDS,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise Deferral("deferred", "deployment clone is not a Git work tree")
        origin = _run(["git", "-C", str(source), "remote", "get-url", "origin"], GIT_QUICK_TIMEOUT_SECONDS)
        if origin.returncode != 0 or origin.stdout.strip() != REMOTE_URL:
            raise Deferral("deferred", "deployment clone origin is not the expected repository")
    else:
        cloned = _run(
            ["git", "clone", "--quiet", REMOTE_URL, str(source)],
            GIT_TIMEOUT_SECONDS,
            timeout_reason="cloning origin timed out",
        )
        if cloned.returncode != 0:
            raise Deferral("deferred", "creating the deployment clone from origin failed")
    status = _run(["git", "-C", str(source), "status", "--porcelain"], GIT_QUICK_TIMEOUT_SECONDS)
    if status.returncode != 0:
        raise Deferral("deferred", "deployment clone state could not be inspected")
    if status.stdout.strip():
        raise Deferral("deferred", "deployment clone has uncommitted changes; refusing to reset or clean")
    return source


def _fetch_candidate(source: Path) -> str:
    fetched = _run(
        ["git", "-C", str(source), "fetch", "--quiet", "origin", "main"],
        GIT_TIMEOUT_SECONDS,
        timeout_reason="fetching origin main timed out",
    )
    if fetched.returncode != 0:
        detail = _bounded(fetched.stderr)
        if detail:
            print(f"aflowd-deploy: origin fetch failed: {detail}", flush=True)
        raise Deferral("deferred", "fetching origin main failed")
    resolved = _run(
        ["git", "-C", str(source), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        GIT_QUICK_TIMEOUT_SECONDS,
    )
    candidate = resolved.stdout.strip()
    if resolved.returncode != 0 or not COMMIT_PATTERN.match(candidate):
        raise PollerError("fetched origin main does not resolve to an exact commit")
    return candidate


def _current_release_commit(release_root: Path) -> str:
    current = release_root / "current"
    if not current.is_symlink():
        raise Deferral("deferred", "current release link is missing or is not a symlink")
    manifest = current / "release-manifest.sha256"
    if not manifest.is_file():
        raise Deferral("deferred", "current release manifest is unavailable")
    lines = manifest.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise Deferral("deferred", "current release manifest has no source_commit")
    match = re.fullmatch(r"source_commit=([0-9a-f]{40})", lines[0].strip())
    if match is None:
        raise Deferral("deferred", "current release manifest has no source_commit")
    return match.group(1)


def _require_descendant(source: Path, current: str, candidate: str) -> None:
    ancestry = _run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", current, candidate],
        GIT_QUICK_TIMEOUT_SECONDS,
    )
    if ancestry.returncode == 0:
        return
    if ancestry.returncode == 1:
        raise Deferral("deferred", "candidate is not a descendant of the current release")
    raise Deferral("deferred", "current release commit is absent from fetched history")


def _http_get_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aflowd-continuous-deploy",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise Deferral("deferred", f"GitHub Actions API returned HTTP {response.status}")
            raw = response.read(HTTP_MAX_BYTES + 1)
    except Deferral:
        raise
    except urllib.error.HTTPError as exc:
        raise Deferral("deferred", f"GitHub Actions API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError):
        raise Deferral("deferred", "GitHub Actions API is unreachable") from None
    if len(raw) > HTTP_MAX_BYTES:
        raise Deferral("deferred", "GitHub Actions API returned invalid data")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Deferral("deferred", "GitHub Actions API returned invalid data") from None
    if not isinstance(payload, dict):
        raise Deferral("deferred", "GitHub Actions API returned invalid data")
    return payload


def _require_validated_ci(candidate: str) -> None:
    query = urllib.parse.urlencode(
        {"head_sha": candidate, "branch": "main", "event": "push", "per_page": "100"}
    )
    payload = _http_get_json(f"{API_BASE}/repos/{OWNER_REPO}/actions/runs?{query}")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise Deferral("deferred", "GitHub Actions API returned invalid data")
    qualifying = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == candidate
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("path") == CI_WORKFLOW_PATH
    ]
    if any(run.get("status") == "completed" and run.get("conclusion") == "success" for run in qualifying):
        return
    if any(run.get("status") != "completed" for run in qualifying):
        raise Deferral("waiting_for_ci", "CI run for candidate has not completed")
    if qualifying:
        raise Deferral("deferred", "CI for candidate completed unsuccessfully")
    raise Deferral("waiting_for_ci", "no completed CI run for candidate yet")


def _detach_candidate(source: Path, candidate: str) -> None:
    detached = _run(
        ["git", "-C", str(source), "checkout", "--detach", "--quiet", candidate],
        GIT_QUICK_TIMEOUT_SECONDS,
    )
    if detached.returncode != 0:
        raise PollerError("checking out the validated candidate failed")
    status = _run(["git", "-C", str(source), "status", "--porcelain"], GIT_QUICK_TIMEOUT_SECONDS)
    if status.returncode != 0 or status.stdout.strip():
        raise PollerError("deployment clone is dirty after checkout; refusing to deploy")


def _preflight_arguments(source: Path, preflight_dir: Path, args: argparse.Namespace) -> list[str]:
    return [
        str(source / "deploy" / "aflowd" / "preflight.sh"),
        "--output-dir", str(preflight_dir),
        "--current-backend-url", args.backend_url,
        "--project-config-root", str(args.project_config_root),
        "--root", str(args.release_root),
        "--service-path", str(args.service_path),
        "--environment-file", str(args.environment_file),
        "--registry-path", str(args.project_registry_path),
    ]


def _installer_arguments(source: Path, candidate: str, attempt_dir: Path, args: argparse.Namespace) -> list[str]:
    return [
        str(source / "deploy" / "aflowd" / "install.sh"),
        "--source", str(source),
        "--commit", candidate,
        "--root", str(args.release_root),
        "--service-path", str(args.service_path),
        "--environment-file", str(args.environment_file),
        "--managed-projects-root", str(args.managed_projects_root),
        "--project-registry-path", str(args.project_registry_path),
        "--preflight-snapshot", str(attempt_dir),
        "--apply",
    ]


def _attempt_directory(state_dir: Path, candidate: str) -> Path:
    attempts = state_dir / "attempts"
    attempts.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt_dir = attempts / f"{stamp}-{candidate[:12]}"
    suffix = 0
    while attempt_dir.exists():
        suffix += 1
        attempt_dir = attempts / f"{stamp}-{candidate[:12]}-{suffix}"
    return attempt_dir


def _preflight_snapshot_is_unsafe(preflight_dir: Path) -> bool:
    """Decode a nonzero preflight's snapshot conservatively.

    Only a structurally valid schema-1 snapshot that explicitly records
    ``safe_to_rollout: false`` counts as an expected active-or-ambiguous-run
    deferral. Missing, malformed, or safe-but-failed output is an operational
    fault, not a deferral.
    """
    try:
        payload = json.loads((preflight_dir / "preflight.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return False
    return payload.get("safe_to_rollout") is False


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Stop a timed-out command and its descendants before releasing ownership."""
    # start_new_session makes the original PID the stable group ID even if
    # the parent already exited while a descendant still owns an output pipe.
    group = process.pid
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=INSTALLER_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # Parent exit does not prove descendants exited. Always finish the group,
    # including children that ignore TERM, before returning to the poll loop.
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _deploy(source: Path, candidate: str, state_dir: Path, status: dict[str, object], args: argparse.Namespace) -> None:
    """Run the candidate's own preflight and installer; raise RolloutFailed on failure."""
    _detach_candidate(source, candidate)
    temporary_root = state_dir / "tmp"
    temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    preflight_dir = Path(tempfile.mkdtemp(prefix="preflight-", dir=temporary_root))
    # Reserve a unique name, then let preflight create its own new directory.
    preflight_dir.rmdir()
    try:
        preflight = _run(_preflight_arguments(source, preflight_dir, args), PREFLIGHT_TIMEOUT_SECONDS)
    except PollerError:
        shutil.rmtree(preflight_dir, ignore_errors=True)
        raise
    if preflight.returncode != 0:
        # Refused and failed preflights are poller-owned temporaries; discard them.
        detail = _bounded(preflight.stderr or preflight.stdout)
        unsafe = _preflight_snapshot_is_unsafe(preflight_dir)
        shutil.rmtree(preflight_dir, ignore_errors=True)
        if unsafe:
            if detail:
                print(f"aflowd-deploy: preflight refused rollout: {detail}", flush=True)
            raise Deferral("deferred", "preflight refused rollout; current service untouched")
        print("aflowd-deploy: preflight failed without a valid unsafe snapshot", flush=True)
        raise PollerError("preflight failed without a valid unsafe snapshot")

    attempt_dir = _attempt_directory(state_dir, candidate)
    os.replace(preflight_dir, attempt_dir)
    installer_log = attempt_dir / "installer.log"
    status["attempt_dir"] = str(attempt_dir)
    status["installer_log"] = str(installer_log)
    status["phase"] = "deploying"
    status["reason"] = "installer is running from the validated candidate"
    status["checked_at"] = _utc_now()
    _write_status(state_dir, status)
    _journal(status)

    with installer_log.open("w", encoding="utf-8") as log:
        installer = subprocess.Popen(
            _installer_arguments(source, candidate, attempt_dir, args),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return_code = installer.wait(timeout=INSTALLER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_process_group(installer)
            raise RolloutFailed(attempt_dir, installer_log) from None
    if return_code != 0:
        raise RolloutFailed(attempt_dir, installer_log)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy validated main to the aflowd release when no run is active.",
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--release-root", type=Path, default=DEFAULT_RELEASE_ROOT)
    parser.add_argument("--service-path", type=Path, default=DEFAULT_SERVICE_PATH)
    parser.add_argument("--environment-file", type=Path, default=DEFAULT_ENVIRONMENT_FILE)
    parser.add_argument("--managed-projects-root", type=Path, default=DEFAULT_MANAGED_PROJECTS_ROOT)
    parser.add_argument("--project-registry-path", type=Path, default=DEFAULT_PROJECT_REGISTRY_PATH)
    parser.add_argument("--project-config-root", type=Path, default=DEFAULT_PROJECT_CONFIG_ROOT)
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="allow an explicit reattempt of the candidate that last failed rollout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    state_dir = args.state_dir
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    lock_handle = (state_dir / "lock").open("a+")
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("aflowd-deploy: another poll holds the deployment lock; skipping", flush=True)
            return 0

        previous = _load_previous_status(state_dir)
        last_failed = previous.get("last_failed_commit")
        status: dict[str, object] = {
            "schema_version": 1,
            "checked_at": _utc_now(),
            "current_commit": None,
            "candidate_commit": None,
            "phase": "checking",
            "reason": "poll started",
            "last_failed_commit": last_failed if isinstance(last_failed, str) else None,
            "attempt_dir": None,
            "installer_log": None,
        }
        _write_status(state_dir, status)
        _journal(status)
        try:
            source = _prepare_source(state_dir)
            candidate = _fetch_candidate(source)
            current = _current_release_commit(args.release_root)
            status["candidate_commit"] = candidate
            status["current_commit"] = current
            status["checked_at"] = _utc_now()
            _write_status(state_dir, status)

            if candidate == current:
                return _finish(state_dir, status, "up_to_date", "current release equals validated main")
            _require_descendant(source, current, candidate)
            if status["last_failed_commit"] == candidate and not args.retry_failed:
                raise Deferral(
                    "deferred",
                    "candidate previously failed rollout; rerun with --retry-failed to reattempt",
                )
            _require_validated_ci(candidate)

            _deploy(source, candidate, state_dir, status, args)
            # A successful installer is authoritative: the candidate is active now.
            status["current_commit"] = candidate
            return _finish(state_dir, status, "deployed", "installer reported success")
        except Deferral as exc:
            return _finish(state_dir, status, exc.phase, exc.reason)
        except RolloutFailed as exc:
            status["attempt_dir"] = str(exc.attempt_dir)
            status["installer_log"] = str(exc.installer_log)
            status["last_failed_commit"] = status["candidate_commit"]
            return _finish(
                state_dir,
                status,
                "failed",
                "installer failed; release unchanged or rolled back; see installer log",
            )
        except PollerError as exc:
            return _finish(state_dir, status, "failed", str(exc))
        except Exception as exc:
            return _finish(state_dir, status, "failed", f"unexpected error: {type(exc).__name__}")
    finally:
        lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())

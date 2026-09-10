from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "aflowd"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="aflowd deployment scripts require Linux systemd and GNU tools",
)


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("fixture\n")
    for command in (
        ("git", "init", "-q", str(path)),
        ("git", "-C", str(path), "config", "user.email", "test@example.invalid"),
        ("git", "-C", str(path), "config", "user.name", "AFlow Test"),
        ("git", "-C", str(path), "add", "."),
        ("git", "-C", str(path), "commit", "-qm", "fixture"),
    ):
        assert _run(*command).returncode == 0
    return path


def _git_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    (source / "apps" / "aflow_app" / "server").mkdir(parents=True)
    (source / "apps" / "aflow_app" / "web").mkdir(parents=True)
    (source / "deploy" / "aflowd").mkdir(parents=True)
    (source / "apps" / "aflow_app" / "server" / "pyproject.toml").write_text("[project]\nname='fake'\nversion='0'\n")
    (source / "apps" / "aflow_app" / "web" / "package.json").write_text("{}\n")
    (source / "apps" / "aflow_app" / "web" / "package-lock.json").write_text("{}\n")
    _write_executable(source / "deploy" / "aflowd" / "validate-runtime.sh", "#!/bin/sh\nexit 0\n")
    _git_repo(source)
    commit = _run("git", "-C", str(source), "rev-parse", "HEAD").stdout.strip()
    return source, commit


def _fake_build_tools(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(
        tools / "uv",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "venv" ]]; then
  destination=""
  for value in "$@"; do destination="$value"; done
  mkdir -p "$destination/bin"
  printf '#!/bin/sh\\nexit 0\\n' >"$destination/bin/python"
  chmod 0755 "$destination/bin/python"
elif [[ "$1" == "sync" ]]; then
  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
fi
""",
    )
    _write_executable(
        tools / "npm",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "run" && "$2" == "build" ]]; then
  mkdir -p dist
  printf '<!doctype html>\\n' >dist/index.html
fi
""",
    )
    return tools, {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "AFLOWD_READINESS_DELAY_SECONDS": "0"}


def _project_and_token(tmp_path: Path, name: str = "project") -> tuple[Path, Path, Path, Path]:
    managed = tmp_path / "managed"
    project = _git_repo(managed / name)
    legacy = project / "legacy-config"
    legacy.mkdir()
    aflow = legacy / "aflow.toml"
    workflows = legacy / "workflows.toml"
    aflow.write_text("[aflow]\nworkflow = 'default'\n")
    workflows.write_text("[workflows.default]\nsteps = ['implement']\n")
    token = tmp_path / f"{name}.env"
    token.write_text("AFLOW_APP_TOKEN=opaque-token\n")
    token.chmod(0o600)
    return project, aflow, workflows, token


def _install_args(source: Path, commit: str, state_root: Path, managed: Path, token: Path, tools: Path) -> list[str]:
    return [
        "bash", str(DEPLOY / "install.sh"),
        "--source", str(source),
        "--commit", commit,
        "--root", str(state_root),
        "--service-path", str(state_root / "aflowd.service"),
        "--environment-file", str(token),
        "--managed-projects-root", str(managed),
        "--project-registry-path", str(state_root / "projects.json"),
        "--uv", str(tools / "uv"),
        "--npm", str(tools / "npm"),
    ]


def _release_fixture(tmp_path: Path, release_id: str = "b" * 40) -> Path:
    release = tmp_path / "releases" / release_id
    (release / "bin").mkdir(parents=True)
    (release / "config").mkdir()
    (release / "src" / "apps" / "aflow_app" / "web" / "dist").mkdir(parents=True)
    for entrypoint in ("aflow", "aflow-app-server"):
        _write_executable(release / "bin" / entrypoint, "#!/bin/sh\nexit 0\n")
    (release / "config" / "config.toml").write_text("[server]\n")
    (release / "src" / "apps" / "aflow_app" / "web" / "dist" / "index.html").write_text("ok\n")
    manifest = [f"source_commit={release_id}"]
    for relative in (
        "bin/aflow", "bin/aflow-app-server", "config/config.toml", "src/apps/aflow_app/web/dist/index.html"
    ):
        digest = hashlib.sha256((release / relative).read_bytes()).hexdigest()
        manifest.append(f"{digest}  {relative}")
    (release / "release-manifest.sha256").write_text("\n".join(manifest) + "\n")
    return release


def test_package_scripts_omit_aflowd_but_retain_aflow_entrypoints() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]

    assert "aflowd" not in scripts
    assert scripts["aflow"] == "aflow.cli:main"
    assert scripts["aworkflow"] == "aflow.cli:main"


def _registry_record(project_id: str, relative_root: str) -> dict[str, object]:
    timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "id": project_id,
        "display_name": project_id.title(),
        "relative_root": relative_root,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _preflight_snapshot(path: Path, current_release: str | None) -> Path:
    path.mkdir()
    (path / "preflight.json").write_text(json.dumps({
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "current_release": current_release,
        "active_workflow_units": [],
        "active_controllers": [],
        "unsafe_runs": [],
        "project_errors": {},
        "safe_to_rollout": True,
    }))
    return path


def test_installer_dry_run_names_registry_and_loopback_without_writing(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    state_root = tmp_path / "aflowd"
    result = _run("bash", str(DEPLOY / "install.sh"), "--source", str(source), "--commit", commit, "--root", str(state_root))
    assert result.returncode == 0, result.stderr
    assert f"release destination: {state_root}/releases/{commit}" in result.stdout
    assert "backend bind: 127.0.0.1:8765 (Tailscale Serve target)" in result.stdout
    assert "managed projects root: /root/code" in result.stdout
    assert "project registry: /var/lib/aflowd/projects.json" in result.stdout
    assert "Tailscale mappings" in result.stdout
    assert not state_root.exists()


def test_fresh_install_is_immutable_and_registry_backed(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    tools, env = _fake_build_tools(tmp_path)
    project, _, _, token = _project_and_token(tmp_path)
    state_root = tmp_path / "aflowd"
    staged = _run(*_install_args(source, commit, state_root, project.parent, token, tools), "--stage-only", "--apply", env=env)
    assert staged.returncode == 0, staged.stderr
    assert not (state_root / "current").exists()
    result = _run(*_install_args(source, commit, state_root, project.parent, token, tools), "--apply", "--skip-service", "--skip-readiness", env=env)
    assert result.returncode == 0, result.stderr
    release = state_root / "releases" / commit
    assert (state_root / "current").resolve() == release.resolve()
    parsed = tomllib.loads((release / "config" / "config.toml").read_text())
    assert parsed["server"] == {"bind_host": "127.0.0.1", "bind_port": 8765}
    assert "project_catalog" not in parsed and "planning" not in parsed
    assert parsed["control_plane"]["managed_projects_root"] == str(project.parent)
    assert parsed["control_plane"]["project_registry_path"] == str(state_root / "projects.json")
    assert parsed["control_plane"]["aflow_executable"] == f"{release}/bin/aflow"
    assert parsed["control_plane"]["release_identity"] == commit
    assert "/current/" not in (release / "config" / "config.toml").read_text()
    manifest_entries = {
        line.split("  ", 1)[1]
        for line in (release / "release-manifest.sha256").read_text().splitlines()
        if "  " in line
    }
    assert not (release / "bin" / "aflowd").exists()
    assert {"bin/aflow", "bin/aflow-app-server"} <= manifest_entries
    assert "bin/aflowd" not in manifest_entries
    for entrypoint in ("aflow", "aflow-app-server"):
        assert os.access(release / "bin" / entrypoint, os.X_OK)
    (release / "bin" / "aflow").write_text("corrupt\n")
    stale = _run(*_install_args(source, commit, state_root, project.parent, token, tools), "--apply", "--skip-service", "--skip-readiness", env=env)
    assert stale.returncode != 0
    assert "release snapshot hashes are stale" in stale.stderr


def test_install_validates_a_multiseed_registry_and_rejects_invalid_inventory(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    tools, env = _fake_build_tools(tmp_path)
    first, _, _, token = _project_and_token(tmp_path, "first")
    second = _git_repo(first.parent / "second")
    state_root = tmp_path / "aflowd"
    state_root.mkdir()
    registry = state_root / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": [_registry_record("first", "first"), _registry_record("second", "second")]}) + "\n")
    result = _run(*_install_args(source, commit, state_root, first.parent, token, tools), "--apply", "--skip-service", "--skip-readiness", env=env)
    assert result.returncode == 0, result.stderr
    registry.write_text("{broken\n")
    invalid = _run(*_install_args(source, commit, state_root, first.parent, token, tools), "--apply", "--skip-service", "--skip-readiness", env=env)
    assert invalid.returncode != 0
    assert "project registry is invalid" in invalid.stderr
    assert second.exists()


def test_service_rollout_requires_preflight_and_rolls_back_failed_readiness(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    tools, env = _fake_build_tools(tmp_path)
    project, _, _, token = _project_and_token(tmp_path)
    state_root = tmp_path / "aflowd"
    old_release = state_root / "releases" / ("a" * 40)
    old_release.mkdir(parents=True)
    (state_root / "current").symlink_to(old_release)
    service_path = state_root / "aflowd.service"
    service_path.write_text("old service\n")
    _write_executable(tools / "systemctl", "#!/bin/sh\nexit 0\n")
    _write_executable(tools / "curl", "#!/bin/sh\nexit 22\n")
    missing = _run(*_install_args(source, commit, state_root, project.parent, token, tools), "--apply", env=env)
    assert missing.returncode != 0 and "preflight" in missing.stderr
    snapshot = _preflight_snapshot(tmp_path / "preflight", str(old_release.resolve()))
    failed = _run(*_install_args(source, commit, state_root, project.parent, token, tools), "--preflight-snapshot", str(snapshot), "--apply", env=env)
    assert failed.returncode != 0
    assert (state_root / "current").resolve() == old_release.resolve()
    assert service_path.read_text() == "old service\n"
    assert "rolled back" in failed.stderr


def test_service_template_uses_exact_writable_paths_and_loopback(tmp_path: Path) -> None:
    service = (DEPLOY / "aflowd.service").read_text()
    assert "Environment=AFLOW_APP_HOST=127.0.0.1" in service
    assert "ConditionPathIsDirectory=@MANAGED_PROJECTS_ROOT@" in service
    assert "ReadWritePaths=@MANAGED_PROJECTS_ROOT@ /var/lib/aflowd" in service
    assert "/@" not in service
    assert "100.103.69.9" not in service and "tailscale0" not in service
    assert "NoNewPrivileges=yes" in service and "ProtectSystem=strict" in service
    assert "@RELEASE_DIR@/bin/aflow-app-server" in service
    example = DEPLOY / "aflow-control-plane.mcp.example.toml"
    assert _run("python3", str(DEPLOY / "validate-mcp-config.py"), str(example)).returncode == 0
    unsafe = tmp_path / "unsafe.toml"
    unsafe.write_text(example.read_text().replace("https://", "http://"))
    rejected = _run("python3", str(DEPLOY / "validate-mcp-config.py"), str(unsafe))
    assert rejected.returncode != 0 and "MagicDNS HTTPS" in rejected.stderr


def test_runtime_validator_checks_loopback_registry_and_release_snapshot(tmp_path: Path) -> None:
    release = _release_fixture(tmp_path)
    project, _, _, token = _project_and_token(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": [_registry_record("project", "project")]}) + "\n")
    config = release / "config" / "config.toml"
    config.write_text(
        "[server]\nbind_host = '127.0.0.1'\nbind_port = 8765\n"
        "[control_plane]\n"
        f'managed_projects_root = "{project.parent}"\n'
        f'project_registry_path = "{registry}"\n'
        f'aflow_executable = "{release}/bin/aflow"\n'
        f'environment_file = "{token}"\n'
        f'release_identity = "{release.name}"\n'
        'environment = { HOME = "/root" }\n'
    )
    _rewrite_manifest(release)
    args = ["bash", str(DEPLOY / "validate-runtime.sh"), "--release", str(release), "--config", str(config), "--environment-file", str(token), "--managed-projects-root", str(project.parent), "--project-registry-path", str(registry)]
    assert _run(*args).returncode == 0
    invalid_bind = _run(*args, "--bind-address", "0.0.0.0")
    assert invalid_bind.returncode != 0 and "127.0.0.1" in invalid_bind.stderr
    (release / "bin" / "aflow").write_text("stale\n")
    stale = _run(*args)
    assert stale.returncode != 0 and "snapshot hashes are stale" in stale.stderr


def _rewrite_manifest(release: Path) -> None:
    lines = [f"source_commit={release.name}"]
    for relative in ("bin/aflow", "bin/aflow-app-server", "config/config.toml", "src/apps/aflow_app/web/dist/index.html"):
        lines.append(f"{hashlib.sha256((release / relative).read_bytes()).hexdigest()}  {relative}")
    (release / "release-manifest.sha256").write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("missing", ["aflow", "aflow-app-server"])
def test_runtime_validator_requires_retained_entrypoints(
    tmp_path: Path, missing: str
) -> None:
    release = _release_fixture(tmp_path)
    project, _, _, token = _project_and_token(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps(
            {"schema_version": 1, "projects": [_registry_record("project", "project")]}
        )
        + "\n"
    )
    config = release / "config" / "config.toml"
    config.write_text(
        "[server]\nbind_host = '127.0.0.1'\nbind_port = 8765\n"
        "[control_plane]\n"
        f'managed_projects_root = "{project.parent}"\n'
        f'project_registry_path = "{registry}"\n'
        f'aflow_executable = "{release}/bin/aflow"\n'
        f'environment_file = "{token}"\n'
        f'release_identity = "{release.name}"\n'
        'environment = { HOME = "/root" }\n'
    )
    _rewrite_manifest(release)
    (release / "bin" / missing).unlink()

    result = _run(
        "bash",
        str(DEPLOY / "validate-runtime.sh"),
        "--release",
        str(release),
        "--config",
        str(config),
        "--environment-file",
        str(token),
        "--managed-projects-root",
        str(project.parent),
        "--project-registry-path",
        str(registry),
    )

    assert result.returncode != 0
    assert f"release entrypoint is not a regular executable: {missing}" in result.stderr


def _migration_args(release: Path, project: Path, aflow: Path, workflows: Path, registry: Path, backup_root: Path) -> list[str]:
    return [
        "python3", str(DEPLOY / "migrate-registry.py"), "prepare",
        "--release", str(release), "--managed-projects-root", str(project.parent),
        "--project-root", str(project), "--project-id", project.name,
        "--display-name", project.name.title(), "--source-aflow-config", str(aflow),
        "--source-workflows-config", str(workflows), "--registry-path", str(registry),
        "--backup-root", str(backup_root),
    ]


def test_migration_publishes_config_and_registry_then_rolls_back_without_runs_or_plans(tmp_path: Path) -> None:
    release = _release_fixture(tmp_path)
    project, aflow, workflows, _ = _project_and_token(tmp_path)
    (project / ".aflow" / "runs" / "run-1").mkdir(parents=True)
    (project / ".aflow" / "runs" / "run-1" / "state.json").write_text("keep\n")
    (project / "plans" / "in-progress").mkdir(parents=True)
    (project / "plans" / "in-progress" / "plan.md").write_text("keep\n")
    registry = tmp_path / "state" / "projects.json"
    registry.parent.mkdir()
    result = _run(*_migration_args(release, project, aflow, workflows, registry, tmp_path / "backups"), "--apply")
    assert result.returncode == 0, result.stderr
    assert (project / ".aflow" / "config" / "aflow.toml").read_bytes() == aflow.read_bytes()
    assert json.loads(registry.read_text())["projects"][0]["relative_root"] == project.name
    transaction = Path(next(line.split(": ", 1)[1] for line in result.stdout.splitlines() if line.startswith("migration transaction:")))
    rollback = _run("python3", str(DEPLOY / "migrate-registry.py"), "rollback", "--transaction", str(transaction), "--apply")
    assert rollback.returncode == 0, rollback.stderr
    assert not registry.exists() and not (project / ".aflow" / "config").exists()
    assert (project / ".aflow" / "runs" / "run-1" / "state.json").read_text() == "keep\n"
    assert (project / "plans" / "in-progress" / "plan.md").read_text() == "keep\n"


def test_migration_appends_to_multiseed_registry_and_refuses_overwrite(tmp_path: Path) -> None:
    release = _release_fixture(tmp_path)
    project, aflow, workflows, _ = _project_and_token(tmp_path, "second")
    _git_repo(project.parent / "first")
    registry = tmp_path / "state" / "projects.json"
    registry.parent.mkdir()
    registry.write_text(json.dumps({"schema_version": 1, "projects": [_registry_record("first", "first")]}) + "\n")
    result = _run(*_migration_args(release, project, aflow, workflows, registry, tmp_path / "backups"), "--apply")
    assert result.returncode == 0, result.stderr
    assert [item["id"] for item in json.loads(registry.read_text())["projects"]] == ["first", "second"]
    (project / ".aflow" / "config" / "aflow.toml").write_text("different\n")
    before = registry.read_bytes()
    refused = _run(*_migration_args(release, project, aflow, workflows, registry, tmp_path / "more-backups"), "--apply")
    assert refused.returncode != 0
    assert registry.read_bytes() == before


def test_migration_rejects_invalid_source_config_without_publishing(tmp_path: Path) -> None:
    release = _release_fixture(tmp_path)
    project, aflow, workflows, _ = _project_and_token(tmp_path, "invalid")
    aflow.write_text("broken = [\n")
    registry = tmp_path / "state" / "projects.json"
    registry.parent.mkdir()
    result = _run(*_migration_args(release, project, aflow, workflows, registry, tmp_path / "backups"), "--apply")
    assert result.returncode != 0
    assert "valid UTF-8 TOML" in result.stderr
    assert not registry.exists()
    assert not (project / ".aflow" / "config").exists()


def test_interrupted_migration_automatically_removes_only_published_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("aflowd_migration", DEPLOY / "migrate-registry.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    release = _release_fixture(tmp_path)
    project, aflow, workflows, _ = _project_and_token(tmp_path)
    (project / ".aflow" / "runs").mkdir(parents=True)
    (project / ".aflow" / "runs" / "keep").write_text("keep\n")
    registry = tmp_path / "state" / "projects.json"
    registry.parent.mkdir()
    original = module._write_atomic
    failed = False

    def interrupt(path: Path, data: bytes, mode: int = 0o600) -> None:
        nonlocal failed
        if Path(path) == registry and not failed:
            failed = True
            raise OSError("simulated interruption")
        original(path, data, mode)

    monkeypatch.setattr(module, "_write_atomic", interrupt)
    args = argparse.Namespace(
        release=str(release), managed_projects_root=str(project.parent), project_root=str(project),
        project_id=project.name, display_name="Project", source_aflow_config=str(aflow),
        source_workflows_config=str(workflows), registry_path=str(registry),
        backup_root=str(tmp_path / "backups"), apply=True,
    )
    with pytest.raises(OSError, match="simulated interruption"):
        module.prepare(args)
    assert not registry.exists() and not (project / ".aflow" / "config").exists()
    assert (project / ".aflow" / "runs" / "keep").read_text() == "keep\n"


def test_preflight_reads_canonical_state_and_fails_closed_on_unsafe_run(tmp_path: Path) -> None:
    project, aflow, workflows, token = _project_and_token(tmp_path)
    config_root = project / ".aflow" / "config"
    config_root.mkdir(parents=True)
    config_root.joinpath("aflow.toml").write_bytes(aflow.read_bytes())
    config_root.joinpath("workflows.toml").write_bytes(workflows.read_bytes())
    state_root = tmp_path / "state"
    release = _release_fixture(state_root)
    (state_root / "current").symlink_to(release)
    service = tmp_path / "aflowd.service"
    service.write_text("fixture\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(tools / "systemctl", "#!/bin/sh\nexit 0\n")
    _write_executable(tools / "pgrep", "#!/bin/sh\nexit 1\n")
    _write_executable(tools / "tailscale", "#!/bin/sh\nprintf '{}\\n'\n")
    run_status = {"value": "completed"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Authorization") != "Bearer opaque-token":
                self.send_response(401); self.end_headers(); return
            if self.path == "/ready":
                payload = {"ready": True, "project_errors": {}}
            elif self.path == "/api/control-plane/projects":
                payload = {"projects": [{"project_id": "project"}]}
            elif self.path.startswith("/api/control-plane/projects/project/runs"):
                payload = {"runs": [{"run_id": "run-1", "status": run_status["value"], "ownership": "control_plane", "launch_phase": run_status.get("phase", run_status["value"])}], "next_cursor": None}
            else:
                self.send_response(404); self.end_headers(); return
            data = json.dumps(payload).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    base_args = [
        "bash", str(DEPLOY / "preflight.sh"), "--current-backend-url", f"http://127.0.0.1:{server.server_port}",
        "--root", str(state_root), "--service-path", str(service), "--environment-file", str(token),
        "--registry-path", str(tmp_path / "missing-registry.json"), "--project-config-root", str(config_root),
    ]
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}
    try:
        safe = _run(*base_args, "--output-dir", str(tmp_path / "safe"), env=env)
        assert safe.returncode == 0, safe.stderr
        canonical = (tmp_path / "safe" / "canonical-state.json").read_text()
        assert "opaque-token" not in canonical
        run_status["value"] = "needs_attention"
        unsafe = _run(*base_args, "--output-dir", str(tmp_path / "unsafe"), env=env)
        assert unsafe.returncode != 0
        assert json.loads((tmp_path / "unsafe" / "preflight.json").read_text())["safe_to_rollout"] is False
        run_status.update(value="awaiting_startup_answer", phase="owner_stopped")
        stopped = _run(*base_args, "--output-dir", str(tmp_path / "stopped"), env=env)
        assert stopped.returncode == 0, stopped.stderr
        stopped_state = json.loads((tmp_path / "stopped" / "preflight.json").read_text())
        assert stopped_state["safe_to_rollout"] is True
        assert stopped_state["unsafe_runs"] == []

        _write_executable(
            tools / "systemctl",
            "#!/bin/sh\n"
            "if [ \"$1\" = list-units ]; then "
            "printf 'aflow-run-run-1.service loaded active running fixture\\n'; fi\n",
        )
        active = _run(*base_args, "--output-dir", str(tmp_path / "active"), env=env)
        assert active.returncode != 0
        active_state = json.loads((tmp_path / "active" / "preflight.json").read_text())
        assert active_state["safe_to_rollout"] is False
        assert active_state["active_workflow_units"] == ["aflow-run-run-1.service"]
    finally:
        server.shutdown(); thread.join(timeout=1); server.server_close()


def test_private_serve_restore_refuses_drift_and_restores_exact_snapshot(tmp_path: Path) -> None:
    tools = tmp_path / "tools"; tools.mkdir()
    state = tmp_path / "serve-state.json"
    before = tmp_path / "before.json"; before.write_text('{"before": 1}\n'); state.write_bytes(before.read_bytes())
    post = tmp_path / "post.json"; post.write_text('{"https": {"443": "http://127.0.0.1:8765"}}\n')
    serve_status = tmp_path / "serve-status.json"; serve_status.write_text('{"HTTPS": {"443": {"Proxy": "http://127.0.0.1:8765"}}}\n')
    tailscale_status = tmp_path / "tailscale-status.json"; tailscale_status.write_text('{"Self": {"DNSName": "node.tailnet.ts.net."}}\n')
    _write_executable(tools / "tailscale", """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == "serve --bg --https=443 127.0.0.1:8765" ]]; then cp "$POST" "$STATE"
elif [[ "$*" == "serve get-config --all" ]]; then cat "$STATE"
elif [[ "$*" == "serve status --json" ]]; then cat "$SERVE_STATUS"
elif [[ "$*" == "status --json" ]]; then cat "$TAILSCALE_STATUS"
elif [[ "$1 $2 $3" == "serve set-config --all" ]]; then cp "$4" "$STATE"
else exit 2
fi
""")
    env = {**os.environ, "STATE": str(state), "POST": str(post), "SERVE_STATUS": str(serve_status), "TAILSCALE_STATUS": str(tailscale_status)}
    evidence = tmp_path / "evidence"
    applied = _run("bash", str(DEPLOY / "serve-private-https.sh"), "--snapshot", str(before), "--evidence-dir", str(evidence), "--tailscale", str(tools / "tailscale"), "--apply", env=env)
    assert applied.returncode == 0, applied.stderr
    expected = evidence / "tailscale-serve.after-config.json"
    state.write_text('{"unrelated": "drift"}\n')
    drift = _run("bash", str(DEPLOY / "serve-private-https.sh"), "--rollback", "--snapshot", str(before), "--expected-current", str(expected), "--tailscale", str(tools / "tailscale"), "--apply", env=env)
    assert drift.returncode != 0 and "drifted" in drift.stderr
    assert json.loads(state.read_text()) == {"unrelated": "drift"}
    state.write_bytes(expected.read_bytes())
    restored = _run("bash", str(DEPLOY / "serve-private-https.sh"), "--rollback", "--snapshot", str(before), "--expected-current", str(expected), "--tailscale", str(tools / "tailscale"), "--apply", env=env)
    assert restored.returncode == 0, restored.stderr
    assert json.loads(state.read_text()) == {"before": 1}


def test_status_rejects_stale_release_snapshot(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    release = _release_fixture(state_root)
    (state_root / "current").symlink_to(release)
    tools = tmp_path / "tools"; tools.mkdir()
    _write_executable(tools / "systemctl", "#!/bin/sh\nexit 0\n")
    _write_executable(tools / "ss", "#!/bin/sh\necho 'LISTEN 0 128 127.0.0.1:8765 0.0.0.0:*'\n")
    _write_executable(tools / "tailscale", """#!/bin/sh
if [ "$1 $2 $3" = "serve status --json" ]; then echo '{"HTTPS":{"443":{"Proxy":"http://127.0.0.1:8765"}}}'
else echo '{"Self":{"DNSName":"node.tailnet.ts.net."}}'; fi
""")
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}
    ok = _run("bash", str(DEPLOY / "status.sh"), "--root", str(state_root), "--tailscale", str(tools / "tailscale"), env=env)
    assert ok.returncode == 0, ok.stderr
    (release / "bin" / "aflow").write_text("stale\n")
    stale = _run("bash", str(DEPLOY / "status.sh"), "--root", str(state_root), "--tailscale", str(tools / "tailscale"), env=env)
    assert stale.returncode != 0 and "snapshot is stale" in stale.stderr


def _add_release_file(release: Path, relative: str, content: str, *, executable: bool = False) -> Path:
    path = release / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if executable:
        path.chmod(0o755)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with (release / "release-manifest.sha256").open("a") as manifest:
        manifest.write(f"{digest}  {relative}\n")
    return path


def _service_unit(release: Path, *, legacy: bool) -> str:
    if legacy:
        project_args = (
            "--project-root /root/code/aflow-control-plane-proof-20260811 "
            "--project-config /root/code/aflow-control-plane-proof-20260811/aflow/aflow.toml"
        )
        bind_address = "100.103.69.9"
        write_paths = "/root/code/aflow-control-plane-proof-20260811 /var/lib/aflowd"
    else:
        project_args = (
            "--managed-projects-root /root/code "
            "--project-registry-path /var/lib/aflowd/projects.json"
        )
        bind_address = "127.0.0.1"
        write_paths = "/root/code /var/lib/aflowd"
    return (
        "[Unit]\n"
        "Description=AFlow daemon-backed control plane\n"
        f"Documentation=file:{release}/src/deploy/aflowd/README.md\n"
        "[Service]\n"
        f"WorkingDirectory={release}\n"
        f"Environment=AFLOW_APP_CONFIG_DIR={release}/config\n"
        f"Environment=AFLOW_APP_WEB_DIST={release}/src/apps/aflow_app/web/dist\n"
        f"Environment=AFLOW_APP_HOST={bind_address}\n"
        f"Environment=PATH={release}/bin:/usr/bin:/bin\n"
        f"ExecStartPre=/usr/bin/env {release}/src/deploy/aflowd/validate-runtime.sh "
        f"--release {release} --config {release}/config/config.toml "
        f"--environment-file /etc/aflowd/aflowd.env {project_args} "
        f"--interface tailscale0 --bind-address {bind_address}\n"
        f"ExecStart=/usr/bin/env {release}/bin/aflow-app-server\n"
        f"ReadWritePaths={write_paths}\n"
    )


def test_rollback_restores_exact_legacy_service_snapshot(tmp_path: Path) -> None:
    state_root = tmp_path / "aflowd"
    active_release = _release_fixture(state_root, "a" * 40)
    selected_release = _release_fixture(state_root, "b" * 40)
    _add_release_file(
        active_release,
        "src/deploy/aflowd/validate-runtime.sh",
        "#!/bin/sh\nexit 0\n",
        executable=True,
    )
    _add_release_file(
        selected_release,
        "src/deploy/aflowd/validate-runtime.sh",
        """#!/bin/sh
case " $* " in
  *" --managed-projects-root "*|*" --project-registry-path "*) exit 91 ;;
esac
case " $* " in *" --project-root "*" --project-config "*) ;; *) exit 92 ;; esac
case " $* " in *" --bind-address 100.103.69.9 "*) ;; *) exit 93 ;; esac
printf '%s\n' "$*" >"$VALIDATOR_ARGS"
""",
        executable=True,
    )
    (state_root / "current").symlink_to(active_release)
    service_path = state_root / "aflowd.service"
    service_path.write_text(_service_unit(active_release, legacy=False))
    snapshot = tmp_path / "aflowd.service.before"
    snapshot.write_text(_service_unit(selected_release, legacy=True))
    validator_args = tmp_path / "validator-args.txt"
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(
        tools / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "restart" ]]; then
  command=$(sed -n 's/^ExecStartPre=//p' "$SERVICE_PATH")
  bash -c "$command"
fi
""",
    )
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "SERVICE_PATH": str(service_path),
        "VALIDATOR_ARGS": str(validator_args),
    }
    result = _run(
        "bash",
        str(DEPLOY / "rollback.sh"),
        "--root",
        str(state_root),
        "--service-path",
        str(service_path),
        "--release",
        selected_release.name,
        "--service-snapshot",
        str(snapshot),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert (state_root / "current").resolve() == selected_release.resolve()
    assert service_path.read_bytes() == snapshot.read_bytes()
    assert "--project-root" in validator_args.read_text()
    assert "--managed-projects-root" not in validator_args.read_text()
    assert "--bind-address 100.103.69.9" in validator_args.read_text()


def test_rollback_failure_restores_pre_attempt_service_and_link(tmp_path: Path) -> None:
    state_root = tmp_path / "aflowd"
    active_release = _release_fixture(state_root, "a" * 40)
    selected_release = _release_fixture(state_root, "b" * 40)
    for release in (active_release, selected_release):
        _add_release_file(
            release,
            "src/deploy/aflowd/validate-runtime.sh",
            "#!/bin/sh\nexit 0\n",
            executable=True,
        )
    (state_root / "current").symlink_to(active_release)
    service_path = state_root / "aflowd.service"
    original_service = _service_unit(active_release, legacy=False)
    service_path.write_text(original_service)
    snapshot = tmp_path / "aflowd.service.before"
    snapshot.write_text(_service_unit(selected_release, legacy=True))
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(
        tools / "systemctl",
        """#!/usr/bin/env bash
if [[ "$1" == "restart" ]] && grep -Fq "$SELECTED_RELEASE" "$SERVICE_PATH"; then
  exit 1
fi
exit 0
""",
    )
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "SERVICE_PATH": str(service_path),
        "SELECTED_RELEASE": str(selected_release),
    }
    result = _run(
        "bash",
        str(DEPLOY / "rollback.sh"),
        "--root",
        str(state_root),
        "--service-path",
        str(service_path),
        "--release",
        selected_release.name,
        "--service-snapshot",
        str(snapshot),
        env=env,
    )
    assert result.returncode != 0
    assert "restored" in result.stderr
    assert (state_root / "current").resolve() == active_release.resolve()
    assert service_path.read_text() == original_service


def test_rollback_rejects_service_snapshot_for_another_release(tmp_path: Path) -> None:
    state_root = tmp_path / "aflowd"
    active_release = _release_fixture(state_root, "a" * 40)
    selected_release = _release_fixture(state_root, "b" * 40)
    for release in (active_release, selected_release):
        _add_release_file(
            release,
            "src/deploy/aflowd/validate-runtime.sh",
            "#!/bin/sh\nexit 0\n",
            executable=True,
        )
    (state_root / "current").symlink_to(active_release)
    service_path = state_root / "aflowd.service"
    original_service = _service_unit(active_release, legacy=False)
    service_path.write_text(original_service)
    snapshot = tmp_path / "aflowd.service.before"
    snapshot.write_text(original_service)
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(tools / "systemctl", "#!/bin/sh\nexit 99\n")
    result = _run(
        "bash",
        str(DEPLOY / "rollback.sh"),
        "--root",
        str(state_root),
        "--service-path",
        str(service_path),
        "--release",
        selected_release.name,
        "--service-snapshot",
        str(snapshot),
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "snapshot is not pinned" in result.stderr
    assert (state_root / "current").resolve() == active_release.resolve()
    assert service_path.read_text() == original_service


@pytest.mark.parametrize("name", ("install.sh", "rollback.sh", "status.sh", "uninstall-emergency.sh", "validate-runtime.sh", "preflight.sh", "serve-private-https.sh", "migrate-registry.py", "validate-serve-status.py"))
def test_deploy_scripts_are_executable(name: str) -> None:
    assert os.access(DEPLOY / name, os.X_OK)

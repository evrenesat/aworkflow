from __future__ import annotations

import os
from pathlib import Path
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import subprocess
from threading import Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "aflowd"


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def _git_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    (source / "apps" / "aflow_app" / "server").mkdir(parents=True)
    (source / "apps" / "aflow_app" / "web").mkdir(parents=True)
    (source / "deploy" / "aflowd").mkdir(parents=True)
    (source / "apps" / "aflow_app" / "server" / "pyproject.toml").write_text("[project]\nname='fake'\nversion='0'\n")
    (source / "apps" / "aflow_app" / "web" / "package.json").write_text("{}\n")
    (source / "apps" / "aflow_app" / "web" / "package-lock.json").write_text("{}\n")
    _write_executable(source / "deploy" / "aflowd" / "validate-runtime.sh", "#!/bin/sh\nexit 0\n")
    for command in (
        ("git", "init", "-q", str(source)),
        ("git", "-C", str(source), "config", "user.email", "test@example.invalid"),
        ("git", "-C", str(source), "config", "user.name", "AFlow Test"),
        ("git", "-C", str(source), "add", "."),
        ("git", "-C", str(source), "commit", "-qm", "fixture"),
    ):
        assert _run(*command).returncode == 0
    commit = _run("git", "-C", str(source), "rev-parse", "HEAD").stdout.strip()
    return source, commit


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


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
    return tools, {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}


def _project_and_token(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    config = project / "aflow" / "aflow.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[aflow]\n")
    token = tmp_path / "aflowd.env"
    token.write_text("AFLOW_APP_TOKEN=opaque-token\n")
    token.chmod(0o600)
    return project, config, token


def _install_args(
    source: Path,
    commit: str,
    state_root: Path,
    project: Path,
    config: Path,
    token: Path,
    tools: Path,
) -> list[str]:
    return [
        "bash", str(DEPLOY / "install.sh"),
        "--source", str(source),
        "--commit", commit,
        "--root", str(state_root),
        "--service-path", str(state_root / "aflowd.service"),
        "--environment-file", str(token),
        "--project-root", str(project),
        "--project-config", str(config),
        "--uv", str(tools / "uv"),
        "--npm", str(tools / "npm"),
    ]


def test_installer_dry_run_names_every_live_target_without_writing(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    state_root = tmp_path / "aflowd"

    result = _run(
        "bash", str(DEPLOY / "install.sh"),
        "--source", str(source),
        "--commit", commit,
        "--root", str(state_root),
    )

    assert result.returncode == 0, result.stderr
    assert f"release source: {source}@{commit}" in result.stdout
    assert f"release destination: {state_root}/releases/{commit}" in result.stdout
    assert "service name: aflowd.service" in result.stdout
    assert "bind address: 100.103.69.9:8765 on tailscale0" in result.stdout
    assert "allowlist path: /root/code/aflow-control-plane" in result.stdout
    assert "rollback target:" in result.stdout
    assert not state_root.exists()


def test_staged_install_uses_release_realpaths_and_atomic_current(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    tools, env = _fake_build_tools(tmp_path)
    project, config, token = _project_and_token(tmp_path)
    state_root = tmp_path / "aflowd"

    result = _run(
        *_install_args(source, commit, state_root, project, config, token, tools),
        "--apply", "--skip-service", "--skip-readiness",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    release = state_root / "releases" / commit
    assert (state_root / "current").resolve() == release.resolve()
    assert "source_commit=" + commit in (release / "release-manifest.sha256").read_text()
    rendered = (release / "config" / "config.toml").read_text()
    assert f'aflow_executable = "{release}/bin/aflow"' in rendered
    assert f'release_identity = "{commit}"' in rendered
    assert "/current/" not in rendered
    for entrypoint in ("aflow", "aflowd", "aflow-app-server"):
        path = release / "bin" / entrypoint
        assert path.is_file() and os.access(path, os.X_OK) and not path.is_symlink()

    (release / "bin" / "aflow").write_text("#!/bin/sh\nexit 1\n")
    corrupted = _run(
        *_install_args(source, commit, state_root, project, config, token, tools),
        "--apply", "--skip-service", "--skip-readiness",
        env=env,
    )
    assert corrupted.returncode != 0
    assert "hashes do not match manifest" in corrupted.stderr


def test_failed_authenticated_readiness_restores_prior_current_and_service(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    tools, env = _fake_build_tools(tmp_path)
    project, config, token = _project_and_token(tmp_path)
    state_root = tmp_path / "aflowd"
    old_release = state_root / "releases" / ("a" * 40)
    old_release.mkdir(parents=True)
    (state_root / "current").symlink_to(old_release)
    service_path = state_root / "aflowd.service"
    service_path.write_text("old service\n")
    _write_executable(tools / "systemctl", "#!/bin/sh\nexit 0\n")
    _write_executable(tools / "ip", "#!/bin/sh\necho '1: tailscale0    inet 100.103.69.9/32'\n")
    curl_config_path = tmp_path / "curl-config-path"
    _write_executable(
        tools / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
while (($#)); do
  case "$1" in
    --config) printf '%s' "$2" >"$AFLOWD_TEST_CURL_CONFIG_PATH"; shift 2 ;;
    *) shift ;;
  esac
done
exit 22
""",
    )

    result = _run(
        *_install_args(source, commit, state_root, project, config, token, tools),
        "--apply",
        env={**env, "AFLOWD_TEST_CURL_CONFIG_PATH": str(curl_config_path)},
    )

    assert result.returncode != 0
    assert (state_root / "current").resolve() == old_release.resolve()
    assert service_path.read_text() == "old service\n"
    assert "rolled back" in result.stderr
    assert not Path(curl_config_path.read_text()).exists()


def test_non_default_install_renders_a_service_for_the_selected_paths(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    tools, env = _fake_build_tools(tmp_path)
    project, config, token = _project_and_token(tmp_path)
    state_root = tmp_path / "non-default-aflowd"
    _write_executable(tools / "systemctl", "#!/bin/sh\nexit 0\n")
    _write_executable(tools / "ip", "#!/bin/sh\necho '1: tailscale0    inet 100.103.69.9/32'\n")
    _write_executable(tools / "curl", "#!/bin/sh\nexit 0\n")

    result = _run(*_install_args(source, commit, state_root, project, config, token, tools), "--apply", env=env)

    assert result.returncode == 0, result.stderr
    release = state_root / "releases" / commit
    service = (state_root / "aflowd.service").read_text()
    assert f"Documentation=file:{release}/src/deploy/aflowd/README.md" in service
    assert f"WorkingDirectory={release}" in service
    assert f"Environment=AFLOW_APP_CONFIG_DIR={release}/config" in service
    assert f"Environment=AFLOW_APP_WEB_DIST={release}/src/apps/aflow_app/web/dist" in service
    assert f"Environment=PATH={release}/bin:" in service
    assert f"EnvironmentFile={token}" in service
    assert f"--release {release} --config {release}/config/config.toml" in service
    assert f"--environment-file {token} --project-root {project} --project-config {config}" in service
    assert f"ExecStart=/usr/bin/env {release}/bin/aflow-app-server" in service
    assert f"ConditionPathIsDirectory={project}" in service
    assert f"ReadWritePaths={project} /var/lib/aflowd" in service
    for default_path in ("/opt/aflowd/releases", "/etc/aflowd/aflowd.env", "/root/code/aflow-control-plane"):
        assert default_path not in service


def test_failed_same_commit_reinstall_restores_the_active_release_config(tmp_path: Path) -> None:
    source, commit = _git_source(tmp_path)
    tools, env = _fake_build_tools(tmp_path)
    first_project, first_config, first_token = _project_and_token(tmp_path)
    second_project = tmp_path / "second-project"
    second_config = second_project / "aflow" / "aflow.toml"
    second_config.parent.mkdir(parents=True)
    second_config.write_text("[aflow]\n")
    second_token = tmp_path / "second-aflowd.env"
    second_token.write_text("AFLOW_APP_TOKEN=second-opaque-token\n")
    second_token.chmod(0o600)
    state_root = tmp_path / "aflowd"
    _write_executable(tools / "systemctl", "#!/bin/sh\nexit 0\n")
    _write_executable(tools / "ip", "#!/bin/sh\necho '1: tailscale0    inet 100.103.69.9/32'\n")
    _write_executable(tools / "curl", "#!/bin/sh\nexit 0\n")

    initial = _run(
        *_install_args(source, commit, state_root, first_project, first_config, first_token, tools),
        "--apply",
        env=env,
    )
    assert initial.returncode == 0, initial.stderr
    release = state_root / "releases" / commit
    previous_config = (release / "config" / "config.toml").read_text()
    previous_service = (state_root / "aflowd.service").read_text()

    _write_executable(tools / "curl", "#!/bin/sh\nexit 22\n")
    failed = _run(
        *_install_args(source, commit, state_root, second_project, second_config, second_token, tools),
        "--apply",
        env=env,
    )

    assert failed.returncode != 0
    assert (state_root / "current").resolve() == release.resolve()
    assert (state_root / "aflowd.service").read_text() == previous_service
    assert (release / "config" / "config.toml").read_text() == previous_config
    assert str(second_project) not in previous_config
    assert str(second_token) not in previous_config


def test_runtime_validator_rejects_non_private_token_and_current_indirection(tmp_path: Path) -> None:
    release = tmp_path / ("b" * 40)
    (release / "bin").mkdir(parents=True)
    for entrypoint in ("aflow", "aflowd", "aflow-app-server"):
        _write_executable(release / "bin" / entrypoint, "#!/bin/sh\nexit 0\n")
    project, project_config, token = _project_and_token(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        f'root = "{project}"\nconfig_path = "{project_config}"\n'
        f'aflow_executable = "{release}/bin/aflow"\nrelease_identity = "{release.name}"\n'
    )

    success = _run(
        "bash", str(DEPLOY / "validate-runtime.sh"),
        "--release", str(release), "--config", str(config),
        "--environment-file", str(token), "--project-root", str(project),
        "--project-config", str(project_config), "--skip-interface-check",
    )
    assert success.returncode == 0, success.stderr

    token.chmod(0o640)
    private_failure = _run(
        "bash", str(DEPLOY / "validate-runtime.sh"),
        "--release", str(release), "--config", str(config),
        "--environment-file", str(token), "--project-root", str(project),
        "--project-config", str(project_config), "--skip-interface-check",
    )
    assert private_failure.returncode != 0
    assert "mode 0600" in private_failure.stderr

    token.chmod(0o600)
    config.write_text(config.read_text() + 'aflow_current = "/opt/aflowd/current/bin/aflow"\n')
    current_failure = _run(
        "bash", str(DEPLOY / "validate-runtime.sh"),
        "--release", str(release), "--config", str(config),
        "--environment-file", str(token), "--project-root", str(project),
        "--project-config", str(project_config), "--skip-interface-check",
    )
    assert current_failure.returncode != 0
    assert "current" in current_failure.stderr

    wildcard_failure = _run(
        "bash", str(DEPLOY / "validate-runtime.sh"),
        "--release", str(release), "--config", str(config),
        "--environment-file", str(token), "--project-root", "/*",
        "--project-config", str(project_config), "--skip-interface-check",
    )
    assert wildcard_failure.returncode != 0
    assert "non-wildcard" in wildcard_failure.stderr


def test_service_template_and_mcp_example_are_hardened_and_credential_free(tmp_path: Path) -> None:
    service = (DEPLOY / "aflowd.service").read_text()
    assert "Restart=always" in service
    assert "Restart=no" not in service
    assert "100.103.69.9" in service and "tailscale0" in service
    assert "/@RELEASE_DIR@/bin/aflow-app-server" in service
    assert "/opt/aflowd/current" not in service
    assert "NoNewPrivileges=yes" in service and "ProtectSystem=strict" in service
    assert "ReadWritePaths=/@PROJECT_ROOT@ /var/lib/aflowd" in service
    assert "Environment=AFLOW_APP_HOST=100.103.69.9" in service
    assert "--interface tailscale0" in service

    example = DEPLOY / "aflow-control-plane.mcp.example.toml"
    valid = _run("python3", str(DEPLOY / "validate-mcp-config.py"), str(example))
    assert valid.returncode == 0, valid.stderr
    unsafe = tmp_path / "unsafe-mcp.toml"
    unsafe.write_text(example.read_text().replace("/mcp\"", "/mcp?token=literal\""))
    rejected = _run("python3", str(DEPLOY / "validate-mcp-config.py"), str(unsafe))
    assert rejected.returncode != 0
    assert "credential-free" in rejected.stderr


def test_tailscale_only_smoke_rejects_loopback_and_eth0_and_requires_bearer() -> None:
    class ReadyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/ready":
                self.send_error(404)
            elif self.headers.get("Authorization") == "Bearer deployment-test-token":
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(401)
                self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("100.103.69.9", 0), ReadyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        connection = http.client.HTTPConnection("100.103.69.9", port, timeout=1)
        connection.request("GET", "/ready", headers={"Authorization": "Bearer deployment-test-token"})
        assert connection.getresponse().status == 200
        connection.close()
        missing = http.client.HTTPConnection("100.103.69.9", port, timeout=1)
        missing.request("GET", "/ready")
        assert missing.getresponse().status == 401
        missing.close()
        for address in ("127.0.0.1", "192.168.1.63"):
            with pytest.raises(OSError):
                socket.create_connection((address, port), timeout=0.25)
    finally:
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()


@pytest.mark.parametrize("name", ("install.sh", "rollback.sh", "status.sh", "uninstall-emergency.sh", "validate-runtime.sh"))
def test_deploy_scripts_are_executable(name: str) -> None:
    assert os.access(DEPLOY / name, os.X_OK)

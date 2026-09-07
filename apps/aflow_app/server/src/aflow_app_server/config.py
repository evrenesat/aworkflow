"""Server configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

@dataclass(frozen=True)
class ControlPlaneProjectConfig:
    """Legacy test helper; runtime project entries are no longer parsed."""
    id: str
    root: Path
    config_path: Path
    aflow_executable: Path
    environment_file: Path
    release_identity: str
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)

    def normalized(self) -> "ControlPlaneProjectConfig":
        root = self.root.expanduser().resolve()
        config_path = self.config_path.expanduser().resolve()
        executable = self.aflow_executable.expanduser().resolve()
        environment_file = self.environment_file.expanduser().resolve()
        if not _PROJECT_ID_PATTERN.fullmatch(self.id):
            raise ValueError("project id must be a path-safe slug")
        if not root.is_dir():
            raise ValueError("project root must be an existing directory")
        if self.config_path.is_symlink() or not config_path.is_file():
            raise ValueError("workflow configuration must be a regular file")
        if self.aflow_executable.is_symlink() or not executable.is_file():
            raise ValueError("aflow executable must be a regular file")
        if self.environment_file.is_symlink() or not environment_file.is_file():
            raise ValueError("daemon environment file must be a regular file")
        if not self.release_identity.strip():
            raise ValueError("release identity is required")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.environment.items()):
            raise ValueError("daemon environment must contain only string values")
        return ControlPlaneProjectConfig(
            id=self.id, root=root, config_path=config_path,
            aflow_executable=executable, environment_file=environment_file,
            release_identity=self.release_identity, environment=dict(self.environment),
        )

@dataclass(frozen=True)
class ServerConfig:
    """Configuration for the workflow control server."""
    bind_host: str
    bind_port: int
    auth_token: str = field(repr=False)
    auth_token_file: Path | None = None
    managed_projects_root: Path = field(default_factory=lambda: Path("~/code").expanduser())
    project_registry_path: Path = field(default_factory=lambda: Path("~/.config/aflow/projects.json").expanduser())
    config_audit_path: Path = field(default_factory=lambda: Path("~/.config/aflow/config_audit.jsonl").expanduser())
    aflow_executable: Path = field(default_factory=lambda: Path("/usr/bin/aflow"))
    release_identity: str = "unconfigured"
    environment_file: Path = field(default_factory=lambda: Path("/etc/aflowd.env"))
    control_plane_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    control_plane_projects: tuple[ControlPlaneProjectConfig, ...] = ()

    @classmethod
    def from_env(cls) -> "ServerConfig":
        config_dir = Path(os.environ.get("AFLOW_APP_CONFIG_DIR", "~/.config/aflow")).expanduser()
        config_file = config_dir / "config.toml"
        file_config: dict[str, Any] = {}
        if config_file.exists():
            with config_file.open("rb") as handle:
                file_config = tomllib.load(handle)
        server_section = file_config.get("server", {})
        control_plane_section = file_config.get("control_plane", {})
        if not isinstance(server_section, dict):
            raise ValueError("server config must be a table")
        if not isinstance(control_plane_section, dict):
            raise ValueError("control_plane config must be a table")
        if "projects" in control_plane_section:
            raise ValueError("control_plane.projects is no longer supported; seed the project registry explicitly")
        child_environment = control_plane_section.get("environment", {})
        if not isinstance(child_environment, dict):
            raise ValueError("control_plane.environment must be a table")
        token_file_value = os.environ.get("AFLOW_APP_TOKEN_FILE") or server_section.get("auth_token_file")
        return cls(
            bind_host=os.environ.get("AFLOW_APP_HOST", server_section.get("bind_host", "127.0.0.1")),
            bind_port=int(os.environ.get("AFLOW_APP_PORT", server_section.get("bind_port", 8765))),
            auth_token=os.environ.get("AFLOW_APP_TOKEN", server_section.get("auth_token", "")),
            auth_token_file=Path(token_file_value).expanduser() if token_file_value else None,
            managed_projects_root=Path(os.environ.get("AFLOW_MANAGED_PROJECTS_ROOT", control_plane_section.get("managed_projects_root", "~/code"))).expanduser(),
            project_registry_path=Path(os.environ.get("AFLOW_PROJECT_REGISTRY_PATH", control_plane_section.get("project_registry_path", str(config_dir / "projects.json")))).expanduser(),
            config_audit_path=Path(os.environ.get("AFLOW_CONFIG_AUDIT_PATH", control_plane_section.get("config_audit_path", str(config_dir / "config_audit.jsonl")))).expanduser(),
            aflow_executable=Path(os.environ.get("AFLOW_EXECUTABLE", control_plane_section.get("aflow_executable", "/usr/bin/aflow"))).expanduser(),
            release_identity=str(os.environ.get("AFLOW_RELEASE_IDENTITY", control_plane_section.get("release_identity", "unconfigured"))),
            environment_file=Path(os.environ.get("AFLOW_ENVIRONMENT_FILE", control_plane_section.get("environment_file", "/etc/aflowd.env"))).expanduser(),
            control_plane_environment={str(key): str(value) for key, value in child_environment.items()},
        )

    def current_auth_token(self) -> str:
        if self.auth_token_file is None:
            return self.auth_token
        path = self.auth_token_file.expanduser()
        if path.is_symlink() or not path.is_file():
            raise ValueError("authentication token file is unavailable")
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("authentication token file is unreadable") from exc
        if not token:
            raise ValueError("authentication token file is empty")
        return token

    def validate(self) -> list[str]:
        errors: list[str] = []
        try:
            if not self.current_auth_token():
                errors.append("authentication token is required")
        except ValueError:
            errors.append("authentication token file is unavailable")
        if self.bind_port < 1 or self.bind_port > 65535:
            errors.append(f"invalid bind_port: {self.bind_port}")
        if not self.release_identity.strip():
            errors.append("control-plane release identity is required")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.control_plane_environment.items()):
            errors.append("control-plane environment must contain only string values")
        return errors

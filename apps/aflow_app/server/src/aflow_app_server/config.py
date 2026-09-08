"""Server configuration loading, validation, and first-run setup."""

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


def global_config_dir() -> Path:
    """The one global AFlow configuration directory for this account."""
    return Path.home() / ".config" / "aflow"


def global_config_file() -> Path:
    return global_config_dir() / "config.toml"


def redact_secret(value: str, *, keep: int = 0) -> str:
    """Render a credential value safely printable in responses, logs, or audit."""
    if not value:
        return ""
    return "***"


def scrub_secrets(text: str, secrets: list[str]) -> str:
    """Remove any literal occurrence of the given secrets from free-form text."""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, redact_secret(secret))
    return text

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
    def _load_tables(cls, config_file: Path) -> dict[str, Any]:
        if not config_file.exists():
            return {}
        with config_file.open("rb") as handle:
            return tomllib.load(handle)

    @classmethod
    def _from_tables(
        cls,
        file_config: dict[str, Any],
        *,
        config_dir: Path,
        bind_host: str | None = None,
        bind_port: int | None = None,
    ) -> "ServerConfig":
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
        token_file_value = server_section.get("auth_token_file")
        return cls(
            bind_host=bind_host if bind_host is not None else server_section.get("bind_host", "127.0.0.1"),
            bind_port=bind_port if bind_port is not None else int(server_section.get("bind_port", 8765)),
            auth_token=server_section.get("auth_token", ""),
            auth_token_file=Path(token_file_value).expanduser() if token_file_value else None,
            managed_projects_root=Path(control_plane_section.get("managed_projects_root", "~/code")).expanduser(),
            project_registry_path=Path(control_plane_section.get("project_registry_path", str(config_dir / "projects.json"))).expanduser(),
            config_audit_path=Path(control_plane_section.get("config_audit_path", str(config_dir / "config_audit.jsonl"))).expanduser(),
            aflow_executable=Path(control_plane_section.get("aflow_executable", "/usr/bin/aflow")).expanduser(),
            release_identity=str(control_plane_section.get("release_identity", "unconfigured")),
            environment_file=Path(control_plane_section.get("environment_file", "/etc/aflowd.env")).expanduser(),
            control_plane_environment={str(key): str(value) for key, value in child_environment.items()},
        )

    @classmethod
    def from_env(cls) -> "ServerConfig":
        config_dir = Path(os.environ.get("AFLOW_APP_CONFIG_DIR", "~/.config/aflow")).expanduser()
        file_config = cls._load_tables(config_dir / "config.toml")
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
        config = cls._from_tables(file_config, config_dir=config_dir)
        return cls(
            bind_host=os.environ.get("AFLOW_APP_HOST", config.bind_host),
            bind_port=int(os.environ.get("AFLOW_APP_PORT", config.bind_port)),
            auth_token=os.environ.get("AFLOW_APP_TOKEN", config.auth_token),
            auth_token_file=Path(token_file_value).expanduser() if token_file_value else None,
            managed_projects_root=Path(os.environ.get("AFLOW_MANAGED_PROJECTS_ROOT", str(config.managed_projects_root))).expanduser(),
            project_registry_path=Path(os.environ.get("AFLOW_PROJECT_REGISTRY_PATH", str(config.project_registry_path))).expanduser(),
            config_audit_path=Path(os.environ.get("AFLOW_CONFIG_AUDIT_PATH", str(config.config_audit_path))).expanduser(),
            aflow_executable=Path(os.environ.get("AFLOW_EXECUTABLE", str(config.aflow_executable))).expanduser(),
            release_identity=str(os.environ.get("AFLOW_RELEASE_IDENTITY", config.release_identity)),
            environment_file=Path(os.environ.get("AFLOW_ENVIRONMENT_FILE", str(config.environment_file))).expanduser(),
            control_plane_environment=config.control_plane_environment,
        )

    @classmethod
    def from_global_dir(
        cls,
        config_dir: Path | None = None,
        *,
        bind_host: str | None = None,
        bind_port: int | None = None,
    ) -> "ServerConfig":
        """Read the default global configuration without environment overrides.

        ``aflow ui`` uses this so an unrelated shell never redirects the UI to
        an alternate config directory. Explicit host/port arguments win for
        that process only.
        """
        resolved_dir = (config_dir or global_config_dir()).expanduser()
        file_config = cls._load_tables(resolved_dir / "config.toml")
        return cls._from_tables(
            file_config,
            config_dir=resolved_dir,
            bind_host=bind_host,
            bind_port=bind_port,
        )

    def runtime_paths(self) -> tuple[Path, Path]:
        """Registry and audit-log paths for this configuration."""
        return self.project_registry_path, self.config_audit_path

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


def update_global_settings(
    config_dir: Path | None = None,
    *,
    auth_token: str | None = None,
    managed_projects_root: str | None = None,
    bind_host: str | None = None,
    bind_port: int | None = None,
) -> Path:
    """Losslessly write transport settings into the global ``config.toml``.

    Only supplied keys change; unrelated tables, keys, and comments are
    preserved byte-for-byte via tomlkit. The credential is never returned or
    logged. The file is stored with mode ``0600`` because it holds the
    shared credential.
    """
    import tomlkit

    directory = (config_dir or global_config_dir()).expanduser()
    path = directory / "config.toml"
    if auth_token is not None:
        if not isinstance(auth_token, str) or not auth_token.strip():
            raise ValueError("authentication token must be a non-empty string")
    document: Any
    if path.exists():
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        document = tomlkit.document()
    server = document.get("server") if isinstance(document.get("server"), tomlkit.items.Table) else None
    if server is None:
        server = tomlkit.table()
        document["server"] = server
    control_plane = (
        document.get("control_plane")
        if isinstance(document.get("control_plane"), tomlkit.items.Table)
        else None
    )
    if control_plane is None:
        control_plane = tomlkit.table()
        document["control_plane"] = control_plane
    if auth_token is not None:
        server["auth_token"] = auth_token
    if bind_host is not None:
        server["bind_host"] = bind_host
    if bind_port is not None:
        server["bind_port"] = int(bind_port)
    if managed_projects_root is not None:
        if not managed_projects_root.strip():
            raise ValueError("managed projects root must be a non-empty path")
        control_plane["managed_projects_root"] = managed_projects_root
    directory.mkdir(parents=True, exist_ok=True)
    rendered = tomlkit.dumps(document)
    import tempfile

    fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


class GlobalCredentialProvider:
    """Per-request credential source that notices ``config.toml`` rotation.

    Caches the parsed settings keyed on the file's mtime/size so password
    changes on disk invalidate sessions immediately without re-parsing the
    file on every request.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        self._config_dir = (config_dir or global_config_dir()).expanduser()
        self._identity: tuple[int, int] | None = None
        self._cached: ServerConfig | None = None

    def config(self) -> ServerConfig:
        path = self._config_dir / "config.toml"
        try:
            stat = path.stat()
            identity = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            identity = (-1, -1)
        if self._cached is None or identity != self._identity:
            self._cached = ServerConfig.from_global_dir(self._config_dir)
            self._identity = identity
        return self._cached

    def current_auth_token(self) -> str:
        return self.config().current_auth_token()

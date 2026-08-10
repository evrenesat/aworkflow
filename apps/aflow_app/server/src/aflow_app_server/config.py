"""Server configuration loading and validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]


_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class PlanningProviderConfig:
    """Configuration for one provider-neutral planning backend."""

    id: str
    kind: str
    display_name: str
    enabled: bool = True
    server_url: str | None = None
    server_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ControlPlaneProjectConfig:
    """One explicitly allowlisted project served by the daemon-backed API."""

    id: str
    root: Path
    config_path: Path
    aflow_executable: Path
    environment_file: Path
    release_identity: str
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)

    def normalized(self) -> "ControlPlaneProjectConfig":
        """Resolve and validate static daemon inputs before the service starts."""
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
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise ValueError("daemon environment must contain only string values")
        return ControlPlaneProjectConfig(
            id=self.id,
            root=root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            release_identity=self.release_identity,
            environment=dict(self.environment),
        )


@dataclass(frozen=True)
class ServerConfig:
    """Configuration for the remote app server."""

    bind_host: str
    bind_port: int
    auth_token: str = field(repr=False)
    repo_registry_path: Path
    codex_app_server_url: str | None
    codex_app_server_token: str | None = field(repr=False)
    transcription_url: str | None
    transcription_token: str | None = field(repr=False)
    projects_home: Path = field(default_factory=lambda: Path("~/code").expanduser())
    project_overrides_path: Path = field(
        default_factory=lambda: Path("~/.config/aflow/project_overrides.json").expanduser()
    )
    planning_providers: tuple[PlanningProviderConfig, ...] = field(
        default_factory=lambda: (
            PlanningProviderConfig(id="codex", kind="codex", display_name="Codex"),
        )
    )
    default_planning_provider_id: str | None = "codex"
    attachment_root: Path = field(
        default_factory=lambda: Path("~/.config/aflow/attachments").expanduser()
    )
    attachment_max_file_size_bytes: int = 25 * 1024 * 1024
    attachment_max_count_per_turn: int = 10
    attachment_max_total_size_bytes_per_turn: int = 50 * 1024 * 1024
    planning_operation_timeout_seconds: float = 30.0
    planning_execution_policy: str = "full_access"
    auth_token_file: Path | None = None
    control_plane_projects: tuple[ControlPlaneProjectConfig, ...] = ()

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Load configuration from environment variables and config file."""
        config_dir = Path(os.environ.get("AFLOW_APP_CONFIG_DIR", "~/.config/aflow")).expanduser()
        config_file = config_dir / "config.toml"

        # Load from file if exists
        file_config: dict[str, Any] = {}
        if config_file.exists():
            with open(config_file, "rb") as f:
                file_config = tomllib.load(f)

        server_section = file_config.get("server", {})
        codex_section = file_config.get("codex_app_server", file_config.get("codex", {}))
        planning_section = file_config.get("planning", {})
        projects_section = file_config.get("project_catalog", file_config.get("projects", {}))
        transcription_section = file_config.get("transcription", {})
        control_plane_section = file_config.get("control_plane", {})

        legacy_codex_url = (
            os.environ.get("AFLOW_CODEX_APP_SERVER_URL")
            or os.environ.get("AFLOW_CODEX_URL")
            or codex_section.get("server_url")
            or codex_section.get("url")
        )
        legacy_codex_token = (
            os.environ.get("AFLOW_CODEX_APP_SERVER_TOKEN")
            or os.environ.get("AFLOW_CODEX_TOKEN")
            or codex_section.get("server_token")
            or codex_section.get("token")
        )
        provider_values = cls._provider_values(planning_section)
        providers_json = os.environ.get("AFLOW_PLANNING_PROVIDERS")
        if providers_json is not None:
            try:
                decoded_providers = json.loads(providers_json)
            except json.JSONDecodeError as exc:
                raise ValueError("AFLOW_PLANNING_PROVIDERS must be valid JSON") from exc
            provider_values = cls._provider_values({"providers": decoded_providers})

        if not provider_values:
            provider_values = [
                {
                    "id": "codex",
                    "kind": "codex",
                    "display_name": "Codex",
                    "server_url": legacy_codex_url,
                    "server_token": legacy_codex_token,
                }
            ]

        # New provider-specific values win over new file values, which already
        # won by selection over the legacy compatibility values above.
        for provider in provider_values:
            if provider.get("id") != "codex":
                continue
            provider["server_url"] = (
                os.environ.get("AFLOW_PLANNING_CODEX_URL")
                or provider.get("server_url")
                or provider.get("url")
                or legacy_codex_url
            )
            provider["server_token"] = (
                os.environ.get("AFLOW_PLANNING_CODEX_TOKEN")
                or provider.get("server_token")
                or provider.get("token")
                or legacy_codex_token
            )

        planning_providers = tuple(
            PlanningProviderConfig(
                id=str(provider.get("id") or ""),
                kind=str(
                    provider.get("kind")
                    or provider.get("type")
                    or provider.get("id")
                    or ""
                ),
                display_name=str(
                    provider.get("display_name") or provider.get("name") or provider.get("id") or ""
                ),
                enabled=bool(provider.get("enabled", True)),
                server_url=provider.get("server_url", provider.get("url")),
                server_token=provider.get("server_token", provider.get("token")),
            )
            for provider in provider_values
        )
        codex_provider = next(
            (provider for provider in planning_providers if provider.id == "codex"), None
        )
        control_plane_projects = cls._control_plane_project_values(control_plane_section)
        projects_json = os.environ.get("AFLOW_CONTROL_PLANE_PROJECTS")
        if projects_json is not None:
            try:
                decoded_projects = json.loads(projects_json)
            except json.JSONDecodeError as exc:
                raise ValueError("AFLOW_CONTROL_PLANE_PROJECTS must be valid JSON") from exc
            control_plane_projects = cls._control_plane_project_values(
                {"projects": decoded_projects}
            )

        # Environment overrides file config
        return cls(
            bind_host=os.environ.get("AFLOW_APP_HOST", server_section.get("bind_host", "127.0.0.1")),
            bind_port=int(os.environ.get("AFLOW_APP_PORT", server_section.get("bind_port", 8765))),
            auth_token=os.environ.get("AFLOW_APP_TOKEN", server_section.get("auth_token", "")),
            repo_registry_path=Path(os.environ.get(
                "AFLOW_APP_REGISTRY_PATH",
                server_section.get("repo_registry_path", str(config_dir / "repos.json"))
            )).expanduser(),
            codex_app_server_url=(
                codex_provider.server_url if codex_provider is not None else legacy_codex_url
            ),
            codex_app_server_token=(
                codex_provider.server_token if codex_provider is not None else legacy_codex_token
            ),
            transcription_url=os.environ.get("AFLOW_TRANSCRIPTION_URL", transcription_section.get("server_url")),
            transcription_token=os.environ.get("AFLOW_TRANSCRIPTION_TOKEN", transcription_section.get("server_token")),
            projects_home=Path(
                os.environ.get(
                    "AFLOW_APP_PROJECTS_HOME",
                    projects_section.get("projects_home", "~/code"),
                )
            ).expanduser(),
            project_overrides_path=Path(
                os.environ.get(
                    "AFLOW_APP_PROJECT_OVERRIDES_PATH",
                    projects_section.get(
                        "project_overrides_path",
                        str(config_dir / "project_overrides.json"),
                    ),
                )
            ).expanduser(),
            planning_providers=planning_providers,
            default_planning_provider_id=(
                os.environ.get("AFLOW_PLANNING_DEFAULT_PROVIDER")
                or planning_section.get("default_provider_id")
                or planning_section.get("default_provider")
                or ("codex" if codex_provider is not None else None)
            ),
            attachment_root=Path(
                os.environ.get(
                    "AFLOW_PLANNING_ATTACHMENT_ROOT",
                    planning_section.get("attachment_root", str(config_dir / "attachments")),
                )
            ).expanduser(),
            attachment_max_file_size_bytes=int(
                os.environ.get(
                    "AFLOW_PLANNING_ATTACHMENT_MAX_FILE_SIZE_BYTES",
                    planning_section.get("attachment_max_file_size_bytes", 25 * 1024 * 1024),
                )
            ),
            attachment_max_count_per_turn=int(
                os.environ.get(
                    "AFLOW_PLANNING_ATTACHMENT_MAX_COUNT_PER_TURN",
                    planning_section.get("attachment_max_count_per_turn", 10),
                )
            ),
            attachment_max_total_size_bytes_per_turn=int(
                os.environ.get(
                    "AFLOW_PLANNING_ATTACHMENT_MAX_TOTAL_SIZE_BYTES_PER_TURN",
                    planning_section.get(
                        "attachment_max_total_size_bytes_per_turn", 50 * 1024 * 1024
                    ),
                )
            ),
            planning_operation_timeout_seconds=float(
                os.environ.get(
                    "AFLOW_PLANNING_OPERATION_TIMEOUT_SECONDS",
                    planning_section.get("operation_timeout_seconds", 30.0),
                )
            ),
            planning_execution_policy=os.environ.get(
                "AFLOW_PLANNING_EXECUTION_POLICY",
                planning_section.get("execution_policy", "full_access"),
            ),
            auth_token_file=(
                Path(
                    os.environ.get(
                        "AFLOW_APP_TOKEN_FILE",
                        server_section.get("auth_token_file", ""),
                    )
                ).expanduser()
                if (
                    os.environ.get("AFLOW_APP_TOKEN_FILE")
                    or server_section.get("auth_token_file")
                )
                else None
            ),
            control_plane_projects=tuple(control_plane_projects),
        )

    @staticmethod
    def _provider_values(planning_section: Any) -> list[dict[str, Any]]:
        """Normalize TOML/JSON provider lists without accepting scalar entries."""
        if not isinstance(planning_section, dict):
            raise ValueError("planning config must be a table")
        raw_providers = planning_section.get("providers", [])
        if isinstance(raw_providers, dict):
            values: list[dict[str, Any]] = []
            for provider_id, raw_value in raw_providers.items():
                if not isinstance(raw_value, dict):
                    raise ValueError("planning.providers entries must be tables")
                values.append({"id": provider_id, **raw_value})
            return values
        if not isinstance(raw_providers, list):
            raise ValueError("planning.providers must be a list or table")
        if not all(isinstance(value, dict) for value in raw_providers):
            raise ValueError("planning.providers entries must be objects")
        return [dict(value) for value in raw_providers]

    @staticmethod
    def _control_plane_project_values(section: Any) -> list[ControlPlaneProjectConfig]:
        """Parse an explicit project allowlist without accepting arbitrary paths."""
        if not isinstance(section, dict):
            raise ValueError("control_plane config must be a table")
        raw_projects = section.get("projects", [])
        if not isinstance(raw_projects, list) or not all(
            isinstance(value, dict) for value in raw_projects
        ):
            raise ValueError("control_plane.projects must be a list of tables")
        projects: list[ControlPlaneProjectConfig] = []
        for raw in raw_projects:
            environment = raw.get("environment", {})
            if not isinstance(environment, dict):
                raise ValueError("control_plane project environment must be a table")
            required = (
                "id",
                "root",
                "config_path",
                "aflow_executable",
                "environment_file",
                "release_identity",
            )
            if any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in required):
                raise ValueError("control_plane projects require complete daemon configuration")
            projects.append(
                ControlPlaneProjectConfig(
                    id=str(raw["id"]),
                    root=Path(str(raw["root"])),
                    config_path=Path(str(raw["config_path"])),
                    aflow_executable=Path(str(raw["aflow_executable"])),
                    environment_file=Path(str(raw["environment_file"])),
                    release_identity=str(raw["release_identity"]),
                    environment={str(key): str(value) for key, value in environment.items()},
                )
            )
        return projects

    def current_auth_token(self) -> str:
        """Read the bearer secret on each request so a file can rotate in place."""
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
        """Validate configuration and return list of errors."""
        errors: list[str] = []
        try:
            if not self.current_auth_token():
                errors.append("authentication token is required")
        except ValueError:
            errors.append("authentication token file is unavailable")
        if self.bind_port < 1 or self.bind_port > 65535:
            errors.append(f"invalid bind_port: {self.bind_port}")
        provider_ids: set[str] = set()
        enabled_provider_ids: set[str] = set()
        for provider in self.planning_providers:
            if not provider.id or not _PROVIDER_ID_PATTERN.fullmatch(provider.id):
                errors.append(
                    f"invalid planning provider id {provider.id!r}: use a non-empty path-safe slug"
                )
            elif provider.id in provider_ids:
                errors.append(f"duplicate planning provider id: {provider.id}")
            provider_ids.add(provider.id)
            if provider.enabled:
                enabled_provider_ids.add(provider.id)
            if not provider.kind:
                errors.append(f"planning provider {provider.id!r} requires a kind")
            if not provider.display_name:
                errors.append(f"planning provider {provider.id!r} requires a display_name")
        if (
            self.default_planning_provider_id is not None
            and self.default_planning_provider_id not in enabled_provider_ids
        ):
            errors.append(
                "default planning provider is unknown or disabled: "
                f"{self.default_planning_provider_id}"
            )
        if self.planning_operation_timeout_seconds <= 0:
            errors.append("planning_operation_timeout_seconds must be greater than zero")
        if self.attachment_max_file_size_bytes <= 0:
            errors.append("attachment_max_file_size_bytes must be greater than zero")
        if self.attachment_max_count_per_turn <= 0:
            errors.append("attachment_max_count_per_turn must be greater than zero")
        if self.attachment_max_total_size_bytes_per_turn <= 0:
            errors.append(
                "attachment_max_total_size_bytes_per_turn must be greater than zero"
            )
        if self.planning_execution_policy != "full_access":
            errors.append("planning_execution_policy must be 'full_access'")
        project_ids: set[str] = set()
        for project in self.control_plane_projects:
            if project.id in project_ids:
                errors.append(f"duplicate control-plane project id: {project.id}")
                continue
            project_ids.add(project.id)
            try:
                project.normalized()
            except ValueError:
                errors.append(f"invalid control-plane project configuration: {project.id}")
        return errors

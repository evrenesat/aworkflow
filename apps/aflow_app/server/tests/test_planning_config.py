"""Focused tests for planning provider configuration."""

from __future__ import annotations

from pathlib import Path

from aflow_app_server.config import PlanningProviderConfig, ServerConfig


def _config(tmp_path: Path, **overrides: object) -> ServerConfig:
    values = {
        "bind_host": "127.0.0.1",
        "bind_port": 8765,
        "auth_token": "token",
        "repo_registry_path": tmp_path / "repos.json",
        "codex_app_server_url": None,
        "codex_app_server_token": None,
        "transcription_url": None,
        "transcription_token": None,
    }
    values.update(overrides)
    return ServerConfig(**values)  # type: ignore[arg-type]


def test_config_rejects_duplicate_and_unknown_default_providers(tmp_path: Path) -> None:
    duplicate = _config(
        tmp_path,
        planning_providers=(
            PlanningProviderConfig("codex", "codex", "Codex"),
            PlanningProviderConfig("codex", "other", "Duplicate"),
        ),
    )
    assert "duplicate planning provider id: codex" in duplicate.validate()

    unknown = _config(
        tmp_path,
        planning_providers=(PlanningProviderConfig("codex", "codex", "Codex"),),
        default_planning_provider_id="missing",
    )
    assert any("default planning provider is unknown" in error for error in unknown.validate())


def test_execution_policy_defaults_to_provider_neutral_full_access(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.planning_execution_policy == "full_access"
    assert config.validate() == []


def test_execution_policy_reads_file_and_environment_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[server]
auth_token = "file-token"

[planning]
execution_policy = "full_access"
""".strip()
    )
    monkeypatch.setenv("AFLOW_APP_CONFIG_DIR", str(config_dir))

    file_config = ServerConfig.from_env()
    assert file_config.planning_execution_policy == "full_access"

    (config_dir / "config.toml").write_text(
        """
[server]
auth_token = "file-token"

[planning]
execution_policy = "provider-native-policy"
""".strip()
    )
    monkeypatch.setenv("AFLOW_PLANNING_EXECUTION_POLICY", "full_access")
    environment_config = ServerConfig.from_env()
    assert environment_config.planning_execution_policy == "full_access"
    assert environment_config.validate() == []


def test_config_rejects_unknown_execution_policy(tmp_path: Path) -> None:
    config = _config(tmp_path, planning_execution_policy="provider-native-policy")

    assert "planning_execution_policy must be 'full_access'" in config.validate()


def test_config_requires_nonzero_attachment_limits(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        attachment_max_file_size_bytes=0,
        attachment_max_count_per_turn=0,
        attachment_max_total_size_bytes_per_turn=0,
    )

    assert config.validate() == [
        "attachment_max_file_size_bytes must be greater than zero",
        "attachment_max_count_per_turn must be greater than zero",
        "attachment_max_total_size_bytes_per_turn must be greater than zero",
    ]


def test_new_file_values_override_legacy_environment_aliases(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[server]
auth_token = "file-token"

[planning]
default_provider = "codex"
attachment_root = "/new/attachments"

[[planning.providers]]
id = "codex"
kind = "codex"
display_name = "Codex SDK"
server_url = "ws://new-file"
""".strip()
    )
    monkeypatch.setenv("AFLOW_APP_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AFLOW_CODEX_APP_SERVER_URL", "ws://legacy-env")

    config = ServerConfig.from_env()

    assert config.codex_app_server_url == "ws://new-file"
    assert config.planning_providers[0].server_url == "ws://new-file"
    assert config.attachment_root == Path("/new/attachments")


def test_new_planning_environment_values_have_highest_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[server]
auth_token = "file-token"

[planning]
default_provider = "codex"

[[planning.providers]]
id = "codex"
kind = "codex"
display_name = "Codex"
server_url = "ws://new-file"
""".strip()
    )
    monkeypatch.setenv("AFLOW_APP_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AFLOW_CODEX_APP_SERVER_URL", "ws://legacy-env")
    monkeypatch.setenv("AFLOW_PLANNING_CODEX_URL", "ws://new-env")
    monkeypatch.setenv("AFLOW_PLANNING_ATTACHMENT_ROOT", str(tmp_path / "env-attachments"))

    config = ServerConfig.from_env()

    assert config.codex_app_server_url == "ws://new-env"
    assert config.planning_providers[0].server_url == "ws://new-env"
    assert config.attachment_root == tmp_path / "env-attachments"


def test_legacy_codex_values_remain_read_compatible(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[server]
auth_token = "file-token"

[codex_app_server]
server_url = "ws://legacy-file"
server_token = "legacy-secret"
""".strip()
    )
    monkeypatch.setenv("AFLOW_APP_CONFIG_DIR", str(config_dir))

    config = ServerConfig.from_env()

    assert config.codex_app_server_url == "ws://legacy-file"
    assert config.codex_app_server_token == "legacy-secret"
    assert config.planning_providers[0].id == "codex"
    assert config.planning_providers[0].server_url == "ws://legacy-file"
    assert config.attachment_root == config_dir / "attachments"

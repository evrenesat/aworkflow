"""Checkpoint 1: exclusive resource identity through configuration."""

import re

import pytest

from tests._support import *  # noqa: F401,F403
from aflow.config import ConfigError, WorkflowUserConfig, execution_resource_key
from aflow.workflow import ResolvedProfile, resolve_profile, resolve_role_selector


def _exclusive_config(tmp_path: Path, profile_table: str) -> WorkflowUserConfig:
    config_path = _write_config(
        tmp_path,
        f"[harness.pi.profiles.worker]\nmodel = \"m1\"\n{profile_table}\n"
        "[roles]\nworker = \"pi.worker\"\n"
        "[prompts]\nimplementation_prompt = \"Implement.\"\n",
    )
    config_path.with_name("workflows.toml").write_text(
        '[workflow.simple.steps.implement]\nrole = "worker"\n'
        'prompts = ["implementation_prompt"]\ngo = [{ to = "END" }]\n',
        encoding="utf-8",
    )
    return load_workflow_config(config_path)


def test_omitted_exclusive_defaults_to_false(tmp_path: Path) -> None:
    config = _exclusive_config(tmp_path, "")
    profile = config.harnesses["pi"].profiles["worker"]
    assert profile.exclusive is False


def test_explicit_false_exclusive_stays_false(tmp_path: Path) -> None:
    config = _exclusive_config(tmp_path, "exclusive = false\n")
    profile = config.harnesses["pi"].profiles["worker"]
    assert profile.exclusive is False


def test_explicit_true_exclusive_is_true(tmp_path: Path) -> None:
    config = _exclusive_config(tmp_path, "exclusive = true\n")
    profile = config.harnesses["pi"].profiles["worker"]
    assert profile.exclusive is True


@pytest.mark.parametrize(
    "value",
    ['"true"', '"yes"', "1", "0", '["true"]'],
)
def test_coerced_exclusive_values_are_rejected_with_field_path(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ConfigError, match=r"harness\.pi\.profiles\.worker\.exclusive must be a boolean"):
        _exclusive_config(tmp_path, f"exclusive = {value}\n")


def test_execution_resource_key_is_stable_64_hex() -> None:
    key = execution_resource_key("pi", "claude-sonnet-4-5", None)
    assert key == "529f2e09fa2930990b8dfb88c6b12ea97978eec0fe9af49871d2eecda32d24d7"
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key == execution_resource_key("pi", "claude-sonnet-4-5", None)


def test_execution_resource_key_treats_none_as_json_null() -> None:
    assert (
        execution_resource_key("pi", None, None)
        == "5f2d8291702f7bb02398c79639961b28492879a3c540367286685b16733bbb5d"
    )
    # Provider-default adapters (no model/effort) still compute a key.
    assert (
        execution_resource_key("zcode", None, None)
        == "1895b154c3a1869e5bdeeb62288f37afbf0daba6b38325950a4be8e57c0df5e7"
    )


def test_execution_resource_key_preserves_model_effort_case() -> None:
    assert (
        execution_resource_key("pi", "Claude-Sonnet", None)
        == "224175c807925f1b494ab064953b09df32e572ce5ba9c7b287f2b7088c4bf34e"
    )
    assert execution_resource_key("pi", "Claude-Sonnet", None) != execution_resource_key(
        "pi", "claude-sonnet", None
    )


def test_identical_marked_tuples_share_key_across_names_and_roots(tmp_path: Path) -> None:
    first = _exclusive_config(tmp_path / "a", "exclusive = true\n")
    second = _exclusive_config(tmp_path / "b", "exclusive = true\n")
    assert (
        execution_resource_key("pi", "m1", None)
        == execution_resource_key("pi", "m1", None)
    )
    resolved_first = resolve_profile("pi.worker", first, step_path="a")
    resolved_second = resolve_profile("pi.worker", second, step_path="b")
    assert (
        resolved_first.exclusive_resource
        == resolved_second.exclusive_resource
        == execution_resource_key("pi", "m1", None)
    )


def test_different_combinations_compute_independent_keys() -> None:
    base = execution_resource_key("pi", "m1", None)
    assert execution_resource_key("pi", "m2", None) != base
    assert execution_resource_key("pi", "m1", "high") != base
    assert execution_resource_key("codex", "m1", None) != base


def test_marked_profile_resolves_derived_key_unmarked_does_not(tmp_path: Path) -> None:
    config = _exclusive_config(tmp_path, "exclusive = true\n")
    resolved = resolve_profile("pi.worker", config, step_path="x")
    assert resolved.exclusive_resource == execution_resource_key("pi", "m1", None)

    unmarked = _exclusive_config(tmp_path / "u", "")
    resolved_unmarked = resolve_profile("pi.worker", unmarked, step_path="x")
    assert resolved_unmarked.exclusive_resource is None


def test_resolved_profile_default_keeps_legacy_construction() -> None:
    resolved = ResolvedProfile(
        harness_name="pi", profile_name="worker", model="m1", effort=None
    )
    assert resolved.exclusive_resource is None


@pytest.mark.parametrize(
    ("team_name", "run_local"),
    [(None, None), ("team_a", None), (None, {"worker": "pi.worker"})],
)
def test_routing_precedence_preserves_exclusive_identity(
    tmp_path: Path, team_name: str | None, run_local: dict | None
) -> None:
    config_path = _write_config(
        tmp_path,
        "[harness.pi.profiles.worker]\nmodel = \"m1\"\nexclusive = true\n"
        "[harness.pi.profiles.other]\nmodel = \"m2\"\n"
        "[roles]\nworker = \"pi.worker\"\n"
        "[teams.team_a]\nworker = \"pi.worker\"\n"
        "[prompts]\nimplementation_prompt = \"Implement.\"\n",
    )
    config_path.with_name("workflows.toml").write_text(
        '[workflow.simple.steps.implement]\nrole = "worker"\n'
        'prompts = ["implementation_prompt"]\ngo = [{ to = "END" }]\n',
        encoding="utf-8",
    )
    config = load_workflow_config(config_path)
    selector = resolve_role_selector(
        "worker",
        team_name,
        config,
        step_path="x",
        run_local_role_selectors=run_local,
    )
    assert selector == "pi.worker"
    resolved = resolve_profile(selector, config, step_path="x")
    assert resolved.exclusive_resource == execution_resource_key("pi", "m1", None)


def test_unmarked_alias_of_same_tuple_stays_unrestricted(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        "[harness.pi.profiles.worker]\nmodel = \"m1\"\nexclusive = true\n"
        "[harness.pi.profiles.alias]\nmodel = \"m1\"\n"
        "[roles]\nworker = \"pi.worker\"\n"
        "[prompts]\nimplementation_prompt = \"Implement.\"\n",
    )
    config_path.with_name("workflows.toml").write_text(
        '[workflow.simple.steps.implement]\nrole = "worker"\n'
        'prompts = ["implementation_prompt"]\ngo = [{ to = "END" }]\n',
        encoding="utf-8",
    )
    config = load_workflow_config(config_path)
    marked = resolve_profile("pi.worker", config, step_path="x")
    alias = resolve_profile("pi.alias", config, step_path="x")
    assert marked.exclusive_resource is not None
    assert alias.exclusive_resource is None
    assert execution_resource_key("pi", "m1", None) == execution_resource_key(
        "pi", "m1", None
    )


def test_profile_without_model_effort_accepts_exclusive_flag(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        "[harness.zcode.profiles.worker]\nexclusive = true\n"
        "[roles]\nworker = \"zcode.worker\"\n"
        "[prompts]\nimplementation_prompt = \"Implement.\"\n",
    )
    config_path.with_name("workflows.toml").write_text(
        '[workflow.simple.steps.implement]\nrole = "worker"\n'
        'prompts = ["implementation_prompt"]\ngo = [{ to = "END" }]\n',
        encoding="utf-8",
    )
    config = load_workflow_config(config_path)
    resolved = resolve_profile("zcode.worker", config, step_path="x")
    assert resolved.exclusive_resource == execution_resource_key("zcode", None, None)

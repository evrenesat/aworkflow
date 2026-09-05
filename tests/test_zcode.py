from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from aflow.api import RunnerConfig, StartupRequest, WorkflowRunner, prepare_startup
from aflow.api.models import PreparedRun
from aflow.config import ConfigError, load_workflow_config, validate_workflow_config
from aflow.harnesses import get_adapter
from aflow.harnesses.zcode import ZcodeAdapter


def configuration(root: Path, profile: str = "") -> Path:
    path = root / "aflow.toml"
    path.write_text(
        '[aflow]\ndefault_workflow = "simple"\n'
        '[harness.zcode.profiles.default]\n' + profile +
        '[roles]\nworker = "zcode.default"\n'
        '[prompts]\nimplement = "Complete the task."\n',
        encoding="utf-8",
    )
    path.with_name("workflows.toml").write_text(
        '[workflow.simple]\nsetup = []\nteardown = []\n'
        '[workflow.simple.steps.implement]\nrole = "worker"\n'
        'prompts = ["implement"]\ngo = [{ to = "END" }]\n',
        encoding="utf-8",
    )
    return path


def test_zcode_prompt_remains_one_literal_argument() -> None:
    prompt = "quoted ' text\n$(touch should-not-exist) 雪"
    invocation = get_adapter("zcode").build_invocation(
        repo_root=Path("/repo with spaces"), model=None,
        system_prompt="SYSTEM", user_prompt=prompt,
    )
    assert invocation.argv == (
        "zcode", "--cwd", "/repo with spaces", "--mode", "yolo",
        "--no-color", "--prompt", "SYSTEM\n\n" + prompt,
    )
    assert invocation.stdin_text is None
    assert invocation.env == {}
    assert invocation.final_output_argv is None
    assert not ZcodeAdapter.supports_effort
    assert ZcodeAdapter.manager_workspace_read


@pytest.mark.parametrize(("model", "effort"), [("GLM-5.3-Flash", None), (None, "max")])
def test_zcode_rejects_direct_unsupported_overrides(
    model: str | None, effort: str | None,
) -> None:
    with pytest.raises(ValueError, match="does not support AFlow"):
        ZcodeAdapter().build_invocation(
            repo_root=Path("/repo"), model=model, effort=effort,
            system_prompt="", user_prompt="",
        )


@pytest.mark.parametrize("profile", ['model = "GLM-5.3-Flash"\n', 'effort = "max"\n'])
def test_zcode_rejects_configured_unsupported_overrides(
    tmp_path: Path, profile: str,
) -> None:
    with pytest.raises(ConfigError, match="harness.zcode.profiles.default.*ZCode"):
        load_workflow_config(configuration(tmp_path, profile))


def test_zcode_completes_through_public_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    config_path = configuration(root)
    plan = root / "plan.md"
    plan.write_text("# Fixture\n\n### [ ] Checkpoint 1: Finish\n- [ ] finish task\n")
    (root / ".gitignore").write_text(".aflow/\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
    ], check=True)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "zcode"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json\nfrom pathlib import Path\n"
        "parser=argparse.ArgumentParser()\n"
        "parser.add_argument('--cwd', required=True)\n"
        "parser.add_argument('--mode', choices=['yolo'], required=True)\n"
        "parser.add_argument('--no-color', action='store_true', required=True)\n"
        "parser.add_argument('--prompt', required=True)\n"
        "args=parser.parse_args()\nroot=Path(args.cwd)\n"
        "(root/'invocation.json').write_text(json.dumps(vars(args)))\n"
        "plan=root/'plan.md'\n"
        "plan.write_text(plan.read_text().replace('[ ]', '[x]'))\n"
        "print('Completed fixture work.')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ["PATH"])
    config = load_workflow_config(config_path)
    assert validate_workflow_config(config) == []
    prepared = prepare_startup(StartupRequest(
        repo_root=root, plan_path=plan, config_path=config_path,
        workflow_config=config, workflow_name="simple", start_step="implement",
        max_turns=1, team=None,
    ))
    assert isinstance(prepared, PreparedRun)
    result = WorkflowRunner(RunnerConfig(prepared_run=prepared)).run()
    assert result.end_reason == "transition_end"
    assert "[ ]" not in plan.read_text()
    invocation = json.loads((root / "invocation.json").read_text())
    assert invocation["no_color"] is True
    assert "Complete the task." in invocation["prompt"]
    results = list((root / ".aflow" / "runs").glob("*/turns/turn-*/result.json"))
    assert len(results) == 1
    assert json.loads(results[0].read_text())["label"] == "zcode"

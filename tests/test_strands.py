from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from aflow.harnesses import get_adapter
from aflow.config import load_workflow_config, resolve_team_config
from aflow.harnesses.session import SessionRequest
from aflow.harnesses.strands import StrandsAcpDriver, StrandsAcpProcess
import aflow.harnesses.strands as strands_module


def _request(tmp_path: Path) -> SessionRequest:
    return SessionRequest(
        repo_root=tmp_path, selector="strands.ds41f", model="litellm/deepseek-flash",
        effort=None, system_prompt="System instructions", user_prompt="User task",
    )


def _peer(tmp_path: Path, mode: str) -> Path:
    path = tmp_path / "fake-strands"
    path.write_text(
        "#!" + sys.executable + "\n"
        + "import json, os, sys, time\n"
        + "mode = " + repr(mode) + "\n"
        + "for line in sys.stdin:\n"
        + "  request = json.loads(line)\n"
        + "  method = request['method']\n"
        + "  if method == 'initialize':\n"
        + "    result = {'protocolVersion': 1, 'agentInfo': {'name': '@strands-agents/cli'}}\n"
        + "  elif method == 'session/new':\n"
        + "    result = {'sessionId': 'fresh-1'}\n"
        + "  elif method == 'session/prompt':\n"
        + "    if mode == 'timeout': time.sleep(5)\n"
        + "    if mode == 'error':\n"
        + "      print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'message':'provider error'}}), flush=True)\n"
        + "      continue\n"
        + "    if mode == 'stderr':\n"
        + "      sys.stderr.write('x' * 200000 + '\\n'); sys.stderr.flush()\n"
        + "    update = {'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fresh-1','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'DONE'}}}}\n"
        + "    print(json.dumps(update), flush=True)\n"
        + "    result = {'stopReason':'end_turn','usage':{'inputTokens':2,'outputTokens':1}}\n"
        + "  else: raise ValueError(method)\n"
        + "  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_registration_and_native_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    adapter = get_adapter("strands")
    invocation = adapter.build_invocation(
        repo_root=tmp_path, model="litellm/deepseek-flash",
        system_prompt="system", user_prompt="user",
    )
    assert invocation.argv[0] == "strands"
    assert invocation.argv[-1] == "--acp-server"
    assert invocation.final_output_argv is not None
    assert invocation.final_output_argv[-1] == "--print"
    assert invocation.env == {}
    assert "--skills" in invocation.argv
    assert invocation.stdin_text == "system\n\nuser"


def test_native_env_file_is_passed_as_path_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    secret_file = tmp_path / ".reasonix" / ".env"
    secret_file.parent.mkdir()
    secret_file.write_text("LITELLM_API_KEY=private-value\n", encoding="utf-8")
    argv = get_adapter("strands").build_invocation(
        repo_root=tmp_path, model="litellm/deepseek-flash",
        system_prompt="a", user_prompt="b",
    ).argv
    assert argv[argv.index("--env-file") + 1] == str(secret_file)
    assert "private-value" not in repr(argv)


@pytest.mark.parametrize("mode", ("success", "stderr"))
def test_owned_acp_success_and_stderr_pressure(tmp_path: Path, mode: str) -> None:
    fake = _peer(tmp_path, mode)
    request = _request(tmp_path)
    driver = StrandsAcpDriver(executable=str(fake))
    execution = driver.execute_session(request, driver.build_invocation(request))
    assert execution.result.final_output == "DONE"
    assert execution.result.session_id == "fresh-1"
    assert execution.result.capabilities.resume_with_model is False
    assert any(event.get("result", {}).get("usage") for event in execution.events)
    assert json.loads(execution.raw_transport.splitlines()[-1])["result"]["stopReason"] == "end_turn"


def test_owned_acp_provider_error_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _peer(tmp_path, "error")
    request = _request(tmp_path)
    process = StrandsAcpProcess.start(repo_root=tmp_path, argv=(str(fake),))
    monkeypatch.setattr(strands_module.StrandsAcpProcess, "start", lambda **kwargs: process)
    driver = StrandsAcpDriver(executable=str(fake))
    with pytest.raises(RuntimeError, match="provider error"):
        driver.execute_session(request, driver.build_invocation(request))
    assert process.process.poll() is not None


def test_owned_acp_timeout_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _peer(tmp_path, "timeout")
    request = _request(tmp_path)
    process = StrandsAcpProcess.start(repo_root=tmp_path, argv=(str(fake),))
    monkeypatch.setattr(strands_module.StrandsAcpProcess, "start", lambda **kwargs: process)
    monkeypatch.setattr(strands_module, "PROMPT_TIMEOUT_SECONDS", 0.2)
    driver = StrandsAcpDriver(executable=str(fake))
    with pytest.raises(TimeoutError):
        driver.execute_session(request, driver.build_invocation(request))
    assert process.process.poll() is not None


def test_owned_acp_control_interruption_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _peer(tmp_path, "timeout")
    request = _request(tmp_path)
    process = StrandsAcpProcess.start(repo_root=tmp_path, argv=(str(fake),))
    monkeypatch.setattr(strands_module.StrandsAcpProcess, "start", lambda **kwargs: process)
    driver = StrandsAcpDriver(executable=str(fake))
    def stop() -> None:
        raise InterruptedError("owner stopped")
    with pytest.raises(InterruptedError):
        driver.execute_session(request, driver.build_invocation(request), stop)
    assert process.process.poll() is not None


def test_fresh_turn_rejects_resume(tmp_path: Path) -> None:
    request = _request(tmp_path)
    driver = StrandsAcpDriver()
    with pytest.raises(RuntimeError, match="resume"):
        driver.build_invocation(SessionRequest(**{**request.__dict__, "session_id": "old"}))


def test_strands_profile_resolves_as_second_worker_stage(tmp_path: Path) -> None:
    config_path = tmp_path / "aflow.toml"
    config_path.write_text(
        '[harness.codex.profiles.sol-high]\nmodel = "gpt-5.6-sol"\neffort = "high"\n'
        '[harness.strands.profiles.ds41f]\nmodel = "litellm/deepseek-flash"\n'
        '[roles]\nworker = "codex.sol-high"\n'
        '[teams.sol]\nupgrade_to = "ds41f"\n'
        '[teams.sol.roles]\nworker = "codex.sol-high"\n'
        '[teams.ds41f.roles]\nworker = "strands.ds41f"\n',
        encoding="utf-8",
    )
    config = load_workflow_config(config_path)
    assert config.teams["sol"].upgrade_to == "ds41f"
    assert resolve_team_config(config, "sol").effective_roles["worker"] == "codex.sol-high"
    assert resolve_team_config(config, "ds41f").effective_roles["worker"] == "strands.ds41f"

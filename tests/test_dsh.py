import json
from pathlib import Path
import subprocess
import sys

import pytest

from aflow.harnesses import get_adapter
from aflow.harnesses import dsh
from aflow.harnesses.session import SessionCapabilities, SessionRequest


def initialize(resume=True):
    return {
        "result": {
            "protocolVersion": 1,
            "agentInfo": {"name": "deepseek-harness-acp"},
            "agentCapabilities": {
                "sessionCapabilities": {"resume": {}} if resume else {}
            },
        }
    }


def options(model='["deepseek-official","deepseek-v4-flash"]', effort="high"):
    return [
        {
            "id": "model",
            "type": "select",
            "currentValue": model,
            "options": [
                {
                    "group": "zai",
                    "options": [
                        {"value": '["zai","glm-5.3-flash"]'},
                        {"value": '["zai","glm-5.3"]'},
                    ],
                }
            ],
        },
        {
            "id": "reasoning_effort",
            "type": "select",
            "currentValue": effort,
            "options": [
                {"value": v}
                for v in (["high"] if model.startswith('["deepseek') else ["max"])
            ],
        },
    ]


class FakeProcess:
    def __init__(self, *, bad_effort=False, stop="end_turn", resume=True):
        self.calls = []
        self.received = []
        self.raw_lines = []
        self.closed = False
        self.model = '["deepseek-official","deepseek-v4-flash"]'
        self.effort = "high"
        self.bad_effort, self.stop, self.resume = bad_effort, stop, resume

    def initialize(self):
        self.calls.append(("initialize", {}))
        return initialize(self.resume)

    def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "session/set_config_option":
            if params["configId"] == "model":
                self.model = params["value"]
            elif not self.bad_effort:
                self.effort = params["value"]
        if method == "session/prompt":
            for sid, text in [("other", "WRONG"), ("session-1", "OK")]:
                self.received.append(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": text},
                            },
                        },
                    }
                )
            return {"result": {"stopReason": self.stop}}
        return {
            "result": {
                "sessionId": "session-1",
                "configOptions": options(self.model, self.effort),
            }
        }

    def close(self):
        self.closed = True


def execute(monkeypatch, process, session_id=None):
    monkeypatch.setattr(dsh.DshAcpProcess, "start", lambda **kw: process)
    driver = dsh.DshAcpDriver(SessionCapabilities())
    request = SessionRequest(
        Path("/repo"),
        "dsh.glm",
        "zai/glm-5.3-flash",
        "max",
        "SYSTEM",
        "USER",
        session_id=session_id,
    )
    return driver.execute_session(request, driver.build_invocation(request))


@pytest.mark.parametrize(
    "session_id,method", [(None, "session/new"), ("session-1", "session/resume")]
)
def test_model_then_dependent_effort_and_exact_prompt(monkeypatch, session_id, method):
    process = FakeProcess()
    result = execute(monkeypatch, process, session_id)
    assert [m for m, _ in process.calls] == [
        "initialize",
        method,
        "session/set_config_option",
        "session/set_config_option",
        "session/prompt",
        "session/close",
    ]
    assert process.calls[2][1]["value"] == '["zai","glm-5.3-flash"]'
    assert process.calls[3][1]["configId"] == "reasoning_effort"
    assert process.calls[4][1]["prompt"] == [{"type": "text", "text": "SYSTEM\n\nUSER"}]
    assert result.result.final_output == "OK"
    assert result.result.session_id == "session-1"
    assert process.closed


def test_unapplied_effort_never_prompts(monkeypatch):
    process = FakeProcess(bad_effort=True)
    with pytest.raises(RuntimeError, match="did not apply"):
        execute(monkeypatch, process)
    assert not any(m == "session/prompt" for m, _ in process.calls)
    assert process.closed


def test_failed_prompt_not_success(monkeypatch):
    process = FakeProcess(stop="cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        execute(monkeypatch, process)
    assert process.closed


def test_unadvertised_resume_rejected(monkeypatch):
    process = FakeProcess(resume=False)
    with pytest.raises(RuntimeError, match="does not advertise session resume"):
        execute(monkeypatch, process, "session-1")
    assert process.closed


def test_invalid_routes_and_no_silent_headless_fallback():
    with pytest.raises(ValueError, match="provider/model"):
        dsh._model_option_value("glm-5.3")
    assert dsh._model_option_value("zai/glm-5.3") == '["zai","glm-5.3"]'
    with pytest.raises(RuntimeError, match="ACP session driver"):
        get_adapter("dsh").build_invocation(
            repo_root=Path("/repo"),
            model="zai/glm-5.3",
            effort="max",
            system_prompt="",
            user_prompt="task",
        )


def test_protocol_identity_is_verified():
    payload = initialize()
    payload["result"]["agentInfo"]["name"] = "another-agent"
    with pytest.raises(ValueError, match="not DeepSeek"):
        dsh.DshAcpDriver.from_initialize(payload)


def test_pipe_burst_and_permission_request(tmp_path):
    script = """
import json,sys
r=json.loads(sys.stdin.readline())
messages=[{'jsonrpc':'2.0','id':99,'method':'session/request_permission',
 'params':{'options':[{'kind':'allow_once','optionId':'once'}]}},
 {'jsonrpc':'2.0','method':'session/update','params':{}},
 {'jsonrpc':'2.0','id':r['id'],'result':{'ok':True}}]
sys.stdout.write(''.join(json.dumps(x)+'\\n' for x in messages));sys.stdout.flush()
answer=json.loads(sys.stdin.readline())
assert answer['result']['outcome']=={'outcome':'selected','optionId':'once'}
sys.stdin.read()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process = dsh.DshAcpProcess(child)
    try:
        assert process.request("test", {}, timeout_seconds=2)["result"]["ok"]
    finally:
        process.close()
    assert child.returncode == 0


def test_partial_line_obeys_timeout():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys;sys.stdout.write('{');sys.stdout.flush();sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process = dsh.DshAcpProcess(child)
    try:
        with pytest.raises(TimeoutError):
            process.request("test", {}, timeout_seconds=0.1)
    finally:
        process.close()


def test_discovery_closes_probe_without_opening_session(monkeypatch):
    from aflow import workflow

    process = FakeProcess()
    monkeypatch.setattr(workflow.shutil, "which", lambda name: "/usr/bin/dsh")
    monkeypatch.setattr(dsh.DshAcpProcess, "start", lambda **kw: process)
    driver = workflow._discover_session_driver(
        get_adapter("dsh"), repo_root=Path("/repo")
    )
    assert driver.capabilities.resume_with_model
    assert process.calls == [("initialize", {})]
    assert process.closed

"""REST/MCP configuration-pair parity and crash-recovery tests.

The global configuration service (the REST ``/api/config`` boundary) and the
web MCP authoring tools (the MCP boundary) must commit and read the same
crash-safe pair through the core transaction owner in
:mod:`aflow.config_pair`.  A renamed prompt and its workflow reference must
recover together, stale revisions and invalid candidates must preserve exact
bytes, and a killed save must leave a fresh REST reader and a fresh-process
live reader on one complete old or new generation.

The broad REST/MCP fixture helpers owned by the control-plane API tests are
intentionally not reused: this module is self-contained over a disposable
service directory, and every crash observation is made by a fresh reader
process so recovery is always seen through a clean startup.
"""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from aflow.config_pair import (
    TRANSACTION_RECORD_NAME,
    pair_revision,
)
from aflow.live_config import LiveConfigPairLockBusy, load_live_config
from aflow.mcp_control_plane import _tool_result
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from test_control_plane_api import TOKEN, control_client  # noqa: F401

from aflow_app_server.config_response import config_response
from aflow_app_server.global_config_service import GlobalConfigService
from aflow_app_server.guided_config import apply_action_batch
from aflow_app_server.mcp_config_authoring import register_global_config_tools
from aflow_app_server.models import GlobalConfigPatchPayload
from aflow_app_server.project_config_service import (
    ProjectConfigError,
    ProjectConfigRevisionConflict,
)

_CRASH_EXIT = 37

AFLOW = """# preserve this comment
[aflow]
default_workflow = "demo"
[harness.codex.profiles.worker]
model = "test"
effort = "high"
[roles]
worker = "codex.worker"
[prompts]
work = "Do {task}."
"""

WORKFLOWS = """[workflow]
merge_prompt = ["work"]
[workflow.demo]
merge_prompt = ["work", "work"]
[workflow.demo.steps.implement]
role = "worker"
prompts = ["work"]
go = [{to="END", when="DONE"}]
"""

# A saver child that commits the staged pair through the real transaction
# owner and dies at the requested durable boundary.
SAVER_SCRIPT = """\
import os, sys
from pathlib import Path
from aflow.config_pair import commit_configuration_pair

pair_dir = Path(sys.argv[1])
new_dir = Path(sys.argv[2])
payloads = {name: (new_dir / name).read_bytes() for name in ("aflow.toml", "workflows.toml")}

def barrier(point):
    if point == sys.argv[4]:
        os._exit(37)

commit_configuration_pair(
    pair_dir, payloads=payloads, expected_revision=sys.argv[3], barrier=barrier
)
"""

# A saver child that runs the real GlobalConfigService.save and dies at the
# requested durable boundary, exercising the service's commit path.
SERVICE_SAVER_SCRIPT = """\
import os, sys
from pathlib import Path
import aflow_app_server.global_config_service as module
from aflow.config_pair import commit_configuration_pair

pair_dir = Path(sys.argv[1])
new_dir = Path(sys.argv[2])
point = sys.argv[3]

def barrier(at):
    if at == point:
        os._exit(37)

def commit(*args, **kwargs):
    commit_configuration_pair(*args, **kwargs, barrier=barrier)

module.commit_configuration_pair = commit
service = module.GlobalConfigService(config_dir=pair_dir, audit_path=pair_dir / "audit.jsonl")
before = service.read()
service.save(*[(new_dir / name).read_text(encoding="utf-8") for name in ("aflow.toml", "workflows.toml")], before.revision)
"""

# A fresh public-filesystem reader: the general load_workflow_config path in
# a new process, reporting exact pair bytes and the interpreted prompts before
# any REST/live reader consumes the record.
GENERAL_READER_SCRIPT = """\
import hashlib, json, sys
from pathlib import Path
from aflow.config import ConfigError, load_workflow_config

pair_dir = Path(sys.argv[1])
try:
    loaded = load_workflow_config(pair_dir / "aflow.toml")
except ConfigError as exc:
    print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
    sys.exit(2)
digests = {}
for name in ("aflow.toml", "workflows.toml"):
    path = pair_dir / name
    digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
print(json.dumps({
    "digests": digests,
    "record": (pair_dir / ".aflow-config-pair.transaction.json").is_file(),
    "prompts": sorted(loaded.prompts),
}))
"""


# A fresh REST-boundary reader: the exact call the /api/config GET handler
# makes, in a new process, so recovery is observed through a clean startup.
REST_READER_SCRIPT = """\
import json, sys
from pathlib import Path
from aflow_app_server.global_config_service import GlobalConfigService

pair_dir = Path(sys.argv[1])
service = GlobalConfigService(config_dir=pair_dir, audit_path=pair_dir / "audit.jsonl")
snapshot = service.read()
print(json.dumps({
    "revision": snapshot.revision,
    "aflow": snapshot.aflow_toml,
    "workflows": snapshot.workflows_toml,
}))
"""

# A fresh live-configuration reader: the engine's current-source read in a
# new process, reporting the exact pair bytes and the interpreted contents.
LIVE_READER_SCRIPT = """\
import hashlib, json, sys
from pathlib import Path
from aflow.live_config import load_live_config

pair_dir = Path(sys.argv[1])
loaded = load_live_config(pair_dir / "aflow.toml")
digests = {}
for name in ("aflow.toml", "workflows.toml"):
    path = pair_dir / name
    digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
print(json.dumps({
    "digests": digests,
    "record": (pair_dir / ".aflow-config-pair.transaction.json").is_file(),
    "prompts": sorted(loaded.workflow_config.prompts),
    "workflows": sorted(loaded.workflow_config.workflows),
}))
"""


# Coordinated overlap cases: a distinct new generation (one model, one extra
# step) so the interpreted combination identifies which generation a reader
# saw, and a real GlobalConfigService.save that announces its lock handoff.
OVERLAP_NEW_AFLOW = AFLOW.replace('model = "test"', 'model = "parity-b"')
OVERLAP_NEW_WORKFLOWS = WORKFLOWS + """
[workflow.demo.steps.review]
role = "worker"
prompts = ["work"]
go = [{to="END", when="DONE"}]
"""

# A saver child that runs the real GlobalConfigService.save.  It announces
# "starting" before requesting the pair lock and "held" once it owns the
# lock, so a parent can order the reader against the saver's lock handoff
# without any sleep-based guess.  ``crash_point`` is "" for a full commit.
OVERLAP_SAVER_SCRIPT = """\
import os, sys
from pathlib import Path
import aflow_app_server.global_config_service as module
from aflow.config_pair import commit_configuration_pair, configuration_pair_lock

pair, candidate = map(Path, sys.argv[1:3])
crash_point = sys.argv[4]

def barrier(at):
    if at == crash_point:
        os._exit(37)

def commit(*args, **kwargs):
    commit_configuration_pair(*args, **kwargs, barrier=barrier)

module.commit_configuration_pair = commit
print("starting", flush=True)
with configuration_pair_lock(pair):
    print("held", flush=True)
    service = module.GlobalConfigService(config_dir=pair, audit_path=pair / "audit.jsonl")
    before = service.read()
    service.save(*[(candidate / name).read_text(encoding="utf-8") for name in ("aflow.toml", "workflows.toml")], before.revision)
print("saved", flush=True)
"""

# A fresh general public reader that announces "ready" before its first
# product call and "pre-lock" right before the product's own pair-lock
# acquisition, which is gated on a flag file so the parent can prove the
# saver owns the lock mid-commit before the reader may acquire it.  It
# reports the interpreted generation and on-disk bytes.
OVERLAP_READER_SCRIPT = """\
import hashlib, json, sys, time
from pathlib import Path
import aflow.config_pair as config_pair
from aflow.config import ConfigError, load_workflow_config

pair, gate = map(Path, sys.argv[1:3])
real_lock = config_pair.configuration_pair_lock
def gated(directory):
    print("pre-lock", flush=True)
    deadline = time.monotonic() + 60
    while not gate.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("pre-lock gate timed out")
        time.sleep(0.02)
    return real_lock(directory)
config_pair.configuration_pair_lock = gated
print("ready", flush=True)
try:
    loaded = load_workflow_config(pair / "aflow.toml")
except ConfigError as exc:
    print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
    sys.exit(2)
digests = {}
for name in ("aflow.toml", "workflows.toml"):
    path = pair / name
    digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({
    "digests": digests,
    "model": loaded.harnesses["codex"].profiles["worker"].model,
    "steps": list(loaded.workflows["demo"].steps),
    "record": (pair / ".aflow-config-pair.transaction.json").is_file(),
}))
"""

# A general reader paused after its first document parse: it signals
# "parsed-first" while still holding the pair lock and waits for a flag file
# before parsing the second document.
PAUSED_READER_SCRIPT = """\
import hashlib, json, sys, time, tomllib
from pathlib import Path
from aflow.config import ConfigError, load_workflow_config

pair, flag = map(Path, sys.argv[1:3])
real_load = tomllib.load
def gap(handle):
    result = real_load(handle)
    if Path(getattr(handle, "name", "")).name == "aflow.toml":
        print("parsed-first", flush=True)
        deadline = time.monotonic() + 60
        while not flag.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("reader pause timed out")
            time.sleep(0.02)
    return result
tomllib.load = gap
try:
    loaded = load_workflow_config(pair / "aflow.toml")
except ConfigError as exc:
    print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
    sys.exit(2)
digests = {}
for name in ("aflow.toml", "workflows.toml"):
    path = pair / name
    digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({
    "digests": digests,
    "model": loaded.harnesses["codex"].profiles["worker"].model,
    "steps": list(loaded.workflows["demo"].steps),
    "record": (pair / ".aflow-config-pair.transaction.json").is_file(),
}))
"""


def _overlap_pair(tmp_path: Path) -> tuple[Path, Path]:
    pair_dir = tmp_path / "pair"
    new_dir = tmp_path / "new"
    _write_pair(pair_dir, AFLOW, WORKFLOWS)
    _write_pair(new_dir, OVERLAP_NEW_AFLOW, OVERLAP_NEW_WORKFLOWS)
    return pair_dir, new_dir


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def test_no_journal_read_overlapping_killed_service_save(tmp_path: Path) -> None:
    """A save that starts after the reader saw no journal cannot mix.

    The general reader announces it is about to read (no journal is present),
    then blocks on the pair lock while a real service save publishes the
    record and replaces the first document before dying.  The reader must
    wait, recover the complete old pair after release, and clean the record.
    """
    pair_dir, new_dir = _overlap_pair(tmp_path)
    gate = tmp_path / "reader-lock"
    reader = subprocess.Popen(
        [sys.executable, "-c", OVERLAP_READER_SCRIPT, str(pair_dir), str(gate)],
        stdout=subprocess.PIPE,
        text=True,
    )
    saver: subprocess.Popen[str] | None = None
    try:
        assert reader.stdout is not None
        assert reader.stdout.readline().strip() == "ready"
        # The reader has confirmed the absent journal and is now gated just
        # before its own pair-lock acquisition.
        assert reader.stdout.readline().strip() == "pre-lock"
        saver = subprocess.Popen(
            [
                sys.executable,
                "-c",
                OVERLAP_SAVER_SCRIPT,
                str(pair_dir),
                str(new_dir),
                "",
                "after_replacement:aflow.toml",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert saver.stdout is not None
        assert saver.stdout.readline().strip() == "starting"
        assert saver.stdout.readline().strip() == "held"
        # The saver owns the lock mid-commit while the reader is gated.
        assert reader.poll() is None
        gate.touch()
        saver.wait(timeout=60)
        assert saver.returncode == _CRASH_EXIT
        out, _ = reader.communicate(timeout=60)
    finally:
        gate.unlink(missing_ok=True)
        _terminate(reader)
        _terminate(saver)
    assert reader.returncode == 0, out
    data = json.loads(out)
    assert data["digests"] == {
        "aflow.toml": sha256(AFLOW.encode("utf-8")).hexdigest(),
        "workflows.toml": sha256(WORKFLOWS.encode("utf-8")).hexdigest(),
    }
    assert data["model"] == "test"
    assert data["steps"] == ["implement"]
    assert data["record"] is False


def test_reader_holds_lock_between_document_parses(tmp_path: Path) -> None:
    """A save cannot replace a document between the reader's two parses.

    The general reader is paused after parsing the first document while it
    still holds the pair lock; a full service save requests the lock and must
    not replace either document until the reader finishes the complete old
    pair, after which it commits the complete new pair.
    """
    pair_dir, new_dir = _overlap_pair(tmp_path)
    flag = tmp_path / "resume-reader"
    reader = subprocess.Popen(
        [sys.executable, "-c", PAUSED_READER_SCRIPT, str(pair_dir), str(flag)],
        stdout=subprocess.PIPE,
        text=True,
    )
    saver: subprocess.Popen[str] | None = None
    try:
        assert reader.stdout is not None
        assert reader.stdout.readline().strip() == "parsed-first"
        saver = subprocess.Popen(
            [
                sys.executable,
                "-c",
                OVERLAP_SAVER_SCRIPT,
                str(pair_dir),
                str(new_dir),
                "",
                "",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert saver.stdout is not None
        assert saver.stdout.readline().strip() == "starting"
        # The saver is running and requesting the lock the reader holds; it
        # cannot have replaced either document while the reader is paused.
        assert saver.poll() is None
        assert (pair_dir / "aflow.toml").read_bytes() == AFLOW.encode("utf-8")
        assert (
            pair_dir / "workflows.toml"
        ).read_bytes() == WORKFLOWS.encode("utf-8")
        flag.touch()
        out, _ = reader.communicate(timeout=60)
        saver.wait(timeout=60)
    finally:
        flag.unlink(missing_ok=True)
        _terminate(reader)
        _terminate(saver)
    assert reader.returncode == 0, out
    data = json.loads(out)
    assert data["model"] == "test"
    assert data["steps"] == ["implement"]
    assert data["record"] is False
    # The saver committed the complete new pair only after the reader done.
    assert saver.returncode == 0
    assert (
        pair_dir / "aflow.toml"
    ).read_bytes() == OVERLAP_NEW_AFLOW.encode("utf-8")
    assert (
        pair_dir / "workflows.toml"
    ).read_bytes() == OVERLAP_NEW_WORKFLOWS.encode("utf-8")
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()


def _run(script: str, *args: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *[str(arg) for arg in args]],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _write_pair(directory: Path, aflow: str, workflows: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "aflow.toml").write_text(aflow, encoding="utf-8")
    (directory / "workflows.toml").write_text(workflows, encoding="utf-8")


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _renamed_pair() -> tuple[str, str]:
    """The committed rename: prompt work -> renamed plus its references."""
    payload = GlobalConfigPatchPayload(
        expected_revision="0" * 64,
        actions=[{"type": "rename_prompt", "name": "work", "new_name": "renamed"}],
    )
    return apply_action_batch(AFLOW, WORKFLOWS, payload.actions)


@pytest.fixture
def service(tmp_path: Path) -> GlobalConfigService:
    _write_pair(tmp_path, AFLOW, WORKFLOWS)
    return GlobalConfigService(config_dir=tmp_path, audit_path=tmp_path / "audit.jsonl")


def _mcp_server(service: GlobalConfigService) -> FastMCP:
    mcp = FastMCP("config-pair-parity")
    register_global_config_tools(
        mcp,
        lambda: service,
        lambda operation, arguments: _tool_result(
            operation,
            arguments,
            extra_error_codes={
                ProjectConfigRevisionConflict: "revision_conflict",
                ProjectConfigError: "operation_rejected",
            },
        ),
    )
    return mcp


def _mcp_call(mcp: FastMCP, name: str, arguments: dict) -> object:
    async def run() -> object:
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def test_rest_and_mcp_config_tools_commit_and_read_the_same_pair(
    service: GlobalConfigService,
) -> None:
    rest_snapshot = service.save(
        *_renamed_pair(),
        service.read().revision,
        caller_scope="rest",
    )

    mcp = _mcp_server(service)
    read_result = _mcp_call(mcp, "get_global_config", {})
    assert read_result.is_error is False
    assert read_result.data == config_response(service.read()).model_dump(
        mode="json"
    )
    assert read_result.data["aflow_toml"] == rest_snapshot.aflow_toml
    assert read_result.data["workflows_toml"] == rest_snapshot.workflows_toml
    assert read_result.data["revision"] == rest_snapshot.revision

    patch_result = _mcp_call(
        mcp,
        "patch_global_config",
        {
            "payload": {
                "expected_revision": rest_snapshot.revision,
                "actions": [
                    {
                        "type": "upsert_profile",
                        "harness": "codex",
                        "profile": "parity",
                        "model": "parity-model",
                    },
                ],
            },
        },
    )
    assert patch_result.is_error is False
    assert patch_result.data["revision"] == service.read().revision
    assert 'model = "parity-model"' in service.read().aflow_toml

    # Both transports recorded the same public save outcomes.
    audit = [
        json.loads(line)
        for line in (service.config_dir / "audit.jsonl").read_text().splitlines()
    ]
    assert [record["caller_scope"] for record in audit] == ["rest", "mcp"]
    assert all(record["outcome"] == "saved" for record in audit)
    assert audit[0]["new_revision"] == rest_snapshot.revision
    assert audit[1]["old_revision"] == rest_snapshot.revision


def test_stale_config_revision_is_a_public_conflict_on_both_transports(
    service: GlobalConfigService,
) -> None:
    before = service.read()
    with pytest.raises(ProjectConfigRevisionConflict):
        service.save(AFLOW, WORKFLOWS, "0" * 64, caller_scope="rest")

    mcp = _mcp_server(service)
    with pytest.raises(ToolError) as excinfo:
        _mcp_call(
            mcp,
            "patch_global_config",
            {
                "payload": {
                    "expected_revision": "0" * 64,
                    "documents": {"aflow.toml": AFLOW},
                },
            },
        )
    assert "revision_conflict" in str(excinfo.value)
    assert service.read() == before

    # The REST save recorded the public conflict outcome.
    audit = [
        json.loads(line)
        for line in (service.config_dir / "audit.jsonl").read_text().splitlines()
    ]
    assert audit[-1] == {
        "schema_version": 1,
        "timestamp": audit[-1]["timestamp"],
        "project_id": "global",
        "outcome": "revision_conflict",
        "old_revision": before.revision,
        "new_revision": None,
        "caller_scope": "rest",
    }


def test_invalid_config_candidate_preserves_exact_bytes(service: GlobalConfigService) -> None:
    before = service.read()
    aflow_path = service.config_dir / "aflow.toml"
    workflows_path = service.config_dir / "workflows.toml"
    with pytest.raises(ProjectConfigError, match="invalid"):
        service.save(
            "not toml at all",
            WORKFLOWS,
            before.revision,
            caller_scope="rest",
        )
    assert service.read() == before
    assert not (service.config_dir / TRANSACTION_RECORD_NAME).exists()
    audit = [
        json.loads(line)
        for line in (service.config_dir / "audit.jsonl").read_text().splitlines()
    ]
    assert audit[-1]["outcome"] == "rejected"
    assert audit[-1]["new_revision"] is None


def _crash_commit(tmp_path: Path, crash_point: str) -> tuple[Path, Path]:
    """Kill a real commit at one durable boundary; return the directories."""
    pair_dir = tmp_path / "pair"
    new_dir = tmp_path / "new"
    _write_pair(pair_dir, AFLOW, WORKFLOWS)
    renamed_aflow, renamed_workflows = _renamed_pair()
    _write_pair(new_dir, renamed_aflow, renamed_workflows)
    expected = pair_revision(
        (pair_dir / "aflow.toml").read_bytes(),
        (pair_dir / "workflows.toml").read_bytes(),
    )
    saver = _run(SAVER_SCRIPT, pair_dir, new_dir, expected, crash_point)
    assert saver.returncode == _CRASH_EXIT, saver.stderr
    return pair_dir, new_dir


def test_killed_prompt_rename_config_save_recovers_old_pair_together(
    tmp_path: Path,
) -> None:
    pair_dir, new_dir = _crash_commit(
        tmp_path, "after_replacement:aflow.toml"
    )
    # The crash left the pair mixed on disk: new aflow, old workflows.
    assert _digest(pair_dir / "aflow.toml") == sha256(
        new_dir.joinpath("aflow.toml").read_bytes()
    ).hexdigest()
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()

    rest = _run(REST_READER_SCRIPT, pair_dir)
    assert rest.returncode == 0, rest.stderr
    rest_data = json.loads(rest.stdout)
    assert rest_data["revision"] == pair_revision(
        AFLOW.encode("utf-8"), WORKFLOWS.encode("utf-8")
    )
    assert rest_data["aflow"] == AFLOW
    assert rest_data["workflows"] == WORKFLOWS
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()

    live = _run(LIVE_READER_SCRIPT, pair_dir)
    assert live.returncode == 0, live.stderr
    live_data = json.loads(live.stdout)
    assert live_data["digests"] == {
        "aflow.toml": sha256(AFLOW.encode("utf-8")).hexdigest(),
        "workflows.toml": sha256(WORKFLOWS.encode("utf-8")).hexdigest(),
    }
    assert live_data["record"] is False
    # The renamed prompt and its workflow reference never appeared half-renamed.
    assert live_data["prompts"] == ["work"]
    assert "renamed" not in live_data["workflows"]


def _service_crash_commit(tmp_path: Path, crash_point: str) -> tuple[Path, Path]:
    """Kill a real GlobalConfigService.save at one durable boundary."""
    pair_dir = tmp_path / "pair"
    new_dir = tmp_path / "new"
    _write_pair(pair_dir, AFLOW, WORKFLOWS)
    _write_pair(new_dir, *_renamed_pair())
    saver = _run(SERVICE_SAVER_SCRIPT, pair_dir, new_dir, crash_point)
    assert saver.returncode == _CRASH_EXIT, saver.stderr
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()
    return pair_dir, new_dir


def test_killed_service_save_general_loader_recovers_old_generation(
    tmp_path: Path,
) -> None:
    """The public filesystem reader recovers before any REST/live reader."""
    pair_dir, new_dir = _service_crash_commit(
        tmp_path, "after_replacement:aflow.toml"
    )
    # The service save died after the first replacement: mixed pair on disk.
    assert _digest(pair_dir / "aflow.toml") == sha256(
        new_dir.joinpath("aflow.toml").read_bytes()
    ).hexdigest()

    general = _run(GENERAL_READER_SCRIPT, pair_dir)
    assert general.returncode == 0, general.stderr
    general_data = json.loads(general.stdout)
    assert general_data["digests"] == {
        "aflow.toml": sha256(AFLOW.encode("utf-8")).hexdigest(),
        "workflows.toml": sha256(WORKFLOWS.encode("utf-8")).hexdigest(),
    }
    assert general_data["prompts"] == ["work"]
    assert general_data["record"] is False


def test_committed_service_save_general_loader_recovers_new_generation(
    tmp_path: Path,
) -> None:
    pair_dir, new_dir = _service_crash_commit(tmp_path, "after_committed_marker")
    renamed_aflow, renamed_workflows = _renamed_pair()

    general = _run(GENERAL_READER_SCRIPT, pair_dir)
    assert general.returncode == 0, general.stderr
    general_data = json.loads(general.stdout)
    assert general_data["digests"] == {
        "aflow.toml": sha256(renamed_aflow.encode("utf-8")).hexdigest(),
        "workflows.toml": sha256(renamed_workflows.encode("utf-8")).hexdigest(),
    }
    assert general_data["prompts"] == ["renamed"]
    assert general_data["record"] is False


def test_killed_service_save_pending_manual_edit_general_loader_fails_closed(
    tmp_path: Path,
) -> None:
    pair_dir, _ = _service_crash_commit(
        tmp_path, "after_replacement:aflow.toml"
    )
    aflow_path = pair_dir / "aflow.toml"
    aflow_path.write_text(
        AFLOW.replace('model = "test"', 'model = "operator-model"'),
        encoding="utf-8",
    )
    edited = aflow_path.read_bytes()
    record = pair_dir / TRANSACTION_RECORD_NAME
    record_bytes = record.read_bytes()

    general = _run(GENERAL_READER_SCRIPT, pair_dir)
    assert general.returncode == 2
    general_data = json.loads(general.stdout)
    assert general_data["type"] == "ConfigError"
    assert "recovery failed" in general_data["error"]
    # Neither document nor the transaction record was changed.
    assert aflow_path.read_bytes() == edited
    assert record.read_bytes() == record_bytes


def test_committed_config_crash_installs_new_pair_for_rest_and_live_reads(
    tmp_path: Path,
) -> None:
    pair_dir, new_dir = _crash_commit(tmp_path, "after_committed_marker")
    renamed_aflow, renamed_workflows = _renamed_pair()
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()

    rest = _run(REST_READER_SCRIPT, pair_dir)
    assert rest.returncode == 0, rest.stderr
    rest_data = json.loads(rest.stdout)
    assert rest_data["revision"] == pair_revision(
        renamed_aflow.encode("utf-8"), renamed_workflows.encode("utf-8")
    )
    assert rest_data["aflow"] == renamed_aflow
    assert rest_data["workflows"] == renamed_workflows
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()

    live = _run(LIVE_READER_SCRIPT, pair_dir)
    assert live.returncode == 0, live.stderr
    live_data = json.loads(live.stdout)
    assert live_data["digests"] == {
        "aflow.toml": sha256(renamed_aflow.encode("utf-8")).hexdigest(),
        "workflows.toml": sha256(renamed_workflows.encode("utf-8")).hexdigest(),
    }
    assert live_data["record"] is False
    assert live_data["prompts"] == ["renamed"]


def test_killed_config_save_recovers_over_the_http_config_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    control_client,  # noqa: F811
) -> None:
    """The real HTTP GET /api/config boundary over a disposable directory."""
    from aflow_app_server import config as config_module
    from aflow_app_server import main

    _, root, _, _ = control_client
    global_dir = root.parent / "global"
    new_dir = root.parent / "global-new"
    _write_pair(global_dir, AFLOW, WORKFLOWS)
    _write_pair(new_dir, *_renamed_pair())
    expected = pair_revision(
        (global_dir / "aflow.toml").read_bytes(),
        (global_dir / "workflows.toml").read_bytes(),
    )
    saver = _run(SAVER_SCRIPT, global_dir, new_dir, expected, "after_replacement:aflow.toml")
    assert saver.returncode == _CRASH_EXIT, saver.stderr
    assert (global_dir / TRANSACTION_RECORD_NAME).is_file()

    config = main._config
    control_service = main._control_plane_service
    assert config is not None
    assert control_service is not None
    monkeypatch.setattr(
        main.ServerConfig, "from_env", classmethod(lambda _cls: config)
    )
    monkeypatch.setattr(main, "ControlPlaneService", lambda _projects: control_service)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: global_dir)

    with TestClient(main.app) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert data["aflow_toml"] == AFLOW
    assert data["workflows_toml"] == WORKFLOWS
    assert not (global_dir / TRANSACTION_RECORD_NAME).exists()

    # The live loader on the same disposable directory sees the old pair.
    live = _run(LIVE_READER_SCRIPT, global_dir)
    assert live.returncode == 0, live.stderr
    live_data = json.loads(live.stdout)
    assert live_data["digests"]["aflow.toml"] == sha256(AFLOW.encode("utf-8")).hexdigest()
    assert live_data["record"] is False


def test_manual_config_edit_during_pending_recovery_is_preserved_and_rejected(
    tmp_path: Path,
) -> None:
    pair_dir, _ = _crash_commit(tmp_path, "after_record")
    aflow_path = pair_dir / "aflow.toml"
    edited = "operator = \"manual edit\"\n"
    aflow_path.write_text(edited, encoding="utf-8")
    edited_digest = _digest(aflow_path)
    record = pair_dir / TRANSACTION_RECORD_NAME
    record_bytes = record.read_bytes()

    # REST boundary: bounded explicit failure, evidence preserved.
    rest = _run(REST_READER_SCRIPT, pair_dir)
    assert rest.returncode != 0
    assert "recovery failed" in rest.stderr
    assert _digest(aflow_path) == edited_digest
    assert record.read_bytes() == record_bytes

    # Live boundary: the engine's bounded config-error contract, preserved too.
    live = _run(LIVE_READER_SCRIPT, pair_dir)
    assert live.returncode != 0
    assert "recovery failed" in live.stderr
    assert _digest(aflow_path) == edited_digest
    assert record.read_bytes() == record_bytes

    # In-process REST read raises the service's public error type.
    service = GlobalConfigService(
        config_dir=pair_dir, audit_path=pair_dir / "audit.jsonl"
    )
    with pytest.raises(ProjectConfigError, match="recovery failed"):
        service.read()


def test_pending_config_transaction_yields_nonblocking_live_reads(
    tmp_path: Path,
) -> None:
    pair_dir, _ = _crash_commit(tmp_path, "after_record")
    assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()

    # A competing process holds the pair lock; a nonblocking live read must
    # yield back to its control loop instead of falling back to an unlocked
    # read, and must not consume the pending record.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from pathlib import Path\n"
            "from aflow.config_pair import configuration_pair_lock\n"
            "with configuration_pair_lock(Path(sys.argv[1])):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(float(sys.argv[2]))\n",
            str(pair_dir),
            "5",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(LiveConfigPairLockBusy):
            load_live_config(pair_dir / "aflow.toml", nonblocking=True)
        assert (pair_dir / TRANSACTION_RECORD_NAME).is_file()
    finally:
        holder.wait(timeout=30)

    # After the holder exits, a blocking live read recovers the old pair.
    loaded = load_live_config(pair_dir / "aflow.toml")
    assert sorted(loaded.workflow_config.prompts) == ["work"]
    assert not (pair_dir / TRANSACTION_RECORD_NAME).exists()

from __future__ import annotations

import importlib.util
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit

import pytest
import yaml

from aflow.issue_intake import source_hash


ROOT = Path(__file__).resolve().parents[1]
RELAY_PATH = ROOT / "deploy" / "issue-intake" / "relay.py"
WORKFLOW_PATH = ROOT / "deploy" / "issue-intake" / "github-actions.example.yml"


def _relay_module():
    spec = importlib.util.spec_from_file_location("issue_intake_relay", RELAY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(*, actor_id: int = 591691, author_id: int = 591691) -> dict[str, object]:
    return {
        "action": "opened",
        "repository": {"id": 123, "full_name": "owner/repo"},
        "issue": {
            "number": 7,
            "title": "Keep this private",
            "body": "Do not forward this body.",
            "user": {"id": author_id},
        },
        "sender": {"id": actor_id},
    }


def test_relay_passes_only_metadata_to_fixed_local_host_command(tmp_path: Path) -> None:
    relay = _relay_module()
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event()), encoding="utf-8")
    calls: list[tuple[list[str], bytes, bool]] = []

    def fake_run(command: list[str], *, input: bytes, check: bool) -> None:
        calls.append((command, input, check))

    result = relay.main(
        ["--host-command", "/opt/aflow/bin/intake", "--config", "/etc/aflow/intake.toml"],
        environ={
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": "issues",
            "GITHUB_RUN_ID": "run-123",
        },
        run=fake_run,
    )
    assert result == 0
    assert calls[0][0] == [
        "/opt/aflow/bin/intake",
        "--config",
        "/etc/aflow/intake.toml",
        "--event-file",
        "-",
    ]
    envelope = json.loads(calls[0][1])
    assert envelope == {
        "action": "opened",
        "actor_id": 591691,
        "delivery_id": "run-123",
        "issue_number": 7,
        "repository_full_name": "owner/repo",
        "repository_id": 123,
        "title_body_sha256": source_hash("Keep this private", "Do not forward this body."),
    }
    assert b"Keep this private" not in calls[0][1]
    assert b"Do not forward this body." not in calls[0][1]


@pytest.mark.parametrize("field", ["actor", "author", "action", "kind"])
def test_relay_rejects_non_owner_or_non_issue_admission_before_host_call(tmp_path: Path, field: str) -> None:
    relay = _relay_module()
    event = _event(actor_id=42 if field == "actor" else 591691, author_id=42 if field == "author" else 591691)
    if field == "action":
        event["action"] = "labeled"
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    calls: list[object] = []
    result = relay.main(
        ["--host-command", "/opt/aflow/bin/intake", "--config", "/etc/aflow/intake.toml"],
        environ={
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": "pull_request" if field == "kind" else "issues",
            "GITHUB_RUN_ID": "run-123",
        },
        run=lambda **kwargs: calls.append(kwargs),
    )
    assert result == 2
    assert calls == []


def test_relay_uses_private_ssh_without_event_supplied_command() -> None:
    relay = _relay_module()
    parser = relay.build_parser()
    args = parser.parse_args(
        [
            "--host-command",
            "/opt/aflow/bin/intake",
            "--config",
            "/etc/aflow/intake.toml",
            "--ssh-host",
            "intake.tailnet",
            "--ssh-user",
            "aflow",
            "--ssh-identity-file",
            "/etc/aflow/ssh/id_ed25519",
        ]
    )
    assert relay.host_command(args) == [
        "ssh",
        "-i",
        "/etc/aflow/ssh/id_ed25519",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "aflow@intake.tailnet",
        "/opt/aflow/bin/intake",
        "--config",
        "/etc/aflow/intake.toml",
        "--event-file",
        "-",
    ]


def test_disabled_workflow_example_has_owner_gate_and_per_issue_concurrency() -> None:
    assert not (ROOT / ".github" / "workflows" / "issue-intake.yml").exists()
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert document[True]["issues"]["types"] == ["opened", "edited"]
    job = document["jobs"]["intake"]
    assert "github.event.issue.user.id == 591691" in job["if"]
    assert "github.event.sender.id == 591691" in job["if"]
    assert "github.event.repository.id" in job["concurrency"]["group"]
    assert "github.event.issue.number" in job["concurrency"]["group"]
    assert job["concurrency"]["cancel-in-progress"] is False
    assert "GITHUB_EVENT_PATH" not in text
    assert "secrets." not in text


class _FakeIntakeAPI:
    def __init__(self, root: Path, *, body: str) -> None:
        self.root = root
        self.body = body
        self.plan = b"# Plan\n\n### [ ] Checkpoint 1: Existing\n- [ ] implement\n"
        self.plans: dict[tuple[str, str], bytes] = {("todo", "source.md"): self.plan}
        self.start_keys: list[str] = []
        self.start_bodies: list[bytes] = []
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return None

            def reply(self, status: int, payload: object) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def document(self, status: str, name: str) -> dict[str, object] | None:
                content = api.plans.get((status, name))
                if content is None:
                    return None
                return {
                    "project_id": "project",
                    "name": name,
                    "path": f"plans/{'in-progress' if status == 'in_progress' else status}/{name}",
                    "status": status,
                    "revision": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "content": content.decode("utf-8"),
                }

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/repos/owner/repo":
                    self.reply(200, {"id": 123, "full_name": "owner/repo"})
                    return
                if path == "/repos/owner/repo/issues/7":
                    self.reply(
                        200,
                        {
                            "number": 7,
                            "title": "Owner request",
                            "body": api.body,
                            "user": {"id": 591691},
                            "state": "open",
                            "labels": [],
                        },
                    )
                    return
                if path == "/api/control-plane/projects":
                    self.reply(
                        200,
                        {"projects": [{"project_id": "project", "root": str(api.root), "schema_version": 1}]},
                    )
                    return
                if path.endswith("/capabilities"):
                    self.reply(
                        200,
                        {
                            "schema_version": 1,
                            "workflows": ["cumulative_delivery"],
                            "teams": ["executor"],
                            "workflow_details": {
                                "cumulative_delivery": {
                                    "declared_steps": ["implement"],
                                    "executable_steps": ["implement"],
                                    "excluded_steps": [],
                                    "first_step": "implement",
                                    "default_team": "executor",
                                }
                            },
                            "service_features": ["daemon_lifecycle"],
                        },
                    )
                    return
                if "/plans/" in path:
                    pieces = path.split("/")
                    status = "in_progress" if pieces[-2] == "in-progress" else pieces[-2]
                    document = self.document(status, pieces[-1])
                    if document is None:
                        self.reply(404, {"detail": "not found"})
                    else:
                        self.reply(200, document)
                    return
                self.reply(404, {"detail": "not found"})

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(size)
                payload = json.loads(raw.decode("utf-8"))
                if path == "/api/projects/project/plans":
                    name = payload["name"]
                    content = payload["content"].encode("utf-8")
                    api.plans[("todo", name)] = content
                    self.reply(200, self.document("todo", name))
                    return
                if path.endswith("/promote"):
                    name = path.split("/")[-2]
                    content = api.plans.pop(("todo", name), None)
                    if content is None:
                        self.reply(409, {"detail": "missing"})
                    else:
                        api.plans[("in_progress", name)] = content
                        self.reply(200, self.document("in_progress", name))
                    return
                if path.endswith("/runs/preflight"):
                    self.reply(
                        200,
                        {
                            "checkout_path": str(api.root / ".worktrees" / "intake"),
                            "execution_mode": "new_worktree",
                            "dirty": False,
                            "requires_confirmation": False,
                            "blockers": [],
                            "total_items": 0,
                            "offset": 0,
                            "limit": 1,
                        },
                    )
                    return
                if path.endswith("/runs"):
                    api.start_keys.append(self.headers.get("Idempotency-Key", ""))
                    api.start_bodies.append(raw)
                    self.reply(
                        200,
                        {"result": {"run_id": "run-1", "created": True, "status": "launch_requested"}},
                    )
                    return
                self.reply(404, {"detail": "not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _git_fixture(root: Path) -> None:
    root.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


@pytest.mark.parametrize("attached", [True, False])
def test_process_level_relay_host_and_fake_api_routes_redelivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attached: bool
) -> None:
    plan = b"# Plan\n\n### [ ] Checkpoint 1: Existing\n- [ ] implement\n"
    generated_plan = b"# Plan\n\n### [ ] Checkpoint 1: Generated\n- [ ] implement\n"
    body = (
        "AFlow-Plan: plans/todo/source.md\n"
        f"AFlow-Plan-SHA256: {hashlib.sha256(plan).hexdigest()}\n"
        if attached
        else ""
    )
    project_root = tmp_path / "project"
    _git_fixture(project_root)
    api = _FakeIntakeAPI(project_root, body=body)
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    payload_path = tmp_path / "relay-payloads.jsonl"
    codex_count = tmp_path / "codex-count"
    wrapper = bin_root / "host-intake"
    _write_executable(
        wrapper,
        "#!/usr/bin/env python3\n"
        "import io, os, sys\n"
        "from pathlib import Path\n"
        "from aflow.issue_intake import main\n"
        "data = sys.stdin.buffer.read()\n"
        "with Path(os.environ['RELAY_PAYLOADS']).open('ab') as handle: handle.write(data + b'\\n')\n"
        "raise SystemExit(main(sys.argv[1:], stdin=io.BytesIO(data)))\n",
    )
    _write_executable(
        bin_root / "codex",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "count = Path(os.environ['CODEX_COUNT'])\n"
        "current = int(count.read_text() if count.exists() else '0') + 1\n"
        "count.write_text(str(current))\n"
        "sys.stdin.buffer.read()\n"
        "output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "output.write_text(json.dumps({'status': 'plan', 'markdown': '# Plan\\n\\n### [ ] Checkpoint 1: Generated\\n- [ ] implement\\n'}))\n",
    )
    private_token = tmp_path / "private.token"
    github_token = tmp_path / "github.token"
    private_token.write_text("private\n", encoding="utf-8")
    github_token.write_text("github\n", encoding="utf-8")
    config = tmp_path / "intake.toml"
    config.write_text(
        "[issue_intake]\n"
        "enabled = true\n"
        f"state_root = {str(tmp_path / 'state')!r}\n"
        f"private_api_url = {api.base_url!r}\n"
        f"private_credential_file = {str(private_token)!r}\n"
        f"github_api_url = {api.base_url!r}\n"
        f"github_credential_file = {str(github_token)!r}\n"
        "deferred_labels = []\nworkflow = 'cumulative_delivery'\nteam = 'executor'\n\n"
        "[[issue_intake.repositories]]\nrepository_id = 123\nfull_name = 'owner/repo'\nproject_id = 'project'\n",
        encoding="utf-8",
    )
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "opened",
                "repository": {"id": 123, "full_name": "owner/repo"},
                "issue": {"number": 7, "title": "Owner request", "body": body, "user": {"id": 591691}},
                "sender": {"id": 591691},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("RELAY_PAYLOADS", str(payload_path))
    monkeypatch.setenv("CODEX_COUNT", str(codex_count))
    monkeypatch.setenv("PATH", str(bin_root) + os.pathsep + os.environ["PATH"])
    relay = _relay_module()
    try:
        monkeypatch.setenv("GITHUB_RUN_ID", "run-1")
        assert relay.main(["--host-command", str(wrapper), "--config", str(config)]) == 0
        monkeypatch.setenv("GITHUB_RUN_ID", "run-2")
        assert relay.main(["--host-command", str(wrapper), "--config", str(config)]) == 0
        payloads = [json.loads(line) for line in payload_path.read_text(encoding="utf-8").splitlines()]
        assert len(payloads) == 2
        assert payloads[0]["delivery_id"] == "run-1"
        assert payloads[1]["delivery_id"] == "run-2"
        assert all("Owner request" not in json.dumps(payload) for payload in payloads)
        if body:
            assert all(body not in json.dumps(payload) for payload in payloads)
        assert len(api.start_keys) == 1
        assert api.start_keys[0]
        start_payload = json.loads(api.start_bodies[0])
        assert start_payload["plan_path"].startswith("plans/in-progress/")
        plan_name = Path(start_payload["plan_path"]).name
        assert api.plans[("in_progress", plan_name)] == (
            plan if attached else generated_plan
        )
        count = int(codex_count.read_text()) if codex_count.exists() else 0
        assert count == (0 if attached else 1)
    finally:
        api.close()

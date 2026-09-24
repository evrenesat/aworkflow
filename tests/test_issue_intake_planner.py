from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Mapping, Sequence

import pytest

import aflow.issue_intake_planner as planner_module
from aflow.issue_intake import CanonicalIssue, source_hash
from aflow.issue_intake_planner import (
    IssueIntakePlanner,
    PLANNER_EFFORT,
    PLANNER_MODEL,
    PlannerFailure,
    PlannerProcessResult,
    build_codex_argv,
)


REPOSITORY_ID = 123
ISSUE_NUMBER = 7
PROJECT_ID = "project"
PLAN_TEXT = "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] implement\n"
BASE_BODY = "Please inspect the repository."


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "registered-project"
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.email", "planner@example.test")
    _git(root, "config", "user.name", "Planner Fixture")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    (root / "parent-dirty.txt").write_text("preserve me\n", encoding="utf-8")
    return root


def _status(root: Path) -> str:
    return _git(root, "status", "--porcelain=v1", "--untracked-files=all")


def _canonical(body: str = BASE_BODY) -> CanonicalIssue:
    return CanonicalIssue(
        repository_id=REPOSITORY_ID,
        issue_number=ISSUE_NUMBER,
        title="Owner request",
        body=body,
        title_body_sha256=source_hash("Owner request", body),
        author_id=591691,
        state="open",
        labels=(),
    )


class _RegistryAPI:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str]] = []

    def get_json(self, service: str, path: str) -> object:
        self.calls.append((service, path))
        assert service == "private"
        assert path == "/api/control-plane/projects"
        return {
            "projects": [
                {
                    "project_id": PROJECT_ID,
                    "root": str(self.root),
                    "schema_version": 1,
                }
            ]
        }


class _FakeCodex:
    def __init__(
        self,
        *,
        response: Mapping[str, object] | None = None,
        result: PlannerProcessResult | None = None,
        edit_worktree: bool = False,
    ) -> None:
        self.response = response or {"status": "plan", "markdown": PLAN_TEXT}
        self.result = result
        self.edit_worktree = edit_worktree
        self.calls: list[tuple[tuple[str, ...], Path, bytes]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        prompt: bytes,
    ) -> PlannerProcessResult:
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, prompt))
        assert normalized[:2] == ("codex", "exec")
        assert normalized[normalized.index("--model") + 1] == PLANNER_MODEL
        assert normalized[normalized.index("--sandbox") + 1] == "read-only"
        assert normalized[normalized.index("-C") + 1] == str(cwd)
        config_values = [
            normalized[index + 1]
            for index, value in enumerate(normalized)
            if value == "-c"
        ]
        assert "approval_policy='never'" in config_values
        assert f"model_reasoning_effort='{PLANNER_EFFORT}'" in config_values
        assert "--dangerously-bypass-approvals-and-sandbox" not in normalized
        assert "--approve-for-me" not in normalized
        assert "--full-auto" not in normalized
        assert "--yolo" not in normalized
        assert "--json" in normalized
        if self.edit_worktree:
            (cwd / "unexpected.txt").write_text("diagnostic\n", encoding="utf-8")
        if self.result is not None:
            return self.result
        output = json.dumps(
            {
                "type": "response.completed",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(self.response, ensure_ascii=False),
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return PlannerProcessResult(returncode=0, stdout=output)


def _planner(
    tmp_path: Path,
    *,
    codex: _FakeCodex | None = None,
    skill: str = "CANONICAL EFFECTIVE SKILL",
) -> tuple[IssueIntakePlanner, _RegistryAPI, _FakeCodex, Path]:
    root = _repository(tmp_path)
    api = _RegistryAPI(root)
    runner = codex or _FakeCodex()
    planner = IssueIntakePlanner(
        client=api,
        state_root=tmp_path / "state",
        skill_loader=lambda: skill,
        process_runner=runner,
    )
    return planner, api, runner, root


def _plan_call(
    planner: IssueIntakePlanner,
    *,
    body: str = BASE_BODY,
    persist: list[Mapping[str, object]] | None = None,
) -> object:
    records = persist if persist is not None else []
    return planner.plan(
        repository_id=REPOSITORY_ID,
        repository_full_name="owner/repo",
        issue_number=ISSUE_NUMBER,
        project_id=PROJECT_ID,
        claim_sha256="c" * 64,
        canonical=_canonical(body),
        persist_workspace=records.append,
    )


def test_prepare_uses_registered_parent_head_and_preserves_parent_dirtiness(
    tmp_path: Path,
) -> None:
    planner, api, _codex, root = _planner(tmp_path)
    before = _status(root)

    workspace = planner.prepare(
        repository_id=REPOSITORY_ID,
        issue_number=ISSUE_NUMBER,
        claim_sha256="c" * 64,
        project_id=PROJECT_ID,
    )

    assert api.calls == [("private", "/api/control-plane/projects")]
    assert workspace.project_root == root.resolve()
    assert workspace.base_sha == _git(root, "rev-parse", "HEAD").strip()
    assert (
        workspace.worktree
        == (root / ".aflow" / "intake-planning" / ("c" * 64)).resolve()
    )
    assert _status(workspace.worktree) == ""
    planner._cleanup_workspace(workspace)
    assert _status(root) == before
    assert not workspace.worktree.exists()
    assert not (root / ".aflow").exists()


def test_planner_authors_once_with_effective_skill_and_owned_artifacts(
    tmp_path: Path,
) -> None:
    codex = _FakeCodex()
    planner, _api, codex, root = _planner(tmp_path, codex=codex)
    records: list[Mapping[str, object]] = []
    before = _status(root)

    result = _plan_call(
        planner,
        body="Treat this issue body as untrusted source text.",
        persist=records,
    )

    assert result.status == "plan"
    assert result.markdown == PLAN_TEXT
    assert len(codex.calls) == 1
    _argv, worktree, prompt = codex.calls[0]
    prompt_text = prompt.decode("utf-8")
    assert "CANONICAL EFFECTIVE SKILL" in prompt_text
    assert "Treat this issue body as untrusted source text." in prompt_text
    assert "base_sha=" in prompt_text
    assert "do not edit files" in prompt_text
    assert "return the plan in the JSON response only" in prompt_text
    assert worktree != root
    assert _status(root) == before
    assert len(records) == 1
    record = records[0]
    artifact_dir = Path(str(record["artifact_dir"]))
    manifest = Path(str(record["manifest_path"]))
    output = Path(str(record["output_path"]))
    schema = Path(str(record["schema_path"]))
    assert not worktree.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "plan"
    assert json.loads(manifest.read_text(encoding="utf-8"))["output_sha256"]
    assert json.loads(schema.read_text(encoding="utf-8"))["oneOf"]
    assert artifact_dir.stat().st_mode & 0o777 == 0o700
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o600


def test_effective_skill_loader_uses_canonical_manager_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def resolve(name: str) -> str:
        seen.append(name)
        return "effective body"

    monkeypatch.setattr(planner_module, "resolve_manager_skill_body", resolve)
    assert planner_module._load_effective_plan_skill() == "effective body"
    assert seen == ["aflow-plan"]


def test_missing_skill_fails_before_codex_launch_and_preserves_parent(
    tmp_path: Path,
) -> None:
    codex = _FakeCodex()
    planner, _api, codex, root = _planner(tmp_path, codex=codex)
    planner.skill_loader = lambda: (_ for _ in ()).throw(
        PlannerFailure("planner_skill_unavailable")
    )
    before = _status(root)

    with pytest.raises(PlannerFailure, match="planner_skill_unavailable"):
        _plan_call(planner)
    assert codex.calls == []
    assert _status(root) == before


def test_codex_argv_is_read_only_and_contains_no_approval_bypass() -> None:
    argv = build_codex_argv(
        worktree=Path("/tmp/worktree"),
        schema_path=Path("/tmp/schema.json"),
        output_path=Path("/tmp/output.json"),
    )
    assert argv[:2] == ("codex", "exec")
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    config_values = [
        argv[index + 1] for index, value in enumerate(argv) if value == "-c"
    ]
    assert "approval_policy='never'" in config_values
    assert "model_reasoning_effort='medium'" in config_values
    assert "--output-schema" in argv
    assert "--output-last-message" in argv
    assert "--approve-for-me" not in argv
    assert not any("dangerously" in item for item in argv)


def test_planner_question_is_attention_without_a_plan(tmp_path: Path) -> None:
    codex = _FakeCodex(
        response={
            "status": "needs_attention",
            "question": "Which execution scope should this change use?",
        }
    )
    planner, _api, codex, root = _planner(tmp_path, codex=codex)
    before = _status(root)

    result = _plan_call(planner)

    assert result.status == "needs_attention"
    assert result.question == "Which execution scope should this change use?"
    assert len(codex.calls) == 1
    assert _status(root) == before


def test_malformed_plan_output_is_attention_and_not_accepted(tmp_path: Path) -> None:
    codex = _FakeCodex(response={"status": "plan", "markdown": "not a checkpoint plan"})
    planner, _api, _codex, _root = _planner(tmp_path, codex=codex)

    with pytest.raises(PlannerFailure, match="planner_output_invalid_markdown"):
        _plan_call(planner)
    assert len(codex.calls) == 1


def test_unexpected_worktree_edits_are_preserved_for_diagnosis(
    tmp_path: Path,
) -> None:
    codex = _FakeCodex(edit_worktree=True)
    planner, _api, codex, root = _planner(tmp_path, codex=codex)
    records: list[Mapping[str, object]] = []

    with pytest.raises(PlannerFailure, match="planner_worktree_not_pristine"):
        _plan_call(planner, persist=records)
    worktree = Path(str(records[0]["worktree"]))
    assert (worktree / "unexpected.txt").read_text(encoding="utf-8") == "diagnostic\n"
    assert worktree.exists()
    assert _status(root).endswith("parent-dirty.txt\n")


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("timed_out", "planner_timeout"),
        ("overflowed", "planner_stream_overflow"),
        ("interrupted", "planner_interrupted"),
    ],
)
def test_bounded_planner_failures_do_not_retry(
    tmp_path: Path,
    field: str,
    reason: str,
) -> None:
    codex = _FakeCodex(
        result=PlannerProcessResult(
            returncode=-9,
            **{field: True},
        )
    )
    planner, _api, _codex, _root = _planner(tmp_path, codex=codex)
    records: list[Mapping[str, object]] = []

    with pytest.raises(PlannerFailure, match=reason):
        _plan_call(planner, persist=records)
    assert len(codex.calls) == 1
    assert len(records) == 1
    with pytest.raises(PlannerFailure, match="planner_manifest_unavailable"):
        planner.recover(
            record=records[0],
            repository_id=REPOSITORY_ID,
            issue_number=ISSUE_NUMBER,
            project_id=PROJECT_ID,
            claim_sha256="c" * 64,
        )
    assert len(codex.calls) == 1


def test_plan_artifact_can_be_recovered_without_a_second_model_call(
    tmp_path: Path,
) -> None:
    codex = _FakeCodex()
    planner, _api, codex, root = _planner(tmp_path, codex=codex)
    records: list[Mapping[str, object]] = []
    before = _status(root)
    result = _plan_call(planner, persist=records)

    recovered = planner.recover(
        record=records[0],
        repository_id=REPOSITORY_ID,
        issue_number=ISSUE_NUMBER,
        project_id=PROJECT_ID,
        claim_sha256="c" * 64,
    )

    assert result.status == recovered.status == "plan"
    assert recovered.markdown == PLAN_TEXT
    assert recovered.provenance["recovered"] is True
    assert len(codex.calls) == 1
    assert _status(root) == before


def test_real_process_runner_bounds_stream_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overflow = planner_module._run_codex(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * (2 * 1024 * 1024 + 1))",
        ],
        tmp_path,
        b"",
    )
    assert overflow.overflowed is True
    assert len(overflow.stdout) <= planner_module.PLANNER_STREAM_MAX_BYTES + 1

    monkeypatch.setattr(planner_module, "PLANNER_TIMEOUT_SECONDS", 0.1)
    timed_out = planner_module._run_codex(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        tmp_path,
        b"",
    )
    assert timed_out.timed_out is True


def _pid_is_alive(pid: int) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return False
    except (IndexError, OSError):
        fields = []
    if fields:
        return fields[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_owned_group(member_pid: int) -> None:
    try:
        process_group = os.getpgid(member_pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return


def _wait_for_file(path: Path, *, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_pid(path: Path, *, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    content = "<missing>"
    while time.monotonic() < deadline:
        try:
            content = path.read_text(encoding="ascii")
        except FileNotFoundError:
            pass
        else:
            # The newline is written last, so a partial write cannot be a PID.
            digits = content[:-1] if content.endswith("\n") else ""
            if digits.isascii() and digits.isdecimal() and int(digits) > 0:
                return int(digits)
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for positive PID in {path}: {content!r}")


def test_wait_for_pid_retries_empty_file_until_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "delayed.pid"
    pid_path.write_text("", encoding="ascii")
    original_read_text = Path.read_text
    reads = 0

    def complete_after_empty_read(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal reads
        content = original_read_text(path, *args, **kwargs)
        if path == pid_path and reads == 0:
            assert content == ""
            reads += 1
            pid_path.write_text("1234\n", encoding="ascii")
        return content

    monkeypatch.setattr(Path, "read_text", complete_after_empty_read)
    assert _wait_for_pid(pid_path) == 1234
    assert reads == 1


@pytest.mark.parametrize("content", ["", "1234", "0\n", "-1\n", "12x\n"])
def test_wait_for_pid_rejects_incomplete_or_invalid_content(
    tmp_path: Path, content: str
) -> None:
    pid_path = tmp_path / "invalid.pid"
    pid_path.write_text(content, encoding="ascii")
    with pytest.raises(AssertionError, match="timed out waiting for positive PID"):
        _wait_for_pid(pid_path, timeout=0.03)


def test_real_process_runner_deadline_applies_while_stdin_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "non-reader.pid"
    monkeypatch.setattr(planner_module, "PLANNER_TIMEOUT_SECONDS", 0.1)
    result = planner_module._run_codex(
        [
            sys.executable,
            "-c",
            (
                "import os, pathlib, sys, time; "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()) + '\\n'); "
                "time.sleep(10)"
            ),
        ],
        tmp_path,
        b"x" * 131072,
    )
    assert result.timed_out is True
    pid = _wait_for_pid(pid_path)
    assert not _pid_is_alive(pid)


def test_real_process_runner_overflow_applies_while_stdin_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "overflow-non-reader.pid"
    monkeypatch.setattr(planner_module, "PLANNER_TIMEOUT_SECONDS", 10.0)
    result = planner_module._run_codex(
        [
            sys.executable,
            "-c",
            (
                "import os, pathlib, sys, time; "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()) + '\\n'); "
                "sys.stdout.write('x' * (2 * 1024 * 1024 + 1)); "
                "sys.stdout.flush(); time.sleep(10)"
            ),
        ],
        tmp_path,
        b"x" * 131072,
    )
    assert result.overflowed is True
    pid = _wait_for_pid(pid_path)
    assert not _pid_is_alive(pid)


@pytest.mark.parametrize(
    ("termination", "planner_timeout"),
    [("timeout", 0.3), ("sigterm", 30.0)],
)
def test_owned_group_escalates_after_leader_exit(
    tmp_path: Path,
    termination: str,
    planner_timeout: float,
) -> None:
    leader_pid_path = tmp_path / "planner-leader.pid"
    descendant_pid_path = tmp_path / "planner-descendant.pid"
    result_path = tmp_path / "result.txt"
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    descendant_code = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(descendant_pid_path)!r}).write_text(str(os.getpid()) + '\\n'); "
        "time.sleep(60)"
    )
    leader_code = (
        "import os, pathlib, subprocess, sys, time; "
        f"pathlib.Path({str(leader_pid_path)!r}).write_text(str(os.getpid()) + '\\n'); "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}], stdin=None, stdout=None, stderr=None); "
        "time.sleep(60)"
    )
    supervisor_code = (
        "import pathlib, sys; "
        "import aflow.issue_intake_planner as planner; "
        f"planner.PLANNER_TIMEOUT_SECONDS = {planner_timeout!r}; "
        f"result = planner._run_codex([sys.executable, '-c', {leader_code!r}], pathlib.Path({str(tmp_path)!r}), b'x' * 131072); "
        f"pathlib.Path({str(result_path)!r}).write_text(str(result.interrupted) + ':' + str(result.timed_out) + ':' + str(result.returncode))"
    )
    supervisor = subprocess.Popen(
        [sys.executable, "-c", supervisor_code],
        cwd=Path(__file__).resolve().parents[1],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    leader_pid: int | None = None
    descendant_pid: int | None = None
    try:
        leader_pid = _wait_for_pid(leader_pid_path)
        descendant_pid = _wait_for_pid(descendant_pid_path)
        if termination == "sigterm":
            os.kill(supervisor.pid, signal.SIGTERM)
        started = time.monotonic()
        try:
            supervisor.communicate(timeout=8)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("planner supervisor exceeded outer timeout") from exc
        assert time.monotonic() - started < 8
        result = _wait_for_file(result_path).split(":", 2)
        assert result[0] == str(termination == "sigterm")
        assert result[1] == str(termination == "timeout")
        assert not _pid_is_alive(leader_pid)
        assert not _pid_is_alive(descendant_pid)
        assert sentinel.poll() is None
    finally:
        if descendant_pid is not None and _pid_is_alive(descendant_pid):
            _kill_owned_group(descendant_pid)
        if leader_pid is not None and _pid_is_alive(leader_pid):
            _kill_owned_group(leader_pid)
        if supervisor.poll() is None:
            _kill_owned_group(supervisor.pid)
            try:
                supervisor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)
        if sentinel.poll() is None:
            _kill_owned_group(sentinel.pid)
            sentinel.wait(timeout=5)


def _planner_response(markdown: str) -> bytes:
    return json.dumps({"status": "plan", "markdown": markdown}).encode("utf-8")


@pytest.mark.parametrize(
    "markdown",
    [
        (
            "# Plan\n\n### [x] Checkpoint 1: Complete\n- [x] implement\n\n"
            "### [ ] Checkpoint 2: Finish\n- [ ] finish\n"
        ),
        "# Plan\n\n### [ ] Checkpoint 1: Partial\n- [x] implement\n- [ ] finish\n",
        (
            "# Plan\n\n### [ ] Checkpoint 1: First\n- [ ] implement\n\n"
            "### [ ] Checkpoint 2: Partial\n- [x] later\n- [ ] finish\n"
        ),
    ],
)
def test_generated_plan_rejects_checked_progress(markdown: str) -> None:
    with pytest.raises(PlannerFailure, match="planner_output_(progress_not_pristine|invalid_markdown)"):
        planner_module._parse_response_bytes(_planner_response(markdown))


def test_recovered_generated_plan_rejects_checked_progress(tmp_path: Path) -> None:
    planner, _api, _codex, _root = _planner(tmp_path)
    workspace = planner.prepare(
        repository_id=REPOSITORY_ID,
        issue_number=ISSUE_NUMBER,
        claim_sha256="c" * 64,
        project_id=PROJECT_ID,
    )
    output = _planner_response(
        "# Plan\n\n### [ ] Checkpoint 1: First\n- [x] already done\n- [ ] finish\n"
    )
    planner_module._atomic_write(workspace.output_path, output, label="planner_output_artifact")
    manifest = {
        "schema_version": planner_module.PLANNER_SCHEMA_VERSION,
        "repository_id": REPOSITORY_ID,
        "issue_number": ISSUE_NUMBER,
        "project_id": PROJECT_ID,
        "claim_sha256": "c" * 64,
        "base_sha": workspace.base_sha,
        "project_root": str(workspace.project_root),
        "worktree": str(workspace.worktree),
        "output_path": str(workspace.output_path),
        "output_sha256": planner_module._sha256(output),
        "status": "plan",
        "returncode": 0,
    }
    planner_module._atomic_write(
        workspace.manifest_path,
        planner_module._json_bytes(manifest),
        label="planner_manifest",
    )
    try:
        with pytest.raises(PlannerFailure, match="planner_output_progress_not_pristine"):
            planner.recover(
                record=workspace.to_record(),
                repository_id=REPOSITORY_ID,
                issue_number=ISSUE_NUMBER,
                project_id=PROJECT_ID,
                claim_sha256="c" * 64,
            )
    finally:
        planner._cleanup_workspace(workspace)

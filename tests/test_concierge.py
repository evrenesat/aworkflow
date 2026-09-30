from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Mapping, Sequence
from urllib import parse as urllib_parse

import pytest

from aflow.concierge import (
    CONCIERGE_EFFORT,
    CONCIERGE_MCP_URL,
    CONCIERGE_MODEL,
    CONCIERGE_OWNER_ID,
    CONCIERGE_PAGE_LIMIT,
    CONCIERGE_PLANNER_EFFORT,
    CONCIERGE_PLANNER_MODEL,
    CONCIERGE_STREAM_MAX_BYTES,
    CONCIERGE_STATUS_TAIL_MAX_CHARS,
    CONCIERGE_TEAM,
    CONCIERGE_TOKEN_ENV,
    CONCIERGE_WORKFLOW_NAME,
    DEFAULT_WORK_DIR,
    ConciergeError,
    ConciergeTickExecutor,
    DefectEvidence,
    DeliveryEvidence,
    DeliveryStatus,
    GithubIssuePage,
    LocalDeliveryGate,
    McpRegistryClient,
    TickDeferral,
    TickObservation,
    TickOutcome,
    TickProcessResult,
    TriageDecision,
    _build_concierge_planner,
    _run_tick_process,
    build_codex_argv,
    build_tick_prompt,
    classify_defect,
    defect_evidence,
    defect_fingerprint,
    defect_fingerprint_marker,
    defect_issue_body,
    defect_issue_title,
    defect_signatures,
    dry_run,
    has_defect_fingerprint,
    load_tick_prompt,
    main,
    project_delivery,
    read_bearer_token,
    redact_text,
    repository_full_name,
    run_tick,
    tick_lock,
    triage_tick,
)
from aflow.issue_intake import source_hash
from aflow.issue_intake_planner import PlannerFailure

TOKEN = "test-concierge-token-ABC123"
ENVIRONMENT: Mapping[str, str] = {CONCIERGE_TOKEN_ENV: TOKEN}
DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy" / "concierge"


class FakeRunner:
    def __init__(self, result: TickProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[Sequence[str], Path, bytes, float]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        prompt: bytes,
        timeout_seconds: float,
    ) -> TickProcessResult:
        self.calls.append((tuple(argv), cwd, prompt, timeout_seconds))
        return self.result


class FakeExecutor:
    """Stand-in for the authoritative tick executor in run_tick tests."""

    def __init__(
        self,
        outcome: TickOutcome | None = None,
        error: ConciergeError | None = None,
    ) -> None:
        self.outcome = outcome or TickOutcome("idle", "no_eligible_work")
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def execute(
        self,
        *,
        state_dir: Path,
        project_root: Path,
        environment: Mapping[str, str],
    ) -> TickOutcome:
        self.calls.append(
            {
                "state_dir": state_dir,
                "project_root": project_root,
                "environment": environment,
            }
        )
        if self.error is not None:
            raise self.error
        return self.outcome

    def close(self) -> None:
        self.closed = True


_GLOBAL_WORKFLOWS_TOML = """
[workflow]
setup = ["worktree", "branch"]
teardown = ["merge", "rm_worktree"]
main_branch = "main"
manager_enabled = false

[workflow.checkpoint_review_only]
team = "sol-6-high"

[workflow.checkpoint_delivery]
extends = "checkpoint_review_only"
upgrade_after_repairs = 0
"""


def _global_config(
    *,
    workflows_toml: str | None = None,
    state: str = "ready",
) -> dict[str, object]:
    config: dict[str, object] = {
        "project_id": "p1",
        "revision": "rev-1",
        "documents": {},
        "aflow_toml": '[project]\nname = "agent-flow"\n',
        "workflows_toml": (
            _GLOBAL_WORKFLOWS_TOML if workflows_toml is None else workflows_toml
        ),
        "validation": {"state": state, "issues": []},
    }
    return config


def _publication_projection(
    *,
    available: bool = False,
    remote: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    return {
        "available": available,
        "publish_remote": remote,
        "publish_branch": branch,
    }


class FakeMcp:
    """In-memory MCP endpoint with bounded pagination and call recording."""

    def __init__(
        self,
        *,
        project_root: Path,
        project_id: str = "p1",
        registered: bool = True,
        runs: Sequence[Mapping[str, object]] = (),
        plans: Sequence[Mapping[str, object]] = (),
        documents: Mapping[str, str] | None = None,
        auto_consume_plans: bool = False,
        max_concurrent_implementations: int = 1,
        preflight: Mapping[str, object] | None = None,
        start_payload: Mapping[str, object] | None = None,
        publication: Mapping[str, object] | None = None,
        global_config: Mapping[str, object] | None = None,
        get_run_payloads: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.project_root = project_root
        self.project_id = project_id
        self.registered = registered
        self.runs: list[dict[str, object]] = [dict(run) for run in runs]
        self.plans: list[dict[str, object]] = [dict(plan) for plan in plans]
        self.documents: dict[str, str] = dict(documents or {})
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: dict[str, Exception] = {}
        self.pre_failures: dict[str, object] = {}
        self.created_plans: list[dict[str, object]] = []
        self.auto_consume_plans = auto_consume_plans
        self.max_concurrent_implementations = max_concurrent_implementations
        self.preflight = dict(preflight) if preflight is not None else None
        self.start_payload = (
            dict(start_payload) if start_payload is not None else None
        )
        self.publication = (
            dict(publication) if publication is not None else _publication_projection()
        )
        self.global_config = (
            dict(global_config) if global_config is not None else _global_config()
        )
        self.get_run_payloads = (
            {key: dict(value) for key, value in get_run_payloads.items()}
            if get_run_payloads is not None
            else None
        )

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((name, dict(arguments)))
        if name in self.failures:
            side_effect = self.pre_failures.get(name)
            if side_effect is not None:
                side_effect()
            raise self.failures[name]
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ConciergeError("mcp_tool_unknown")
        return handler(dict(arguments))

    def close(self) -> None:
        pass

    def _tool_list_projects(self, args: dict[str, object]) -> dict[str, object]:
        if not self.registered:
            return {"projects": []}
        return {
            "projects": [
                {
                    "project_id": self.project_id,
                    "root": str(self.project_root),
                }
            ]
        }

    def _tool_list_runs(self, args: dict[str, object]) -> dict[str, object]:
        limit = int(args.get("limit", CONCIERGE_PAGE_LIMIT))
        cursor = args.get("cursor")
        start = int(cursor) if isinstance(cursor, str) and cursor else 0
        page = self.runs[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(self.runs) else None
        return {"runs": page, "next_cursor": next_cursor, "schema_version": 1}

    def _tool_list_plans(self, args: dict[str, object]) -> dict[str, object]:
        limit = int(args.get("limit", CONCIERGE_PAGE_LIMIT))
        cursor = args.get("cursor")
        start = 0
        if isinstance(cursor, str) and cursor:
            for index, plan in enumerate(self.plans):
                if plan.get("path") == cursor:
                    start = index + 1
                    break
        page = self.plans[start : start + limit]
        return {"plans": page}

    def _tool_read_plan(self, args: dict[str, object]) -> dict[str, object]:
        directory = {
            "todo": "todo",
            "in_progress": "in-progress",
            "failed": "failed",
            "done": "done",
            "needs_plan_change": "needs-plan-change",
        }.get(str(args.get("plan_status")))
        if directory is None:
            raise ConciergeError("mcp_tool_rejected")
        path = f"plans/{directory}/{args.get('name')}"
        if path not in self.documents:
            raise ConciergeError("mcp_tool_rejected")
        return {"path": path, "content": self.documents[path]}

    def _tool_get_run(self, args: dict[str, object]) -> dict[str, object]:
        if self.get_run_payloads is not None:
            payload = self.get_run_payloads.get(str(args.get("run_id")))
            if payload is None:
                raise ConciergeError("mcp_tool_rejected")
            return dict(payload)
        for run in self.runs:
            if run.get("run_id") == args.get("run_id"):
                return dict(run)
        raise ConciergeError("mcp_tool_rejected")

    def _tool_get_global_config(
        self, args: dict[str, object]
    ) -> dict[str, object]:
        return dict(self.global_config)

    def _tool_resume_run(self, args: dict[str, object]) -> dict[str, object]:
        for run in self.runs:
            if run.get("run_id") == args.get("run_id"):
                run["activity"] = "active"
                run["status"] = "running"
        return {"run_id": args.get("run_id"), "state": "resumed"}

    def _tool_preflight_run(self, args: dict[str, object]) -> dict[str, object]:
        if self.preflight is not None:
            return dict(self.preflight)
        return {
            "checkout_path": str(self.project_root),
            "execution_mode": "new_worktree",
            "dirty": False,
            "requires_confirmation": False,
            "blockers": [],
            "total_items": 0,
            "offset": 0,
            "limit": 200,
            "next_offset": None,
            "items": [],
        }

    def _tool_start_run(self, args: dict[str, object]) -> dict[str, object]:
        if self.start_payload is not None:
            return dict(self.start_payload)
        run_id = f"run-started-{len(self.runs)}"
        self.runs.append(
            {
                "run_id": run_id,
                "activity": "active",
                "status": "running",
                "plan_path": args.get("plan_path"),
            }
        )
        return {
            "result": {
                "run_id": run_id,
                "created": True,
                "status": "launch_requested",
                "schema_version": 1,
                "manifest_path": None,
                "reason": None,
                "restarted_from_run_id": None,
            }
        }

    def _tool_get_project_capabilities(
        self, args: dict[str, object]
    ) -> dict[str, object]:
        return {
            "workflows": [CONCIERGE_WORKFLOW_NAME],
            "teams": [CONCIERGE_TEAM],
            "workflow_details": {
                CONCIERGE_WORKFLOW_NAME: {
                    "declared_steps": ["setup", "teardown"],
                    "executable_steps": ["setup", "teardown"],
                    "excluded_steps": [],
                    "first_step": "setup",
                    "default_team": CONCIERGE_TEAM,
                }
            },
            "publication": dict(self.publication),
        }

    def _tool_get_project_scheduling(
        self, args: dict[str, object]
    ) -> dict[str, object]:
        return {
            "auto_consume_plans": self.auto_consume_plans,
            "max_concurrent_implementations": self.max_concurrent_implementations,
            "revision": "rev-1",
            "persisted": False,
            "source": "defaults",
        }

    def _tool_get_project_queue(self, args: dict[str, object]) -> dict[str, object]:
        active = [run for run in self.runs if run.get("activity") == "active"]
        plans: list[dict[str, object]] = []
        for plan in self.plans:
            run_id = next(
                (
                    run.get("run_id")
                    for run in self.runs
                    if run.get("plan_path") == plan.get("path")
                ),
                None,
            )
            plans.append(
                {
                    "path": plan.get("path"),
                    "status": plan.get("status"),
                    "run_id": run_id,
                    "outcome": "claimed" if run_id else "ready",
                }
            )
        return {
            "project_id": self.project_id,
            "settings": {
                "auto_consume_plans": self.auto_consume_plans,
                "max_concurrent_implementations": self.max_concurrent_implementations,
            },
            "capacity": {
                "limit": self.max_concurrent_implementations,
                "active_count": len(active),
                "available_slots": (
                    0 if active else self.max_concurrent_implementations
                ),
            },
            "plans": plans,
        }

    def _tool_create_plan(self, args: dict[str, object]) -> dict[str, object]:
        name = str(args.get("name"))
        path = f"plans/todo/{name}"
        self.plans.append({"path": path, "status": "todo"})
        self.documents[path] = str(args.get("content") or "")
        self.created_plans.append(dict(args))
        return {"path": path, "status": "todo"}

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeGithub:
    def __init__(
        self,
        page: GithubIssuePage | None = None,
        error: ConciergeError | None = None,
        search_results: Sequence[Mapping[str, object]] = (),
        workflow_runs: Sequence[tuple[int, int, str, str]] = (),
        create_error: ConciergeError | None = None,
    ) -> None:
        self.page = (
            page
            if page is not None
            else GithubIssuePage(repository_id=0, issues=())
        )
        self.error = error
        self.search_results = tuple(search_results)
        self._workflow_run_data = tuple(workflow_runs)
        self.create_error = create_error
        self.calls: list[str] = []
        self.search_queries: list[tuple[str, str]] = []
        self.workflow_sha_queries: list[tuple[str, str]] = []
        self.created_issues: list[tuple[str, str, str]] = []

    def open_issues(self, full_name: str) -> GithubIssuePage:
        self.calls.append(full_name)
        if self.error is not None:
            raise self.error
        return self.page

    def workflow_runs(
        self, full_name: str, sha: str
    ) -> tuple[tuple[int, int, str, str], ...]:
        self.workflow_sha_queries.append((full_name, sha))
        if self.error is not None:
            raise self.error
        return self._workflow_run_data

    def search_issues(
        self, full_name: str, query: str
    ) -> tuple[Mapping[str, object], ...]:
        self.search_queries.append((full_name, query))
        if self.error is not None:
            raise self.error
        return self.search_results

    def create_issue(
        self, full_name: str, title: str, body: str
    ) -> Mapping[str, object]:
        self.created_issues.append((full_name, title, body))
        if self.create_error is not None:
            raise self.create_error
        if self.error is not None:
            raise self.error
        number = 100 + len(self.created_issues)
        return {
            "number": number,
            "html_url": f"https://github.com/{full_name}/issues/{number}",
        }


class FakePlanner:
    def __init__(self, result: object | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.plan_calls: list[dict[str, object]] = []
        self.recover_calls: list[dict[str, object]] = []
        self.side_effect = None

    def plan(self, **kwargs: object) -> object:
        self.plan_calls.append(kwargs)
        if self.side_effect is not None:
            self.side_effect()
        if self.error is not None:
            raise self.error
        assert self.result is not None
        persist_workspace = kwargs.get("persist_workspace")
        if persist_workspace is not None and getattr(self.result, "status", None) == "plan":
            persist_workspace(
                {
                    "claim_sha256": kwargs.get("claim_sha256"),
                    "issue_number": kwargs.get("issue_number"),
                    "status": "plan",
                }
            )
        return self.result

    def recover(self, **kwargs: object) -> object:
        self.recover_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _read_status(state_dir: Path) -> dict[str, object]:
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_codex_argv_pins_model_effort_and_local_mcp_without_secret(
    tmp_path: Path,
) -> None:
    argv = build_codex_argv(work_dir=tmp_path)
    joined = " ".join(argv)
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--model" in argv
    assert CONCIERGE_MODEL in argv
    assert f"model_reasoning_effort='{CONCIERGE_EFFORT}'" in argv
    assert f"mcp_servers.aflow.url='{CONCIERGE_MCP_URL}'" in argv
    assert f"mcp_servers.aflow.bearer_token_env_var='{CONCIERGE_TOKEN_ENV}'" in argv
    assert "--sandbox" in argv
    assert "read-only" in argv
    assert "--ephemeral" in argv
    assert "--skip-git-repo-check" in argv
    assert "-C" in argv
    assert str(tmp_path) in argv
    assert argv[-1] == "-"
    assert "dangerously-bypass" not in joined
    assert TOKEN not in joined


def test_default_work_dir_is_the_documented_parent_directory() -> None:
    assert DEFAULT_WORK_DIR == Path("/root/code")
    argv = build_codex_argv(work_dir=DEFAULT_WORK_DIR)
    assert "--skip-git-repo-check" in argv
    assert "-C" in argv
    assert str(DEFAULT_WORK_DIR) in argv


def test_service_unit_pins_private_codex_home_and_mcp_token_source() -> None:
    unit = (DEPLOY_DIR / "aflow-concierge.service").read_text(encoding="utf-8")
    assert "Environment=CODEX_HOME=/var/lib/aflowd/concierge/codex-home" in unit
    assert "ReadWritePaths=/var/lib/aflowd" in unit
    assert "EnvironmentFile=/etc/aflowd/aflowd.env" in unit
    assert "WorkingDirectory=/root/code" in unit
    assert "AFLOW_APP_TOKEN" not in unit


def test_runbook_provisions_and_gates_codex_home_before_enablement() -> None:
    runbook = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    provision = runbook.index(
        "install -d -m 0700 /var/lib/aflowd/concierge/codex-home"
    )
    login_gate = runbook.index("codex login status")
    enable = runbook.index("systemctl enable --now aflow-concierge.timer")
    assert 0 <= provision < login_gate < enable
    assert "--skip-git-repo-check" in runbook


def test_tick_prompt_is_bounded_and_read_only() -> None:
    prompt = build_tick_prompt()
    assert "read-only" in prompt
    assert "gpt-6.1-sol" not in prompt
    assert len(prompt.encode("utf-8")) < 4096
    assert TOKEN not in prompt


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "has space",
        "has\ntab",
        "bad!charset",
        "x" * 1025,
    ],
)
def test_read_bearer_token_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ConciergeError):
        read_bearer_token({CONCIERGE_TOKEN_ENV: value})


def test_read_bearer_token_rejects_missing() -> None:
    with pytest.raises(ConciergeError):
        read_bearer_token({})


def test_read_bearer_token_accepts_valid_value() -> None:
    assert read_bearer_token(ENVIRONMENT) == TOKEN


def test_redact_text_replaces_every_secret() -> None:
    assert (
        redact_text(f"a {TOKEN} b {TOKEN} c", [TOKEN]) == "a [REDACTED] b [REDACTED] c"
    )
    assert redact_text("no secrets", []) == "no secrets"


def test_tick_lock_raises_while_held(tmp_path: Path) -> None:
    with tick_lock(tmp_path):
        with pytest.raises(TickDeferral):
            with tick_lock(tmp_path):
                pass


def test_run_tick_defers_overlapping_tick(tmp_path: Path) -> None:
    runner = FakeRunner(TickProcessResult(returncode=0, stdout=b"never ran"))
    executor = FakeExecutor()
    with tick_lock(tmp_path):
        exit_code = run_tick(
            state_dir=tmp_path,
            work_dir=tmp_path,
            environment=ENVIRONMENT,
            runner=runner,
            tick_executor=executor,
        )
    assert exit_code == 0
    assert runner.calls == []
    assert executor.calls == []
    # Injected executors are caller-owned; run_tick only closes its own.
    assert executor.closed is False
    status = _read_status(tmp_path)
    assert status["phase"] == "deferred"
    assert status["reason"] == "tick_in_progress"
    assert status["returncode"] is None
    assert status["action"] == "deferred"
    assert status["mutating"] is False


def test_run_tick_persists_bounded_redacted_status(tmp_path: Path) -> None:
    stdout = (b"filler " * 2000) + f"token={TOKEN} secret".encode()
    runner = FakeRunner(TickProcessResult(returncode=0, stdout=stdout))
    executor = FakeExecutor(
        TickOutcome(
            "resume",
            "resumed",
            mutating=True,
            details={"run_id": "run-1"},
        )
    )
    exit_code = run_tick(
        state_dir=tmp_path,
        work_dir=tmp_path,
        environment=ENVIRONMENT,
        runner=runner,
        tick_executor=executor,
    )
    assert exit_code == 0
    assert executor.closed is False
    status = _read_status(tmp_path)
    assert status["schema_version"] == 2
    assert status["phase"] == "completed"
    assert status["reason"] == "concierge_tick_completed"
    assert status["model"] == CONCIERGE_MODEL
    assert status["effort"] == CONCIERGE_EFFORT
    assert status["mcp_url"] == CONCIERGE_MCP_URL
    assert status["returncode"] == 0
    assert status["duration_seconds"] >= 0.0
    assert status["action"] == "resume"
    assert status["outcome"] == "resumed"
    assert status["mutating"] is True
    assert status["planner_model"] == CONCIERGE_PLANNER_MODEL
    assert status["planner_effort"] == CONCIERGE_PLANNER_EFFORT
    assert status["decision_run_id"] == "run-1"
    assert TOKEN not in json.dumps(status)
    assert "[REDACTED]" in status["stdout_tail"]
    assert len(status["stdout_tail"]) <= CONCIERGE_STATUS_TAIL_MAX_CHARS
    assert not (tmp_path / "transcript.json").exists()
    assert not list(tmp_path.glob("*.log"))


def test_run_tick_end_to_end_blocks_start_without_publication_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    planner = FakePlanner(result=_plan_result())
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    runner = FakeRunner(TickProcessResult(returncode=0, stdout=b"ok"))
    exit_code = run_tick(
        state_dir=tmp_path,
        work_dir=tmp_path,
        project_root=tmp_path,
        environment=ENVIRONMENT,
        runner=runner,
        tick_executor=executor,
    )
    assert exit_code == 0
    status = _read_status(tmp_path)
    assert status["phase"] == "completed"
    assert status["action"] == "report"
    assert status["outcome"] == "launch_evidence_unavailable"
    assert status["mutating"] is False
    assert status["planner_model"] == CONCIERGE_PLANNER_MODEL
    assert status["planner_effort"] == CONCIERGE_PLANNER_EFFORT
    assert len(mcp.created_plans) == 1
    assert "start_run" not in mcp.call_names()


def test_run_tick_end_to_end_starts_eligible_plan_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        publication=_publication_projection(
            available=True, remote="origin", branch="main"
        ),
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    runner = FakeRunner(TickProcessResult(returncode=0, stdout=b"ok"))
    exit_code = run_tick(
        state_dir=tmp_path,
        work_dir=tmp_path,
        project_root=tmp_path,
        environment=ENVIRONMENT,
        runner=runner,
        tick_executor=executor,
    )
    assert exit_code == 0
    status = _read_status(tmp_path)
    assert status["phase"] == "completed"
    assert status["action"] == "start"
    assert status["outcome"] == "started"
    assert status["mutating"] is True
    assert mcp.call_names().count("start_run") == 1
    assert len(mcp.runs) == 1


def test_run_tick_end_to_end_resumes_exact_predecessor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            {
                "run_id": "run-failed",
                "activity": "inactive",
                "status": "failed",
                "plan_path": "plans/in-progress/alpha.md",
                "evidence": {"can_resume": True},
            }
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    runner = FakeRunner(TickProcessResult(returncode=0, stdout=b"ok"))
    exit_code = run_tick(
        state_dir=tmp_path,
        work_dir=tmp_path,
        project_root=tmp_path,
        environment=ENVIRONMENT,
        runner=runner,
        tick_executor=executor,
    )
    assert exit_code == 0
    status = _read_status(tmp_path)
    assert status["phase"] == "completed"
    assert status["action"] == "resume"
    assert status["outcome"] == "resumed"
    assert status["mutating"] is True
    assert mcp.call_names().count("resume_run") == 1
    assert mcp.runs[0]["activity"] == "active"


def test_run_tick_executor_failure_is_reported_without_process_blame(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(TickProcessResult(returncode=0, stdout=b"ok"))
    executor = FakeExecutor(error=ConciergeError("mcp_unavailable"))
    exit_code = run_tick(
        state_dir=tmp_path,
        work_dir=tmp_path,
        environment=ENVIRONMENT,
        runner=runner,
        tick_executor=executor,
    )
    assert exit_code == 0
    status = _read_status(tmp_path)
    assert status["action"] == "report"
    assert status["outcome"] == "mcp_unavailable"
    assert status["mutating"] is False
    assert status["phase"] == "completed"


@pytest.mark.parametrize(
    ("result", "phase", "reason", "exit_code"),
    [
        (
            TickProcessResult(returncode=0, timed_out=True),
            "timed_out",
            "concierge_timed_out",
            1,
        ),
        (
            TickProcessResult(returncode=0, overflowed=True),
            "failed",
            "concierge_stream_overflow",
            1,
        ),
        (
            TickProcessResult(returncode=0, interrupted=True),
            "failed",
            "concierge_interrupted",
            1,
        ),
        (
            TickProcessResult(returncode=3),
            "failed",
            "concierge_process_failed",
            1,
        ),
    ],
)
def test_run_tick_failure_phases(
    tmp_path: Path,
    result: TickProcessResult,
    phase: str,
    reason: str,
    exit_code: int,
) -> None:
    runner = FakeRunner(result)
    assert (
        run_tick(
            state_dir=tmp_path,
            work_dir=tmp_path,
            environment=ENVIRONMENT,
            runner=runner,
            tick_executor=FakeExecutor(),
        )
        is exit_code
    )
    status = _read_status(tmp_path)
    assert status["phase"] == phase
    assert status["reason"] == reason
    assert status["returncode"] == result.returncode
    assert status["action"] == "idle"
    assert status["mutating"] is False


def test_run_tick_rejects_relative_state_dir(tmp_path: Path) -> None:
    with pytest.raises(ConciergeError):
        run_tick(
            state_dir=Path("relative-state"),
            work_dir=tmp_path,
            environment=ENVIRONMENT,
            runner=FakeRunner(TickProcessResult(returncode=0)),
            tick_executor=FakeExecutor(),
        )


def test_run_tick_rejects_state_dir_collision(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.write_text("not a directory\n")
    with pytest.raises(ConciergeError):
        run_tick(
            state_dir=state_dir,
            work_dir=tmp_path,
            environment=ENVIRONMENT,
            runner=FakeRunner(TickProcessResult(returncode=0)),
            tick_executor=FakeExecutor(),
        )
    assert not state_dir.is_dir()


def test_run_tick_rejects_missing_token_without_side_effects(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with pytest.raises(ConciergeError):
        run_tick(
            state_dir=state_dir,
            work_dir=tmp_path,
            environment={},
            runner=FakeRunner(TickProcessResult(returncode=0)),
            tick_executor=FakeExecutor(),
        )
    assert not state_dir.exists()


def test_dry_run_is_read_only_and_secret_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    exit_code = dry_run(
        state_dir=state_dir,
        work_dir=tmp_path,
        environment=ENVIRONMENT,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert not state_dir.exists()
    assert CONCIERGE_MODEL in captured.out
    assert CONCIERGE_EFFORT in captured.out
    assert CONCIERGE_MCP_URL in captured.out
    assert CONCIERGE_TOKEN_ENV in captured.out
    assert "codex" in captured.out
    assert TOKEN not in captured.out


def test_dry_run_raises_on_missing_config(tmp_path: Path) -> None:
    with pytest.raises(ConciergeError):
        dry_run(state_dir=tmp_path, work_dir=tmp_path, environment={})


def test_main_dry_run_has_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(CONCIERGE_TOKEN_ENV, TOKEN)
    exit_code = main(
        [
            "--dry-run",
            "--state-dir",
            str(tmp_path / "state"),
            "--work-dir",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert not (tmp_path / "state").exists()
    assert TOKEN not in captured.out + captured.err


def test_main_reports_unsafe_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(CONCIERGE_TOKEN_ENV, raising=False)
    exit_code = main(
        [
            "--dry-run",
            "--state-dir",
            str(tmp_path / "state"),
            "--work-dir",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "bearer_token_missing" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_main_rejects_timeout_at_or_above_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(CONCIERGE_TOKEN_ENV, TOKEN)
    exit_code = main(
        [
            "--dry-run",
            "--timeout-seconds",
            "1200",
            "--state-dir",
            str(tmp_path / "state"),
            "--work-dir",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert not (tmp_path / "state").exists()
    assert "20-minute" in captured.err


def test_main_runs_bounded_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(CONCIERGE_TOKEN_ENV, TOKEN)
    runner = FakeRunner(TickProcessResult(returncode=0, stdout=b"tick ok"))
    monkeypatch.setattr("aflow.concierge._run_tick_process", runner)
    executor = FakeExecutor(TickOutcome("idle", "no_eligible_work"))
    monkeypatch.setattr(
        "aflow.concierge._build_production_executor",
        lambda token, environment: executor,
    )
    project_root = tmp_path / "repo"
    project_root.mkdir()
    state_dir = tmp_path / "state"
    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--work-dir",
            str(tmp_path),
            "--project-root",
            str(project_root),
            "--timeout-seconds",
            "900",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert len(runner.calls) == 1
    argv, cwd, prompt, timeout_seconds = runner.calls[0]
    assert argv[:2] == ("codex", "exec")
    assert cwd == tmp_path
    assert timeout_seconds == 900.0
    assert build_tick_prompt().encode("utf-8") == prompt
    assert len(executor.calls) == 1
    assert executor.calls[0]["project_root"] == project_root
    # run_tick owns executors built through the production factory.
    assert executor.closed is True
    status = _read_status(state_dir)
    assert status["phase"] == "completed"
    assert status["action"] == "idle"
    assert TOKEN not in captured.out + captured.err


def test_process_runner_bounds_stream_overflow(tmp_path: Path) -> None:
    argv = (
        "python3",
        "-c",
        "import sys; sys.stdout.write("
        f"'x' * ({CONCIERGE_STREAM_MAX_BYTES} + 1)); sys.stdout.flush()",
    )
    result = _run_tick_process(argv, tmp_path, b"", 30.0)
    assert result.overflowed is True
    assert result.timed_out is False


def test_process_runner_times_out_and_kills_process_group(tmp_path: Path) -> None:
    argv = (
        "python3",
        "-c",
        "import os, time; print(os.getpid(), flush=True); time.sleep(30)",
    )
    result = _run_tick_process(argv, tmp_path, b"", 1.0)
    assert result.timed_out is True
    assert result.overflowed is False
    pid_line = result.stdout.decode("utf-8").strip().splitlines()
    assert pid_line, "expected the child pid on stdout"
    assert not _pid_is_alive(int(pid_line[0]))


def test_process_runner_returns_bounded_success(tmp_path: Path) -> None:
    argv = ("python3", "-c", "import sys; sys.stdout.write('done')")
    result = _run_tick_process(argv, tmp_path, b"ignored", 30.0)
    assert result.returncode == 0
    assert result.stdout == b"done"
    assert result.timed_out is False
    assert result.overflowed is False
    assert result.interrupted is False


def test_tick_prompt_loads_packaged_resource_with_triage_rules() -> None:
    prompt = build_tick_prompt()
    resource = (
        Path(__file__).resolve().parent.parent
        / "aflow"
        / "concierge_prompt.md"
    ).read_text(encoding="utf-8")
    assert prompt == resource
    assert load_tick_prompt() == resource
    assert "read-only" in prompt
    assert "gpt-6.1-sol" not in prompt
    assert len(prompt.encode("utf-8")) < 4096
    assert TOKEN not in prompt
    for rule in (
        "MCP",
        "REST",
        "CLI",
        "591691",
        "checkpoint_delivery",
        "xtx-mtp",
        "gpt-6-astra",
        "idempotency",
        "blind recovery",
    ):
        assert rule in prompt


def _observation(
    *,
    runs: Sequence[Mapping[str, object]] = (),
    plans: Sequence[Mapping[str, object]] = (),
    issues: Sequence[Mapping[str, object]] = (),
    plan_documents: Mapping[str, str] | None = None,
) -> TickObservation:
    return TickObservation(
        project={"project_id": "agent-flow-test", "root": "/root/code/agent-flow"},
        runs=tuple(runs),
        plans=tuple(plans),
        issues=tuple(issues),
        plan_documents=dict(plan_documents or {}),
    )


def _run(
    *,
    run_id: str = "run-1",
    activity: str = "inactive",
    status: str = "succeeded",
    plan_path: str | None = None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "activity": activity,
        "status": status,
        "plan_path": plan_path,
    }


def _plan(
    *,
    path: str = "plans/todo/alpha.md",
    status: str = "todo",
) -> dict[str, object]:
    return {"path": path, "status": status}


def _issue(
    *,
    number: int = 7,
    author_id: int = CONCIERGE_OWNER_ID,
    full_name: str = "owner/repo",
    created_at: str = "2026-09-01T00:00:00Z",
    state: str = "open",
) -> dict[str, object]:
    return {
        "number": number,
        "author_id": author_id,
        "full_name": full_name,
        "created_at": created_at,
        "state": state,
    }


def test_triage_defers_on_active_run_even_with_eligible_issue() -> None:
    decision = triage_tick(
        _observation(runs=[_run(activity="active")], issues=[_issue()])
    )
    assert decision.action == "defer"
    assert decision.reason == "active_run_present"
    assert decision.run_id == "run-1"
    assert decision.idempotency_key is None
    assert decision.mutating is False


def test_triage_defers_on_uncertain_run() -> None:
    decision = triage_tick(_observation(runs=[_run(activity="unknown")]))
    assert decision.action == "defer"
    assert decision.reason == "uncertain_run_present"
    assert decision.mutating is False


def test_triage_defers_when_run_activity_is_missing() -> None:
    run = _run()
    del run["activity"]
    decision = triage_tick(_observation(runs=[run]))
    assert decision.action == "defer"
    assert decision.reason == "uncertain_run_present"


def test_triage_resumes_inactive_failed_lineage_with_stable_key() -> None:
    observation = _observation(
        runs=[
            _run(
                run_id="run-failed",
                status="failed",
                plan_path="plans/in-progress/alpha.md",
            )
        ],
        plans=[_plan(path="plans/in-progress/alpha.md", status="in_progress")],
        issues=[_issue()],
    )
    first = triage_tick(observation)
    second = triage_tick(observation)
    assert first.action == "resume"
    assert first.reason == "failed_lineage_resumable"
    assert first.run_id == "run-failed"
    assert first.plan_path == "plans/in-progress/alpha.md"
    assert first.idempotency_key == "concierge-resume-run-failed"
    assert first.idempotency_key == second.idempotency_key
    assert first.mutating is True


def test_triage_reports_ambiguous_predecessor_ownership() -> None:
    decision = triage_tick(
        _observation(
            runs=[
                _run(
                    run_id="run-a",
                    status="failed",
                    plan_path="plans/in-progress/alpha.md",
                ),
                _run(
                    run_id="run-b",
                    status="failed",
                    plan_path="plans/in-progress/alpha.md",
                ),
            ],
            plans=[_plan(path="plans/in-progress/alpha.md", status="in_progress")],
        )
    )
    assert decision.action == "report"
    assert decision.reason == "ambiguous_predecessor_ownership"
    assert decision.plan_path == "plans/in-progress/alpha.md"
    assert decision.idempotency_key is None
    assert decision.mutating is False


def test_triage_reports_failed_run_without_plan_lineage() -> None:
    decision = triage_tick(
        _observation(runs=[_run(run_id="run-failed", status="failed")])
    )
    assert decision.action == "report"
    assert decision.reason == "failed_lineage_missing_plan"
    assert decision.run_id == "run-failed"
    assert decision.mutating is False


def test_triage_reports_unregistered_failed_plan_lineage() -> None:
    decision = triage_tick(
        _observation(
            runs=[
                _run(
                    run_id="run-failed",
                    status="failed",
                    plan_path="plans/in-progress/ghost.md",
                )
            ]
        )
    )
    assert decision.action == "report"
    assert decision.reason == "failed_lineage_plan_unregistered"
    assert decision.plan_path == "plans/in-progress/ghost.md"


def test_triage_reports_competing_run_ownership() -> None:
    decision = triage_tick(
        _observation(
            runs=[
                _run(
                    run_id="run-failed",
                    status="failed",
                    plan_path="plans/in-progress/alpha.md",
                ),
                _run(
                    run_id="run-other",
                    status="succeeded",
                    plan_path="plans/in-progress/alpha.md",
                ),
            ],
            plans=[_plan(path="plans/in-progress/alpha.md", status="in_progress")],
        )
    )
    assert decision.action == "report"
    assert decision.reason == "competing_run_ownership"
    assert decision.run_id == "run-failed"
    assert decision.mutating is False


def test_triage_starts_single_launchable_plan_with_concierge_defaults() -> None:
    observation = _observation(plans=[_plan()], issues=[_issue()])
    decision = triage_tick(observation)
    assert decision.action == "start"
    assert decision.reason == "single_launchable_plan"
    assert decision.plan_path == "plans/todo/alpha.md"
    assert decision.workflow_name == CONCIERGE_WORKFLOW_NAME
    assert decision.team == CONCIERGE_TEAM
    assert decision.idempotency_key is not None
    assert decision.idempotency_key.startswith("concierge-start-")
    assert decision.idempotency_key == triage_tick(observation).idempotency_key
    assert decision.mutating is True


def test_triage_reports_multiple_launchable_plans() -> None:
    observation = _observation(
        plans=[
            _plan(path="plans/todo/alpha.md"),
            _plan(path="plans/todo/beta.md"),
        ]
    )
    decision = triage_tick(observation)
    assert decision.action == "report"
    assert decision.reason == "multiple_launchable_plans"
    assert decision.plan_path == "plans/todo/alpha.md"
    assert decision.mutating is False


def test_triage_does_not_start_a_plan_already_referenced_by_a_run() -> None:
    decision = triage_tick(
        _observation(
            runs=[_run(status="succeeded", plan_path="plans/todo/alpha.md")],
            plans=[_plan()],
        )
    )
    assert decision.action == "idle"
    assert decision.reason == "no_eligible_work"


def test_triage_treats_in_progress_plan_as_not_launchable() -> None:
    decision = triage_tick(
        _observation(
            plans=[_plan(path="plans/in-progress/alpha.md", status="in_progress")]
        )
    )
    assert decision.action == "idle"
    assert decision.reason == "no_eligible_work"


def test_triage_ignores_non_owner_issue() -> None:
    decision = triage_tick(
        _observation(issues=[_issue(number=50, author_id=12345)])
    )
    assert decision.action == "idle"
    assert decision.reason == "no_eligible_work"
    assert decision.issue_number is None
    assert decision.idempotency_key is None


def test_triage_ignores_closed_owner_issue() -> None:
    decision = triage_tick(_observation(issues=[_issue(state="closed")]))
    assert decision.action == "idle"
    assert decision.reason == "no_eligible_work"


def test_triage_avoids_duplicate_plan_for_owner_issue() -> None:
    # A launchable plan already mapped to the issue is started, not re-planned.
    decision = triage_tick(
        _observation(
            plans=[_plan(path="plans/todo/alpha.md")],
            issues=[_issue(number=7)],
        )
    )
    assert decision.action == "start"
    assert decision.plan_path == "plans/todo/alpha.md"
    assert decision.issue_number is None

    # A non-launchable plan whose document references the issue blocks a
    # duplicate plan.
    decision = triage_tick(
        _observation(
            plans=[
                _plan(path="plans/in-progress/alpha.md", status="in_progress"),
            ],
            issues=[_issue(number=7)],
            plan_documents={
                "plans/in-progress/alpha.md": (
                    "# Plan\n\nIssue: https://github.com/owner/repo/issues/7\n"
                )
            },
        )
    )
    assert decision.action == "idle"
    assert decision.reason == "no_eligible_work"
    assert decision.issue_number is None


def test_triage_duplicate_detection_covers_all_evidence_statuses() -> None:
    # A launchable todo plan mapped to the issue is started, never re-planned.
    decision = triage_tick(
        _observation(
            plans=[_plan(path="plans/todo/alpha.md", status="todo")],
            issues=[_issue(number=7)],
            plan_documents={
                "plans/todo/alpha.md": (
                    "Source: https://github.com/owner/repo/issues/7"
                )
            },
        )
    )
    assert decision.action == "start"
    assert decision.plan_path == "plans/todo/alpha.md"

    for directory, status in (
        ("in-progress", "in_progress"),
        ("failed", "failed"),
        ("done", "done"),
    ):
        decision = triage_tick(
            _observation(
                plans=[_plan(path=f"plans/{directory}/alpha.md", status=status)],
                issues=[_issue(number=7)],
                plan_documents={
                    f"plans/{directory}/alpha.md": (
                        f"Source: https://github.com/owner/repo/issues/7"
                    )
                },
            )
        )
        assert decision.action == "idle"
        assert decision.reason == "no_eligible_work"


def test_triage_reports_missing_plan_document_as_evidence_gap() -> None:
    decision = triage_tick(
        _observation(
            plans=[
                _plan(path="plans/in-progress/alpha.md", status="in_progress"),
            ],
            issues=[_issue(number=7)],
        )
    )
    assert decision.action == "report"
    assert decision.reason == "plan_evidence_unavailable"
    assert decision.plan_path == "plans/in-progress/alpha.md"
    assert decision.mutating is False


def test_triage_ignores_unrelated_issue_urls_in_plan_documents() -> None:
    decision = triage_tick(
        _observation(
            plans=[
                _plan(path="plans/in-progress/alpha.md", status="in_progress"),
            ],
            issues=[_issue(number=7)],
            plan_documents={
                "plans/in-progress/alpha.md": (
                    "Related: https://github.com/owner/repo/issues/99"
                )
            },
        )
    )
    assert decision.action == "plan_and_start"
    assert decision.issue_number == 7


def test_triage_selects_oldest_owner_issue() -> None:
    older = _issue(number=6, created_at="2026-08-01T00:00:00Z")
    newer = _issue(number=9, created_at="2026-09-01T00:00:00Z")
    decision = triage_tick(_observation(issues=[newer, older]))
    assert decision.action == "plan_and_start"
    assert decision.reason == "oldest_owner_issue"
    assert decision.issue_number == 6
    assert decision.issue_url == "https://github.com/owner/repo/issues/6"
    assert decision.workflow_name == CONCIERGE_WORKFLOW_NAME
    assert decision.team == CONCIERGE_TEAM
    assert decision.idempotency_key is not None
    assert decision.idempotency_key.startswith("concierge-start-")
    assert decision.idempotency_key == triage_tick(
        _observation(issues=[newer, older])
    ).idempotency_key
    assert decision.mutating is True


def test_triage_prefers_failed_lineage_over_plan_over_issue() -> None:
    decision = triage_tick(
        _observation(
            runs=[
                _run(
                    run_id="run-failed",
                    status="failed",
                    plan_path="plans/in-progress/alpha.md",
                )
            ],
            plans=[
                _plan(path="plans/in-progress/alpha.md", status="in_progress"),
                _plan(path="plans/todo/beta.md", status="todo"),
            ],
            issues=[_issue()],
        )
    )
    assert decision.action == "resume"
    assert decision.run_id == "run-failed"


def test_triage_prefers_launchable_plan_over_issue() -> None:
    decision = triage_tick(_observation(plans=[_plan()], issues=[_issue()]))
    assert decision.action == "start"
    assert decision.plan_path == "plans/todo/alpha.md"


def test_triage_idle_when_nothing_is_eligible() -> None:
    decision = triage_tick(_observation())
    assert decision.action == "idle"
    assert decision.reason == "no_eligible_work"
    assert decision.mutating is False


def test_triage_rejects_invalid_issue_observation() -> None:
    issue = _issue()
    del issue["number"]
    with pytest.raises(ConciergeError, match="observation_issue_invalid"):
        triage_tick(_observation(issues=[issue]))


def test_triage_rejects_invalid_project_observation() -> None:
    with pytest.raises(ConciergeError, match="observation_project_invalid"):
        triage_tick(TickObservation(project={}))


def test_triage_decision_fields_are_bounded() -> None:
    decision = TriageDecision(action="idle", reason="no_eligible_work")
    assert decision.run_id is None
    assert decision.plan_path is None
    assert decision.issue_number is None
    assert decision.issue_url is None
    assert decision.idempotency_key is None
    assert decision.workflow_name is None
    assert decision.team is None
    assert decision.mutating is False


# ---------------------------------------------------------------------------
# Authoritative tick boundary (ConciergeTickExecutor) end-to-end tests
# ---------------------------------------------------------------------------


def _github_issue(
    *,
    number: int = 7,
    title: str = "Fix it",
    body: str = "Details",
) -> dict[str, object]:
    return {
        "number": number,
        "author_id": CONCIERGE_OWNER_ID,
        "full_name": "owner/repo",
        "title": title,
        "body": body,
        "state": "open",
        "created_at": "2026-09-01T00:00:00Z",
    }


def _plan_result(
    *,
    status: str = "plan",
    markdown: str | None = None,
    question: str | None = None,
) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        status=status,
        markdown=markdown
        or "# Plan\n\nIssue: https://github.com/owner/repo/issues/7\n",
        question=question,
        provenance={},
    )


def _executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mcp: FakeMcp,
    github: FakeGithub | None = None,
    planner: FakePlanner | None = None,
    delivery_gate: object | None = None,
) -> ConciergeTickExecutor:
    monkeypatch.setattr(
        "aflow.concierge.repository_full_name", lambda root: "owner/repo"
    )
    return ConciergeTickExecutor(
        mcp=mcp,
        github=github if github is not None else FakeGithub(),
        planner_factory=(
            (lambda state_dir, mcp_client: planner) if planner is not None else None
        ),
        delivery_gate=(
            delivery_gate
            if delivery_gate is not None
            else FakeDeliveryGate(
                status=DeliveryStatus(state="pending", ci="pending", live="missing")
            )
        ),
    )


class FakeDeliveryGate:
    def __init__(self, status: DeliveryStatus) -> None:
        self.status = status
        self.calls: list[str | None] = []

    def evaluate(self, *, full_name: str | None) -> DeliveryStatus:
        self.calls.append(full_name)
        return self.status


def test_executor_reports_unregistered_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path, registered=False)
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "project_not_registered"
    assert outcome.mutating is False


def test_executor_defers_on_active_run_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[{"run_id": "run-1", "activity": "active", "status": "running"}],
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "defer"
    assert outcome.reason == "active_run_present"
    assert outcome.mutating is False
    assert not set(mcp.call_names()) & {"resume_run", "start_run", "create_plan"}


def test_executor_resumes_failed_lineage_with_idempotency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            {
                "run_id": "run-failed",
                "activity": "inactive",
                "status": "failed",
                "plan_path": "plans/in-progress/alpha.md",
                "evidence": {"can_resume": True},
            }
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "resume"
    assert outcome.reason == "resumed"
    assert outcome.mutating is True
    resume_calls = [
        args
        for name, args in mcp.calls
        if name == "resume_run"
    ]
    assert len(resume_calls) == 1
    assert resume_calls[0]["run_id"] == "run-failed"
    assert resume_calls[0]["idempotency_key"] == "concierge-resume-run-failed"


def test_executor_start_blocked_without_publication_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_unavailable"
    assert outcome.mutating is False
    names = mcp.call_names()
    assert names.count("get_global_config") == 1
    assert names.count("preflight_run") == 1
    assert "start_run" not in names


def test_executor_start_eligible_plan_starts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        publication=_publication_projection(
            available=True, remote="origin", branch="main"
        ),
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "start"
    assert outcome.reason == "started"
    assert outcome.mutating is True
    assert outcome.details["publish_remote"] == "origin"
    assert outcome.details["publish_branch"] == "main"
    assert mcp.call_names().count("start_run") == 1
    assert len(mcp.runs) == 1


@pytest.mark.parametrize(
    "publication",
    [
        pytest.param(
            _publication_projection(available=True, remote="upstream", branch="main"),
            id="wrong_remote",
        ),
        pytest.param(
            _publication_projection(available=True, remote="origin", branch="release"),
            id="wrong_branch",
        ),
        pytest.param(
            _publication_projection(available=True, remote="origin", branch=None),
            id="missing_branch",
        ),
    ],
)
def test_executor_start_blocked_with_wrong_publication_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication: Mapping[str, object],
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        publication=publication,
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_contradictory"
    assert outcome.mutating is False
    assert "start_run" not in mcp.call_names()


def test_executor_plans_but_blocks_start_without_publication_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    planner = FakePlanner(result=_plan_result())
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    state_dir = tmp_path / "state"
    outcome = executor.execute(
        state_dir=state_dir,
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_unavailable"
    assert outcome.mutating is False
    assert outcome.details["planner_model"] == CONCIERGE_PLANNER_MODEL
    assert outcome.details["planner_effort"] == CONCIERGE_PLANNER_EFFORT

    assert len(planner.plan_calls) == 1
    call = planner.plan_calls[0]
    assert call["repository_id"] == 42
    assert call["repository_full_name"] == "owner/repo"
    assert call["issue_number"] == 7
    assert call["project_id"] == "p1"
    canonical = call["canonical"]
    assert canonical.title == "Fix it"
    assert canonical.body == "Details"
    assert canonical.title_body_sha256 == source_hash("Fix it", "Details")
    expected_claim = hashlib.sha256(
        f"concierge-plan:owner/repo:7:{canonical.title_body_sha256}".encode("utf-8")
    ).hexdigest()
    assert call["claim_sha256"] == expected_claim
    record_path = state_dir / "planner-records" / f"{expected_claim}.json"
    assert record_path.is_file()

    assert len(mcp.created_plans) == 1
    created = mcp.created_plans[0]
    assert created["name"] == "owner-issue-7.md"
    assert "https://github.com/owner/repo/issues/7" in created["content"]
    names = mcp.call_names()
    assert names.count("get_global_config") == 1
    assert names.count("preflight_run") == 1
    assert "start_run" not in names


def test_executor_plans_and_starts_new_owner_issue_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        publication=_publication_projection(
            available=True, remote="origin", branch="main"
        ),
    )
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    planner = FakePlanner(result=_plan_result())
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "plan_and_start"
    assert outcome.reason == "planned_and_started"
    assert outcome.mutating is True
    assert outcome.details["publish_remote"] == "origin"
    assert outcome.details["publish_branch"] == "main"
    assert len(mcp.created_plans) == 1
    assert mcp.created_plans[0]["name"] == "owner-issue-7.md"
    assert mcp.call_names().count("start_run") == 1
    assert len(mcp.runs) == 1


def test_executor_no_duplicate_when_issue_planned_on_later_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans: list[dict[str, object]] = []
    documents: dict[str, str] = {}
    for index in range(CONCIERGE_PAGE_LIMIT):
        path = f"plans/done/p{index:03d}.md"
        plans.append({"path": path, "status": "done"})
        documents[path] = ""
    covering_path = "plans/in-progress/covering.md"
    plans.append({"path": covering_path, "status": "in_progress"})
    documents[covering_path] = (
        "Source: https://github.com/owner/repo/issues/7"
    )
    mcp = FakeMcp(
        project_root=tmp_path, plans=plans, documents=documents
    )
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    planner = FakePlanner(result=_plan_result())
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "idle"
    assert outcome.reason == "no_eligible_work"
    assert mcp.created_plans == []
    assert planner.plan_calls == []


def test_executor_reports_missing_plan_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[
            {"path": "plans/in-progress/alpha.md", "status": "in_progress"}
        ],
    )
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "evidence_unavailable"
    assert mcp.created_plans == []


def test_executor_planner_question_blocks_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    planner = FakePlanner(
        result=_plan_result(status="needs_attention", question="Which milestone?")
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "planner_needs_attention"
    assert outcome.details["question"] == "Which milestone?"
    assert mcp.created_plans == []


def test_executor_planner_failure_blocks_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    planner = FakePlanner(error=PlannerFailure("planner_provider_unavailable"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "planner_failed"
    assert outcome.details["planner_reason"] == "planner_provider_unavailable"
    assert mcp.created_plans == []


def test_executor_aborts_when_duplicate_appears_before_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )

    def inject_duplicate() -> None:
        mcp.plans.append({"path": "plans/todo/rival.md", "status": "todo"})
        mcp.documents["plans/todo/rival.md"] = (
            "Source: https://github.com/owner/repo/issues/7"
        )

    planner = FakePlanner(result=_plan_result())
    planner.side_effect = inject_duplicate
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "duplicate_detected_before_authoring"
    assert mcp.created_plans == []


def test_executor_resume_timeout_reconciles_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            {
                "run_id": "run-failed",
                "activity": "inactive",
                "status": "failed",
                "plan_path": "plans/in-progress/alpha.md",
                "evidence": {"can_resume": True},
            }
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    mcp.failures["resume_run"] = ConciergeError("mcp_call_failed")
    mcp.pre_failures["resume_run"] = lambda: mcp.runs[0].update(
        {"activity": "active", "status": "running"}
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "resume"
    assert outcome.reason == "resume_reconciled_active"
    assert outcome.mutating is True


def test_executor_start_timeout_reconciles_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        publication=_publication_projection(available=True, remote="origin", branch="main"),
    )
    mcp.failures["start_run"] = ConciergeError("mcp_call_failed")
    mcp.pre_failures["start_run"] = lambda: mcp.runs.append(
        {
            "run_id": "run-late",
            "activity": "active",
            "status": "running",
            "plan_path": "plans/todo/alpha.md",
        }
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "start"
    assert outcome.reason == "start_reconciled_active"
    assert outcome.mutating is True


def test_mcp_http_endpoint_session_lifecycle_uses_async_context_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp
    import mcp.client.streamable_http as mcp_streamable
    from types import SimpleNamespace

    from aflow.concierge import McpHttpEndpoint

    transport_state: dict[str, bool] = {"entered": False, "exited": False}

    class FakeTransport:
        async def __aenter__(self) -> tuple[object, object, object]:
            transport_state["entered"] = True
            return object(), object(), None

        async def __aexit__(self, *exc: object) -> bool:
            transport_state["exited"] = True
            return False

    session_state: dict[str, object] = {
        "initialized": False,
        "exited": False,
        "calls": [],
    }

    class FakeSession:
        def __init__(self, read: object, write: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            session_state["exited"] = True
            return False

        async def initialize(self) -> None:
            session_state["initialized"] = True

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> object:
            session_state["calls"].append((name, dict(arguments)))
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=json.dumps({"projects": []}))],
            )

    monkeypatch.setattr(
        mcp_streamable, "streamablehttp_client", lambda *a, **k: FakeTransport()
    )
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    endpoint = McpHttpEndpoint("http://127.0.0.1:8765/mcp", TOKEN)
    payload = endpoint.call_tool("list_projects", {})
    assert payload == {"projects": []}
    assert transport_state["entered"] is True
    assert session_state["initialized"] is True
    assert session_state["calls"] == [("list_projects", {})]
    endpoint.close()
    assert session_state["exited"] is True
    assert transport_state["exited"] is True


def test_mcp_http_endpoint_setup_failure_closes_entered_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp
    import mcp.client.streamable_http as mcp_streamable

    from aflow.concierge import McpHttpEndpoint

    state: dict[str, bool] = {"transport_exited": False, "session_exited": False}

    class FakeTransport:
        async def __aenter__(self) -> tuple[object, object, object]:
            return object(), object(), None

        async def __aexit__(self, *exc: object) -> bool:
            state["transport_exited"] = True
            return False

    class FakeSession:
        def __init__(self, read: object, write: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            state["session_exited"] = True
            return False

        async def initialize(self) -> None:
            raise RuntimeError("handshake failed")

    monkeypatch.setattr(
        mcp_streamable, "streamablehttp_client", lambda *a, **k: FakeTransport()
    )
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    endpoint = McpHttpEndpoint("http://127.0.0.1:8765/mcp", TOKEN)
    with pytest.raises(ConciergeError) as excinfo:
        endpoint.call_tool("list_projects", {})
    assert excinfo.value.reason == "mcp_unavailable"
    assert state["session_exited"] is True
    assert state["transport_exited"] is True
    assert endpoint._loop is None


@pytest.mark.parametrize(
    "preflight",
    [
        pytest.param(
            {
                "execution_mode": "same_checkout",
                "blockers": [],
                "requires_confirmation": False,
            },
            id="same_checkout",
        ),
        pytest.param(
            {
                "execution_mode": "new_worktree",
                "blockers": ["dirty"],
                "requires_confirmation": False,
            },
            id="blockers",
        ),
        pytest.param(
            {
                "execution_mode": "new_worktree",
                "blockers": [],
                "requires_confirmation": True,
            },
            id="requires_confirmation",
        ),
    ],
)
def test_executor_start_reports_unsafe_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight: dict[str, object],
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        preflight=preflight,
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_contradictory"
    assert outcome.mutating is False
    names = mcp.call_names()
    assert "preflight_run" in names
    assert "start_run" not in names


def test_executor_start_reports_startup_question_without_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        start_payload={
            "startup_question": {
                "question_id": "q1",
                "kind": "startup",
                "message": "Which step should start?",
                "options": {},
                "choices": (),
                "run_id": None,
                "schema_version": 1,
            }
        },
        publication=_publication_projection(available=True, remote="origin", branch="main"),
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "startup_question"
    assert outcome.mutating is False
    assert outcome.details["question"] == "Which step should start?"
    assert mcp.runs == []
    assert mcp.call_names().count("start_run") == 1


def test_executor_start_reports_invalid_start_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        start_payload={"result": {"created": True}},
        publication=_publication_projection(available=True, remote="origin", branch="main"),
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "start_result_invalid"
    assert outcome.mutating is False
    assert mcp.runs == []


def test_executor_start_reports_missing_capabilities_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
    )
    mcp.failures["get_project_capabilities"] = ConciergeError("mcp_call_failed")
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_unavailable"
    assert outcome.mutating is False
    assert "start_run" not in mcp.call_names()


@pytest.mark.parametrize(
    ("auto_consume_plans", "max_concurrent_implementations"),
    [
        pytest.param(True, 1, id="auto_consume_plans"),
        pytest.param(False, 2, id="multiple_slots"),
    ],
)
def test_executor_start_reports_contradictory_scheduling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_consume_plans: bool,
    max_concurrent_implementations: int,
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        auto_consume_plans=auto_consume_plans,
        max_concurrent_implementations=max_concurrent_implementations,
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_contradictory"
    assert outcome.mutating is False
    assert "start_run" not in mcp.call_names()


def test_executor_start_blocked_when_global_config_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
    )
    mcp.failures["get_global_config"] = ConciergeError("mcp_call_failed")
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_unavailable"
    assert outcome.mutating is False
    assert "start_run" not in mcp.call_names()


@pytest.mark.parametrize(
    "global_config",
    [
        pytest.param(
            _global_config(
                workflows_toml=(
                    "[workflow]\n"
                    'setup = ["worktree"]\n'
                    'teardown = ["merge", "rm_worktree"]\n'
                    "[workflow.checkpoint_delivery]\n"
                )
            ),
            id="setup_missing_branch",
        ),
        pytest.param(
            _global_config(
                workflows_toml=(
                    "[workflow]\n"
                    'setup = ["worktree", "branch"]\n'
                    'teardown = ["merge"]\n'
                    "[workflow.checkpoint_delivery]\n"
                )
            ),
            id="teardown_missing_rm_worktree",
        ),
        pytest.param(
            _global_config(state="configuration_required"),
            id="validation_not_ready",
        ),
        pytest.param(
            _global_config(
                workflows_toml=(
                    "[workflow]\n"
                    'setup = ["worktree", "branch"]\n'
                    'teardown = ["merge", "rm_worktree"]\n'
                )
            ),
            id="workflow_missing",
        ),
    ],
)
def test_executor_start_blocked_when_workflow_lifecycle_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    global_config: Mapping[str, object],
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        global_config=global_config,
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "launch_evidence_contradictory"
    assert outcome.mutating is False
    assert "start_run" not in mcp.call_names()


def test_executor_resume_not_admitted_when_can_resume_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            {
                "run_id": "run-failed",
                "activity": "inactive",
                "status": "failed",
                "plan_path": "plans/in-progress/alpha.md",
                "evidence": {"can_resume": False},
            }
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "resume_not_admitted"
    assert outcome.mutating is False
    assert "resume_run" not in mcp.call_names()
    assert mcp.runs[0]["activity"] == "inactive"


def test_executor_resume_not_admitted_on_lineage_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            {
                "run_id": "run-failed",
                "activity": "inactive",
                "status": "failed",
                "plan_path": "plans/in-progress/alpha.md",
            }
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
        get_run_payloads={
            "run-failed": {
                "run_id": "run-failed",
                "activity": "inactive",
                "status": "failed",
                "plan_path": "plans/in-progress/other.md",
                "evidence": {"can_resume": True},
            }
        },
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "resume_not_admitted"
    assert outcome.mutating is False
    assert "resume_run" not in mcp.call_names()


def test_executor_resume_not_admitted_when_get_run_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            {
                "run_id": "run-failed",
                "activity": "inactive",
                "status": "failed",
                "plan_path": "plans/in-progress/alpha.md",
                "evidence": {"can_resume": True},
            }
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    mcp.failures["get_run"] = ConciergeError("mcp_call_failed")
    executor = _executor(tmp_path, monkeypatch, mcp=mcp)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "resume_not_admitted"
    assert outcome.mutating is False
    assert "resume_run" not in mcp.call_names()


def test_executor_plan_and_start_aborts_on_competing_run_during_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )

    def inject_competing_run() -> None:
        mcp.runs.append(
            {
                "run_id": "run-rival",
                "activity": "active",
                "status": "running",
                "plan_path": "plans/todo/rival.md",
            }
        )

    planner = FakePlanner(result=_plan_result())
    planner.side_effect = inject_competing_run
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "state_changed_before_action"
    assert outcome.mutating is False
    assert mcp.created_plans == []
    names = mcp.call_names()
    assert "create_plan" not in names
    assert "start_run" not in names


def test_executor_plan_and_start_aborts_on_claim_during_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )

    def inject_claim() -> None:
        mcp.plans.append({"path": "plans/todo/owner-issue-7.md", "status": "todo"})
        mcp.documents["plans/todo/owner-issue-7.md"] = "# Plan\n"
        mcp.runs.append(
            {
                "run_id": "run-claim",
                "activity": "inactive",
                "status": "succeeded",
                "plan_path": "plans/todo/owner-issue-7.md",
            }
        )

    planner = FakePlanner(result=_plan_result())
    planner.side_effect = inject_claim
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "state_changed_before_action"
    assert outcome.mutating is False
    assert mcp.created_plans == []
    names = mcp.call_names()
    assert "create_plan" not in names
    assert "start_run" not in names


def test_executor_reports_github_evidence_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(error=ConciergeError("github_unavailable"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state",
        project_root=tmp_path,
        environment=ENVIRONMENT,
    )
    assert outcome.action == "report"
    assert outcome.reason == "github_evidence_unavailable"
    assert mcp.created_plans == []


def test_build_concierge_planner_pins_astra_high_evidence(
    tmp_path: Path,
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    planner = _build_concierge_planner(tmp_path / "state", mcp)
    assert planner.model == CONCIERGE_PLANNER_MODEL == "gpt-6-astra"
    assert planner.effort == CONCIERGE_PLANNER_EFFORT == "high"
    assert planner.require_concierge_evidence is True
    assert planner.state_root == tmp_path / "state" / "planner"
    assert isinstance(planner.client, McpRegistryClient)


def test_mcp_registry_client_maps_project_lookup() -> None:
    mcp = FakeMcp(project_root=Path("/root/code/agent-flow"))
    client = McpRegistryClient(mcp)
    payload = client.get_json("private", "/api/control-plane/projects")
    assert payload["projects"][0]["project_id"] == "p1"
    with pytest.raises(ConciergeError):
        client.get_json("private", "/api/control-plane/runs")
    with pytest.raises(ConciergeError):
        client.get_json("public", "/api/control-plane/projects")


def test_repository_full_name_parses_github_remotes(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/owner/repo.git",
        ],
        check=True,
    )
    assert repository_full_name(repo) == "owner/repo"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "origin",
            "git@github.com:owner/repo.git",
        ],
        check=True,
    )
    assert repository_full_name(repo) == "owner/repo"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "origin",
            "https://gitlab.com/owner/repo.git",
        ],
        check=True,
    )
    with pytest.raises(ConciergeError):
        repository_full_name(repo)


def test_github_rest_client_filters_pull_requests_and_maps_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload: object) -> None:
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self, limit: int = -1) -> bytes:
            return self._payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    responses = {
        "/repos/owner/repo": {"id": 42},
        "/repos/owner/repo/issues?state=open&per_page=100": [
            {
                "number": 7,
                "user": {"id": CONCIERGE_OWNER_ID},
                "title": "Fix it",
                "body": "Details",
                "state": "open",
                "created_at": "2026-09-01T00:00:00Z",
            },
            {
                "number": 8,
                "user": {"id": 12345},
                "title": "A pull request",
                "body": "Details",
                "state": "open",
                "created_at": "2026-09-01T00:00:00Z",
                "pull_request": {},
            },
        ],
    }

    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        return FakeResponse(responses[url.replace("https://api.github.com", "")])

    monkeypatch.setattr(
        "aflow.concierge.urllib_request.urlopen", fake_urlopen
    )
    client = GithubRestClient(token="gh-token")
    page = client.open_issues("owner/repo")
    assert page.repository_id == 42
    assert [issue["number"] for issue in page.issues] == [7]
    assert page.issues[0]["author_id"] == CONCIERGE_OWNER_ID
    assert page.issues[0]["full_name"] == "owner/repo"


# Checkpoint 2: trusted defect confirmation and exact-SHA CI precedence
# ---------------------------------------------------------------------------


def _failed_run(
    *,
    run_id: str = "run-defect",
    reason_code: str = "startup_failed",
    activity: str = "inactive",
    status: str = "failed",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "activity": activity,
        "status": status,
        "status_reason_code": reason_code,
        "plan_path": "plans/in-progress/alpha.md",
        "evidence": {"can_resume": True},
    }


def _valid_confirmation(**overrides: object) -> dict[str, object]:
    confirmation = {
        "schema_version": 1,
        "kind": "engine_internal_assertion",
        "source": "controller",
        "component": "control_plane/repository.py",
        "site": "get_run_status:418",
        "signature": "a" * 64,
    }
    confirmation.update(overrides)
    return confirmation


def _confirmed_run(
    *,
    run_id: str = "run-defect",
    signature: str = "a" * 64,
    reason_code: str = "startup_failed",
) -> dict[str, object]:
    run = _failed_run(run_id=run_id, reason_code=reason_code)
    run["defect_confirmation"] = _valid_confirmation(signature=signature)
    return run


def test_classify_defect_files_nothing_without_confirmation() -> None:
    for reason_code in ("startup_failed", "controller_failed", "worker_failed"):
        assert classify_defect(_failed_run(reason_code=reason_code)) is None
    assert classify_defect(_failed_run(activity="active")) is None
    assert classify_defect(_failed_run(status="succeeded")) is None
    run = _failed_run()
    del run["run_id"]
    run["defect_confirmation"] = _valid_confirmation()
    assert classify_defect(run) is None


def test_classify_defect_rejects_malformed_confirmations() -> None:
    malformed = (
        None,
        "confirmed",
        42,
        [_valid_confirmation()],
        {**_valid_confirmation(), "schema_version": 2},
        {**_valid_confirmation(), "kind": "worker_failure"},
        {**_valid_confirmation(), "source": "worker"},
        {**_valid_confirmation(), "component": "../escape.py"},
        {**_valid_confirmation(), "component": "/absolute/path.py"},
        {**_valid_confirmation(), "component": "sub/../sub/module.py"},
        {**_valid_confirmation(), "component": "module.txt"},
        {**_valid_confirmation(), "site": "no_line_number"},
        {**_valid_confirmation(), "site": "fn:-1"},
        {**_valid_confirmation(), "signature": "A" * 64},
        {**_valid_confirmation(), "signature": "abc123"},
        {k: v for k, v in _valid_confirmation().items() if k != "signature"},
        {**_valid_confirmation(), "detail": "assert x"},
    )
    for confirmation in malformed:
        run = _failed_run()
        run["defect_confirmation"] = confirmation
        assert classify_defect(run) is None, confirmation


def test_classify_defect_returns_bounded_evidence_for_valid_confirmation() -> None:
    run = _confirmed_run(run_id="run-42", signature="b" * 64)
    evidence = classify_defect(run)
    assert evidence is not None
    assert evidence.source == "controller"
    assert evidence.signature == "b" * 64
    assert evidence.subject == "run-42"
    assert evidence.component == "control_plane/repository.py"
    assert evidence.site == "get_run_status:418"


def test_defect_evidence_prefers_first_confirmed_run() -> None:
    runs = [
        _failed_run(run_id="run-b"),
        _confirmed_run(run_id="run-a", signature="c" * 64),
        _confirmed_run(run_id="run-c", signature="d" * 64),
    ]
    evidence = defect_evidence(runs)
    assert evidence is not None
    assert evidence.subject == "run-a"
    assert evidence.signature == "c" * 64
    assert defect_evidence([_failed_run()]) is None


def test_defect_signatures_keeps_distinct_signatures_in_stable_order() -> None:
    runs = [
        _confirmed_run(run_id="run-c", signature="d" * 64),
        _confirmed_run(run_id="run-a", signature="c" * 64),
        _failed_run(run_id="run-b"),
        _confirmed_run(run_id="run-d", signature="c" * 64),
    ]
    evidence = defect_signatures(runs)
    assert [item.subject for item in evidence] == ["run-a", "run-c"]
    assert [item.signature for item in evidence] == ["c" * 64, "d" * 64]
    assert defect_signatures([_failed_run()]) == ()


def test_defect_fingerprint_is_stable_per_signature_not_run() -> None:
    first_run = DefectEvidence(source="controller", signature="a" * 64, subject="run-1")
    second_run = DefectEvidence(source="controller", signature="a" * 64, subject="run-2")
    first = defect_fingerprint(first_run)
    second = defect_fingerprint(second_run)
    assert first == second
    assert len(first) == 64
    marker = defect_fingerprint_marker(first)
    assert marker.startswith("aflow-concierge-defect-fingerprint:")
    assert has_defect_fingerprint("noise\n" + marker + "\n", first)
    assert not has_defect_fingerprint("noise", first)
    assert defect_fingerprint(
        DefectEvidence(source="controller", signature="b" * 64, subject="run-1")
    ) != first


def test_defect_issue_body_is_sanitized_and_bounded() -> None:
    evidence = classify_defect(_confirmed_run(run_id="run-1"))
    assert evidence is not None
    fingerprint = defect_fingerprint(evidence)
    title = defect_issue_title(evidence)
    body = defect_issue_body(evidence, fingerprint)
    assert "control_plane/repository.py:get_run_status:418" in title
    assert "run-1" in title
    assert "source: controller" in body
    assert "component: control_plane/repository.py" in body
    assert "site: get_run_status:418" in body
    assert f"signature: {'a' * 64}" in body
    assert defect_fingerprint_marker(fingerprint) in body
    assert "token" not in body.lower()
    assert len(body.encode("utf-8")) < 2048


def test_project_delivery_reports_pending_without_evidence() -> None:
    status = project_delivery(DeliveryEvidence())
    assert status.state == "pending"
    assert status.ci == "pending"
    assert status.live == "missing"


def test_project_delivery_reports_ok_when_exact_sha_is_live() -> None:
    sha = "a" * 40
    status = project_delivery(
        DeliveryEvidence(
            origin_main_sha=sha,
            ci="green",
            deploy_phase="deployed",
            current_commit=sha,
            live_commit=sha,
        )
    )
    assert status.state == "ok"
    assert status.ci == "green"
    assert status.live == "installed"


def test_project_delivery_fails_on_red_ci_or_failed_deploy() -> None:
    sha = "a" * 40
    red = project_delivery(
        DeliveryEvidence(
            origin_main_sha=sha,
            ci="red",
            deploy_phase="deployed",
            current_commit=sha,
            live_commit=sha,
        )
    )
    assert red.state == "failed"
    assert red.ci == "red"
    failed = project_delivery(
        DeliveryEvidence(
            origin_main_sha=sha,
            ci="green",
            deploy_phase="failed",
            current_commit=sha,
            live_commit=sha,
        )
    )
    assert failed.state == "failed"
    assert failed.live == "missing"


def test_project_delivery_reports_live_release_separately_from_ci() -> None:
    origin = "a" * 40
    stale = "b" * 40
    status = project_delivery(
        DeliveryEvidence(
            origin_main_sha=origin,
            ci="green",
            deploy_phase="deployed",
            current_commit=stale,
            live_commit=stale,
        )
    )
    assert status.ci == "green"
    assert status.live == "stale"
    assert status.state == "pending"


def test_executor_does_not_file_generic_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_failed_run(reason_code="startup_failed")],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    github = FakeGithub()
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "resume"
    assert outcome.reason == "resumed"
    assert github.created_issues == []


def test_executor_files_one_new_confirmed_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run()],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    github = FakeGithub()
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "file_defect"
    assert outcome.reason == "defect_filed"
    assert outcome.mutating is True
    assert len(github.created_issues) == 1
    full_name, title, body = github.created_issues[0]
    assert full_name == "owner/repo"
    assert "control_plane/repository.py:get_run_status:418" in title
    fingerprint = str(outcome.details["defect_fingerprint"])
    assert defect_fingerprint_marker(fingerprint) in body
    assert "resume_run" not in mcp.call_names()
    assert "start_run" not in mcp.call_names()


def test_executor_continues_after_an_already_filed_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run()],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    evidence = classify_defect(_confirmed_run())
    assert evidence is not None
    fingerprint = defect_fingerprint(evidence)
    github = FakeGithub(
        search_results=[
            {"number": 55, "body": f"previously filed\n{defect_fingerprint_marker(fingerprint)}\n"}
        ]
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "resume"
    assert outcome.reason == "resumed"
    assert outcome.mutating is True
    assert github.created_issues == []
    assert mcp.call_names().count("resume_run") == 1


def test_executor_retained_defect_files_once_then_continues_on_later_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run()],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    github = FakeGithub()
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    first = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert first.action == "file_defect"
    assert first.reason == "defect_filed"
    assert first.mutating is True
    assert len(github.created_issues) == 1
    assert "resume_run" not in mcp.call_names()
    assert "start_run" not in mcp.call_names()

    github.search_results = (
        {"number": 101, "body": github.created_issues[0][2]},
    )
    second = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert second.action == "resume"
    assert second.reason == "resumed"
    assert second.mutating is True
    assert len(github.created_issues) == 1
    assert mcp.call_names().count("resume_run") == 1


def test_executor_files_one_issue_for_same_signature_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First tick: run-1 confirms the defect and files exactly one issue.
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run(run_id="run-1")],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    github = FakeGithub()
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    first = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert first.action == "file_defect"
    assert len(github.created_issues) == 1

    # Second tick: a different run ID with the same trusted signature must
    # not file a duplicate.
    mcp_later = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run(run_id="run-2")],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    github.search_results = (
        {"number": 101, "body": github.created_issues[0][2]},
    )
    executor_later = _executor(tmp_path, monkeypatch, mcp=mcp_later, github=github)
    second = executor_later.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert second.action == "resume"
    assert second.reason == "resumed"
    assert len(github.created_issues) == 1


def test_executor_files_later_signature_when_earlier_is_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    early = classify_defect(_confirmed_run(run_id="run-a", signature="a" * 64))
    late = classify_defect(_confirmed_run(run_id="run-b", signature="b" * 64))
    assert early is not None
    assert late is not None
    early_fingerprint = defect_fingerprint(early)
    late_fingerprint = defect_fingerprint(late)
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            _confirmed_run(run_id="run-a", signature="a" * 64),
            _confirmed_run(run_id="run-b", signature="b" * 64),
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    github = FakeGithub(
        search_results=[
            {
                "number": 50,
                "body": f"previously filed\n{defect_fingerprint_marker(early_fingerprint)}\n",
            }
        ]
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    # The earlier signature is already filed, so the later distinct signature
    # must be the one filed, and only one issue may be created this tick.
    assert outcome.action == "file_defect"
    assert outcome.reason == "defect_filed"
    assert outcome.mutating is True
    assert outcome.details["defect_fingerprint"] == late_fingerprint
    assert len(github.created_issues) == 1
    assert github.created_issues[0][0] == "owner/repo"
    assert defect_fingerprint_marker(late_fingerprint) in github.created_issues[0][2]
    assert "resume_run" not in mcp.call_names()
    assert "start_run" not in mcp.call_names()


def test_executor_continues_when_all_confirmed_signatures_are_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    early = classify_defect(_confirmed_run(run_id="run-a", signature="a" * 64))
    late_run = _confirmed_run(run_id="run-b", signature="b" * 64)
    # A distinct plan keeps triage ownership unambiguous across two failed
    # runs, so the tick can continue to the eligible safe action.
    late_run["plan_path"] = "plans/in-progress/beta.md"
    late = classify_defect(late_run)
    assert early is not None
    assert late is not None
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            _confirmed_run(run_id="run-a", signature="a" * 64),
            late_run,
        ],
        plans=[
            {"path": "plans/in-progress/alpha.md", "status": "in_progress"},
            {"path": "plans/in-progress/beta.md", "status": "in_progress"},
        ],
        documents={
            "plans/in-progress/alpha.md": "# Plan\n",
            "plans/in-progress/beta.md": "# Plan\n",
        },
    )
    github = FakeGithub(
        search_results=[
            {
                "number": 50,
                "body": (
                    "previously filed\n"
                    f"{defect_fingerprint_marker(defect_fingerprint(early))}\n"
                    f"{defect_fingerprint_marker(defect_fingerprint(late))}\n"
                ),
            }
        ]
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    # Every distinct signature is already filed, so the tick continues to the
    # eligible safe action without filing anything.
    assert outcome.action == "resume"
    assert outcome.reason == "resumed"
    assert github.created_issues == []
    assert mcp.call_names().count("resume_run") == 1


def test_executor_defect_search_failure_is_report_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[
            _confirmed_run(run_id="run-a", signature="a" * 64),
            _confirmed_run(run_id="run-b", signature="b" * 64),
        ],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    github = FakeGithub(error=ConciergeError("github_unavailable"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    # An uncertain GitHub result must never be guessed as filing-safe.
    assert outcome.action == "report"
    assert outcome.reason == "defect_dedup_unavailable"
    assert outcome.mutating is False
    assert github.created_issues == []
    assert "resume_run" not in mcp.call_names()
    assert "start_run" not in mcp.call_names()


def test_executor_partial_search_envelope_is_report_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run()],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    # A partial (incomplete_results: true) or malformed search envelope makes
    # the REST client raise, exactly like an unavailable search.
    github = FakeGithub(error=ConciergeError("github_search_invalid"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "report"
    assert outcome.reason == "defect_dedup_unavailable"
    assert outcome.mutating is False
    assert github.created_issues == []
    assert "resume_run" not in mcp.call_names()
    assert "start_run" not in mcp.call_names()


def test_executor_does_not_file_malformed_or_unconfirmed_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases: list[dict[str, object]] = [
        _failed_run(reason_code="startup_failed"),
        _failed_run(reason_code="controller_failed"),
        _failed_run(reason_code="worker_failed"),
        _failed_run(reason_code="missing_completion"),
    ]
    broken = _confirmed_run()
    broken["defect_confirmation"] = {
        **_valid_confirmation(),
        "signature": "not-a-signature",
    }
    cases.append(broken)
    for run in cases:
        mcp = FakeMcp(
            project_root=tmp_path,
            runs=[run],
            plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
            documents={"plans/in-progress/alpha.md": "# Plan\n"},
        )
        github = FakeGithub()
        executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=github)
        outcome = executor.execute(
            state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
        )
        assert outcome.action == "resume"
        assert github.created_issues == []


def test_delivery_gate_blocks_start_on_failed_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        publication=_publication_projection(available=True, remote="origin", branch="main"),
    )
    gate = FakeDeliveryGate(DeliveryStatus(state="failed", ci="red", live="missing"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, delivery_gate=gate)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "report"
    assert outcome.reason == "delivery_gate_failed"
    assert outcome.mutating is False
    assert outcome.details["delivery_ci"] == "red"
    assert outcome.details["delivery_live"] == "missing"
    assert "start_run" not in mcp.call_names()


def test_delivery_gate_blocks_plan_and_start_on_failed_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    github = FakeGithub(
        page=GithubIssuePage(repository_id=42, issues=(_github_issue(),))
    )
    planner = FakePlanner(result=_plan_result())
    gate = FakeDeliveryGate(DeliveryStatus(state="failed", ci="red", live="missing"))
    executor = _executor(
        tmp_path, monkeypatch, mcp=mcp, github=github, planner=planner, delivery_gate=gate
    )
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "report"
    assert outcome.reason == "delivery_gate_failed"
    assert outcome.mutating is False
    assert planner.plan_calls == []
    assert "create_plan" not in mcp.call_names()
    assert "start_run" not in mcp.call_names()


def test_delivery_gate_does_not_block_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_failed_run(reason_code="worker_failed")],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    gate = FakeDeliveryGate(DeliveryStatus(state="failed", ci="red", live="missing"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, delivery_gate=gate)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "resume"
    assert outcome.mutating is True


def test_pending_delivery_gate_allows_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        publication=_publication_projection(available=True, remote="origin", branch="main"),
    )
    gate = FakeDeliveryGate(DeliveryStatus(state="pending", ci="pending", live="missing"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, delivery_gate=gate)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "start"
    assert outcome.mutating is True


def test_delivery_status_is_reported_on_idle_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = FakeMcp(project_root=tmp_path)
    gate = FakeDeliveryGate(DeliveryStatus(state="pending", ci="pending", live="stale"))
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, delivery_gate=gate)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert outcome.action == "idle"
    assert outcome.details["delivery_state"] == "pending"
    assert outcome.details["delivery_ci"] == "pending"
    assert outcome.details["delivery_live"] == "stale"


def test_local_delivery_gate_reports_pending_without_evidence(tmp_path: Path) -> None:
    github = FakeGithub()
    gate = LocalDeliveryGate(
        github=github,
        project_root=tmp_path,
        deploy_status_path=tmp_path / "absent.json",
        live_release_root=tmp_path / "absent-live",
    )
    status = gate.evaluate(full_name="owner/repo")
    assert status.state == "pending"
    assert status.ci == "pending"
    assert status.live == "missing"


def _live_release(live_root: Path, release_id: str) -> None:
    release = live_root / release_id
    release.mkdir(parents=True)
    os.symlink(release, live_root / "current")


def test_local_delivery_gate_reports_live_and_ci_separately(tmp_path: Path) -> None:
    origin = "a" * 40
    stale = "b" * 40
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"phase": "deployed", "current_commit": stale}), encoding="utf-8"
    )
    live_root = tmp_path / "live"
    _live_release(live_root, stale)
    github = FakeGithub(workflow_runs=[(1, 1, "completed", "success")])
    gate = LocalDeliveryGate(
        github=github,
        project_root=tmp_path,
        deploy_status_path=status_path,
        live_release_root=live_root,
    )
    gate._origin_main_sha = lambda: origin  # type: ignore[method-assign]
    status = gate.evaluate(full_name="owner/repo")
    assert status.ci == "green"
    assert status.live == "stale"
    assert status.state == "pending"


def test_local_delivery_gate_reports_ok_when_exact_sha_is_live(tmp_path: Path) -> None:
    origin = "a" * 40
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"phase": "deployed", "current_commit": origin}), encoding="utf-8"
    )
    live_root = tmp_path / "live"
    _live_release(live_root, origin)
    github = FakeGithub(workflow_runs=[(1, 1, "completed", "success")])
    gate = LocalDeliveryGate(
        github=github,
        project_root=tmp_path,
        deploy_status_path=status_path,
        live_release_root=live_root,
    )
    gate._origin_main_sha = lambda: origin  # type: ignore[method-assign]
    status = gate.evaluate(full_name="owner/repo")
    assert status.state == "ok"
    assert status.ci == "green"
    assert status.live == "installed"


# Checkpoint 3 review fixes: remote main tip and qualifying CI only
# ---------------------------------------------------------------------------


def _git_run(argv: list[str], cwd: Path | None = None) -> None:
    subprocess.run(
        argv, cwd=cwd, check=True, capture_output=True, text=True
    )


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_commit(repo: Path, filename: str, content: str) -> None:
    (repo / filename).write_text(content, encoding="utf-8")
    _git_run(["git", "add", filename], cwd=repo)
    _git_run(["git", "commit", "-m", filename], cwd=repo)


def _git_init_remote_and_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare remote and a working repo whose origin points to it."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git_run(["git", "init", "--bare", "--initial-branch=main", "."], cwd=bare)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_run(["git", "init", "--initial-branch=main", "."], cwd=repo)
    _git_run(["git", "config", "user.email", "concierge@test"], cwd=repo)
    _git_run(["git", "config", "user.name", "Concierge Test"], cwd=repo)
    _git_run(["git", "remote", "add", "origin", str(bare)], cwd=repo)
    return bare, repo


def test_local_delivery_gate_uses_remote_main_tip_not_stale_tracking_ref(
    tmp_path: Path,
) -> None:
    bare, repo = _git_init_remote_and_repo(tmp_path)
    _git_commit(repo, "a.txt", "a")
    _git_run(["git", "push", "origin", "main"], cwd=repo)
    stale = _git_head(repo)

    # A second publisher advances the remote main beyond this checkout's
    # tracking ref, leaving ``origin/main`` stale in ``repo``.
    repo2 = tmp_path / "repo2"
    _git_run(["git", "clone", str(bare), str(repo2)])
    _git_run(["git", "config", "user.email", "other@test"], cwd=repo2)
    _git_run(["git", "config", "user.name", "Other"], cwd=repo2)
    _git_commit(repo2, "b.txt", "b")
    _git_run(["git", "push", "origin", "main"], cwd=repo2)
    remote_tip = _git_head(repo2)
    assert remote_tip != stale

    # The live release sits at the stale SHA, so a gate that (wrongly) used the
    # stale tracking ref would report it as installed.
    live_root = tmp_path / "live"
    _live_release(live_root, stale)
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"phase": "deployed", "current_commit": stale}),
        encoding="utf-8",
    )
    github = FakeGithub(workflow_runs=[(1, 1, "completed", "success")])
    gate = LocalDeliveryGate(
        github=github,
        project_root=repo,
        deploy_status_path=status_path,
        live_release_root=live_root,
    )
    assert gate._origin_main_sha() == remote_tip
    status = gate.evaluate(full_name="owner/repo")
    # Both the CI query and the live comparison use the remote tip, not the
    # stale local tracking ref.
    assert github.workflow_sha_queries == [("owner/repo", remote_tip)]
    assert status.live == "stale"


def test_local_delivery_gate_reports_pending_when_remote_unreachable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_run(["git", "init", "--initial-branch=main", "."], cwd=repo)
    _git_run(["git", "config", "user.email", "concierge@test"], cwd=repo)
    _git_run(["git", "config", "user.name", "Concierge Test"], cwd=repo)
    _git_commit(repo, "a.txt", "a")
    stale = _git_head(repo)
    # Point origin at a nonexistent path so ls-remote fails, and seed a stale
    # local tracking ref that must never be used as a fallback.
    _git_run(
        ["git", "remote", "add", "origin", str(tmp_path / "absent.git")],
        cwd=repo,
    )
    _git_run(
        ["git", "update-ref", "refs/remotes/origin/main", stale], cwd=repo
    )
    github = FakeGithub(workflow_runs=[(1, 1, "completed", "success")])
    gate = LocalDeliveryGate(
        github=github,
        project_root=repo,
        deploy_status_path=tmp_path / "absent.json",
        live_release_root=tmp_path / "absent-live",
    )
    assert gate._origin_main_sha() is None
    status = gate.evaluate(full_name="owner/repo")
    assert status.state == "pending"
    assert status.ci == "pending"
    # An unreachable remote must not fall back to the stale tracking ref.
    assert github.workflow_sha_queries == []


def _fake_github_response(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, object],
    record: list[tuple[str, bytes | None]] | None = None,
) -> None:
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload: object) -> None:
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self, limit: int = -1) -> bytes:
            return self._payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        path = url.replace("https://api.github.com", "")
        if record is not None:
            record.append((request.method, request.data))  # type: ignore[attr-defined]
        return FakeResponse(paths[path])

    monkeypatch.setattr("aflow.concierge.urllib_request.urlopen", fake_urlopen)


def test_github_rest_client_filters_to_qualifying_ci_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    sha = "c" * 40
    runs = [
        # Qualifying CI: the only run that counts (a failure).
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "run_number": 4,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "failure",
        },
        # Unrelated workflow that succeeded; must not green the CI.
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/unrelated.yml",
            "status": "completed",
            "conclusion": "success",
        },
        # Same CI workflow but a different branch; must not count.
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "feature",
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
        },
        # Same CI workflow but a pull_request event; must not count.
        {
            "head_sha": sha,
            "event": "pull_request",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
        },
        # Same CI workflow but a different SHA; must not count.
        {
            "head_sha": "d" * 40,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
        },
    ]
    _fake_github_response(
        monkeypatch,
        {
            f"/repos/owner/repo/actions/runs"
            f"?head_sha={sha}&branch=main&event=push&per_page=100": {
                "workflow_runs": runs,
            }
        },
    )
    client = GithubRestClient(token="gh-token")
    assert client.workflow_runs("owner/repo", sha) == ((4, 1, "completed", "failure"),)


def test_github_rest_client_search_is_repository_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    fingerprint = "f" * 64
    marker = defect_fingerprint_marker(fingerprint)
    # No state qualifier: the Search Issues API includes open and closed
    # issues alike, and ``state:open,closed`` is not a supported term.
    scoped = f"{marker} repo:owner/repo"
    _fake_github_response(
        monkeypatch,
        {
            f"/search/issues?q={urllib_parse.quote(scoped, safe='')}&per_page=100": {
                "total_count": 2,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 11,
                        "state": "open",
                        "body": f"still open\n{marker}\n",
                        "repository_url": "https://api.github.com/repos/owner/repo",
                    },
                    {
                        "number": 12,
                        "state": "closed",
                        "body": f"closed earlier\n{marker}\n",
                        "repository_url": "https://api.github.com/repos/owner/repo",
                    },
                ]
            }
        },
    )
    client = GithubRestClient(token="gh-token")
    results = client.search_issues("owner/repo", marker)
    # Open and closed issues in the target repository both count as filed.
    assert [item["number"] for item in results] == [11, 12]
    for item in results:
        assert has_defect_fingerprint(str(item["body"]), fingerprint)


def test_github_rest_client_search_drops_other_repository_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    fingerprint = "f" * 64
    marker = defect_fingerprint_marker(fingerprint)
    scoped = f"{marker} repo:owner/repo"
    _fake_github_response(
        monkeypatch,
        {
            f"/search/issues?q={urllib_parse.quote(scoped, safe='')}&per_page=100": {
                # total_count counts every returned item before repository
                # filtering, not only the target repository's matches.
                "total_count": 2,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 11,
                        "body": f"other repository\n{marker}\n",
                        "repository_url": "https://api.github.com/repos/other/repo",
                    },
                    {
                        "number": 12,
                        "body": f"target repository\n{marker}\n",
                        "repository_url": "https://api.github.com/repos/owner/repo",
                    },
                ]
            }
        },
    )
    client = GithubRestClient(token="gh-token")
    results = client.search_issues("owner/repo", marker)
    # A matching fingerprint in another repository can never satisfy this
    # repository's deduplication.
    assert [item["number"] for item in results] == [12]
    assert has_defect_fingerprint(str(results[0]["body"]), fingerprint)


@pytest.mark.parametrize(
    ("items"),
    [
        # Item without any repository identity cannot be attributed.
        [{"number": 12, "body": "unattributable"}],
        # Repository identity that is not a well-formed repository URL.
        [
            {
                "number": 12,
                "body": "malformed identity",
                "repository_url": "not-a-url",
            }
        ],
        # Non-mapping item in the page.
        ["not-a-mapping"],
        # Target-repository item missing a usable issue number.
        [
            {
                "number": "12",
                "body": "bad number",
                "repository_url": "https://api.github.com/repos/owner/repo",
            }
        ],
    ],
)
def test_github_rest_client_search_rejects_unsafe_items(
    monkeypatch: pytest.MonkeyPatch, items: object
) -> None:
    from aflow.concierge import GithubRestClient

    marker = "m" + "f" * 63
    scoped = f"{marker} repo:owner/repo"
    _fake_github_response(
        monkeypatch,
        {
            f"/search/issues?q={urllib_parse.quote(scoped, safe='')}&per_page=100": {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        },
    )
    client = GithubRestClient(token="gh-token")
    # A malformed or incomplete search response must surface as an error so
    # the caller stays report-only instead of filing a duplicate.
    with pytest.raises(ConciergeError) as excinfo:
        client.search_issues("owner/repo", marker)
    assert excinfo.value.reason == "github_search_invalid"


@pytest.mark.parametrize(
    ("payload"),
    [
        # GitHub explicitly reports a partial result with no items.
        {"items": [], "total_count": 0, "incomplete_results": True},
        # A partial result can still carry nonmatching items.
        {
            "items": [
                {
                    "number": 12,
                    "body": "no fingerprint",
                    "repository_url": "https://api.github.com/repos/owner/repo",
                }
            ],
            "total_count": 1,
            "incomplete_results": True,
        },
        # More reported matches than the returned first page.
        {"items": [], "total_count": 101, "incomplete_results": False},
        # Missing completeness fields.
        {"items": []},
        {"items": [], "total_count": 0},
        {"items": [], "incomplete_results": False},
        # Malformed completeness fields.
        {"items": [], "total_count": "0", "incomplete_results": False},
        {"items": [], "total_count": -1, "incomplete_results": False},
        {"items": [], "total_count": True, "incomplete_results": False},
        {"items": [], "total_count": 0, "incomplete_results": "false"},
        {"items": [], "total_count": 0, "incomplete_results": 0},
    ],
)
def test_github_rest_client_search_requires_complete_envelope(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    from aflow.concierge import GithubRestClient

    marker = "m" + "f" * 63
    scoped = f"{marker} repo:owner/repo"
    _fake_github_response(
        monkeypatch,
        {
            f"/search/issues?q={urllib_parse.quote(scoped, safe='')}&per_page=100": payload
        },
    )
    client = GithubRestClient(token="gh-token")
    # A partial, paginated, or malformed search cannot prove absence, so it
    # must surface as an error and keep the caller report-only.
    with pytest.raises(ConciergeError) as excinfo:
        client.search_issues("owner/repo", marker)
    assert excinfo.value.reason == "github_search_invalid"


def test_github_rest_client_search_accepts_complete_empty_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    marker = "m" + "f" * 63
    scoped = f"{marker} repo:owner/repo"
    _fake_github_response(
        monkeypatch,
        {
            f"/search/issues?q={urllib_parse.quote(scoped, safe='')}&per_page=100": {
                "total_count": 0,
                "incomplete_results": False,
                "items": [],
            }
        },
    )
    client = GithubRestClient(token="gh-token")
    # A complete empty search is sufficient negative evidence.
    assert client.search_issues("owner/repo", marker) == ()


def test_live_search_retains_filed_issue_and_blocks_duplicate_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aflow.concierge import GithubRestClient

    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run()],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    evidence = classify_defect(_confirmed_run())
    assert evidence is not None
    fingerprint = defect_fingerprint(evidence)
    marker = defect_fingerprint_marker(fingerprint)
    search_path = (
        f"/search/issues?q={urllib_parse.quote(f'{marker} repo:owner/repo', safe='')}"
        "&per_page=100"
    )
    paths: dict[str, object] = {
        "/repos/owner/repo": {"id": 42},
        "/repos/owner/repo/issues?state=open&per_page=100": [],
        search_path: {
            "total_count": 0,
            "incomplete_results": False,
            "items": [],
        },
        "/repos/owner/repo/issues": {
            "number": 101,
            "html_url": "https://github.com/owner/repo/issues/101",
        },
    }
    requests: list[tuple[str, bytes | None]] = []
    _fake_github_response(monkeypatch, paths, record=requests)
    client = GithubRestClient(token="gh-token")
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=client)
    first = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert first.action == "file_defect"
    assert first.reason == "defect_filed"
    posts = [entry for entry in requests if entry[0] == "POST"]
    assert len(posts) == 1
    created = json.loads(posts[0][1].decode("utf-8"))
    # The next tick sees the filed issue in the live Search Issues shape,
    # identified by repository_url, and must not file a duplicate.
    paths[search_path] = {
        "total_count": 1,
        "incomplete_results": False,
        "items": [
            {
                "number": 101,
                "state": "open",
                "title": str(created["title"]),
                "body": str(created["body"]),
                "repository_url": "https://api.github.com/repos/owner/repo",
            }
        ],
    }
    second = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    assert second.action == "resume"
    assert second.reason == "resumed"
    assert len([entry for entry in requests if entry[0] == "POST"]) == 1
    assert mcp.call_names().count("resume_run") == 1


@pytest.mark.parametrize(
    ("search_payload"),
    [
        # GitHub reports a partial result: the empty page proves nothing.
        {"items": [], "total_count": 0, "incomplete_results": True},
        # The count exceeds the first page: matches may be beyond it.
        {"items": [], "total_count": 101, "incomplete_results": False},
    ],
    ids=["incomplete_results", "count_beyond_first_page"],
)
def test_live_incomplete_search_keeps_tick_report_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    search_payload: object,
) -> None:
    from aflow.concierge import GithubRestClient

    mcp = FakeMcp(
        project_root=tmp_path,
        runs=[_confirmed_run()],
        plans=[{"path": "plans/in-progress/alpha.md", "status": "in_progress"}],
        documents={"plans/in-progress/alpha.md": "# Plan\n"},
    )
    evidence = classify_defect(_confirmed_run())
    assert evidence is not None
    fingerprint = defect_fingerprint(evidence)
    marker = defect_fingerprint_marker(fingerprint)
    search_path = (
        f"/search/issues?q={urllib_parse.quote(f'{marker} repo:owner/repo', safe='')}"
        "&per_page=100"
    )
    requests: list[tuple[str, bytes | None]] = []
    _fake_github_response(
        monkeypatch,
        {
            "/repos/owner/repo": {"id": 42},
            "/repos/owner/repo/issues?state=open&per_page=100": [],
            search_path: search_payload,
            "/repos/owner/repo/issues": {
                "number": 101,
                "html_url": "https://github.com/owner/repo/issues/101",
            },
        },
        record=requests,
    )
    client = GithubRestClient(token="gh-token")
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, github=client)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    # An incomplete search is uncertainty, not absence: no issue may be
    # created and the tick stays report-only.
    assert outcome.action == "report"
    assert outcome.reason == "defect_dedup_unavailable"
    assert outcome.mutating is False
    assert [entry for entry in requests if entry[0] == "POST"] == []


def test_github_rest_client_search_rejects_unsafe_full_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    client = GithubRestClient(token="gh-token")
    for full_name in (
        "owner",
        "owner/repo repo:other/repo",
        "../escape",
        "owner/repo/extra",
    ):
        with pytest.raises(ConciergeError) as excinfo:
            client.search_issues(full_name, "a" * 64)
        assert excinfo.value.reason == "github_full_name_invalid"


def test_local_delivery_gate_ci_uses_only_qualifying_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    sha = "e" * 40
    runs = [
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "run_number": 3,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
        },
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/unrelated.yml",
            "status": "completed",
            "conclusion": "failure",
        },
    ]
    _fake_github_response(
        monkeypatch,
        {
            f"/repos/owner/repo/actions/runs"
            f"?head_sha={sha}&branch=main&event=push&per_page=100": {
                "workflow_runs": runs,
            }
        },
    )
    gate = LocalDeliveryGate(
        github=GithubRestClient(token="gh-token"),
        project_root=tmp_path,
        deploy_status_path=tmp_path / "absent.json",
        live_release_root=tmp_path / "absent-live",
    )
    gate._origin_main_sha = lambda: sha  # type: ignore[method-assign]
    status = gate.evaluate(full_name="owner/repo")
    # The unrelated workflow's failure must not red the qualifying green CI.
    assert status.ci == "green"


def test_local_delivery_gate_ci_pending_when_only_unrelated_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    sha = "a1" * 20
    runs = [
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "run_number": 5,
            "run_attempt": 1,
            "status": "in_progress",
            "conclusion": None,
        },
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/unrelated.yml",
            "status": "completed",
            "conclusion": "success",
        },
    ]
    _fake_github_response(
        monkeypatch,
        {
            f"/repos/owner/repo/actions/runs"
            f"?head_sha={sha}&branch=main&event=push&per_page=100": {
                "workflow_runs": runs,
            }
        },
    )
    gate = LocalDeliveryGate(
        github=GithubRestClient(token="gh-token"),
        project_root=tmp_path,
        deploy_status_path=tmp_path / "absent.json",
        live_release_root=tmp_path / "absent-live",
    )
    gate._origin_main_sha = lambda: sha  # type: ignore[method-assign]
    status = gate.evaluate(full_name="owner/repo")
    # An unrelated success must not green a still-running qualifying CI.
    assert status.ci == "pending"


def _ci_run(
    *,
    sha: str,
    number: int,
    attempt: int = 1,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, object]:
    return {
        "head_sha": sha,
        "event": "push",
        "head_branch": "main",
        "path": ".github/workflows/ci.yml",
        "run_number": number,
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
    }


def _ci_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, object]]
) -> LocalDeliveryGate:
    from aflow.concierge import GithubRestClient

    sha = "e" * 40
    _fake_github_response(
        monkeypatch,
        {
            f"/repos/owner/repo/actions/runs"
            f"?head_sha={sha}&branch=main&event=push&per_page=100": {
                "workflow_runs": runs,
            }
        },
    )
    gate = LocalDeliveryGate(
        github=GithubRestClient(token="gh-token"),
        project_root=tmp_path,
        deploy_status_path=tmp_path / "absent.json",
        live_release_root=tmp_path / "absent-live",
    )
    gate._origin_main_sha = lambda: sha  # type: ignore[method-assign]
    return gate


def test_local_delivery_gate_ci_latest_run_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "e" * 40
    # An earlier success never greens a later failure.
    gate = _ci_gate(
        tmp_path,
        monkeypatch,
        [
            _ci_run(sha=sha, number=1, conclusion="success"),
            _ci_run(sha=sha, number=2, conclusion="failure"),
        ],
    )
    assert gate.evaluate(full_name="owner/repo").ci == "red"
    # An earlier failure never reds a later success.
    gate = _ci_gate(
        tmp_path,
        monkeypatch,
        [
            _ci_run(sha=sha, number=1, conclusion="failure"),
            _ci_run(sha=sha, number=2, conclusion="success"),
        ],
    )
    assert gate.evaluate(full_name="owner/repo").ci == "green"
    # A non-terminal latest run is pending even after an earlier failure.
    gate = _ci_gate(
        tmp_path,
        monkeypatch,
        [
            _ci_run(sha=sha, number=1, conclusion="failure"),
            _ci_run(sha=sha, number=2, status="in_progress", conclusion=None),
        ],
    )
    assert gate.evaluate(full_name="owner/repo").ci == "pending"


def test_local_delivery_gate_ci_rerun_attempt_overrides_earlier_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "e" * 40
    # Same run number: the re-run (attempt 2) decides, not attempt 1.
    gate = _ci_gate(
        tmp_path,
        monkeypatch,
        [
            _ci_run(sha=sha, number=7, attempt=1, conclusion="success"),
            _ci_run(sha=sha, number=7, attempt=2, conclusion="cancelled"),
        ],
    )
    assert gate.evaluate(full_name="owner/repo").ci == "red"
    gate = _ci_gate(
        tmp_path,
        monkeypatch,
        [
            _ci_run(sha=sha, number=7, attempt=1, conclusion="failure"),
            _ci_run(sha=sha, number=7, attempt=2, conclusion="success"),
        ],
    )
    assert gate.evaluate(full_name="owner/repo").ci == "green"


def test_fresh_dispatch_uses_only_qualifying_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aflow.concierge import GithubRestClient

    sha = "f" * 40
    runs = [
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "run_number": 6,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
        },
        {
            "head_sha": sha,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/unrelated.yml",
            "status": "completed",
            "conclusion": "failure",
        },
    ]
    _fake_github_response(
        monkeypatch,
        {
            f"/repos/owner/repo/actions/runs"
            f"?head_sha={sha}&branch=main&event=push&per_page=100": {
                "workflow_runs": runs,
            }
        },
    )
    gate = LocalDeliveryGate(
        github=GithubRestClient(token="gh-token"),
        project_root=tmp_path,
        deploy_status_path=tmp_path / "absent.json",
        live_release_root=tmp_path / "absent-live",
    )
    gate._origin_main_sha = lambda: sha  # type: ignore[method-assign]
    mcp = FakeMcp(
        project_root=tmp_path,
        plans=[{"path": "plans/todo/alpha.md", "status": "todo"}],
        documents={"plans/todo/alpha.md": "# Plan\n"},
        publication=_publication_projection(available=True, remote="origin", branch="main"),
    )
    executor = _executor(tmp_path, monkeypatch, mcp=mcp, delivery_gate=gate)
    outcome = executor.execute(
        state_dir=tmp_path / "state", project_root=tmp_path, environment=ENVIRONMENT
    )
    # The unrelated workflow's failure must not block the fresh dispatch.
    assert outcome.action == "start"
    assert outcome.mutating is True

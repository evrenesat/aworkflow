"""Bounded, non-overlapping concierge tick for the p100 owner-issue relay.

Each tick owns one short-lived advisory Codex CLI process running
``gpt-6.1-sol`` at ``xhigh`` reasoning effort, configured for the local
AFlow MCP endpoint with environment-variable-backed bearer authentication.
The concierge process is the authoritative decision and execution boundary:
it reads the queue and plan documents through MCP, plans eligible
owner-authored issues with the read-only ``gpt-6-astra`` high-effort
planner, and performs at most one bounded MCP action per tick.  The tick
follows the packaged one-plan triage policy: it prefers a verified
resumable failed lineage, then one launchable plan, then the oldest
owner-authored issue that no plan document already covers.  It never edits
implementation code, never uses a non-MCP AFlow control channel, never
performs blind recovery, and never persists raw transcripts.  Only a
bounded, redacted status record is written.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import resources
import argparse
import asyncio
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Protocol
from urllib import parse as urllib_parse
from urllib import request as urllib_request

CONCIERGE_MODEL = "gpt-6.1-sol"
CONCIERGE_EFFORT = "xhigh"
CONCIERGE_MCP_NAME = "aflow"
CONCIERGE_MCP_URL = "http://127.0.0.1:8765/mcp"
CONCIERGE_TOKEN_ENV = "AFLOW_APP_TOKEN"
CONCIERGE_GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
CONCIERGE_STATUS_SCHEMA_VERSION = 2
TICK_INTERVAL_SECONDS = 20 * 60
DEFAULT_TIMEOUT_SECONDS = 15 * 60
DEFAULT_STATE_DIR = Path("/var/lib/aflowd/concierge")
DEFAULT_WORK_DIR = Path("/root/code")
CONCIERGE_PROJECT_ROOT_DEFAULT = Path("/root/code/agent-flow")
GITHUB_API_URL_DEFAULT = "https://api.github.com"
CONCIERGE_STREAM_MAX_BYTES = 2 * 1024 * 1024
CONCIERGE_STATUS_TAIL_MAX_CHARS = 4096
CONCIERGE_PROMPT_RESOURCE = "concierge_prompt.md"
CONCIERGE_PROMPT_MAX_BYTES = 32 * 1024
CONCIERGE_OWNER_ID = 591691
CONCIERGE_PLANNER_MODEL = "gpt-6-astra"
CONCIERGE_PLANNER_EFFORT = "high"
CONCIERGE_WORKFLOW_NAME = "checkpoint_delivery"
CONCIERGE_TEAM = "xtx-mtp"
REQUIRED_WORKFLOW_SETUP = frozenset({"worktree", "branch"})
REQUIRED_WORKFLOW_TEARDOWN = frozenset({"merge", "rm_worktree"})
CONCIERGE_MCP_CALL_TIMEOUT_SECONDS = 60.0
CONCIERGE_GITHUB_TIMEOUT_SECONDS = 30.0
CONCIERGE_GITHUB_MAX_PAGES = 5
CONCIERGE_PAGE_LIMIT = 100
CONCIERGE_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CONCIERGE_GIT_REMOTE_TIMEOUT_SECONDS = 15.0
CONCIERGE_MAX_PAGES = 100
CONCIERGE_PLANNER_QUESTION_MAX_CHARS = 512
ACTIVE_RUN_ACTIVITY = "active"
UNCERTAIN_RUN_ACTIVITY = "unknown"
INACTIVE_RUN_ACTIVITY = "inactive"
TERMINAL_FAILED_RUN_STATUSES = frozenset({"failed", "stopped"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "interrupted", "owner_stopped"})
STARTUP_QUESTION_RUN_STATUS = "awaiting_startup_answer"
STARTUP_FAILED_RUN_CODE = "startup_failed"
OCCUPANCY_ACTIVE = "active"
OCCUPANCY_TERMINAL = "terminal"
OCCUPANCY_STARTUP_HOLD = "startup_hold"
OCCUPANCY_BLOCKED = "blocked"
LAUNCHABLE_PLAN_STATUSES = frozenset({"todo"})
CANONICAL_PLAN_STATUSES = frozenset(
    {"draft", "todo", "in_progress", "done", "failed", "needs_plan_change"}
)
PLAN_DOCUMENT_STATUSES = frozenset(
    {"todo", "in_progress", "done", "failed", "needs_plan_change"}
)
PLANNED_EVIDENCE_STATUSES = frozenset(
    {"todo", "in_progress", "done", "failed", "needs_plan_change"}
)
MUTATING_TICK_ACTIONS = frozenset({"resume", "start", "plan_and_start"})
DEFECT_FINGERPRINT_MARKER_PREFIX = "aflow-concierge-defect-fingerprint:"
DEPLOY_STATUS_PATH_DEFAULT = Path("/var/lib/aflowd/deploy/status.json")
LIVE_RELEASE_ROOT_DEFAULT = Path("/opt/aflowd")
DEPLOY_DELIVERED_PHASES = frozenset({"deployed", "up_to_date"})
TOKEN_MIN_LENGTH = 8
TOKEN_MAX_LENGTH = 1024
_TOKEN_SAFE_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_ISSUE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/issues/(\d+)"
)
_GITHUB_HTTPS_REMOTE_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
_GITHUB_SSH_REMOTE_RE = re.compile(
    r"^git@github\.com:([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
_GITHUB_FULL_NAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
_GITHUB_LINK_NEXT_RE = re.compile(r'\s*<([^>]+)>;\s*rel="next"')
_GITHUB_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PLAN_STATUS_BY_DIRECTORY = {
    "todo": "todo",
    "in-progress": "in_progress",
    "failed": "failed",
    "done": "done",
    "needs-plan-change": "needs_plan_change",
}


class ConciergeError(RuntimeError):
    """A bounded concierge failure with a safe, fixed reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TickDeferral(ConciergeError):
    """A tick that must defer because another tick owns the single slot."""


@dataclass(frozen=True)
class TickProcessResult:
    """Bounded process output kept out of durable records."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    overflowed: bool = False
    interrupted: bool = False


class ProcessRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        prompt: bytes,
        timeout_seconds: float,
    ) -> TickProcessResult: ...


@dataclass(frozen=True)
class TickObservation:
    """Bounded MCP/GitHub snapshot for one triage decision.

    ``runs`` entries are the MCP ``list_runs`` mappings.  ``plans`` is the
    joined complete-lifecycle inventory: one row per exact plan path with the
    canonical ``status``, ``revision``, and ``modified_at`` projected from
    ``list_plans`` plus ``list_plan_documents``/``read_plan``.  ``issues``
    entries carry ``number``, ``author_id``, ``full_name``, ``created_at``,
    and ``state``.  ``plan_documents`` maps every lifecycle plan path to its
    read document content; duplicate detection matches canonical source issue
    URLs inside that content.  ``occupancy`` carries the shared run-occupancy
    classification: ``blocks`` (bool), ``blocking_run_id``/
    ``blocking_reason`` (str), and ``startup_holds`` ((run_id, plan_path)
    pairs that may hold only their own plan).
    """

    project: Mapping[str, object]
    runs: tuple[Mapping[str, object], ...] = ()
    plans: tuple[Mapping[str, object], ...] = ()
    issues: tuple[Mapping[str, object], ...] = ()
    plan_documents: Mapping[str, str] = field(default_factory=dict)
    occupancy: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GithubIssuePage:
    """Bounded open-issue evidence for one repository.

    ``complete`` is False when the bounded page cap was reached with an
    unconsumed continuation, or a repeated page failed to advance the
    inventory; callers must report the gap instead of selecting from a
    partial inventory.
    """

    repository_id: int
    issues: tuple[Mapping[str, object], ...]
    complete: bool = True


@dataclass(frozen=True)
class ConciergeCanonicalIssue:
    """Canonical owner-issue text for one planner claim."""

    title: str
    body: str
    title_body_sha256: str


@dataclass(frozen=True)
class TickOutcome:
    """Bounded result of one authoritative tick decision and execution."""

    action: str
    reason: str
    mutating: bool = False
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DefectEvidence:
    """Bounded, sanitized identity for one confirmed AFlow engine defect.

    Every field comes from the strictly validated six-field
    ``defect_confirmation`` contract, so the evidence carries no exception
    text, host path, transcript, or raw log.
    """

    source: str
    signature: str
    subject: str
    component: str = ""
    site: str = ""


@dataclass(frozen=True)
class DeliveryEvidence:
    """Bounded raw exact-SHA delivery inputs for one projection."""

    origin_main_sha: str | None = None
    ci: str | None = None
    deploy_phase: str | None = None
    current_commit: str | None = None
    live_commit: str | None = None


@dataclass(frozen=True)
class DeliveryStatus:
    """Projected exact-SHA delivery state: overall, CI, and live release."""

    state: str
    ci: str
    live: str


class McpClient(Protocol):
    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class GithubClient(Protocol):
    def open_issues(self, full_name: str) -> GithubIssuePage: ...

    def workflow_runs(
        self, full_name: str, sha: str
    ) -> tuple[tuple[int, int, str, str], ...]: ...

    def search_issues(
        self, full_name: str, query: str
    ) -> tuple[Mapping[str, object], ...]: ...

    def create_issue(
        self, full_name: str, title: str, body: str
    ) -> Mapping[str, object]: ...


class DeliveryGate(Protocol):
    def evaluate(self, *, full_name: str | None) -> DeliveryStatus: ...


class TickExecutor(Protocol):
    def execute(
        self,
        *,
        state_dir: Path,
        project_root: Path,
        environment: Mapping[str, str],
    ) -> TickOutcome: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class TriageDecision:
    """The single bounded action (or non-action) chosen for one tick."""

    action: str
    reason: str
    run_id: str | None = None
    plan_path: str | None = None
    issue_number: int | None = None
    issue_url: str | None = None
    idempotency_key: str | None = None
    workflow_name: str | None = None
    team: str | None = None

    @property
    def mutating(self) -> bool:
        return self.action in MUTATING_TICK_ACTIONS


def read_bearer_token(environment: Mapping[str, str]) -> str:
    """Read and validate the local UI bearer from the current environment."""
    token = environment.get(CONCIERGE_TOKEN_ENV)
    if not isinstance(token, str) or not token:
        raise ConciergeError("bearer_token_missing")
    if any(character.isspace() for character in token):
        raise ConciergeError("bearer_token_unsafe")
    if not (TOKEN_MIN_LENGTH <= len(token) <= TOKEN_MAX_LENGTH):
        raise ConciergeError("bearer_token_unsafe")
    if not _TOKEN_SAFE_RE.fullmatch(token):
        raise ConciergeError("bearer_token_unsafe")
    return token


def build_codex_argv(*, work_dir: Path) -> tuple[str, ...]:
    """Return the reviewed, bounded Codex invocation contract.

    The bearer is referenced only by environment-variable name; the token
    value never appears in argv.  ``--skip-git-repo-check`` keeps the tick
    runnable from the configured parent directory, which is not required to
    be a Git repository; the tick itself stays read-only.
    """
    return (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--model",
        CONCIERGE_MODEL,
        "-c",
        f"model_reasoning_effort='{CONCIERGE_EFFORT}'",
        "-c",
        f"mcp_servers.{CONCIERGE_MCP_NAME}.url='{CONCIERGE_MCP_URL}'",
        "-c",
        f"mcp_servers.{CONCIERGE_MCP_NAME}.bearer_token_env_var='{CONCIERGE_TOKEN_ENV}'",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "-C",
        str(work_dir),
        "-",
    )


def load_tick_prompt() -> str:
    """Load the packaged one-plan triage tick prompt with bounded size."""
    try:
        text = (
            resources.files("aflow")
            .joinpath(CONCIERGE_PROMPT_RESOURCE)
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, AttributeError) as exc:
        raise ConciergeError("tick_prompt_unavailable") from exc
    if not text.strip() or "\x00" in text:
        raise ConciergeError("tick_prompt_invalid")
    if len(text.encode("utf-8")) > CONCIERGE_PROMPT_MAX_BYTES:
        raise ConciergeError("tick_prompt_too_large")
    return text


def build_tick_prompt() -> str:
    """Build the bounded one-plan triage tick prompt."""
    return load_tick_prompt()


def _start_key(identity: str) -> str:
    """Derive the stable lifecycle start key for one logical action."""
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"concierge-start-{digest[:16]}"


def _run_activity(run: Mapping[str, object]) -> str:
    value = run.get("activity")
    if value in (ACTIVE_RUN_ACTIVITY, INACTIVE_RUN_ACTIVITY, UNCERTAIN_RUN_ACTIVITY):
        return str(value)
    return UNCERTAIN_RUN_ACTIVITY


def classify_run_occupancy(run: Mapping[str, object]) -> str:
    """Classify one canonical run row for dispatch occupancy.

    Any active activity, unit, or preparation evidence is ``OCCUPANCY_ACTIVE``
    and blocks all dispatch.  Canonical terminal statuses with a canonical
    ``unknown`` or ``inactive`` activity and no conflicting active evidence
    are ``OCCUPANCY_TERMINAL`` history and never grant a resume; a terminal
    row whose activity is missing or malformed is not provably historical and
    stays ``OCCUPANCY_BLOCKED`` until fresh canonical detail resolves it.
    A pre-execution startup question or startup failure may be
    ``OCCUPANCY_STARTUP_HOLD``, holding only its own plan once the shared
    occupancy report verifies it.  Everything else is ``OCCUPANCY_BLOCKED``.
    """
    evidence = run.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if (
        _run_activity(run) == ACTIVE_RUN_ACTIVITY
        or evidence.get("unit_active") is True
        or evidence.get("preparation_active") is True
    ):
        return OCCUPANCY_ACTIVE
    status = run.get("status")
    if isinstance(status, str) and status in TERMINAL_RUN_STATUSES:
        if run.get("activity") in (
            UNCERTAIN_RUN_ACTIVITY,
            INACTIVE_RUN_ACTIVITY,
        ):
            return OCCUPANCY_TERMINAL
        return OCCUPANCY_BLOCKED
    if status == STARTUP_QUESTION_RUN_STATUS or (
        status == "needs_attention"
        and run.get("status_reason_code") == STARTUP_FAILED_RUN_CODE
    ):
        return OCCUPANCY_STARTUP_HOLD
    return OCCUPANCY_BLOCKED


def _occupancy_from_runs(
    runs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Derive a conservative occupancy summary from raw list rows.

    Unlike the executor's report, this cannot refresh a row through
    ``get_run`` or check queue capacity, so a pre-execution startup hold is
    never verified here and any non-terminal, non-active row conservatively
    blocks the tick.  The executor always supplies a verified report; this
    fallback keeps the pure triage decision consistent with the classifier.
    """
    blocking_run_id: str | None = None
    blocking_reason: str | None = None
    for run in runs:
        classification = classify_run_occupancy(run)
        if classification == OCCUPANCY_ACTIVE:
            return {
                "blocks": True,
                "blocking_run_id": str(run.get("run_id")),
                "blocking_reason": OCCUPANCY_ACTIVE,
                "startup_holds": [],
            }
        if classification == OCCUPANCY_TERMINAL:
            continue
        if blocking_run_id is None:
            blocking_run_id = str(run.get("run_id"))
            blocking_reason = OCCUPANCY_BLOCKED
    if blocking_run_id is None:
        return {
            "blocks": False,
            "blocking_run_id": None,
            "blocking_reason": None,
            "startup_holds": [],
        }
    return {
        "blocks": True,
        "blocking_run_id": blocking_run_id,
        "blocking_reason": blocking_reason,
        "startup_holds": [],
    }


def classify_defect(run: Mapping[str, object]) -> DefectEvidence | None:
    """Return bounded defect evidence for one strictly validated confirmation.

    Only the live MCP ``get_run.defect_confirmation`` projection is trusted:
    the fixed six-field contract (schema version 1,
    ``engine_internal_assertion``, ``controller``, a bounded package-relative
    component, a bounded site, and a 64-hex signature).  Missing, malformed,
    or unconfirmed data yields ``None`` so nothing is filed.  Free-text
    failure reasons, worker receipts, and evidence are never consulted.
    """
    from aflow.runlog import _validated_defect_confirmation

    confirmation = run.get("defect_confirmation")
    try:
        validated = _validated_defect_confirmation(confirmation)
    except ValueError:
        return None
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    return DefectEvidence(
        source=str(validated["source"]),
        signature=str(validated["signature"]),
        subject=run_id,
        component=str(validated["component"]),
        site=str(validated["site"]),
    )


def defect_signatures(
    runs: Sequence[Mapping[str, object]],
) -> tuple[DefectEvidence, ...]:
    """Return the distinct confirmed defects across the observed runs.

    Runs are visited in stable run-ID order.  Each strictly validated
    confirmation contributes at most one entry, keyed by its fingerprint, so
    the same signature across run IDs collapses to the first observed
    subject while every distinct signature is preserved.  Malformed or
    unconfirmed runs yield nothing.
    """
    seen: set[str] = set()
    evidence: list[DefectEvidence] = []
    for run in sorted(runs, key=lambda item: str(item.get("run_id") or "")):
        item = classify_defect(run)
        if item is None:
            continue
        fingerprint = defect_fingerprint(item)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        evidence.append(item)
    return tuple(evidence)


def defect_evidence(
    runs: Sequence[Mapping[str, object]],
) -> DefectEvidence | None:
    """Return the first confirmed defect across the observed runs, if any."""
    signatures = defect_signatures(runs)
    return signatures[0] if signatures else None


def defect_fingerprint(evidence: DefectEvidence) -> str:
    """Stable fingerprint for one bounded defect signature, not run instance."""
    payload = "\x00".join((evidence.source, evidence.signature))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def defect_fingerprint_marker(fingerprint: str) -> str:
    return f"{DEFECT_FINGERPRINT_MARKER_PREFIX}{fingerprint}"


def has_defect_fingerprint(body: str, fingerprint: str) -> bool:
    return defect_fingerprint_marker(fingerprint) in body


def defect_issue_title(evidence: DefectEvidence) -> str:
    return (
        f"AFlow concierge defect: {evidence.component}:{evidence.site} "
        f"(run {evidence.subject})"
    )


def defect_issue_body(evidence: DefectEvidence, fingerprint: str) -> str:
    return (
        "Bounded AFlow engine defect observed by the p100 owner-issue "
        "concierge. The confirmation came from the control plane's "
        "validated `defect_confirmation` projection; no transcripts, "
        "credentials, stack traces, or raw logs are included.\n"
        "\n"
        f"- source: {evidence.source}\n"
        f"- component: {evidence.component}\n"
        f"- site: {evidence.site}\n"
        f"- signature: {evidence.signature}\n"
        f"- run: {evidence.subject}\n"
        "\n"
        f"{defect_fingerprint_marker(fingerprint)}\n"
    )


def project_delivery(evidence: DeliveryEvidence) -> DeliveryStatus:
    """Project bounded exact-SHA delivery inputs into overall/CI/live state.

    Missing evidence projects to pending, never success.  A red exact-SHA CI
    run or a failed deploy phase is a failed release; the live release is
    reported separately from CI.
    """
    ci = evidence.ci
    if ci not in ("green", "red", "pending"):
        ci = "pending"
    deployed = evidence.deploy_phase in DEPLOY_DELIVERED_PHASES
    if not deployed or evidence.live_commit is None:
        live = "missing"
    elif evidence.origin_main_sha is None:
        live = "installed"
    elif evidence.live_commit == evidence.origin_main_sha:
        live = "installed"
    else:
        live = "stale"
    if ci == "red" or evidence.deploy_phase == "failed":
        state = "failed"
    elif ci == "green" and deployed and live == "installed":
        state = "ok"
    else:
        state = "pending"
    return DeliveryStatus(state=state, ci=ci, live=live)


def _validate_observation(observation: TickObservation) -> None:
    project = observation.project
    if not isinstance(project, Mapping) or not isinstance(
        project.get("project_id"), str
    ) or not project.get("project_id"):
        raise ConciergeError("observation_project_invalid")
    for run in observation.runs:
        if (
            not isinstance(run, Mapping)
            or not isinstance(run.get("run_id"), str)
            or not run.get("run_id")
        ):
            raise ConciergeError("observation_run_invalid")
    for plan in observation.plans:
        if (
            not isinstance(plan, Mapping)
            or not isinstance(plan.get("path"), str)
            or not plan.get("path")
            or not isinstance(plan.get("status"), str)
            or not plan.get("status")
        ):
            raise ConciergeError("observation_plan_invalid")
    occupancy = observation.occupancy
    if not isinstance(occupancy, Mapping):
        raise ConciergeError("observation_occupancy_invalid")
    if occupancy.get("blocks") is not None and not isinstance(
        occupancy.get("blocks"), bool
    ):
        raise ConciergeError("observation_occupancy_invalid")
    blocking_run_id = occupancy.get("blocking_run_id")
    if blocking_run_id is not None and (
        not isinstance(blocking_run_id, str) or not blocking_run_id
    ):
        raise ConciergeError("observation_occupancy_invalid")
    blocking_reason = occupancy.get("blocking_reason")
    if blocking_reason is not None and (
        not isinstance(blocking_reason, str) or not blocking_reason
    ):
        raise ConciergeError("observation_occupancy_invalid")
    startup_holds = occupancy.get("startup_holds")
    if startup_holds is not None:
        if not isinstance(startup_holds, (list, tuple)):
            raise ConciergeError("observation_occupancy_invalid")
        for hold in startup_holds:
            if (
                not isinstance(hold, (list, tuple))
                or len(hold) != 2
                or not isinstance(hold[0], str)
                or not hold[0]
                or (
                    hold[1] is not None
                    and (not isinstance(hold[1], str) or not hold[1])
                )
            ):
                raise ConciergeError("observation_occupancy_invalid")
    for issue in observation.issues:
        if (
            not isinstance(issue, Mapping)
            or type(issue.get("number")) is not int
            or type(issue.get("author_id")) is not int
            or not isinstance(issue.get("full_name"), str)
            or not issue.get("full_name")
            or not isinstance(issue.get("created_at"), str)
            or not issue.get("created_at")
        ):
            raise ConciergeError("observation_issue_invalid")
    documents = observation.plan_documents
    if not isinstance(documents, Mapping):
        raise ConciergeError("observation_plan_documents_invalid")
    for path, content in documents.items():
        if not isinstance(path, str) or not path or not isinstance(content, str):
            raise ConciergeError("observation_plan_documents_invalid")


def _canonical_plan_status(value: object) -> str:
    """Normalize one plan status spelling to the canonical underscore form."""
    if not isinstance(value, str):
        return ""
    return value.replace("-", "_")


def _issue_urls_in(text: str) -> frozenset[str]:
    """Extract canonical source issue URLs from one plan document."""
    return frozenset(
        f"https://github.com/{owner}/{repo}/issues/{number}"
        for owner, repo, number in _ISSUE_URL_RE.findall(text)
    )


def _issue_url(issue: Mapping[str, object]) -> str:
    return f"https://github.com/{issue['full_name']}/issues/{issue['number']}"


def repository_full_name(project_root: Path) -> str:
    """Derive the GitHub ``owner/repo`` identity from the origin remote."""
    try:
        completed = subprocess.run(
            ("git", "-C", str(project_root), "remote", "get-url", "origin"),
            capture_output=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConciergeError("repository_full_name_unavailable") from exc
    if completed.returncode != 0:
        raise ConciergeError("repository_full_name_unavailable")
    remote = completed.stdout.decode("utf-8", errors="replace").strip()
    for pattern in (_GITHUB_HTTPS_REMOTE_RE, _GITHUB_SSH_REMOTE_RE):
        match = pattern.fullmatch(remote)
        if match is not None:
            return f"{match.group(1)}/{match.group(2)}"
    raise ConciergeError("repository_full_name_unsupported")


def _lifecycle_array(value: object) -> tuple[str, ...] | None:
    """Validate one workflow lifecycle list of non-empty step names."""
    if not isinstance(value, (list, tuple)):
        return None
    for item in value:
        if not isinstance(item, str) or not item:
            return None
    return tuple(value)


def _resolve_workflow_lifecycle(
    workflows_toml: str, workflow_name: str
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Resolve a workflow's effective setup/teardown from the shared document.

    Mirrors the shared ``[workflow]`` defaults and ``extends`` inheritance so
    a listed workflow cannot hide a missing delivery boundary.  Returns
    ``None`` when the document, the workflow, or a lifecycle list is missing
    or malformed.
    """
    try:
        document = tomllib.loads(workflows_toml)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    if not isinstance(document, Mapping):
        return None
    root = document.get("workflow")
    if not isinstance(root, Mapping):
        return None
    default_setup = _lifecycle_array(root.get("setup"))
    default_teardown = _lifecycle_array(root.get("teardown"))
    if default_setup is None:
        default_setup = ()
    if default_teardown is None:
        default_teardown = ()
    resolved: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    resolving: set[str] = set()

    def resolve(name: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        if name in resolved:
            return resolved[name]
        if name in resolving:
            return None
        table = root.get(name)
        if not isinstance(table, Mapping):
            return None
        resolving.add(name)
        setup = _lifecycle_array(table.get("setup"))
        teardown = _lifecycle_array(table.get("teardown"))
        extends = table.get("extends")
        if extends is None:
            if setup is None:
                setup = default_setup
            if teardown is None:
                teardown = default_teardown
        else:
            if not isinstance(extends, str) or not extends:
                resolving.discard(name)
                return None
            base = resolve(extends)
            if base is None:
                resolving.discard(name)
                return None
            if setup is None:
                setup = base[0]
            if teardown is None:
                teardown = base[1]
        lifecycle = (setup, teardown)
        resolved[name] = lifecycle
        resolving.discard(name)
        return lifecycle

    return resolve(workflow_name)


def _publication_settings_evidence(
    capabilities: Mapping[str, object],
) -> tuple[str, str] | None:
    """Return the exact origin/main publication grant from the capabilities read.

    Returns None when the project-scoped projection is unavailable or does not
    grant exactly aflow.publishRemote=origin and aflow.publishBranch=main.
    """
    publication = capabilities.get("publication")
    if not isinstance(publication, Mapping) or publication.get("available") is not True:
        return None
    remote = publication.get("publish_remote")
    branch = publication.get("publish_branch")
    if remote != "origin" or branch != "main":
        return None
    return remote, branch


def triage_tick(observation: TickObservation) -> TriageDecision:
    """Choose at most one bounded action for the observed queue state.

    Priority order: defer on the shared run-occupancy classification, resume
    a verified inactive failed lineage, start exactly one launchable plan
    not held by a verified pre-execution startup hold, then plan and start
    the oldest eligible owner issue.  Ambiguity reports without a guessed
    action.
    """
    _validate_observation(observation)
    runs = observation.runs
    occupancy = (
        observation.occupancy
        if observation.occupancy
        else _occupancy_from_runs(runs)
    )

    if occupancy.get("blocks") is True:
        blocking_run_id = occupancy.get("blocking_run_id")
        reason = (
            "active_run_present"
            if occupancy.get("blocking_reason") == OCCUPANCY_ACTIVE
            else "uncertain_run_present"
        )
        return TriageDecision(
            action="defer",
            reason=reason,
            run_id=(
                str(blocking_run_id)
                if isinstance(blocking_run_id, str) and blocking_run_id
                else None
            ),
        )

    plans = observation.plans
    plan_paths = {plan.get("path") for plan in plans}
    held_paths = {
        path
        for _run_id, path in occupancy.get("startup_holds", ())
        if isinstance(path, str) and path
    }

    failed = [
        run
        for run in runs
        if _run_activity(run) == INACTIVE_RUN_ACTIVITY
        and run.get("status") in TERMINAL_FAILED_RUN_STATUSES
    ]
    if failed:
        for run in sorted(failed, key=lambda item: str(item["run_id"])):
            path = run.get("plan_path")
            if not (isinstance(path, str) and path):
                return TriageDecision(
                    action="report",
                    reason="failed_lineage_missing_plan",
                    run_id=str(run["run_id"]),
                )
        by_plan: dict[str, int] = {}
        for run in failed:
            by_plan[str(run["plan_path"])] = by_plan.get(str(run["plan_path"]), 0) + 1
        ambiguous = sorted(
            path for path, count in by_plan.items() if count > 1
        )
        if ambiguous:
            return TriageDecision(
                action="report",
                reason="ambiguous_predecessor_ownership",
                plan_path=ambiguous[0],
            )
        run = sorted(failed, key=lambda item: str(item["run_id"]))[0]
        path = str(run["plan_path"])
        if path not in plan_paths:
            return TriageDecision(
                action="report",
                reason="failed_lineage_plan_unregistered",
                run_id=str(run["run_id"]),
                plan_path=path,
            )
        competing = [
            other
            for other in runs
            if other.get("plan_path") == path
            and other.get("run_id") != run.get("run_id")
        ]
        if competing:
            return TriageDecision(
                action="report",
                reason="competing_run_ownership",
                run_id=str(run["run_id"]),
                plan_path=path,
            )
        return TriageDecision(
            action="resume",
            reason="failed_lineage_resumable",
            run_id=str(run["run_id"]),
            plan_path=path,
            idempotency_key=f"concierge-resume-{run['run_id']}",
        )

    referenced = {run.get("plan_path") for run in runs}
    launchable = [
        plan
        for plan in plans
        if plan.get("status") in LAUNCHABLE_PLAN_STATUSES
        and plan.get("path") not in referenced
        and plan.get("path") not in held_paths
    ]
    if len(launchable) > 1:
        return TriageDecision(
            action="report",
            reason="multiple_launchable_plans",
            plan_path=sorted(str(plan["path"]) for plan in launchable)[0],
        )
    if len(launchable) == 1:
        path = str(launchable[0]["path"])
        return TriageDecision(
            action="start",
            reason="single_launchable_plan",
            plan_path=path,
            idempotency_key=_start_key(path),
            workflow_name=CONCIERGE_WORKFLOW_NAME,
            team=CONCIERGE_TEAM,
        )

    owner_issues = [
        issue
        for issue in observation.issues
        if issue.get("author_id") == CONCIERGE_OWNER_ID
        and issue.get("state", "open") == "open"
    ]
    if not owner_issues:
        return TriageDecision(action="idle", reason="no_eligible_work")
    planned_urls: set[str] = set()
    for plan in plans:
        if _canonical_plan_status(plan.get("status")) not in PLANNED_EVIDENCE_STATUSES:
            continue
        path = str(plan["path"])
        content = observation.plan_documents.get(path)
        if not isinstance(content, str):
            return TriageDecision(
                action="report",
                reason="plan_evidence_unavailable",
                plan_path=path,
            )
        planned_urls.update(_issue_urls_in(content))
    unplanned = [issue for issue in owner_issues if _issue_url(issue) not in planned_urls]
    if not unplanned:
        return TriageDecision(action="idle", reason="no_eligible_work")
    issue = sorted(
        unplanned,
        key=lambda item: (str(item["created_at"]), int(item["number"])),
    )[0]
    number = int(issue["number"])
    full_name = str(issue["full_name"])
    return TriageDecision(
        action="plan_and_start",
        reason="oldest_owner_issue",
        issue_number=number,
        issue_url=f"https://github.com/{full_name}/issues/{number}",
        idempotency_key=_start_key(f"issue:{full_name}:{number}"),
        workflow_name=CONCIERGE_WORKFLOW_NAME,
        team=CONCIERGE_TEAM,
    )


class McpHttpEndpoint:
    """Minimal synchronous client for the local AFlow MCP endpoint.

    One dedicated event-loop thread owns the streamable-HTTP session; every
    ``call_tool`` is dispatched to it and bounded by a call timeout.  All
    failures collapse to bounded :class:`ConciergeError` reason codes.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        call_timeout: float = CONCIERGE_MCP_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        self._token = token
        self._call_timeout = call_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stack: Any = None
        self._lock = threading.Lock()

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        with self._lock:
            self._ensure_session()
            loop = self._loop
        if loop is None:
            raise ConciergeError("mcp_unavailable")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._call_tool(name, dict(arguments)), loop
            )
            return future.result(timeout=self._call_timeout)
        except ConciergeError:
            raise
        except Exception as exc:
            raise ConciergeError("mcp_call_failed") from exc

    def close(self) -> None:
        with self._lock:
            loop = self._loop
            if loop is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self._close_session(), loop
                ).result(timeout=10.0)
            except Exception:
                pass
            self._stop_loop()

    def _ensure_session(self) -> None:
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=self._run_loop, args=(loop,), name="aflow-concierge-mcp", daemon=True
        )
        self._loop = loop
        self._thread = thread
        thread.start()
        try:
            asyncio.run_coroutine_threadsafe(
                self._start_session(), loop
            ).result(timeout=self._call_timeout)
        except ConciergeError:
            self._stop_loop()
            raise
        except Exception as exc:
            self._stop_loop()
            raise ConciergeError("mcp_unavailable") from exc

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _start_session(self) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(
                    self._url,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            )
            session = ClientSession(read, write)
            await stack.enter_async_context(session)
            await session.initialize()
        except Exception as exc:
            await stack.aclose()
            raise ConciergeError("mcp_unavailable") from exc
        self._stack = stack
        self._session = session

    async def _call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> Mapping[str, object]:
        session = self._session
        if session is None:
            raise ConciergeError("mcp_unavailable")
        try:
            result = await session.call_tool(name, arguments)
        except Exception as exc:
            raise ConciergeError("mcp_call_failed") from exc
        if getattr(result, "isError", False):
            raise ConciergeError("mcp_tool_rejected")
        contents = list(getattr(result, "content", ()) or ())
        if len(contents) != 1:
            raise ConciergeError("mcp_tool_invalid_payload")
        text = getattr(contents[0], "text", None)
        if not isinstance(text, str):
            raise ConciergeError("mcp_tool_invalid_payload")
        try:
            payload = json.loads(text)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ConciergeError("mcp_tool_invalid_payload") from exc
        if not isinstance(payload, Mapping):
            raise ConciergeError("mcp_tool_invalid_payload")
        return payload

    async def _close_session(self) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    def _stop_loop(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout=5.0)
            try:
                loop.close()
            except Exception:
                pass


class GithubRestClient:
    """Bounded read-only GitHub evidence client for open issues."""

    def __init__(
        self,
        *,
        api_url: str = GITHUB_API_URL_DEFAULT,
        token: str | None = None,
        timeout_seconds: float = CONCIERGE_GITHUB_TIMEOUT_SECONDS,
        max_pages: int = CONCIERGE_GITHUB_MAX_PAGES,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._max_pages = max_pages

    def open_issues(self, full_name: str) -> GithubIssuePage:
        quoted = urllib_parse.quote(full_name, safe="/")
        repository, _ = self._request(f"/repos/{quoted}")
        repository_id = repository.get("id") if isinstance(repository, Mapping) else None
        if type(repository_id) is not int or repository_id <= 0:
            raise ConciergeError("github_repository_invalid")
        issues: list[Mapping[str, object]] = []
        seen_numbers: set[int] = set()
        complete = True
        path = f"/repos/{quoted}/issues?state=open&per_page={CONCIERGE_PAGE_LIMIT}"
        for _ in range(self._max_pages):
            payload, headers = self._request(path)
            if not isinstance(payload, list):
                raise ConciergeError("github_issues_invalid")
            fresh: list[Mapping[str, object]] = []
            for item in payload:
                issue = self._issue_mapping(item, full_name)
                if issue is None:
                    continue
                number = int(issue["number"])
                if number in seen_numbers:
                    continue
                seen_numbers.add(number)
                fresh.append(issue)
            path = self._next_path(headers)
            if path is not None and not fresh:
                # A repeated page failed to advance the inventory; the
                # remaining open issues are not covered by this read.
                complete = False
                break
            issues.extend(fresh)
            if path is None:
                break
        else:
            # The page cap was reached with an unconsumed continuation.
            complete = path is None
        return GithubIssuePage(
            repository_id=repository_id, issues=tuple(issues), complete=complete
        )

    def _issue_mapping(
        self, payload: object, full_name: str
    ) -> Mapping[str, object] | None:
        if not isinstance(payload, Mapping) or "pull_request" in payload:
            return None
        number = payload.get("number")
        user = payload.get("user")
        title = payload.get("title")
        body = payload.get("body")
        state = payload.get("state")
        created_at = payload.get("created_at")
        author_id = user.get("id") if isinstance(user, Mapping) else None
        if (
            type(number) is not int
            or number <= 0
            or type(author_id) is not int
            or author_id <= 0
            or not isinstance(title, str)
            or body is not None
            and not isinstance(body, str)
            or not isinstance(state, str)
            or not isinstance(created_at, str)
            or not created_at
        ):
            return None
        return {
            "number": number,
            "author_id": author_id,
            "full_name": full_name,
            "title": title,
            "body": body or "",
            "state": state,
            "created_at": created_at,
        }

    def _next_path(self, headers: Mapping[str, str]) -> str | None:
        link = headers.get("link")
        if not isinstance(link, str):
            return None
        match = _GITHUB_LINK_NEXT_RE.search(link)
        if match is None:
            return None
        url = match.group(1)
        if url.startswith(self._api_url):
            return url[len(self._api_url) :]
        return url

    def workflow_runs(
        self, full_name: str, sha: str
    ) -> tuple[tuple[int, int, str, str], ...]:
        # Only the deploy poller's qualifying CI workflow counts for delivery
        # evidence. Unrelated workflows on the same SHA must neither green a
        # failing CI run nor red a successful one, so the query is scoped to
        # the main push CI and filtered exactly like the poller's
        # ``_require_validated_ci``. Each result carries the run number and
        # attempt so the caller can apply the poller's latest-attempt
        # precedence.
        if not _GITHUB_SHA_RE.fullmatch(sha):
            raise ConciergeError("github_sha_invalid")
        quoted = urllib_parse.quote(full_name, safe="/")
        path = (
            f"/repos/{quoted}/actions/runs"
            f"?head_sha={sha}&branch=main&event=push"
            f"&per_page={CONCIERGE_PAGE_LIMIT}"
        )
        payload, _ = self._request(path)
        if not isinstance(payload, Mapping):
            raise ConciergeError("github_workflow_runs_invalid")
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            raise ConciergeError("github_workflow_runs_invalid")
        results: list[tuple[int, int, str, str]] = []
        for item in runs:
            if not isinstance(item, Mapping):
                continue
            if item.get("head_sha") != sha:
                continue
            if item.get("event") != "push":
                continue
            if item.get("head_branch") != "main":
                continue
            if item.get("path") != CONCIERGE_CI_WORKFLOW_PATH:
                continue
            number = item.get("run_number")
            if type(number) is not int or number <= 0:
                continue
            attempt = item.get("run_attempt")
            if type(attempt) is not int or attempt <= 0:
                continue
            status = item.get("status")
            if not isinstance(status, str):
                continue
            conclusion = item.get("conclusion")
            results.append(
                (
                    number,
                    attempt,
                    status,
                    conclusion if isinstance(conclusion, str) else "",
                )
            )
        return tuple(results)

    def search_issues(
        self, full_name: str, query: str
    ) -> tuple[Mapping[str, object], ...]:
        if not _GITHUB_FULL_NAME_RE.fullmatch(full_name):
            raise ConciergeError("github_full_name_invalid")
        if not isinstance(query, str) or not query:
            raise ConciergeError("github_search_query_invalid")
        # Deduplication is repository-local: the repo qualifier keeps a
        # matching fingerprint in another repository from suppressing the
        # target repository issue. GitHub's issue search returns open and
        # closed issues alike when no state qualifier is present, and the
        # combined ``state:open,closed`` qualifier is not a supported search
        # term (it silently matches nothing), so it must stay out of the query.
        scoped_query = f"{query} repo:{full_name}"
        quoted_query = urllib_parse.quote(scoped_query, safe="")
        path = f"/search/issues?q={quoted_query}&per_page={CONCIERGE_PAGE_LIMIT}"
        payload, _ = self._request(path)
        if not isinstance(payload, Mapping):
            raise ConciergeError("github_search_invalid")
        # An empty or partial page is not evidence of absence. The search is
        # sufficient negative evidence only when GitHub reports it complete:
        # ``incomplete_results`` must be exactly ``false``, ``total_count``
        # must be a nonnegative integer, and every reported match must be in
        # this page.  A partial, paginated, or malformed envelope raises so
        # the caller stays report-only instead of filing a duplicate.
        if payload.get("incomplete_results") is not False:
            raise ConciergeError("github_search_invalid")
        total_count = payload.get("total_count")
        if type(total_count) is not int or total_count < 0:
            raise ConciergeError("github_search_invalid")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ConciergeError("github_search_invalid")
        if total_count > len(items):
            raise ConciergeError("github_search_invalid")
        expected_repository_url = f"{self._api_url}/repos/{full_name}"
        repository_prefix = f"{self._api_url}/repos/"
        results: list[Mapping[str, object]] = []
        for item in items:
            # A search item that cannot be attributed to a repository means
            # deduplication cannot be established safely, so the caller must
            # not file; only a well-formed other-repository item is rejected.
            if not isinstance(item, Mapping):
                raise ConciergeError("github_search_invalid")
            repository_url = item.get("repository_url")
            if not isinstance(repository_url, str):
                raise ConciergeError("github_search_invalid")
            if repository_url != expected_repository_url:
                other_name = (
                    repository_url[len(repository_prefix) :]
                    if repository_url.startswith(repository_prefix)
                    else None
                )
                if other_name is None or not _GITHUB_FULL_NAME_RE.fullmatch(
                    other_name
                ):
                    raise ConciergeError("github_search_invalid")
                continue
            number = item.get("number")
            if type(number) is not int or number <= 0:
                raise ConciergeError("github_search_invalid")
            body = item.get("body")
            results.append(
                {
                    "number": number,
                    "body": body if isinstance(body, str) else "",
                }
            )
        return tuple(results)

    def create_issue(
        self, full_name: str, title: str, body: str
    ) -> Mapping[str, object]:
        if not isinstance(title, str) or not title:
            raise ConciergeError("github_issue_title_invalid")
        quoted = urllib_parse.quote(full_name, safe="/")
        payload, _ = self._request(
            f"/repos/{quoted}/issues",
            method="POST",
            payload={"title": title, "body": body if isinstance(body, str) else ""},
        )
        if not isinstance(payload, Mapping):
            raise ConciergeError("github_create_issue_invalid")
        number = payload.get("number")
        if type(number) is not int or number <= 0:
            raise ConciergeError("github_create_issue_invalid")
        html_url = payload.get("html_url")
        return {
            "number": number,
            "html_url": html_url if isinstance(html_url, str) else None,
        }

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, object] | None = None,
    ) -> tuple[object, Mapping[str, str]]:
        url = path if path.startswith("http") else self._api_url + path
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "aflow-concierge",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib_request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib_request.urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read(CONCIERGE_STREAM_MAX_BYTES + 1)
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
        except (OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
            raise ConciergeError("github_unavailable") from exc
        if len(body) > CONCIERGE_STREAM_MAX_BYTES:
            raise ConciergeError("github_response_too_large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ConciergeError("github_response_invalid") from exc
        return payload, response_headers


class LocalDeliveryGate:
    """Exact-SHA delivery evidence from git, deploy status, and live release.

    Every evidence source is best-effort and bounded: a missing or unreadable
    source collapses to absent evidence, which the projection reports as
    pending rather than success.
    """

    def __init__(
        self,
        *,
        github: GithubClient,
        project_root: Path,
        deploy_status_path: Path = DEPLOY_STATUS_PATH_DEFAULT,
        live_release_root: Path = LIVE_RELEASE_ROOT_DEFAULT,
    ) -> None:
        self._github = github
        self._project_root = project_root
        self._deploy_status_path = deploy_status_path
        self._live_release_root = live_release_root

    def evaluate(self, *, full_name: str | None) -> DeliveryStatus:
        return project_delivery(self._gather_evidence(full_name))

    def _gather_evidence(self, full_name: str | None) -> DeliveryEvidence:
        sha = self._origin_main_sha()
        phase, commit_sha = self._deploy_status()
        live_commit = self._live_commit()
        ci = self._ci_status(full_name, sha)
        return DeliveryEvidence(
            origin_main_sha=sha,
            ci=ci,
            deploy_phase=phase,
            current_commit=commit_sha,
            live_commit=live_commit,
        )

    def _origin_main_sha(self) -> str | None:
        # Resolve the remote's refs/heads/main directly. A local tracking ref
        # can lag behind when another worktree publishes origin/main, so the
        # stale ref must never stand in for the remote tip. ls-remote is a
        # bounded, read-only query: it neither fetches nor mutates shared refs.
        try:
            result = subprocess.run(
                ["git", "ls-remote", "origin", "refs/heads/main"],
                cwd=self._project_root,
                capture_output=True,
                text=True,
                timeout=CONCIERGE_GIT_REMOTE_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            sha, ref = parts
            if ref != "refs/heads/main":
                continue
            return sha if _GITHUB_SHA_RE.fullmatch(sha) else None
        return None

    def _deploy_status(self) -> tuple[str | None, str | None]:
        try:
            payload = json.loads(self._deploy_status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, None
        if not isinstance(payload, Mapping):
            return None, None
        phase = payload.get("phase")
        current = payload.get("current_commit")
        return (
            phase if isinstance(phase, str) else None,
            current if isinstance(current, str) else None,
        )

    def _live_commit(self) -> str | None:
        current = self._live_release_root / "current"
        try:
            release = current.resolve()
        except (OSError, RuntimeError):
            return None
        release_id = release.name
        return release_id if _GITHUB_SHA_RE.fullmatch(release_id) else None

    def _ci_status(self, full_name: str | None, sha: str | None) -> str | None:
        if full_name is None or sha is None:
            return None
        try:
            runs = self._github.workflow_runs(full_name, sha)
        except ConciergeError:
            return None
        if not runs:
            return "pending"
        # Match the deploy poller: the latest qualifying attempt by
        # (run_number, run_attempt) decides. An earlier success never greens
        # a later failure, and an earlier failure never reds a later success.
        latest = max(runs, key=lambda item: (item[0], item[1]))
        status, conclusion = latest[2], latest[3]
        if status != "completed":
            return "pending"
        if conclusion == "success":
            return "green"
        if conclusion in {"failure", "cancelled"}:
            return "red"
        return "pending"


class McpRegistryClient:
    """Serve the planner's project-registry lookup through MCP reads."""

    def __init__(self, mcp: McpClient) -> None:
        self._mcp = mcp

    def get_json(self, scope: str, path: str) -> Mapping[str, object]:
        if scope != "private" or path != "/api/control-plane/projects":
            raise ConciergeError("planner_registry_path_unsupported")
        return self._mcp.call_tool("list_projects", {})


def _build_concierge_planner(state_dir: Path, mcp: McpClient) -> Any:
    """Build the live tick planner: read-only, Astra, high, evidence-gated."""
    from aflow.issue_intake_planner import IssueIntakePlanner

    return IssueIntakePlanner(
        client=McpRegistryClient(mcp),
        state_root=state_dir / "planner",
        model=CONCIERGE_PLANNER_MODEL,
        effort=CONCIERGE_PLANNER_EFFORT,
        require_concierge_evidence=True,
    )


class ConciergeTickExecutor:
    """Authoritative concierge tick: MCP reads, triage, bounded execution."""

    def __init__(
        self,
        *,
        mcp: McpClient,
        github: GithubClient,
        planner_factory: Callable[[Path, McpClient], Any] | None = None,
        delivery_gate: DeliveryGate | None = None,
    ) -> None:
        self._mcp = mcp
        self._github = github
        self._planner_factory = planner_factory
        self._delivery_gate = delivery_gate

    def execute(
        self,
        *,
        state_dir: Path,
        project_root: Path,
        environment: Mapping[str, str],
    ) -> TickOutcome:
        project = self._resolve_project(project_root)
        if project is None:
            return TickOutcome("report", "project_not_registered")
        project_id = str(project["project_id"])
        full_name: str | None
        try:
            full_name = repository_full_name(project_root)
        except ConciergeError:
            full_name = None
        try:
            runs, plans, plan_documents = self._gather_evidence(project_id)
        except ConciergeError:
            return TickOutcome("report", "evidence_unavailable")
        issues: tuple[Mapping[str, object], ...] = ()
        github_gap = full_name is None
        if full_name is not None:
            try:
                page = self._github.open_issues(full_name)
            except ConciergeError:
                page = None
            if page is None or not page.complete:
                github_gap = True
            else:
                issues = page.issues
        occupancy = self._occupancy_report(project_id, runs)
        observation = TickObservation(
            project=project,
            runs=runs,
            plans=plans,
            issues=issues,
            plan_documents=plan_documents,
            occupancy=occupancy,
        )
        try:
            decision = triage_tick(observation)
        except ConciergeError as exc:
            return TickOutcome("report", exc.reason, details=self._decision_details(decision=None))
        if decision.action == "defer":
            return TickOutcome(
                decision.action, decision.reason,
                details=self._decision_details(decision=decision),
            )
        if github_gap:
            # Incomplete GitHub evidence is a global gap: report it before
            # defect filing, resume, start, or plan authoring, and never
            # select from partial issue rows.
            details = self._decision_details(decision=decision)
            details.update(
                self._delivery_details(self._delivery_status(full_name))
            )
            return TickOutcome(
                "report", "github_evidence_unavailable",
                details=details,
            )
        defects = defect_signatures(observation.runs)
        if defects:
            outcome = self._report_defects(full_name, defects)
            if outcome is not None:
                return outcome
        delivery = self._delivery_status(full_name)
        if not decision.mutating:
            details = self._decision_details(decision=decision)
            details.update(self._delivery_details(delivery))
            return TickOutcome(decision.action, decision.reason, details=details)
        if decision.action == "resume":
            return self._execute_resume(project_id, decision)
        if decision.action in ("start", "plan_and_start"):
            if delivery.state == "failed":
                details = self._decision_details(decision=decision)
                details.update(self._delivery_details(delivery))
                return TickOutcome("report", "delivery_gate_failed", details=details)
            if decision.action == "start":
                return self._execute_start(project_id, decision)
            return self._execute_plan_and_start(
                project_id, decision, state_dir, full_name
            )
        return TickOutcome("report", "unrecognized_decision")

    def close(self) -> None:
        self._mcp.close()

    def _delivery_status(self, full_name: str | None) -> DeliveryStatus:
        gate = self._delivery_gate
        if gate is None:
            return project_delivery(DeliveryEvidence())
        try:
            return gate.evaluate(full_name=full_name)
        except ConciergeError:
            return project_delivery(DeliveryEvidence())

    @staticmethod
    def _delivery_details(delivery: DeliveryStatus) -> dict[str, object]:
        return {
            "delivery_state": delivery.state,
            "delivery_ci": delivery.ci,
            "delivery_live": delivery.live,
        }

    def _report_defects(
        self, full_name: str, defects: tuple[DefectEvidence, ...]
    ) -> TickOutcome | None:
        # Process each distinct validated signature in stable order.  File the
        # first signature not already filed in this repository, then stop for
        # this tick (one issue mutation per tick).  A signature that is
        # already filed is skipped so a later distinct signature can still be
        # filed.  A failed search or filing remains report-only: never guess
        # that filing is safe when the GitHub result is uncertain.
        for defect in defects:
            fingerprint = defect_fingerprint(defect)
            marker = defect_fingerprint_marker(fingerprint)
            try:
                existing = self._github.search_issues(full_name, marker)
            except ConciergeError:
                return TickOutcome("report", "defect_dedup_unavailable")
            if any(
                isinstance(issue.get("body"), str)
                and has_defect_fingerprint(issue.get("body"), fingerprint)
                for issue in existing
            ):
                continue
            title = defect_issue_title(defect)
            body = defect_issue_body(defect, fingerprint)
            try:
                created = self._github.create_issue(full_name, title, body)
            except ConciergeError:
                return TickOutcome("report", "defect_filing_failed")
            number = created.get("number")
            url = created.get("html_url")
            return TickOutcome(
                "file_defect",
                "defect_filed",
                mutating=True,
                details={
                    "defect_fingerprint": fingerprint,
                    "defect_subject": defect.subject,
                    "issue_number": number if type(number) is int else None,
                    "issue_url": url if isinstance(url, str) else None,
                },
            )
        return None

    @staticmethod
    def _decision_details(
        *, decision: TriageDecision | None
    ) -> dict[str, object]:
        if decision is None:
            return {}
        details: dict[str, object] = {}
        if decision.run_id is not None:
            details["run_id"] = decision.run_id
        if decision.plan_path is not None:
            details["plan_path"] = decision.plan_path
        if decision.issue_number is not None:
            details["issue_number"] = decision.issue_number
        return details

    def _resolve_project(self, project_root: Path) -> Mapping[str, object] | None:
        try:
            payload = self._mcp.call_tool("list_projects", {})
        except ConciergeError:
            return None
        projects = payload.get("projects")
        if not isinstance(projects, list):
            return None
        try:
            target = project_root.resolve()
        except (OSError, RuntimeError):
            return None
        for item in projects:
            if not isinstance(item, Mapping) or not isinstance(
                item.get("project_id"), str
            ):
                continue
            root = item.get("root")
            if not isinstance(root, str) or not root:
                continue
            try:
                if Path(root).resolve() == target:
                    return item
            except (OSError, RuntimeError):
                continue
        return None

    def _gather_evidence(
        self, project_id: str
    ) -> tuple[
        tuple[Mapping[str, object], ...],
        tuple[Mapping[str, object], ...],
        dict[str, str],
    ]:
        """Gather the complete bounded inventory: runs, joined plan lifecycle
        inventory, and per-path document content.

        The plan inventory joins the lifecycle ``list_plans`` rows (which
        cover the draft/todo/in-progress/done directories) with
        ``list_plan_documents`` (which covers the full lifecycle including
        ``failed`` and ``needs_plan_change``) on the exact canonical path.
        Both sources must agree on status where they overlap; content comes
        from one ``read_plan`` per document.  Legacy ``draft`` rows have no
        document projection by design and are preserved as held inventory
        rows.  Any other gap raises a bounded :class:`ConciergeError` so the
        caller reports instead of acting on a partial inventory.
        """
        runs = self._list_runs(project_id)
        lifecycle_rows = self._list_plans(project_id)
        documents = self._plan_documents(project_id)
        for row in lifecycle_rows:
            if _canonical_plan_status(row.get("status")) == "draft":
                continue
            if str(row["path"]) not in documents:
                raise ConciergeError("plan_document_unavailable")
        inventory: dict[str, dict[str, str]] = {}
        for row in lifecycle_rows:
            path = str(row["path"])
            entry = inventory.setdefault(
                path,
                {"path": path, "status": "", "revision": "", "modified_at": ""},
            )
            entry["status"] = _canonical_plan_status(row.get("status"))
            entry["modified_at"] = str(row.get("modified_at") or "")
        for path in sorted(documents):
            document = documents[path]
            entry = inventory.setdefault(
                path,
                {"path": path, "status": "", "revision": "", "modified_at": ""},
            )
            if entry["status"] and entry["status"] != document["status"]:
                raise ConciergeError("plan_status_conflict")
            entry["status"] = document["status"]
            entry["revision"] = document["revision"]
        plans = tuple(inventory[path] for path in sorted(inventory))
        content_by_path = {
            path: documents[path]["content"] for path in documents
        }
        return runs, plans, content_by_path

    def _list_runs(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        """Page the complete run inventory through the opaque ``next_cursor``.

        Every row must carry a unique, non-empty run identity.  A page that
        is empty while a continuation is offered, repeats an already used
        cursor, or repeats a run identity is malformed; the page cap bounds
        the walk.  The final page carries no continuation.
        """
        runs: list[Mapping[str, object]] = []
        seen_ids: set[str] = set()
        used_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(CONCIERGE_MAX_PAGES):
            payload = self._mcp.call_tool(
                "list_runs",
                {"project_id": project_id, "limit": CONCIERGE_PAGE_LIMIT, "cursor": cursor},
            )
            if not isinstance(payload, Mapping):
                raise ConciergeError("run_page_invalid")
            page = payload.get("runs")
            if not isinstance(page, list):
                raise ConciergeError("run_page_invalid")
            for item in page:
                if not isinstance(item, Mapping):
                    raise ConciergeError("run_row_invalid")
                run_id = item.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise ConciergeError("run_row_invalid")
                if run_id in seen_ids:
                    raise ConciergeError("run_row_duplicate")
                seen_ids.add(run_id)
                runs.append(item)
            next_cursor = payload.get("next_cursor")
            if next_cursor is None:
                return tuple(runs)
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in used_cursors
            ):
                raise ConciergeError("run_page_invalid")
            used_cursors.add(next_cursor)
            if not page:
                raise ConciergeError("run_page_invalid")
            cursor = next_cursor
        raise ConciergeError("run_page_overflow")

    def _list_plans(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        """Page the lifecycle plan rows to the final empty page.

        The control plane answers ``list_plans`` with lexicographically
        ordered ``(path, status)`` rows and no continuation field, so the
        walk is complete only after an empty page.  Rows must carry a unique,
        non-empty path and a known status; a page whose first path does not
        advance past the cursor is malformed; the page cap bounds the walk.
        """
        plans: list[Mapping[str, object]] = []
        seen_paths: set[str] = set()
        cursor: str | None = None
        for _ in range(CONCIERGE_MAX_PAGES):
            payload = self._mcp.call_tool(
                "list_plans",
                {"project_id": project_id, "limit": CONCIERGE_PAGE_LIMIT, "cursor": cursor},
            )
            if not isinstance(payload, Mapping):
                raise ConciergeError("plan_page_invalid")
            page = payload.get("plans")
            if not isinstance(page, list):
                raise ConciergeError("plan_page_invalid")
            if not page:
                return tuple(plans)
            for item in page:
                if not isinstance(item, Mapping):
                    raise ConciergeError("plan_row_invalid")
                path = item.get("path")
                status = _canonical_plan_status(item.get("status"))
                if (
                    not isinstance(path, str)
                    or not path
                    or status not in CANONICAL_PLAN_STATUSES
                    or not isinstance(item.get("modified_at"), str)
                    or not item.get("modified_at")
                ):
                    raise ConciergeError("plan_row_invalid")
                if path in seen_paths:
                    raise ConciergeError("plan_row_duplicate")
                if cursor is not None and path <= cursor:
                    raise ConciergeError("plan_page_invalid")
                seen_paths.add(path)
                plans.append(item)
            last_path = str(page[-1]["path"])
            if len(page) < CONCIERGE_PAGE_LIMIT:
                # A short page is final only when the follow-up read confirms
                # it; fetch the terminating empty page.
                follow_up = self._mcp.call_tool(
                    "list_plans",
                    {
                        "project_id": project_id,
                        "limit": CONCIERGE_PAGE_LIMIT,
                        "cursor": last_path,
                    },
                )
                if (
                    not isinstance(follow_up, Mapping)
                    or follow_up.get("plans") != []
                ):
                    raise ConciergeError("plan_page_invalid")
                return tuple(plans)
            cursor = last_path
        raise ConciergeError("plan_page_overflow")

    def _plan_documents(self, project_id: str) -> dict[str, dict[str, str]]:
        """Read the full-lifecycle plan document coverage for one project.

        ``list_plan_documents`` supplies every lifecycle status (including
        ``failed`` and ``needs_plan_change``) with name, path, status, and
        revision; one ``read_plan`` per document supplies the content.
        Conflicting duplicates for one path are malformed.
        """
        try:
            payload = self._mcp.call_tool(
                "list_plan_documents", {"project_id": project_id}
            )
        except ConciergeError as exc:
            raise ConciergeError("plan_document_list_invalid") from exc
        if not isinstance(payload, Mapping):
            raise ConciergeError("plan_document_list_invalid")
        rows = payload.get("plans")
        if not isinstance(rows, list):
            raise ConciergeError("plan_document_list_invalid")
        documents: dict[str, dict[str, str]] = {}
        for item in rows:
            if not isinstance(item, Mapping):
                raise ConciergeError("plan_document_row_invalid")
            name = item.get("name")
            path = item.get("path")
            status = item.get("status")
            revision = item.get("revision")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(path, str)
                or not path
                or status not in PLAN_DOCUMENT_STATUSES
                or not isinstance(revision, str)
                or not revision
            ):
                raise ConciergeError("plan_document_row_invalid")
            existing = documents.get(path)
            if existing is not None:
                if (
                    existing["name"] != name
                    or existing["status"] != status
                    or existing["revision"] != revision
                ):
                    raise ConciergeError("plan_document_conflict")
                continue
            content = self._read_plan_content(project_id, str(status), name)
            documents[path] = {
                "name": name,
                "status": str(status),
                "revision": revision,
                "content": content,
            }
        return documents

    def _read_plan_content(
        self, project_id: str, plan_status: str, name: str
    ) -> str:
        try:
            payload = self._mcp.call_tool(
                "read_plan",
                {
                    "project_id": project_id,
                    "plan_status": plan_status,
                    "name": name,
                },
            )
        except ConciergeError as exc:
            raise ConciergeError("plan_document_unavailable") from exc
        if not isinstance(payload, Mapping):
            raise ConciergeError("plan_document_unavailable")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ConciergeError("plan_document_unavailable")
        path = payload.get("path")
        if isinstance(path, str) and path:
            parts = path.split("/")
            if len(parts) == 3 and parts[0] == "plans":
                expected = _PLAN_STATUS_BY_DIRECTORY.get(parts[1])
                if expected is not None and expected != plan_status:
                    raise ConciergeError("plan_document_conflict")
        return content

    @staticmethod
    def _preflight_admits_launch(preflight: object) -> bool:
        if not isinstance(preflight, Mapping):
            return False
        if preflight.get("execution_mode") != "new_worktree":
            return False
        blockers = preflight.get("blockers")
        if not isinstance(blockers, (list, tuple)) or blockers:
            return False
        return preflight.get("requires_confirmation") is False

    @staticmethod
    def _scheduling_keeps_single_slot(scheduling: Mapping[str, object]) -> bool:
        limit = scheduling.get("max_concurrent_implementations")
        if type(limit) is not int or limit != 1:
            return False
        return scheduling.get("auto_consume_plans") is False

    @staticmethod
    def _queue_permits_new_implementation(queue: object) -> bool:
        """Require the canonical queue to admit one new implementation now.

        The project must keep the exactly-one-slot setting with
        auto-consumption disabled and expose at least one available slot.
        """
        if not isinstance(queue, Mapping):
            return False
        settings = queue.get("settings")
        if not isinstance(settings, Mapping) or not (
            ConciergeTickExecutor._scheduling_keeps_single_slot(settings)
        ):
            return False
        capacity = queue.get("capacity")
        if not isinstance(capacity, Mapping):
            return False
        available = capacity.get("available_slots")
        if type(available) is not int or available < 1:
            return False
        return True

    @classmethod
    def _queue_admits_single_launch(cls, queue: object, plan_path: str) -> bool:
        if not cls._queue_permits_new_implementation(queue):
            return False
        plans = queue.get("plans")
        if not isinstance(plans, (list, tuple)):
            return False
        for plan in plans:
            if not isinstance(plan, Mapping):
                return False
            if plan.get("path") == plan_path and plan.get("run_id"):
                return False
        return True

    def _project_queue(self, project_id: str) -> Mapping[str, object] | None:
        try:
            payload = self._mcp.call_tool(
                "get_project_queue", {"project_id": project_id}
            )
        except ConciergeError:
            return None
        return payload if isinstance(payload, Mapping) else None

    def _run_detail(
        self, project_id: str, run_id: str
    ) -> Mapping[str, object] | None:
        try:
            payload = self._mcp.call_tool(
                "get_run", {"project_id": project_id, "run_id": run_id}
            )
        except ConciergeError:
            return None
        if not isinstance(payload, Mapping) or payload.get("run_id") != run_id:
            return None
        return payload

    def _startup_hold_verified(
        self, detail: Mapping[str, object], queue_permits: bool
    ) -> bool:
        """Verify one candidate-local startup hold from fresh canonical data.

        The run must still be pre-execution: manifest-only launch phase, no
        agent started, no observed unit, no active preparation, and the
        canonical queue must permit one new implementation.  Anything less
        certain keeps the run blocking.
        """
        if not queue_permits:
            return False
        if detail.get("launch_phase") != "manifest_only":
            return False
        evidence = detail.get("evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        if evidence.get("no_agent_started") is not True:
            return False
        if evidence.get("unit_observation") != "missing":
            return False
        if _run_activity(detail) != INACTIVE_RUN_ACTIVITY:
            return False
        if evidence.get("unit_active") is True:
            return False
        if evidence.get("preparation_active") is True:
            return False
        return True

    def _occupancy_report(
        self, project_id: str, runs: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]:
        """Produce the bounded shared run-occupancy report for one project.

        Every row is classified with :func:`classify_run_occupancy`.  Rows
        that are neither provably active nor terminal history are refreshed
        through the corresponding ``get_run``; a verified pre-execution
        startup question or startup failure holds only its own plan when the
        fresh detail carries a nonempty plan path that exactly matches the
        list row's plan path.  A missing or mismatched identity cannot
        establish which plan the startup record holds, so the run blocks all
        dispatch like every other uncertain row.  The report carries no
        mutations and bounds the reasons to fixed codes.
        """
        queue = self._project_queue(project_id)
        queue_permits = self._queue_permits_new_implementation(queue)
        blocks = False
        blocking_run_id: str | None = None
        blocking_reason: str | None = None
        holds: list[tuple[str, str]] = []

        def note_blocked(run_id: str, reason: str) -> None:
            nonlocal blocks, blocking_run_id, blocking_reason
            blocks = True
            if blocking_run_id is None:
                blocking_run_id, blocking_reason = run_id, reason

        for run in sorted(runs, key=lambda item: str(item.get("run_id") or "")):
            run_id = str(run.get("run_id") or "")
            classification = classify_run_occupancy(run)
            if classification == OCCUPANCY_ACTIVE:
                note_blocked(run_id, OCCUPANCY_ACTIVE)
                continue
            if classification == OCCUPANCY_TERMINAL:
                continue
            detail = self._run_detail(project_id, run_id)
            if detail is None:
                note_blocked(run_id, OCCUPANCY_BLOCKED)
                continue
            detail_classification = classify_run_occupancy(detail)
            if detail_classification == OCCUPANCY_ACTIVE:
                note_blocked(run_id, OCCUPANCY_ACTIVE)
                continue
            if detail_classification == OCCUPANCY_TERMINAL:
                continue
            if (
                detail_classification == OCCUPANCY_STARTUP_HOLD
                and self._startup_hold_verified(detail, queue_permits)
            ):
                list_path = run.get("plan_path")
                detail_path = detail.get("plan_path")
                if (
                    isinstance(list_path, str)
                    and list_path
                    and isinstance(detail_path, str)
                    and detail_path == list_path
                ):
                    holds.append(
                        (str(detail.get("run_id") or run_id), detail_path)
                    )
                    continue
            note_blocked(run_id, OCCUPANCY_BLOCKED)
        return {
            "blocks": blocks,
            "blocking_run_id": blocking_run_id,
            "blocking_reason": blocking_reason,
            "startup_holds": tuple(holds),
        }

    def _occupancy_blocks(
        self, project_id: str, runs: Sequence[Mapping[str, object]]
    ) -> bool:
        return (
            self._occupancy_report(project_id, runs).get("blocks") is True
        )

    def _verify_launch_evidence(
        self,
        project_id: str,
        plan_path: str,
        decision: TriageDecision,
    ) -> dict[str, object]:
        """Validate preflight and resolved launch settings before a start.

        Returns bounded verified details on success and raises a bounded
        :class:`ConciergeError` when evidence is missing or contradictory.
        """
        try:
            preflight = self._mcp.call_tool(
                "preflight_run",
                {
                    "project_id": project_id,
                    "plan_path": plan_path,
                    "workflow_name": decision.workflow_name,
                    "team": decision.team,
                },
            )
        except ConciergeError as exc:
            raise ConciergeError("launch_evidence_unavailable") from exc
        if not self._preflight_admits_launch(preflight):
            raise ConciergeError("launch_evidence_contradictory")
        workflow_name = decision.workflow_name
        team = decision.team
        if (
            not isinstance(workflow_name, str)
            or not workflow_name
            or not isinstance(team, str)
            or not team
        ):
            raise ConciergeError("launch_evidence_contradictory")
        try:
            capabilities = self._mcp.call_tool(
                "get_project_capabilities", {"project_id": project_id}
            )
            scheduling = self._mcp.call_tool(
                "get_project_scheduling", {"project_id": project_id}
            )
        except ConciergeError as exc:
            raise ConciergeError("launch_evidence_unavailable") from exc
        if not isinstance(capabilities, Mapping) or not isinstance(
            scheduling, Mapping
        ):
            raise ConciergeError("launch_evidence_contradictory")
        workflows = capabilities.get("workflows")
        teams = capabilities.get("teams")
        if (
            not isinstance(workflows, (list, tuple))
            or workflow_name not in workflows
            or not isinstance(teams, (list, tuple))
            or team not in teams
        ):
            raise ConciergeError("launch_evidence_contradictory")
        workflow_details = capabilities.get("workflow_details")
        detail = (
            workflow_details.get(workflow_name)
            if isinstance(workflow_details, Mapping)
            else None
        )
        executable_steps = (
            detail.get("executable_steps") if isinstance(detail, Mapping) else None
        )
        if (
            not isinstance(executable_steps, (list, tuple))
            or not executable_steps
        ):
            raise ConciergeError("launch_evidence_contradictory")
        if not self._scheduling_keeps_single_slot(scheduling):
            raise ConciergeError("launch_evidence_contradictory")
        try:
            config = self._mcp.call_tool("get_global_config", {})
        except ConciergeError as exc:
            raise ConciergeError("launch_evidence_unavailable") from exc
        if not isinstance(config, Mapping):
            raise ConciergeError("launch_evidence_contradictory")
        validation = config.get("validation")
        if (
            not isinstance(validation, Mapping)
            or validation.get("state") != "ready"
        ):
            raise ConciergeError("launch_evidence_contradictory")
        workflows_toml = config.get("workflows_toml")
        if not isinstance(workflows_toml, str) or not workflows_toml:
            raise ConciergeError("launch_evidence_contradictory")
        lifecycle = _resolve_workflow_lifecycle(workflows_toml, workflow_name)
        if (
            lifecycle is None
            or not REQUIRED_WORKFLOW_SETUP <= set(lifecycle[0])
            or not REQUIRED_WORKFLOW_TEARDOWN <= set(lifecycle[1])
        ):
            raise ConciergeError("launch_evidence_contradictory")
        publication_projection = capabilities.get("publication")
        if (
            not isinstance(publication_projection, Mapping)
            or publication_projection.get("available") is not True
        ):
            raise ConciergeError("launch_evidence_unavailable")
        publication = _publication_settings_evidence(capabilities)
        if publication is None:
            raise ConciergeError("launch_evidence_contradictory")
        return {
            "execution_mode": "new_worktree",
            "publish_remote": publication[0],
            "publish_branch": publication[1],
        }

    def _start_with_evidence(
        self,
        project_id: str,
        plan_path: str,
        decision: TriageDecision,
        details: dict[str, object],
    ) -> TickOutcome:
        try:
            payload = self._mcp.call_tool(
                "start_run",
                {
                    "project_id": project_id,
                    "plan_path": plan_path,
                    "idempotency_key": decision.idempotency_key,
                    "workflow_name": decision.workflow_name,
                    "team": decision.team,
                },
            )
        except ConciergeError as exc:
            if self._plan_has_active_run(project_id, plan_path):
                reconciled = (
                    "start_reconciled_active"
                    if decision.action == "start"
                    else "planned_and_reconciled_active"
                )
                return TickOutcome(
                    decision.action, reconciled, mutating=True, details=details
                )
            return TickOutcome("report", exc.reason, details=details)
        if not isinstance(payload, Mapping):
            return TickOutcome("report", "start_result_invalid", details=details)
        question = payload.get("startup_question")
        if isinstance(question, Mapping):
            message = str(question.get("message") or "")
            return TickOutcome(
                "report",
                "startup_question",
                details={
                    **details,
                    "question": message[:CONCIERGE_PLANNER_QUESTION_MAX_CHARS],
                },
            )
        result = payload.get("result")
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("run_id"), str)
            or not result.get("run_id")
        ):
            return TickOutcome("report", "start_result_invalid", details=details)
        started = (
            "started" if decision.action == "start" else "planned_and_started"
        )
        return TickOutcome(
            decision.action,
            started,
            mutating=True,
            details={**details, "run_id": str(result["run_id"])},
        )

    def _fresh_decision(
        self,
        project_id: str,
        decision: TriageDecision,
        full_name: str | None,
    ) -> TriageDecision | None:
        try:
            runs, plans, plan_documents = self._gather_evidence(project_id)
        except ConciergeError:
            return None
        issues: tuple[Mapping[str, object], ...] = ()
        if full_name is not None:
            try:
                page = self._github.open_issues(full_name)
            except ConciergeError:
                return None
            if not page.complete:
                return None
            issues = page.issues
        observation = TickObservation(
            project={"project_id": project_id},
            runs=runs,
            plans=plans,
            issues=issues,
            plan_documents=plan_documents,
            occupancy=self._occupancy_report(project_id, runs),
        )
        try:
            fresh = triage_tick(observation)
        except ConciergeError:
            return None
        if fresh.action != decision.action:
            return None
        if fresh.idempotency_key != decision.idempotency_key:
            return None
        if decision.action == "resume" and fresh.run_id != decision.run_id:
            return None
        if decision.action == "start" and fresh.plan_path != decision.plan_path:
            return None
        if (
            decision.action == "plan_and_start"
            and fresh.issue_number != decision.issue_number
        ):
            return None
        return fresh

    def _execute_resume(
        self, project_id: str, decision: TriageDecision
    ) -> TickOutcome:
        if self._fresh_decision(project_id, decision, None) is None:
            return TickOutcome(
                "report", "state_changed_before_action",
                details=self._decision_details(decision=decision),
            )
        if not self._resume_admits(project_id, decision):
            return TickOutcome(
                "report", "resume_not_admitted",
                details=self._decision_details(decision=decision),
            )
        if not self._queue_permits_new_implementation(
            self._project_queue(project_id)
        ):
            return TickOutcome(
                "report", "resume_not_admitted",
                details=self._decision_details(decision=decision),
            )
        try:
            self._mcp.call_tool(
                "resume_run",
                {
                    "project_id": project_id,
                    "run_id": decision.run_id,
                    "idempotency_key": decision.idempotency_key,
                },
            )
        except ConciergeError as exc:
            if self._run_is_active(project_id, str(decision.run_id)):
                return TickOutcome(
                    "resume", "resume_reconciled_active", mutating=True,
                    details=self._decision_details(decision=decision),
                )
            return TickOutcome("report", exc.reason)
        return TickOutcome(
            "resume", "resumed", mutating=True,
            details=self._decision_details(decision=decision),
        )

    def _resume_admits(self, project_id: str, decision: TriageDecision) -> bool:
        """Require the exact failed predecessor to be MCP-admissible now."""
        try:
            payload = self._mcp.call_tool(
                "get_run",
                {"project_id": project_id, "run_id": str(decision.run_id)},
            )
        except ConciergeError:
            return False
        if not isinstance(payload, Mapping):
            return False
        if payload.get("run_id") != decision.run_id:
            return False
        if payload.get("activity") != INACTIVE_RUN_ACTIVITY:
            return False
        if payload.get("status") not in TERMINAL_FAILED_RUN_STATUSES:
            return False
        plan_path = payload.get("plan_path")
        if not isinstance(plan_path, str) or plan_path != decision.plan_path:
            return False
        evidence = payload.get("evidence")
        return isinstance(evidence, Mapping) and evidence.get("can_resume") is True

    def _execute_start(
        self, project_id: str, decision: TriageDecision
    ) -> TickOutcome:
        if self._fresh_decision(project_id, decision, None) is None:
            return TickOutcome(
                "report", "state_changed_before_action",
                details=self._decision_details(decision=decision),
            )
        plan_path = str(decision.plan_path)
        details = self._decision_details(decision=decision)
        try:
            details.update(
                self._verify_launch_evidence(project_id, plan_path, decision)
            )
        except ConciergeError as exc:
            return TickOutcome("report", exc.reason, details=details)
        if not self._queue_permits_new_implementation(
            self._project_queue(project_id)
        ):
            return TickOutcome(
                "report", "state_changed_before_action", details=details
            )
        return self._start_with_evidence(project_id, plan_path, decision, details)

    def _execute_plan_and_start(
        self,
        project_id: str,
        decision: TriageDecision,
        state_dir: Path,
        full_name: str,
    ) -> TickOutcome:
        if self._fresh_decision(project_id, decision, full_name) is None:
            return TickOutcome(
                "report", "state_changed_before_action",
                details=self._decision_details(decision=decision),
            )
        number = int(decision.issue_number)
        details: dict[str, object] = {
            **self._decision_details(decision=decision),
            "planner_model": CONCIERGE_PLANNER_MODEL,
            "planner_effort": CONCIERGE_PLANNER_EFFORT,
        }
        page: GithubIssuePage | None = None
        try:
            page = self._github.open_issues(full_name)
        except ConciergeError:
            return TickOutcome(
                "report", "github_evidence_unavailable", details=details
            )
        if page is None or not page.complete:
            return TickOutcome(
                "report", "github_evidence_unavailable", details=details
            )
        issue = next(
            (
                candidate
                for candidate in page.issues
                if int(candidate["number"]) == number
            ),
            None,
        )
        if issue is None:
            return TickOutcome(
                "report", "state_changed_before_action", details=details
            )
        title = str(issue["title"])
        body = str(issue["body"])
        from aflow.issue_intake import source_hash

        try:
            source_sha256 = source_hash(title, body)
        except (TypeError, ValueError) as exc:
            raise ConciergeError("canonical_source_invalid") from exc
        canonical = ConciergeCanonicalIssue(
            title=title, body=body, title_body_sha256=source_sha256
        )
        claim_sha256 = hashlib.sha256(
            f"concierge-plan:{full_name}:{number}:{source_sha256}".encode("utf-8")
        ).hexdigest()
        record_path = state_dir / "planner-records" / f"{claim_sha256}.json"
        record: Mapping[str, object] | None
        if record_path.is_file():
            try:
                loaded = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                loaded = None
            record = loaded if isinstance(loaded, Mapping) else None
        else:
            record = None

        def persist_workspace(workspace_record: Mapping[str, object]) -> None:
            _atomic_write(
                record_path,
                (json.dumps(dict(workspace_record), sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
                label="planner_workspace",
            )

        factory = self._planner_factory or _build_concierge_planner
        planner = factory(state_dir, self._mcp)
        try:
            if record is not None:
                result = planner.recover(
                    record=record,
                    repository_id=page.repository_id,
                    issue_number=number,
                    project_id=project_id,
                    claim_sha256=claim_sha256,
                )
                details["planner_recovered"] = True
            else:
                result = planner.plan(
                    repository_id=page.repository_id,
                    repository_full_name=full_name,
                    issue_number=number,
                    project_id=project_id,
                    claim_sha256=claim_sha256,
                    canonical=canonical,
                    persist_workspace=persist_workspace,
                )
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            return TickOutcome(
                "report",
                "planner_failed",
                details={
                    **details,
                    "planner_reason": str(reason or "planner_failure")[:128],
                },
            )
        if result.status == "needs_attention":
            question = str(result.question or "")[:CONCIERGE_PLANNER_QUESTION_MAX_CHARS]
            return TickOutcome(
                "report", "planner_needs_attention", details={**details, "question": question}
            )
        if result.status != "plan" or not isinstance(result.markdown, str):
            return TickOutcome("report", "planner_result_invalid", details=details)
        try:
            runs, plans, plan_documents = self._gather_evidence(project_id)
        except ConciergeError:
            return TickOutcome("report", "evidence_unavailable", details=details)
        if self._occupancy_blocks(project_id, runs):
            return TickOutcome(
                "report", "state_changed_before_action", details=details
            )
        try:
            fresh_page = self._github.open_issues(full_name)
        except ConciergeError:
            return TickOutcome(
                "report", "github_evidence_unavailable", details=details
            )
        if not fresh_page.complete:
            return TickOutcome(
                "report", "github_evidence_unavailable", details=details
            )
        fresh_issue = next(
            (
                candidate
                for candidate in fresh_page.issues
                if int(candidate["number"]) == number
            ),
            None,
        )
        fresh_author = fresh_issue.get("author_id") if fresh_issue else None
        if (
            fresh_issue is None
            or type(fresh_author) is not int
            or fresh_author != CONCIERGE_OWNER_ID
            or fresh_issue.get("state", "open") != "open"
        ):
            return TickOutcome(
                "report", "state_changed_before_action", details=details
            )
        plan_name = f"owner-issue-{number}.md"
        plan_path = f"plans/todo/{plan_name}"
        try:
            queue = self._mcp.call_tool(
                "get_project_queue", {"project_id": project_id}
            )
        except ConciergeError:
            return TickOutcome(
                "report", "launch_evidence_unavailable", details=details
            )
        if not self._queue_admits_single_launch(queue, plan_path):
            return TickOutcome(
                "report", "state_changed_before_action", details=details
            )
        planned_urls: set[str] = set()
        for plan in plans:
            if _canonical_plan_status(plan.get("status")) not in PLANNED_EVIDENCE_STATUSES:
                continue
            content = plan_documents.get(str(plan.get("path") or ""))
            if not isinstance(content, str):
                return TickOutcome("report", "evidence_unavailable", details=details)
            planned_urls.update(_issue_urls_in(content))
        if decision.issue_url in planned_urls:
            return TickOutcome(
                "report", "duplicate_detected_before_authoring", details=details
            )
        try:
            self._mcp.call_tool(
                "create_plan",
                {"project_id": project_id, "name": plan_name, "content": result.markdown},
            )
        except ConciergeError as exc:
            return TickOutcome("report", exc.reason, details=details)
        try:
            runs, _, _ = self._gather_evidence(project_id)
            queue = self._mcp.call_tool(
                "get_project_queue", {"project_id": project_id}
            )
        except ConciergeError:
            return TickOutcome("report", "evidence_unavailable", details=details)
        if self._occupancy_blocks(project_id, runs) or not (
            self._queue_admits_single_launch(queue, plan_path)
        ):
            return TickOutcome(
                "report", "state_changed_before_action", details=details
            )
        try:
            details.update(
                self._verify_launch_evidence(project_id, plan_path, decision)
            )
        except ConciergeError as exc:
            return TickOutcome("report", exc.reason, details=details)
        return self._start_with_evidence(project_id, plan_path, decision, details)

    def _run_is_active(self, project_id: str, run_id: str) -> bool:
        detail = self._run_detail(project_id, run_id)
        if detail is None:
            return False
        return classify_run_occupancy(detail) == OCCUPANCY_ACTIVE

    def _plan_has_active_run(self, project_id: str, plan_path: str) -> bool:
        try:
            runs = self._list_runs(project_id)
        except ConciergeError:
            return False
        return any(
            run.get("plan_path") == plan_path
            and classify_run_occupancy(run) == OCCUPANCY_ACTIVE
            for run in runs
        )


def _build_production_executor(
    token: str, environment: Mapping[str, str]
) -> ConciergeTickExecutor:
    github = GithubRestClient(token=environment.get(CONCIERGE_GITHUB_TOKEN_ENV))
    return ConciergeTickExecutor(
        mcp=McpHttpEndpoint(CONCIERGE_MCP_URL, token),
        github=github,
        planner_factory=_build_concierge_planner,
        delivery_gate=LocalDeliveryGate(
            github=github, project_root=CONCIERGE_PROJECT_ROOT_DEFAULT
        ),
    )


def redact_text(text: str, secrets: Sequence[str]) -> str:
    """Replace every known secret value with a fixed marker."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


@contextmanager
def tick_lock(state_dir: Path) -> Iterator[None]:
    """Own the single concierge tick slot for the duration of the body."""
    lock_path = state_dir / "concierge.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TickDeferral("tick_in_progress") from exc
        locked = True
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        if locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, grace: float = 5.0
) -> None:
    """Terminate only the process group created for this tick."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The leader can exit on SIGTERM while a descendant keeps the owned
    # session alive. Do not treat a successful leader wait as group
    # completion; escalate against the session before returning.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.kill()
        except OSError:
            return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _run_tick_process(
    argv: Sequence[str],
    cwd: Path,
    prompt: bytes,
    timeout_seconds: float,
) -> TickProcessResult:
    """Run the tick process with bounded streams and owned cancellation."""
    if not argv:
        raise ConciergeError("concierge_argv_empty")
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ConciergeError("concierge_unavailable") from exc
    except OSError as exc:
        raise ConciergeError("concierge_launch_failed") from exc

    deadline = time.monotonic() + timeout_seconds
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    overflow = threading.Event()
    termination_requested = threading.Event()

    def drain(stream: Any, buffer: bytearray) -> None:
        try:
            read_chunk = getattr(stream, "read1", stream.read)
            while True:
                chunk = read_chunk(65536)
                if not chunk:
                    return
                if len(buffer) + len(chunk) > CONCIERGE_STREAM_MAX_BYTES:
                    overflow.set()
                if len(buffer) < CONCIERGE_STREAM_MAX_BYTES:
                    remaining = CONCIERGE_STREAM_MAX_BYTES + 1 - len(buffer)
                    buffer.extend(chunk[:remaining])
        except OSError:
            return

    stdout_thread = threading.Thread(
        target=drain, args=(process.stdout, stdout_buffer), daemon=True
    )
    stderr_thread = threading.Thread(
        target=drain, args=(process.stderr, stderr_buffer), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    overflowed = False
    interrupted = False
    stdin_thread: threading.Thread | None = None
    previous_sigterm: Any = None
    signal_handler_installed = False

    def handle_sigterm(_signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        termination_requested.set()
        _terminate_process_group(process, grace=1.0)

    try:
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, handle_sigterm)
        signal_handler_installed = True
    except ValueError:
        # A caller embedded in a non-main thread cannot install process signal
        # handlers; the normal deadline still applies.
        pass

    def feed_stdin() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    try:
        assert process.stdin is not None
        stdin_thread = threading.Thread(target=feed_stdin, daemon=True)
        stdin_thread.start()
        while process.poll() is None:
            if termination_requested.is_set():
                break
            if overflow.is_set():
                overflowed = True
                _terminate_process_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_group(process)
                break
            time.sleep(0.05)
        if process.poll() is None:
            process.wait(timeout=5.0)
    except KeyboardInterrupt:
        interrupted = True
        _terminate_process_group(process)
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        # A successful leader exit does not prove that its owned session has
        # no descendants. Escalate before closing any inherited pipes; an
        # already-empty group returns immediately.
        _terminate_process_group(process, grace=1.0)
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        if stdin_thread is not None:
            stdin_thread.join(timeout=5.0)
        stdout_thread.join(timeout=5.0)
        stderr_thread.join(timeout=5.0)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        if signal_handler_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return TickProcessResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
        timed_out=timed_out,
        overflowed=overflowed or overflow.is_set(),
        interrupted=interrupted,
    )


def _validate_state_dir(state_dir: Path) -> None:
    if not state_dir.is_absolute():
        raise ConciergeError("state_dir_must_be_absolute")
    current = Path("/")
    for part in state_dir.parts:
        current = current / part
        if current.is_symlink():
            raise ConciergeError("state_dir_unsafe")


def _ensure_private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ConciergeError("state_dir_unsafe")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConciergeError("state_dir_unavailable") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConciergeError("state_dir_unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ConciergeError("state_dir_unsafe")
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ConciergeError("state_dir_unsafe") from exc
    return path


def _atomic_write(path: Path, data: bytes, *, label: str) -> None:
    parent = _ensure_private_directory(path.parent)
    if path.is_symlink():
        raise ConciergeError(f"{label}_symlink")
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ConciergeError(f"{label}_symlink")
        os.replace(temporary, path)
        temporary = None
        directory_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except ConciergeError:
        raise
    except OSError as exc:
        raise ConciergeError(f"{label}_write_failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _write_status(state_dir: Path, status: Mapping[str, object]) -> None:
    _atomic_write(
        state_dir / "status.json",
        (json.dumps(dict(status), sort_keys=True) + "\n").encode("utf-8"),
        label="status",
    )


def _bounded_redacted_tail(raw: bytes, secret: str) -> str:
    text = redact_text(raw.decode("utf-8", errors="replace"), [secret])
    return text[-CONCIERGE_STATUS_TAIL_MAX_CHARS:]


def dry_run(
    *,
    state_dir: Path,
    work_dir: Path,
    environment: Mapping[str, str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    journal: Callable[[str], None] = print,
) -> int:
    """Validate configuration and report the tick plan without side effects.

    A dry run never creates the state directory, never acquires the lock,
    and never launches a process.
    """
    token = read_bearer_token(environment)
    argv = build_codex_argv(work_dir=work_dir)
    if any(token in part for part in argv):
        raise ConciergeError("bearer_token_leak")
    for line in (
        "aflow-concierge: dry-run (no lock, no process, no state)",
        f"  model={CONCIERGE_MODEL}",
        f"  effort={CONCIERGE_EFFORT}",
        f"  mcp_url={CONCIERGE_MCP_URL}",
        f"  token_env={CONCIERGE_TOKEN_ENV}",
        f"  work_dir={work_dir}",
        f"  state_dir={state_dir}",
        f"  timeout_seconds={timeout_seconds:g}",
        f"  argv={redact_text(' '.join(argv), [token])}",
    ):
        journal(line)
    return 0


def run_tick(
    *,
    state_dir: Path,
    work_dir: Path,
    environment: Mapping[str, str],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner: ProcessRunner | None = None,
    tick_executor: TickExecutor | None = None,
    project_root: Path | None = None,
    journal: Callable[[str], None] = print,
) -> int:
    """Run one bounded, non-overlapping concierge tick.

    The authoritative decision and at most one bounded MCP action come from
    ``tick_executor`` (the production :class:`ConciergeTickExecutor` by
    default).  The short-lived Codex process remains an advisory read-only
    review whose bounded output is recorded in the status file.
    """
    token = read_bearer_token(environment)
    _validate_state_dir(state_dir)
    _ensure_private_directory(state_dir)
    executor = tick_executor if tick_executor is not None else None
    owns_executor = executor is None
    if owns_executor:
        executor = _build_production_executor(token, environment)
    resolved_project_root = (
        project_root if project_root is not None else CONCIERGE_PROJECT_ROOT_DEFAULT
    )
    outcome: TickOutcome | None = None
    try:
        with tick_lock(state_dir):
            try:
                outcome = executor.execute(
                    state_dir=state_dir,
                    project_root=resolved_project_root,
                    environment=environment,
                )
            except ConciergeError as exc:
                outcome = TickOutcome("report", exc.reason)
            process_runner = runner if runner is not None else _run_tick_process
            argv = build_codex_argv(work_dir=work_dir)
            prompt = build_tick_prompt().encode("utf-8")
            started_at = time.time()
            result = process_runner(argv, work_dir, prompt, timeout_seconds)
            finished_at = time.time()
            stdout_tail = _bounded_redacted_tail(result.stdout, token)
            stderr_tail = _bounded_redacted_tail(result.stderr, token)
            if result.timed_out:
                phase, reason, exit_code = "timed_out", "concierge_timed_out", 1
            elif result.overflowed:
                phase, reason, exit_code = "failed", "concierge_stream_overflow", 1
            elif result.interrupted:
                phase, reason, exit_code = "failed", "concierge_interrupted", 1
            elif result.returncode == 0:
                phase, reason, exit_code = "completed", "concierge_tick_completed", 0
            else:
                phase, reason, exit_code = "failed", "concierge_process_failed", 1
            status = {
                "schema_version": CONCIERGE_STATUS_SCHEMA_VERSION,
                "phase": phase,
                "reason": reason,
                "model": CONCIERGE_MODEL,
                "effort": CONCIERGE_EFFORT,
                "mcp_url": CONCIERGE_MCP_URL,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": round(finished_at - started_at, 3),
                "returncode": result.returncode,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "action": outcome.action,
                "outcome": outcome.reason,
                "mutating": outcome.mutating,
                "planner_model": CONCIERGE_PLANNER_MODEL,
                "planner_effort": CONCIERGE_PLANNER_EFFORT,
            }
            for key, value in outcome.details.items():
                if key in {"run_id", "plan_path", "issue_number"}:
                    status[f"decision_{key}"] = value
            _write_status(state_dir, status)
            journal(f"aflow-concierge: phase={phase} reason={reason}")
            journal(
                f"aflow-concierge: action={outcome.action} outcome={outcome.reason}"
            )
            return exit_code
    except TickDeferral:
        status = {
            "schema_version": CONCIERGE_STATUS_SCHEMA_VERSION,
            "phase": "deferred",
            "reason": "tick_in_progress",
            "model": CONCIERGE_MODEL,
            "effort": CONCIERGE_EFFORT,
            "mcp_url": CONCIERGE_MCP_URL,
            "started_at": time.time(),
            "finished_at": time.time(),
            "duration_seconds": 0.0,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "action": "deferred",
            "outcome": "tick_in_progress",
            "mutating": False,
            "planner_model": CONCIERGE_PLANNER_MODEL,
            "planner_effort": CONCIERGE_PLANNER_EFFORT,
        }
        _write_status(state_dir, status)
        journal("aflow-concierge: phase=deferred reason=tick_in_progress")
        return 0
    finally:
        if owns_executor:
            try:
                executor.close()
            except Exception:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aflow-concierge",
        description=(
            "Run one bounded, non-overlapping AFlow owner-issue concierge tick."
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help="private state directory for the tick lock and status record",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
        help=(
            "read-only working directory for the tick process; need not be a "
            "Git repository because the invocation passes --skip-git-repo-check"
        ),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=CONCIERGE_PROJECT_ROOT_DEFAULT,
        help="registered AFlow project root the concierge serves",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="internal tick timeout, strictly below the 20-minute interval",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and print the tick plan without side effects",
    )
    args = parser.parse_args(argv)
    if not 0 < args.timeout_seconds < TICK_INTERVAL_SECONDS:
        print(
            "aflow-concierge: timeout_seconds must be greater than zero and "
            "strictly below the 20-minute tick interval",
            file=sys.stderr,
        )
        return 1
    environment = dict(os.environ)
    try:
        if args.dry_run:
            return dry_run(
                state_dir=args.state_dir,
                work_dir=args.work_dir,
                environment=environment,
                timeout_seconds=args.timeout_seconds,
            )
        return run_tick(
            state_dir=args.state_dir,
            work_dir=args.work_dir,
            environment=environment,
            timeout_seconds=args.timeout_seconds,
            project_root=args.project_root,
        )
    except ConciergeError as exc:
        print(f"aflow-concierge: {exc.reason}", file=sys.stderr)
        return 1


__all__ = [
    "CONCIERGE_EFFORT",
    "CONCIERGE_GITHUB_TOKEN_ENV",
    "CONCIERGE_MCP_URL",
    "CONCIERGE_MODEL",
    "CONCIERGE_OWNER_ID",
    "CONCIERGE_PLANNER_EFFORT",
    "CONCIERGE_PLANNER_MODEL",
    "CONCIERGE_PROJECT_ROOT_DEFAULT",
    "CONCIERGE_TEAM",
    "CONCIERGE_TOKEN_ENV",
    "CONCIERGE_WORKFLOW_NAME",
    "ConciergeCanonicalIssue",
    "ConciergeError",
    "ConciergeTickExecutor",
    "GithubClient",
    "GithubIssuePage",
    "McpClient",
    "McpHttpEndpoint",
    "McpRegistryClient",
    "TickDeferral",
    "TickExecutor",
    "TickObservation",
    "TickOutcome",
    "TickProcessResult",
    "TriageDecision",
    "build_codex_argv",
    "build_tick_prompt",
    "defect_signatures",
    "dry_run",
    "load_tick_prompt",
    "main",
    "read_bearer_token",
    "redact_text",
    "repository_full_name",
    "run_tick",
    "tick_lock",
    "triage_tick",
]

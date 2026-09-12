#!/usr/bin/env python3
"""Capture a bounded, sanitized UI data-loading baseline.

The runner profiles the existing UI through its browser-visible routes. It can
either use an explicit owner-facing URL or launch a disposable loopback server
over a deterministic fixture. It intentionally does not profile implementation
code directly and does not write to a live project.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_SRC = REPO_ROOT / "apps" / "aflow_app" / "server" / "src"
WEB_DIST = REPO_ROOT / "apps" / "aflow_app" / "web" / "dist"
NL = chr(10)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(SERVER_SRC))


REPORT_SCHEMA_VERSION = 1
MAX_REPEATS = 10
MAX_LIVE_REPEATS = 3
MAX_REQUEST_RECORDS = 500
WAIT_TIMEOUT_MS = 15_000
SCALE_DATA_TIMEOUT_MS = 45_000
SERVER_START_TIMEOUT_SECONDS = 10.0
OPTIONAL_COUNTERFACTUAL_SUFFIXES = ("/events/stream", "/restart-options")
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "code",
        "password",
        "secret",
        "token",
    }
)


@dataclass(frozen=True)
class FixtureSpec:
    """One bounded fixture scale and history shape."""

    name: str
    run_count: int
    selected_event_count: int
    other_event_count: int
    history_shape: str


@dataclass(frozen=True)
class FixtureBundle:
    """Paths and identity for a disposable fixture server."""

    root: Path
    home: Path
    config_dir: Path
    project_root: Path
    token: str
    project_id: str
    selected_run_id: str
    representative_run_id: str
    run_count: int
    selected_event_count: int
    other_event_count: int
    file_count: int


@dataclass(frozen=True)
class RouteSpec:
    name: str
    path: str
    useful_expression: str
    useful_argument: str | None
    milestone: str


@dataclass
class BrowserCapture:
    """Collect browser timing metadata without retaining response bodies."""

    page: Any
    base_url: str
    requests: dict[int, dict[str, Any]] = field(default_factory=dict)
    request_order: list[int] = field(default_factory=list)
    truncated: bool = False
    started_at: float = field(default_factory=time.perf_counter)

    def __post_init__(self) -> None:
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfinished", self._on_request_finished)
        self.page.on("requestfailed", self._on_request_failed)

    def reset(self) -> None:
        self.requests.clear()
        self.request_order.clear()
        self.truncated = False
        self.started_at = time.perf_counter()
        try:
            self.page.evaluate(
                """() => {
                    window.__aflowLatencyReset?.();
                    performance.clearResourceTimings?.();
                }"""
            )
        except Exception:
            # Navigation may replace the document between reset and capture;
            # the subsequent browser snapshot still records the failure.
            pass

    def _record_for(self, request: Any) -> dict[str, Any] | None:
        key = id(request)
        record = self.requests.get(key)
        if record is not None:
            return record
        if len(self.requests) >= MAX_REQUEST_RECORDS:
            self.truncated = True
            return None
        redirected_from = getattr(request, "redirected_from", None)
        if callable(redirected_from):
            redirected_from = redirected_from()
        record = {
            "method": str(getattr(request, "method", "GET")),
            "path": canonical_url(str(getattr(request, "url", "")), self.base_url),
            "resource_type": str(getattr(request, "resource_type", "unknown")),
            "started_ms": round((time.perf_counter() - self.started_at) * 1000, 3),
            "redirected_from": redirected_from is not None,
            "status": None,
            "content_length_bytes": None,
            "cache_control_present": None,
            "age_header_present": None,
            "etag_present": None,
            "finished_ms": None,
            "failed": False,
            "failure": None,
        }
        self.requests[key] = record
        self.request_order.append(key)
        return record

    def _on_request(self, request: Any) -> None:
        self._record_for(request)

    def _on_response(self, response: Any) -> None:
        record = self._record_for(response.request)
        if record is None:
            return
        record["status"] = int(response.status)
        try:
            value = response.headers.get("content-length")
            if value is not None and value.isdigit():
                record["content_length_bytes"] = int(value)
            record["cache_control_present"] = bool(
                response.headers.get("cache-control")
            )
            record["age_header_present"] = bool(response.headers.get("age"))
            record["etag_present"] = bool(response.headers.get("etag"))
        except Exception:
            pass

    def _on_request_finished(self, request: Any) -> None:
        record = self._record_for(request)
        if record is not None:
            record["finished_ms"] = round(
                (time.perf_counter() - self.started_at) * 1000, 3
            )

    def _on_request_failed(self, request: Any) -> None:
        record = self._record_for(request)
        if record is None:
            return
        record["failed"] = True
        record["failure"] = safe_error(getattr(request, "failure", None))
        record["finished_ms"] = round(
            (time.perf_counter() - self.started_at) * 1000, 3
        )

    def snapshot(self) -> dict[str, Any]:
        try:
            browser_state = self.page.evaluate(
                """() => ({
                    ...(window.__aflowLatencySnapshot?.() ?? {}),
                    resources: performance.getEntriesByType('resource').map(entry => ({
                        name: entry.name,
                        startTime: entry.startTime,
                        domainLookupStart: entry.domainLookupStart,
                        domainLookupEnd: entry.domainLookupEnd,
                        connectStart: entry.connectStart,
                        secureConnectionStart: entry.secureConnectionStart,
                        connectEnd: entry.connectEnd,
                        requestStart: entry.requestStart,
                        responseStart: entry.responseStart,
                        responseEnd: entry.responseEnd,
                        transferSize: entry.transferSize,
                        encodedBodySize: entry.encodedBodySize,
                        decodedBodySize: entry.decodedBodySize,
                    })).slice(-500),
                })"""
            )
        except Exception as exc:
            browser_state = {
                "fetches": [],
                "long_tasks": [],
                "performance_measures": [],
                "paint_entries": [],
                "action_start_ms": None,
                "accepted_useful_ms": None,
                "run_detail_data_mutation_times": [],
                "resources": [],
                "snapshot_error": safe_error(exc),
            }

        resources = browser_state.get("resources", [])
        if not isinstance(resources, list):
            resources = []
        resource_by_path: dict[str, list[dict[str, Any]]] = {}
        for resource in resources:
            if not isinstance(resource, Mapping):
                continue
            path = canonical_url(str(resource.get("name", "")), self.base_url)
            resource_by_path.setdefault(path, []).append(dict(resource))

        network: list[dict[str, Any]] = []
        for key in self.request_order:
            record = self.requests.get(key)
            if record is None:
                continue
            item = dict(record)
            entries = resource_by_path.get(str(item["path"]), [])
            resource = entries.pop(0) if entries else None
            if resource is not None:
                item.update(resource_timing(resource))
            elif item.get("content_length_bytes") is not None:
                item["transfer_size_bytes"] = item["content_length_bytes"]
            network.append(item)

        fetches = browser_state.get("fetches", [])
        if not isinstance(fetches, list):
            fetches = []
        action_start_ms = finite_number(browser_state.get("action_start_ms"))
        accepted_useful_ms = finite_number(browser_state.get("accepted_useful_ms"))

        def from_action(value: Any) -> float | None:
            numeric = finite_number(value)
            if numeric is None or action_start_ms is None:
                return None
            return round(numeric - action_start_ms, 3)

        api_flow = []
        for sequence, fetch in enumerate(fetches, start=1):
            if not isinstance(fetch, Mapping):
                continue
            path = canonical_url(str(fetch.get("url", "")), self.base_url)
            if not path.startswith("/api/"):
                continue
            fetch_start_ms = finite_number(fetch.get("fetch_start_ms"))
            response_resolved_ms = finite_number(fetch.get("response_resolved_ms"))
            json_start_ms = finite_number(fetch.get("json_start_ms"))
            json_end_ms = finite_number(fetch.get("json_end_ms"))
            api_flow.append(
                {
                    "sequence": sequence,
                    "method": str(fetch.get("method", "GET")),
                    "path": path,
                    "fetch_start_ms": fetch_start_ms,
                    "response_resolved_ms": response_resolved_ms,
                    "json_start_ms": json_start_ms,
                    "json_end_ms": json_end_ms,
                    "fetch_start_from_action_ms": from_action(fetch_start_ms),
                    "response_resolved_from_action_ms": from_action(
                        response_resolved_ms
                    ),
                    "json_start_from_action_ms": from_action(json_start_ms),
                    "json_end_from_action_ms": from_action(json_end_ms),
                    "json_error": bool(fetch.get("json_error", False)),
                    "fetch_error": bool(fetch.get("fetch_error", False)),
                }
            )
        previous_json_end: float | None = None
        for item in api_flow:
            fetch_start = item["fetch_start_ms"]
            if previous_json_end is not None and fetch_start is not None:
                item["start_delta_from_previous_ms"] = round(
                    fetch_start - previous_json_end, 3
                )
                item["starts_after_previous_json"] = fetch_start >= previous_json_end
            else:
                item["start_delta_from_previous_ms"] = None
                item["starts_after_previous_json"] = None
            previous_json_end = item["json_end_ms"] or item["response_resolved_ms"]

        duplicate_counts = Counter(
            item["path"] for item in network if str(item["path"]).startswith("/api/")
        )
        duplicates = sorted(
            path for path, count in duplicate_counts.items() if count > 1
        )
        failures = [
            {"path": item["path"], "failure": item["failure"]}
            for item in network
            if item.get("failed")
        ]
        return {
            "network": network,
            "api_request_flow": api_flow,
            "main_thread_long_tasks": browser_state.get("long_tasks", []),
            "run_detail_mutation_times": browser_state.get(
                "run_detail_mutation_times", []
            ),
            "run_detail_data_mutation_times": browser_state.get(
                "run_detail_data_mutation_times", []
            ),
            "browser_action_start_ms": action_start_ms,
            "browser_accepted_useful_ms": accepted_useful_ms,
            "browser_useful_after_action_ms": (
                round(accepted_useful_ms - action_start_ms, 3)
                if accepted_useful_ms is not None and action_start_ms is not None
                else None
            ),
            "performance_measures": browser_state.get("performance_measures", []),
            "paint_entries": browser_state.get("paint_entries", []),
            "duplicate_api_paths": duplicates,
            "failed_requests": failures,
            "request_count": len(network),
            "api_request_count": sum(
                1 for item in network if str(item["path"]).startswith("/api/")
            ),
            "capture_truncated": self.truncated,
            "snapshot_error": browser_state.get("snapshot_error"),
        }


def finite_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric == numeric and abs(numeric) != float("inf"):
            return round(numeric, 3)
    return None


def resource_timing(resource: Mapping[str, Any]) -> dict[str, Any]:
    """Turn Resource Timing fields into bounded, comparable durations."""

    def difference(end_key: str, start_key: str) -> float | None:
        end = finite_number(resource.get(end_key))
        start = finite_number(resource.get(start_key))
        if end is None or start is None or end < start:
            return None
        return round(end - start, 3)

    start = finite_number(resource.get("startTime"))
    response_end = finite_number(resource.get("responseEnd"))
    secure_start = finite_number(resource.get("secureConnectionStart"))
    connect_end = finite_number(resource.get("connectEnd"))
    result: dict[str, Any] = {
        "resource_start_ms": start,
        "request_start_offset_ms": difference("requestStart", "startTime"),
        "dns_ms": difference("domainLookupEnd", "domainLookupStart"),
        "connect_ms": difference("connectEnd", "connectStart"),
        "tls_ms": (
            round(connect_end - secure_start, 3)
            if secure_start is not None
            and connect_end is not None
            and secure_start > 0
            and connect_end >= secure_start
            else None
        ),
        "ttfb_ms": difference("responseStart", "requestStart"),
        "download_ms": difference("responseEnd", "responseStart"),
        "transfer_size_bytes": integer_value(resource.get("transferSize")),
        "encoded_body_size_bytes": integer_value(resource.get("encodedBodySize")),
        "decoded_body_size_bytes": integer_value(resource.get("decodedBodySize")),
    }
    if response_end is not None and start is not None and response_end >= start:
        result["browser_duration_ms"] = round(response_end - start, 3)
    else:
        result["browser_duration_ms"] = None
    return result


def integer_value(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, float) and value == int(value) and value >= 0:
        return int(value)
    return None


def canonical_url(raw_url: str, base_url: str) -> str:
    """Keep only a same-origin path and redact sensitive query values."""

    del base_url
    try:
        parsed = urlsplit(raw_url)
        if not parsed.path:
            return "/"
        query_items = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in SENSITIVE_QUERY_KEYS:
                value = "<redacted>"
            query_items.append((key, value))
        query = urlencode(query_items)
        return urlunsplit(("", "", parsed.path, query, ""))
    except (TypeError, ValueError):
        return "/<invalid-url>"


def safe_error(error: Any) -> str | None:
    if error is None:
        return None
    try:
        if callable(error):
            error = error()
        text = str(error)
    except Exception:
        return "error"
    return " ".join(text.split())[:300] or "error"


def source_sha() -> str:
    status = subprocess.run(
        (
            "git",
            "-C",
            str(REPO_ROOT),
            "--no-optional-locks",
            "status",
            "--short",
            "--untracked-files=all",
            "--",
            "scripts/perf/profile_ui_data_load.py",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "browser runner source is dirty; commit the runner before collecting shareable evidence"
        )
    result = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "--no-optional-locks", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def product_source_sha() -> str:
    """Return the last committed revision that changed product source.

    Discovery runners are edited independently of the application.  Using the
    runner's HEAD as the product identity would make a tooling-only commit
    look like a product revision, while accepting a caller-provided SHA would
    be an unchecked pin.  Refuse a dirty product tree and derive the identity
    from Git's product-path history instead.
    """

    dirty = product_source_snapshot()
    if dirty:
        raise RuntimeError(
            "product source is dirty; discovery requires an unchanged committed product tree"
        )
    result = subprocess.run(
        (
            "git",
            "-C",
            str(REPO_ROOT),
            "--no-optional-locks",
            "log",
            "-1",
            "--format=%H",
            "--",
            "aflow",
            "apps/aflow_app",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("could not determine the committed product source revision")
    return value


def host_activity() -> dict[str, Any]:
    load_average = None
    try:
        load_average = [round(float(value), 3) for value in os.getloadavg()[:3]]
    except (AttributeError, OSError):
        pass
    process_count = None
    aflow_named_process_count = None
    try:
        process_result = subprocess.run(
            ("ps", "-eo", "comm="),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        process_names = [line.strip().lower() for line in process_result.stdout.splitlines() if line.strip()]
        process_count = len(process_names)
        aflow_named_process_count = sum(
            1
            for name in process_names
            if "aflow" in name or "uvicorn" in name
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "host_label": platform.node() or "unknown",
        "os": platform.platform(aliased=True),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load_average": load_average,
        "process_count": process_count,
        "aflow_named_process_count": aflow_named_process_count,
        "external_workflows": "ambient process names sampled without attribution; runner remained serial",
    }


def fixture_specs(scenario: str) -> list[FixtureSpec]:
    specs = {
        "isolated-smoke": [FixtureSpec("smoke", 12, 6, 1, "short")],
        "isolated-small": [
            FixtureSpec("small-short-history", 100, 8, 2, "short"),
            FixtureSpec("small-long-history", 100, 80, 2, "long"),
        ],
        "isolated-large": [
            FixtureSpec("large-short-history", 1000, 8, 2, "short"),
            FixtureSpec("large-long-history", 1000, 80, 2, "long"),
        ],
    }
    if scenario == "isolated":
        return specs["isolated-small"] + specs["isolated-large"]
    return specs[scenario]


def write_text(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def fixture_config_text() -> tuple[str, str]:
    aflow = """[aflow]
default_workflow = "managed"

[harness.codex.profiles.test]
model = "test"

[roles]
worker = "codex.test"

[prompts]
p = "Work."
"""
    workflows = """[workflow.managed.steps.implement]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]
"""
    return aflow, workflows


def build_fixture(spec: FixtureSpec, seed: int, parent: Path) -> FixtureBundle:
    """Build one fixture with the same durable helper shapes as tests."""

    from aflow.control_plane import LaunchManifest, create_launch_manifest, write_launch_phase
    from aflow.control_plane.persistence import append_run_event
    from aflow_app_server.project_registry import ProjectRegistry

    fixture_root = parent / spec.name
    home = fixture_root / "home"
    config_dir = home / ".config" / "aflow"
    managed_root = fixture_root / "managed"
    project_root = managed_root / "fixture-project"
    project_root.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    write_text(project_root / ".gitignore", ".aflow/" + NL)
    plan_path = project_root / "plans" / "in-progress" / "ui-latency-fixture.md"
    plan_text = """# UI latency fixture

### [x] Checkpoint 1: Indexed fixture records
- [x] Generate a bounded sanitized run record.

### [ ] Checkpoint 2: Fixture tail
- [ ] Keep a second checkpoint so progress projection has real plan shape.
"""
    write_text(plan_path, plan_text)

    subprocess.run(("git", "-C", str(project_root), "init", "-q"), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(project_root),
            "config",
            "user.email",
            "fixture@example.invalid",
        ),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(project_root), "config", "user.name", "UI latency fixture"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(project_root),
            "add",
            "-f",
            ".gitignore",
            str(plan_path.relative_to(project_root)),
        ),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(project_root), "commit", "-qm", "fixture: add bounded plan"),
        check=True,
    )

    aflow_text, workflows_text = fixture_config_text()
    write_text(config_dir / "aflow.toml", aflow_text)
    write_text(config_dir / "workflows.toml", workflows_text)
    write_text(config_dir / "config.toml", "[server]" + NL + 'bind_host = "127.0.0.1"' + NL)

    environment_file = fixture_root / "aflowd.env"
    write_text(environment_file, "AFLOWD_MODE=ui-latency-fixture" + NL)
    executable = fixture_root / "release" / "bin" / "aflow"
    write_text(executable, "#!/bin/sh" + NL + "exit 0" + NL, executable=True)

    registry = ProjectRegistry(managed_root, config_dir / "projects.json")
    registry.register("fixture-project", "UI latency fixture", "fixture-project")

    width = max(4, len(str(max(0, spec.run_count - 1))))
    # Keep the selected identity stable across list-size fixtures.  The
    # selected history count is the intentional event-scaling variable; the
    # selected run itself must not move from run-0099 to run-0999.
    selected_index = 0
    selected_run_id = f"run-{selected_index:0{width}d}"
    representative_run_id = f"run-{1:0{width}d}" if spec.run_count > 1 else selected_run_id
    for index in range(spec.run_count):
        run_id = f"run-{index:0{width}d}"
        run_dir = project_root / ".aflow" / "runs" / run_id
        event_count = (
            spec.selected_event_count
            if run_id == selected_run_id
            else spec.other_event_count
        )
        create_launch_manifest(
            project_root,
            LaunchManifest(
                run_id=run_id,
                project_root=str(project_root.resolve()),
                plan_path=str(plan_path.resolve()),
                workflow_name="managed",
                max_turns=100,
                team="fixture-team",
                start_step="implement",
                extra_instructions=(),
                caller_scope="fixture:ui-latency",
                created_at=f"2026-09-12T00:00:{index % 60:02d}Z",
            ),
        )
        write_launch_phase(project_root, run_id, "completed")
        run_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "status": "completed",
            "repo_root": str(project_root.resolve()),
            "plan_path": str(plan_path.resolve()),
            "active_plan_path": str(plan_path.resolve()),
            "original_plan_path": str(plan_path.resolve()),
            "workflow_name": "managed",
            "team": "fixture-team",
            "current_step_name": "implement",
            "turns_completed": event_count,
            "effective_max_turns": 100,
            "run_started_at": f"2026-09-12T00:00:{index % 60:02d}Z",
        }
        write_text(run_dir / "run.json", json.dumps(metadata, sort_keys=True) + NL)
        for turn_number in range(1, event_count + 1):
            append_run_event(
                run_dir,
                "turn_completed",
                {
                    "turn_number": turn_number,
                    "step_name": "implement",
                    "role": "worker",
                    "status": "completed",
                    "outcome": "accepted",
                    "summary": f"sanitized fixture event {turn_number} seed {seed}",
                },
            )

    file_count = sum(1 for path in fixture_root.rglob("*") if path.is_file())
    token = f"fixture-{secrets.token_urlsafe(18)}"
    config_toml = f"""[server]
bind_host = "127.0.0.1"
auth_token = "{token}"

[control_plane]
managed_projects_root = "{managed_root}"
project_registry_path = "{config_dir / "projects.json"}"
config_audit_path = "{config_dir / "config_audit.jsonl"}"
aflow_executable = "{executable}"
release_identity = "fixture-source"
environment_file = "{environment_file}"
"""
    write_text(config_dir / "config.toml", config_toml)
    return FixtureBundle(
        root=fixture_root,
        home=home,
        config_dir=config_dir,
        project_root=project_root,
        token=token,
        project_id="fixture-project",
        selected_run_id=selected_run_id,
        representative_run_id=representative_run_id,
        run_count=spec.run_count,
        selected_event_count=spec.selected_event_count,
        other_event_count=spec.other_event_count,
        file_count=file_count,
    )


def global_visible_run_id(fixture: FixtureBundle) -> str:
    """Choose the newest row that the bounded global list renders."""

    width = len(fixture.selected_run_id.rsplit("-", 1)[-1])
    return f"run-{fixture.run_count - 1:0{width}d}"


class IsolatedServer:
    """Own exactly one disposable child server process."""

    def __init__(self, fixture: FixtureBundle, port: int) -> None:
        self.fixture = fixture
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("isolated server already started")
        if not WEB_DIST.is_dir():
            raise RuntimeError(
                f"web build is missing at {WEB_DIST}; run npm --prefix apps/aflow_app/web run build"
            )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.fixture.home),
                "AFLOW_APP_CONFIG_DIR": str(self.fixture.config_dir),
                "AFLOW_APP_HOST": "127.0.0.1",
                "AFLOW_APP_PORT": str(self.port),
                "AFLOW_APP_TOKEN": self.fixture.token,
                "AFLOW_MANAGED_PROJECTS_ROOT": str(self.fixture.root / "managed"),
                "AFLOW_PROJECT_REGISTRY_PATH": str(self.fixture.config_dir / "projects.json"),
                "AFLOW_CONFIG_AUDIT_PATH": str(self.fixture.config_dir / "config_audit.jsonl"),
                "AFLOW_EXECUTABLE": str(self.fixture.root / "release" / "bin" / "aflow"),
                "AFLOW_ENVIRONMENT_FILE": str(self.fixture.root / "aflowd.env"),
                "AFLOW_RELEASE_IDENTITY": "fixture-source",
                "AFLOW_APP_WEB_DIST": str(WEB_DIST),
                "PYTHONPATH": os.pathsep.join(
                    [str(SERVER_SRC), str(REPO_ROOT), env.get("PYTHONPATH", "")]
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "uvicorn",
                "aflow_app_server.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "error",
                "--no-access-log",
            ),
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        wait_for_server(self.url, self.process)

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("isolated server exited before becoming ready")
        try:
            response = http_json(url + "/health", token=None)
            if response.status == 200:
                return
        except (OSError, RuntimeError):
            pass
        time.sleep(0.05)
    raise RuntimeError("timed out waiting for isolated server")


@dataclass(frozen=True)
class HttpJsonResult:
    status: int
    payload: Any


def http_json(url: str, token: str | None) -> HttpJsonResult:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read()
            payload = json.loads(body) if body else None
            return HttpJsonResult(int(response.status), payload)
    except HTTPError as exc:
        # Do not read or print the error body; it can contain deployment data.
        return HttpJsonResult(int(exc.code), None)
    except (URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"request unavailable: {type(exc).__name__}") from exc


def get_live_identity(
    base_url: str,
    token: str,
    project_id: str | None,
    run_id: str | None,
) -> tuple[str, str]:
    projects = http_json(base_url + "/api/control-plane/projects", token)
    if projects.status != 200 or not isinstance(projects.payload, Mapping):
        raise RuntimeError(f"could not read owner project list (HTTP {projects.status})")
    values = projects.payload.get("projects")
    if not isinstance(values, list):
        raise RuntimeError("owner project response had no bounded project list")
    chosen_project = project_id
    if chosen_project is None:
        for project in values:
            if isinstance(project, Mapping) and isinstance(project.get("id"), str):
                chosen_project = project["id"]
                break
    if not chosen_project:
        raise RuntimeError("no project ID available; pass --project-id")
    runs = http_json(
        base_url
        + f"/api/control-plane/projects/{chosen_project}/runs?history=all&limit=100",
        token,
    )
    if runs.status != 200 or not isinstance(runs.payload, Mapping):
        raise RuntimeError(f"could not read owner run list (HTTP {runs.status})")
    run_values = runs.payload.get("runs")
    chosen_run = run_id
    if chosen_run is None and isinstance(run_values, list):
        for run in run_values:
            if isinstance(run, Mapping) and isinstance(run.get("run_id"), str):
                chosen_run = run["run_id"]
                break
    if not chosen_run:
        raise RuntimeError("no run ID available; pass --selected-run-id")
    return chosen_project, chosen_run


def profile_init_script() -> str:
    return r"""
(() => {
  const state = {
    fetches: [],
    long_tasks: [],
    run_detail_mutation_times: [],
    run_detail_data_mutation_times: [],
    action_start_ms: performance.now(),
    accepted_useful_ms: null,
    accepted_generation: null,
    status_values: Object.create(null),
    nextId: 0,
  };
  window.__aflowLatencyProfile = state;
  window.__aflowLatencyMarkAction = () => {
    state.action_start_ms = performance.now();
    state.accepted_useful_ms = null;
    return state.action_start_ms;
  };
  window.__aflowLatencyMarkAcceptedUseful = () => {
    state.accepted_useful_ms = performance.now();
    return state.accepted_useful_ms;
  };
  window.__aflowLatencyReset = () => {
    state.fetches = [];
    state.long_tasks = [];
    state.run_detail_mutation_times = [];
    state.run_detail_data_mutation_times = [];
    state.status_values = Object.create(null);
    state.accepted_generation = null;
    state.nextId = 0;
    window.__aflowLatencyMarkAction();
  };
  window.__aflowLatencySnapshot = () => ({
    fetches: state.fetches.slice(0, 500),
    long_tasks: state.long_tasks.slice(0, 100),
    run_detail_mutation_times: state.run_detail_mutation_times.slice(-100),
    run_detail_data_mutation_times: state.run_detail_data_mutation_times.slice(-100),
    action_start_ms: state.action_start_ms,
    accepted_useful_ms: state.accepted_useful_ms,
    performance_measures: performance.getEntriesByType('measure')
      .map(entry => ({
        name: String(entry.name).slice(0, 128),
        start_ms: entry.startTime,
        duration_ms: entry.duration,
      }))
      .slice(-100),
    paint_entries: performance.getEntriesByType('paint')
      .map(entry => ({
        name: String(entry.name).slice(0, 128),
        start_ms: entry.startTime,
        duration_ms: entry.duration,
      }))
      .slice(-20),
  });
  try {
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        state.long_tasks.push({
          start_ms: entry.startTime,
          duration_ms: entry.duration,
          attribution_count: Array.isArray(entry.attribution)
            ? entry.attribution.length
            : null,
        });
      }
    }).observe({ type: 'longtask', buffered: true });
  } catch (_) {
    // Chromium supports this observer; other engines report an empty set.
  }
  try {
    new MutationObserver(mutations => {
      for (const mutation of mutations) {
        const target = mutation.target;
        const element = target instanceof Element ? target : target.parentElement;
        if (element?.closest('section.run-detail')) {
          state.run_detail_mutation_times.push(performance.now());
          if (element?.closest(
            '.run-progress-header .status-pill, .checkpoint-history-summary, '
            + '.checkpoint-history-entry, .run-scan-summary, .run-progress-strip, '
            + '.run-timeline, .timeline-event'
          )) {
            state.run_detail_data_mutation_times.push(performance.now());
          }
        }
      }
      if (state.run_detail_mutation_times.length > 200) {
        state.run_detail_mutation_times = state.run_detail_mutation_times.slice(-100);
      }
    }).observe(document, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  } catch (_) {
    // A document-level observer is unavailable in a non-browser probe.
  }
  const nativeFetch = window.fetch.bind(window);
  const inputUrl = input => typeof input === 'string' ? input : (input?.url ?? '');
  const inputMethod = (input, init) => String(init?.method ?? input?.method ?? 'GET');
  const pathFor = item => {
    try { return new URL(String(item?.url || ''), window.location.href).pathname; }
    catch (_) { return ''; }
  };
  const latestFetch = (fetches, suffix) => {
    let latest = null;
    for (const item of fetches) {
      if (!item || !pathFor(item).endsWith(suffix)) continue;
      if (!latest || Number(item.id) > Number(latest.id)) latest = item;
    }
    return latest;
  };
  const completedFetch = item => Boolean(
    item && !item.fetch_error && !item.json_error && item.json_end_ms != null
  );
  const normalized = value => String(value ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const statusMatches = (value, label) => {
    const aliases = {
      done: 'completed',
      ownerstopped: 'stopped',
      needsattention: 'needsattention',
      manifestonly: 'starting',
      launchrequested: 'starting',
      unitstarted: 'starting',
      launchstarted: 'starting',
      awaitingstartupanswer: 'inputneeded',
    };
    const expected = aliases[normalized(value)] ?? normalized(value);
    const actual = normalized(label);
    if (expected === 'failed') return actual === 'failed' || actual === 'couldnotstart';
    return actual === expected;
  };
  window.__aflowLatencySelectedGeneration = (runId, includeList = false) => {
    const id = String(runId || '');
    const accepted = window.__aflowLatencyProfile?.accepted_generation;
    if (accepted && accepted.runId === id &&
        (!includeList || accepted.includeList)) return {...accepted.result};
    const snapshot = window.__aflowLatencySnapshot?.() || {};
    const fetches = Array.isArray(snapshot.fetches) ? snapshot.fetches : [];
    const stateStatusValues = window.__aflowLatencyProfile?.status_values || {};
    const status = latestFetch(fetches, '/runs/' + id);
    const events = latestFetch(fetches, '/runs/' + id + '/events');
    const list = includeList ? latestFetch(fetches, '/runs') : null;
    const required = [status, events, ...(includeList ? [list] : [])];
    const requiredComplete = required.every(completedFetch);
    const requiredEnds = required
      .filter(completedFetch)
      .map(item => Number(item.json_end_ms))
      .filter(value => Number.isFinite(value));
    const latestRequiredEnd = requiredEnds.length ? Math.max(...requiredEnds) : null;
    const detail = document.querySelector('section.run-detail');
    const identity = detail?.querySelector('[title="Copy run ID"]');
    const statusPill = detail?.querySelector('.run-progress-header .status-pill');
    const statusAccepted = completedFetch(status) &&
      typeof stateStatusValues[String(status.id)] === 'string' &&
      statusMatches(stateStatusValues[String(status.id)], statusPill?.textContent);
    const dataMutations = Array.isArray(snapshot.run_detail_data_mutation_times)
      ? snapshot.run_detail_data_mutation_times
      : [];
    const renderedAfterRequiredResponses = latestRequiredEnd != null &&
      dataMutations.some(value => Number(value) >= latestRequiredEnd);
    const identityMatches = identity?.textContent?.trim() === id;
    const visibleCurrentStatus = Boolean(identityMatches &&
      statusPill?.textContent?.trim());
    // The direct status field agreeing with the already-rendered selected
    // identity is the observable accepted-rendering boundary. React may reuse
    // the existing DOM when a refresh returns unchanged data, so a fresh
    // semantic mutation is retained as evidence but is not required for an
    // otherwise current status/events generation.
    const acceptedRendering = visibleCurrentStatus;
    const acceptedResult = {
      accepted: Boolean(requiredComplete && statusAccepted &&
        acceptedRendering && visibleCurrentStatus),
      required_complete: requiredComplete,
      status_accepted: statusAccepted,
      rendered_after_required: renderedAfterRequiredResponses,
    };
    if (acceptedResult.accepted) {
      window.__aflowLatencyProfile.accepted_generation = {
        runId: id,
        includeList: Boolean(includeList),
        generationKey: [
          includeList ? (list?.id ?? '-') : '-',
          status?.id ?? '-',
          events?.id ?? '-',
        ].join(':'),
        result: acceptedResult,
      };
    }
    return acceptedResult;
  };
  window.fetch = async (input, init) => {
    const item = {
      id: state.nextId++,
      url: inputUrl(input),
      method: inputMethod(input, init),
      fetch_start_ms: performance.now(),
      response_resolved_ms: null,
      json_start_ms: null,
      json_end_ms: null,
      json_error: false,
      fetch_error: false,
    };
    state.fetches.push(item);
    try {
      const response = await nativeFetch(input, init);
      item.response_resolved_ms = performance.now();
      const originalJson = response.json.bind(response);
      response.json = async () => {
        item.json_start_ms = performance.now();
        try {
          const value = await originalJson();
          if (value && typeof value === 'object' && typeof value.status === 'string') {
            state.status_values[String(item.id)] = value.status;
          }
          item.json_end_ms = performance.now();
          return value;
        } catch (error) {
          item.json_error = true;
          throw error;
        }
      };
      return response;
    } catch (error) {
      item.fetch_error = true;
      throw error;
    }
  };
})();
"""


def base_route(
    base_url: str,
    *,
    view: str,
    project_id: str | None = None,
    run_id: str | None = None,
) -> str:
    query = [("view", view)]
    if project_id is not None:
        query.insert(0, ("project", project_id))
    if run_id is not None:
        query.append(("run", run_id))
    return f"{base_url.rstrip('/')}/?{urlencode(query)}"


def route_specs(
    base_url: str,
    project_id: str,
    run_id: str,
    *,
    global_run_id: str | None = None,
) -> list[RouteSpec]:
    global_identity = global_run_id or run_id
    return [
        RouteSpec(
            "all_runs",
            base_route(base_url, view="all-runs"),
            """(id) => [...document.querySelectorAll('.global-run-row')].some(row => {
                const text = row.textContent || '';
                return text.includes('Plan:') && text.includes('Run: ' + id);
            })""",
            global_identity,
            "actual global row containing plan and run identity",
        ),
        RouteSpec(
            "project_runs",
            base_route(base_url, view="runs", project_id=project_id),
            """(id) => {
                const row = document.querySelector('[data-sidebar-editor-item="' + CSS.escape(id) + '"]');
                return Boolean(row && row.textContent?.includes('Plan:') && row.textContent?.includes('Run: ' + id));
            }""",
            run_id,
            "actual project run row containing plan and run identity",
        ),
        RouteSpec(
            "selected_run_summary",
            base_route(base_url, view="runs", project_id=project_id, run_id=run_id),
            """(id) => {
                return Boolean(window.__aflowLatencySelectedGeneration?.(id)?.accepted);
            }""",
            run_id,
            "accepted selected-run status and event generation with current rendering",
        ),
        RouteSpec(
            "checkpoint_history",
            base_route(base_url, view="runs", project_id=project_id, run_id=run_id),
            """(id) => {
                const generation = window.__aflowLatencySelectedGeneration?.(id);
                const history = document.querySelector('section.checkpoint-history[aria-label="Checkpoint history"]');
                const identity = document.querySelector('section.run-detail [title="Copy run ID"]');
                const summary = history?.querySelector('.checkpoint-history-summary');
                const values = summary
                    ? [...summary.querySelectorAll('dd')].map(item => item.textContent?.trim() || '')
                    : [];
                const actualValue = values.some(value => /\\d/.test(value) && !/not reported/i.test(value));
                const returnedEntry = history?.querySelectorAll('.checkpoint-history-entry').length > 0;
                const hasEmptyState = /not available|no checkpoint entries/i.test(history?.textContent || '');
                return Boolean(generation?.accepted && history && identity?.textContent?.trim() === id &&
                    history.textContent?.includes('Checkpoint progress') &&
                    history.textContent?.includes('Checkpoints') && summary &&
                    values.length >= 3 && actualValue && (returnedEntry || values.some(value => /checkpoint/i.test(value))) &&
                    !hasEmptyState);
            }""",
            run_id,
            "selected-run checkpoint summary with returned record values",
        ),
        RouteSpec(
            "settings_initial",
            base_route(base_url, view="settings"),
            """() => {
                const body = document.querySelector('.settings-body');
                const profile = document.querySelector('.profile-editor-name strong');
                const textarea = document.querySelector('textarea[aria-label="aflow.toml contents"]');
                return Boolean(body && ((profile && profile.textContent?.trim()) || (textarea && textarea.value.trim())));
            }""",
            None,
            "loaded settings data (profile identity or non-empty configuration)",
        ),
    ]


REFRESH_GENERATION_EXPRESSION = """(runId) => {
    const generation = window.__aflowLatencySelectedGeneration?.(runId, true);
    const history = document.querySelector('section.checkpoint-history[aria-label="Checkpoint history"]');
    const summary = history?.querySelector('.checkpoint-history-summary');
    const values = summary
        ? [...summary.querySelectorAll('dd')].map(item => item.textContent?.trim() || '')
        : [];
    const historyAccepted = Boolean(history && summary && values.length >= 3 &&
        values.some(value => /\\d/.test(value) && !/not reported/i.test(value)) &&
        (history.querySelectorAll('.checkpoint-history-entry').length > 0 || values.some(value => /checkpoint/i.test(value))) &&
        !/not available|no checkpoint entries/i.test(history.textContent || ''));
    // Do not require an enabled button here.  The dashboard may have an
    // unrelated periodic/event-driven refresh in flight while this explicit
    // generation settles; the request set was reset immediately before the
    // More > Refresh action and the selected-generation helper is the
    // acceptance boundary.
    return Boolean(generation?.accepted && historyAccepted);
}"""


def refresh_generation_status(
    flow: Sequence[Mapping[str, Any]], run_id: str, *, rendered_state_accepted: bool
) -> dict[str, Any]:
    """Validate the latest explicit-refresh request generation."""

    expected = {
        "list": f"/runs",
        "status": f"/runs/{run_id}",
        "events": f"/runs/{run_id}/events",
    }
    latest: dict[str, Mapping[str, Any]] = {}
    for item in flow:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path", "")).split("?", 1)[0]
        if path.endswith(expected["events"]):
            latest["events"] = item
        elif path.endswith(expected["status"]):
            latest["status"] = item
        elif path.endswith(expected["list"]):
            latest["list"] = item
    required = tuple(expected)
    completed = {
        name: str(latest[name].get("path", ""))
        for name in required
        if name in latest
        and not latest[name].get("fetch_error")
        and not latest[name].get("json_error")
        and latest[name].get("json_end_ms") is not None
    }
    return {
        "required_paths": [expected[name] for name in required],
        "latest_paths": [
            str(latest[name].get("path", "")) for name in required if name in latest
        ],
        "completed_paths": [completed[name] for name in required if name in completed],
        "required_completed_count": len(completed),
        "required_total": len(required),
        "request_generation_complete": len(completed) == len(required),
        "rendered_state_accepted": rendered_state_accepted,
        "accepted": len(completed) == len(required) and rendered_state_accepted,
    }


def wait_shell(page: Any) -> None:
    page.locator(".app-header").wait_for(state="visible", timeout=WAIT_TIMEOUT_MS)


def mark_accepted_useful(page: Any) -> float:
    value = finite_number(
        page.evaluate("() => window.__aflowLatencyMarkAcceptedUseful?.() ?? null")
    )
    if value is None:
        raise RuntimeError("browser accepted-useful marker unavailable")
    return value


def wait_useful(page: Any, route: RouteSpec) -> float:
    page.wait_for_function(
        route.useful_expression,
        arg=route.useful_argument,
        timeout=(
            SCALE_DATA_TIMEOUT_MS
            if route.name in {"all_runs", "project_runs"}
            else WAIT_TIMEOUT_MS
        ),
    )
    return mark_accepted_useful(page)


def login(page: Any, base_url: str, token: str) -> None:
    page.goto(
        base_url.rstrip("/") + "/",
        wait_until="domcontentloaded",
        timeout=WAIT_TIMEOUT_MS,
    )
    password = page.locator('input[type="password"]')
    if password.count() > 0:
        password.first.fill(token)
        page.get_by_role("button", name="Login", exact=True).click()
    wait_shell(page)
    page.evaluate("window.__aflowLatencyReset?.()")


def capture_route(page: Any, capture: BrowserCapture, route: RouteSpec) -> dict[str, Any]:
    capture.reset()
    started = time.perf_counter()
    result: dict[str, Any] = {
        "route": route.name,
        "path": canonical_url(route.path, route.path),
        "milestone": route.milestone,
        "navigation_started_at": datetime.now(timezone.utc).isoformat(),
        "navigation_to_shell_ms": None,
        "useful_data_ready_ms": None,
        "useful_after_shell_ms": None,
        "error": None,
    }
    shell_at: float | None = None
    try:
        page.goto(route.path, wait_until="domcontentloaded", timeout=WAIT_TIMEOUT_MS)
        wait_shell(page)
        shell_at = time.perf_counter()
        result["navigation_to_shell_ms"] = round((shell_at - started) * 1000, 3)
        wait_useful(page, route)
        useful_at = time.perf_counter()
        result["useful_data_ready_ms"] = round((useful_at - started) * 1000, 3)
        result["useful_after_shell_ms"] = round((useful_at - shell_at) * 1000, 3)
    except Exception as exc:
        result["error"] = safe_error(exc)
    result.update(capture.snapshot())
    return result


def capture_refresh(
    page: Any, capture: BrowserCapture, run_id: str
) -> dict[str, Any]:
    capture.reset()
    started = time.perf_counter()
    result: dict[str, Any] = {
        "route": "explicit_refresh",
        "path": "current selected-run route",
        "milestone": "selected-run useful data after More > Refresh",
        "refresh_to_useful_ms": None,
        "refresh_generation": None,
        "error": None,
    }
    try:
        page.locator(".app-header-row-two").get_by_role(
            "button", name="More", exact=True
        ).click()
        page.get_by_role("menuitem", name="Refresh", exact=True).click()
        page.wait_for_function(
            REFRESH_GENERATION_EXPRESSION,
            arg=run_id,
            timeout=SCALE_DATA_TIMEOUT_MS,
        )
        mark_accepted_useful(page)
        result["refresh_to_useful_ms"] = round(
            (time.perf_counter() - started) * 1000, 3
        )
    except Exception as exc:
        result["error"] = safe_error(exc)
    result.update(capture.snapshot())
    flow = result.get("api_request_flow")
    result["refresh_generation"] = refresh_generation_status(
        flow if isinstance(flow, list) else [],
        run_id,
        rendered_state_accepted=result["error"] is None,
    )
    return result


def capture_in_app_navigation(
    page: Any,
    capture: BrowserCapture,
    route: RouteSpec,
    selected_run_id: str,
    alternate_run_id: str,
    cycle: int,
    counterfactual: bool,
) -> dict[str, Any]:
    """Measure selection changes without reloading the document."""

    capture.reset()
    started = time.perf_counter()
    result: dict[str, Any] = {
        "route": route.name,
        "path": canonical_url(route.path, route.path),
        "milestone": "selected-run useful data after two in-app run selections",
        "navigation_started_at": datetime.now(timezone.utc).isoformat(),
        "navigation_to_shell_ms": None,
        "useful_data_ready_ms": None,
        "useful_after_shell_ms": None,
        "navigation_mode": "in_app_selection",
        "document_reload": False,
        "selection_sequence": [alternate_run_id, selected_run_id],
        "navigation_cycle": cycle,
        "counterfactual": counterfactual,
        "error": None,
    }
    try:
        page.locator(
            f'[data-sidebar-editor-item="{alternate_run_id}"]'
        ).click()
        alternate_route = RouteSpec(
            route.name,
            route.path,
            route.useful_expression,
            alternate_run_id,
            route.milestone,
        )
        wait_useful(page, alternate_route)
        page.locator(
            f'[data-sidebar-editor-item="{selected_run_id}"]'
        ).click()
        wait_useful(page, route)
        useful_at = time.perf_counter()
        result["useful_data_ready_ms"] = round((useful_at - started) * 1000, 3)
        result["useful_after_shell_ms"] = result["useful_data_ready_ms"]
    except Exception as exc:
        result["error"] = safe_error(exc)
    result.update(capture.snapshot())
    return result


def browser_context_sample(
    playwright: Any,
    browser_name: str,
    base_url: str,
    token: str,
    project_id: str,
    run_id: str,
    repeats: int,
    navigation_cycles: int,
    sample_kind: str,
    block_optional_requests: bool = False,
    global_run_id: str | None = None,
) -> list[dict[str, Any]]:
    browser_type = getattr(playwright, browser_name)
    launch_options: dict[str, Any] = {"headless": True}
    if browser_name == "chromium":
        launch_options["args"] = ["--no-sandbox"]
    browser = browser_type.launch(**launch_options)
    try:
        context = browser.new_context()
        if block_optional_requests:
            def handle_counterfactual_route(route: Any) -> None:
                path = canonical_url(str(route.request.url), base_url).split("?", 1)[0]
                if any(path.endswith(suffix) for suffix in OPTIONAL_COUNTERFACTUAL_SUFFIXES):
                    route.abort(error_code="blockedbyclient")
                else:
                    route.continue_()

            context.route("**/api/**", handle_counterfactual_route)
        context.add_init_script(profile_init_script())
        page = context.new_page()
        capture = BrowserCapture(page, base_url)
        login(page, base_url, token)
        routes = route_specs(
            base_url,
            project_id,
            run_id,
            global_run_id=global_run_id,
        )
        samples: list[dict[str, Any]] = []
        for repeat in range(repeats):
            for route in routes:
                sample = capture_route(page, capture, route)
                sample.update(
                    {
                        "sample_kind": sample_kind,
                        "repeat": repeat + 1,
                        "navigation_cycle": None,
                        "counterfactual": block_optional_requests,
                    }
                )
                samples.append(sample)
                if route.name == "checkpoint_history":
                    refresh_sample = capture_refresh(page, capture, run_id)
                    refresh_sample.update(
                        {
                            "sample_kind": sample_kind,
                            "repeat": repeat + 1,
                            "navigation_cycle": None,
                            "counterfactual": block_optional_requests,
                        }
                    )
                    samples.append(refresh_sample)
        if navigation_cycles:
            route = routes[2]
            # Establish the project-run list once.  Each bounded cycle below
            # is then a real client-side selection change, not a document
            # reload, and its capture is reset independently.
            page.goto(
                routes[1].path,
                wait_until="domcontentloaded",
                timeout=WAIT_TIMEOUT_MS,
            )
            wait_shell(page)
            wait_useful(page, routes[1])
            alternate_run_id = "run-0001" if run_id != "run-0001" else "run-0000"
            for cycle in range(navigation_cycles):
                sample = capture_in_app_navigation(
                    page,
                    capture,
                    route,
                    run_id,
                    alternate_run_id,
                    cycle + 1,
                    block_optional_requests,
                )
                sample.update(
                    {
                        "sample_kind": "repeat_navigation",
                        "repeat": None,
                    }
                )
                samples.append(sample)
        context.close()
        return samples
    finally:
        browser.close()


def browser_version(playwright: Any, browser_name: str) -> str:
    browser_type = getattr(playwright, browser_name)
    launch_options: dict[str, Any] = {"headless": True}
    if browser_name == "chromium":
        launch_options["args"] = ["--no-sandbox"]
    browser = browser_type.launch(**launch_options)
    try:
        return str(browser.version)
    finally:
        browser.close()


def summarize_samples(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for sample in samples:
        fixture = str(sample.get("fixture_name", "unlabelled"))
        route = str(sample.get("route", "unknown"))
        kind = str(sample.get("sample_kind", "unknown"))
        groups.setdefault((fixture, route, kind), []).append(sample)
    summaries = []
    fields_by_route = {"explicit_refresh": ["refresh_to_useful_ms"]}
    default_fields = [
        "navigation_to_shell_ms",
        "useful_data_ready_ms",
        "useful_after_shell_ms",
    ]
    for (fixture, route, kind), values in sorted(groups.items()):
        fields = fields_by_route.get(route, default_fields)
        summary: dict[str, Any] = {
            "fixture_name": fixture,
            "route": route,
            "sample_kind": kind,
            "sample_count": len(values),
            "errors": sum(1 for value in values if value.get("error")),
        }
        for field_name in fields:
            numeric = [
                float(value[field_name])
                for value in values
                if isinstance(value.get(field_name), (int, float))
                and not isinstance(value.get(field_name), bool)
            ]
            summary[field_name] = numeric_summary(numeric)
        api_counts = [
            int(value["api_request_count"])
            for value in values
            if isinstance(value.get("api_request_count"), int)
        ]
        summary["api_request_count"] = numeric_summary(api_counts, integer=True)
        summary["duplicate_samples"] = sum(
            1 for value in values if value.get("duplicate_api_paths")
        )
        summaries.append(summary)
    return summaries


def numeric_summary(values: Sequence[float], *, integer: bool = False) -> dict[str, Any]:
    if not values:
        return {"count": 0, "median_ms": None, "min_ms": None, "max_ms": None}
    median = statistics.median(values)
    minimum = min(values)
    maximum = max(values)
    if integer:
        return {
            "count": len(values),
            "median": int(median),
            "min": int(minimum),
            "max": int(maximum),
        }
    return {
        "count": len(values),
        "median_ms": round(median, 3),
        "min_ms": round(minimum, 3),
        "max_ms": round(maximum, 3),
    }


def secret_scan(value: Any, secrets_to_redact: Iterable[str]) -> bool:
    encoded = json.dumps(value, sort_keys=True)
    return not any(secret and secret in encoded for secret in secrets_to_redact)


def replace_private(value: Any, private_values: Iterable[str]) -> Any:
    if isinstance(value, str):
        result = value
        for private in private_values:
            if private:
                result = result.replace(private, "<private-artifact>")
        return result
    if isinstance(value, list):
        return [replace_private(item, private_values) for item in value]
    if isinstance(value, dict):
        return {key: replace_private(item, private_values) for key, item in value.items()}
    return value


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + NL,
        encoding="utf-8",
    )


def report_markdown(report: Mapping[str, Any], command: str) -> str:
    discovery = report["discovery"]
    fixtures = report.get("fixtures") or []
    lines = [
        "# UI data-latency baseline",
        "",
        "This is a bounded discovery baseline for the existing UI. It does not",
        "implement an optimization or claim that useful data became faster.",
        "",
        "## Coverage",
        "",
        f"- Target: {discovery['target']}; runner SHA {discovery['source_sha']}; product source SHA {discovery['product_source_sha']}; deployed SHA {discovery['deployed_sha']}.",
        f"- Host: {discovery['host']['host_label']}; browser {discovery['browser']['name']} {discovery['browser']['version']}.",
        f"- Seed {discovery['seed']}; serial repeats {discovery['repeats']}; repeat-navigation bound {discovery['navigation_cycles']}.",
        f"- Concurrent activity: {discovery['host']['external_workflows']}; live state mutation: {discovery['live_state_mutation']}.",
    ]
    for fixture in fixtures:
        lines.append(
            f"- Fixture {fixture['name']}: {fixture['run_count']} runs, selected run {fixture['selected_run_id']}, global visible row {fixture['global_visible_run_id']}, selected history {fixture['selected_event_count']} events, other histories {fixture['other_event_count']} events, {fixture['file_count']} files."
        )
        lines.append(f"- Fixture identity: {fixture['identity']}.")
    lines.extend(
        [
            "",
            "## Useful-data milestones",
            "",
            "Shell readiness is the visible .app-header. Useful readiness requires real data: an all-runs row with Plan: and Run:, a project row with the same identity, a selected detail whose current status and event request generation is accepted by the rendered UI, checkpoint history with selected-run summary values rather than an empty/loading panel, Settings profile/config content, and the selected detail after the actual More > Refresh request generation completes. Skeletons, headings, old DOM and response arrival without accepted rendering are not counted.",
            "",
            "## Samples",
            "",
            "Only medians and observed ranges are reported; no synthetic percentile is inferred from these bounded samples.",
            "",
            "| Fixture / route / sample kind | n | useful median | observed range | API requests | errors |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for summary in report["summary"]:
        useful_field = (
            "refresh_to_useful_ms"
            if summary["route"] == "explicit_refresh"
            else "useful_data_ready_ms"
        )
        timing = summary[useful_field]
        if timing["count"]:
            timing_text = f"{timing['median_ms']:.1f} ms"
            range_text = f"{timing['min_ms']:.1f}–{timing['max_ms']:.1f} ms"
        else:
            timing_text = "n/a"
            range_text = "n/a"
        api = summary["api_request_count"]
        api_text = str(api["median"]) if api["count"] else "n/a"
        lines.append(
            f"| {summary['fixture_name']} / {summary['route']} / {summary['sample_kind']} | {summary['sample_count']} | {timing_text} | {range_text} | {api_text} | {summary['errors']} |"
        )
    lines.extend(
        [
            "",
            "## Network evidence",
            "",
            "Each raw sample in baseline.json keeps sanitized API/resource paths, request order, request start, DNS/connect/TLS/TTFB/download fields where the browser supplied them, payload-size fields, JSON processing intervals, duplicate paths, cancellations/failures, and errors. Response bodies, request bodies, headers, cookies, prompts, transcripts, tokens, and private fixture paths are excluded.",
            "",
            "## Reproduction",
            "",
            "    " + command.replace(NL, NL + "    "),
            "",
            "The four-second owner symptom is not established by an isolated loopback run. No owner URL, in-memory credential and exact reference run identity were available in this repair, so live coverage is explicitly unavailable rather than measured. The smallest follow-up is one coordinator-owned serial capture on that owner URL (at most three navigations per selected route) with deployed SHA and sanitized dataset bounds; no remote-client timing is inferred from loopback.",
            "",
            "## Validation",
            "",
            f"- Report schema: {report['schema_version']}; raw sample count: {len(report['samples'])}.",
            f"- Secrets absent from shareable output: {report['validation']['secrets_absent']}.",
            f"- Product source mutation observed: {report['validation']['product_source_mutated']}.",
        ]
    )
    return NL.join(lines) + NL


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture bounded sanitized aflow UI data-load timing evidence."
    )
    parser.add_argument(
        "--scenario",
        choices=("live", "isolated-smoke", "isolated-small", "isolated-large", "isolated"),
        default="isolated-smoke",
        help="owner URL or disposable fixture scale (default: isolated-smoke)",
    )
    parser.add_argument("--base-url", help="explicit owner-facing URL for --scenario live")
    parser.add_argument(
        "--auth-token-env",
        default="AFLOW_PROFILE_AUTH_TOKEN",
        help="environment variable containing the live bearer token",
    )
    parser.add_argument(
        "--project-id",
        help="live project ID; otherwise the first project is used",
    )
    parser.add_argument(
        "--selected-run-id",
        help="live selected run ID; otherwise the first run is used",
    )
    parser.add_argument(
        "--deployed-sha",
        default=None,
        help="label for the deployed revision; never inferred",
    )
    parser.add_argument("--browser", choices=("chromium", "webkit"), default="chromium")
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="serial warm samples per route, 1-10",
    )
    parser.add_argument(
        "--navigation-cycles",
        type=int,
        default=None,
        help="bounded selected-route repeat-navigation cycles (10 isolated, 0 live)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="serial-only control; live targets reject values other than 1",
    )
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument(
        "--block-optional-requests",
        action="store_true",
        help="isolated-only counterfactual: abort eager stream/action requests",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="private external directory for raw sanitized samples (default: temporary)",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=REPO_ROOT / "plans" / "research" / "ui-data-latency-20260912",
        help="shareable report directory",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.navigation_cycles is None:
        args.navigation_cycles = 0 if args.scenario == "live" else 10
    if not 1 <= args.repeats <= MAX_REPEATS:
        raise ValueError("--repeats must be between 1 and 10")
    if not 0 <= args.navigation_cycles <= MAX_REPEATS:
        raise ValueError("--navigation-cycles must be between 0 and 10")
    if args.concurrency != 1:
        raise ValueError("discovery is serial-only; --concurrency must remain 1")
    if args.scenario == "live":
        if not args.base_url:
            raise ValueError("--base-url is required for --scenario live")
        if args.repeats > MAX_LIVE_REPEATS:
            raise ValueError("live profiling permits at most three serial repeats")
        if args.navigation_cycles:
            raise ValueError("live profiling uses only the capped warm navigations; repeat-navigation cycles are isolated-only")
        if args.block_optional_requests:
            raise ValueError("--block-optional-requests is isolated-only")
    elif args.base_url:
        raise ValueError("--base-url is only valid for --scenario live")
    if args.scenario != "live" and not WEB_DIST.is_dir():
        raise ValueError(
            f"web build is missing at {WEB_DIST}; run npm --prefix apps/aflow_app/web run build"
        )
    if args.artifact_dir is not None:
        artifact_dir = args.artifact_dir.expanduser().resolve()
        try:
            artifact_dir.relative_to(REPO_ROOT)
        except ValueError:
            pass
        else:
            raise ValueError("--artifact-dir must be outside the repository")


def product_source_snapshot() -> tuple[str, ...]:
    result = subprocess.run(
        (
            "git",
            "-C",
            str(REPO_ROOT),
            "--no-optional-locks",
            "status",
            "--short",
            "--untracked-files=all",
            "--",
            "aflow",
            "apps/aflow_app",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(result.stdout.splitlines())


def collect_scenario(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    from playwright.sync_api import sync_playwright

    sha = source_sha()
    product_sha = product_source_sha()
    samples: list[dict[str, Any]] = []
    fixture_metadata: list[dict[str, Any]] = []
    private_secrets: list[str] = []
    target_label = "owner-facing-url" if args.scenario == "live" else "isolated-loopback"
    server: IsolatedServer | None = None
    fixture_root: tempfile.TemporaryDirectory[str] | None = None
    browser_info: dict[str, str] | None = None
    try:
        if args.scenario == "live":
            token = os.environ.get(args.auth_token_env, "")
            if not token:
                if not sys.stdin.isatty():
                    raise ValueError(
                        f"{args.auth_token_env} is unset and stdin is not interactive; credentials stay in memory"
                    )
                token = getpass.getpass("Live UI bearer token (not recorded): ")
            if not token:
                raise ValueError("a non-empty live bearer token is required")
            private_secrets.append(token)
            base_url = str(args.base_url).rstrip("/")
            project_id, run_id = get_live_identity(
                base_url, token, args.project_id, args.selected_run_id
            )
            private_secrets.extend((project_id, run_id))
            with sync_playwright() as playwright:
                browser_info = {
                    "name": args.browser,
                    "version": browser_version(playwright, args.browser),
                }
                samples.extend(
                    browser_context_sample(
                        playwright,
                        args.browser,
                        base_url,
                        token,
                        project_id,
                        run_id,
                        args.repeats,
                        args.navigation_cycles,
                        "warm",
                        args.block_optional_requests,
                    )
                )
        else:
            fixture_root = tempfile.TemporaryDirectory(prefix="aflow-ui-data-latency-")
            for spec in fixture_specs(args.scenario):
                fixture = build_fixture(spec, args.seed, Path(fixture_root.name))
                fixture_metadata.append(
                    {
                        "name": spec.name,
                        "identity": f"seed={args.seed}; deterministic sanitized plan; fixed selected run; independent event-count variation",
                        "run_count": fixture.run_count,
                        "selected_run_id": fixture.selected_run_id,
                        "global_visible_run_id": global_visible_run_id(fixture),
                        "representative_run_id": fixture.representative_run_id,
                        "selected_event_count": fixture.selected_event_count,
                        "other_event_count": fixture.other_event_count,
                        "file_count": fixture.file_count,
                        "history_shape": spec.history_shape,
                    }
                )
                server = IsolatedServer(fixture, free_port())
                server.start()
                with sync_playwright() as playwright:
                    browser_info = {
                        "name": args.browser,
                        "version": browser_version(playwright, args.browser),
                    }
                    before = len(samples)
                    samples.extend(
                        browser_context_sample(
                            playwright,
                            args.browser,
                            server.url,
                            fixture.token,
                            fixture.project_id,
                            fixture.selected_run_id,
                            args.repeats,
                            args.navigation_cycles,
                            "warm",
                            args.block_optional_requests,
                            global_run_id=global_visible_run_id(fixture),
                        )
                    )
                    for sample in samples[before:]:
                        sample["fixture_name"] = spec.name
                server.stop()
                server = IsolatedServer(fixture, free_port())
                server.start()
                with sync_playwright() as playwright:
                    browser_info = {
                        "name": args.browser,
                        "version": browser_version(playwright, args.browser),
                    }
                    before = len(samples)
                    samples.extend(
                        browser_context_sample(
                            playwright,
                            args.browser,
                            server.url,
                            fixture.token,
                            fixture.project_id,
                            fixture.selected_run_id,
                            1,
                            0,
                            "fresh_process",
                            args.block_optional_requests,
                            global_run_id=global_visible_run_id(fixture),
                        )
                    )
                    for sample in samples[before:]:
                        sample["fixture_name"] = spec.name
                server.stop()
                private_secrets.append(fixture.token)
            project_id = "fixture-project"
            run_id = "run-0000"

        if browser_info is None:
            raise RuntimeError("browser identity was not captured")
        discovery: dict[str, Any] = {
            "source_sha": sha,
            "product_source_sha": product_sha,
            "deployed_sha": args.deployed_sha or ("unknown" if args.scenario == "live" else product_sha),
            "target": target_label,
            "seed": args.seed,
            "repeats": args.repeats,
            "navigation_cycles": args.navigation_cycles,
            "concurrency": args.concurrency,
            "blocked_optional_requests": (
                list(OPTIONAL_COUNTERFACTUAL_SUFFIXES)
                if args.block_optional_requests
                else []
            ),
            "browser": browser_info,
            "host": host_activity(),
            "live_state_mutation": "none observed",
            "project_identity": "synthetic fixture project" if args.scenario != "live" else "owner project ID redacted",
            "selected_run_identity": "synthetic fixture run" if args.scenario != "live" else "owner run ID redacted",
            "live_coverage": {
                "status": "measured" if args.scenario == "live" else "not_requested",
                "owner_url_used": args.scenario == "live",
                "owner_credentials_used": args.scenario == "live",
                "four_second_symptom": "not assessed" if args.scenario != "live" else "not separately classified",
            },
        }
        if fixture_metadata:
            discovery["fixtures"] = fixture_metadata
        return discovery, samples, private_secrets
    finally:
        if server is not None:
            server.stop()
        if fixture_root is not None:
            fixture_root.cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        report_dir = args.report_dir.expanduser().resolve()
        report_dir.mkdir(parents=True, exist_ok=True)
        owned_artifacts = args.artifact_dir is None
        artifact_dir = (
            Path(tempfile.mkdtemp(prefix="aflow-ui-data-latency-artifacts-"))
            if owned_artifacts
            else args.artifact_dir.expanduser().resolve()
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        source_before = product_source_snapshot()
        discovery, samples, private_secrets = collect_scenario(args)
        source_after = product_source_snapshot()
        raw_payload: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "kind": "ui_data_latency_baseline",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "discovery": discovery,
            "fixtures": discovery.get("fixtures", []),
            "samples": samples,
            "summary": summarize_samples(samples),
            "validation": {
                "secrets_absent": True,
                "product_source_mutated": source_before != source_after,
                "shareable_paths_redacted": True,
            },
        }
        private_values = [
            *private_secrets,
            str(REPO_ROOT),
            str(artifact_dir),
            str(report_dir),
        ]
        report_payload = replace_private(raw_payload, private_values)
        report_payload["validation"]["secrets_absent"] = secret_scan(
            report_payload, private_secrets
        )
        raw_path = artifact_dir / "samples.json"
        write_json(raw_path, report_payload)
        baseline_path = report_dir / "baseline.json"
        write_json(baseline_path, report_payload)
        command = " ".join(
            [
                "uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py",
                f"--scenario {args.scenario}",
                f"--repeats {args.repeats}",
                f"--navigation-cycles {args.navigation_cycles}",
                "--artifact-dir <artifact-dir>",
                "--report-dir plans/research/ui-data-latency-20260912",
            ]
        )
        (report_dir / "report.md").write_text(
            report_markdown(report_payload, command),
            encoding="utf-8",
        )
        print(f"baseline: {baseline_path}")
        print(f"report: {report_dir / 'report.md'}")
        print(f"raw samples: {raw_path}")
        if owned_artifacts:
            print(f"private artifact directory: {artifact_dir}")
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

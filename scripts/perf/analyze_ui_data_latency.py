#!/usr/bin/env python3
"""Merge CP2 profiles and browser observations into the discovery evidence.

The input files are produced by the bounded CP1/CP2 runners.  This command
does analysis only: it never edits product source, fixture state, caches or
services.  It keeps browser request durations overlap-aware by taking the
latest required completion instead of adding concurrent requests together.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics
import subprocess
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NL = "\n"
REFRESH_BLOCKING_PREREQUISITE_PATHS = frozenset(
    {"/ready", "/api/config", "/api/config/form"}
)


def number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        if value == value and abs(value) != float("inf"):
            return value
    return None


def numeric_summary(values: Iterable[float], *, integer: bool = False) -> dict[str, Any]:
    values = list(values)
    if not values:
        return {
            "count": 0,
            "median": None if integer else None,
            "min": None if integer else None,
            "max": None if integer else None,
        }
    median = statistics.median(values)
    if integer:
        return {
            "count": len(values),
            "median": int(median),
            "min": int(min(values)),
            "max": int(max(values)),
        }
    return {
        "count": len(values),
        "median_ms": round(median, 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + NL, encoding="utf-8")


def resolved_product_source_sha() -> str:
    """Resolve the current committed product revision for analyzer inputs."""

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
            "aflow",
            "apps/aflow_app",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise ValueError("product source is dirty; analyzer requires the committed source")
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
    if not PRODUCT_SHA_RE.fullmatch(value):
        raise ValueError("could not determine the committed product source revision")
    return value


def load_historical_baseline(input_path: Path, report_dir: Path) -> dict[str, Any]:
    """Load the prior canonical report so changed browser evidence is retained."""

    candidate = (report_dir / "baseline.json").resolve()
    if candidate == input_path.expanduser().resolve() or not candidate.is_file():
        return {}
    previous = load_json(candidate)
    if previous.get("kind") != "ui_data_latency_baseline":
        return {}
    return previous


def audit_baseline(value: Mapping[str, Any]) -> dict[str, Any]:
    """Retain one prior canonical baseline without recursively copying audits."""

    return {
        key: item
        for key, item in value.items()
        if key != "superseded_evidence"
    }


def path_without_query(path: str) -> str:
    return path.split("?", 1)[0]


def request_role(route: str, path: str) -> str:
    """Classify requests using the actual route owners in the web source."""

    base = path_without_query(path)
    if route == "explicit_refresh" and (
        base in REFRESH_BLOCKING_PREREQUISITE_PATHS
        or base.endswith("/capabilities")
        or base.endswith("/plans")
    ):
        # refreshPage awaits loadDashboard, whose Promise.all and committed
        # config projection both settle before loadSelectedRun starts.
        return "blocking_prerequisite"
    if base.endswith("/events/stream"):
        return "optional_event_stream"
    if base.endswith("/restart-options"):
        return "optional_action_metadata"
    if base.endswith("/context"):
        return "optional_diagnostics_context"
    if route == "settings_initial":
        if base in {"/api/config", "/api/config/form", "/api/settings"}:
            return "required_data"
        return "support"
    if route == "all_runs":
        if base == "/api/control-plane/projects" or base.endswith("/runs"):
            return "required_data"
    elif route in {"project_runs", "selected_run_summary", "checkpoint_history", "explicit_refresh"}:
        if (
            base.endswith("/runs")
            or "/runs/" in base
            and not base.endswith("/preflight")
            and not base.endswith("/archive")
            and not base.endswith("/restore")
            and not base.endswith("/events")
            and not base.endswith("/context")
        ):
            return "required_data"
        if base.endswith("/events"):
            return "required_data"
    if base in {"/api/session", "/api/projects", "/ready", "/api/control-plane/projects"}:
        return "support"
    if base.endswith("/capabilities") or base.endswith("/plans"):
        return "support"
    if base in {"/api/config", "/api/config/form", "/api/settings"}:
        return "optional_configuration"
    return "other"


def action_relative(item: Mapping[str, Any], field: str) -> float | None:
    """Read a producer-normalized browser timestamp, never infer its origin."""

    return number(item.get(f"{field}_from_action_ms"))


def flow_end(item: Mapping[str, Any]) -> float | None:
    json_end = action_relative(item, "json_end")
    if json_end is not None:
        return json_end
    return action_relative(item, "response_resolved")


def sample_useful_ms(sample: Mapping[str, Any]) -> float | None:
    route = str(sample.get("route", ""))
    field = "refresh_to_useful_ms" if route == "explicit_refresh" else "useful_data_ready_ms"
    return number(sample.get(field))


def browser_useful_ms(sample: Mapping[str, Any]) -> float | None:
    """Return the accepted-useful point relative to the explicit browser action."""

    direct = number(sample.get("browser_useful_after_action_ms"))
    if direct is not None:
        return direct
    accepted = number(sample.get("browser_accepted_useful_ms"))
    action = number(sample.get("browser_action_start_ms"))
    if accepted is None or action is None:
        return None
    return round(accepted - action, 3)


def max_in_flight(intervals: Sequence[tuple[float, float]]) -> int:
    points: list[tuple[float, int]] = []
    for start, end in intervals:
        points.extend(((start, 1), (end, -1)))
    active = 0
    maximum = 0
    for _time, delta in sorted(points, key=lambda point: (point[0], point[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def analyze_sample(sample: Mapping[str, Any]) -> dict[str, Any]:
    route = str(sample.get("route", "unknown"))
    flow = sample.get("api_request_flow")
    flow_items = flow if isinstance(flow, list) else []
    required_data: list[tuple[Mapping[str, Any], float]] = []
    blocking_prerequisites: list[tuple[Mapping[str, Any], float]] = []
    optional: list[tuple[Mapping[str, Any], float]] = []
    intervals: list[tuple[float, float]] = []
    parse_durations: list[float] = []
    for raw_item in flow_items:
        if not isinstance(raw_item, Mapping):
            continue
        path = str(raw_item.get("path", ""))
        end = flow_end(raw_item)
        start = action_relative(raw_item, "fetch_start")
        if start is not None and end is not None and end >= start:
            intervals.append((start, end))
        json_start = number(raw_item.get("json_start_ms"))
        json_end = number(raw_item.get("json_end_ms"))
        if json_start is not None and json_end is not None and json_end >= json_start:
            parse_durations.append(json_end - json_start)
        if end is None:
            continue
        role = request_role(route, path)
        if role == "required_data":
            required_data.append((raw_item, end))
        elif role == "blocking_prerequisite":
            blocking_prerequisites.append((raw_item, end))
        elif role.startswith("optional"):
            optional.append((raw_item, end))

    action_required = [*required_data, *blocking_prerequisites]
    data_barrier = max((end for _item, end in required_data), default=None)
    blocking_prerequisite_barrier = max(
        (end for _item, end in blocking_prerequisites), default=None
    )
    required_barrier = max((end for _item, end in action_required), default=None)
    useful = sample_useful_ms(sample)
    browser_useful = browser_useful_ms(sample)
    network = sample.get("network")
    network_items = network if isinstance(network, list) else []
    api_network = [
        item
        for item in network_items
        if isinstance(item, Mapping) and str(item.get("path", "")).startswith("/api/")
    ]
    payloads = [
        value
        for item in api_network
        for value in (number(item.get("decoded_body_size_bytes")),)
        if value is not None
    ]
    long_tasks = sample.get("main_thread_long_tasks")
    long_task_items = long_tasks if isinstance(long_tasks, list) else []
    long_task_durations = [
        value
        for item in long_task_items
        if isinstance(item, Mapping)
        for value in (number(item.get("duration_ms")),)
        if value is not None
    ]
    measures = sample.get("performance_measures")
    measure_items = measures if isinstance(measures, list) else []
    named_measures = [
        str(item.get("name"))[:128]
        for item in measure_items
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        and re.search(r"parse|react|commit", str(item.get("name")), re.IGNORECASE)
    ]
    cache_header_keys = (
        "cache_control_present",
        "age_header_present",
        "etag_present",
    )
    cache_headers_available = any(
        key in item
        for item in network_items
        if isinstance(item, Mapping)
        for key in cache_header_keys
    )
    cache_presence = {
        key: sum(1 for item in network_items if isinstance(item, Mapping) and item.get(key) is True)
        for key in cache_header_keys
    }
    duplicate_paths = sample.get("duplicate_api_paths")
    duplicate_count = len(duplicate_paths) if isinstance(duplicate_paths, list) else 0
    result = {
        "fixture_name": str(sample.get("fixture_name", "unlabelled")),
        "route": route,
        "sample_kind": str(sample.get("sample_kind", "unknown")),
        "repeat": sample.get("repeat"),
        "navigation_cycle": sample.get("navigation_cycle"),
        "counterfactual": bool(sample.get("counterfactual", False)),
        "error": sample.get("error"),
        "useful_data_ms": useful,
        "browser_useful_after_action_ms": browser_useful,
        "shell_ms": number(sample.get("navigation_to_shell_ms")),
        "required_data_barrier_ms": round(data_barrier, 3) if data_barrier is not None else None,
        "blocking_prerequisite_barrier_ms": (
            round(blocking_prerequisite_barrier, 3)
            if blocking_prerequisite_barrier is not None
            else None
        ),
        "required_barrier_ms": round(required_barrier, 3) if required_barrier is not None else None,
        "required_barrier_gap_ms": (
            round(browser_useful - required_barrier, 3)
            if browser_useful is not None and required_barrier is not None
            else None
        ),
        "required_request_count": len(required_data),
        "blocking_prerequisite_count": len(blocking_prerequisites),
        "action_required_request_count": len(action_required),
        "api_request_count": len(api_network),
        "optional_request_count": len(optional),
        "optional_after_required_count": sum(
            1
            for _item, end in optional
            if required_barrier is not None and end > required_barrier
        ),
        "optional_after_useful_count": sum(
            1
            for _item, end in optional
            if browser_useful is not None and end > browser_useful
        ) if browser_useful is not None else None,
        "required_payload_bytes": sum(
            number(item.get("decoded_body_size_bytes")) or 0
            for item, _end in required_data
            if isinstance(item, Mapping)
        ),
        "blocking_prerequisite_payload_bytes": sum(
            number(item.get("decoded_body_size_bytes")) or 0
            for item, _end in blocking_prerequisites
            if isinstance(item, Mapping)
        ),
        "api_payload_bytes": int(sum(payloads)),
        "api_http_content_length_bytes": int(
            sum(
                number(item.get("content_length_bytes")) or 0
                for item in api_network
            )
        ),
        "api_encoded_body_size_bytes": int(
            sum(
                number(item.get("encoded_body_size_bytes")) or 0
                for item in api_network
            )
        ),
        "max_in_flight_api_requests": max_in_flight(intervals),
        "overlapping_api_pairs": sum(
            1
            for index, (start, end) in enumerate(intervals)
            for other_start, other_end in intervals[index + 1 :]
            if max(start, other_start) < min(end, other_end)
        ),
        "json_parse_total_ms": round(sum(parse_durations), 3),
        "duplicate_api_path_count": duplicate_count,
        "long_task_count": len(long_task_durations),
        "long_task_total_ms": round(sum(long_task_durations), 3),
        "long_task_max_ms": round(max(long_task_durations), 3) if long_task_durations else None,
        "named_parse_react_commit_measures": named_measures,
        "cache_headers_available": cache_headers_available,
        "cache_header_presence": cache_presence,
    }
    return result


def summarize_observations(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for observation in observations:
        groups[
            (
                str(observation.get("fixture_name", "unlabelled")),
                str(observation["route"]),
                str(observation["sample_kind"]),
            )
        ].append(observation)
    summaries: list[dict[str, Any]] = []
    for (fixture_name, route, sample_kind), values in sorted(groups.items()):
        def values_for(field: str) -> list[float]:
            return [
                value
                for item in values
                for value in (number(item.get(field)),)
                if value is not None
            ]

        summaries.append(
            {
                "fixture_name": fixture_name,
                "route": route,
                "sample_kind": sample_kind,
                "sample_count": len(values),
                "errors": sum(1 for item in values if item.get("error")),
                "useful_data_ms": numeric_summary(values_for("useful_data_ms")),
                "browser_useful_after_action_ms": numeric_summary(
                    values_for("browser_useful_after_action_ms")
                ),
                "shell_ms": numeric_summary(values_for("shell_ms")),
                "required_data_barrier_ms": numeric_summary(
                    values_for("required_data_barrier_ms")
                ),
                "blocking_prerequisite_barrier_ms": numeric_summary(
                    values_for("blocking_prerequisite_barrier_ms")
                ),
                "required_barrier_ms": numeric_summary(values_for("required_barrier_ms")),
                "required_barrier_gap_ms": numeric_summary(values_for("required_barrier_gap_ms")),
                "required_request_count": numeric_summary(values_for("required_request_count"), integer=True),
                "blocking_prerequisite_count": numeric_summary(
                    values_for("blocking_prerequisite_count"), integer=True
                ),
                "action_required_request_count": numeric_summary(
                    values_for("action_required_request_count"), integer=True
                ),
                "optional_request_count": numeric_summary(values_for("optional_request_count"), integer=True),
                "api_payload_bytes": numeric_summary(values_for("api_payload_bytes"), integer=True),
                "api_http_content_length_bytes": numeric_summary(
                    values_for("api_http_content_length_bytes"), integer=True
                ),
                "api_encoded_body_size_bytes": numeric_summary(
                    values_for("api_encoded_body_size_bytes"), integer=True
                ),
                "duplicate_api_path_count": numeric_summary(values_for("duplicate_api_path_count"), integer=True),
                "max_in_flight_api_requests": numeric_summary(values_for("max_in_flight_api_requests"), integer=True),
                "overlapping_api_pairs": numeric_summary(values_for("overlapping_api_pairs"), integer=True),
                "long_task_count": numeric_summary(values_for("long_task_count"), integer=True),
                "long_task_total_ms": numeric_summary(values_for("long_task_total_ms")),
            }
        )
    return summaries


def endpoint_stats(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for sample in samples:
        fixture_name = str(sample.get("fixture_name", "unlabelled"))
        sample_kind = str(sample.get("sample_kind", "unknown"))
        route = str(sample.get("route", "unknown"))
        network = sample.get("network")
        if not isinstance(network, list):
            continue
        for item in network:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path", ""))
            if not path.startswith("/api/"):
                continue
            fields_for_item = grouped[(fixture_name, route, sample_kind, path)]
            for field in (
                "ttfb_ms",
                "browser_duration_ms",
                "content_length_bytes",
                "encoded_body_size_bytes",
                "decoded_body_size_bytes",
            ):
                value = number(item.get(field))
                if value is not None:
                    fields_for_item[field].append(value)
    rows = []
    for (fixture_name, route, sample_kind, path), fields in grouped.items():
        rows.append(
            {
                "fixture_name": fixture_name,
                "route": route,
                "sample_kind": sample_kind,
                "path": path,
                "completed_observations": len(fields.get("browser_duration_ms", [])),
                "ttfb_ms": numeric_summary(fields.get("ttfb_ms", [])),
                "browser_duration_ms": numeric_summary(fields.get("browser_duration_ms", [])),
                "http_content_length_bytes": numeric_summary(
                    fields.get("content_length_bytes", []), integer=True
                ),
                "encoded_body_size_bytes": numeric_summary(
                    fields.get("encoded_body_size_bytes", []), integer=True
                ),
                "decoded_body_size_bytes": numeric_summary(
                    fields.get("decoded_body_size_bytes", []), integer=True
                ),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -(row["ttfb_ms"].get("median_ms") or -1),
            row["fixture_name"],
            row["sample_kind"],
            row["path"],
        ),
    )


def cache_observation(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize cache-related response observations without inferring hits."""

    api_items = [
        item
        for sample in samples
        for item in (sample.get("network") if isinstance(sample.get("network"), list) else [])
        if isinstance(item, Mapping)
        and str(item.get("path", "")).startswith("/api/")
        and item.get("finished_ms") is not None
    ]
    cache_header_keys = (
        "cache_control_present",
        "age_header_present",
        "etag_present",
    )
    return {
        "completed_api_response_count": len(api_items),
        "header_presence": {
            key: sum(1 for item in api_items if item.get(key) is True)
            for key in cache_header_keys
        },
        "zero_transfer_observation_count": sum(
            1
            for item in api_items
            if item.get("transfer_size_bytes") == 0
        ),
        "header_values_retained": False,
        "cache_hit_or_miss_inferred": False,
        "invalidation_behavior_measured": False,
    }


def profile_operation(profile: Mapping[str, Any], fixture: str, operation: str) -> dict[str, Any]:
    for item in profile.get("fixtures", []):
        if isinstance(item, Mapping) and item.get("name") == fixture:
            for candidate in item.get("operations", []):
                if isinstance(candidate, Mapping) and candidate.get("name") == operation:
                    return dict(candidate)
    raise ValueError(f"missing profile operation {fixture}/{operation}")


def profile_counterfactual(profile: Mapping[str, Any], fixture: str, operation: str) -> dict[str, Any]:
    for item in profile.get("fixtures", []):
        if isinstance(item, Mapping) and item.get("name") == fixture:
            for candidate in item.get("counterfactuals", []):
                if isinstance(candidate, Mapping) and candidate.get("name") == operation:
                    return dict(candidate)
    raise ValueError(f"missing profile counterfactual {fixture}/{operation}")


def core_baseline(operation: Mapping[str, Any]) -> float:
    value = operation.get("baseline", {}).get("core_wall_ms", {}).get("median_ms")
    if not isinstance(value, (int, float)):
        raise ValueError("profile operation has no unprofiled core median")
    return float(value)


def payload_bytes(operation: Mapping[str, Any]) -> int:
    value = operation.get("profiled", {}).get("payload_bytes")
    if not isinstance(value, int):
        raise ValueError("profile operation has no payload size")
    return value


def percent_change(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return round((after - before) / before * 100, 2)


def safe_secret_check(value: Any) -> bool:
    encoded = json.dumps(value, sort_keys=True)
    forbidden = (
        str(REPO_ROOT),
        "Authorization:",
        "Bearer ",
        "AFLOW_PROFILE_AUTH_TOKEN",
    )
    return not any(marker in encoded for marker in forbidden)


def route_observation(
    observations: Sequence[Mapping[str, Any]],
    route: str,
    fixture_name: str | None = None,
) -> Mapping[str, Any]:
    for item in observations:
        if item.get("route") == route and (
            fixture_name is None or item.get("fixture_name") == fixture_name
        ):
            return item
    suffix = f"/{fixture_name}" if fixture_name else ""
    raise ValueError(f"missing browser observation for {route}{suffix}")


def fixture_metadata(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values = document.get("fixtures")
    if not isinstance(values, list):
        values = document.get("discovery", {}).get("fixtures", [])
    return {
        str(item.get("name")): item
        for item in values
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }


def fixture_identity(document: Mapping[str, Any], name: str) -> tuple[Any, ...]:
    item = fixture_metadata(document).get(name)
    if item is None:
        raise ValueError(f"missing fixture metadata for {name}")
    discovery = document.get("discovery", {})
    return (
        discovery.get("product_source_sha", discovery.get("source_sha")),
        discovery.get("seed"),
        item.get("run_count"),
        item.get("selected_run_id"),
        item.get("selected_event_count"),
        item.get("other_event_count"),
        item.get("history_shape"),
        item.get("identity"),
    )


def fixture_measurement_key(document: Mapping[str, Any], name: str) -> tuple[Any, ...]:
    """Return comparable source/seed/shape fields without prose labels."""

    identity = fixture_identity(document, name)
    return identity[:-1]


def matching_fixture_names(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> list[str]:
    left_items = fixture_metadata(left)
    right_items = fixture_metadata(right)
    names = sorted(set(left_items) & set(right_items))
    matched = [
        name
        for name in names
        if fixture_measurement_key(left, name) == fixture_measurement_key(right, name)
    ]
    if not matched:
        raise ValueError("paired browser inputs have no matching fixture/source/seed identity")
    return matched


def browser_fixture_scaling(
    observations: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compare only same-kind routes across the explicitly labelled fixtures."""

    metadata = fixture_metadata(documents)
    rows: list[dict[str, Any]] = []
    for route in ("all_runs", "project_runs", "selected_run_summary", "checkpoint_history"):
        for fixture_name in sorted(metadata):
            fixture_observations = [
                item
                for item in observations
                if item.get("fixture_name") == fixture_name
                and item.get("route") == route
                and item.get("sample_kind") == "warm"
            ]
            if not fixture_observations:
                continue
            rows.append(
                {
                    "fixture_name": fixture_name,
                    "route": route,
                    "sample_kind": "warm",
                    "run_count": metadata[fixture_name].get("run_count"),
                    "selected_run_id": metadata[fixture_name].get("selected_run_id"),
                    "selected_event_count": metadata[fixture_name].get("selected_event_count"),
                    "sample_count": len(fixture_observations),
                    "errors": sum(1 for item in fixture_observations if item.get("error")),
                    "useful_data_ms": numeric_summary(
                        value
                        for item in fixture_observations
                        for value in (number(item.get("useful_data_ms")),)
                        if value is not None
                    ),
                    "decoded_body_bytes": numeric_summary(
                        value
                        for item in fixture_observations
                        for value in (number(item.get("api_payload_bytes")),)
                        if value is not None
                    ),
                    "http_content_length_bytes": numeric_summary(
                        value
                        for item in fixture_observations
                        for value in (number(item.get("api_http_content_length_bytes")),)
                        if value is not None
                    ),
                }
            )
    return rows


def browser_counterfactual_rows(
    normal_samples: Sequence[Mapping[str, Any]],
    counterfactual_samples: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    def choose(
        samples: Sequence[Mapping[str, Any]], fixture: str, route: str
    ) -> Mapping[str, Any] | None:
        candidates = [
            item
            for item in samples
            if item.get("fixture_name") == fixture and item.get("route") == route
        ]
        return next(
            (item for item in candidates if item.get("sample_kind") == "warm"),
            candidates[0] if candidates else None,
        )

    fixtures = sorted(
        {
            str(item.get("fixture_name", "unlabelled"))
            for item in normal_samples
        }
        & {
            str(item.get("fixture_name", "unlabelled"))
            for item in counterfactual_samples
        }
    )
    rows = []
    for fixture in fixtures:
        routes = sorted(
            {
                str(item.get("route"))
                for item in normal_samples
                if item.get("fixture_name") == fixture
            }
            & {
                str(item.get("route"))
                for item in counterfactual_samples
                if item.get("fixture_name") == fixture
            }
        )
        for route in routes:
            normal_item = choose(normal_samples, fixture, route)
            counterfactual_item = choose(counterfactual_samples, fixture, route)
            if normal_item is None or counterfactual_item is None:
                continue
            left = analyze_sample(normal_item)
            right = analyze_sample(counterfactual_item)
            normal_flow = normal_item.get("api_request_flow", [])
            counterfactual_flow = counterfactual_item.get("api_request_flow", [])
            normal_flow = normal_flow if isinstance(normal_flow, list) else []
            counterfactual_flow = counterfactual_flow if isinstance(counterfactual_flow, list) else []
            normal_failures = normal_item.get("failed_requests", [])
            counterfactual_failures = counterfactual_item.get("failed_requests", [])
            normal_failures = normal_failures if isinstance(normal_failures, list) else []
            counterfactual_failures = counterfactual_failures if isinstance(counterfactual_failures, list) else []
            rows.append(
                {
                    "fixture_name": fixture,
                    "route": route,
                    "normal_useful_data_ms": left.get("useful_data_ms"),
                    "counterfactual_useful_data_ms": right.get("useful_data_ms"),
                    "useful_delta_ms": (
                        round(right["useful_data_ms"] - left["useful_data_ms"], 3)
                        if isinstance(left.get("useful_data_ms"), (int, float))
                        and isinstance(right.get("useful_data_ms"), (int, float))
                        else None
                    ),
                    "normal_api_request_count": len(normal_flow),
                    "counterfactual_api_request_count": len(counterfactual_flow),
                    "normal_duplicate_api_path_count": left["duplicate_api_path_count"],
                    "counterfactual_duplicate_api_path_count": right["duplicate_api_path_count"],
                    "counterfactual_failed_request_count": len(counterfactual_failures),
                }
            )
    return rows


def profile_has_complete_samples(profile: Mapping[str, Any]) -> bool:
    """Reject a profile whose summaries hide failed or missing raw calls."""

    for fixture in profile.get("fixtures", []):
        if not isinstance(fixture, Mapping):
            return False
        for operation in [
            *fixture.get("operations", []),
            *fixture.get("counterfactuals", []),
        ]:
            if not isinstance(operation, Mapping):
                return False
            baseline = operation.get("baseline", {})
            samples = baseline.get("samples")
            if not isinstance(samples, list) or not samples:
                return False
            if baseline.get("failed_repeats", 0) or baseline.get("partial"):
                return False
            if any(
                isinstance(sample, Mapping) and sample.get("error")
                for sample in samples
            ):
                return False
            profiled = operation.get("profiled", {})
            if profiled.get("error") is not None:
                return False
    return True


def build_analysis(
    baseline: Mapping[str, Any],
    profile: Mapping[str, Any],
    browser_normal: Mapping[str, Any],
    browser_counterfactual: Mapping[str, Any],
    product_source_sha: str,
    historical_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    corrected_samples = [
        item for item in baseline.get("samples", []) if isinstance(item, Mapping)
    ]
    paired_normal_samples = [
        item for item in browser_normal.get("samples", []) if isinstance(item, Mapping)
    ]
    counterfactual_samples = [
        item for item in browser_counterfactual.get("samples", []) if isinstance(item, Mapping)
    ]
    historical = historical_baseline or {}
    retained_samples = [
        item for item in historical.get("samples", []) if isinstance(item, Mapping)
    ]
    if not retained_samples:
        retained_samples = corrected_samples
    if not corrected_samples or not paired_normal_samples or not counterfactual_samples:
        raise ValueError("analysis requires corrected and paired browser samples")
    if profile.get("discovery", {}).get("product_source_sha") != product_source_sha:
        raise ValueError("profile product source does not match the requested source SHA")
    for name, value in (
        ("corrected browser baseline", baseline),
        ("profile", profile),
        ("browser normal pair", browser_normal),
        ("browser counterfactual", browser_counterfactual),
    ):
        discovery = value.get("discovery", {})
        observed_product_sha = discovery.get("product_source_sha")
        if name != "profile" and observed_product_sha != product_source_sha:
            raise ValueError(f"{name} product source does not match the requested source SHA")
        if value.get("validation", {}).get("product_source_mutated") is True:
            raise ValueError(f"{name} reports product source mutation")
        if value.get("validation", {}).get("secrets_absent") is False:
            raise ValueError(f"{name} reports secret material")
    if not profile_has_complete_samples(profile):
        raise ValueError("profile contains failed, partial or missing raw samples")
    if baseline.get("discovery", {}).get("seed") != browser_normal.get("discovery", {}).get("seed"):
        raise ValueError("corrected and paired browser seeds do not match")
    matching_fixture_names(baseline, browser_normal)
    matching_fixture_names(browser_normal, browser_counterfactual)
    matching_fixture_names(profile, baseline)

    retained_observations = [analyze_sample(item) for item in retained_samples]
    corrected_observations = [analyze_sample(item) for item in corrected_samples]
    paired_normal_observations = [analyze_sample(item) for item in paired_normal_samples]
    counterfactual_observations = [analyze_sample(item) for item in counterfactual_samples]

    small_short_list = profile_operation(profile, "small-short-history", "list_runs")
    large_short_list = profile_operation(profile, "large-short-history", "list_runs")
    small_short_cf = profile_counterfactual(
        profile, "small-short-history", "list_runs_without_progress_projection"
    )
    large_short_cf = profile_counterfactual(
        profile, "large-short-history", "list_runs_without_progress_projection"
    )

    def projection_fields(operation: Mapping[str, Any]) -> dict[str, Any]:
        profiled = operation["profiled"]
        return {
            "repository_with_progress_calls": profiled["progress_projection_call_count"],
            "progress_summary_calls": profiled["progress_summary_call_count"],
            "progress_detail_calls": profiled["progress_detail_call_count"],
        }

    list_comparison = {
        "normal": {
            "100_runs": {
                "fixture": "small-short-history",
                "core_wall_ms": core_baseline(small_short_list),
                "profiled_python_calls": small_short_list["profiled"]["python_call_count"],
                "progress_projection_calls": small_short_list["profiled"]["progress_projection_call_count"],
                "progress_call_breakdown": projection_fields(small_short_list),
                "path_stat_calls": small_short_list["io"]["path_stat"],
            },
            "1000_runs": {
                "fixture": "large-short-history",
                "core_wall_ms": core_baseline(large_short_list),
                "profiled_python_calls": large_short_list["profiled"]["python_call_count"],
                "progress_projection_calls": large_short_list["profiled"]["progress_projection_call_count"],
                "progress_call_breakdown": projection_fields(large_short_list),
                "path_stat_calls": large_short_list["io"]["path_stat"],
            },
        },
        "without_progress_projection": {
            "100_runs_core_wall_ms": core_baseline(small_short_cf),
            "100_runs_profiled_core_wall_ms": small_short_cf["profiled"]["core_wall_ms"],
            "1000_runs_core_wall_ms": core_baseline(large_short_cf),
            "1000_runs_profiled_core_wall_ms": large_short_cf["profiled"]["core_wall_ms"],
            "100_runs_progress_projection_calls": small_short_cf["profiled"]["progress_projection_call_count"],
            "1000_runs_progress_projection_calls": large_short_cf["profiled"]["progress_projection_call_count"],
            "100_runs_progress_call_breakdown": projection_fields(small_short_cf),
            "1000_runs_progress_call_breakdown": projection_fields(large_short_cf),
        },
        "projection_gate": {
            "measured_before_repository_calls_per_100_row_page": small_short_list["profiled"]["progress_projection_call_count"],
            "measured_before_summary_calls_per_100_row_page": small_short_list["profiled"]["progress_summary_call_count"],
            "measured_before_detail_calls_per_100_row_page": small_short_list["profiled"]["progress_detail_call_count"],
            "future_target_repository_calls_per_100_row_page": 100,
            "counterfactual_repository_calls_per_100_row_page": small_short_cf["profiled"]["progress_projection_call_count"],
            "counterfactual_summary_calls_per_100_row_page": small_short_cf["profiled"]["progress_summary_call_count"],
            "counterfactual_detail_calls_per_100_row_page": small_short_cf["profiled"]["progress_detail_call_count"],
            "counting_rule": "repository RunRepository._with_progress only; summary/detail are nested diagnostics, never added",
        },
        "list_size_core_wall_change_percent": percent_change(
            core_baseline(small_short_list), core_baseline(large_short_list)
        ),
        "list_size_python_call_change_percent": percent_change(
            float(small_short_list["profiled"]["python_call_count"]),
            float(large_short_list["profiled"]["python_call_count"]),
        ),
        "progress_disabled_core_wall_reduction_percent_100_runs": round(
            (1 - core_baseline(small_short_cf) / core_baseline(small_short_list)) * 100,
            2,
        ),
        "progress_disabled_core_wall_reduction_percent_1000_runs": round(
            (1 - core_baseline(large_short_cf) / core_baseline(large_short_list)) * 100,
            2,
        ),
    }

    profile_fixtures = fixture_metadata(profile)
    event_comparison: dict[str, Any] = {}
    for operation_name in ("selected_events", "selected_context_lite", "selected_context_full"):
        short = profile_operation(profile, "small-short-history", operation_name)
        long = profile_operation(profile, "small-long-history", operation_name)
        short_events = profile_fixtures["small-short-history"].get("selected_event_count")
        long_events = profile_fixtures["small-long-history"].get("selected_event_count")
        event_comparison[operation_name] = {
            "short_fixture": "small-short-history",
            "long_fixture": "small-long-history",
            "short_history_events": short_events,
            "long_history_events": long_events,
            "short_core_wall_ms": core_baseline(short),
            "long_core_wall_ms": core_baseline(long),
            "core_wall_change_percent": percent_change(
                core_baseline(short), core_baseline(long)
            ),
            "short_serializer_bytes": payload_bytes(short),
            "long_serializer_bytes": payload_bytes(long),
            "payload_change_percent": percent_change(
                float(payload_bytes(short)), float(payload_bytes(long))
            ),
            "short_path_read_bytes": short["io"]["path_read_bytes"],
            "long_path_read_bytes": long["io"]["path_read_bytes"],
        }

    retained_summary = summarize_observations(retained_observations)
    normal_summary = summarize_observations(corrected_observations)
    paired_normal_summary = summarize_observations(paired_normal_observations)
    counterfactual_summary = summarize_observations(counterfactual_observations)
    cache_summary = cache_observation(corrected_samples)
    repeated = sorted(
        [
            observation
            for observation in corrected_observations
            if observation["route"] == "selected_run_summary"
            and observation["sample_kind"] == "repeat_navigation"
        ],
        key=lambda item: number(item.get("navigation_cycle")) or 0,
    )
    repeated_comparison = {
        "sample_count": len(repeated),
        "first_cycle": repeated[0] if repeated else None,
        "last_cycle": repeated[-1] if repeated else None,
        "per_cycle": [
            {
                "navigation_cycle": item.get("navigation_cycle"),
                "api_request_count": item.get("api_request_count"),
                "error": item.get("error"),
                "duplicate_api_path_count": item.get("duplicate_api_path_count"),
            }
            for item in repeated
        ],
        "useful_data_ms": numeric_summary(
            value
            for item in repeated
            for value in (number(item.get("useful_data_ms")),)
            if value is not None
        ),
        "api_request_count": numeric_summary(
            (int(item.get("api_request_count", 0)) for item in repeated),
            integer=True,
        ),
        "duplicate_api_path_count": numeric_summary(
            (int(item.get("duplicate_api_path_count", 0)) for item in repeated),
            integer=True,
        ),
        "navigation_mode": "in_app_selection",
        "interpretation": "per-cycle in-app selection captures retain request/error counts; this bounded ten-cycle sample does not establish monotonic listener/request growth",
    }
    counterfactual_rows = browser_counterfactual_rows(
        paired_normal_samples, counterfactual_samples
    )
    scaling_rows = browser_fixture_scaling(corrected_observations, baseline)
    selected_retained = next(
        (
            item
            for item in retained_observations
            if item["route"] == "selected_run_summary"
            and item["sample_kind"] == "warm"
        ),
        route_observation(corrected_observations, "selected_run_summary"),
    )
    checkpoint_retained = next(
        (
            item
            for item in retained_observations
            if item["route"] == "checkpoint_history"
            and item["sample_kind"] == "warm"
        ),
        route_observation(corrected_observations, "checkpoint_history"),
    )
    list_finding = {
        "rank": 1,
        "id": "run_list_history_and_progress_projection",
        "label": "Run-list history scanning and additive progress projection",
        "contribution": (
            f"Unprofiled list_runs core median is {list_comparison['normal']['100_runs']['core_wall_ms']:.1f} ms at 100 runs and "
            f"{list_comparison['normal']['1000_runs']['core_wall_ms']:.1f} ms at 1000 runs; the in-memory no-progress counterfactual reduces those medians by "
            f"{list_comparison['progress_disabled_core_wall_reduction_percent_100_runs']:.1f}% and "
            f"{list_comparison['progress_disabled_core_wall_reduction_percent_1000_runs']:.1f}%."
        ),
        "affected_routes": ["all_runs", "project_runs", "selected_run_summary", "checkpoint_history"],
        "data_shape": "100 versus 1000 runs; selected run identity and selected history are held at run-0000 and 8 events",
        "confidence": "high",
        "estimated_fix_size": "medium",
        "proposed_fix": True,
        "causal_evidence": "list-size profile plus restored in-memory progress-disabled counterfactual",
        "source_mapping": [
            "aflow/control_plane/repository.py:379 list_history",
            "aflow/control_plane/repository.py:228 get_run_status",
            "aflow/control_plane/repository.py:190 _with_progress",
            "aflow/control_plane/run_progress.py:5137 project_run_progress_summary",
            "apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:252 list_runs",
        ],
    }
    event_finding = {
        "rank": 2,
        "id": "selected_history_event_and_context_payload",
        "label": "Selected history increases event/context work and payload",
        "contribution": (
            f"At 100 runs, selected_events core median changes from {event_comparison['selected_events']['short_core_wall_ms']:.1f} ms for "
            f"{event_comparison['selected_events']['short_history_events']} events to {event_comparison['selected_events']['long_core_wall_ms']:.1f} ms for "
            f"{event_comparison['selected_events']['long_history_events']}, while canonical serializer bytes grow from "
            f"{event_comparison['selected_events']['short_serializer_bytes']} to {event_comparison['selected_events']['long_serializer_bytes']}."
        ),
        "affected_routes": ["selected_run_summary", "checkpoint_history"],
        "data_shape": "small-short-history versus small-long-history; 100 runs and selected run run-0000 held constant",
        "confidence": "high",
        "estimated_fix_size": "small-to-medium",
        "proposed_fix": True,
        "causal_evidence": "event-count counterfactual fixture with list size and selected identity held constant",
        "source_mapping": [
            "aflow/control_plane/persistence.py:490 read_events",
            "aflow/control_plane/persistence.py:608 build_context_bundle",
            "apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:287 events",
            "apps/aflow_app/server/src/aflow_app_server/control_plane_service.py:304 context",
        ],
    }
    browser_finding = {
        "rank": 3,
        "id": "selected_route_eager_request_fanout",
        "label": "Selected-route eager duplicate/support request fan-out",
        "contribution": (
            f"Corrected browser coverage records {selected_retained.get('duplicate_api_path_count')} duplicate API paths for the selected summary and "
            f"{checkpoint_retained.get('duplicate_api_path_count')} for checkpoint history in the retained/paired observation; the runner now records "
            "each in-app selection cycle separately, so this remains an observation rather than a claimed optimization cause."
        ),
        "affected_routes": ["selected_run_summary", "checkpoint_history"],
        "data_shape": "four isolated fixture scales/shapes, five warm samples per route and a ten-cycle in-app selection bound",
        "confidence": "medium",
        "estimated_fix_size": "medium",
        "proposed_fix": False,
        "causal_evidence": "corrected paired optional-request capture; no useful-data improvement is dispatched from the variable counterfactual",
        "source_mapping": [
            "apps/aflow_app/web/src/components/RunDashboard.tsx:1343 loadDashboard",
            "apps/aflow_app/web/src/components/RunDashboard.tsx:1439 loadSelectedRun",
            "apps/aflow_app/web/src/components/RunDashboard.tsx:1304 subscribeToRunEvents",
            "apps/aflow_app/web/src/components/RunDashboard.tsx:1628 loadContext",
        ],
    }
    network_finding = {
        "rank": 4,
        "id": "context_body_delivery",
        "label": "Context response body delivery is network/client-dominant in the waterfall",
        "contribution": "Corrected browser Resource Timing retains encoded/decoded body and header-size observations separately; direct Python context profiles remain a separate canonical-serializer measurement, so no Python optimization is inferred from browser transfer time.",
        "affected_routes": ["selected_run_summary", "checkpoint_history"],
        "data_shape": "isolated Chromium waterfall grouped by fixture and route; no remote network claim",
        "confidence": "medium-high",
        "estimated_fix_size": "unknown",
        "proposed_fix": False,
        "causal_evidence": "browser Resource Timing versus direct service profile; no Python optimization is justified by this evidence",
        "source_mapping": [
            "scripts/perf/profile_ui_data_load.py:BrowserCapture",
            "apps/aflow_app/web/src/api.ts:55 fetchJson",
        ],
    }
    findings = [list_finding, event_finding, browser_finding, network_finding]

    previous_duration = historical.get("cp2", {}).get("workflow_execution_duration", {})
    browser_runtime = {
        "product_source_sha": product_source_sha,
        "runner_source_sha": baseline.get("discovery", {}).get("source_sha"),
        "timing_normalization": {
            "clock": "browser performance.now()",
            "action_start_field": "browser_action_start_ms",
            "accepted_useful_field": "browser_accepted_useful_ms",
            "accepted_useful_relative_field": "browser_useful_after_action_ms",
            "normalized_flow_suffix": "_from_action_ms",
            "python_elapsed_fields": [
                "useful_data_ready_ms",
                "refresh_to_useful_ms",
            ],
        },
        "normal_sample_count": len(corrected_samples),
        "paired_normal_sample_count": len(paired_normal_samples),
        "counterfactual_sample_count": len(counterfactual_samples),
        "normal_samples": corrected_samples,
        "paired_normal_samples": paired_normal_samples,
        "counterfactual_samples": counterfactual_samples,
        "counterfactual_blocked_suffixes": browser_counterfactual.get("discovery", {}).get(
            "blocked_optional_requests", []
        ),
        "coverage_limits": {
            "fixture_names": sorted(fixture_metadata(baseline)),
            "warm_repeats_per_fixture_route": baseline.get("discovery", {}).get("repeats"),
            "fresh_process_samples_per_fixture_route": sum(
                1
                for item in corrected_samples
                if item.get("sample_kind") == "fresh_process"
            ),
            "navigation_cycles": baseline.get("discovery", {}).get("navigation_cycles"),
            "navigation_mode": "in_app_selection",
            "live": "unavailable; no authorized owner URL, credential or exact reference run identity was supplied",
        },
    }
    request_role_mapping = {
        "explicit_refresh": {
            "blocking_prerequisites": [
                "/ready",
                "/api/control-plane/projects/{project_id}/capabilities",
                "/api/control-plane/projects/{project_id}/plans",
                "/api/config",
                "/api/config/form",
            ],
            "displayed_data": [
                "/api/control-plane/projects/{project_id}/runs",
                "/api/control-plane/projects/{project_id}/runs/{run_id}",
                "/api/control-plane/projects/{project_id}/runs/{run_id}/events",
            ],
            "optional": [
                "/api/control-plane/projects/{project_id}/runs/{run_id}/context",
                "/api/control-plane/projects/{project_id}/runs/{run_id}/events/stream",
                "/api/control-plane/projects/{project_id}/runs/{run_id}/restart-options",
            ],
            "owner_sequence": (
                "refreshPage awaits loadDashboard; loadDashboard awaits its Promise.all and "
                "the committed config projection before loadSelectedRun"
            ),
            "retained_flow_limit": (
                "The retained api_request_flow is API-only, so /ready is mapped but not "
                "included in its measured request counts"
            ),
        },
        "initial_selected_routes": {
            "configuration_role": "optional_configuration",
            "reason": (
                "Initial selected-route loading does not globally promote config/form; "
                "the Refresh owner is the scoped blocking dependency"
            ),
        },
    }
    return {
        "profile": profile,
        "browser_runtime": browser_runtime,
        "browser_analysis": {
            "superseded_cp1_summary": retained_summary,
            "corrected_normal_summary": normal_summary,
            "paired_normal_summary": paired_normal_summary,
            "optional_request_counterfactual_summary": counterfactual_summary,
            "optional_request_counterfactual_rows": counterfactual_rows,
            "repeat_navigation": repeated_comparison,
            "fixture_scaling": scaling_rows,
            "endpoint_stats_by_fixture": endpoint_stats(corrected_samples),
            "cache_observation": cache_summary,
        },
        "comparisons": {
            "list_size": list_comparison,
            "selected_history": event_comparison,
        },
        "ranked_findings": findings,
        "request_role_mapping": request_role_mapping,
        "ruled_out_or_unresolved": [
            {
                "hypothesis": "browser main-thread long tasks or named React/parse commit work",
                "evidence": "corrected instrumented Chromium samples retain paint entries, long-task observations and named measures by fixture; no forced main-thread finding is claimed without a positive sample",
                "status": "not evidenced in this bounded run",
            },
            {
                "hypothesis": "lock, queue or event-loop contention",
                "evidence": "all direct profiles were serial over an in-memory unit observer",
                "status": "not measured; do not claim absence under concurrent remote load",
            },
            {
                "hypothesis": "Git/subprocess work dominates read latency",
                "evidence": "profile IO counters show only bounded Git subprocess observations per request-shaped operation; no Git span appears among ranked hot functions",
                "status": "ruled out for this fixture read path",
            },
            {
                "hypothesis": "listener/request growth over the ten-cycle bound",
                "evidence": repeated_comparison["interpretation"],
                "status": "inconclusive beyond the measured in-app selection cycles",
            },
        ],
        "profiling_overhead": {
            "method": "each operation has serial unprofiled repeats and one cProfile call; overhead is profiled wall divided by the unprofiled median",
            "profiled_call_is_not_a_baseline": True,
            "critical_path_uses_max_required_completion": True,
            "overlap_durations_summed": False,
        },
        "workflow_execution_duration": previous_duration or {
            "reported_seconds": 683,
            "reported_text": "11m23s",
            "source": "owner report reference in the retained plan",
            "measured_by_ui_runner": False,
            "combined_with_ui_latency": False,
        },
        "live_coverage": {
            "status": "BLOCKED",
            "symptom": "four-second owner-facing symptom remains unmeasured",
            "missing_input": "authorized owner URL, in-memory credential and exact reference run identity",
            "smallest_owner_measurement": "one serial Chromium capture on the owner URL, at most three navigations per selected route, with deployed SHA and sanitized dataset bounds",
            "remote_timing_inferred": False,
        },
        "commands": [
            "uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py --scenario isolated-smoke --repeats 1 --navigation-cycles 0 --artifact-dir <external-artifact-dir> --report-dir <external-smoke-report-dir>",
            "uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py --scenario isolated --seed 20260912 --repeats 5 --navigation-cycles 10 --artifact-dir <external-browser-artifact-dir> --report-dir <external-browser-report-dir>",
            "uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_ui_data_load.py --scenario isolated --seed 20260912 --repeats 1 --navigation-cycles 0 --block-optional-requests --artifact-dir <external-browser-pair-counterfactual-artifact-dir> --report-dir <external-browser-pair-counterfactual-report-dir>",
            f"uv run --frozen --project apps/aflow_app/server python scripts/perf/profile_control_plane_reads.py --scenario isolated --product-source-sha {product_source_sha} --seed 20260912 --repeats 5 --artifact-dir <external-profile-artifact-dir>",
            f"uv run --frozen --project apps/aflow_app/server python scripts/perf/analyze_ui_data_latency.py --baseline <external-browser-report-dir>/baseline.json --profile <external-profile-artifact-dir>/control-plane-profile.json --browser-baseline <external-browser-pair-normal-report-dir>/baseline.json --browser-counterfactual <external-browser-pair-counterfactual-report-dir>/baseline.json --product-source-sha {product_source_sha} --report-dir plans/research/ui-data-latency-20260912",
        ],
        "follow_up_plan": "plans/in-progress/ui-data-latency-improvements-20260912.md",
        "follow_up_plan_status": "ready for isolated implementation review; owner-facing baseline BLOCKED pending the stated measurement",
        "validation": {
            "secrets_absent": safe_secret_check(
                {"profile": profile, "browser_runtime": browser_runtime}
            ),
            "product_source_mutated": False,
            "live_profiled": False,
            "raw_response_bodies_retained": False,
            "raw_timing_samples_retained": profile_has_complete_samples(profile),
            "private_paths_retained": False,
        },
    }


def fmt(value: Any, suffix: str = "") -> str:
    if isinstance(value, (int, float)):
        return f"{value:.1f}{suffix}"
    return "n/a"




def corrected_report_markdown(
    baseline: Mapping[str, Any],
    analysis: Mapping[str, Any],
    product_source_sha: str,
) -> str:
    """Render only values from the corrected, fixture-labelled evidence."""

    discovery = baseline["discovery"]
    profile = analysis["profile"]
    list_size = analysis["comparisons"]["list_size"]
    selected_history = analysis["comparisons"]["selected_history"]
    browser_analysis = analysis["browser_analysis"]
    cache_summary = browser_analysis["cache_observation"]
    request_role_mapping = analysis["request_role_mapping"]
    refresh_summary = next(
        (
            row
            for row in browser_analysis["corrected_normal_summary"]
            if row["route"] == "explicit_refresh" and row["sample_kind"] == "warm"
        ),
        {},
    )

    def median(row: Mapping[str, Any], field: str, suffix: str = "") -> str:
        value = row.get(field)
        if isinstance(value, Mapping) and isinstance(value.get("median_ms"), (int, float)):
            return f"{value['median_ms']:.1f}{suffix}"
        if isinstance(value, Mapping) and isinstance(value.get("median"), (int, float)):
            return f"{value['median']}{suffix}"
        return "n/a"

    lines = [
        "# UI data-latency discovery",
        "",
        "This report attributes the existing UI's useful-data delay under bounded",
        "isolated fixtures. It contains no product optimization and makes no claim",
        "that loading became faster. Values are observations, not SLAs.",
        "",
        "## Scope and identity",
        "",
        f"- Product source SHA: {product_source_sha}; browser runner SHA: {discovery.get('source_sha', 'n/a')}; deployed SHA: {discovery.get('deployed_sha', 'n/a')}.",
        f"- Browser: {discovery['browser']['name']} {discovery['browser']['version']}; host: {discovery['host']['host_label']}; seed: {discovery['seed']}; profile seed: {profile['discovery']['seed']}.",
        "- Server profiles used disposable fixture repositories, fresh isolated service composition, an in-memory unit observer and serial calls. No systemd unit, workflow, shared AFlow installation, live state or cache was changed.",
        "- Browser summaries are grouped by fixture, route and sample kind. The selected identity is held while run count or selected history changes; generated file counts remain in the machine-readable fixture metadata.",
        "- The referenced owner workflow duration is recorded separately from measured request latency; the UI runner did not measure workflow execution. F1 selected-readiness observations and F5 document-relative/action-origin comparisons from the prior baseline are superseded; corrected samples use the browser action clock.",
        "",
        "## Before-optimization browser baseline",
        "",
        "Useful-data readiness requires an actual record: selected identity plus an accepted direct status observation whose value agrees with the rendered status, checkpoint summary values from the selected run, and a completed Refresh request generation containing list/status/events JSON plus accepted current rendering. Empty headings, old DOM, loading panels, unrelated mutations and response arrival alone are rejected.",
        f"Refresh dependency attribution follows {request_role_mapping['explicit_refresh']['owner_sequence']}: GET /api/config and POST /api/config/form, along with the loadDashboard readiness/capability/plan prerequisites, are blocking prerequisites; selected list/status/events are displayed-data requirements; context, stream and restart metadata remain optional. In the retained warm Refresh samples, the median counts are {median(refresh_summary, 'required_request_count')} displayed-data requests, {median(refresh_summary, 'blocking_prerequisite_count')} blocking prerequisites, {median(refresh_summary, 'action_required_request_count')} action-required requests total and {median(refresh_summary, 'optional_request_count')} optional requests. /ready is mapped but omitted from these counts because the retained api_request_flow is API-only.",
        "A bounded real-fixture regression held GET /api/config after the Refresh list JSON completed: selected status/events did not start and useful Refresh did not accept stale data until configuration was released; the released GET/POST configuration pair was followed by current selected status/events and accepted useful content. Initial selected-route configuration remains optional in the mapping because this correction is scoped to the Refresh owner.",
        "",
        "The corrected baseline contains five warm samples per route for each isolated 100/1000-run and 8/80-event fixture, separately labelled fresh-process samples, and a bounded ten-cycle in-app selection capture. It does not call document reloads listener-accumulation evidence. Browser decoded bytes, HTTP content-length/encoded bytes and direct canonical serializer bytes are different measurements.",
        "",
        "### Corrected fixture scaling",
        "",
        "| Fixture | Route | Runs | Selected events | Warm n | Useful median | Decoded API body median | HTTP/encoded body median | Errors |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in browser_analysis["fixture_scaling"]:
        lines.append(
            f"| {row['fixture_name']} | {row['route']} | {row.get('run_count', 'n/a')} | {row.get('selected_event_count', 'n/a')} | {row['sample_count']} | {median(row, 'useful_data_ms', ' ms')} | {median(row, 'decoded_body_bytes', ' B')} | {median(row, 'http_content_length_bytes', ' B')} | {row['errors']} |"
        )
    lines.extend(
        [
            "",
            "## Server profiles and causal measurements",
            "",
            "list_runs was profiled because the isolated browser waterfall identified the run-list path as a repeated backend cost. Each row reports a median derived from successful raw calls; the cProfile call is overhead-bearing and retained separately.",
            "",
            "| Fixture / operation | Core median | Profiled CPU | Direct serializer bytes | Python calls | Repository _with_progress | Nested summary/detail | Reads / stats |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for fixture in profile["fixtures"]:
        for operation in fixture["operations"]:
            baseline_op = operation["baseline"]
            profiled = operation["profiled"]
            io = operation["io"]
            reads_stats = f"{io['path_read_bytes']} bytes / {io['path_read_text']} text / {io['path_stat']} stat"
            lines.append(
                f"| {fixture['name']} / {operation['name']} | {fmt(baseline_op['core_wall_ms'].get('median_ms'), ' ms')} | {fmt(profiled.get('cpu_ms'), ' ms')} | {profiled.get('payload_bytes', 'n/a')} B | {profiled.get('python_call_count', 'n/a')} | {profiled.get('progress_projection_call_count', 'n/a')} | {profiled.get('progress_summary_call_count', 'n/a')} / {profiled.get('progress_detail_call_count', 'n/a')} | {reads_stats} |"
            )
    lines.extend(
        [
            "",
            f"For direct fixtures {list_size['normal']['100_runs']['fixture']} and {list_size['normal']['1000_runs']['fixture']}, disabling only the additive projection changes core medians from {list_size['normal']['100_runs']['core_wall_ms']:.1f} ms to {list_size['without_progress_projection']['100_runs_core_wall_ms']:.1f} ms ({list_size['progress_disabled_core_wall_reduction_percent_100_runs']:.1f}% lower), and from {list_size['normal']['1000_runs']['core_wall_ms']:.1f} ms to {list_size['without_progress_projection']['1000_runs_core_wall_ms']:.1f} ms ({list_size['progress_disabled_core_wall_reduction_percent_1000_runs']:.1f}% lower). The patched behavior was never retained.",
            f"Changing only the list fixture changes core wall by {list_size['list_size_core_wall_change_percent']:.1f}% and profiled Python calls by {list_size['list_size_python_call_change_percent']:.1f}%. The repository projection count is one named _with_progress entry point; nested summary/detail counts are visible separately and are not added.",
            f"Projection gate: measured before = {list_size['projection_gate']['measured_before_repository_calls_per_100_row_page']} repository calls for the 100-row page; future target = {list_size['projection_gate']['future_target_repository_calls_per_100_row_page']}; the no-projection counterfactual is {list_size['projection_gate']['counterfactual_repository_calls_per_100_row_page']}. This is a measurement, not a claim that the current product meets the target.",
            "",
            "Selected-history scaling holds the list fixture and selected run identity constant while varying only selected event count:",
            "",
            "| Operation | Short core | Long core | Core change | Short serializer bytes | Long serializer bytes | Byte change |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for name, item in selected_history.items():
        lines.append(
            f"| {name} ({item['short_history_events']}→{item['long_history_events']} events) | {item['short_core_wall_ms']:.1f} ms | {item['long_core_wall_ms']:.1f} ms | {item['core_wall_change_percent']:.1f}% | {item['short_serializer_bytes']} B | {item['long_serializer_bytes']} B | {item['payload_change_percent']:.1f}% |"
        )
    lines.extend(
        [
            "",
            "The selected-events and context measurements support a scale-sensitive experiment, but no optimization is executed here. No lock/queue contention probe was added because isolated calls were serial.",
            "",
            "## Browser sequencing, rendering and cache observations",
            "",
            "- The runner records latest-request generation, accepted useful rendering, overlap-aware completion, paint/long-task/measure metadata, and fixture identity without retaining response bodies. Required barriers and optional-after-useful classifications use browser action-relative timestamps; Python elapsed durations remain separately labelled.",
            f"- {browser_analysis['repeat_navigation']['interpretation']}. The paired optional-request capture is a request-ownership experiment only; no browser optimization is proposed from it.",
            f"- Cache observation covered {cache_summary['completed_api_response_count']} completed API responses: cache-control on {cache_summary['header_presence']['cache_control_present']}, age on {cache_summary['header_presence']['age_header_present']}, and etag on {cache_summary['header_presence']['etag_present']}; {cache_summary['zero_transfer_observation_count']} had zero recorded transfer bytes. Header values are not retained and cache hits were not inferred.",
            "",
            "## Ranked findings",
            "",
        ]
    )
    for finding in analysis["ranked_findings"]:
        lines.extend(
            [
                f"{finding['rank']}. {finding['label']} ({finding['confidence']} confidence; estimated {finding['estimated_fix_size']} fix).",
                f"   Contribution: {finding['contribution']}",
                f"   Affected routes/data: {', '.join(finding['affected_routes'])}; {finding['data_shape']}.",
                f"   Causal evidence: {finding['causal_evidence']}",
                f"   Source mapping: {'; '.join(finding['source_mapping'])}",
                f"   Follow-up status: {'proposed for the separate plan' if finding['proposed_fix'] else 'observation only; do not dispatch as an optimization without the plan gate'}.",
                "",
            ]
        )
    lines.extend(
        [
            "Plausible hypotheses ruled out or left explicitly unresolved:",
            "",
        ]
    )
    for item in analysis["ruled_out_or_unresolved"]:
        lines.append(f"- {item['hypothesis']}: {item['evidence']} Status: {item['status']}.")
    lines.extend(
        [
            "",
            "## Follow-up handoff",
            "",
            "The separate evidence-backed implementation plan is at plans/in-progress/ui-data-latency-improvements-20260912.md. Isolated implementation dispatch is ready for review; the owner-facing baseline is explicitly BLOCKED until the missing authorized URL/credential/reference identity measurement is supplied. This discovery worker did not execute that plan.",
            "",
            "## Reproduction and evidence",
            "",
            f"The machine-readable baseline retains corrected browser raw samples plus a superseded_evidence copy of the prior canonical evidence ({len(browser_analysis['superseded_cp1_summary'])} grouped summaries); prior selected-readiness, mixed-clock timing and Refresh optional-role metrics are audit-only. The cp2 object contains all sanitized profile samples, cProfile rows/top functions, fixture-grouped browser observations, paired counterfactual, comparisons, findings and commands. Response/request bodies, auth material, cookies, prompts, transcripts and private fixture paths are absent.",
            "",
        ]
    )
    for command in analysis["commands"]:
        lines.append("    " + command)
    lines.extend(
        [
            "",
            f"Validation: secrets absent = {analysis['validation']['secrets_absent']}; product source mutated = {analysis['validation']['product_source_mutated']}; live profile = {analysis['validation']['live_profiled']}; raw response bodies retained = {analysis['validation']['raw_response_bodies_retained']}; raw timing samples retained = {analysis['validation']['raw_timing_samples_retained']}.",
            f"Live coverage: BLOCKED — {analysis['live_coverage']['missing_input']}. Four-second symptom status: {analysis['live_coverage']['symptom']}.",
            "",
            "The next implementation worker must repeat focused same-fixture measurements and semantic regressions from the separate plan. Full suites, serialized integration, publication, CI and live activation remain coordinator-owned.",
        ]
    )
    return NL.join(lines) + NL


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze bounded CP2 UI data-latency discovery evidence."
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--browser-baseline", type=Path, required=True)
    parser.add_argument("--browser-counterfactual", type=Path, required=True)
    parser.add_argument("--product-source-sha", required=True)
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=REPO_ROOT / "plans" / "research" / "ui-data-latency-20260912",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if not PRODUCT_SHA_RE.fullmatch(args.product_source_sha):
            raise ValueError("--product-source-sha must be a 40-character lowercase Git SHA")
        if args.product_source_sha != resolved_product_source_sha():
            raise ValueError(
                "--product-source-sha does not match the committed product source revision"
            )
        report_dir = args.report_dir.expanduser().resolve()
        baseline = load_json(args.baseline)
        profile = load_json(args.profile)
        browser_normal = load_json(args.browser_baseline)
        browser_counterfactual = load_json(args.browser_counterfactual)
        historical_baseline = load_historical_baseline(args.baseline, report_dir)
        analysis = build_analysis(
            baseline,
            profile,
            browser_normal,
            browser_counterfactual,
            args.product_source_sha,
            historical_baseline,
        )
        merged = dict(baseline)
        if historical_baseline:
            merged["superseded_evidence"] = {
                "status": "superseded",
                "reason": "prior canonical report used smoke-only browser coverage and mixed fixture claims; F1 selected-readiness acceptance was stale-data susceptible, F5 timing comparisons mixed document and action origins, and F6 treated Refresh configuration prerequisites as optional",
                "superseded_fields": [
                    "selected summary, checkpoint-history and explicit-refresh readiness observations",
                    "required-barrier gaps and optional-after-useful classifications derived from mixed timing origins",
                    "Refresh request-role counts and dependency attribution for configuration prerequisites",
                ],
                "source": "previous plans/research/ui-data-latency-20260912/baseline.json",
                "baseline": audit_baseline(historical_baseline),
            }
        merged["cp2"] = analysis
        write_json(report_dir / "baseline.json", merged)
        (report_dir / "report.md").write_text(
            corrected_report_markdown(merged, analysis, args.product_source_sha),
            encoding="utf-8",
        )
        print(f"baseline: {report_dir / 'baseline.json'}")
        print(f"report: {report_dir / 'report.md'}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

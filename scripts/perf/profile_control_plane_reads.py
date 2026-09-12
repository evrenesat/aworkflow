#!/usr/bin/env python3
"""Profile bounded control-plane read paths on disposable fixture projects.

This is a discovery tool, not a product benchmark.  It composes the existing
control-plane service with an in-memory unit observer, profiles one serial
request-shaped call at a time, and retains only sanitized timings, counters,
function names and payload sizes.  Fixture creation and daemon startup happen
outside the measured region so the result describes request work rather than
setup or workflow execution.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import cProfile
from dataclasses import dataclass
from io import StringIO
import json
import os
from pathlib import Path
import pstats
import re
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_SRC = REPO_ROOT / "apps" / "aflow_app" / "server" / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(SERVER_SRC))


PRODUCT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_REPEATS = 5
PROFILE_FUNCTION_LIMIT = 20


@dataclass(frozen=True)
class Operation:
    """One endpoint-shaped service read and its sanitized path label."""

    name: str
    endpoint: str
    invoke: Callable[[Any, Any], Any]


def _list_runs(service: Any, fixture: Any) -> Any:
    return service.list_runs(
        fixture.project_id,
        limit=100,
        cursor=None,
        history="visible",
    )


def _list_plans(service: Any, fixture: Any) -> Any:
    return service.list_plans(fixture.project_id, limit=100, cursor=None)


def _selected_detail(service: Any, fixture: Any) -> Any:
    return service.run_status(fixture.project_id, fixture.selected_run_id)


def _selected_events(service: Any, fixture: Any) -> Any:
    return service.events(
        fixture.project_id,
        fixture.selected_run_id,
        after_sequence=None,
        limit=100,
    )


def _selected_context_lite(service: Any, fixture: Any) -> Any:
    return service.context(
        fixture.project_id,
        fixture.selected_run_id,
        level="lite",
        full_scope=False,
    )


def _selected_context_full(service: Any, fixture: Any) -> Any:
    return service.context(
        fixture.project_id,
        fixture.selected_run_id,
        level="full",
        full_scope=True,
    )


OPERATIONS = (
    # The list endpoint is deliberately first: CP1 browser evidence showed
    # this request as the slowest repeated isolated API path.
    Operation(
        "list_runs",
        "/api/control-plane/projects/{project_id}/runs?limit=100&history=visible",
        _list_runs,
    ),
    Operation(
        "list_plans",
        "/api/control-plane/projects/{project_id}/plans?limit=100",
        _list_plans,
    ),
    Operation(
        "selected_detail",
        "/api/control-plane/projects/{project_id}/runs/{run_id}",
        _selected_detail,
    ),
    Operation(
        "selected_events",
        "/api/control-plane/projects/{project_id}/runs/{run_id}/events?limit=100",
        _selected_events,
    ),
    Operation(
        "selected_context_lite",
        "/api/control-plane/projects/{project_id}/runs/{run_id}/context?level=lite",
        _selected_context_lite,
    ),
    Operation(
        "selected_context_full",
        "/api/control-plane/projects/{project_id}/runs/{run_id}/context?level=full&full_scope=true",
        _selected_context_full,
    ),
)


def numeric_summary(values: Sequence[float]) -> dict[str, Any]:
    """Return a bounded median and observed range, never a synthetic percentile."""

    if not values:
        return {"count": 0, "median_ms": None, "min_ms": None, "max_ms": None}
    return {
        "count": len(values),
        "median_ms": round(statistics.median(values), 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }


def safe_error(error: Any) -> str:
    """Keep failure evidence bounded and free of exception object details."""

    text = " ".join(str(error).split())
    text = re.sub(r"(?<![\w:])/(?:[^/\s]+/)+[^/\s]+", "<path>", text)
    return text[:300] or type(error).__name__


def canonical_payload(value: Any) -> Any:
    """Convert domain values to the same bounded public serializer shape."""

    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): canonical_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical_payload(item) for item in value]
    return value


def invoke_and_measure(operation: Operation, service: Any, fixture: Any) -> dict[str, Any]:
    """Measure core service work and canonical serialization separately."""

    try:
        core_wall_started = time.perf_counter()
        core_cpu_started = time.process_time()
        value = operation.invoke(service, fixture)
        core_cpu_ms = (time.process_time() - core_cpu_started) * 1000
        core_wall_ms = (time.perf_counter() - core_wall_started) * 1000

        serialization_wall_started = time.perf_counter()
        serialization_cpu_started = time.process_time()
        encoded = json.dumps(
            canonical_payload(value),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        serialization_cpu_ms = (time.process_time() - serialization_cpu_started) * 1000
        serialization_wall_ms = (time.perf_counter() - serialization_wall_started) * 1000
        return {
            "error": None,
            "core_wall_ms": round(core_wall_ms, 3),
            "core_cpu_ms": round(core_cpu_ms, 3),
            "serialization_wall_ms": round(serialization_wall_ms, 3),
            "serialization_cpu_ms": round(serialization_cpu_ms, 3),
            "payload_bytes": len(encoded),
            "total_wall_ms": round(core_wall_ms + serialization_wall_ms, 3),
            "total_cpu_ms": round(core_cpu_ms + serialization_cpu_ms, 3),
        }
    except Exception as exc:
        return {
            "error": safe_error(exc),
            "error_type": type(exc).__name__,
        }


def projection_count_breakdown(counts: Mapping[str, int]) -> dict[str, int]:
    """Expose one canonical projection count and nested diagnostics separately."""

    return {
        "repository_with_progress_calls": int(
            counts.get("repository_with_progress_calls", 0)
        ),
        "progress_summary_calls": int(counts.get("progress_summary_calls", 0)),
        "progress_detail_calls": int(counts.get("progress_detail_calls", 0)),
    }


def summarize_timing_samples(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Recompute timing summaries from successful retained raw samples."""

    successful = [sample for sample in samples if sample.get("error") is None]
    return {
        "wall_ms": numeric_summary(
            [float(sample["total_wall_ms"]) for sample in successful]
        ),
        "cpu_ms": numeric_summary(
            [float(sample["total_cpu_ms"]) for sample in successful]
        ),
        "core_wall_ms": numeric_summary(
            [float(sample["core_wall_ms"]) for sample in successful]
        ),
        "serialization_wall_ms": numeric_summary(
            [float(sample["serialization_wall_ms"]) for sample in successful]
        ),
    }


@contextmanager
def without_progress_projection(service: Any, fixture: Any) -> Iterator[None]:
    """Temporarily remove only the additive progress observer in memory.

    This is a bounded causal counterfactual for discovery.  It changes no
    source or fixture artifact and is restored before the next operation.
    Canonical status, history and permission reads still execute.
    """

    item = service._projects[fixture.project_id]
    repository = item.daemon.application.repository
    original = repository._with_progress
    repository._with_progress = lambda status, _run_dir, _metadata: status
    try:
        yield
    finally:
        repository._with_progress = original


@contextmanager
def projection_probe(service: Any) -> Iterator[dict[str, int]]:
    """Count the canonical repository projection and its nested helpers."""

    import aflow.control_plane.repository as repository_module
    from aflow.control_plane import run_progress

    repository_class = repository_module.RunRepository
    original_repository_projection = repository_class._with_progress
    original_summary = run_progress.project_run_progress_summary
    original_detail = run_progress.project_run_progress_detail
    counts = {
        "repository_with_progress_calls": 0,
        "progress_summary_calls": 0,
        "progress_detail_calls": 0,
    }

    def counted_repository_projection(*args: Any, **kwargs: Any) -> Any:
        counts["repository_with_progress_calls"] += 1
        return original_repository_projection(*args, **kwargs)

    def counted_summary(*args: Any, **kwargs: Any) -> Any:
        counts["progress_summary_calls"] += 1
        return original_summary(*args, **kwargs)

    def counted_detail(*args: Any, **kwargs: Any) -> Any:
        counts["progress_detail_calls"] += 1
        return original_detail(*args, **kwargs)

    repository_class._with_progress = staticmethod(counted_repository_projection)
    run_progress.project_run_progress_summary = counted_summary
    run_progress.project_run_progress_detail = counted_detail
    try:
        yield counts
    finally:
        repository_class._with_progress = staticmethod(original_repository_projection)
        run_progress.project_run_progress_summary = original_summary
        run_progress.project_run_progress_detail = original_detail


@contextmanager
def io_probe() -> Iterator[dict[str, int]]:
    """Count bounded Python file and subprocess entrypoints for one call."""

    counts = {
        "path_read_bytes": 0,
        "path_read_text": 0,
        "path_stat": 0,
        "path_iterdir": 0,
        "path_glob": 0,
        "subprocess_run": 0,
        "subprocess_git": 0,
        "subprocess_other": 0,
    }

    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    original_stat = Path.stat
    original_iterdir = Path.iterdir
    original_glob = Path.glob
    original_run = subprocess.run

    def counted_read_bytes(path: Path, *args: Any, **kwargs: Any) -> bytes:
        counts["path_read_bytes"] += 1
        return original_read_bytes(path, *args, **kwargs)

    def counted_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        counts["path_read_text"] += 1
        return original_read_text(path, *args, **kwargs)

    def counted_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        counts["path_stat"] += 1
        return original_stat(path, *args, **kwargs)

    def counted_iterdir(path: Path, *args: Any, **kwargs: Any) -> Any:
        counts["path_iterdir"] += 1
        return original_iterdir(path, *args, **kwargs)

    def counted_glob(path: Path, *args: Any, **kwargs: Any) -> Any:
        counts["path_glob"] += 1
        return original_glob(path, *args, **kwargs)

    def counted_run(*args: Any, **kwargs: Any) -> Any:
        counts["subprocess_run"] += 1
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, (tuple, list)) and command and command[0] == "git":
            counts["subprocess_git"] += 1
        else:
            counts["subprocess_other"] += 1
        return original_run(*args, **kwargs)

    Path.read_bytes = counted_read_bytes  # type: ignore[method-assign]
    Path.read_text = counted_read_text  # type: ignore[method-assign]
    Path.stat = counted_stat  # type: ignore[method-assign]
    Path.iterdir = counted_iterdir  # type: ignore[method-assign]
    Path.glob = counted_glob  # type: ignore[method-assign]
    subprocess.run = counted_run  # type: ignore[assignment]
    try:
        yield counts
    finally:
        Path.read_bytes = original_read_bytes  # type: ignore[method-assign]
        Path.read_text = original_read_text  # type: ignore[method-assign]
        Path.stat = original_stat  # type: ignore[method-assign]
        Path.iterdir = original_iterdir  # type: ignore[method-assign]
        Path.glob = original_glob  # type: ignore[method-assign]
        subprocess.run = original_run  # type: ignore[assignment]


def safe_module_name(filename: str) -> str:
    """Keep pstats source labels useful without retaining machine paths."""

    normalized = filename.replace("\\", "/")
    for marker in ("apps/aflow_app/server/src/", "aflow/"):
        index = normalized.rfind(marker)
        if index >= 0:
            return normalized[index:]
    return Path(normalized).name


def profile_functions(profile: cProfile.Profile) -> tuple[list[dict[str, Any]], int, int]:
    """Extract top cumulative functions and aggregate projection calls."""

    stats = pstats.Stats(profile, stream=StringIO())
    ordered = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)
    top: list[dict[str, Any]] = []
    progress_calls = 0
    for (filename, line_number, function_name), values in ordered:
        primitive_calls, total_calls, self_time, cumulative_time, _callers = values
        if (
            function_name == "_with_progress"
            and "control_plane/repository.py" in str(filename).replace("\\", "/")
        ):
            progress_calls += int(total_calls)
        if len(top) >= PROFILE_FUNCTION_LIMIT:
            continue
        top.append(
            {
                "module": safe_module_name(str(filename)),
                "line": int(line_number),
                "function": str(function_name)[:160],
                "primitive_calls": int(primitive_calls),
                "calls": int(total_calls),
                "self_ms": round(float(self_time) * 1000, 3),
                "cumulative_ms": round(float(cumulative_time) * 1000, 3),
            }
        )
    return top, int(stats.total_calls), progress_calls


def run_operation(
    operation: Operation,
    service: Any,
    fixture: Any,
    repeats: int,
    context_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Collect baseline repeats followed by one instrumented profile call."""

    context_factory = context_factory or nullcontext
    baseline_samples: list[dict[str, Any]] = []
    for call_index in range(1, repeats + 1):
        sample = _invoke_in_context(operation, service, fixture, context_factory)
        sample["call_index"] = call_index
        baseline_samples.append(sample)
    successful_baseline = [
        item for item in baseline_samples if item.get("error") is None
    ]
    if not successful_baseline:
        raise RuntimeError(f"{operation.name} produced no successful baseline samples")
    profile = cProfile.Profile()
    with projection_probe(service) as projection_counts:
        with context_factory():
            with io_probe() as counts:
                profile_wall_started = time.perf_counter()
                profile_cpu_started = time.process_time()
                profiled_value = profile.runcall(
                    invoke_and_measure,
                    operation,
                    service,
                    fixture,
                )
                profiled_cpu_ms = (time.process_time() - profile_cpu_started) * 1000
                profiled_wall_ms = (time.perf_counter() - profile_wall_started) * 1000
    top_functions, total_calls, pstats_progress_calls = profile_functions(profile)
    projection_breakdown = projection_count_breakdown(projection_counts)

    baseline_wall = [
        float(item["total_wall_ms"]) for item in successful_baseline
    ]
    baseline_median_wall = statistics.median(baseline_wall)
    wall_delta = profiled_wall_ms - baseline_median_wall
    wall_ratio = profiled_wall_ms / baseline_median_wall if baseline_median_wall else None
    profiled_error = profiled_value.get("error")
    profiled_sample: dict[str, Any] = {
        "call_index": repeats + 1,
        "error": profiled_error,
        "wall_ms": round(profiled_wall_ms, 3),
        "cpu_ms": round(profiled_cpu_ms, 3),
    }
    for field in (
        "core_wall_ms",
        "core_cpu_ms",
        "serialization_wall_ms",
        "serialization_cpu_ms",
        "payload_bytes",
    ):
        if field in profiled_value:
            profiled_sample[field] = profiled_value[field]
    return {
        "name": operation.name,
        "endpoint": operation.endpoint,
        "baseline": {
            "repeats": repeats,
            "samples": baseline_samples,
            "successful_repeats": len(successful_baseline),
            "failed_repeats": len(baseline_samples) - len(successful_baseline),
            "partial": len(successful_baseline) != len(baseline_samples),
            **summarize_timing_samples(baseline_samples),
        },
        "profiled": {
            "raw_sample": profiled_sample,
            "error": profiled_error,
            "wall_ms": round(profiled_wall_ms, 3),
            "cpu_ms": round(profiled_cpu_ms, 3),
            "core_wall_ms": profiled_value.get("core_wall_ms"),
            "core_cpu_ms": profiled_value.get("core_cpu_ms"),
            "serialization_wall_ms": profiled_value.get("serialization_wall_ms"),
            "serialization_cpu_ms": profiled_value.get("serialization_cpu_ms"),
            "payload_bytes": profiled_value.get("payload_bytes"),
            "python_call_count": total_calls,
            "progress_projection_call_count": projection_breakdown[
                "repository_with_progress_calls"
            ],
            "progress_summary_call_count": projection_breakdown[
                "progress_summary_calls"
            ],
            "progress_detail_call_count": projection_breakdown[
                "progress_detail_calls"
            ],
            "pstats_repository_with_progress_call_count": pstats_progress_calls,
            "top_functions": top_functions,
        },
        "profiling_overhead": {
            "wall_delta_ms": round(wall_delta, 3),
            "wall_ratio": round(wall_ratio, 4) if wall_ratio is not None else None,
            "baseline_reference": "same service and fixture; median of serial unprofiled calls",
            "partial_baseline": len(successful_baseline) != len(baseline_samples),
        },
        "io": counts,
    }


def _invoke_in_context(
    operation: Operation,
    service: Any,
    fixture: Any,
    context_factory: Callable[[], Any],
) -> dict[str, Any]:
    with context_factory():
        return invoke_and_measure(operation, service, fixture)


def service_for_fixture(fixture: Any) -> Any:
    """Compose the real service over the fixture without systemd or workers."""

    from aflow.control_plane.units import InMemoryUnitManager
    from aflow_app_server.control_plane_service import ControlPlaneService
    from aflow_app_server.project_registry import ProjectRegistry

    registry = ProjectRegistry(
        fixture.root / "managed",
        fixture.config_dir / "projects.json",
    )
    service = ControlPlaneService(
        registry,
        aflow_executable=fixture.root / "release" / "bin" / "aflow",
        environment_file=fixture.root / "aflowd.env",
        release_identity="fixture-source",
        environment={},
        unit_manager_factory=lambda: InMemoryUnitManager(),
        workflow_config_path=fixture.config_dir / "aflow.toml",
    )
    service.start()
    readiness = service.readiness().get(fixture.project_id)
    if readiness is not None:
        raise RuntimeError(f"isolated fixture control plane was not ready: {readiness}")
    return service


def runner_source_sha() -> str:
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
            "scripts/perf/profile_control_plane_reads.py",
            "scripts/perf/profile_ui_data_load.py",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "control-plane runner source is dirty; commit the runners before collecting shareable evidence"
        )
    result = subprocess.run(
        (
            "git",
            "-C",
            str(REPO_ROOT),
            "--no-optional-locks",
            "rev-parse",
            "HEAD",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def host_activity() -> dict[str, Any]:
    """Reuse only bounded ambient host facts; never attribute external work."""

    load_average = None
    try:
        load_average = [round(float(value), 3) for value in os.getloadavg()[:3]]
    except (AttributeError, OSError):
        pass
    return {
        "host_label": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "cpu_count": os.cpu_count(),
        "load_average": load_average,
        "external_workflows": "ambient load is un-attributed; profile remained serial",
    }


def validate_args(args: argparse.Namespace) -> None:
    if not PRODUCT_SHA_RE.fullmatch(args.product_source_sha):
        raise ValueError("--product-source-sha must be a 40-character lowercase Git SHA")
    actual_product_sha = product_source_sha()
    if args.product_source_sha != actual_product_sha:
        raise ValueError(
            "--product-source-sha does not match the committed product source revision "
            f"{actual_product_sha}"
        )
    if not 1 <= args.repeats <= MAX_REPEATS:
        raise ValueError(f"--repeats must be between 1 and {MAX_REPEATS}")
    if args.artifact_dir is not None:
        artifact_dir = args.artifact_dir.expanduser().resolve()
        try:
            artifact_dir.relative_to(REPO_ROOT)
        except ValueError:
            pass
        else:
            raise ValueError("--artifact-dir must be outside the repository")


def collect(args: argparse.Namespace) -> dict[str, Any]:
    from scripts.perf.profile_ui_data_load import build_fixture, fixture_specs

    product_source_snapshot_before = product_source_snapshot()
    fixture_records: list[dict[str, Any]] = []
    private_fixture_parent = tempfile.TemporaryDirectory(
        prefix="aflow-ui-data-latency-profile-"
    )
    try:
        for spec in fixture_specs(args.scenario):
            fixture = build_fixture(spec, args.seed, Path(private_fixture_parent.name))
            service = service_for_fixture(fixture)
            operations = [
                run_operation(operation, service, fixture, args.repeats)
                for operation in OPERATIONS
            ]
            counterfactuals = [
                run_operation(
                    Operation(
                        "list_runs_without_progress_projection",
                        OPERATIONS[0].endpoint,
                        _list_runs,
                    ),
                    service,
                    fixture,
                    args.repeats,
                    context_factory=lambda: without_progress_projection(
                        service, fixture
                    ),
                )
            ]
            fixture_records.append(
                {
                    "name": spec.name,
                    "run_count": fixture.run_count,
                    "selected_event_count": fixture.selected_event_count,
                    "other_event_count": fixture.other_event_count,
                    "history_shape": spec.history_shape,
                    "selected_run_id": fixture.selected_run_id,
                    "representative_run_id": fixture.representative_run_id,
                    "file_count": fixture.file_count,
                    "identity": (
                        f"seed={args.seed}; deterministic sanitized plan; fixed selected run; "
                        "selected history varies independently from list size"
                    ),
                    "operations": operations,
                    "counterfactuals": counterfactuals,
                }
            )
    finally:
        private_fixture_parent.cleanup()
    product_source_snapshot_after = product_source_snapshot()
    return {
        "schema_version": 1,
        "kind": "control-plane-read-profile",
        "discovery": {
            "target": "isolated-control-plane-service",
            "product_source_sha": args.product_source_sha,
            "runner_source_sha": runner_source_sha(),
            "seed": args.seed,
            "repeats": args.repeats,
            "concurrency": 1,
            "host": host_activity(),
            "setup": "fixture creation and daemon reconciliation excluded from measured calls",
            "unit_observer": "in-memory; no systemd, workflow unit or live state",
            "contention": "no lock/queue wait probe; all measured calls were serial and no contention boundary was indicated",
        },
        "fixtures": fixture_records,
        "validation": {
            "product_source_mutated": product_source_snapshot_before
            != product_source_snapshot_after,
            "live_state_mutation": "none; disposable fixture only",
            "secrets_absent": True,
            "raw_payloads_retained": False,
            "raw_timing_samples_retained": True,
            "profile_failures_are_explicit": True,
            "paths_redacted": True,
            "overlapping_request_durations_summed": False,
        },
    }


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


def product_source_sha() -> str:
    """Resolve the committed product revision instead of trusting an input label."""

    if product_source_snapshot():
        raise RuntimeError(
            "product source is dirty; control-plane profiling requires an unchanged committed product tree"
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
    if not PRODUCT_SHA_RE.fullmatch(value):
        raise RuntimeError("could not determine the committed product source revision")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Profile bounded isolated aflow control-plane read paths."
    )
    parser.add_argument(
        "--scenario",
        choices=("isolated-small", "isolated-large", "isolated"),
        default="isolated",
        help="fixture scale to profile (default: isolated)",
    )
    parser.add_argument(
        "--product-source-sha",
        required=True,
        help="40-character SHA of the product source represented by the fixture",
    )
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help=f"serial unprofiled baseline calls per operation, 1-{MAX_REPEATS}",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="external private directory for the sanitized profile JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        artifact_dir = (
            args.artifact_dir.expanduser().resolve()
            if args.artifact_dir is not None
            else Path(tempfile.mkdtemp(prefix="aflow-ui-data-latency-profile-artifacts-"))
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        payload = collect(args)
        output = artifact_dir / "control-plane-profile.json"
        write_json(output, payload)
        print(f"profile: {output}")
        print(
            "fixtures: "
            + ", ".join(str(item["name"]) for item in payload["fixtures"])
        )
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

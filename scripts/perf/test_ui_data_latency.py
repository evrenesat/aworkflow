"""Focused regressions for the bounded UI data-latency discovery evidence."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from scripts.perf import analyze_ui_data_latency as analyzer
from scripts.perf import profile_control_plane_reads as control_plane
from scripts.perf import profile_ui_data_load as ui


@contextmanager
def browser_page() -> Iterator[Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        context.add_init_script(ui.profile_init_script())
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


@pytest.fixture
def isolated_fixture(tmp_path: Path) -> Iterator[tuple[Any, Any]]:
    fixture = ui.build_fixture(
        ui.fixture_specs("isolated-smoke")[0],
        20260912,
        tmp_path,
    )
    server = ui.IsolatedServer(fixture, ui.free_port())
    server.start()
    try:
        yield fixture, server
    finally:
        server.stop()


def _refresh_fetches(*, events_complete: bool) -> list[dict[str, Any]]:
    return [
        {
            "id": 0,
            "path": "/api/control-plane/projects/fixture-project/runs",
            "url": "http://127.0.0.1/api/control-plane/projects/fixture-project/runs",
            "fetch_error": False,
            "json_error": False,
            "json_end_ms": 10,
        },
        {
            "id": 1,
            "path": "/api/control-plane/projects/fixture-project/runs/run-0000",
            "url": "http://127.0.0.1/api/control-plane/projects/fixture-project/runs/run-0000",
            "fetch_error": False,
            "json_error": False,
            "json_end_ms": 11,
        },
        {
            "id": 2,
            "path": "/api/control-plane/projects/fixture-project/runs/run-0000/events",
            "url": "http://127.0.0.1/api/control-plane/projects/fixture-project/runs/run-0000/events",
            "fetch_error": False,
            "json_error": False,
            "json_end_ms": 12 if events_complete else None,
        },
    ]


def _refresh_snapshot(*, events_complete: bool) -> dict[str, Any]:
    fetches = _refresh_fetches(events_complete=events_complete)
    return {
        "fetches": fetches,
        "run_detail_mutation_times": [13] if events_complete else [],
        "run_detail_data_mutation_times": [13] if events_complete else [],
    }


def _accepted_dom() -> str:
    return """
    <section class="run-detail">
      <span title="Copy run ID">run-0000</span>
      <div class="run-progress-header"><span class="status-pill">Completed</span></div>
    </section>
    <section class="checkpoint-history" aria-label="Checkpoint history">
      <h4>Checkpoint progress</h4>
      <h5>Checkpoints</h5>
      <dl class="checkpoint-history-summary">
        <dd>CP2 of 2 · Completed — Checkpoint 2: Fixture tail</dd>
        <dd>1 / 2 approved</dd>
        <dd>1 checkpoint</dd>
      </dl>
      <article class="checkpoint-history-entry">Checkpoint 1</article>
    </section>
    """


def test_held_refresh_generation_cannot_complete_early() -> None:
    """A held required response blocks the browser and Python acceptance gates."""

    run_id = "run-0000"
    with browser_page() as page:
        page.set_content(_accepted_dom())
        page.evaluate(
            "snapshot => { window.__aflowLatencySnapshot = () => snapshot; window.__aflowLatencyProfile.status_values = {'1': 'done'}; }",
            _refresh_snapshot(events_complete=False),
        )
        assert page.evaluate(ui.REFRESH_GENERATION_EXPRESSION, run_id) is False

        accepted_snapshot = _refresh_snapshot(events_complete=True)
        accepted_snapshot["run_detail_mutation_times"] = [11]
        accepted_snapshot["run_detail_data_mutation_times"] = []
        page.evaluate(
            "snapshot => { window.__aflowLatencySnapshot = () => snapshot; window.__aflowLatencyProfile.status_values = {'1': 'failed'}; }",
            accepted_snapshot,
        )
        assert page.evaluate(ui.REFRESH_GENERATION_EXPRESSION, run_id) is False

        accepted_snapshot["run_detail_mutation_times"] = [13]
        accepted_snapshot["run_detail_data_mutation_times"] = [13]
        page.evaluate(
            "snapshot => { window.__aflowLatencySnapshot = () => snapshot; window.__aflowLatencyProfile.status_values = {'1': 'failed'}; }",
            accepted_snapshot,
        )
        assert page.evaluate(ui.REFRESH_GENERATION_EXPRESSION, run_id) is False

        page.locator(".run-progress-header .status-pill").evaluate(
            "element => { element.textContent = 'Failed'; }"
        )
        accepted_snapshot["run_detail_data_mutation_times"] = []
        assert page.evaluate(ui.REFRESH_GENERATION_EXPRESSION, run_id) is True

    held = ui.refresh_generation_status(
        _refresh_fetches(events_complete=False),
        run_id,
        rendered_state_accepted=True,
    )
    assert held["request_generation_complete"] is False
    assert held["accepted"] is False
    accepted = ui.refresh_generation_status(
        _refresh_fetches(events_complete=True),
        run_id,
        rendered_state_accepted=True,
    )
    assert accepted["accepted"] is True


def test_refresh_generation_ignores_superseded_completed_request() -> None:
    flow = _refresh_fetches(events_complete=True)
    flow.append(
        {
            "id": 3,
            "path": "/api/control-plane/projects/fixture-project/runs/run-0000/events",
            "fetch_error": False,
            "json_error": False,
            "json_end_ms": None,
        }
    )
    result = ui.refresh_generation_status(
        flow,
        "run-0000",
        rendered_state_accepted=True,
    )
    assert result["request_generation_complete"] is False
    assert result["required_completed_count"] == 2
    assert result["latest_paths"][-1].endswith("/events")


def test_accepted_generation_latch_is_bound_to_run_identity() -> None:
    with browser_page() as page:
        page.set_content(_accepted_dom())
        accepted = _refresh_snapshot(events_complete=True)
        page.evaluate(
            "snapshot => { window.__aflowLatencySnapshot = () => snapshot; window.__aflowLatencyProfile.status_values = {'1': 'done'}; }",
            accepted,
        )
        assert page.evaluate(
            "id => window.__aflowLatencySelectedGeneration(id)?.accepted",
            "run-0000",
        ) is True

        pending = _refresh_snapshot(events_complete=False)
        for item in pending["fetches"]:
            item["url"] = item["url"].replace("run-0000", "run-0001")
        page.evaluate(
            """snapshot => {
                document.querySelector('[title="Copy run ID"]').textContent = 'run-0001';
                window.__aflowLatencySnapshot = () => snapshot;
                window.__aflowLatencyProfile.status_values = {'1': 'done'};
            }""",
            pending,
        )
        assert page.evaluate(
            "id => window.__aflowLatencySelectedGeneration(id)?.accepted",
            "run-0001",
        ) is False


def test_selected_summary_rejects_stale_list_status_until_event_release(
    isolated_fixture: tuple[Any, Any],
) -> None:
    """The selected status must be accepted with its event-generation owner."""

    fixture, server = isolated_fixture
    run_id = fixture.selected_run_id
    held_event_routes: list[Any] = []

    def mutate_status(route: Any) -> None:
        response = route.fetch()
        payload = response.json()
        payload["status"] = "failed"
        route.fulfill(response=response, json=payload)

    def hold_events(route: Any) -> None:
        held_event_routes.append(route)

    with browser_page() as page:
        ui.login(page, server.url, fixture.token)
        page.route(
            f"**/api/control-plane/projects/{fixture.project_id}/runs/{run_id}",
            mutate_status,
        )
        page.route(
            f"**/api/control-plane/projects/{fixture.project_id}/runs/{run_id}/events**",
            hold_events,
        )
        route = ui.route_specs(server.url, fixture.project_id, run_id)[2]
        page.goto(route.path, wait_until="domcontentloaded", timeout=ui.WAIT_TIMEOUT_MS)
        page.wait_for_function(
            """(id) => (window.__aflowLatencySnapshot?.()?.fetches || []).some(item => {
                try {
                    return new URL(String(item.url || ''), window.location.href).pathname.endsWith('/runs/' + id) &&
                        item.json_end_ms != null;
                } catch (_) { return false; }
            })""",
            arg=run_id,
            timeout=ui.WAIT_TIMEOUT_MS,
        )
        page.wait_for_function(
            """(id) => (window.__aflowLatencySnapshot?.()?.fetches || []).some(item => {
                try {
                    return new URL(String(item.url || ''), window.location.href).pathname.endsWith('/runs/' + id + '/events') &&
                        item.json_end_ms == null;
                } catch (_) { return false; }
            })""",
            arg=run_id,
            timeout=ui.WAIT_TIMEOUT_MS,
        )
        assert held_event_routes
        status_pill = page.locator(".run-progress-header .status-pill").first
        assert status_pill.inner_text() == "Completed"
        with pytest.raises(PlaywrightTimeoutError):
            page.wait_for_function(
                route.useful_expression,
                arg=route.useful_argument,
                timeout=500,
            )

        for held_route in held_event_routes:
            held_route.continue_()
        ui.wait_useful(page, route)
        assert status_pill.inner_text() == "Failed"


def test_refresh_waits_for_configuration_prerequisite(
    isolated_fixture: tuple[Any, Any],
) -> None:
    """Refresh must finish its configuration owner before selected data starts."""

    fixture, server = isolated_fixture
    run_id = fixture.selected_run_id
    held_config_routes: list[Any] = []
    config_pattern = "**/api/config"

    def hold_config(route: Any) -> None:
        held_config_routes.append(route)

    with browser_page() as page:
        ui.login(page, server.url, fixture.token)
        history_route = ui.route_specs(
            server.url,
            fixture.project_id,
            run_id,
        )[3]
        page.goto(
            history_route.path,
            wait_until="domcontentloaded",
            timeout=ui.WAIT_TIMEOUT_MS,
        )
        ui.wait_useful(page, history_route)
        capture = ui.BrowserCapture(page, server.url)
        capture.reset()
        page.route(config_pattern, hold_config)
        released = False
        try:
            page.locator(".app-header-row-two").get_by_role(
                "button", name="More", exact=True
            ).click()
            page.get_by_role("menuitem", name="Refresh", exact=True).click()
            page.wait_for_function(
                """() => {
                    const fetches = window.__aflowLatencySnapshot?.()?.fetches || [];
                    const path = item => {
                        try { return new URL(String(item.url || ''), location.href).pathname; }
                        catch (_) { return ''; }
                    };
                    const completed = expected => fetches.some(item =>
                        path(item) === expected && item.json_end_ms != null);
                    const started = expected => fetches.some(item =>
                        path(item) === expected);
                    return completed('/api/control-plane/projects/fixture-project/runs') &&
                        fetches.some(item => path(item) === '/api/config' && item.json_end_ms == null) &&
                        !started('/api/control-plane/projects/fixture-project/runs/run-0000') &&
                        !started('/api/control-plane/projects/fixture-project/runs/run-0000/events');
                }""",
                timeout=5_000,
            )
            assert held_config_routes
            assert page.evaluate(
                ui.REFRESH_GENERATION_EXPRESSION,
                arg=run_id,
            ) is False

            held_config_routes[0].continue_()
            released = True
            page.unroute(config_pattern, hold_config)
            page.wait_for_function(
                ui.REFRESH_GENERATION_EXPRESSION,
                arg=run_id,
                timeout=ui.WAIT_TIMEOUT_MS,
            )
            snapshot = capture.snapshot()
            flow = snapshot["api_request_flow"]
            assert any(
                item["path"] == "/api/config"
                and item["json_end_ms"] is not None
                for item in flow
            )
            assert any(
                item["path"] == "/api/config/form"
                and item["json_end_ms"] is not None
                for item in flow
            )
            generation = ui.refresh_generation_status(
                flow,
                run_id,
                rendered_state_accepted=True,
            )
            assert generation["accepted"] is True
        finally:
            if not released:
                for route in held_config_routes:
                    try:
                        route.continue_()
                    except Exception:
                        pass
                page.unroute(config_pattern, hold_config)


def test_refresh_request_roles_include_blocking_prerequisites() -> None:
    assert analyzer.request_role("explicit_refresh", "/api/config") == "blocking_prerequisite"
    assert analyzer.request_role("explicit_refresh", "/api/config/form") == "blocking_prerequisite"
    assert analyzer.request_role("explicit_refresh", "/ready") == "blocking_prerequisite"
    assert analyzer.request_role(
        "explicit_refresh",
        "/api/control-plane/projects/fixture-project/capabilities",
    ) == "blocking_prerequisite"
    assert analyzer.request_role(
        "explicit_refresh",
        "/api/control-plane/projects/fixture-project/plans",
    ) == "blocking_prerequisite"
    assert analyzer.request_role("selected_run_summary", "/api/config") == "optional_configuration"
    assert analyzer.request_role("checkpoint_history", "/api/config/form") == "optional_configuration"
    assert analyzer.request_role(
        "selected_run_summary",
        "/api/control-plane/projects/fixture-project/capabilities",
    ) == "support"

    def flow_item(path: str, end: float) -> dict[str, Any]:
        return {
            "path": path,
            "fetch_start_from_action_ms": end - 1,
            "json_end_from_action_ms": end,
            "response_resolved_from_action_ms": end,
        }

    sample = {
        "route": "explicit_refresh",
        "sample_kind": "warm",
        "useful_data_ms": 100,
        "browser_useful_after_action_ms": 100,
        "api_request_flow": [
            flow_item("/api/config", 10),
            flow_item("/api/config/form", 20),
            flow_item("/api/control-plane/projects/fixture-project/capabilities", 30),
            flow_item("/api/control-plane/projects/fixture-project/plans", 40),
            flow_item("/api/control-plane/projects/fixture-project/runs", 50),
            flow_item("/api/control-plane/projects/fixture-project/runs/run-0000", 60),
            flow_item("/api/control-plane/projects/fixture-project/runs/run-0000/events", 70),
            flow_item("/api/control-plane/projects/fixture-project/runs/run-0000/context", 80),
        ],
        "network": [],
        "duplicate_api_paths": [],
        "main_thread_long_tasks": [],
    }
    result = analyzer.analyze_sample(sample)
    assert result["required_request_count"] == 3
    assert result["blocking_prerequisite_count"] == 4
    assert result["action_required_request_count"] == 7
    assert result["optional_request_count"] == 1
    assert result["blocking_prerequisite_barrier_ms"] == 40
    assert result["required_barrier_ms"] == 70


def test_historical_audit_copy_is_not_recursive() -> None:
    prior = {
        "samples": [{"route": "explicit_refresh"}],
        "cp2": {"browser_runtime": {"normal_samples": []}},
        "superseded_evidence": {"baseline": {"samples": [{"route": "old"}]}},
    }
    audit = analyzer.audit_baseline(prior)
    assert audit["samples"] == prior["samples"]
    assert audit["cp2"] == prior["cp2"]
    assert "superseded_evidence" not in audit


def test_real_fixture_checkpoint_history_requires_returned_content(
    isolated_fixture: tuple[Any, Any],
) -> None:
    fixture, server = isolated_fixture
    with browser_page() as page:
        ui.login(page, server.url, fixture.token)
        route = ui.route_specs(
            server.url,
            fixture.project_id,
            fixture.selected_run_id,
        )[3]
        page.goto(route.path, wait_until="domcontentloaded", timeout=ui.WAIT_TIMEOUT_MS)
        ui.wait_useful(page, route)
        page.evaluate(
            """() => {
                const snapshot = window.__aflowLatencySnapshot?.() || {};
                const fetches = Array.isArray(snapshot.fetches) ? snapshot.fetches : [];
                const nextId = fetches.reduce(
                    (maximum, item) => Math.max(maximum, Number(item.id) || 0),
                    0,
                ) + 1;
                fetches.push({
                    id: nextId,
                    url: location.origin + '/api/control-plane/projects/fixture-project/runs/run-0000/events',
                    fetch_error: false,
                    json_error: false,
                    json_end_ms: null,
                });
                snapshot.fetches = fetches;
                window.__aflowLatencySnapshot = () => snapshot;
            }"""
        )
        assert page.evaluate(route.useful_expression, arg=route.useful_argument) is True
        history = page.locator(
            'section.checkpoint-history[aria-label="Checkpoint history"]'
        )
        assert history.locator(".checkpoint-history-summary").count() == 1
        assert history.locator(".checkpoint-history-entry").count() > 0


def test_empty_history_is_not_reported_as_useful(
    isolated_fixture: tuple[Any, Any],
) -> None:
    fixture, server = isolated_fixture
    with browser_page() as page:
        ui.login(page, server.url, fixture.token)
        route = ui.route_specs(
            server.url,
            fixture.project_id,
            fixture.selected_run_id,
        )[3]
        page.goto(route.path, wait_until="domcontentloaded", timeout=ui.WAIT_TIMEOUT_MS)
        ui.wait_useful(page, route)
        page.evaluate(
            """() => {
                const history = document.querySelector(
                    'section.checkpoint-history[aria-label="Checkpoint history"]'
                );
                history?.querySelector('.checkpoint-history-summary')?.remove();
                history?.querySelectorAll('.checkpoint-history-entry').forEach(
                    entry => entry.remove()
                );
            }"""
        )
        history = page.locator(
            'section.checkpoint-history[aria-label="Checkpoint history"]'
        )
        assert history.locator("h4").count() == 1
        assert history.locator(".checkpoint-history-summary").count() == 0
        assert history.locator(".checkpoint-history-entry").count() == 0
        with pytest.raises(PlaywrightTimeoutError):
            page.wait_for_function(
                route.useful_expression,
                arg=route.useful_argument,
                timeout=500,
            )


def test_projection_breakdown_does_not_add_nested_helpers() -> None:
    breakdown = control_plane.projection_count_breakdown(
        {
            "repository_with_progress_calls": 300,
            "progress_summary_calls": 300,
            "progress_detail_calls": 300,
        }
    )
    assert breakdown == {
        "repository_with_progress_calls": 300,
        "progress_summary_calls": 300,
        "progress_detail_calls": 300,
    }


def test_timing_summary_recomputes_from_retained_raw_samples() -> None:
    samples = [
        {
            "call_index": 1,
            "error": None,
            "total_wall_ms": 10,
            "total_cpu_ms": 4,
            "core_wall_ms": 8,
            "serialization_wall_ms": 2,
        },
        {
            "call_index": 2,
            "error": None,
            "total_wall_ms": 30,
            "total_cpu_ms": 12,
            "core_wall_ms": 24,
            "serialization_wall_ms": 6,
        },
        {
            "call_index": 3,
            "error": "TimeoutError",
        },
    ]
    first = control_plane.summarize_timing_samples(samples)
    assert first["wall_ms"]["median_ms"] == 20
    samples[1]["total_wall_ms"] = 50
    second = control_plane.summarize_timing_samples(samples)
    assert second["wall_ms"]["median_ms"] == 30
    assert second["wall_ms"] != first["wall_ms"]


def test_profile_failure_is_bounded_and_redacted() -> None:
    def fail(_service: Any, _fixture: Any) -> Any:
        raise RuntimeError("/tmp/private-fixture/secret response")

    result = control_plane.invoke_and_measure(
        control_plane.Operation("failing", "/private", fail),
        None,
        None,
    )
    assert result["error_type"] == "RuntimeError"
    assert "<path>" in result["error"]
    assert "/tmp" not in result["error"]
    assert "secret response" not in result["error"]
    assert "core_wall_ms" not in result


def test_analyzer_medians_follow_input_timings() -> None:
    observations = [
        {
            "fixture_name": "small-short-history",
            "route": "selected_run_summary",
            "sample_kind": "warm",
            "useful_data_ms": 10,
        },
        {
            "fixture_name": "small-short-history",
            "route": "selected_run_summary",
            "sample_kind": "warm",
            "useful_data_ms": 30,
        },
    ]
    first = analyzer.summarize_observations(observations)
    assert first[0]["useful_data_ms"]["median_ms"] == 20
    observations[1]["useful_data_ms"] = 50
    second = analyzer.summarize_observations(observations)
    assert second[0]["useful_data_ms"]["median_ms"] == 30


def test_action_relative_timing_ignores_document_age() -> None:
    def sample(document_origin: float) -> dict[str, Any]:
        def flow_item(path: str, end: float) -> dict[str, Any]:
            return {
                "path": path,
                "fetch_start_ms": document_origin + 10,
                "json_end_ms": document_origin + end,
                "response_resolved_ms": document_origin + end,
                "fetch_start_from_action_ms": 10,
                "json_end_from_action_ms": end,
                "response_resolved_from_action_ms": end,
            }

        return {
            "fixture_name": "small-short-history",
            "route": "selected_run_summary",
            "sample_kind": "warm",
            "useful_data_ready_ms": 500,
            "browser_action_start_ms": document_origin,
            "browser_accepted_useful_ms": document_origin + 500,
            "browser_useful_after_action_ms": 500,
            "api_request_flow": [
                flow_item("/api/control-plane/projects/fixture-project/runs", 100),
                flow_item("/api/control-plane/projects/fixture-project/runs/run-0000", 200),
                flow_item("/api/control-plane/projects/fixture-project/runs/run-0000/events", 400),
                flow_item("/api/control-plane/projects/fixture-project/runs/run-0000/events/stream", 550),
            ],
            "network": [],
            "duplicate_api_paths": [],
            "main_thread_long_tasks": [],
        }

    first = analyzer.analyze_sample(sample(1_000))
    aged = analyzer.analyze_sample(sample(11_000))
    for field in (
        "browser_useful_after_action_ms",
        "required_barrier_ms",
        "required_barrier_gap_ms",
        "optional_after_required_count",
        "optional_after_useful_count",
        "max_in_flight_api_requests",
    ):
        assert first[field] == aged[field], field
    assert first["required_barrier_ms"] == 400
    assert first["required_barrier_gap_ms"] == 100
    assert first["optional_after_useful_count"] == 1


def test_fixture_pairing_rejects_changed_selected_identity() -> None:
    def document(selected_run_id: str) -> dict[str, Any]:
        return {
            "discovery": {"product_source_sha": "a" * 40, "seed": 20260912},
            "fixtures": [
                {
                    "name": "small-short-history",
                    "run_count": 100,
                    "selected_run_id": selected_run_id,
                    "selected_event_count": 8,
                    "other_event_count": 2,
                    "history_shape": "short",
                    "identity": "same fixture",
                }
            ],
        }

    assert analyzer.matching_fixture_names(
        document("run-0000"), document("run-0000")
    ) == ["small-short-history"]
    with pytest.raises(ValueError, match="matching fixture"):
        analyzer.matching_fixture_names(document("run-0000"), document("run-0099"))

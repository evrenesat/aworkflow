"""Browser acceptance journey for exclusive execution resource waiting.

One cached production web build (``web/dist``) is consumed by both the
Chromium and WebKit engines (select the engine with
``AFLOW_TEST_BROWSER=chromium|webkit``).  All runs are disposable fixtures
created inside the throwaway test project; no model is launched, no paid
harness is called, and every status assertion flows through the real
control-plane projection (``project_activity``) over the live API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

from test_control_plane_api import PROJECT_ID, control_client, live_server
from test_responsive_browser import (
    _assert_action_hit_test,
    _assert_document_moves,
    _assert_no_horizontal_overflow,
    _browser,
    _login,
    _set_theme_preference,
)

from aflow.control_plane import LaunchManifest, create_launch_manifest, write_launch_phase
from aflow.control_plane import repository as repository_module
from aflow.control_plane.persistence import append_run_event
from aflow.config import execution_resource_key
from aflow.execution_resources import ControllerIdentity
from aflow.workflow import _resource_wait_record

WAITING_RUN_ID = "exclusive-wait-run"
TERMINAL_RUN_ID = "exclusive-terminal-run"
UNKNOWN_RUN_ID = "exclusive-unknown-run"
LONG_LABEL = "reasonix / very-long-model-name-2026 / effort high"
WAIT_MESSAGE = f"Waiting for {LONG_LABEL} (exclusive)"
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def _wait_record() -> dict:
    return _resource_wait_record(
        resource=execution_resource_key("reasonix", "very-long-model-name-2026", "high"),
        label=LONG_LABEL, invocation_id="fixture-invocation", kind="workflow",
        role="reviewer", selector="reasonix.review", step_name="review",
        ticket=2, reason=None, wait_started_at="2026-10-04T10:00:00+00:00",
        controller=ControllerIdentity(pid=4242, birth="fixture-birth", boot="fixture-boot"),
    )


def _seed_run(
    root: Path,
    run_id: str,
    *,
    plan_name: str,
    status: str,
    wait: bool,
    phase: str | None,
    manifest: bool = True,
) -> Path:
    plan = root / "plans" / "todo" / f"{plan_name}.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"# {plan_name}\n\n### [ ] Checkpoint 1: {plan_name}\n- [ ] step\n", encoding="utf-8")
    if manifest:
        create_launch_manifest(
            root,
            LaunchManifest(
                run_id=run_id,
                project_root=str(root.resolve()),
                plan_path=str(plan.resolve()),
                workflow_name="managed",
                max_turns=5,
                caller_scope=f"bearer:{PROJECT_ID}",
                intended_unit=f"aflow-run-{run_id}.service",
            ),
        )
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "run_started_at": "2026-10-04T10:00:00Z",
        "current_step_name": "implement",
        "turns_completed": 0,
        "max_turns": 5,
        "workflow_name": "managed",
        "team": "base-team",
    }
    if wait:
        payload["execution_resource_wait"] = _wait_record()
    (run_dir / "run.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if phase is not None:
        write_launch_phase(root, run_id, phase)
    return run_dir


def _clear_wait(root: Path, run_id: str) -> None:
    path = root / ".aflow" / "runs" / run_id / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("execution_resource_wait", None)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _restore_wait(root: Path, run_id: str) -> None:
    """Re-seed the durable waiting state (idempotent) for the next iteration."""
    path = root / ".aflow" / "runs" / run_id / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["execution_resource_wait"] = _wait_record()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _turn_count(root: Path, run_id: str) -> int:
    turns = root / ".aflow" / "runs" / run_id / "turns"
    return len(list(turns.iterdir())) if turns.is_dir() else 0


def _force_fixture_activity(monkeypatch: pytest.MonkeyPatch, run_id: str) -> None:
    """Confirm the fixture controller active so the real projection runs.

    The patch only injects ``unit_active`` evidence; every other step of
    ``project_activity`` (redaction, precedence, message formatting) is the
    production code path.
    """
    original = repository_module.project_activity

    def project_fixture_activity(status):
        if status.run_id == run_id:
            status = replace(status, evidence={**status.evidence, "unit_active": True})
        return original(status)

    monkeypatch.setattr(repository_module, "project_activity", project_fixture_activity)


def _global_run_row(page, run_id: str):
    # The row's accessible name ends with the run id.
    return page.locator(f"button.run-list-select[aria-label$=' · {run_id}']")


def _refresh(page) -> None:
    with page.expect_response(lambda response: response.request.method == "GET"
                              and f"/runs/{WAITING_RUN_ID}/context" in response.url) as loaded:
        page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
    assert loaded.value.ok
    assert isinstance(loaded.value.json(), dict)
    expect(page.locator(".run-dashboard")).not_to_contain_text("Loading run context")


def _scroll_geometry(page) -> dict:
    return page.evaluate("""() => {
        const result = document.querySelector('[data-ui-fidelity-anchor="latest-result"]');
        const rect = result?.getBoundingClientRect();
        return {scroll: window.scrollY, height: document.documentElement.scrollHeight,
                viewport: innerHeight, result_top: rect?.top ?? null,
                result_height: rect?.height ?? null,
                result_open: Boolean(result?.querySelector('details[open]'))};
    }""")


@pytest.fixture(scope="module")
def browser():
    """Reuse one browser per engine; contexts and server state stay isolated."""
    with sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def browser_server(control_client, browser):
    # Stop fixture HTTP requests before control_client clears its globals.
    with live_server() as url:
        yield url, browser


@pytest.fixture
def no_launch(control_client, monkeypatch):
    units = control_client[2]
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Browser status reads must not launch a model or unit")

    monkeypatch.setattr(units, "start", forbidden)
    return calls


VIEWPORTS = (
    pytest.param(320, 568, id="phone-320x568"),
    pytest.param(390, 844, id="phone-390x844"),
    pytest.param(768, 1024, id="tablet-portrait-768x1024"),
    pytest.param(844, 390, id="tablet-landscape-844x390"),
    pytest.param(1280, 720, id="desktop-short-1280x720"),
    pytest.param(1440, 900, id="desktop-1440x900"),
    pytest.param(390, 420, id="phone-short-390x420"),
)


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
def test_exclusive_resource_waiting_journey(control_client, monkeypatch, tmp_path, width, height, browser_server, no_launch):
    _, root, _, _ = control_client
    assert WEB_DIST.is_dir(), "build the cached web bundle first: npm run build in apps/aflow_app/web"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(WEB_DIST))
    run_dir = _seed_run(root, WAITING_RUN_ID, plan_name="exclusive-wait", status="running", wait=True, phase="unit_started")
    payload = json.loads((run_dir / "run.json").read_text())
    payload.update(current_step_name="review", turns_completed=1)
    (run_dir / "run.json").write_text(json.dumps(payload))
    append_run_event(run_dir, "turn_started", {"turn_number": 1, "step_name": "implement", "step_role": "worker", "resolved_selector": "pi.previous-worker", "resolved_model_display": "previous-worker-model"})
    append_run_event(run_dir, "turn_finished", {"turn_number": 1, "step_name": "implement", "status": "completed", "summary": "Completed fixture work", "output": "Completed fixture work\n" + "Preserved result details.\n" * 60})
    _force_fixture_activity(monkeypatch, WAITING_RUN_ID)
    shots = tmp_path / "shots"
    shots.mkdir()

    url, browser = browser_server
    context = browser.new_context(viewport={"width": width, "height": height})
    with context:
        try:
            page = context.new_page()
            _login(page, url)
            for theme in ("light", "dark"):
                _restore_wait(root, WAITING_RUN_ID)
                _set_theme_preference(page, theme)
                page.goto(f"{url}/?view=all-runs")
                page.get_by_role("heading", name="All runs", exact=True).wait_for()
                page.get_by_label("Search loaded runs", exact=True).fill(WAITING_RUN_ID)
                row = _global_run_row(page, WAITING_RUN_ID)
                row.wait_for()

                # Neutral waiting pill plus the exact bounded resource message,
                # including a long model/profile label, on the global row.
                expect(row.locator(".status-pill").first).to_have_text("Waiting for resource")
                expect(row.locator(".run-row-meta-fact")).to_have_text(WAIT_MESSAGE)
                _assert_no_horizontal_overflow(page)
                page.screenshot(path=str(shots / f"waiting-{width}x{height}-{theme}.png"))

                # The run detail names the resource as the current work.
                row.click()
                current_work = page.locator("[data-ui-fidelity-anchor='current-work'] .run-overview-content")
                expect(current_work).to_contain_text(WAIT_MESSAGE)
                # Current work is deliberately usable before the initial
                # history/configuration/context reads finish. Their loading
                # notices are not an unchanged background refresh: wait for
                # this fixture's admitted detail before measuring its scroll.
                page.get_by_role("button", name="More", exact=True).click()
                page_menu = page.get_by_role("menu", name="More run page actions", exact=True)
                expect(page_menu.get_by_role("menuitem", name="Refresh", exact=True)).to_be_enabled()
                page_menu.press("Escape")
                expect(page_menu).not_to_be_visible()
                expect(page.locator('.checkpoint-history[aria-label="Checkpoint history"]')
                       .get_by_role("button", name="Expand all", exact=True)).to_be_visible()
                expect(page.locator(".run-dashboard")).not_to_contain_text("Run history is still loading")
                expect(current_work).not_to_contain_text("controller_pid")
                expect(current_work).not_to_contain_text("pi.previous-worker")
                expect(current_work).not_to_contain_text("previous-worker-model")
                result = page.locator("[data-ui-fidelity-anchor='latest-result']")
                expect(result).to_contain_text("Completed")
                result.get_by_text("Read full update", exact=True).click()
                disclosure = result.locator("details")
                expect(disclosure).to_have_attribute("open", "")
                focus = disclosure.locator("summary")
                focus.focus()
                page.evaluate("window.scrollTo(0, Math.min(180, document.documentElement.scrollHeight - innerHeight))")
                node = result.element_handle()
                page.screenshot(path=str(shots / f"waiting-detail-{width}x{height}-{theme}.png"))

                # A refresh while waiting changes nothing: same message, same
                # scroll position, and no model launch or turn appears.
                before = _scroll_geometry(page)
                _refresh(page)
                expect(current_work).to_contain_text(WAIT_MESSAGE)
                after = _scroll_geometry(page)
                artifact_dir = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(shots)))
                artifact_dir.mkdir(parents=True, exist_ok=True)
                browser_name = os.environ.get("AFLOW_TEST_BROWSER", "chromium")
                prefix = f"exclusive-refresh-{browser_name}-{width}x{height}-{theme}"
                (artifact_dir / f"{prefix}.json").write_text(
                    json.dumps({"before": before, "after": after}, indent=2), encoding="utf-8"
                )
                shutil.copyfile(shots / f"waiting-detail-{width}x{height}-{theme}.png",
                                artifact_dir / f"{prefix}-before.png")
                if after["scroll"] != before["scroll"]:
                    try:
                        page.screenshot(path=str(artifact_dir / f"{prefix}-failure.png"))
                    except PlaywrightError as screenshot_error:
                        print("EXCLUSIVE_SCROLL_SCREENSHOT_ERROR", str(screenshot_error)[:300])
                assert after["scroll"] == before["scroll"], {"before": before, "after": after}
                assert node.evaluate("node => node.isConnected")
                assert focus.evaluate("node => node === document.activeElement")
                expect(disclosure).to_have_attribute("open", "")
                expect(result).to_contain_text("Completed")
                assert _turn_count(root, WAITING_RUN_ID) == 0
                assert no_launch == []
                page.screenshot(path=str(shots / f"waiting-refreshed-{width}x{height}-{theme}.png"))
                shutil.copyfile(shots / f"waiting-refreshed-{width}x{height}-{theme}.png",
                                artifact_dir / f"{prefix}-after.png")

                # Acquired: the controller picks the resource up, the exact
                # waiting message disappears, and normal active work returns.
                _clear_wait(root, WAITING_RUN_ID)
                _refresh(page)
                expect(current_work).not_to_contain_text(WAIT_MESSAGE)
                expect(current_work).to_contain_text("Review")
                expect(page.locator(".status-pill").first).to_have_text("Running")
                assert node.evaluate("node => node.isConnected")
                expect(disclosure).to_have_attribute("open", "")
                expect(result).to_contain_text("Completed")
                # Wheel outside the expanded raw payload's permitted local
                # scroller so this proves movement of the document itself.
                page.mouse.move(2, 2)
                _assert_document_moves(page)
                _assert_action_hit_test(page, page.get_by_role("button", name="Actions", exact=True))
                page.locator("html").evaluate("node => node.style.fontSize = '20px'")
                _assert_no_horizontal_overflow(page)
                page.locator("html").evaluate("node => node.style.fontSize = ''")
                if width < 960 or height < 600:
                    page.get_by_role("button", name="← Back to Run history", exact=True).click()
                    # Global run selection opens the owning project's detail;
                    # its Back action returns to that project's run history.
                    expect(page.get_by_role("navigation", name="Run history", exact=True)).to_be_visible()
                    expect(page.get_by_role("button", name=re.compile(rf"^{WAITING_RUN_ID} Running"))).to_be_visible()
                page.screenshot(path=str(shots / f"acquired-{width}x{height}-{theme}.png"))
        finally:
            context.close()


def test_exclusive_resource_stop_and_precedence(control_client, monkeypatch, tmp_path, browser_server):
    _, root, _, _ = control_client
    assert WEB_DIST.is_dir(), "build the cached web bundle first: npm run build in apps/aflow_app/web"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(WEB_DIST))
    _seed_run(root, WAITING_RUN_ID, plan_name="exclusive-wait", status="running", wait=True, phase="unit_started")
    _seed_run(root, TERMINAL_RUN_ID, plan_name="exclusive-terminal", status="completed", wait=True, phase="completed")
    _seed_run(root, UNKNOWN_RUN_ID, plan_name="exclusive-unknown", status="running", wait=True, phase=None, manifest=False)
    _force_fixture_activity(monkeypatch, WAITING_RUN_ID)
    shots = tmp_path / "shots"
    shots.mkdir()

    url, browser = browser_server
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    with context:
        try:
            page = context.new_page()
            _login(page, url)
            page.goto(f"{url}/?view=all-runs")
            page.get_by_role("heading", name="All runs", exact=True).wait_for()

            # Stale wait evidence on a terminal run never renders as waiting.
            page.get_by_label("Search loaded runs", exact=True).fill(TERMINAL_RUN_ID)
            terminal_row = _global_run_row(page, TERMINAL_RUN_ID)
            terminal_row.wait_for()
            expect(terminal_row.locator(".status-pill").first).to_have_text("Completed")
            expect(terminal_row).not_to_contain_text("Waiting for")
            page.screenshot(path=str(shots / "terminal-precedence.png"))

            # Unknown (legacy, unconfirmed) evidence takes precedence too.
            page.get_by_label("Search loaded runs", exact=True).fill(UNKNOWN_RUN_ID)
            unknown_row = _global_run_row(page, UNKNOWN_RUN_ID)
            unknown_row.wait_for()
            expect(unknown_row.locator(".status-pill").first).to_have_text("Needs attention")
            expect(unknown_row).not_to_contain_text("Waiting for")
            page.screenshot(path=str(shots / "unknown-precedence.png"))

            # Existing owner stop flow stops the waiting run; the waiting
            # message disappears with the stopped state.
            page.get_by_label("Search loaded runs", exact=True).fill(WAITING_RUN_ID)
            row = _global_run_row(page, WAITING_RUN_ID)
            row.wait_for()
            row.click()
            current_work = page.locator("[data-ui-fidelity-anchor='current-work'] .run-overview-content")
            expect(current_work).to_contain_text(WAIT_MESSAGE)
            page.get_by_role("button", name="Actions", exact=True).click()
            page.get_by_role("menuitem", name="Review stop options…", exact=True).click()
            page.get_by_role("button", name="Stop now…", exact=True).click()
            page.get_by_role("button", name="Stop now", exact=True).click()
            page.get_by_text("Stop now recorded", exact=False).first.wait_for()
            expect(current_work).not_to_contain_text(WAIT_MESSAGE)
            expect(page.locator(".status-pill").first).to_have_text("Stopped")
            page.screenshot(path=str(shots / "stopped-after-waiting.png"))
        finally:
            context.close()


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_exclusive_checkbox_save_and_reload(control_client, monkeypatch, tmp_path, width, height, theme, browser_server, no_launch):
    _, root, _, _ = control_client
    assert WEB_DIST.is_dir(), "build the cached web bundle first: npm run build in apps/aflow_app/web"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(WEB_DIST))
    long_profile = "very-long-profile-name"
    config_path = tmp_path / "global" / "aflow.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f"\n[harness.codex.profiles.{long_profile}]\nmodel = \"other-model\"\n",
        encoding="utf-8",
    )

    url, browser = browser_server
    context = browser.new_context(viewport={"width": width, "height": height})
    with context:
        try:
            page = context.new_page()
            _login(page, url)
            _set_theme_preference(page, theme)
            page.goto(f"{url}/?view=settings")
            # Compact settings uses a section select instead of desktop tabs.
            page.get_by_text("codex.test", exact=True).wait_for()

            test_row = page.locator(".profile-editor-row", has=page.get_by_text("codex.test", exact=True))
            long_row = page.locator(".profile-editor-row", has=page.get_by_text(f"codex.{long_profile}", exact=True))
            test_exclusive = test_row.get_by_label(f"Exclusive codex.test", exact=True)
            long_exclusive = long_row.get_by_label(f"Exclusive codex.{long_profile}", exact=True)
            expect(test_exclusive).not_to_be_checked()
            expect(long_exclusive).not_to_be_checked()
            # Rows can render before all initial settings reads finish. Native
            # keyboard input does not activate a disabled checkbox.
            expect(test_exclusive).to_be_enabled()
            test_exclusive.focus()
            test_exclusive.press("Space")
            expect(test_exclusive).to_be_checked()
            save_button = page.get_by_role("button", name="Save all changes")
            expect(save_button).to_be_enabled()
            with page.expect_response(lambda response: response.request.method == "PATCH"
                                      and "/config" in response.url) as saved:
                save_button.click()
            assert saved.value.ok
            assert isinstance(saved.value.json(), dict)

            # Reload: the exclusive flag persists on the edited profile row
            # and the untouched model row stays unchanged.
            page.reload()
            test_row = page.locator(".profile-editor-row", has=page.get_by_text("codex.test", exact=True))
            long_row = page.locator(".profile-editor-row", has=page.get_by_text(f"codex.{long_profile}", exact=True))
            expect(test_row.get_by_label("Exclusive codex.test", exact=True)).to_be_checked()
            expect(long_row.get_by_label(f"Exclusive codex.{long_profile}", exact=True)).not_to_be_checked()
            expect(long_row.get_by_label(f"Model codex.{long_profile}", exact=True)).to_have_value("other-model")
            _assert_no_horizontal_overflow(page)
            page.mouse.move(2, 2)
            _assert_document_moves(page)
            assert no_launch == []
            (tmp_path / "shots").mkdir(exist_ok=True)
            page.screenshot(path=str(tmp_path / "shots" / "exclusive-checkbox-reloaded.png"))
        finally:
            context.close()

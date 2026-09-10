"""Behavioral responsive journeys for the authenticated dashboard."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, sync_playwright

from aflow.control_plane.persistence import append_run_event
from test_control_plane_api import PROJECT_ID, TOKEN, control_client, live_server  # noqa: F401
from test_control_plane_api import _add_live_control_targets, _prepared


VIEWPORTS = (
    pytest.param(320, 568, id="phone-portrait"),
    pytest.param(390, 844, id="phone-tall"),
    pytest.param(768, 1024, id="tablet"),
    pytest.param(844, 390, id="phone-landscape"),
    pytest.param(1280, 720, id="desktop"),
    pytest.param(1440, 900, id="desktop-tall"),
    pytest.param(390, 420, id="phone-short"),
)

LONG_LABEL = "route-" + "x" * 120
LONG_TEXT = "# Responsive fixture\n\n" + ("A long document line for ordinary document flow. " * 420) + "\n"


def _seed_responsive_fixture(root: Path) -> None:
    plans = root / "plans" / "todo"
    plans.mkdir(parents=True, exist_ok=True)
    for index in range(40):
        (plans / f"long-plan-{index:02}.md").write_text(
            f"# Long plan {index:02}\n\n{LONG_TEXT}\nFixture path {LONG_LABEL}-{index:02}.\n"
        )
    ready = root / "plans" / "in-progress"
    ready.mkdir(parents=True, exist_ok=True)
    (ready / "ready-launch-plan.md").write_text(
        f"# Ready long plan 39\n\n{LONG_TEXT}\nFixture path {LONG_LABEL}-ready.\n"
    )

    runs = root / ".aflow" / "runs"
    for index in range(40):
        run_dir = runs / f"responsive-run-{index:02}"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({
            "status": "completed",
            "plan_path": f"plans/todo/long-plan-{index:02}.md",
            "workflow_name": "managed",
            "max_turns": 5,
        }))

    config_dir = root.parent / "global"
    config_path = config_dir / "aflow.toml"
    profiles = "\n".join(
        f'[harness.codex.profiles.profile_{index:02}]\nmodel = "model-{index:02}"\neffort = "high"'
        for index in range(40)
    )
    teams = "\n".join(
        f'[teams.team_{index:02}.roles]\nworker = "codex.profile_{index:02}"'
        for index in range(40)
    )
    config_path.write_text(config_path.read_text() + f"\n{profiles}\n{teams}\n")
    workflows_path = config_dir / "workflows.toml"
    workflows = "\n".join(
        f'[workflow.workflow_{index:02}.steps.implement]\nrole = "worker"\nprompts = ["p"]\ngo = [{{ to = "END", when = "DONE" }}]'
        for index in range(40)
    )
    workflows_path.write_text(workflows_path.read_text() + f"\n{workflows}\n")


def _browser(playwright):
    name = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    if name not in {"chromium", "webkit"}:
        raise pytest.UsageError("AFLOW_TEST_BROWSER must be chromium or webkit")
    browser_type = getattr(playwright, name)
    launch = {"headless": True}
    if name == "chromium":
        launch["args"] = ["--no-sandbox"]
    return browser_type.launch(**launch)


def _compact(page: Page) -> bool:
    width, height = page.viewport_size["width"], page.viewport_size["height"]
    return width < 960 or height < 600


def _login(page: Page, url: str) -> None:
    page.goto(url)
    page.get_by_placeholder("Auth token").fill(TOKEN)
    page.get_by_role("button", name="Login", exact=True).click()
    page.locator(".app-header").wait_for()


def _open_destination(page: Page, label: str) -> None:
    if _compact(page):
        page.get_by_role("button", name="Menu", exact=True).click()
        page.get_by_role("menuitem", name=label, exact=True).click()
    else:
        page.get_by_role("button", name=label, exact=True).click()


def _select_settings_section(page: Page, name: str) -> None:
    selector = page.get_by_role("combobox", name="Settings section", exact=True)
    if selector.count():
        selector.select_option(label=name)
    else:
        page.get_by_role("tab", name=name, exact=True).click()


def _ensure_project(page: Page) -> None:
    _open_destination(page, "Projects")
    project = page.get_by_role("button", name="Test project", exact=False).first
    project.wait_for()
    project.click()
    page.wait_for_function(
        "new URL(location.href).searchParams.get('project') === 'test-project'"
    )
    page.get_by_role("button", name="New run", exact=True).wait_for()


def _assert_no_horizontal_overflow(page: Page) -> None:
    metrics = page.evaluate("""() => ({
        width: innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
        documentHeight: document.scrollingElement?.scrollHeight ?? 0,
        viewportHeight: innerHeight,
    })""")
    assert metrics["scrollWidth"] <= metrics["width"] + 1, metrics
    assert metrics["documentHeight"] >= metrics["viewportHeight"], metrics


def _assert_no_unauthorized_scrollers(page: Page) -> None:
    scrollers = page.evaluate("""() => {
        const root = document.scrollingElement;
        return [...document.querySelectorAll('*')]
          .filter(element => {
            if (element === root) return false;
            const style = getComputedStyle(element);
            return /(auto|scroll)/.test(style.overflowY)
              && element.scrollHeight > element.clientHeight + 1;
          })
          .map(element => ({
            tag: element.tagName,
            className: String(element.className || ''),
            role: element.getAttribute('role'),
          }));
    }""")
    unexpected = [item for item in scrollers if not (
        item["tag"] in {"TEXTAREA", "SELECT"}
        or "sidebar-editor-navigation" in item["className"]
        or "combobox-options" in item["className"]
        or item["role"] == "menu"
    )]
    assert not unexpected, unexpected


def _assert_header_and_flow(page: Page) -> None:
    width, height = page.viewport_size["width"], page.viewport_size["height"]
    _assert_no_horizontal_overflow(page)
    _assert_no_unauthorized_scrollers(page)
    main = page.locator(".workspace-main").bounding_box()
    assert main and main["height"] > 0, main

    if width >= 960 and height >= 600:
        row_two = page.locator(".app-header-row-two").bounding_box()
        content = page.locator(".workspace-main").bounding_box()
        assert row_two and row_two["y"] + row_two["height"] <= 112, row_two
        assert content and content["y"] <= 128, content
    else:
        page.get_by_role("button", name="Menu", exact=True).wait_for()
        assert page.evaluate("""() => [...document.styleSheets].some(sheet => {
            try {
                return [...sheet.cssRules].some(rule => rule.cssText.includes('safe-area-inset-top'))
            } catch {
                return false
            }
        })""")
        sticky = page.evaluate("""() => [
            ...document.querySelectorAll('.app-header-row-two, .settings-toolbar')
        ].filter(element => getComputedStyle(element).position === 'sticky').length""")
        assert sticky <= 1, sticky
        if height < 600:
            assert page.evaluate("""() => [
                ...document.querySelectorAll('.app-header-row-two, .settings-toolbar')
            ].every(element => getComputedStyle(element).position !== 'sticky')""")

    controls = page.locator(
        "button.btn:visible, select:visible, input:not([type='checkbox']):not([type='radio']):visible, textarea:visible"
    ).evaluate_all("""elements => elements.map(element => ({
        tag: element.tagName,
        height: element.getBoundingClientRect().height,
        label: element.getAttribute('aria-label') || element.textContent?.trim().slice(0, 40),
    }))""")
    if _compact(page):
        undersized = [control for control in controls if control["height"] < 43]
        assert not undersized, undersized


def _assert_last_action_hit_test(page: Page) -> None:
    last = page.locator("button:visible").last
    last.scroll_into_view_if_needed()
    box = last.bounding_box()
    assert box and 0 <= box["y"] < page.viewport_size["height"]
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"]
    hit = page.evaluate("""({x, y}) => {
        const target = document.elementFromPoint(x, y);
        return Boolean(target && (target.closest('button') || target.closest('[role="button"]')));
    }""", {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2})
    assert hit, box


def _assert_document_moves(page: Page) -> None:
    page.evaluate("window.scrollTo(0, 0)")
    before = page.evaluate("() => document.scrollingElement.scrollTop")
    page.mouse.wheel(0, max(240, page.viewport_size["height"] // 2))
    page.wait_for_timeout(50)
    after = page.evaluate("() => document.scrollingElement.scrollTop")
    height = page.evaluate("() => document.scrollingElement.scrollHeight")
    assert after > before or height <= page.viewport_size["height"] + 1, {
        "before": before,
        "after": after,
        "height": height,
    }


def _assert_menu_keyboard_contract(page: Page) -> None:
    if not _compact(page):
        return
    menu = page.get_by_role("button", name="Menu", exact=True)
    menu.focus()
    menu.press("Enter")
    nav = page.get_by_role("menu", name="Workspace navigation")
    nav.wait_for()
    for label in ("All runs", "Projects", "Settings", "Plans", "Runs", "Logout"):
        assert nav.get_by_role("menuitem", name=label, exact=True).is_visible()
    page.keyboard.press("Escape")
    assert menu.evaluate("element => document.activeElement === element")


def _set_theme_preference(page: Page, theme: str) -> None:
    page.evaluate("theme => localStorage.setItem('aflow.appearance', theme)", theme)


def _assert_theme(page: Page, theme: str) -> None:
    assert page.locator("html").get_attribute("data-theme") == theme


def _double_visible_text(page: Page) -> None:
    page.evaluate("""() => {
        const textBearing = /^(BUTTON|INPUT|TEXTAREA|SELECT|OPTION|LABEL|A|P|LI|H[1-6]|SPAN|LEGEND|SUMMARY|DT|DD|TH|TD|OUTPUT)$/;
        const elements = [...document.querySelectorAll('body, body *')].filter(element => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            const directText = [...element.childNodes].some(node => (
                node.nodeType === Node.TEXT_NODE && Boolean(node.textContent?.trim())
            ));
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && box.width > 0
                && box.height > 0
                && (textBearing.test(element.tagName) || directText);
        });
        const snapshot = elements.map(element => ({
            element,
            size: Number.parseFloat(getComputedStyle(element).fontSize),
        })).filter(({size}) => Number.isFinite(size) && size > 0);
        snapshot.forEach(({element, size}) => {
            element.dataset.responsiveTestFontZoom = '1';
            element.style.setProperty('font-size', `${size * 2}px`, 'important');
        });
    }""")


def _clear_test_text_zoom(page: Page) -> None:
    page.evaluate("""() => {
        document.querySelectorAll('[data-responsive-test-font-zoom]').forEach(element => {
            element.style.removeProperty('font-size');
            delete element.dataset.responsiveTestFontZoom;
        });
    }""")


def _wait_for_loaded_plan_rows(page: Page) -> None:
    """Wait for the fixture's plan list, not only the independent filename form."""
    page.wait_for_function("""() => {
        const rows = [...document.querySelectorAll('.plan-list .content-button')]
        return rows.length >= 40
            && rows.some(row => row.textContent?.includes('long-plan-39.md'))
    }""")


def _visible_dashboard(page: Page):
    dashboard = page.locator(".dashboard-host:not([hidden])").first
    dashboard.wait_for()
    return dashboard


def _open_live_controls(page: Page):
    dashboard = _visible_dashboard(page)
    details = dashboard.locator("details.dashboard-section").filter(has_text="Adjust run").first
    details.wait_for()
    max_turns = dashboard.get_by_label("Control max turns", exact=True)
    if details.get_attribute("open") is None:
        details.locator("summary").click()
    max_turns.wait_for(state="visible")
    return dashboard


def _choose_combobox(dashboard, label: str, query: str, option_text: str) -> None:
    field = dashboard.get_by_label(label, exact=True)
    field.click()
    field.fill(query)
    option = dashboard.get_by_role("option").filter(has_text=option_text).first
    option.wait_for()
    option.click()


def _assert_action_hit_test(page: Page, action) -> None:
    action.scroll_into_view_if_needed()
    box = action.bounding_box()
    assert box and 0 <= box["y"] < page.viewport_size["height"], box
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"], box
    hit = page.evaluate("""({x, y}) => {
        const target = document.elementFromPoint(x, y)
        return Boolean(target && (target.closest('button') || target.closest('[role="button"]')))
    }""", {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2})
    assert hit, box


def _create_live_control_fixture(control_client, root: Path, monkeypatch) -> tuple[str, dict[str, object]]:
    """Create one real control-plane run while keeping later browser writes intercepted."""
    client, _, units, _ = control_client
    _add_live_control_targets(root.parent / "global" / "aflow.toml")
    with (root.parent / "global" / "aflow.toml").open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[teams.fast_team.roles]\n"
            'worker = "reasonix.new"\n'
            "\n[teams.fast__team.roles]\n"
            'worker = "reasonix.new"\n'
        )
    monkeypatch.setattr("aflow.daemon.prepare_startup", _prepared)
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "responsive-live-start"},
        json={
            "plan_path": "plans/in-progress/ready-launch-plan.md",
            "workflow_name": "managed",
            "team": "team_00",
            "max_turns": 8,
            "dirty_worktree_confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    result = response.json()["result"]
    run_id = result["run_id"]
    assert len(units.start_calls) == 1
    # The disposable unit manager proves the launch without running a controller.
    # Advertise the admitted controller state in the same durable run metadata
    # used by the repository, so the browser exercises live controls rather than
    # a synthetic launch-pending response.
    (root / ".aflow" / "runs" / run_id / "run.json").write_text(
        json.dumps({
            "status": "running",
            "active_plan_path": "plans/in-progress/ready-launch-plan.md",
            "workflow_name": "managed",
            "team": "team_00",
            "current_step_name": "implement",
            "turns_completed": 2,
            "max_turns": 8,
            "run_started_at": "2026-09-10T00:00:00Z",
        })
    )
    status = client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs/{run_id}")
    assert status.status_code == 200, status.text
    state = status.json()
    assert state["ownership"] == "control_plane"
    assert state["status"] == "running"
    assert state["revision"] == 0
    return run_id, state


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
def test_responsive_route_matrix(control_client, monkeypatch, width: int, height: int):
    """Exercise every shell destination at each required CSS viewport."""
    _, root, _, _ = control_client
    _seed_responsive_fixture(root)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            _assert_menu_keyboard_contract(page)
            _ensure_project(page)

            _open_destination(page, "Settings")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            for section in ("General", "Teams", "Workflows", "Prompts", "Agents & Roles"):
                _select_settings_section(page, section)
                page.locator("#settings-domain-panel").wait_for()
            _select_settings_section(page, "Agents & Roles")
            page.get_by_label("Effort codex.profile_39", exact=True).wait_for()
            _assert_header_and_flow(page)
            _assert_document_moves(page)
            _assert_last_action_hit_test(page)

            _select_settings_section(page, "Skills")
            skills_nav = page.locator(".sidebar-editor-navigation")
            skills_nav.get_by_role("button", name="aflow-plan", exact=True).click()
            skill_editor = page.get_by_label("SKILL.md for aflow-plan", exact=True)
            skill_editor.wait_for()
            editor_box = skill_editor.bounding_box()
            assert editor_box and editor_box["height"] >= 280
            _assert_header_and_flow(page)

            _open_destination(page, "Plans")
            page.get_by_label("New plan filename", exact=True).wait_for()
            _wait_for_loaded_plan_rows(page)
            assert page.locator(".plan-list .content-button").count() >= 40
            _assert_header_and_flow(page)
            _assert_document_moves(page)
            plan_row = page.get_by_role("button", name="long-plan-39.md", exact=False).first
            plan_row.scroll_into_view_if_needed()
            plan_row.click()
            plan_editor = page.get_by_label("Plan content", exact=True)
            plan_editor.wait_for()
            assert plan_editor.input_value().startswith("# Long plan 39")
            assert plan_editor.bounding_box()["height"] > 0
            _assert_header_and_flow(page)
            _assert_last_action_hit_test(page)
            if _compact(page):
                page.get_by_role("button", name="← Back to Plans", exact=True).click()
                page.get_by_label("New plan filename", exact=True).wait_for()

            _open_destination(page, "Runs")
            page.get_by_role("button", name="New run", exact=True).wait_for()
            page.locator(".run-list-item").first.wait_for()
            assert page.locator(".run-list-item").count() >= 40
            _assert_header_and_flow(page)
            _assert_document_moves(page)
            run_row = page.locator(".run-list-item").filter(has_text="long-plan-39.md").first
            run_row.scroll_into_view_if_needed()
            run_row.click()
            page.locator(".run-detail h3").filter(has_text="long-plan-39.md").wait_for()
            detail_box = page.locator(".sidebar-editor-detail").bounding_box()
            assert detail_box and detail_box["height"] > 0
            _assert_header_and_flow(page)
            _assert_last_action_hit_test(page)
            if _compact(page):
                page.get_by_role("button", name="← Back to Run history", exact=True).click()
                page.locator(".run-list-item").first.wait_for()

            _open_destination(page, "All runs")
            page.get_by_role("heading", name="All runs", exact=True).wait_for()
            all_run = page.get_by_role("button", name="Test project · Completed", exact=False).first
            all_run.wait_for()
            all_run.click()
            page.locator(".run-detail h3").filter(has_text="long-plan").wait_for()
            _assert_header_and_flow(page)
            if _compact(page):
                page.get_by_role("button", name="← Back to Run history", exact=True).click()
                page.locator(".run-list-item").first.wait_for()

            _open_destination(page, "Projects")
            page.get_by_role("button", name="Add project", exact=True).wait_for()
            page.get_by_role("button", name="Add project", exact=True).click()
            project_path = page.get_by_label("Relative project path", exact=True)
            project_path.wait_for()
            project_path.fill("../invalid-responsive-project")
            page.get_by_role("button", name="Create project", exact=True).click()
            page.get_by_role("alert").wait_for()
            project_path.fill("disposable/" + "project-" * 30)
            create = page.get_by_role("button", name="Create project", exact=True)
            create.scroll_into_view_if_needed()
            create_box = create.bounding_box()
            assert create_box and 0 <= create_box["y"] < height
            _assert_header_and_flow(page)
            page.get_by_role("button", name="Cancel", exact=True).click()

            _ensure_project(page)
            page.get_by_role("button", name="New run", exact=True).click()
            page.get_by_label("Run plan", exact=True).wait_for()
            plan_input = page.get_by_label("Run plan", exact=True)
            plan_input.click()
            page.get_by_role("option", name="ready-launch-plan.md", exact=False).click()
            workflow = page.get_by_label("Run workflow", exact=True)
            workflow.click()
            workflow.fill("managed")
            workflow.press("ArrowDown")
            workflow.press("Enter")
            page.get_by_role("button", name="Advanced options", exact=True).click()
            extra = page.get_by_label("Run extra instructions", exact=True)
            extra.fill("Long launch instruction. " * 100)
            assert page.get_by_role("button", name="Start run", exact=True).is_visible()
            _assert_header_and_flow(page)
            _assert_last_action_hit_test(page)
        finally:
            browser.close()


def test_responsive_focus_resize_and_screenshots(control_client, monkeypatch, tmp_path):
    """Cover keyboard/reflow behavior and persist representative visual evidence."""
    _, root, _, _ = control_client
    _seed_responsive_fixture(root)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            _login(page, url)
            _ensure_project(page)
            _assert_menu_keyboard_contract(page)

            _open_destination(page, "Settings")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            _select_settings_section(page, "Agents & Roles")
            field = page.get_by_label("Effort codex.profile_39", exact=True)
            field.wait_for()
            field.focus()
            field.press("Tab")
            page.keyboard.press("Shift+Tab")
            assert field.evaluate("element => document.activeElement === element")
            page.set_viewport_size({"width": 390, "height": 420})
            assert field.evaluate("element => document.activeElement === element")
            assert not page.locator(".app-header-row-two").evaluate(
                "element => getComputedStyle(element).position === 'sticky'"
            )
            _assert_header_and_flow(page)

            page.set_viewport_size({"width": 390, "height": 844})
            _select_settings_section(page, "Skills")
            page.locator(".sidebar-editor-navigation").get_by_role("button", name="aflow-plan", exact=True).click()
            area = page.get_by_label("SKILL.md for aflow-plan", exact=True)
            area.wait_for()
            back = page.get_by_role("button", name="← Back to Skills", exact=True)
            back.wait_for()
            baseline_sizes = page.evaluate("""() => {
                const size = element => Number.parseFloat(getComputedStyle(element).fontSize);
                return {
                    editor: size(document.querySelector('textarea[aria-label="SKILL.md for aflow-plan"]')),
                    control: size(document.querySelector('button.sidebar-editor-back')),
                };
            }""")
            assert baseline_sizes["editor"] > 0
            assert baseline_sizes["control"] > 0
            _double_visible_text(page)
            enlarged_sizes = page.evaluate("""() => {
                const size = element => Number.parseFloat(getComputedStyle(element).fontSize);
                return {
                    editor: size(document.querySelector('textarea[aria-label="SKILL.md for aflow-plan"]')),
                    control: size(document.querySelector('button.sidebar-editor-back')),
                };
            }""")
            assert enlarged_sizes["editor"] == pytest.approx(baseline_sizes["editor"] * 2, abs=0.1)
            assert enlarged_sizes["control"] == pytest.approx(baseline_sizes["control"] * 2, abs=0.1)
            _assert_no_horizontal_overflow(page)
            area_box = area.bounding_box()
            assert area_box and area_box["height"] >= 280
            _assert_last_action_hit_test(page)
            page.set_viewport_size({"width": 320, "height": 568})
            _assert_no_horizontal_overflow(page)
            _assert_header_and_flow(page)
            resized_area_box = area.bounding_box()
            assert resized_area_box and resized_area_box["height"] > 0
            _assert_last_action_hit_test(page)
            _clear_test_text_zoom(page)

            page.set_viewport_size({"width": 390, "height": 844})
            _open_destination(page, "Plans")
            page.get_by_label("New plan filename", exact=True).wait_for()
            _wait_for_loaded_plan_rows(page)
            page.get_by_role("button", name="long-plan-39.md", exact=False).first.click()
            editor = page.get_by_label("Plan content", exact=True)
            editor.wait_for()
            draft = "# Keep this draft after resize\n\n" + LONG_TEXT
            failed_save = {"enabled": True}

            def fail_plan_save(route):
                if route.request.method == "PUT" and failed_save["enabled"]:
                    route.fulfill(
                        status=500,
                        content_type="application/json",
                        body=json.dumps({"detail": "responsive save failure"}),
                    )
                else:
                    route.continue_()

            plan_api = f"**/api/projects/{PROJECT_ID}/plans/todo/long-plan-39.md"
            page.route(plan_api, fail_plan_save)
            editor.fill(draft)
            page.get_by_role("button", name="Save", exact=True).click()
            page.get_by_role("alert").last.wait_for()
            assert editor.input_value() == draft
            failed_save["enabled"] = False
            page.unroute(plan_api, fail_plan_save)
            editor.focus()
            page.set_viewport_size({"width": 390, "height": 420})
            assert editor.input_value() == draft
            assert editor.evaluate("element => document.activeElement === element")
            page.get_by_role("button", name="← Back to Plans", exact=True).click()
            page.get_by_role("button", name="Discard edits", exact=True).click()

            page.set_viewport_size({"width": 390, "height": 844})
            screenshot_targets = (
                ("skills-list", "settings", "Skills", False),
                ("skills-detail", "settings", "Skills", True),
                ("profile-editing", "settings", "Agents & Roles", False),
                ("run-history", "runs", None, False),
                ("run-detail", "runs", None, True),
            )
            for theme in ("light", "dark"):
                for name, view, section, detail in screenshot_targets:
                    _set_theme_preference(page, theme)
                    page.goto(f"{url}/?project={PROJECT_ID}&view={view}")
                    _assert_theme(page, theme)
                    if view == "settings":
                        page.get_by_role("heading", name="Settings", exact=True).wait_for()
                        _select_settings_section(page, section)
                        if detail:
                            page.locator(".sidebar-editor-navigation").get_by_role("button", name="aflow-plan", exact=True).click()
                            page.get_by_label("SKILL.md for aflow-plan", exact=True).wait_for()
                        else:
                            if section == "Skills":
                                page.locator(".sidebar-editor-navigation").get_by_role("button", name="aflow-plan", exact=True).wait_for()
                            else:
                                profile_field = page.get_by_label("Effort codex.profile_39", exact=True)
                                profile_field.wait_for()
                                if name == "profile-editing":
                                    profile_field.fill("responsive-profile-model")
                    else:
                        page.get_by_role("button", name="New run", exact=True).wait_for()
                        row = page.locator(".run-list-item").filter(has_text="long-plan-39.md").first
                        row.wait_for()
                        if detail:
                            row.click()
                            page.locator(".run-detail h3").filter(has_text="long-plan-39.md").wait_for()
                    image = tmp_path / f"responsive-{theme}-{name}.png"
                    page.screenshot(path=str(image), full_page=True)
                    print("RESPONSIVE_SCREENSHOT", image)

                page.set_viewport_size({"width": 844, "height": 390})
                _set_theme_preference(page, theme)
                page.goto(f"{url}/?project={PROJECT_ID}&view=new-run")
                _assert_theme(page, theme)
                page.get_by_label("Run plan", exact=True).wait_for()
                page.get_by_label("Run plan", exact=True).click()
                page.get_by_role("option", name="ready-launch-plan.md", exact=False).click()
                workflow = page.get_by_label("Run workflow", exact=True)
                workflow.click()
                workflow.fill("managed")
                workflow.press("ArrowDown")
                workflow.press("Enter")
                visible_dashboard = page.locator('.dashboard-host:not([hidden])').first
                visible_dashboard.locator(".run-preview-list").wait_for()
                preflight = visible_dashboard.locator('section[aria-label="Working tree preflight"]')
                preflight.wait_for(state="visible")
                preflight.get_by_role("button", name="Refresh worktree inspection", exact=True).wait_for()
                loaded_clean = preflight.get_by_text("No uncommitted changes detected.", exact=True).is_visible()
                loaded_dirty = preflight.get_by_role(
                    "checkbox", name="Continue despite uncommitted changes", exact=True
                ).is_visible()
                assert loaded_clean or loaded_dirty, preflight.inner_text()
                image = tmp_path / f"responsive-{theme}-new-run-landscape.png"
                page.screenshot(path=str(image), full_page=True)
                print("RESPONSIVE_SCREENSHOT", image)

                _set_theme_preference(page, theme)
                page.goto(f"{url}/?project={PROJECT_ID}&view=plans")
                _assert_theme(page, theme)
                page.get_by_label("New plan filename", exact=True).wait_for()
                _wait_for_loaded_plan_rows(page)
                page.get_by_role("button", name="long-plan-39.md", exact=False).first.click()
                page.get_by_label("Plan content", exact=True).wait_for()
                image = tmp_path / f"responsive-{theme}-plans-landscape.png"
                page.screenshot(path=str(image), full_page=True)
                print("RESPONSIVE_SCREENSHOT", image)
                page.set_viewport_size({"width": 390, "height": 844})
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("width", "height"),
    (
        pytest.param(1280, 720, id="desktop-live-controls"),
        pytest.param(390, 844, id="phone-live-controls"),
        pytest.param(844, 390, id="landscape-live-controls"),
    ),
)
def test_responsive_live_controls_and_restart(
    control_client,
    monkeypatch,
    tmp_path,
    width: int,
    height: int,
):
    """Exercise live control and successor recovery through the hosted shell."""
    _, root, _, _ = control_client
    _seed_responsive_fixture(root)
    run_id, initial_state = _create_live_control_fixture(control_client, root, monkeypatch)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            _ensure_project(page)

            live_state = dict(initial_state)
            successor_state: dict[str, object] | None = None
            control_requests: list[dict[str, object]] = []
            owner_stop_requests: list[dict[str, object]] = []
            successor_requests: list[dict[str, object]] = []
            successor_id = "responsive-successor"
            base_path = f"/api/control-plane/projects/{PROJECT_ID}/runs"
            run_path = f"{base_path}/{run_id}"

            def reply(route, payload: object, status: int = 200) -> None:
                route.fulfill(
                    status=status,
                    content_type="application/json",
                    body=json.dumps(payload),
                )

            def runs_payload() -> dict[str, object]:
                runs = [live_state]
                if successor_state is not None:
                    runs.append(successor_state)
                return {"runs": runs, "next_cursor": None, "schema_version": 1}

            def route_control_plane(route) -> None:
                nonlocal successor_state
                request = route.request
                path = urlsplit(request.url).path
                method = request.method

                if method == "GET" and path == base_path:
                    reply(route, runs_payload())
                    return
                if method == "GET" and path in {run_path, f"{base_path}/{successor_id}"}:
                    reply(route, successor_state if path.endswith(successor_id) and successor_state else live_state)
                    return
                if method == "GET" and path == f"{run_path}/restart-options":
                    reply(route, {
                        "eligible": True,
                        "reason": None,
                        "requires_stop": live_state["status"] == "running",
                        "run_id": run_id,
                        "extra_instructions_unavailable": False,
                        "options": {
                            "plan_path": "plans/in-progress/ready-launch-plan.md",
                            "workflow_name": "managed",
                            "team": "fast__team",
                            "max_turns": 8,
                        },
                    })
                    return
                if method == "POST" and path == f"{base_path}/preflight":
                    reply(route, {
                        "checkout_path": str(root),
                        "execution_mode": "same_checkout",
                        "dirty": True,
                        "requires_confirmation": True,
                        "blockers": [],
                        "total_items": 1,
                        "offset": 0,
                        "limit": 200,
                        "next_offset": None,
                        "items": [{
                            "path": "plans/in-progress/ready-launch-plan.md",
                            "index_status": "?",
                            "worktree_status": "?",
                            "original_path": None,
                        }],
                    })
                    return
                if method == "PATCH" and path == f"{run_path}/control":
                    payload = request.post_data_json
                    control_requests.append({
                        "payload": payload,
                        "key": request.headers.get("idempotency-key"),
                    })
                    if len(control_requests) == 1:
                        live_state.update({"revision": 1, "max_turns": 12, "team": "fast__team"})
                        run_dir = root / ".aflow" / "runs" / run_id
                        (run_dir / "overrides.toml").write_text(
                            'revision = 1\nmax_turns = 12\nteam = "fast__team"\n\n'
                            '[roles]\nworker = "reasonix.new"\n'
                        )
                        append_run_event(run_dir, "control_changed", {
                            "revision": 1,
                            "max_turns": 12,
                            "team": "fast__team",
                            "owner_stop": False,
                            "roles": {"worker": "reasonix.new"},
                        })
                        reply(route, {
                            "revision": 1,
                            "changed": True,
                            "owner_stop": False,
                            "run": live_state,
                        })
                    else:
                        reply(route, {
                            "detail": {
                                "code": "validation_error",
                                "message": "responsive fixture rejected this control draft",
                            },
                        }, status=422)
                    return
                if method == "POST" and path == f"{run_path}/owner-stop":
                    owner_stop_requests.append({
                        "payload": request.post_data_json,
                        "key": request.headers.get("idempotency-key"),
                    })
                    live_state.update({
                        "status": "owner_stopped",
                        "activity": "inactive",
                        "status_reason_code": "owner_stopped",
                        "launch_phase": "owner_stopped",
                    })
                    reply(route, live_state)
                    return
                if method == "POST" and path == base_path:
                    successor_requests.append({
                        "payload": request.post_data_json,
                        "key": request.headers.get("idempotency-key"),
                    })
                    if len(successor_requests) == 1:
                        route.abort(error_code="failed")
                    else:
                        successor_state = dict(live_state)
                        successor_state.update({
                            "run_id": successor_id,
                            "status": "running",
                            "activity": "active",
                            "status_reason_code": "active",
                            "revision": 0,
                            "launch_phase": "running",
                            "team": "fast__team",
                            "max_turns": 10,
                            "restarted_from_run_id": run_id,
                        })
                        reply(route, {
                            "result": {
                                "run_id": successor_id,
                                "created": True,
                                "status": "running",
                                "schema_version": 1,
                                "manifest_path": None,
                                "reason": None,
                                "restarted_from_run_id": run_id,
                            },
                            "startup_question": None,
                        }, status=201)
                    return
                route.continue_()

            page.route(f"**/api/control-plane/projects/{PROJECT_ID}/runs**", route_control_plane)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}")
            dashboard = _open_live_controls(page)
            dashboard.locator(".run-detail h3").wait_for()
            page.get_by_text("Adjust run", exact=True).wait_for()
            _assert_header_and_flow(page)

            control_max_turns = dashboard.get_by_label("Control max turns", exact=True)
            control_team = dashboard.get_by_label("Control team", exact=True)
            control_selector = dashboard.get_by_label("Selector for Worker", exact=True)
            assert not control_max_turns.is_disabled()
            assert not control_team.is_disabled()
            assert not control_selector.is_disabled()
            control_team.locator("option[value='']").wait_for(state="attached")
            control_team.locator("option[value='fast_team']").wait_for(state="attached")
            control_team.locator("option[value='fast__team']").wait_for(state="attached")
            assert control_team.locator("option[value='']").text_content() == "No team"
            assert control_team.locator("option[value='fast_team']").text_content() == "Fast team (fast_team)"
            assert control_team.locator("option[value='fast__team']").text_content() == "Fast team (fast__team)"
            control_max_turns.fill("12")
            control_team.select_option("fast__team")
            control_selector.select_option("reasonix.new")
            save_controls = dashboard.get_by_role("button", name="Save run settings", exact=True)
            _assert_action_hit_test(page, save_controls)
            save_controls.click()
            page.get_by_text("Safe controls recorded at revision 1", exact=False).wait_for()
            assert control_requests[0]["payload"] == {
                "expected_revision": 0,
                "max_turns": 12,
                "team": "fast__team",
                "role_selectors": {"worker": "reasonix.new"},
            }
            assert control_requests[0]["key"]

            control_max_turns.fill("13")
            save_controls.click()
            page.get_by_role("alert").filter(has_text="responsive fixture rejected this control draft").wait_for()
            assert control_max_turns.input_value() == "13"
            assert control_requests[1]["payload"] == {"expected_revision": 1, "max_turns": 13}

            if _compact(page):
                page.get_by_role("button", name="← Back to Run history", exact=True).click()
                dashboard = _visible_dashboard(page)
                dashboard.locator(".sidebar-editor-navigation").wait_for(state="visible")
                run_row = dashboard.locator(f"[data-sidebar-editor-item='{run_id}']")
                run_row.wait_for(state="visible")
                run_row.click()
                dashboard = _open_live_controls(page)
            else:
                _open_destination(page, "Settings")
                page.get_by_role("heading", name="Settings", exact=True).wait_for()
                _open_destination(page, "Runs")
                dashboard = _open_live_controls(page)
            assert dashboard.get_by_label("Control max turns", exact=True).input_value() == "13"
            assert dashboard.get_by_label("Control team", exact=True).input_value() == "fast__team"

            page.set_viewport_size({"width": 390, "height": 844})
            if width >= 960 and height >= 600:
                dashboard = _visible_dashboard(page)
                dashboard.locator(".sidebar-editor-navigation").wait_for(state="visible")
                run_row = dashboard.locator(f"[data-sidebar-editor-item='{run_id}']")
                run_row.wait_for(state="visible")
                run_row.click()
            dashboard = _open_live_controls(page)
            assert dashboard.get_by_label("Control max turns", exact=True).input_value() == "13"
            page.set_viewport_size({"width": width, "height": height})
            dashboard = _open_live_controls(page)
            assert dashboard.get_by_label("Control max turns", exact=True).input_value() == "13"
            _assert_action_hit_test(page, dashboard.get_by_role("button", name="Save run settings", exact=True))

            for theme in ("light", "dark"):
                _set_theme_preference(page, theme)
                page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}")
                _assert_theme(page, theme)
                dashboard = _open_live_controls(page)
                dashboard.get_by_label("Control max turns", exact=True).wait_for()
                image = tmp_path / f"responsive-{width}x{height}-{theme}-live-controls.png"
                page.screenshot(path=str(image), full_page=True)
                print("RESPONSIVE_LIVE_SCREENSHOT", image)

            _set_theme_preference(page, "light")
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}")
            dashboard = _visible_dashboard(page)
            dashboard.locator(".run-detail h3").wait_for()
            page.get_by_role("button", name="Restart with changes", exact=True).click()
            dashboard = _visible_dashboard(page)
            dashboard.get_by_label("Run plan", exact=True).wait_for()
            _choose_combobox(dashboard, "Run team", "fast__team", "Fast team (fast__team)")
            if not dashboard.get_by_label("Run max turns", exact=True).is_visible():
                dashboard.get_by_role("button", name="Advanced options", exact=True).click()
            dashboard.get_by_label("Run max turns", exact=True).fill("10")
            preflight = dashboard.locator('section[aria-label="Working tree preflight"]')
            preflight.get_by_role("button", name="Refresh worktree inspection", exact=True).wait_for()
            dirty_ack = preflight.get_by_role(
                "checkbox", name="Continue despite uncommitted changes", exact=True
            )
            dirty_ack.wait_for(state="visible")
            assert not dirty_ack.is_checked()
            dirty_ack.check()
            assert dirty_ack.is_checked()
            confirm = dashboard.get_by_role("button", name="Confirm stop and start successor", exact=True)
            confirm.wait_for(state="visible")
            page.wait_for_function(
                """() => {
                    const button = document.querySelector('.confirmation button')
                    return Boolean(button && !button.disabled && (
                        button.offsetWidth || button.offsetHeight || button.getClientRects().length
                    ))
                }""",
            )
            assert not confirm.is_disabled()
            confirm.click()
            page.get_by_role("button", name="Retry exact successor request", exact=True).wait_for()
            assert dashboard.get_by_label("Run workflow", exact=True).is_disabled()
            assert page.get_by_role("button", name="Start run", exact=True).is_disabled()
            image = tmp_path / f"responsive-{width}x{height}-light-restart-unknown.png"
            page.screenshot(path=str(image), full_page=True)
            print("RESPONSIVE_RESTART_SCREENSHOT", image)

            _open_destination(page, "Settings")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            page.locator("#settings-domain-panel").wait_for()
            _select_settings_section(page, "General")
            page.locator("#settings-domain-panel").wait_for()
            theme = page.locator("label").filter(has_text="Color theme").locator("select").first
            theme.wait_for()
            theme.select_option("dark")
            _assert_theme(page, "dark")
            page.get_by_role("button", name="Resolve pending successor", exact=True).click()
            page.get_by_role("button", name="Retry exact successor request", exact=True).wait_for()
            image = tmp_path / f"responsive-{width}x{height}-dark-restart-unknown.png"
            page.screenshot(path=str(image), full_page=True)
            print("RESPONSIVE_RESTART_SCREENSHOT", image)

            retry = page.get_by_role("button", name="Retry exact successor request", exact=True)
            retry.click()
            page.get_by_text("Successor retry created run responsive-successor", exact=False).wait_for()
            assert len(owner_stop_requests) == 1
            assert owner_stop_requests[0]["payload"] == {"expected_revision": 1}
            assert owner_stop_requests[0]["key"]
            assert control_requests[0]["payload"] == {
                "expected_revision": 0,
                "max_turns": 12,
                "team": "fast__team",
                "role_selectors": {"worker": "reasonix.new"},
            }
            assert len(successor_requests) == 2
            assert successor_requests[0]["payload"] == {
                "plan_path": "plans/in-progress/ready-launch-plan.md",
                "workflow_name": "managed",
                "team": "fast__team",
                "start_step": "implement",
                "max_turns": 10,
                "restarted_from_run_id": run_id,
                "dirty_worktree_confirmed": True,
            }
            assert successor_requests[1] == successor_requests[0]
        finally:
            browser.close()

"""Behavioral responsive journeys for the authenticated dashboard."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from test_control_plane_api import PROJECT_ID, TOKEN, control_client, live_server  # noqa: F401


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
                page.locator(".run-preview-list").wait_for()
                image = tmp_path / f"responsive-{theme}-new-run-landscape.png"
                page.screenshot(path=str(image), full_page=True)
                print("RESPONSIVE_SCREENSHOT", image)

                _set_theme_preference(page, theme)
                page.goto(f"{url}/?project={PROJECT_ID}&view=plans")
                _assert_theme(page, theme)
                page.get_by_label("New plan filename", exact=True).wait_for()
                page.get_by_role("button", name="long-plan-39.md", exact=False).first.click()
                page.get_by_label("Plan content", exact=True).wait_for()
                image = tmp_path / f"responsive-{theme}-plans-landscape.png"
                page.screenshot(path=str(image), full_page=True)
                print("RESPONSIVE_SCREENSHOT", image)
                page.set_viewport_size({"width": 390, "height": 844})
        finally:
            browser.close()

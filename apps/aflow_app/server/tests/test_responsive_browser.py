"""Behavioral responsive journeys for the authenticated dashboard."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from aflow.control_plane.persistence import append_run_event
from test_control_plane_api import PROJECT_ID, TOKEN, control_client, live_server  # noqa: F401
from test_control_plane_api import (
    _add_live_control_targets,
    _prepared,
    _seed_recovery_source,
    _source_artifact_bytes,
)


VIEWPORTS = (
    pytest.param(320, 568, id="phone-portrait"),
    pytest.param(390, 844, id="phone-tall"),
    pytest.param(768, 1024, id="tablet"),
    pytest.param(844, 390, id="phone-landscape"),
    pytest.param(1280, 720, id="desktop"),
    pytest.param(1440, 900, id="desktop-tall"),
    pytest.param(390, 420, id="phone-short"),
)

RESPONSIVE_FIXTURE_RUN_ID = "responsive-run-39"
RESPONSIVE_FIXTURE_PLAN_PATH = "plans/todo/long-plan-39.md"
RESPONSIVE_FIXTURE_TITLE = "Long plan 39"
RESPONSIVE_GLOBAL_RUN_ID = "responsive-run-00"
RESPONSIVE_GLOBAL_TITLE = "Long plan 00"
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


def _register_responsive_worktree(root: Path) -> str:
    """Create one registered linked checkout for the presentation journey."""
    from aflow_app_server import main

    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=responsive-test",
            "-c",
            "user.email=responsive-test@example.invalid",
            "add",
            "-A",
        ),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=responsive-test",
            "-c",
            "user.email=responsive-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "responsive fixture base",
        ),
        check=True,
    )
    child = root.parent / "responsive-worktree"
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "worktree",
            "add",
            "-q",
            str(child),
            "-b",
            "feature/responsive-worktree",
        ),
        check=True,
    )
    for source in (root / ".aflow" / "runs").iterdir():
        target = child / ".aflow" / "runs" / source.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "run.json").write_text((source / "run.json").read_text())
    registry = main._project_registry
    assert registry is not None
    registry.register("responsive-worktree", "Feature worktree", "responsive-worktree")
    return "responsive-worktree"


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
        overflowing: [...document.querySelectorAll('*')]
          .map(element => ({ tag: element.tagName, className: String(element.className || ''), right: element.getBoundingClientRect().right }))
          .filter(element => element.right > innerWidth + 1)
          .slice(0, 8),
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
    _assert_action_hit_test(page, page.locator("button:visible").last)


def _assert_document_moves(page: Page) -> None:
    page.evaluate("window.scrollTo(0, 0)")
    before = page.evaluate("() => document.scrollingElement.scrollTop")
    page.mouse.wheel(0, max(240, page.viewport_size["height"] // 2))
    page.wait_for_function(
        """({before, viewportHeight}) => {
            const scrolling = document.scrollingElement
            return Boolean(scrolling && (
                scrolling.scrollTop > before
                || scrolling.scrollHeight <= viewportHeight + 1
            ))
        }""",
        arg={"before": before, "viewportHeight": page.viewport_size["height"]},
    )
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


def _assert_run_detail(page: Page, expected_title: str, expected_run_id: str) -> None:
    """Wait for readable presentation and the exact requested run identity."""
    detail = page.locator(".run-detail:visible").first
    detail.wait_for()
    expect(detail.locator("h3")).to_have_text(expected_title)
    expect(detail.get_by_title("Copy run ID", exact=True)).to_have_text(expected_run_id)
    page.wait_for_function(
        "expectedRunId => new URL(location.href).searchParams.get('run') === expectedRunId",
        arg=expected_run_id,
    )


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
    """Check the actionable target and its hit identity from one DOM snapshot."""
    action.scroll_into_view_if_needed()
    action.click(trial=True)
    observation = action.evaluate("""element => {
        const rect = element.getBoundingClientRect()
        const x = rect.left + rect.width / 2
        const y = rect.top + rect.height / 2
        const hit = document.elementFromPoint(x, y)
        return {
            box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
            is_target: hit === element || Boolean(hit && element.contains(hit)),
        }
    }""")
    box = observation["box"]
    assert box and box["width"] > 0 and box["height"] > 0, box
    assert 0 <= box["y"] < page.viewport_size["height"], box
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"], box
    assert observation["is_target"], box


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
def test_project_worktree_presentation(control_client, monkeypatch, width: int, height: int):
    """Exercise one-level disclosure, direct child links, history and context."""
    _, root, _, _ = control_client
    _seed_responsive_fixture(root)
    child_id = _register_responsive_worktree(root)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            for theme in ("light", "dark"):
                _set_theme_preference(page, theme)
                page.goto(f"{url}/?view=projects")
                _assert_theme(page, theme)
                parent = page.get_by_role("button", name=re.compile(r"^Test project"))
                parent.wait_for()
                disclosure = page.locator("details.worktree-disclosure")
                assert disclosure.get_attribute("open") is None
                _assert_header_and_flow(page)

                summary = page.locator("summary").filter(has_text="Worktrees (1)")
                summary.focus()
                summary.press("Enter")
                child = page.get_by_role("button", name=re.compile(r"^Feature worktree"))
                child.wait_for()
                _assert_no_horizontal_overflow(page)
                _assert_no_unauthorized_scrollers(page)

                child.click()
                page.wait_for_function(
                    "child => new URL(location.href).searchParams.get('project') === child",
                    arg=child_id,
                )
                page.get_by_role("button", name="New run", exact=True).wait_for()
                history_row = page.locator(
                    f".run-list-item[data-sidebar-editor-item='{RESPONSIVE_FIXTURE_RUN_ID}']"
                )
                history_row.wait_for()
                expect(history_row).to_contain_text(RESPONSIVE_FIXTURE_PLAN_PATH)
                _assert_document_moves(page)
                history_row.click()
                _assert_run_detail(page, RESPONSIVE_FIXTURE_TITLE, RESPONSIVE_FIXTURE_RUN_ID)
                _assert_header_and_flow(page)

                page.goto(f"{url}/?view=projects")
                search = page.get_by_role("textbox", name="Search projects and available candidates")
                search.fill("Feature worktree")
                child = page.get_by_role("button", name=re.compile(r"^Feature worktree"))
                child.wait_for()

                page.goto(f"{url}/?project={child_id}&view=projects")
                selected_child = page.get_by_role("button", name=re.compile(r"^Feature worktree"))
                selected_child.wait_for()
                expect(selected_child).to_have_attribute("aria-pressed", "true")
                _assert_header_and_flow(page)

                page.goto(f"{url}/?view=all-runs")
                page.get_by_role("heading", name="All runs", exact=True).wait_for()
                child_global_row = page.get_by_role(
                    "button",
                    name=re.compile(
                        rf"^Test project · Worktree: Feature worktree · Completed · "
                        rf"{re.escape(RESPONSIVE_GLOBAL_TITLE)} · {re.escape(RESPONSIVE_GLOBAL_RUN_ID)}$"
                    ),
                )
                child_global_row.wait_for()
                _assert_header_and_flow(page)
        finally:
            browser.close()


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
            run_row = page.locator(
                f".run-list-item[data-sidebar-editor-item='{RESPONSIVE_FIXTURE_RUN_ID}']"
            )
            run_row.scroll_into_view_if_needed()
            expect(run_row).to_contain_text(RESPONSIVE_FIXTURE_PLAN_PATH)
            run_row.click()
            _assert_run_detail(page, RESPONSIVE_FIXTURE_TITLE, RESPONSIVE_FIXTURE_RUN_ID)
            detail_box = page.locator(".sidebar-editor-detail").bounding_box()
            assert detail_box and detail_box["height"] > 0
            _assert_header_and_flow(page)
            _assert_last_action_hit_test(page)
            if _compact(page):
                page.get_by_role("button", name="← Back to Run history", exact=True).click()
                page.locator(".run-list-item").first.wait_for()

            _open_destination(page, "All runs")
            page.get_by_role("heading", name="All runs", exact=True).wait_for()
            all_run = page.get_by_role(
                "button",
                name=re.compile(
                    rf"^Test project · Completed · {re.escape(RESPONSIVE_GLOBAL_TITLE)} · "
                    rf"{re.escape(RESPONSIVE_GLOBAL_RUN_ID)}$"
                ),
            )
            all_run.wait_for()
            all_run.click()
            _assert_run_detail(page, RESPONSIVE_GLOBAL_TITLE, RESPONSIVE_GLOBAL_RUN_ID)
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
            expect(workflow).to_have_value("Managed")
            advanced = page.get_by_role("button", name="Advanced options", exact=True)
            expect(advanced).to_be_visible()
            advanced.click()
            expect(advanced).to_have_attribute("aria-expanded", "true")
            extra = page.get_by_label("Run extra instructions", exact=True)
            expect(extra).to_be_visible()
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
                        row = page.locator(
                            f".run-list-item[data-sidebar-editor-item='{RESPONSIVE_FIXTURE_RUN_ID}']"
                        )
                        row.wait_for()
                        if detail:
                            row.click()
                            _assert_run_detail(page, RESPONSIVE_FIXTURE_TITLE, RESPONSIVE_FIXTURE_RUN_ID)
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
            expect(control_max_turns).to_be_enabled()
            expect(control_team).to_be_enabled()
            expect(control_selector).to_be_enabled()
            control_team.locator("option[value='']").wait_for(state="attached")
            control_team.locator("option[value='fast_team']").wait_for(state="attached")
            control_team.locator("option[value='fast__team']").wait_for(state="attached")
            expect(control_team.locator("option[value='']")).to_have_text("No team")
            expect(control_team.locator("option[value='fast_team']")).to_have_text("Fast team (fast_team)")
            expect(control_team.locator("option[value='fast__team']")).to_have_text("Fast team (fast__team)")
            control_max_turns.fill("12")
            control_team.select_option("fast__team")
            control_selector.select_option("reasonix.new")
            save_controls = dashboard.get_by_role("button", name="Save run settings", exact=True)
            expect(save_controls).to_be_enabled()
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
            expect(save_controls).to_be_enabled()
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
            expect(confirm).to_be_enabled()
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


def test_durable_recovery_ui_journey(control_client, monkeypatch):
    """Drive replacement recovery through the real controller with fake providers."""
    from dataclasses import replace
    import hashlib

    from aflow.api.runner import execute_workflow as canonical_execute_workflow
    from aflow.control_plane.units import UnitState
    from aflow.daemon import worker_main
    from aflow.harnesses.base import HarnessInvocation
    from aflow.harnesses.muse import MuseAdapter
    from aflow.harnesses.session import SessionCapabilities, SessionRequest, SessionResult
    from aflow.plan import PlanSnapshot
    from aflow.repartition import EvidenceArtifactReferenceV2, create_envelope_v2, write_envelope_atomic
    from aflow.run_state import (
        ActiveImplementationScope,
        ControllerConfig,
        ControllerState,
        ExecutionContext,
    )
    from aflow.runlog import (
        RunMetadataWriter,
        RunPaths,
        capture_checkpoint_evidence,
        capture_plan_evidence,
    )
    from aflow.workflow import load_scope_evidence_for_resume
    from aflow_app_server import main
    from test_control_plane_api import _commit_fixture_repository

    _, root, units, _ = control_client
    service = main._control_plane_service
    assert service is not None
    plan_path = root / "plans" / "todo" / "test-plan.md"
    plan_text = """# Durable recovery fixture

### [x] Checkpoint 1: Setup
- [x] preserve setup

### [x] Checkpoint 2: Evidence
- [x] preserve evidence

### [x] Checkpoint 3: Review
- [x] preserve review boundary

### [ ] Checkpoint 4: Replacement
- [ ] continue from durable evidence
"""
    plan_path.write_text(plan_text, encoding="utf-8")
    config_path = root.parent / "global" / "aflow.toml"
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[harness.dsh.profiles.source]\n"
            'model = "unavailable-source"\n'
            "\n[harness.muse.profiles.replacement]\n"
            'model = "replacement-worker"\n'
            "\n[teams.recovery.roles]\n"
            'worker = "muse.replacement"\n'
        )

    # Establish the disposable main branch and a real Git-registered managed
    # worktree before the source run or its recovery evidence is created.
    # The repository-level test gitignore intentionally hides plans; this
    # fixture explicitly tracks its runnable plan so the managed worktree has
    # the same input that recovery binds.
    subprocess.run(
        ("git", "-C", str(root), "add", "-f", str(plan_path)),
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_fixture_repository(root)
    subprocess.run(("git", "-C", str(root), "branch", "-M", "main"), check=True)
    feature_branch = "feature/issue36-browser-recovery"
    worktree = root.parent / "issue36-browser-recovery-worktree"
    subprocess.run(
        (
            "git", "-C", str(root), "worktree", "add", "-q", "-b",
            feature_branch, str(worktree), "main",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    daemon = service._project(PROJECT_ID).daemon
    real_resume_bootstrap = daemon.service._resume_bootstrap
    source_id, source_dir, daemon = _seed_recovery_source(
        service,
        root,
        monkeypatch,
        source_id="browser-recovery-source",
    )
    monkeypatch.setattr(daemon.service, "_resume_bootstrap", real_resume_bootstrap)

    # Replace the fixture's compact source metadata with a canonical failed
    # run containing the schema-v2 scope envelope and exact Markdown evidence.
    source_paths = RunPaths(
        repo_root=root,
        runs_root=root / ".aflow" / "runs",
        run_dir=source_dir,
        turns_dir=source_dir / "turns",
        manager_dir=source_dir / "manager",
        run_json=source_dir / "run.json",
    )
    plan_ref = capture_plan_evidence(source_paths, plan_text)
    checkpoint_text = "### [ ] Checkpoint 4: Replacement\n- [ ] continue from durable evidence\n"
    checkpoint_ref = capture_checkpoint_evidence(source_paths, checkpoint_text)
    envelope = create_envelope_v2(
        scope_id="test-plan.md::checkpoint-4::Replacement",
        original_plan_path=plan_path,
        plan_text=plan_text,
        checkpoint_index=4,
        repo_root=root,
        plan_ref=EvidenceArtifactReferenceV2(
            kind=plan_ref.kind,
            path=plan_ref.path,
            sha256=plan_ref.sha256,
            byte_size=plan_ref.byte_size,
        ),
        checkpoint_ref=EvidenceArtifactReferenceV2(
            kind=checkpoint_ref.kind,
            path=checkpoint_ref.path,
            sha256=checkpoint_ref.sha256,
            byte_size=checkpoint_ref.byte_size,
        ),
    )
    envelope_path = write_envelope_atomic(
        envelope, source_dir / "scopes" / envelope.scope_digest
    )
    envelope_bytes = envelope_path.read_bytes()
    scope = ActiveImplementationScope(
        scope_id=envelope.scope_id,
        original_plan_path=str(plan_path),
        checkpoint_index=4,
        checkpoint_name=envelope.checkpoint_name,
        opened_turn_number=1,
        envelope_artifact_path=envelope_path.relative_to(source_dir).as_posix(),
        envelope_artifact_sha256=hashlib.sha256(envelope_bytes).hexdigest(),
        envelope_canonical_sha256=envelope.canonical_envelope_sha256,
    )
    scope_artifacts = load_scope_evidence_for_resume(
        source_dir, scope, envelope_bytes
    )
    source_state = ControllerState(
        last_snapshot=PlanSnapshot(
            current_checkpoint_name="Checkpoint 4: Replacement",
            unchecked_checkpoint_count=1,
            current_checkpoint_unchecked_step_count=1,
            is_complete=False,
            total_checkpoint_count=4,
            current_checkpoint_index=4,
        ),
        run_id=source_id,
        selected_start_step="implement",
        effective_max_turns=3,
        role_selectors={"worker": "dsh.source"},
        active_implementation_scope=scope,
    )
    source_config = ControllerConfig(
        repo_root=root,
        plan_path=plan_path,
        max_turns=3,
        keep_runs=20,
        start_step="implement",
    )
    source_execution = ExecutionContext(
        primary_repo_root=root,
        execution_repo_root=worktree,
        main_branch="main",
        feature_branch=feature_branch,
        worktree_path=worktree,
        setup=("worktree", "branch"),
        teardown=(),
    )
    (source_dir / "run.json").unlink()
    RunMetadataWriter(
        paths=source_paths,
        config=source_config,
        state=source_state,
        workflow_name="managed",
    ).write(
        status="failed",
        execution_context=source_execution,
        original_plan_path=plan_path,
        active_plan_path=plan_path,
        current_step_name="implement",
    )
    source_before = _source_artifact_bytes(source_dir)

    # Admission uses the same complete source context as the real worker
    # reconstruction, including the registered worktree and v2 artifacts.
    base_resume_bootstrap = real_resume_bootstrap

    def browser_resume_bootstrap(*args, **kwargs):
        bootstrap = base_resume_bootstrap(*args, **kwargs)
        context = replace(
            bootstrap.resume_context,
            feature_branch=feature_branch,
            worktree_path=worktree,
            main_branch="main",
            setup=("worktree", "branch"),
            teardown=(),
            active_plan_path=plan_path,
            active_implementation_scope=scope,
            scope_envelope_bytes=envelope_bytes,
            scope_envelope_source_path=str(envelope_path),
            scope_evidence_artifact_bytes=scope_artifacts,
        )
        return replace(bootstrap, resume_context=context)

    monkeypatch.setattr(daemon.service, "_resume_bootstrap", browser_resume_bootstrap)

    class FakeTargetDriver:
        capabilities = SessionCapabilities(
            session_identity=True,
            followup_turn=True,
            resume_with_model=True,
            idempotent_turn_start=True,
        )

        def __init__(self) -> None:
            self.requests: list[SessionRequest] = []

        def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
            self.requests.append(request)
            effective = f"{request.system_prompt}\n\n{request.user_prompt}"
            return HarnessInvocation(
                label="fake-muse",
                argv=("fake-muse",),
                env={},
                prompt_mode="stdin",
                system_prompt=request.system_prompt,
                user_prompt=request.user_prompt,
                effective_prompt=effective,
                stdin_text=effective,
            )

        def parse_result(
            self, request: SessionRequest, stdout: str, *, returncode: int = 0
        ) -> SessionResult:
            assert returncode == 0
            return SessionResult(
                session_id="browser-target-session",
                selector=request.selector,
                model=request.model,
                effort=request.effort,
                final_output=stdout,
                provider_operation_id="browser-target-operation",
                idempotency_key=request.idempotency_key,
                capabilities=self.capabilities,
            )

    class NeverSourceDriver:
        capabilities = FakeTargetDriver.capabilities

        def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
            raise AssertionError(f"source provider was called: {request.selector}")

        def parse_result(
            self, request: SessionRequest, stdout: str, *, returncode: int = 0
        ) -> SessionResult:
            raise AssertionError(f"source provider returned: {request.selector}")

    target_driver = FakeTargetDriver()
    source_driver = NeverSourceDriver()
    runner_calls: list[tuple[list[str], str]] = []
    completed_plan = plan_text.replace(
        "### [ ] Checkpoint 4: Replacement\n- [ ] continue from durable evidence",
        "### [x] Checkpoint 4: Replacement\n- [x] continue from durable evidence",
    )

    def fake_runner(argv, **kwargs):
        cwd = Path(str(kwargs["cwd"]))
        runner_calls.append((list(argv), str(cwd)))
        execution_plan = cwd / plan_path.relative_to(root)
        execution_plan.write_text(completed_plan, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "DONE\n", "")

    def execute_with_fake_providers(prepared, **kwargs):
        return canonical_execute_workflow(
            prepared,
            adapter=MuseAdapter(),
            runner=fake_runner,
            session_driver=target_driver,
            source_session_driver=source_driver,
            **kwargs,
        )

    # The browser acceptance is local-only; do not let inherited repository
    # publication settings contact a remote while the fake worker runs.
    monkeypatch.setattr("aflow.workflow.publish_completed_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("aflow.daemon.execute_workflow", execute_with_fake_providers)
    source_unit_name = f"aflow-run-{source_id}.service"
    units.units[source_unit_name] = UnitState(
        name=source_unit_name,
        active_state="inactive",
        sub_state="dead",
    )

    original_start = units.start

    def start_and_run_worker(name, argv, **kwargs):
        started = original_start(name, argv, **kwargs)
        run_id = name.removeprefix("aflow-run-").removesuffix(".service")
        assert worker_main(repo_root=root, config_path=config_path, run_id=run_id) == 0
        units.units[name] = UnitState(
            name=name,
            active_state="inactive",
            sub_state="dead",
            result="success",
        )
        return started

    monkeypatch.setattr(units, "start", start_and_run_worker)

    repository = service._project(PROJECT_ID).daemon.application.repository
    original_status = repository.get_run_status
    source_active = False

    def source_status(run_id: str):
        status = original_status(run_id)
        if run_id != source_id:
            return status
        return replace(
            status,
            activity="active" if source_active else "inactive",
            status_reason_code="execution_active" if source_active else "controller_failed",
            evidence={
                **status.evidence,
                "unit_active": source_active,
                "unit_observation": "observed",
                "worker": {"active": True} if source_active else status.evidence.get("worker"),
            },
        )

    monkeypatch.setattr(repository, "get_run_status", source_status)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    evidence_root = Path(
        "/root/code/evidence/aflow-dogfood-20260909/issue36-review-20260911"
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    browser_name = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()

    try:
        with live_server() as url, sync_playwright() as playwright:
            browser = _browser(playwright)
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                _login(page, url)
                page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={source_id}")
                dashboard = _visible_dashboard(page)
                dashboard.locator(".run-detail h3").wait_for()
                page.get_by_role("button", name="Resume as new run…", exact=True).wait_for()
                recovery_button = page.get_by_role(
                    "button", name="Recover with another worker…", exact=True
                )
                recovery_button.wait_for()
                expect(recovery_button).to_have_class(re.compile(r"btn-secondary"))
                recovery_button.click()
                _choose_combobox(
                    dashboard,
                    "Recovery worker",
                    "muse.replacement",
                    "muse.replacement",
                )
                dashboard.get_by_text(
                    re.compile(r"Submission mode: durable_evidence"),
                ).wait_for()
                assert "private context from the old provider session is unavailable" in dashboard.inner_text().lower()
                submit = dashboard.get_by_role(
                    "button", name="Recover with selected worker", exact=True
                )
                assert dashboard.get_by_label("Recovery worker", exact=True).input_value() == "muse.replacement"
                _assert_action_hit_test(page, submit)

                # The source changes after the draft was opened. The actual
                # structured REST rejection must preserve the selected worker.
                source_active = True
                submit.click()
                dashboard.get_by_text(
                    re.compile(r"Recovery was rejected:.*source activity"),
                    exact=False,
                ).wait_for()
                assert dashboard.get_by_label("Recovery worker", exact=True).input_value() == "muse.replacement"
                assert len(units.start_calls) == 0
                source_active = False

                submit = dashboard.get_by_role(
                    "button", name="Recover with selected worker", exact=True
                )
                submit.click()
                page.wait_for_timeout(1000)
                dashboard.get_by_role(
                    "heading", name="Replacement worker started", exact=True
                ).wait_for()
                successor_id = dashboard.get_by_title("Copy run ID", exact=True).inner_text()
                assert successor_id != source_id
                page.wait_for_function(
                    "expected => new URL(location.href).searchParams.get('run') === expected",
                    arg=successor_id,
                )
                detail_text = dashboard.inner_text()
                assert "Source run" in detail_text
                assert "dsh.source" in detail_text
                assert "muse.replacement" in detail_text
                assert "recovery/recovery-intent.json" in detail_text
                assert f"worktree:{worktree.resolve()}" in detail_text
                assert "private context from the old provider session was unavailable" in detail_text.lower()
                assert dashboard.get_by_role(
                    "link", name=re.compile(r"open the recorded event and artifact details"),
                ).is_visible()
                assert len(units.start_calls) == 1
                assert units.start_calls[0][0] == f"aflow-run-{successor_id}.service"
                assert units.start_calls[0][0] != source_unit_name
                assert runner_calls == [(["fake-muse"], str(worktree))]
                assert len(target_driver.requests) == 1
                assert target_driver.requests[0].selector == "muse.replacement"
                assert target_driver.requests[0].session_id is None
                assert source_driver.capabilities.session_identity
                assert "no source session ID" in target_driver.requests[0].user_prompt
                assert _source_artifact_bytes(source_dir) == source_before
                assert (worktree / plan_path.relative_to(root)).read_text(encoding="utf-8") == completed_plan

                successor_payload = json.loads(
                    (root / ".aflow" / "runs" / successor_id / "run.json").read_text(
                        encoding="utf-8"
                    )
                )
                assert successor_payload["recovery_runtime"]["operation_state"] == "consumed"
                assert successor_payload["active_role_sessions"][0]["selector"] == "muse.replacement"
                assert successor_payload["active_role_sessions"][0]["session_id"] == "browser-target-session"

                page.reload()
                dashboard = _visible_dashboard(page)
                dashboard.get_by_role(
                    "heading", name="Replacement worker started", exact=True
                ).wait_for()
                dashboard.get_by_text("All 4 checkpoints complete", exact=True).wait_for()
                _assert_header_and_flow(page)
                screenshot = evidence_root / f"issue36-recovery-{browser_name}-390x844.png"
                page.screenshot(path=str(screenshot), full_page=True)
                print("ISSUE36_RECOVERY_SCREENSHOT", screenshot)

                source_active = True
                units.units[source_unit_name] = UnitState(
                    name=source_unit_name,
                    active_state="active",
                    sub_state="running",
                )
                page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={source_id}")
                active_dashboard = _visible_dashboard(page)
                active_dashboard.locator(".run-detail h3").wait_for()
                page.get_by_text(re.compile(r"Restart unavailable:"), exact=False).wait_for()
                assert page.get_by_role(
                    "button", name="Recover with another worker…", exact=True
                ).count() == 0
                assert len(units.start_calls) == 1
                assert _source_artifact_bytes(source_dir) == source_before
                _assert_header_and_flow(page)
            finally:
                browser.close()
    finally:
        if worktree.exists():
            subprocess.run(
                ("git", "-C", str(root), "worktree", "remove", "--force", str(worktree)),
                check=True,
                capture_output=True,
                text=True,
            )

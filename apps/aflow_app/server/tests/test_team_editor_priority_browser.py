"""Real-browser coverage for the compact, Worker/Reviewer-first team editor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from test_control_plane_api import control_client, live_server  # noqa: F401
from test_responsive_browser import (
    _assert_header_and_flow,
    _browser,
    _login,
    _open_destination,
    _seed_responsive_fixture,
    _select_settings_section,
)


VIEWPORTS = (
    pytest.param(320, 568, id="phone-portrait"),
    pytest.param(390, 844, id="phone-tall"),
    pytest.param(390, 420, id="phone-short"),
    pytest.param(844, 390, id="phone-landscape"),
    pytest.param(1280, 720, id="desktop"),
)


def _seed_priority_fixture(root: Path) -> None:
    """Create realistic legacy, family, inherited, and custom-only teams."""
    _seed_responsive_fixture(root)
    config_dir = root.parent / "global"
    (config_dir / "aflow.toml").write_text(
        """
[aflow]
default_workflow = "managed"
max_turns = 5

[harness.codex.profiles.global_worker]
model = "global-worker"

[harness.codex.profiles.global_reviewer]
model = "global-reviewer"

[harness.codex.profiles.luna_max]
model = "gpt-5.6-luna"
effort = "max"

[harness.codex.profiles.astra_low]
model = "gpt-6-astra"
effort = "low"

[harness.codex.profiles.sol_medium]
model = "gpt-5.6-sol"
effort = "medium"

[harness.codex.profiles.manager_full]
model = "manager-full"

[harness.codex.profiles.manager_lite]
model = "manager-lite"

[harness.codex.profiles.senior_architect]
model = "senior-architect"

[harness.codex.profiles.alt_worker]
model = "alternate-worker"

[roles]
worker = "codex.global_worker"
reviewer = "codex.global_reviewer"
architect = "codex.astra_low"
manager_full = "codex.manager_full"
manager_lite = "codex.manager_lite"
senior_architect = "codex.senior_architect"

[prompts]
implement = "Implement the selected task."
review = "Review the selected task."

[teams.luna_checkpoint]
display_name = "Luna Max / Luna CP / Astra final"
upgrade_to = "luna_checkpoint_sol"

[teams.luna_checkpoint.roles]
worker = "codex.astra_low"
reviewer = "codex.luna_max"
architect = "codex.astra_low"
manager_full = "codex.sol_medium"
manager_lite = "codex.luna_max"
senior_architect = "codex.astra_low"

[teams.luna_checkpoint.prompts]
worker = "Legacy worker prompt."

[teams.luna_checkpoint_sol]
display_name = "Sol Medium / Luna CP / Astra final"

[teams.luna_checkpoint_sol.roles]
worker = "codex.sol_medium"

[teams.review_family]
display_name = "Review family / inherited source"
upgrade_to = "review_family_inherited"

[teams.review_family.roles]
worker = "codex.luna_max"
reviewer = "codex.luna_max"

[teams.review_family_inherited]
display_name = "Inherited stage / final review"
extends = "review_family"

[teams.custom_only]
display_name = "Custom roles only"

[teams.custom_only.roles]
manager_full = "codex.manager_full"
manager_lite = "codex.manager_lite"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (config_dir / "workflows.toml").write_text(
        """
[workflow.managed]
team = "review_family"

[workflow.managed.steps.implement]
role = "worker"
prompts = ["implement"]
go = [{ to = "END", when = "DONE" }, { to = "review" }]

[workflow.managed.steps.review]
role = "reviewer"
prompts = ["review"]
go = [{ to = "END", when = "DONE" }, { to = "implement" }]
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _open_team_editor(page: Page, label: str) -> Page:
    if page.locator(".team-families-settings .sidebar-editor-navigation").is_hidden():
        page.get_by_role("button", name="← Back to Team families", exact=True).click()
    navigation = page.get_by_role("navigation", name="Team families", exact=True)
    entry = navigation.get_by_role("button", name=label, exact=True)
    entry.wait_for(state="visible")
    entry.click()
    page.locator(".team-family-detail").wait_for(state="visible")
    return page


def _open_advanced(page: Page) -> Page:
    advanced = page.locator("details.team-family-advanced-details")
    expect(advanced).to_have_count(1)
    if advanced.get_attribute("open") is None:
        advanced.locator(":scope > summary").click()
    expect(advanced).to_have_attribute("open", "")
    return page


def _open_advanced_section(page: Page, label: str) -> Page:
    advanced = page.locator("details.team-family-advanced-details")
    section = advanced.locator(".team-family-advanced-content > details").filter(has_text=label).first
    if section.get_attribute("open") is None:
        section.locator(":scope > summary").click()
    expect(section).to_have_attribute("open", "")
    return page


def _choose_combobox(page: Page, label: str, query: str, option_text: str) -> None:
    field = page.get_by_role("combobox", name=label, exact=True)
    field.click()
    field.fill(query)
    option = page.get_by_role("option").filter(has_text=option_text).first
    option.wait_for(state="visible")
    option.click()


def _visible_primary_rows(page: Page):
    return page.locator(".team-family-primary-role-list .team-family-role-row:visible")


@pytest.mark.parametrize("theme", ("light", "dark"))
@pytest.mark.parametrize("width,height", VIEWPORTS)
def test_selected_team_editor_priority_and_capabilities(
    control_client,
    monkeypatch,
    tmp_path,
    theme: str,
    width: int,
    height: int,
):
    """The opened editor is compact without removing domain capabilities."""
    _, root, _, _ = control_client
    _seed_priority_fixture(root)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    browser_name = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    screenshot_dir = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", "")).expanduser() if os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", "").strip() else tmp_path
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.add_init_script(f"localStorage.setItem('aflow.appearance', {json.dumps(theme)})")
            _login(page, url)
            _open_destination(page, "Settings")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            for section in ("Agents & Roles", "Workflows", "Changelog", "Teams"):
                _select_settings_section(page, section)
                _assert_header_and_flow(page)
            page.locator(".team-families-settings").wait_for()

            _open_team_editor(page, "Luna Max / Luna CP / Astra final")
            detail = page.locator(".team-family-detail")
            primary = _visible_primary_rows(page)
            expect(primary).to_have_count(2)
            assert primary.evaluate_all(
                "rows => rows.map(row => row.querySelector('.team-family-role-heading strong')?.textContent)"
            ) == ["Worker", "Reviewer"]
            advanced = detail.locator("details.team-family-advanced-details")
            expect(advanced).not_to_have_attribute("open", "")
            expect(advanced.locator(":scope > summary")).to_have_text("Advanced details")

            if (width, height) == (390, 844):
                worker_box = primary.nth(0).bounding_box()
                reviewer_box = primary.nth(1).bounding_box()
                assert worker_box and worker_box["y"] <= 400, worker_box
                assert reviewer_box and reviewer_box["y"] <= 562, reviewer_box
            if width >= 960 and height >= 600:
                assert all(
                    (box := primary.nth(index).bounding_box()) is not None
                    and box["y"] + box["height"] <= height
                    for index in range(2)
                )

            initial_screenshot = screenshot_dir / f"team-editor-priority-{browser_name}-{width}x{height}-{theme}-legacy-initial.png"
            page.screenshot(path=str(initial_screenshot), full_page=True)
            print("AFLOW_TEAM_EDITOR_PRIORITY_SCREENSHOT", initial_screenshot)

            requests_before_details: list[str] = []
            page.on("request", lambda request: requests_before_details.append(request.method) if request.method in {"POST", "PATCH", "PUT", "DELETE"} else None)
            request_count = len(requests_before_details)
            _open_advanced(page)
            assert len(requests_before_details) == request_count
            _open_advanced_section(page, "Other roles")
            expect(detail.locator(".team-family-role-row").filter(has_text="Architect").first).to_be_visible()
            _open_advanced_section(page, "Role prompts")
            expect(detail.get_by_role("button", name="Edit in Prompts", exact=True).first).to_be_visible()
            _open_advanced_section(page, "Upgrade routing")
            expect(detail.get_by_label(re.compile(r"Upgrade to for team"), exact=False)).to_be_visible()
            _open_advanced_section(page, "Name and identity")
            identity = detail.get_by_label("Display name", exact=True)
            identity.fill("Luna Max / Luna CP / Astra final edited")
            identity.press("Tab")

            # A real role edit keeps its exact team ID and becomes the only
            # configuration PATCH when the shared Save all action is used.
            _choose_combobox(page, "Worker", "codex.alt", "codex.alt_worker")
            save_requests: list[dict[str, object]] = []

            def capture_save(route) -> None:
                if route.request.method == "PATCH":
                    payload = route.request.post_data_json
                    if isinstance(payload, dict):
                        save_requests.append(payload)
                route.continue_()

            page.route("**/api/config", capture_save)
            save = page.get_by_role("button", name="Save all changes", exact=True)
            expect(save).to_be_enabled()
            save.click()
            page.get_by_text("Workflow settings saved; new runs use the saved configuration", exact=False).wait_for()
            assert save_requests
            actions = save_requests[-1].get("actions")
            assert isinstance(actions, list)
            assert {
                "type": "set_team_role",
                "team": "luna_checkpoint",
                "role": "worker",
                "selector": "codex.alt_worker",
            } in actions
            assert all(action.get("team") == "luna_checkpoint" for action in actions if isinstance(action, dict) and action.get("type") == "set_team_role")
            page.unroute("**/api/config", capture_save)

            # Inherited primary assignments are visible but do not become
            # overrides until the existing action is deliberately selected.
            _open_team_editor(page, "Review family / inherited source")
            detail = page.locator(".team-family-detail")
            detail.get_by_role("button", name="Inherited stage / final review", exact=True).click()
            primary = _visible_primary_rows(page)
            expect(primary).to_have_count(2)
            reviewer_row = primary.nth(1)
            expect(reviewer_row).to_contain_text("Inherited from")
            expect(reviewer_row.get_by_role("button", name="Override role", exact=True)).to_be_visible()
            reviewer_row.get_by_role("button", name="Override role", exact=True).click()
            expect(reviewer_row.get_by_role("button", name="Restore inheritance", exact=True)).to_be_visible()
            _choose_combobox(page, "Reviewer", "codex.sol", "codex.sol_medium")
            if width < 960 or height < 600:
                page.get_by_role("button", name="← Back to Team families", exact=True).click()
            _open_team_editor(page, "Custom roles only")
            detail = page.locator(".team-family-detail")
            # The global Worker/Reviewer defaults remain effective on this
            # standalone team; its explicitly configured manager roles stay
            # secondary and are not promoted into invented primary controls.
            expect(_visible_primary_rows(page)).to_have_count(2)
            _open_advanced(page)
            _open_advanced_section(page, "Other roles")
            expect(detail.locator(".team-family-role-row").filter(has_text="Manager full")).to_be_visible()
            expect(detail.locator(".team-family-role-row").filter(has_text="Manager lite")).to_be_visible()

            screenshot = screenshot_dir / f"team-editor-priority-{browser_name}-{width}x{height}-{theme}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            print("AFLOW_TEAM_EDITOR_PRIORITY_SCREENSHOT", screenshot)
        finally:
            browser.close()

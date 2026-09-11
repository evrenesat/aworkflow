"""Real-browser coverage for readable plan backup provenance."""

from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from aflow.workflow import _backup_original_plan
from test_control_plane_api import PROJECT_ID, TOKEN, control_client, live_server  # noqa: F401
from test_responsive_browser import _browser


def _login(page, url: str) -> None:
    page.goto(url)
    page.get_by_placeholder("Auth token").fill(TOKEN)
    page.get_by_role("button", name="Login", exact=True).click()
    page.locator(".app-header").wait_for()


def test_plan_backup_provenance_browser_journey(control_client, monkeypatch, tmp_path: Path) -> None:
    _, root, _, _ = control_client
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    plan_name = "browser-backup.md"
    initial = "# Browser baseline\n\n- created in the browser\n"
    edited = "# Browser edit\n\n- saved after promotion\n"
    draft = edited + "\n# Keep this unsaved draft\n"

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        try:
            page = context.new_page()
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=plans")
            page.get_by_label("New plan filename", exact=True).fill(plan_name)
            page.get_by_role("button", name="Create plan", exact=True).click()
            editor = page.get_by_label("Plan content", exact=True)
            editor.wait_for()

            editor.fill(initial)
            with page.expect_response(
                lambda response: response.request.method == "PUT"
                and f"/plans/todo/{plan_name}" in response.url
            ) as save_response:
                page.get_by_role("button", name="Save", exact=True).click()
            assert save_response.value.status == 200
            expect(editor).to_have_value(initial)

            page.get_by_role("button", name="More", exact=True).click()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and f"/plans/todo/{plan_name}/promote" in response.url
            ) as promote_response:
                page.get_by_role("menuitem", name="Move to Ready", exact=True).click()
            assert promote_response.value.status == 200
            page.get_by_role("button", name="More", exact=True).click()
            expect(page.get_by_role("menuitem", name="Move to Done", exact=True)).to_be_visible()
            page.keyboard.press("Escape")

            editor.fill(edited)
            with page.expect_response(
                lambda response: response.request.method == "PUT"
                and f"/plans/in_progress/{plan_name}" in response.url
            ) as save_response:
                page.get_by_role("button", name="Save", exact=True).click()
            assert save_response.value.status == 200
            expect(editor).to_have_value(edited)

            current_path = root / "plans" / "in-progress" / plan_name
            assert current_path.read_text(encoding="utf-8") == edited
            _backup_original_plan(root, current_path, event="startup_preparation")
            editor.fill(draft)

            with page.expect_response(
                lambda response: response.request.method == "GET"
                and "/plans/in_progress/browser-backup.md/backups" in response.url
            ) as history_response:
                page.get_by_text("Backup history", exact=True).click()
            assert history_response.value.status == 200
            expect(page.get_by_text("Baseline", exact=True)).to_be_visible()
            expect(page.get_by_text("Snapshot", exact=True)).to_be_visible()
            expect(page.get_by_text("Initial Ready baseline", exact=True)).to_be_visible()
            expect(page.get_by_text("Captured plan snapshot", exact=True)).to_be_visible()
            expect(page.get_by_text("Later snapshot; original baseline is unknown", exact=True)).to_have_count(0)
            expect(editor).to_have_value(draft)
            assert page.get_by_role("button", name=re.compile("reset", re.IGNORECASE)).count() == 0

            metrics = page.evaluate(
                """() => ({
                    width: innerWidth,
                    scrollWidth: document.documentElement.scrollWidth,
                    documentHeight: document.scrollingElement?.scrollHeight ?? 0,
                    viewportHeight: innerHeight,
                })"""
            )
            assert metrics["scrollWidth"] <= metrics["width"] + 1, metrics
            assert metrics["documentHeight"] >= metrics["viewportHeight"], metrics
            artifact_root = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
            artifact_root.mkdir(parents=True, exist_ok=True)
            engine = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
            screenshot = artifact_root / f"plan-backup-{engine}-390x844.png"
            page.screenshot(path=str(screenshot), full_page=True)
            print("PLAN_BACKUP_BROWSER_EVIDENCE", screenshot)
        finally:
            context.close()
            browser.close()

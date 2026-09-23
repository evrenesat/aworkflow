"""Disposable browser coverage for revocable launch confirmation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from test_control_plane_api import (
    PROJECT_ID,
    TOKEN,
    _commit_fixture_repository,
    control_client,  # noqa: F401
    live_server,
)
from test_responsive_browser import _browser, _login


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_launch_confirmation_cancel_while_revalidation_held(
    control_client,
    monkeypatch,
) -> None:
    """Cancel a held final read without sending a disposable start mutation."""
    _, root, units, _ = control_client
    _commit_fixture_repository(root)
    branch = _git(root, "symbolic-ref", "--short", "HEAD")
    base_head = _git(root, "rev-parse", "HEAD")
    plan = root / "plans" / "in-progress" / "cancel-confirmation.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "# Cancel confirmation\n\n"
        "## Git Tracking\n\n"
        f"- Plan Branch: `{branch}`\n"
        f"- Pre-Handoff Base HEAD: `{base_head}`\n\n"
        "### [ ] Checkpoint 1: Start\n"
        "- [ ] keep cancellation safe\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AFLOW_APP_WEB_DIST",
        str(Path(__file__).resolve().parents[2] / "web" / "dist"),
    )

    held_config_route = []
    config_reads = 0
    start_requests = []

    def hold_second_config(route):
        nonlocal config_reads
        if route.request.method == "GET" and urlsplit(route.request.url).path == "/api/config":
            config_reads += 1
            if config_reads == 2:
                held_config_route.append(route)
                return
        route.continue_()

    def reject_start(route):
        if route.request.method == "POST":
            start_requests.append(route.request)
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": "start must remain unsubmitted"}),
            )
            return
        route.continue_()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.route("**/api/config", hold_second_config)
        page.route(f"**/api/control-plane/projects/{PROJECT_ID}/runs", reject_start)
        try:
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs")
            page.get_by_role("button", name="New run", exact=True).click()
            plan_input = page.get_by_label("Run plan", exact=True)
            plan_input.wait_for()
            plan_input.click()
            plan_input.fill("cancel-confirmation")
            page.get_by_role("option", name="cancel-confirmation.md", exact=False).click()
            expect(plan_input).to_have_value("plans/in-progress/cancel-confirmation.md")

            workflow = page.get_by_label("Run workflow", exact=True)
            workflow.click()
            workflow.fill("managed")
            workflow.press("ArrowDown")
            workflow.press("Enter")
            expect(workflow).to_have_value("Managed")
            page.wait_for_function(
                "() => document.querySelector('.worktree-preflight')?.dataset.preflightStatus === 'ready'"
            )
            confirmation = page.get_by_role(
                "checkbox",
                name="Continue despite uncommitted changes",
                exact=True,
            )
            if confirmation.count():
                confirmation.check()
            review = page.get_by_role("button", name="Review start…", exact=True)
            expect(review).to_be_enabled()
            review.click()
            page.get_by_role("region", name="Review start", exact=True).wait_for()
            start = page.get_by_role("button", name="Start run", exact=True)
            expect(start).to_be_enabled()
            start.click()

            deadline = time.monotonic() + 5
            while not held_config_route and time.monotonic() < deadline:
                page.wait_for_timeout(25)
            assert held_config_route, f"the second /api/config read was not held (reads={config_reads})"
            cancel = page.get_by_role("button", name="Cancel review", exact=True)
            expect(cancel).to_be_enabled()
            cancel.click()
            expect(page.get_by_role("region", name="Review start", exact=True)).to_have_count(0)

            response = held_config_route[0].fetch()
            held_config_route[0].fulfill(response=response)
            page.wait_for_timeout(250)
            assert start_requests == []
            assert units.start_calls == []
        finally:
            if held_config_route:
                try:
                    response = held_config_route[0].fetch()
                    held_config_route[0].fulfill(response=response)
                except Exception:
                    pass
            page.unroute("**/api/config", hold_second_config)
            page.unroute(f"**/api/control-plane/projects/{PROJECT_ID}/runs", reject_start)
            page.close()
            browser.close()

"""Real-client plan pagination journey across desktop and compact layouts."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
import pytest

from test_control_plane_api import (
    PROJECT_ID,
    _commit_fixture_repository,
    control_client as _control_client_fixture,  # noqa: F401
    live_server,
)
from test_responsive_browser import _browser, _login


DONE_PLAN_COUNT = 120
READY_PLAN_PATH = "plans/in-progress/pagination-ready.md"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _seed_pagination_fixture(root: Path) -> None:
    done_dir = root / "plans" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    for index in range(DONE_PLAN_COUNT):
        (done_dir / f"completed-{index:03d}.md").write_text(
            f"# Completed plan {index:03d}\n\nThis disposable plan is before the Ready choice.\n",
            encoding="utf-8",
        )

    # Establish a real base commit before writing the selectable plan's
    # tracking metadata. The second fixture commit leaves the checkout clean
    # for the read-only preflight.
    subprocess.run(("git", "add", "-f", "--", "plans"), cwd=root, check=True)
    _commit_fixture_repository(root)
    ready = root / READY_PLAN_PATH
    ready.parent.mkdir(parents=True, exist_ok=True)
    backtick = chr(96)
    ready.write_text(
        "# Pagination Ready choice\n\n"
        "## Git Tracking\n\n"
        f"- Plan Branch: {backtick}{_git(root, 'symbolic-ref', '--short', 'HEAD')}{backtick}\n"
        f"- Pre-Handoff Base HEAD: {backtick}{_git(root, 'rev-parse', 'HEAD')}{backtick}\n\n"
        "### [ ] Checkpoint 1: Browser selection\n"
        "- [ ] Select this exact path without starting a run.\n",
        encoding="utf-8",
    )
    subprocess.run(("git", "add", "-f", "--", READY_PLAN_PATH), cwd=root, check=True)
    _commit_fixture_repository(root)


@pytest.mark.parametrize(
    ("width", "height"),
    (
        pytest.param(1280, 720, id="desktop-1280x720"),
        pytest.param(390, 844, id="mobile-390x844"),
    ),
)
def test_complete_plan_pagination_reaches_read_only_review(
    _control_client_fixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    height: int,
) -> None:
    _, root, units, _ = _control_client_fixture
    _seed_pagination_fixture(root)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    plan_requests: list[str] = []
    preflight_bodies: list[dict[str, object]] = []
    start_requests: list[str] = []

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})

        def observe_request(request) -> None:
            parsed = urlsplit(request.url)
            if request.method == "GET" and parsed.path.endswith("/plans"):
                plan_requests.append(request.url)
            if request.method != "POST":
                return
            if parsed.path.endswith("/runs/preflight") and request.post_data:
                preflight_bodies.append(json.loads(request.post_data))
            elif parsed.path.endswith("/runs"):
                start_requests.append(request.url)

        page.on("request", observe_request)
        try:
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs")
            page.get_by_role("button", name="New run", exact=True).wait_for()
            page.get_by_role("button", name="New run", exact=True).click()

            plan = page.get_by_label("Run plan", exact=True)
            plan.wait_for()
            plan.click()
            page.get_by_role("option", name=READY_PLAN_PATH, exact=False).click()
            expect(plan).to_have_value(READY_PLAN_PATH)

            workflow = page.get_by_label("Run workflow", exact=True)
            workflow.click()
            workflow.fill("managed")
            workflow.press("ArrowDown")
            workflow.press("Enter")
            expect(workflow).to_have_value("Managed")

            preflight = page.locator(".worktree-preflight")
            preflight.wait_for()
            page.wait_for_function(
                """() => document.querySelector('.worktree-preflight')?.dataset.preflightStatus === 'ready'"""
            )
            assert any(body.get("plan_path") == READY_PLAN_PATH for body in preflight_bodies)

            review_button = page.get_by_role("button", name="Review start…", exact=True)
            expect(review_button).to_be_enabled()
            review_button.click()
            review = page.get_by_role("region", name="Review start", exact=True)
            review.wait_for()
            expect(review).to_contain_text(READY_PLAN_PATH)

            assert len(plan_requests) >= 2
            assert all(parse_qs(urlsplit(request).query).get("limit") == ["100"] for request in plan_requests)
            assert any(
                parse_qs(urlsplit(request).query).get("cursor") == [
                    "plans/done/completed-099.md"
                ]
                for request in plan_requests
            )
            assert start_requests == []
            assert units.start_calls == []
        finally:
            page.close()
            browser.close()

"""Real browser parity journeys for truthful run progress."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from test_control_plane_api import (
    PROJECT_ID,
    _close_issue35_repair_scope,
    _seed_issue35_progress_fixture,
    live_server,
)
from test_control_plane_api import control_client  # noqa: F401
from test_responsive_browser import _browser, _login


def _screenshot_path(tmp_path: Path, state: str, width: int, height: int) -> Path:
    artifact_root = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    artifact_root.mkdir(parents=True, exist_ok=True)
    engine = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    return artifact_root / f"run-progress-{engine}-{state}-{width}x{height}.png"


def _assert_shell_contract(page, width: int, height: int) -> None:
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
    main = page.locator(".workspace-main").bounding_box()
    assert main and main["height"] > 0, main
    if width >= 960 and height >= 600:
        assert page.locator(".app-header-row-one").is_visible()
        assert page.locator(".app-header-row-two").is_visible()
        assert page.locator(".header-slot-context").is_visible()
        return

    menu_button = page.get_by_role("button", name="Menu", exact=True)
    menu_button.click()
    menu = page.get_by_role("menu", name="Workspace navigation")
    expect(menu).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Runs", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()


def _assert_selected_run_identity(page, run_id: str) -> None:
    expect(
        page.locator(".run-detail").get_by_role("button", name=run_id, exact=True)
    ).to_be_visible()


@pytest.mark.parametrize(
    ("width", "height"),
    ((1280, 720), (390, 844)),
    ids=("desktop", "mobile-390"),
)
def test_run_progress_transport_and_browser_parity(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    _, root, _, _ = control_client
    fixture = _seed_issue35_progress_fixture(root)
    run_id = fixture["run_id"]
    missing_run_id = fixture["missing_run_id"]
    assert isinstance(run_id, str)
    assert isinstance(missing_run_id, str)

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}")
            page.get_by_role("heading", name="Repair overlay", exact=True).wait_for()
            _assert_selected_run_identity(page, run_id)

            expect(page.get_by_text("Checkpoint 4: Stage 4 (4 of 14)", exact=True)).to_be_visible()
            repairing = page.locator(".dashboard-section p").filter(has_text="Repairing").first
            expect(repairing).to_contain_text("repair-overlay.md")
            expect(page.get_by_text("Current turn: turn 4 · Implement · starting", exact=True)).to_be_visible()
            expect(page.get_by_text("Last finished turn: turn 3 · Review · completed", exact=True)).to_be_visible()
            detail_text = page.locator(".run-detail").inner_text()
            assert "? of 0" not in detail_text
            assert "All 0 checkpoints complete" not in detail_text
            _assert_shell_contract(page, width, height)
            page.screenshot(
                path=str(_screenshot_path(tmp_path, "repair", width, height)),
                full_page=True,
            )

            _close_issue35_repair_scope(fixture)
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Refresh", exact=True).click()
            expect(page.get_by_text("Checkpoint 5: Stage 5 (5 of 14)", exact=True)).to_be_visible()
            assert page.get_by_text("Repairing", exact=True).count() == 0
            detail_text = page.locator(".run-detail").inner_text()
            assert "? of 0" not in detail_text
            assert "All 0 checkpoints complete" not in detail_text
            page.screenshot(
                path=str(_screenshot_path(tmp_path, "next", width, height)),
                full_page=True,
            )

            page.goto(
                f"{url}/?project={PROJECT_ID}&view=runs&run={missing_run_id}"
            )
            page.get_by_role("heading", name="Missing evidence", exact=True).wait_for()
            _assert_selected_run_identity(page, missing_run_id)
            expect(
                page.get_by_text(
                    "Progress unavailable — the original plan is unavailable.",
                    exact=True,
                )
            ).to_be_visible()
            detail_text = page.locator(".run-detail").inner_text()
            assert "? of 0" not in detail_text
            assert "All 0 checkpoints complete" not in detail_text
            page.screenshot(
                path=str(_screenshot_path(tmp_path, "unavailable", width, height)),
                full_page=True,
            )
        finally:
            browser.close()

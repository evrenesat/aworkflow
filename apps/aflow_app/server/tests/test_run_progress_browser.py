"""Real browser parity journeys for truthful run progress."""

from __future__ import annotations

import json
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


LONG_SUMMARY_TOKEN = "commit" + "0123456789abcdef" * 12


def _screenshot_path(tmp_path: Path, state: str, width: int, height: int) -> Path:
    artifact_root = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    artifact_root.mkdir(parents=True, exist_ok=True)
    engine = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    return artifact_root / f"run-progress-{engine}-{state}-{width}x{height}.png"


def _geometry_path(tmp_path: Path, width: int, height: int) -> Path:
    artifact_root = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    artifact_root.mkdir(parents=True, exist_ok=True)
    engine = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    return artifact_root / f"run-summary-wrapping-{engine}-{width}x{height}.json"


def _summary_geometry(page) -> dict[str, object]:
    return page.evaluate(
        """() => {
            const paragraph = [...document.querySelectorAll(
                '.run-detail .dashboard-section > p'
            )].find(element => element.textContent?.startsWith('Last finished summary:'))
            if (!paragraph) throw new Error('last finished summary paragraph not found')
            const rect = paragraph.getBoundingClientRect()
            return {
                viewportWidth: innerWidth,
                documentWidth: document.documentElement.scrollWidth,
                bodyWidth: document.body.scrollWidth,
                documentHeight: document.scrollingElement?.scrollHeight ?? 0,
                viewportHeight: innerHeight,
                paragraphClientWidth: paragraph.clientWidth,
                paragraphScrollWidth: paragraph.scrollWidth,
                paragraphRight: rect.right,
                overflowing: [...document.querySelectorAll('*')]
                    .map(element => ({
                        tag: element.tagName,
                        className: String(element.className || ''),
                        text: element.textContent?.trim().slice(0, 120),
                        right: element.getBoundingClientRect().right,
                    }))
                    .filter(element => element.right > innerWidth + 1)
                    .slice(0, 8),
            }
        }"""
    )


def _disable_summary_wrapping(page) -> None:
    page.evaluate(
        """() => {
            const style = document.createElement('style')
            style.dataset.testSummaryWrapping = 'disabled'
            style.textContent = '.run-detail .dashboard-section > p { overflow-wrap: normal !important; }'
            document.head.append(style)
        }"""
    )


def _enable_summary_wrapping(page) -> None:
    page.evaluate(
        """() => document.querySelector('style[data-test-summary-wrapping="disabled"]')?.remove()"""
    )


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


@pytest.mark.parametrize(
    ("width", "height"),
    ((320, 568), (390, 844), (1280, 720)),
    ids=("phone-320", "phone-390", "desktop"),
)
def test_run_summary_wraps_without_document_overflow(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    _, root, _, _ = control_client
    fixture = _seed_issue35_progress_fixture(root)
    run_id = fixture["run_id"]
    run_dir = fixture["run_dir"]
    run_json = fixture["run_json"]
    assert isinstance(run_id, str)
    assert isinstance(run_dir, Path)
    assert isinstance(run_json, Path)
    metadata = json.loads(run_json.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    # Keep the disposable shell focused on the finalized progress prose; the
    # active fixture's unavailable-control hint is a separate narrow-screen
    # presentation case.
    metadata["status"] = "completed"
    run_json.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    finished_stdout = run_dir / "turns" / "turn-003" / "stdout.txt"
    synthetic_summary = f"review approved with exact evidence {LONG_SUMMARY_TOKEN}\n"
    finished_stdout.write_text(synthetic_summary, encoding="utf-8")

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}")
            page.get_by_role("heading", name="Repair overlay", exact=True).wait_for()
            summary = page.locator(
                ".run-detail .dashboard-section > p"
            ).filter(has_text="Last finished summary").first
            summary.wait_for()
            expect(summary).to_contain_text(LONG_SUMMARY_TOKEN)

            _disable_summary_wrapping(page)
            before = _summary_geometry(page)
            _enable_summary_wrapping(page)
            after = _summary_geometry(page)
            (geometry_path := _geometry_path(tmp_path, width, height)).write_text(
                json.dumps({"before_no_wrap": before, "after_wrap": after}, indent=2) + "\n",
                encoding="utf-8",
            )

            if width <= 390:
                assert before["documentWidth"] > width + 1, before
                assert before["paragraphScrollWidth"] > before["paragraphClientWidth"], before
            assert after["documentWidth"] <= width + 1, {"path": geometry_path, **after}
            assert after["bodyWidth"] <= width + 1, {"path": geometry_path, **after}
            assert after["paragraphRight"] <= width + 1, {"path": geometry_path, **after}
            assert after["documentHeight"] >= height, after
            assert LONG_SUMMARY_TOKEN in page.locator(".run-detail").inner_text()

            run_detail = page.locator(".run-detail")
            expect(run_detail.get_by_title("Copy run ID", exact=True)).to_be_visible()
            actions = run_detail.get_by_role("button", name="More run actions", exact=True)
            expect(actions).to_be_visible()
            actions.click()
            menu = page.get_by_role("menu", name="More run actions", exact=True)
            expect(menu).to_be_visible()
            page.keyboard.press("Escape")
            expect(menu).to_be_hidden()
        finally:
            browser.close()

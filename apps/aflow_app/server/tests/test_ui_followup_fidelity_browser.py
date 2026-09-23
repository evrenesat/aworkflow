"""Populated Runs-detail fidelity checks for the follow-up presentation pass."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from aflow.control_plane.units import InMemoryUnitManager, UnitState
from test_control_plane_api import PROJECT_ID, control_client, live_server  # noqa: F401
from test_responsive_browser import _browser, _login, _set_theme_preference
from test_ui_demo_fidelity_browser import (
    _fidelity_artifact_dir,
    _prepare_disposable_fidelity_config,
    _wait_for_fidelity_readiness,
)
from ui_demo_fidelity import (
    PRODUCTION_ANCHORS,
    capture_reference_surface,
    load_reference_manifest,
    measure_named_anchors,
    seed_demo_fidelity_fixture,
)


def _box(capture: dict[str, object], name: str) -> dict[str, float]:
    anchors = capture["anchors"]
    assert isinstance(anchors, dict)
    value = anchors[name]
    assert isinstance(value, dict)
    box = value["box"]
    assert isinstance(box, dict)
    return box


def _assert_anchor_order(capture: dict[str, object]) -> None:
    anchors = capture["anchors"]
    assert isinstance(anchors, dict)
    expected = tuple(PRODUCTION_ANCHORS)
    assert tuple(anchors) == expected
    for name in expected:
        value = anchors[name]
        assert value["present"] is True and value["visible"] is True and value["box"] is not None, name
    boxes = [_box(capture, name) for name in expected]
    assert all(left["y"] <= right["y"] + 1 for left, right in zip(boxes, boxes[1:])), boxes


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    (
        pytest.param(1280, 720, "light", id="desktop-light"),
        pytest.param(1280, 720, "dark", id="desktop-dark"),
        pytest.param(390, 844, "light", id="mobile-light"),
        pytest.param(390, 844, "dark", id="mobile-dark"),
    ),
)
def test_ui_followup_run_detail(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Compare a populated ten-checkpoint running detail with the frozen demo."""
    _, root, units, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    running = fixtures["running"]
    assert isinstance(running, dict)
    running_plan = Path(running["plan"])
    assert running_plan.read_text(encoding="utf-8").count("### [") == 10
    running_unit = f"aflow-run-{running['run_id']}.service"
    assert isinstance(units, InMemoryUnitManager)
    units.units[running_unit] = UnitState(name=running_unit, active_state="active", sub_state="running")
    _prepare_disposable_fidelity_config(root, monkeypatch)

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail("The follow-up fidelity test requires the real built web app; run the web build first.")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    artifact_dir = _fidelity_artifact_dir(tmp_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stem = f"run-detail-{theme}-{width}x{height}"
    reference_path = artifact_dir / f"{stem}-reference.png"
    app_path = artifact_dir / f"{stem}-app.png"
    expanded_path = artifact_dir / f"{stem}-expanded.png"
    requests: list[tuple[str, str]] = []
    page_errors: list[str] = []

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            # Establish the disposable app origin before touching its
            # origin-scoped preference store; the page starts at about:blank.
            page.goto(url, wait_until="domcontentloaded")
            _set_theme_preference(page, theme)
            reference = capture_reference_surface(
                page,
                width=width,
                height=height,
                theme=theme,
                screenshot_path=reference_path,
            )
            reference_anchors = reference["anchors"]
            assert tuple(reference_anchors) == ("title", "current_work", "latest_result", "recent_activity", "first_disclosure")
            assert load_reference_manifest()["demo_sha256"] == "2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4"

            _login(page, url)
            page_errors.clear()
            page.goto(
                f"{url}/?project={PROJECT_ID}&view=runs&run={running['run_id']}",
                wait_until="load",
            )
            _wait_for_fidelity_readiness(page)
            detail = page.locator(".run-detail:visible").first
            detail.wait_for()
            # WebKit can finish the pre-login app bootstrap request after
            # the session redirect; only retain runtime errors from the
            # settled, authenticated detail surface below.
            page_errors.clear()
            expect(detail.locator(".run-progress-header h3")).to_contain_text("Automatic plan consumption")
            expect(detail.locator(".run-progress-header")).to_contain_text("Running")
            expect(detail.locator("[data-ui-fidelity-anchor='current-work']")).to_be_visible()
            expect(detail.locator("[data-ui-fidelity-anchor='latest-result']")).to_be_visible()
            expect(detail.locator("[data-ui-fidelity-anchor='recent-activity']")).to_be_visible()
            expect(detail.locator("[data-ui-fidelity-anchor='first-disclosure']")).to_be_visible()

            capture = {
                "reference_sha256": load_reference_manifest()["demo_sha256"],
                "width": width,
                "height": height,
                "theme": theme,
                "run_id": running["run_id"],
                "anchors": measure_named_anchors(page, PRODUCTION_ANCHORS),
                "default_open_disclosures": page.locator(".run-progress-evidence details[open]").count(),
                "visible_event_count": detail.locator(".run-overview-event").count(),
                "screenshot": app_path.name,
            }
            _assert_anchor_order(capture)
            latest = _box(capture, "latest_result")
            first_disclosure = _box(capture, "first_disclosure")
            if width >= 960:
                assert latest["y"] <= 350, latest
                assert first_disclosure["y"] <= 760, first_disclosure
            else:
                assert latest["y"] <= 475, latest
                assert first_disclosure["y"] <= 1050, first_disclosure
            assert capture["default_open_disclosures"] == 0
            assert capture["visible_event_count"] >= 3
            assert detail.locator(".run-overview-event-time").count() >= 3
            assert detail.locator(".run-overview-event-action").count() >= 3
            assert detail.locator(".run-compatibility-evidence").count() == 0
            assert detail.get_by_text(re.compile(r"No finished result has been recorded", re.I)).count() == 0
            expect(detail.locator("#checkpoint-history-delivery-evidence[open]")).to_have_count(0)
            assert detail.get_by_text("Adjust run", exact=True).count() == 0
            page.screenshot(path=str(app_path), full_page=True)

            # The selected current checkpoint and the full delivery receipt stay
            # reachable through named information disclosures.
            first_disclosure_locator = detail.locator("details[data-ui-fidelity-anchor='first-disclosure']")
            first_disclosure_locator.locator(":scope > summary").click()
            expect(first_disclosure_locator).to_have_attribute("open", "")
            expect(detail.locator(".checkpoint-history-navigation [aria-current='true']")).to_be_visible()
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menu", name="More run page actions").get_by_role("menuitem", name="Refresh", exact=True).click()
            expect(first_disclosure_locator).to_have_attribute("open", "")
            expect(detail.locator(".checkpoint-history-navigation [aria-current='true']")).to_be_visible()

            expand_start = len(requests)
            detail.get_by_role("button", name="Expand all", exact=True).click()
            expect(detail.locator("details.checkpoint-history-checkpoints[open]")).to_have_count(1)
            expect(detail.locator("details#checkpoint-history-delivery-evidence[open]")).to_have_count(1)
            expect(detail.get_by_text("Count definitions & evidence", exact=True)).to_be_visible()
            expect(detail.get_by_text("Final review", exact=True)).to_be_visible()
            expect(detail.locator(".checkpoint-history-navigation [aria-current='true']")).to_be_visible()
            writes_after = [
                {"method": method, "url": url}
                for method, url in requests[expand_start:]
                if method not in {"GET", "HEAD", "OPTIONS"}
                and not url.endswith("/api/config/form")  # pure config projection, not a write
            ]
            assert writes_after == []

            page.screenshot(path=str(expanded_path), full_page=True)
            (artifact_dir / f"{stem}.json").write_text(
                json.dumps(
                    {
                        "capture": capture,
                        "page_errors": page_errors,
                        "writes": writes_after,
                        "reference_screenshot": reference_path.name,
                        "app_screenshot": app_path.name,
                        "expanded_screenshot": expanded_path.name,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        finally:
            browser.close()

    assert page_errors == []

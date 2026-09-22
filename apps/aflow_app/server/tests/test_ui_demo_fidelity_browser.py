"""Frozen reference and production capture harness smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from aflow.control_plane.units import InMemoryUnitManager, UnitState
from test_control_plane_api import PROJECT_ID, control_client, live_server  # noqa: F401
from test_responsive_browser import _browser, _login, _set_theme_preference
from ui_demo_fidelity import (
    CAPTURE_THEMES,
    CAPTURE_VIEWPORTS,
    PRODUCTION_ANCHORS,
    PRODUCTION_PROGRESS_SURFACE,
    capture_dom_identity,
    capture_reference_surface,
    install_mutation_observer,
    load_reference_manifest,
    measure_named_anchors,
    read_mutation_records,
    seed_demo_fidelity_fixture,
)


def _assert_reference_capture(capture: dict[str, object]) -> None:
    assert capture["page_errors"] == []
    anchors = capture["anchors"]
    assert isinstance(anchors, dict)
    expected = ("title", "current_work", "latest_result", "recent_activity", "first_disclosure")
    assert tuple(anchors) == expected
    missing = [
        name
        for name in expected
        if not anchors[name]["present"] or not anchors[name]["visible"] or anchors[name]["box"] is None
    ]
    assert not missing, {name: anchors[name] for name in missing}
    boxes = [anchors[name]["box"] for name in expected]
    assert all(boxes[index]["y"] <= boxes[index + 1]["y"] for index in range(len(boxes) - 1))


def _assert_production_capture(page, capture: dict[str, object]) -> None:
    assert capture["page_errors"] == []
    assert capture["legacy_warning"] is False
    anchors = capture["anchors"]
    assert isinstance(anchors, dict)
    expected = tuple(PRODUCTION_ANCHORS)
    assert tuple(anchors) == expected
    missing = [
        name
        for name in expected
        if not anchors[name]["present"] or not anchors[name]["visible"] or anchors[name]["box"] is None
    ]
    assert not missing, {name: anchors[name] for name in missing}
    boxes = [anchors[name]["box"] for name in expected]
    assert all(boxes[index]["y"] <= boxes[index + 1]["y"] for index in range(len(boxes) - 1))

    progress_surface = capture["progress_surface"]
    assert progress_surface["present"] is True
    assert progress_surface["visible"] is True
    assert progress_surface["box"] is not None
    latest_box = anchors["latest_result"]["box"]
    current_box = anchors["current_work"]["box"]
    recent_box = anchors["recent_activity"]["box"]
    progress_box = progress_surface["box"]
    assert latest_box["height"] < progress_box["height"], {
        "latest_result": latest_box,
        "progress_surface": progress_box,
    }
    # The frozen reference measures Current work at about 67px desktop and
    # 92px Chromium / 110px WebKit mobile; Recent activity is about 147px /
    # 168px. Keep a small allowance for real five-event content while
    # preventing the rejected padded layout.
    if capture["width"] >= 960:
        assert current_box["height"] <= 76, current_box
        assert latest_box["height"] <= 104, latest_box
        assert recent_box["height"] <= 170, recent_box
    else:
        assert current_box["height"] <= 112, current_box
        assert latest_box["height"] <= 120, latest_box
        assert recent_box["height"] <= 200, recent_box
    assert page.evaluate(
        """selectors => {
          const nodes = selectors.map(selector => document.querySelector(selector));
          return nodes.every(Boolean) && new Set(nodes).size === nodes.length;
        }""",
        [*PRODUCTION_ANCHORS.values(), PRODUCTION_PROGRESS_SURFACE],
    ) is True


def _visible_run_row_boxes(page) -> list[dict[str, float]]:
    return page.evaluate(
        """() => [...document.querySelectorAll('.run-list-item[data-run-row="true"]')]
          .filter(element => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
          })
          .map(element => {
            const box = element.getBoundingClientRect();
            return {x: box.x, y: box.y, width: box.width, height: box.height};
          })"""
    )


def _assert_compact_run_rows(page, *, width: int, height: int, fixture_count: int) -> dict[str, object]:
    if width < 960 or height < 600:
        return {"skipped": True}
    rows = page.locator(".run-list-item[data-run-row='true']:visible")
    expect(rows).to_have_count(fixture_count)
    before = _visible_run_row_boxes(page)
    assert len(before) == fixture_count
    assert all(56 <= row["height"] <= 72 for row in before), before
    assert all(
        before[index]["y"] + before[index]["height"] <= before[index + 1]["y"] + 1
        for index in range(len(before) - 1)
    ), before
    assert page.locator(".run-list-item[data-run-row='true'] button button").count() == 0

    rows.first.hover()
    page.wait_for_timeout(350)
    preview = page.locator(".run-row-preview:visible").first
    expect(preview).to_be_visible()
    preview_box = preview.bounding_box()
    assert preview_box is not None
    assert 0 <= preview_box["x"]
    assert preview_box["x"] + preview_box["width"] <= width
    assert 0 <= preview_box["y"]
    assert preview_box["y"] + preview_box["height"] <= height
    after = _visible_run_row_boxes(page)
    assert after == before, {"before": before, "after": after}

    rows.first.locator(".run-list-select").focus()
    expect(page.locator(".run-row-preview:visible").first).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".run-row-preview:visible")).to_have_count(0)
    return {
        "skipped": False,
        "row_boxes": before,
        "hover_preview_box": preview_box,
        "sibling_boxes_stable": after == before,
        "focus_preview_opened": True,
        "escape_closed_preview": True,
    }


def _assert_mobile_run_preview(page, *, width: int, height: int, fixture_count: int) -> dict[str, object]:
    if width >= 960:
        return {"skipped": True}
    back = page.locator(".sidebar-editor-back:visible").first
    expect(back).to_be_visible()
    back.click()
    rows = page.locator(".run-list-item[data-run-row='true']:visible")
    expect(rows).to_have_count(fixture_count)
    before = _visible_run_row_boxes(page)
    toggle = rows.first.locator(".run-row-preview-toggle")
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    preview = page.locator(".run-row-preview:visible").first
    expect(preview).to_be_visible()
    preview_box = preview.bounding_box()
    assert preview_box is not None
    assert 0 <= preview_box["x"]
    assert preview_box["x"] + preview_box["width"] <= width
    assert 0 <= preview_box["y"]
    assert preview_box["y"] + preview_box["height"] <= height
    after_preview = _visible_run_row_boxes(page)
    assert after_preview == before, {"before": before, "after": after_preview}
    page.keyboard.press("Escape")
    expect(page.locator(".run-row-preview:visible")).to_have_count(0)
    return {
        "skipped": False,
        "row_boxes": before,
        "touch_preview_box": preview_box,
        "sibling_boxes_stable": _visible_run_row_boxes(page) == before,
        "explicit_toggle_opened": True,
        "escape_closed_preview": True,
    }


def _write_artifact_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("AFLOW_UI_DEMO_FIDELITY_ARTIFACT", path)


def test_ui_demo_reference_renders_with_named_anchors(tmp_path: Path) -> None:
    """The frozen fragment opens in a real browser before product redesign work."""
    manifest = load_reference_manifest()
    captures: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page()
        try:
            for width, height in CAPTURE_VIEWPORTS:
                for theme in CAPTURE_THEMES:
                    capture = capture_reference_surface(
                        page,
                        width=width,
                        height=height,
                        theme=theme,
                        screenshot_path=tmp_path / f"reference-{theme}-{width}x{height}.png",
                    )
                    _assert_reference_capture(capture)
                    captures.append(capture)

            install_mutation_observer(page, "#af-frame")
            baseline = capture_dom_identity(page, "#af-run-name")
            assert baseline["present"] is True
            assert read_mutation_records(page) == []
        finally:
            browser.close()
    _write_artifact_manifest(
        tmp_path / "reference-captures.json",
        {
            "reference_sha256": manifest["demo_sha256"],
            "captures": captures,
            "disposable": True,
        },
    )


def test_ui_demo_fixture_captures_authenticated_built_app(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Capture the real built app against disposable running demo-shaped data."""
    _, root, units, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    assert isinstance(units, InMemoryUnitManager)
    running = fixtures["running"]
    assert isinstance(running, dict)
    running_unit = f"aflow-run-{running['run_id']}.service"
    units.units[running_unit] = UnitState(
        name=running_unit,
        active_state="active",
        sub_state="running",
    )
    for fixture in fixtures.values():
        assert isinstance(fixture, dict)
        manifest_path = Path(fixture["launch_manifest"])
        launch_state_path = Path(fixture["launch_state"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        launch_state = json.loads(launch_state_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == fixture["run_id"]
        assert manifest["project_root"] == str(Path(root).resolve())
        assert manifest["plan_path"] == str(Path(fixture["plan"]).resolve())
        assert manifest["workflow_name"] == "managed"
        assert manifest["max_turns"] >= 1
        assert launch_state["run_id"] == fixture["run_id"]
        assert launch_state["phase"] == fixture["launch_phase"]
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail(
            "The fidelity harness requires the real built web app; run "
            "npm --prefix apps/aflow_app/web run build first."
        )
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    captures: list[dict[str, object]] = []
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            _login(page, url)
            for fixture_name, fixture in fixtures.items():
                assert isinstance(fixture, dict)
                run_id = fixture["run_id"]
                expected_status = str(fixture["status"]).replace("_", " ").title()
                plan_name = Path(fixture["plan"]).name
                for width, height in CAPTURE_VIEWPORTS:
                    for theme in CAPTURE_THEMES:
                        page.set_viewport_size({"width": width, "height": height})
                        page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
                        _set_theme_preference(page, theme)
                        page.goto(
                            f"{url}/?project={PROJECT_ID}&view=runs&run={run_id}",
                            wait_until="load",
                        )
                        detail = page.locator(".run-detail:visible").first
                        detail.wait_for()
                        expect(detail).to_contain_text(plan_name)
                        expect(detail.locator(".run-progress-header")).to_contain_text(expected_status)
                        if width >= 960 and height >= 600:
                            for variant in fixtures.values():
                                row = page.locator(
                                    f".run-list-item[data-run-key='{variant['run_id']}']:visible"
                                )
                                expect(row).to_be_visible()
                        diagnostics = detail.locator("button[aria-label='Diagnostics']")
                        expect(diagnostics).to_be_visible()
                        expect(detail.locator(PRODUCTION_ANCHORS["recent_activity"])).to_be_visible()
                        expect(detail.locator(PRODUCTION_ANCHORS["first_disclosure"])).to_be_visible()
                        detail_text = detail.inner_text()
                        legacy_warning = (
                            "Legacy execution record." in detail_text
                            or "legacy run has no control-plane launch manifest" in detail_text
                        )
                        assert legacy_warning is False
                        error_count_before_capture = len(page_errors)
                        capture_path = tmp_path / f"production-{fixture_name}-{theme}-{width}x{height}.png"
                        app_shell = page.locator(".app-shell").first
                        app_shell.screenshot(path=str(capture_path))
                        capture = {
                            "fixture_variant": fixture_name,
                            "run_id": run_id,
                            "status": fixture["status"],
                            "width": width,
                            "height": height,
                            "theme": theme,
                            "surface_selector": ".app-shell",
                            "capture_state": {"diagnostics": "closed", "raw_details": "closed"},
                            "page_errors": page_errors[error_count_before_capture:],
                            "legacy_warning": legacy_warning,
                            "anchors": measure_named_anchors(page, PRODUCTION_ANCHORS),
                            "progress_surface": measure_named_anchors(
                                page,
                                {"progress_surface": PRODUCTION_PROGRESS_SURFACE},
                            )["progress_surface"],
                            "screenshot": capture_path.name,
                        }
                        _assert_production_capture(page, capture)
                        diagnostics.click()
                        expect(detail.locator(".run-technical-details")).to_contain_text("control_plane")
                        desktop_rows = _assert_compact_run_rows(
                            page,
                            width=width,
                            height=height,
                            fixture_count=len(fixtures),
                        )
                        mobile_rows = _assert_mobile_run_preview(
                            page,
                            width=width,
                            height=height,
                            fixture_count=len(fixtures),
                        )
                        capture["row_comparison"] = {
                            "desktop": desktop_rows,
                            "mobile": mobile_rows,
                        }
                        captures.append(capture)
            assert page_errors == []
        finally:
            browser.close()
    _write_artifact_manifest(
        tmp_path / "production-captures.json",
        {
            "reference_sha256": load_reference_manifest()["demo_sha256"],
            "production_anchors": PRODUCTION_ANCHORS,
            "fixture_variants": fixtures,
            "captures": captures,
            "disposable": True,
        },
    )

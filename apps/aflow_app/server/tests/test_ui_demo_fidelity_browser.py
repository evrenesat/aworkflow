"""Frozen reference and production capture harness smoke tests."""

from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from aflow.control_plane.units import InMemoryUnitManager, UnitState
from test_control_plane_api import PROJECT_ID, control_client, live_server  # noqa: F401
from test_responsive_browser import _assert_header_and_flow, _assert_theme, _browser, _login, _set_theme_preference
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


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    (
        pytest.param(1280, 720, "light", id="desktop-light"),
        pytest.param(390, 844, "dark", id="mobile-dark"),
    ),
)
def test_ui_demo_project_and_global_overview_captures(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Capture populated project and All runs surfaces at reference breakpoints."""
    _, root, _, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail("The CP7 capture requires the real built web app; run the web build first.")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    captures: dict[str, object] = {
        "reference_sha256": load_reference_manifest()["demo_sha256"],
        "width": width,
        "height": height,
        "theme": theme,
        "screenshots": {},
    }
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)

            page.goto(f"{url}/?view=projects", wait_until="load")
            _assert_theme(page, theme)
            project_row = page.locator("ul[aria-label='Added projects'] > li").first
            project_row.wait_for()
            expect(project_row.locator(".project-row-facts")).to_contain_text("Git root")
            discovery = page.locator("details.project-discovery-disclosure")
            discovery.wait_for()
            assert discovery.get_attribute("open") is None
            expect(discovery.locator("summary")).to_contain_text("Find projects on this server")
            _assert_header_and_flow(page)
            project_image = tmp_path / f"cp7-projects-{theme}-{width}x{height}.png"
            page.screenshot(path=str(project_image), full_page=True)
            captures["projects"] = {
                "row": page.evaluate(
                    """element => {
                      const box = element.getBoundingClientRect();
                      return {x: box.x, y: box.y, width: box.width, height: box.height};
                    }""",
                    project_row.element_handle(),
                ),
                "worktrees_open": page.locator("details.worktree-disclosure[open]").count(),
                "discovery_open": page.locator("details.project-discovery-disclosure[open]").count(),
                "screenshot": project_image.name,
            }

            page.goto(f"{url}/?view=all-runs", wait_until="load")
            page.get_by_role("heading", name="All runs", exact=True).wait_for()
            page.get_by_role("heading", name=re.compile(r"Ongoing \(\d+\)")).wait_for()
            expect(page.locator(".global-run-results h3")).to_have_count(3)
            expect(page.get_by_role("heading", name=re.compile(r"Recent \(\d+\)"))).to_be_visible()
            expect(page.get_by_text(re.compile(r"No (?:ongoing|recent|runs need attention)"))).to_have_count(0)
            expect(page.locator(".global-run-row")).to_have_count(3)
            expect(page.locator(".global-run-row[data-enrichment-state='settled']")).to_have_count(3)
            expect(page.get_by_text("Loading checkpoint progress…", exact=True)).to_have_count(0)
            _assert_header_and_flow(page)
            runs_image = tmp_path / f"cp7-all-runs-{theme}-{width}x{height}.png"
            page.screenshot(path=str(runs_image), full_page=True)
            captures["all_runs"] = {
                "groups": page.locator(".global-run-results h3").all_text_contents(),
                "rows": page.locator(".global-run-row:visible").count(),
                "enrichment_states": page.locator(".global-run-row").evaluate_all(
                    "rows => rows.map(row => row.getAttribute('data-enrichment-state'))"
                ),
                "row_text": page.locator(".global-run-row").all_text_contents(),
                "screenshot": runs_image.name,
            }
        finally:
            page.close()
            browser.close()

    _write_artifact_manifest(tmp_path / f"cp7-overview-{theme}-{width}x{height}.json", captures)


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    (
        pytest.param(1280, 720, "light", id="desktop-light"),
        pytest.param(1280, 720, "dark", id="desktop-dark"),
        pytest.param(390, 844, "light", id="mobile-light"),
        pytest.param(390, 844, "dark", id="mobile-dark"),
    ),
)
def test_ui_demo_cp8_plan_editor_and_review_captures(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Capture plan lifecycle, editor, launch draft, and read-only review states."""
    _, root, units, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    running = fixtures["running"]
    assert isinstance(running, dict)
    manifest = load_reference_manifest()
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail("The CP8 capture requires the real built web app; run the web build first.")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    plan_name = Path(running["plan"]).name
    plan_path = Path(running["plan"]).relative_to(root).as_posix()
    reference_path = tmp_path / f"cp8-reference-{theme}-{width}x{height}.png"
    captures: dict[str, object] = {
        "reference_sha256": manifest["demo_sha256"],
        "viewport": {"width": width, "height": height},
        "theme": theme,
        "screenshots": {},
        "states": {},
    }

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            _login(page, url)
            reference = capture_reference_surface(
                page,
                width=width,
                height=height,
                theme=theme,
                screenshot_path=reference_path,
            )
            _assert_reference_capture(reference)
            captures["reference_anchor_order"] = list(reference["anchors"])
            captures["screenshots"]["reference"] = reference_path.name

            page.goto(f"{url}/?project={PROJECT_ID}&view=plans", wait_until="load")
            plan_list = page.locator(".plan-list")
            plan_list.wait_for()
            _assert_theme(page, theme)
            _assert_header_and_flow(page)
            lifecycle_headings = page.locator(".plan-list > section h3").all_text_contents()
            assert lifecycle_headings == ["Draft", "Ready", "Done"]
            assert page.locator(".plan-list > section").count() == 3
            plan_screenshot = tmp_path / f"cp8-plans-{theme}-{width}x{height}.png"
            page.screenshot(path=str(plan_screenshot), full_page=True)
            captures["screenshots"]["plans"] = plan_screenshot.name
            captures["states"]["plans"] = {
                "lifecycle_headings": lifecycle_headings,
                "ready_rows": page.locator(".plan-list > section").nth(1).locator("button.content-button").count(),
                "done_rows": page.locator(".plan-list > section").nth(2).locator("button.content-button").count(),
                "anchors": measure_named_anchors(page, {
                    "plan_list": ".plan-list",
                    "draft": ".plan-list > section:nth-of-type(1)",
                    "ready": ".plan-list > section:nth-of-type(2)",
                    "done": ".plan-list > section:nth-of-type(3)",
                }),
            }

            ready_row = page.locator(".plan-list > section").nth(1).locator("button.content-button").filter(has_text=plan_name).first
            expect(ready_row).to_be_visible()
            ready_row.click()
            page.get_by_label("Plan content", exact=True).wait_for()
            expect(page.locator(".plan-save-state:visible")).to_have_text(re.compile(r"^Saved$"))
            metadata = page.locator("details.plan-metadata")
            history = page.locator("details.plan-backup-history")
            assert metadata.get_attribute("open") is None
            assert history.get_attribute("open") is None
            editor_screenshot = tmp_path / f"cp8-editor-{theme}-{width}x{height}.png"
            page.screenshot(path=str(editor_screenshot), full_page=True)
            captures["screenshots"]["editor"] = editor_screenshot.name
            captures["states"]["editor"] = {
                "plan_path": plan_path,
                "save_state": page.locator(".plan-save-state:visible").inner_text(),
                "metadata_open": metadata.get_attribute("open") is not None,
                "history_open": history.get_attribute("open") is not None,
                "anchors": measure_named_anchors(page, {
                    "editor": ".plan-editor",
                    "content": "[aria-label='Plan content']",
                    "metadata": "details.plan-metadata",
                    "history": "details.plan-backup-history",
                }),
            }

            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Configure run…", exact=True).click()
            page.get_by_label("Run plan", exact=True).wait_for()
            expect(page.get_by_label("Run plan", exact=True)).to_have_value(plan_path)
            expect(page.get_by_label("Run workflow", exact=True)).to_be_visible()
            expect(page.get_by_label("Run team", exact=True)).to_be_visible()
            expect(page.get_by_label("Run max turns", exact=True)).to_be_visible()
            expect(page.get_by_text("Server default: 15.", exact=True)).to_be_visible()
            expect(page.get_by_label("Effective choices for this launch", exact=True)).to_contain_text("15 — server default")
            advanced = page.get_by_role("button", name="Advanced options", exact=True)
            expect(advanced).to_have_attribute("aria-expanded", "false")
            preflight = page.locator(".worktree-preflight")
            preflight.wait_for()
            page.wait_for_function(
                "() => document.querySelector('.worktree-preflight')?.dataset.preflightStatus === 'ready'"
            )
            expect(preflight).to_have_attribute("data-preflight-status", "ready")
            confirmation = page.get_by_role(
                "checkbox",
                name="Continue despite uncommitted changes",
                exact=True,
            )
            if confirmation.count():
                confirmation.check()
                page.wait_for_function(
                    "() => document.querySelector('.worktree-preflight')?.dataset.preflightStatus === 'ready'"
                )
                expect(preflight).to_have_attribute("data-preflight-status", "ready")
            review_button = page.get_by_role("button", name="Review start…", exact=True)
            expect(review_button).to_be_enabled()
            expect(page.get_by_role("button", name="Start run", exact=True)).to_have_count(0)
            preflight_status = preflight.get_attribute("data-preflight-status")
            assert preflight_status == "ready", preflight_status
            new_run_screenshot = tmp_path / f"cp8-new-run-{theme}-{width}x{height}.png"
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(new_run_screenshot), full_page=True)
            captures["screenshots"]["new_run"] = new_run_screenshot.name
            captures["states"]["new_run"] = {
                "plan": page.get_by_label("Run plan", exact=True).input_value(),
                "workflow": page.get_by_label("Run workflow", exact=True).input_value(),
                "team": page.get_by_label("Run team", exact=True).input_value(),
                "max_turns": page.get_by_label("Run max turns", exact=True).input_value(),
                "advanced_open": advanced.get_attribute("aria-expanded") == "true",
                "preflight_status": preflight_status,
                "start_calls_before_review": len(units.start_calls),
                "anchors": measure_named_anchors(page, {
                    "new_run": ".start-run-form.card",
                    "plan": "[aria-label='Run plan']",
                    "preflight": ".worktree-preflight",
                    "review_action": "button:has-text('Review start…')",
                }),
            }

            review_button.click()
            review_region = page.get_by_role("region", name="Review start", exact=True)
            review_region.wait_for()
            expect(review_region).to_contain_text("Read-only check — no run has been allocated.")
            expect(review_region).to_contain_text("Ready for the final start action.")
            expect(review_region).to_contain_text(plan_path)
            expect(review_region).to_contain_text("15 · server default")
            final_start = page.get_by_role("button", name="Start run", exact=True)
            expect(final_start).to_be_enabled()
            expect(page.get_by_role("button", name="Review start…", exact=True)).to_have_count(0)
            review_screenshot = tmp_path / f"cp8-review-{theme}-{width}x{height}.png"
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(review_screenshot), full_page=True)
            captures["screenshots"]["review"] = review_screenshot.name
            captures["states"]["review"] = {
                "read_only_notice": "Read-only check — no run has been allocated.",
                "resolved_max_turns": "15 · server default",
                "final_action_enabled": not final_start.is_disabled(),
                "start_calls_before_cancel": len(units.start_calls),
                "anchors": measure_named_anchors(page, {
                    "review": "[aria-label='Review start']",
                    "summary": ".launch-review-summary",
                    "requirements": ".launch-review-requirements",
                    "final_action": "button:has-text('Start run')",
                }),
            }
            review_region.get_by_role("button", name="Cancel review", exact=True).click()
            expect(page.get_by_role("region", name="Review start", exact=True)).to_have_count(0)
            assert len(units.start_calls) == 0
        finally:
            browser.close()

    _write_artifact_manifest(
        tmp_path / f"cp8-plan-launch-{theme}-{width}x{height}.json",
        {
            **captures,
            "disposable": True,
            "comparison": "Each production state records the matching frozen reference checksum, viewport/theme, named anchor boxes, text values, disclosure state, screenshot names, and zero-start review evidence.",
        },
    )


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    (
        pytest.param(1280, 720, "light", id="desktop-light"),
        pytest.param(390, 844, "dark", id="mobile-dark"),
    ),
)
def test_ui_demo_equal_refresh_retains_detail_and_changed_rows_update(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Hold equal data in the real app, then prove a changed row updates locally."""
    _, root, _, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    selected = fixtures["paused"]
    changed_fixture = fixtures["completed"]
    assert isinstance(selected, dict)
    assert isinstance(changed_fixture, dict)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail("The CP6 refresh capture requires the real built web app; run the web build first.")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    base_path = f"/api/control-plane/projects/{PROJECT_ID}/runs"
    held_routes = []
    request_count = {"value": 0}
    initial_body: bytes | None = None
    changed_body: bytes | None = None

    def intercept_runs(route) -> None:
        nonlocal initial_body, changed_body
        request = route.request
        if request.method != "GET" or urlsplit(request.url).path != base_path:
            route.continue_()
            return
        request_count["value"] += 1
        if request_count["value"] == 1:
            response = route.fetch()
            initial_body = response.body()
            payload = json.loads(initial_body)
            changed = json.loads(initial_body)
            changed_run = next(run for run in changed["runs"] if run["run_id"] == changed_fixture["run_id"])
            changed_run["status"] = "failed"
            changed_run["activity"] = "inactive"
            changed_run["status_reason_code"] = "worker_failure"
            changed_run["revision"] = int(changed_run.get("revision", 0)) + 1
            changed_body = json.dumps(changed).encode()
            assert payload["runs"]
            route.fulfill(status=response.status, headers=response.headers, body=initial_body)
        elif request_count["value"] == 2:
            held_routes.append(route)
        elif request_count["value"] == 3:
            assert changed_body is not None
            route.fulfill(status=200, content_type="application/json", body=changed_body)
        else:
            # Keep any follow-up stream/poll refreshes on the changed
            # snapshot; an uncontrolled server response would otherwise
            # race the assertion with the original completed fixture.
            assert changed_body is not None
            route.fulfill(status=200, content_type="application/json", body=changed_body)

    captures: dict[str, object] = {
        "viewport": {"width": width, "height": height},
        "theme": theme,
        "run_id": selected["run_id"],
        "changed_row_id": changed_fixture["run_id"],
    }
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.route(f"**{base_path}**", intercept_runs)
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            page.goto(
                f"{url}/?project={PROJECT_ID}&view=runs&run={selected['run_id']}",
                wait_until="load",
            )
            detail = page.locator(".run-detail:visible").first
            detail.wait_for()
            first_disclosure = detail.locator("details[data-ui-fidelity-anchor='first-disclosure']").first
            first_disclosure.locator("summary").click()
            expect(first_disclosure).to_have_attribute("open", "")
            page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight - window.innerHeight, 180))")

            actions = detail.get_by_role("button", name="Actions", exact=True)
            actions.click()
            expect(page.get_by_role("menu").last).to_be_visible()
            actions.focus()
            page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight - window.innerHeight, 180))")
            # Let the selected paused fixture finish its initial progress and
            # diagnostics reads before observing the equal refresh. WebKit
            # resolves those independent reads later than Chromium.
            page.wait_for_timeout(2_500)
            expect(page.get_by_role("menu").last).to_be_visible()
            actions.focus()
            page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight - window.innerHeight, 180))")
            page.wait_for_timeout(50)
            before_detail = capture_dom_identity(page, ".run-detail")
            before_disclosure = capture_dom_identity(page, "details[data-ui-fidelity-anchor='first-disclosure']")
            before_box = first_disclosure.bounding_box()
            assert before_box is not None
            page.locator(".app-shell").screenshot(path=str(tmp_path / f"refresh-before-{theme}-{width}x{height}.png"))
            refresh_baseline_disclosure = capture_dom_identity(page, "details[data-ui-fidelity-anchor='first-disclosure']")
            refresh_baseline_box = first_disclosure.bounding_box()
            assert refresh_baseline_box is not None
            # Observe the opened, stable disclosure subtree. The surrounding
            # detail also contains asynchronous event/progress evidence that
            # may settle independently of this held history response; the
            # detail/disclosure identity, text, geometry, focus and scroll
            # assertions below still cover the outer surface.
            install_mutation_observer(page, "details[data-ui-fidelity-anchor='first-disclosure']")
            page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            read_mutation_records(page)
            for _ in range(100):
                if held_routes:
                    break
                page.wait_for_timeout(25)
            assert held_routes, "the equal refresh request was not held"
            page.wait_for_timeout(250)
            held_detail = capture_dom_identity(page, ".run-detail")
            held_disclosure = capture_dom_identity(page, "details[data-ui-fidelity-anchor='first-disclosure']")
            held_box = first_disclosure.bounding_box()
            assert held_box is not None
            assert held_detail["identity"] == before_detail["identity"]
            assert held_disclosure["identity"] == before_disclosure["identity"]
            assert held_disclosure["text"] == before_disclosure["text"]
            assert held_box == refresh_baseline_box
            assert held_disclosure["scrollY"] == refresh_baseline_disclosure["scrollY"]
            assert page.get_by_role("status").filter(has_text="Refreshing runs").count() == 0
            assert page.evaluate("document.activeElement?.textContent?.trim() === 'Actions'") is True
            captures["held_mutations"] = read_mutation_records(page)
            assert captures["held_mutations"] == []

            page.keyboard.press("Escape")
            read_mutation_records(page)
            page.wait_for_timeout(50)
            closed_detail = capture_dom_identity(page, ".run-detail")
            closed_disclosure = capture_dom_identity(page, "details[data-ui-fidelity-anchor='first-disclosure']")
            closed_box = first_disclosure.bounding_box()
            assert closed_box is not None
            assert initial_body is not None
            held_routes.pop(0).fulfill(status=200, content_type="application/json", body=initial_body)
            page.wait_for_timeout(250)
            after_equal = capture_dom_identity(page, ".run-detail")
            after_disclosure = capture_dom_identity(page, "details[data-ui-fidelity-anchor='first-disclosure']")
            after_box = first_disclosure.bounding_box()
            assert after_box is not None
            assert after_equal["identity"] == closed_detail["identity"]
            assert after_disclosure["identity"] == closed_disclosure["identity"]
            assert after_disclosure["text"] == closed_disclosure["text"]
            assert after_box == closed_box
            page.locator(".app-shell").screenshot(path=str(tmp_path / f"refresh-after-equal-{theme}-{width}x{height}.png"))

            for _ in range(5):
                page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
                for _ in range(40):
                    if request_count["value"] >= 3:
                        break
                    page.wait_for_timeout(50)
                if request_count["value"] >= 3:
                    break
            assert request_count["value"] >= 3, "the changed refresh request was not issued"
            if width < 960:
                page.get_by_role("button", name="← Back to Run history", exact=True).click()
            changed_row = page.locator(
                f".run-list-item[data-run-key='{changed_fixture['run_id']}']:visible"
            )
            expect(changed_row).to_be_visible()
            expect(changed_row).to_contain_text("Failed")
            captures["changed_row_text"] = changed_row.inner_text()
            captures["after_equal"] = {
                "detail": after_equal,
                "disclosure": after_disclosure,
                "box": after_box,
            }
            captures["screenshots"] = [
                f"refresh-before-{theme}-{width}x{height}.png",
                f"refresh-after-equal-{theme}-{width}x{height}.png",
            ]
        finally:
            for route in held_routes:
                try:
                    if initial_body is not None:
                        route.fulfill(status=200, content_type="application/json", body=initial_body)
                except Exception:
                    pass
            browser.close()
    _write_artifact_manifest(tmp_path / f"refresh-comparison-{theme}-{width}x{height}.json", captures)

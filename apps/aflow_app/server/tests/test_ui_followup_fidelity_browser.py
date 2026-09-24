"""Populated Runs-detail fidelity checks for the follow-up presentation pass."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from aflow.control_plane.units import InMemoryUnitManager, UnitState
from test_control_plane_api import (
    PROJECT_ID,
    _commit_fixture_repository,
    control_client,
    live_server,
)  # noqa: F401
from test_responsive_browser import (
    _assert_global_run_row,
    _assert_no_horizontal_overflow,
    _assert_theme,
    _browser,
    _login,
    _register_responsive_worktree,
    _set_theme_preference,
)
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


RUN_ROW_VIEWPORTS = (
    (320, 568),
    (390, 844),
    (768, 1024),
    (844, 390),
    (1280, 720),
    (1440, 900),
    (390, 420),
)
RUN_ROW_THEMES = ("light", "dark")
RUN_ROW_PARENT_LABEL = "Suno Live Personas"
RUN_ROW_WORKTREE_LABEL = "Suno checkpoint review-frequency arm"
RUN_ROW_PROJECT_LABEL = f"{RUN_ROW_PARENT_LABEL} · Worktree: {RUN_ROW_WORKTREE_LABEL}"
RUN_ROW_LONG_TITLE = "Automatic plan consumption with a very long repair policy review title"
RUN_ROW_DATED_TITLE = "Readable run history"


def _run_row_boxes(page) -> list[dict[str, float]]:
    return page.evaluate(
        """() => [...document.querySelectorAll('.global-run-row')]
          .filter(element => {
            const style = getComputedStyle(element)
            const box = element.getBoundingClientRect()
            return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0
          })
          .map(element => {
            const box = element.getBoundingClientRect()
            return {x: box.x, y: box.y, width: box.width, height: box.height}
          })"""
    )


def _wait_for_render_settle(page) -> None:
    page.evaluate(
        """() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )


def _rewrite_run_metadata(path: Path, **updates: object) -> None:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.update(updates)
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def test_ui_followup_run_rows(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise populated rows against the deployed overflow shape and full viewport matrix."""
    _, root, units, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    running = fixtures["running"]
    paused = fixtures["paused"]
    completed = fixtures["completed"]
    assert isinstance(running, dict) and isinstance(paused, dict) and isinstance(completed, dict)
    running_id = str(running["run_id"])
    paused_id = str(paused["run_id"])
    completed_id = str(completed["run_id"])
    running_plan = Path(running["plan"])
    long_plan = running_plan.with_name("automatic-plan-consumption-with-a-very-long-repair-policy-review-title.md")
    plan_lines = running_plan.read_text(encoding="utf-8").splitlines()
    plan_lines[0] = f"# {RUN_ROW_LONG_TITLE}"
    long_plan.write_text("\n".join(plan_lines) + "\n", encoding="utf-8")
    running_metadata_path = root / ".aflow" / "runs" / running_id / "run.json"
    _rewrite_run_metadata(
        running_metadata_path,
        original_plan_display_name=RUN_ROW_LONG_TITLE,
        original_plan_path=str(long_plan.resolve()),
        active_plan_path=str(long_plan.resolve()),
        plan_path=str(long_plan.resolve()),
        progress_history_complete=False,
    )
    running_metadata = json.loads(running_metadata_path.read_text(encoding="utf-8"))
    scope = running_metadata.get("active_implementation_scope")
    if isinstance(scope, dict):
        scope["original_plan_path"] = str(long_plan.resolve())
        running_metadata_path.write_text(json.dumps(running_metadata, indent=2) + "\n", encoding="utf-8")
    launch_manifest = root / ".aflow" / "launches" / f"{running_id}.json"
    _rewrite_run_metadata(launch_manifest, plan_path=str(long_plan.resolve()))

    completed_plan = Path(completed["plan"])
    dated_plan = completed_plan.with_name("readable-run-history-20260923.md")
    dated_plan.write_bytes(completed_plan.read_bytes())
    completed_metadata_path = root / ".aflow" / "runs" / completed_id / "run.json"
    _rewrite_run_metadata(
        completed_metadata_path,
        original_plan_display_name=dated_plan.name,
        original_plan_path=str(dated_plan.resolve()),
        active_plan_path=str(dated_plan.resolve()),
        plan_path=str(dated_plan.resolve()),
    )
    completed_metadata = json.loads(completed_metadata_path.read_text(encoding="utf-8"))
    completed_scope = completed_metadata.get("active_implementation_scope")
    if isinstance(completed_scope, dict):
        completed_scope["original_plan_path"] = str(dated_plan.resolve())
        completed_metadata_path.write_text(json.dumps(completed_metadata, indent=2) + "\n", encoding="utf-8")
    completed_launch_manifest = root / ".aflow" / "launches" / f"{completed_id}.json"
    _rewrite_run_metadata(completed_launch_manifest, plan_path=str(dated_plan.resolve()))
    assert isinstance(units, InMemoryUnitManager)
    running_unit = f"aflow-run-{running_id}.service"
    units.units[running_unit] = UnitState(name=running_unit, active_state="active", sub_state="running")

    from aflow_app_server import main

    worktree_id = _register_responsive_worktree(root)
    child_launches = root.parent / worktree_id / ".aflow" / "launches"
    child_launches.mkdir(parents=True, exist_ok=True)
    for launch in (root / ".aflow" / "launches").iterdir():
        if launch.is_file():
            target = child_launches / launch.name
            target.write_bytes(launch.read_bytes())
            if target.suffix != ".json" or target.name.endswith(".state.json"):
                continue
            child_metadata = json.loads(target.read_text(encoding="utf-8"))
            child_metadata["caller_scope"] = f"bearer:{worktree_id}"
            child_metadata["project_root"] = str((root.parent / worktree_id).resolve())
            plan_path = child_metadata.get("plan_path")
            if isinstance(plan_path, str):
                source_plan = Path(plan_path)
                try:
                    relative_plan = source_plan.resolve().relative_to(root.resolve())
                except ValueError:
                    relative_plan = None
                if relative_plan is not None:
                    child_plan = root.parent / worktree_id / relative_plan
                    child_plan.parent.mkdir(parents=True, exist_ok=True)
                    if source_plan.is_file():
                        child_plan.write_bytes(source_plan.read_bytes())
                    child_metadata["plan_path"] = str(child_plan.resolve())
            target.write_text(json.dumps(child_metadata, indent=2) + "\n", encoding="utf-8")
    assert main._project_registry is not None
    assert main._project_registry.rename(PROJECT_ID, RUN_ROW_PARENT_LABEL) is not None
    assert main._project_registry.rename(worktree_id, RUN_ROW_WORKTREE_LABEL) is not None

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail("The follow-up row fidelity test requires the real built web app; run the web build first.")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    artifact_dir = _fidelity_artifact_dir(tmp_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    browser_name = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    requests: list[tuple[str, str]] = []
    page_errors: list[str] = []
    loading_mode = {"enabled": False}
    held_loading_routes = []
    stale_mode = {"enabled": False}
    held_stale_routes = []
    stale_revision_changes: list[tuple[str, str, int, int]] = []

    def intercept_row_requests(route) -> None:
        parsed = urlsplit(route.request.url)
        parts = parsed.path.strip("/").split("/")
        if route.request.method != "GET" or len(parts) < 5 or parts[:3] != ["api", "control-plane", "projects"]:
            route.continue_()
            return
        project_id = parts[3]
        if len(parts) == 5 and parts[4] == "runs" and project_id == worktree_id and stale_mode["enabled"]:
            response = route.fetch()
            payload = response.json()
            changed = next((candidate for candidate in payload.get("runs", []) if candidate.get("run_id") == running_id), None)
            if changed is not None:
                previous_revision = int(changed.get("revision", 0))
                changed_revision = previous_revision + 1
                changed["revision"] = changed_revision
                stale_revision_changes.append((project_id, running_id, previous_revision, changed_revision))
            route.fulfill(response=response, json=payload)
            return
        if len(parts) == 6 and parts[4] == "runs" and project_id == worktree_id:
            run_id = parts[5]
            if run_id == paused_id:
                route.abort(error_code="failed")
                return
            if run_id == running_id and loading_mode["enabled"]:
                held_loading_routes.append(route)
                return
            if run_id == running_id and stale_mode["enabled"]:
                held_stale_routes.append(route)
                return
        route.continue_()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            _login(page, url)
            page.route("**/api/control-plane/projects/*/runs**", intercept_row_requests)
            for theme in RUN_ROW_THEMES:
                for width, height in RUN_ROW_VIEWPORTS:
                    page.set_viewport_size({"width": width, "height": height})
                    _set_theme_preference(page, theme)
                    exercise_loading = theme == "light" and (width, height) == (1280, 720)
                    loading_mode["enabled"] = exercise_loading
                    page.goto(f"{url}/?view=all-runs", wait_until="load")
                    _wait_for_fidelity_readiness(page)
                    _assert_theme(page, theme)
                    page.get_by_role("heading", name="All runs", exact=True).wait_for()
                    if exercise_loading:
                        search = page.get_by_role("searchbox", name="Search loaded runs", exact=True)
                        search.fill(running_id)
                        loading_row = _assert_global_run_row(
                            page,
                            running_id,
                            project_label=RUN_ROW_PROJECT_LABEL,
                            title=RUN_ROW_LONG_TITLE,
                            status="Running",
                        )
                        expect(loading_row).to_have_attribute("data-enrichment-state", "loading")
                        loading_name = loading_row.get_attribute("aria-label") or ""
                        assert "Loading checkpoint progress…" in loading_name, loading_name
                        expect(loading_row.locator(".compact-run-progress-row-notice")).to_have_count(0)
                        assert "Checkpoint progress unavailable" not in loading_row.inner_text()
                        for route in held_loading_routes:
                            route.continue_()
                        held_loading_routes.clear()
                        loading_mode["enabled"] = False
                        expect(loading_row).to_have_attribute("data-enrichment-state", "settled")
                    else:
                        page.wait_for_function(
                            """() => {
                              const result = document.querySelector('.global-run-results')
                              const rows = [...document.querySelectorAll('.global-run-row')]
                              return result?.getAttribute('aria-busy') === 'false'
                                && rows.length > 0
                                && rows.every(row => row.getAttribute('data-enrichment-state') !== 'loading')
                            }"""
                        )
                    _assert_no_horizontal_overflow(page)

                    search = page.get_by_role("searchbox", name="Search loaded runs", exact=True)
                    search.fill(running_id)
                    long_row = _assert_global_run_row(
                        page,
                        running_id,
                        project_label=RUN_ROW_PROJECT_LABEL,
                        title=RUN_ROW_LONG_TITLE,
                        status="Running",
                    )
                    expect(long_row).to_have_attribute("data-enrichment-state", "settled")
                    expect(long_row.locator(".compact-run-progress-row")).to_have_attribute("data-progress-state", "settled")
                    long_row.scroll_into_view_if_needed()
                    expect(long_row.locator(".global-run-row-project")).to_have_text(RUN_ROW_PROJECT_LABEL)
                    assert "Duration not reported" not in long_row.inner_text()
                    assert "Activity not reported" not in long_row.inner_text()
                    assert "Approval progress unknown" not in long_row.inner_text()
                    assert "Checkpoint progress unavailable" not in long_row.inner_text()
                    row_item = long_row.locator("xpath=..")
                    selection_box = long_row.bounding_box()
                    preview_toggle = row_item.locator(".run-row-preview-toggle")
                    preview_box = preview_toggle.bounding_box()
                    assert selection_box and selection_box["height"] >= 44, selection_box
                    assert preview_box and preview_box["width"] >= 44 and preview_box["height"] >= 44, preview_box

                    search.fill(completed_id)
                    dated_row = _assert_global_run_row(
                        page,
                        completed_id,
                        project_label=RUN_ROW_PROJECT_LABEL,
                        title=RUN_ROW_DATED_TITLE,
                        status="Completed",
                    )
                    dated_row.scroll_into_view_if_needed()
                    dated_name = dated_row.get_attribute("aria-label") or ""
                    assert "2026" in dated_name, dated_name
                    assert "2026" not in dated_row.inner_text()
                    dated_box = dated_row.bounding_box()
                    assert dated_box and dated_box["height"] >= 44, dated_box
                    if width >= 1280 and height >= 600:
                        assert 56 <= dated_box["height"] <= 72, dated_box
                    if width < 680:
                        expect(dated_row.locator(".run-list-title")).to_have_text(RUN_ROW_DATED_TITLE)
                        expect(dated_row.locator(".run-list-title")).to_be_visible()
                    if theme == "light" and (width, height) in {(390, 844), (1280, 720)}:
                        dated_toggle = dated_row.locator("xpath=..").locator(".run-row-preview-toggle")
                        dated_toggle.click()
                        dated_preview = page.locator(".run-row-preview:visible").first
                        expect(dated_preview).to_contain_text(RUN_ROW_DATED_TITLE)
                        expect(dated_preview).to_contain_text("2026")
                        expect(dated_preview).to_contain_text(completed_id)
                        page.keyboard.press("Escape")
                        expect(page.locator(".run-row-preview:visible")).to_have_count(0)
                        assert page.evaluate("() => document.activeElement?.classList.contains('run-row-preview-toggle')") is True

                    search.fill("")
                    page.wait_for_function(
                        """() => [...document.querySelectorAll('.global-run-row')]
                          .every(row => row.getAttribute('data-enrichment-state') !== 'loading')"""
                    )
                    long_row = _assert_global_run_row(
                        page,
                        running_id,
                        project_label=RUN_ROW_PROJECT_LABEL,
                        title=RUN_ROW_LONG_TITLE,
                        status="Running",
                    )
                    long_row.scroll_into_view_if_needed()
                    _wait_for_render_settle(page)
                    before_boxes = _run_row_boxes(page)
                    if width >= 1280 and height >= 600:
                        assert all(56 <= box["height"] <= 72 for box in before_boxes), before_boxes
                    row_item = long_row.locator("xpath=..")
                    preview_toggle = row_item.locator(".run-row-preview-toggle")

                    # Focus-triggered previews return focus to their actual
                    # selection opener rather than the sibling preview toggle,
                    # without the restored focus scheduling a delayed reopen.
                    page.mouse.move(1, 1)
                    long_row.focus()
                    expect(page.locator(".run-row-preview:visible")).to_have_count(1)
                    preview_toggle.focus()
                    page.keyboard.press("Escape")
                    expect(page.locator(".run-row-preview:visible")).to_have_count(0)
                    restored_focus = page.evaluate(
                        """() => ({
                          tag: document.activeElement?.tagName,
                          classes: document.activeElement?.className,
                          label: document.activeElement?.getAttribute('aria-label'),
                        })"""
                    )
                    assert "run-list-select" in str(restored_focus.get("classes", "")), restored_focus
                    page.wait_for_timeout(350)
                    expect(page.locator(".run-row-preview:visible")).to_have_count(0)

                    preview_toggle.click()
                    preview = page.locator(".run-row-preview:visible").first
                    expect(preview).to_be_visible()
                    _wait_for_render_settle(page)
                    expect(preview).to_contain_text(RUN_ROW_PROJECT_LABEL)
                    expect(preview).to_contain_text(RUN_ROW_LONG_TITLE)
                    expect(preview).to_contain_text(running_id)
                    assert "Duration not reported" not in preview.inner_text()
                    assert "Activity not reported" not in preview.inner_text()
                    after_boxes = _run_row_boxes(page)
                    assert after_boxes == before_boxes, {"before": before_boxes, "after": after_boxes}
                    _assert_no_horizontal_overflow(page)
                    page.keyboard.press("Escape")
                    expect(page.locator(".run-row-preview:visible")).to_have_count(0)
                    assert page.evaluate("() => document.activeElement?.classList.contains('run-row-preview-toggle')") is True

                    # Pointer previews are non-modal: an unrelated search
                    # control keeps focus even when the pointer enters selection.
                    search.focus()
                    search.hover()
                    long_row.hover()
                    expect(page.locator(".run-row-preview:visible")).to_have_count(1)
                    page.keyboard.press("Escape")
                    expect(page.locator(".run-row-preview:visible")).to_have_count(0)
                    assert page.evaluate("() => document.activeElement?.getAttribute('aria-label')") == "Search loaded runs"

                    # The exact global identity remains searchable and opens the
                    # child project/run pair; the row presentation never rewrites it.
                    if theme == "light" and (width, height) == (1280, 720):
                        search.fill(RUN_ROW_LONG_TITLE)
                        exact_row = _assert_global_run_row(
                            page,
                            running_id,
                            project_label=RUN_ROW_PROJECT_LABEL,
                            title=RUN_ROW_LONG_TITLE,
                            status="Running",
                        )
                        exact_row.click()
                        page.wait_for_function(
                            """expected => {
                              const params = new URL(location.href).searchParams
                              return params.get('project') === expected.project
                                && params.get('view') === 'runs'
                                && params.get('run') === expected.run
                            }""",
                            arg={"project": worktree_id, "run": running_id},
                        )
                        page.goto(f"{url}/?view=all-runs", wait_until="load")
                        _wait_for_fidelity_readiness(page)
                        page.get_by_role("heading", name="All runs", exact=True).wait_for()

                    # The parent copy has an evidenced zero approval count in
                    # its anchored preview; the child copy is intentionally
                    # failed below to prove the sighted retry state.
                    if theme == "light" and (width, height) == (1280, 720):
                        zero_row = _assert_global_run_row(
                            page,
                            paused_id,
                            project_label=RUN_ROW_PARENT_LABEL,
                            title="Provider recovery",
                            status="Paused",
                        )
                        zero_row.locator("xpath=..").locator(".run-row-preview-toggle").click()
                        zero_preview = page.locator(".run-row-preview:visible").first
                        expect(zero_preview).to_contain_text("0 of 3 checkpoints approved")
                        page.keyboard.press("Escape")

                    search.fill(paused_id)
                    failed_row = _assert_global_run_row(
                        page,
                        paused_id,
                        project_label=RUN_ROW_PROJECT_LABEL,
                        title="Provider recovery",
                        status="Paused",
                    )
                    expect(failed_row.locator(".compact-run-progress-row-notice.failed")).to_contain_text("Refresh to retry")
                    search.fill("")

                    if theme == "light" and (width, height) == (1280, 720):
                        search.fill(running_id)
                        stale_row = _assert_global_run_row(
                            page,
                            running_id,
                            project_label=RUN_ROW_PROJECT_LABEL,
                            title=RUN_ROW_LONG_TITLE,
                            status="Running",
                        )
                        expect(stale_row).to_have_attribute("data-enrichment-state", "settled")
                        stale_progress = stale_row.locator(".compact-run-progress-row")
                        expect(stale_progress).to_have_attribute("data-progress-state", "settled")
                        assert stale_progress.get_attribute("data-progress-availability") != "unavailable"

                        stale_mode["enabled"] = True
                        page.get_by_role("button", name="Refresh", exact=True).first.click()
                        expect(stale_row).to_have_attribute("data-enrichment-state", "stale")
                        expect(stale_row.locator(".compact-run-progress-row-notice.stale")).to_contain_text("Run changed; refresh")
                        assert held_stale_routes
                        expected_detail_path = f"/api/control-plane/projects/{worktree_id}/runs/{running_id}"
                        assert all(urlsplit(route.request.url).path == expected_detail_path for route in held_stale_routes)
                        assert any(
                            project_id == worktree_id
                            and run_id == running_id
                            and changed_revision == previous_revision + 1
                            for project_id, run_id, previous_revision, changed_revision in stale_revision_changes
                        ), stale_revision_changes

                        released_stale_routes = list(held_stale_routes)
                        stale_mode["enabled"] = False
                        for route in released_stale_routes:
                            route.continue_()
                        held_stale_routes.clear()
                        for route in released_stale_routes:
                            response = route.request.response()
                            assert response is not None and response.ok, route.request.url
                            response.body()
                        _wait_for_render_settle(page)
                        expect(stale_row).to_have_attribute("data-enrichment-state", "stale")
                        expect(stale_row.locator(".compact-run-progress-row-notice.stale")).to_contain_text("Run changed; refresh")
                        assert "Loading checkpoint progress…" not in (stale_row.get_attribute("aria-label") or "")
                        assert "Checkpoint progress unavailable" not in stale_row.inner_text()

                    screenshot = artifact_dir / f"run-rows-{browser_name}-{theme}-{width}x{height}.png"
                    page.screenshot(path=str(screenshot), full_page=True)
            writes = [
                {"method": method, "url": request_url}
                for method, request_url in requests
                if method not in {"GET", "HEAD", "OPTIONS"}
                and not request_url.endswith("/api/session")  # authentication setup
            ]
            assert writes == []
        finally:
            loading_mode["enabled"] = False
            for route in held_loading_routes:
                route.continue_()
            for route in held_stale_routes:
                route.continue_()
            browser.close()

    (artifact_dir / f"run-rows-{browser_name}.json").write_text(
        json.dumps(
            {
                "browser": browser_name,
                "viewports": RUN_ROW_VIEWPORTS,
                "themes": RUN_ROW_THEMES,
                "project_label": RUN_ROW_PROJECT_LABEL,
                "long_title": RUN_ROW_LONG_TITLE,
                "dated_title": RUN_ROW_DATED_TITLE,
                "screenshots": [
                    f"run-rows-{browser_name}-{theme}-{width}x{height}.png"
                    for theme in RUN_ROW_THEMES
                    for width, height in RUN_ROW_VIEWPORTS
                ],
                "writes": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    assert page_errors == [], page_errors


@pytest.mark.parametrize(
    ("width", "height", "theme", "dirty"),
    (
        pytest.param(1280, 720, "light", False, id="desktop-light-clean"),
        pytest.param(1280, 720, "dark", False, id="desktop-dark-clean"),
        pytest.param(390, 844, "light", False, id="phone-light-clean"),
        pytest.param(390, 844, "dark", False, id="phone-dark-clean"),
        pytest.param(844, 390, "light", False, id="landscape-light-clean"),
        pytest.param(844, 390, "dark", False, id="landscape-dark-clean"),
        pytest.param(1280, 720, "light", True, id="desktop-light-dirty"),
        pytest.param(1280, 720, "dark", True, id="desktop-dark-dirty"),
        pytest.param(390, 844, "light", True, id="phone-light-dirty"),
        pytest.param(390, 844, "dark", True, id="phone-dark-dirty"),
        pytest.param(844, 390, "light", True, id="landscape-light-dirty"),
        pytest.param(844, 390, "dark", True, id="landscape-dark-dirty"),
    ),
)
def test_ui_followup_launch_review(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
    dirty: bool,
) -> None:
    """Keep launch preparation concise while making the final action deliberate."""
    _, root, units, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    running = fixtures["running"]
    assert isinstance(running, dict)
    running_plan = Path(running["plan"])
    _commit_fixture_repository(root)
    dirty_paths = [f"launch-review-dirty-{index:02}.txt" for index in range(12)]
    if dirty:
        for path in dirty_paths:
            (root / path).write_text(path, encoding="utf-8")
    assert isinstance(units, InMemoryUnitManager)
    _prepare_disposable_fidelity_config(root, monkeypatch)

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail("The launch-review fidelity test requires the real built web app; run the web build first.")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    artifact_dir = _fidelity_artifact_dir(tmp_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stem = f"launch-review-{'dirty' if dirty else 'clean'}-{theme}-{width}x{height}"
    preparation_path = artifact_dir / f"{stem}-preparation.png"
    review_path = artifact_dir / f"{stem}-review.png"
    review_details_path = artifact_dir / f"{stem}-review-details.png"
    page_errors: list[str] = []

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs", wait_until="load")
            _wait_for_fidelity_readiness(page)
            # WebKit can finish the authenticated history bootstrap after the
            # session redirect; only retain runtime errors from the settled
            # launch-review surface below.
            page_errors.clear()
            assert load_reference_manifest()["demo_sha256"] == "2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4"
            page.get_by_role("button", name="New run", exact=True).click()

            plan_input = page.get_by_label("Run plan", exact=True)
            plan_input.wait_for()
            # Wait for the committed launch projection before selecting a
            # plan, so its first preflight identity is not superseded when
            # the background configuration read finishes.
            expect(page.get_by_text("Server default: 15.", exact=True)).to_be_visible(timeout=30_000)
            plan_input.click()
            plan_input.fill(running_plan.name)
            page.get_by_role("option", name=running_plan.name, exact=False).click()
            expect(plan_input).to_have_value(running_plan.relative_to(root).as_posix())

            workflow = page.get_by_label("Run workflow", exact=True)
            workflow.click()
            workflow.fill("managed")
            workflow.press("ArrowDown")
            workflow.press("Enter")
            expect(workflow).to_have_value("Managed")

            visible_dashboard = page.locator('.dashboard-host:not([hidden])').first
            preflight = visible_dashboard.locator('section[aria-label="Working tree preflight"]')
            preflight.wait_for(state="visible")
            expect(preflight).to_have_attribute("data-preflight-status", "ready", timeout=30_000)
            expect(preflight).to_contain_text(
                "Execution mode: existing checkout — uses the current checkout; "
                "acknowledge any uncommitted changes before continuing."
            )
            if dirty:
                expect(preflight.get_by_text("12 uncommitted changes detected.", exact=True)).to_be_visible()
                changed_files = preflight.locator("details.worktree-changed-files")
                assert changed_files.get_attribute("open") is None
                first_path = preflight.get_by_text(dirty_paths[0], exact=True)
                assert not first_path.is_visible()
                confirmation = preflight.get_by_role(
                    "checkbox", name="Continue despite uncommitted changes", exact=True
                )
                expect(confirmation).to_be_visible()
                confirmation.check()
                changed_files.locator(":scope > summary").click()
                expect(changed_files).to_have_attribute("open", "")
                expect(first_path).to_be_visible()
                expect(changed_files.get_by_text(dirty_paths[-1], exact=True)).to_be_visible()
                changed_files.locator(":scope > summary").click()
                assert changed_files.get_attribute("open") is None
            else:
                expect(preflight.get_by_text("No uncommitted changes detected.", exact=True)).to_be_visible()
                expect(preflight.get_by_role("checkbox", name="Continue despite uncommitted changes", exact=True)).to_have_count(0)

            advanced = page.get_by_role("button", name="Advanced options", exact=True)
            expect(advanced).to_have_attribute("aria-expanded", "false")
            launch_details = page.locator("details.launch-preparation-details")
            assert launch_details.get_attribute("open") is None
            expect(page.get_by_role("button", name="Review start…", exact=True)).to_be_enabled()
            page.screenshot(path=str(preparation_path), full_page=True)

            review_trigger = page.get_by_role("button", name="Review start…", exact=True)
            review_trigger.click()
            review_region = page.get_by_role("region", name="Review start", exact=True)
            review_region.wait_for(state="visible")
            heading = review_region.locator("h4.launch-review-heading")
            expect(heading).to_be_visible()
            expect(heading).to_have_attribute("tabindex", "-1")
            assert heading.evaluate("element => document.activeElement === element")
            page.wait_for_function(
                "height => { const heading = document.querySelector('.launch-review-heading')?.getBoundingClientRect(); const consequence = document.querySelector('.launch-review-consequence')?.getBoundingClientRect(); if (!heading || !consequence || heading.top < 0 || heading.top > height / 2 || consequence.bottom > height) return false; const previous = window.__aflowLaunchReviewScroll; const stableFrames = previous && Math.abs(previous.top - heading.top) <= 0.5 ? previous.stableFrames + 1 : 0; window.__aflowLaunchReviewScroll = { top: heading.top, stableFrames }; return stableFrames >= 4; }",
                arg=height,
            )
            heading_box = heading.bounding_box()
            assert heading_box is not None
            assert 0 <= heading_box["y"] <= height / 2, heading_box
            expect(review_region.locator(".launch-review-consequence")).to_be_visible()
            expect(review_region).to_contain_text("No run is allocated until you choose Start run.")
            expect(review_region).to_contain_text("Execution mode")
            expect(review_region).to_contain_text("Turn limit")
            full_details = review_region.locator("details.launch-review-details")
            assert full_details.get_attribute("open") is None
            assert units.start_calls == []
            page.screenshot(path=str(review_path), full_page=False)

            full_details.locator(":scope > summary").click()
            expect(full_details).to_have_attribute("open", "")
            assert units.start_calls == []
            page.screenshot(path=str(review_details_path), full_page=False)

            should_start = not dirty and width == 1280 and height == 720 and theme == "light"
            if should_start:
                with page.expect_response(
                    lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == f"/api/control-plane/projects/{PROJECT_ID}/runs"
                ) as start_response:
                    page.get_by_role("button", name="Start run", exact=True).click()
                assert start_response.value.status == 201
                result = start_response.value.json()["result"]
                returned_run_id = result["run_id"]
                page.wait_for_function(
                    "runId => new URL(location.href).searchParams.get('run') === runId",
                    arg=returned_run_id,
                )
                assert len(units.start_calls) == 1
                assert units.start_calls[0][0] == f"aflow-run-{returned_run_id}.service"
            else:
                review_region.get_by_role("button", name="Cancel review", exact=True).click()
                expect(page.get_by_role("region", name="Review start", exact=True)).to_have_count(0)
                page.wait_for_function(
                    "() => document.activeElement?.textContent?.trim() === 'Review start…'"
                )
                assert units.start_calls == []
        finally:
            page.close()
            browser.close()

    assert page_errors == []

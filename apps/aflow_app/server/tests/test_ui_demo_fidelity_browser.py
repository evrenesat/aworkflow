"""Frozen reference and production capture harness smoke tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

from aflow.control_plane.units import InMemoryUnitManager, UnitState
from test_control_plane_api import PROJECT_ID, control_client, live_server  # noqa: F401
from test_responsive_browser import _assert_header_and_flow, _assert_theme, _browser, _login, _seed_team_family_fixture, _select_settings_section, _set_theme_preference
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

    content = page.locator(
        ".workspace-main > .dashboard-host:visible, .workspace-main > .workspace-content:visible"
    ).first
    content_box = content.bounding_box()
    assert content_box is not None
    title_box = anchors["title"]["box"]
    latest_box = anchors["latest_result"]["box"]
    if capture["width"] >= 960 and capture["height"] >= 600:
        assert content_box["y"] <= 128, {"content": content_box, "capture": capture}
        assert title_box["y"] + title_box["height"] <= capture["height"], title_box
        assert current_box["y"] + current_box["height"] <= capture["height"], current_box
        assert latest_box["y"] + latest_box["height"] <= capture["height"], latest_box
    elif capture["width"] == 390 and capture["height"] == 844:
        assert latest_box["y"] < capture["height"], {
            "latest_result": latest_box,
            "viewport": {"width": capture["width"], "height": capture["height"]},
        }


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


_FIDELITY_DIAGNOSTIC_STYLE_PROPERTIES = (
    "display",
    "visibility",
    "position",
    "top",
    "height",
    "minHeight",
    "paddingTop",
    "paddingBottom",
    "marginTop",
    "marginBottom",
    "gap",
    "overflow",
    "overflowY",
    "flexDirection",
    "flexWrap",
    "alignItems",
    "boxSizing",
    "fontFamily",
    "fontSize",
    "lineHeight",
)

_FIDELITY_CREDENTIAL_REDACTION_PATTERN = (
    r"(?i)(?:"
    r"\b(?:authorization|auth(?:entication)?|token|password|secret|cookie)\b"
    r"\s*[:=]?\s*(?:(?:bearer|basic)\s+)?\S+"
    r"|\bbearer\b\s*[:=]?\s*\S+)"
)


def _redact_fidelity_text(value: object, *, limit: int = 1200) -> str:
    """Keep failure messages useful without copying credentials into artifacts."""
    text = str(value)
    for pattern in (
        _FIDELITY_CREDENTIAL_REDACTION_PATTERN,
        r"\b[A-Za-z0-9_-]{32,}\b",
    ):
        text = re.sub(pattern, "[redacted]", text)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _fidelity_artifact_dir(tmp_path: Path) -> Path:
    configured = os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", "").strip()
    return Path(configured) if configured else tmp_path


def _prepare_disposable_fidelity_config(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make project readiness use the control fixture's valid config pair."""
    config_path = root.parent / "global" / "aflow.toml"
    workflows_path = config_path.with_name("workflows.toml")
    assert config_path.is_file()
    assert workflows_path.is_file()

    # The control fixture already supplies this pair to the daemon.  The
    # project-list readiness projection uses aflow.config's default lookup,
    # which otherwise depends on the host HOME and can add a real warning to
    # the captured app only on clean CI workers.
    import aflow.config as aflow_config

    monkeypatch.setattr(aflow_config, "_config_path", lambda: config_path)
    assert aflow_config.project_configuration_state() == "ready"


def _wait_for_fidelity_readiness(page) -> None:
    """Wait for font readiness and two render frames without a fixed sleep."""
    page.evaluate(
        """() => new Promise(resolve => {
          const settle = () => requestAnimationFrame(() => requestAnimationFrame(resolve));
          if (document.fonts && document.fonts.status === 'loading') {
            document.fonts.ready.then(settle, settle);
          } else {
            settle();
          }
        })"""
    )


def _capture_fidelity_failure_snapshot(page, capture: dict[str, object]) -> dict[str, object]:
    """Capture one bounded DOM state before the production geometry assertion."""
    selectors = {
        "app_shell": ".app-shell",
        "app_header": ".app-header",
        "header_row_one": ".app-header-row-one",
        "header_row_two": ".app-header-row-two",
        "workspace_main": ".workspace-main",
        "workspace_content": ".workspace-main > .workspace-content",
        "dashboard_host": ".workspace-main > .dashboard-host",
        "readiness_notice": ".workspace-main > .notice[role='note']",
        "run_detail": ".run-detail",
        "progress_surface": PRODUCTION_PROGRESS_SURFACE,
        **PRODUCTION_ANCHORS,
    }
    context = {
        "fixture_variant": capture["fixture_variant"],
        "status": capture["status"],
        "width": capture["width"],
        "height": capture["height"],
        "theme": capture["theme"],
        "browser": os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower(),
    }
    page_errors = capture.get("page_errors")
    if isinstance(page_errors, list):
        context["page_errors"] = [_redact_fidelity_text(error, limit=400) for error in page_errors]
    return page.evaluate(
        r"""({selectors, context, styleProperties}) => {
          const rect = element => {
            if (!(element instanceof Element)) return null;
            const box = element.getBoundingClientRect();
            return {
              x: box.x, y: box.y, width: box.width, height: box.height,
              top: box.top, right: box.right, bottom: box.bottom, left: box.left,
            };
          };
          const style = element => {
            if (!(element instanceof Element)) return null;
            const computed = getComputedStyle(element);
            return Object.fromEntries(styleProperties.map(property => [property, computed[property]]));
          };
          const visible = element => {
            if (!(element instanceof Element)) return false;
            const computed = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return computed.display !== 'none'
              && computed.visibility !== 'hidden'
              && box.width > 0
              && box.height > 0;
          };
          const node = (selector, element) => ({
            selector,
            present: Boolean(element),
            visible: visible(element),
            rect: rect(element),
            styles: style(element),
            scroll: element instanceof Element ? {
              top: element.scrollTop,
              left: element.scrollLeft,
              height: element.scrollHeight,
              width: element.scrollWidth,
            } : null,
          });
          const nodes = Object.fromEntries(Object.entries(selectors).map(([name, selector]) => {
            const matches = [...document.querySelectorAll(selector)];
            return [name, {
              selector,
              matchCount: matches.length,
              matches: matches.slice(0, 8).map(element => node(selector, element)),
            }];
          }));
          const redactText = value => String(value || '')
            .replace(/(?:\b(?:authorization|auth(?:entication)?|token|password|secret|cookie)\b\s*[:=]?\s*(?:(?:bearer|basic)\s+)?\S+|\bbearer\b\s*[:=]?\s*\S+)/gi, '[redacted]')
            .replace(/\b[A-Za-z0-9_-]{32,}\b/g, '[redacted]')
            .slice(0, 240);
          const alertElements = [...document.querySelectorAll(
            '[role="alert"], [role="alertdialog"], [role="status"], [role="note"], .notice'
          )];
          const alerts = [...new Set(alertElements)].slice(0, 16).map(element => ({
            role: element.getAttribute('role'),
            className: String(element.className || ''),
            text: redactText(element.textContent),
            visible: visible(element),
            rect: rect(element),
            styles: style(element),
          }));
          const scrollingElement = document.scrollingElement;
          const active = document.activeElement;
          return {
            context,
            readyState: document.readyState,
            viewport: {
              innerWidth: window.innerWidth,
              innerHeight: window.innerHeight,
              devicePixelRatio: window.devicePixelRatio,
              visual: window.visualViewport ? {
                width: window.visualViewport.width,
                height: window.visualViewport.height,
                offsetLeft: window.visualViewport.offsetLeft,
                offsetTop: window.visualViewport.offsetTop,
                scale: window.visualViewport.scale,
              } : null,
            },
            scroll: {
              windowX: window.scrollX,
              windowY: window.scrollY,
              documentTop: scrollingElement?.scrollTop ?? null,
              documentHeight: scrollingElement?.scrollHeight ?? null,
              documentClientHeight: scrollingElement?.clientHeight ?? null,
              bodyHeight: document.body?.scrollHeight ?? null,
            },
            focus: active ? {
              tag: active.tagName,
              id: active.id,
              className: String(active.className || ''),
              role: active.getAttribute('role'),
              ariaLabel: active.getAttribute('aria-label'),
            } : null,
            fonts: {
              status: document.fonts?.status ?? null,
              count: document.fonts?.size ?? null,
              sansReady: document.fonts ? document.fonts.check('16px sans-serif') : null,
            },
            configuration: {
              theme: document.documentElement.getAttribute('data-theme'),
              prefersDark: window.matchMedia('(prefers-color-scheme: dark)').matches,
              readyState: document.readyState,
              viewportMeta: document.querySelector('meta[name="viewport"]')?.getAttribute('content') ?? null,
            },
            nodes,
            alerts,
          };
        }""",
        {
            "selectors": selectors,
            "context": context,
            "styleProperties": _FIDELITY_DIAGNOSTIC_STYLE_PROPERTIES,
        },
    )


def _write_fidelity_failure_artifacts(
    page,
    *,
    tmp_path: Path,
    capture: dict[str, object],
    snapshot: dict[str, object],
    error: AssertionError,
) -> None:
    artifact_dir = _fidelity_artifact_dir(tmp_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    browser = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(snapshot["context"]["browser"]))
    stem = (
        f"ui-demo-fidelity-{capture['fixture_variant']}-{capture['theme']}"
        f"-{capture['width']}x{capture['height']}-{browser}"
    )
    artifact_path = artifact_dir / f"{stem}-failure.json"
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "failure": _redact_fidelity_text(error),
                "capture": {
                    "fixture_variant": capture["fixture_variant"],
                    "status": capture["status"],
                    "width": capture["width"],
                    "height": capture["height"],
                    "theme": capture["theme"],
                },
                "snapshot": snapshot,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    screenshot_path = artifact_dir / f"{stem}-failure.png"
    try:
        page.screenshot(path=str(screenshot_path), full_page=False)
    except PlaywrightError as screenshot_error:
        print("AFLOW_UI_DEMO_FIDELITY_SCREENSHOT_ERROR", _redact_fidelity_text(screenshot_error))
    print("AFLOW_UI_DEMO_FIDELITY_FAILURE_ARTIFACT", artifact_path)
    print("AFLOW_UI_DEMO_FIDELITY_FAILURE_SCREENSHOT", screenshot_path)


def test_fidelity_diagnostic_redaction_consumes_short_credentials() -> None:
    """Both diagnostic representations remove complete short credentials."""
    credential_values = (
        "short-bearer",
        "short-token",
        "short-cookie",
        "short-password",
        "short-secret",
        "short-standalone-bearer",
        "long-" + "x" * 40,
    )
    diagnostic_text = (
        "Authorization: Bearer short-bearer "
        "token=short-token cookie=short-cookie password=short-password "
        "secret=short-secret bearer short-standalone-bearer "
        "long-"
        + "x" * 40
    )
    python_redacted = _redact_fidelity_text(diagnostic_text)
    assert all(value not in python_redacted for value in credential_values)
    assert python_redacted.count("[redacted]") >= len(credential_values)
    assert len(python_redacted) <= 1200

    with sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page()
        try:
            page.set_content('<div class="notice" role="alert"></div>')
            page.locator(".notice").evaluate(
                "(element, value) => { element.textContent = value }",
                diagnostic_text,
            )
            snapshot = _capture_fidelity_failure_snapshot(
                page,
                {
                    "fixture_variant": "redaction",
                    "status": "failed",
                    "width": 1280,
                    "height": 720,
                    "theme": "light",
                    "page_errors": [],
                },
            )
            browser_redacted = "\n".join(alert["text"] for alert in snapshot["alerts"])
            assert all(value not in browser_redacted for value in credential_values)
            assert browser_redacted.count("[redacted]") >= len(credential_values)
            assert all(len(alert["text"]) <= 240 for alert in snapshot["alerts"])
        finally:
            browser.close()


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
    _prepare_disposable_fidelity_config(root, monkeypatch)
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
                        _wait_for_fidelity_readiness(page)
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
                        failure_snapshot = _capture_fidelity_failure_snapshot(page, capture)
                        try:
                            expect(page.locator(".workspace-main > .notice[role='note']:visible")).to_have_count(0)
                            _assert_production_capture(page, capture)
                        except AssertionError as error:
                            _write_fidelity_failure_artifacts(
                                page,
                                tmp_path=tmp_path,
                                capture=capture,
                                snapshot=failure_snapshot,
                                error=error,
                            )
                            raise
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
    ("width", "height", "theme", "held_refresh"),
    (
        pytest.param(1280, 720, "light", False, id="desktop-light"),
        pytest.param(1280, 720, "dark", False, id="desktop-dark"),
        pytest.param(390, 844, "light", False, id="mobile-light"),
        pytest.param(390, 844, "dark", False, id="mobile-dark"),
        pytest.param(1280, 720, "dark", True, id="desktop-dark-held-refresh"),
    ),
)
def test_ui_demo_cp8_plan_editor_and_review_captures(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
    held_refresh: bool,
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
    preflight_trace: list[dict[str, object]] = []
    routed_preflights: list[dict[str, object]] = []
    routed_requests = []
    routed_terminals: list[dict[str, object]] = []
    trace_active = False
    latest_preflight_identity: dict[str, object] | None = None
    latest_preflight_readiness: dict[str, object] | None = None

    def append_preflight_trace(event: dict[str, object]) -> None:
        if trace_active and len(preflight_trace) < 48:
            preflight_trace.append({"order": len(preflight_trace), **event})

    def endpoint_for(url: str) -> str | None:
        path = urlsplit(url).path
        if path == "/api/config":
            return "committed-config"
        if path == "/api/config/form":
            return "committed-defaults"
        if path.endswith("/runs/preflight"):
            return "preflight"
        return None

    def preflight_identity(request) -> dict[str, object]:
        try:
            payload = json.loads(request.post_data or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        plan_path_value = payload.get("plan_path")
        return {
            "plan_path_present": bool(plan_path_value),
            "plan_path_sha256": (
                hashlib.sha256(str(plan_path_value).encode()).hexdigest() if plan_path_value else None
            ),
            "workflow_name": payload.get("workflow_name"),
            "team": payload.get("team"),
            "start_step": payload.get("start_step"),
            "max_turns": payload.get("max_turns"),
            "extra_instructions_present": bool(payload.get("extra_instructions")),
            "dirty_worktree_confirmed": payload.get("dirty_worktree_confirmed"),
            "offset": payload.get("offset"),
            "limit": payload.get("limit"),
        }

    def route_index_for(request) -> int | None:
        return next((index for index, routed in enumerate(routed_requests) if routed is request), None)

    def routed_outcome_records() -> list[dict[str, object]]:
        return [
            {
                "route_index": index,
                **record,
                "terminal_outcome": next(
                    (event["kind"] for event in routed_terminals if event["route_index"] == index),
                    None,
                ),
            }
            for index, record in enumerate(routed_preflights[:24])
        ]

    def trace_request(request) -> None:
        nonlocal latest_preflight_identity
        endpoint = endpoint_for(request.url)
        if endpoint is None or not trace_active:
            return
        event: dict[str, object] = {"kind": "request", "endpoint": endpoint, "method": request.method}
        if endpoint == "preflight":
            event["identity"] = preflight_identity(request)
            latest_preflight_identity = event["identity"]
        append_preflight_trace(event)

    def trace_response(response) -> None:
        nonlocal latest_preflight_readiness
        endpoint = endpoint_for(response.url)
        if endpoint is None or not trace_active:
            return
        event: dict[str, object] = {"kind": "response", "endpoint": endpoint, "status": response.status}
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and endpoint == "committed-config":
            event["revision"] = payload.get("revision")
        elif isinstance(payload, dict) and endpoint == "committed-defaults":
            event["server_default_max_turns"] = payload.get("server_default_max_turns")
        elif isinstance(payload, dict) and endpoint == "preflight":
            blockers = payload.get("blockers")
            items = payload.get("items")
            event["readiness"] = {
                "dirty": payload.get("dirty"),
                "requires_confirmation": payload.get("requires_confirmation"),
                "blocker_count": len(blockers) if isinstance(blockers, list) else None,
                "total_items": payload.get("total_items"),
                "returned_items": len(items) if isinstance(items, list) else None,
                "offset": payload.get("offset"),
                "next_offset": payload.get("next_offset"),
                "execution_mode": payload.get("execution_mode"),
            }
            latest_preflight_readiness = event["readiness"]
            event["identity"] = preflight_identity(response.request)
            event["route_index"] = route_index_for(response.request)
        append_preflight_trace(event)

    def trace_terminal(request, outcome: str) -> None:
        route_index = route_index_for(request)
        if route_index is None:
            return
        event = {
            "kind": outcome,
            "endpoint": "preflight",
            "route_index": route_index,
            "identity": preflight_identity(request),
        }
        routed_terminals.append(event)
        append_preflight_trace(event)

    def trace_dom(label: str) -> dict[str, object]:
        snapshot = page.evaluate(
            """label => {
              const dashboard = [...document.querySelectorAll('.dashboard-host:not([hidden])')]
                .find(host => getComputedStyle(host).display !== 'none' && host.getClientRects().length > 0
                  && host.querySelector('section.card.start-run-form')) ?? null;
              const preflight = dashboard?.querySelector('.worktree-preflight') ?? null;
              const review = [...document.querySelectorAll('button')]
                .find(button => button.textContent?.trim() === 'Review start…' && button.getClientRects().length > 0) ?? null;
              const reviewRegion = dashboard?.querySelector('[role="region"][aria-label="Review start"]') ?? null;
              const value = name => dashboard?.querySelector(`[aria-label="${name}"]`)?.value ?? null;
              const confirmation = dashboard?.querySelector('.worktree-confirmation input[type="checkbox"]') ?? null;
              const feedback = [...document.querySelectorAll('[role="alert"], [role="status"]')]
                .filter(element => element.getClientRects().length > 0)
                .map(element => element.textContent?.trim() ?? '')
                .filter(Boolean)
                .slice(0, 4);
              const active = document.activeElement;
              return {
                kind: 'dom',
                label,
                preflight_status: preflight?.dataset.preflightStatus ?? null,
                launch_identity: {
                  plan_path: value('Run plan'),
                  workflow: value('Run workflow'),
                  team: value('Run team'),
                  team_stage: value('Run team stage'),
                  max_turns: value('Run max turns'),
                  start_step: value('Run start step'),
                  dirty_worktree_confirmed: confirmation?.checked ?? null,
                },
                review_button_present: review !== null,
                review_button_disabled: review?.hasAttribute('disabled') ?? null,
                review_enabled: review !== null && !review.hasAttribute('disabled'),
                review_region_present: reviewRegion !== null,
                review_region_visible: reviewRegion !== null && reviewRegion.getClientRects().length > 0,
                action_feedback: feedback,
                focused_element: active === reviewRegion?.querySelector('.launch-review-heading')
                  ? 'review-heading'
                  : active?.tagName?.toLowerCase() ?? null,
              };
            }""",
            label,
        )
        snapshot["latest_preflight_identity"] = latest_preflight_identity
        snapshot["latest_preflight_readiness"] = latest_preflight_readiness
        append_preflight_trace(snapshot)
        return snapshot

    def trace_payload(label: str) -> dict[str, object]:
        snapshot = trace_dom(label)
        return {
            "events": list(preflight_trace),
            "routed_count": len(routed_preflights),
            "routed": routed_outcome_records(),
            "final_dom": snapshot,
        }

    def save_preflight_failure(label: str, error: Exception) -> None:
        artifact_dir = _fidelity_artifact_dir(tmp_path)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        failure_stem = f"cp8-preflight-failure-{theme}-{width}x{height}"
        failure_screenshot = artifact_dir / f"{failure_stem}.png"
        failure_manifest = artifact_dir / f"{failure_stem}.json"
        screenshot_error = None
        try:
            page.screenshot(path=str(failure_screenshot), full_page=True, timeout=10_000)
        except Exception as screenshot_exception:
            screenshot_error = type(screenshot_exception).__name__
        evidence = {
            "error_type": type(error).__name__,
            "error": str(error).splitlines()[0][:240],
            "trace": trace_payload(label),
            "start_call_count": len(units.start_calls),
            "screenshot": failure_screenshot.name if screenshot_error is None else None,
            "screenshot_error": screenshot_error,
        }
        failure_manifest.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("AFLOW_CP8_PREFLIGHT_FAILURE", json.dumps(evidence, sort_keys=True))

    def wait_for_current_preflight(label: str, *, require_review: bool) -> dict[str, object]:
        require_confirmation = bool(
            require_review
            and latest_preflight_readiness
            and latest_preflight_readiness.get("requires_confirmation")
        )
        try:
            snapshot = page.wait_for_function(
                """({label, requireReview, requireConfirmation}) => {
                  const dashboard = [...document.querySelectorAll('.dashboard-host:not([hidden])')]
                    .find(host => getComputedStyle(host).display !== 'none' && host.getClientRects().length > 0
                      && host.querySelector('section.card.start-run-form')) ?? null;
                  const preflight = dashboard?.querySelector('.worktree-preflight') ?? null;
                  const review = [...document.querySelectorAll('button')]
                    .find(button => button.textContent?.trim() === 'Review start…' && button.getClientRects().length > 0) ?? null;
                  if (!preflight || preflight.dataset.preflightStatus !== 'ready') return false;
                  const confirmation = dashboard?.querySelector('.worktree-confirmation input[type="checkbox"]');
                  if (requireConfirmation && (!confirmation || !confirmation.checked)) return false;
                  if (requireReview && (!review || review.hasAttribute('disabled'))) return false;
                  const value = name => dashboard?.querySelector(`[aria-label="${name}"]`)?.value ?? null;
                  return {
                    kind: 'dom',
                    label,
                    preflight_status: preflight.dataset.preflightStatus,
                    launch_identity: {
                      plan_path: value('Run plan'),
                      workflow: value('Run workflow'),
                      team: value('Run team'),
                      team_stage: value('Run team stage'),
                      max_turns: value('Run max turns'),
                      start_step: value('Run start step'),
                      dirty_worktree_confirmed: confirmation?.checked ?? null,
                    },
                    review_button_present: review !== null,
                    review_button_disabled: review?.hasAttribute('disabled') ?? null,
                    review_enabled: review !== null && !review.hasAttribute('disabled'),
                    review_region_present: Boolean(dashboard?.querySelector('[role="region"][aria-label="Review start"]')),
                  };
                }""",
                arg={
                    "label": label,
                    "requireReview": require_review,
                    "requireConfirmation": require_confirmation,
                },
            ).json_value()
        except Exception as error:
            save_preflight_failure(f"{label}-timeout", error)
            raise
        snapshot["latest_preflight_identity"] = latest_preflight_identity
        snapshot["latest_preflight_readiness"] = latest_preflight_readiness
        append_preflight_trace(snapshot)
        return snapshot

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.expose_function("__aflowCp8TerminalCount", lambda: len(routed_terminals))
        page.on("request", trace_request)
        page.on("response", trace_response)
        page.on("requestfinished", lambda request: trace_terminal(request, "requestfinished"))
        page.on("requestfailed", lambda request: trace_terminal(request, "requestfailed"))
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
            trace_active = True
            page.get_by_label("Run plan", exact=True).wait_for()
            trace_dom("launch-form-visible")
            expect(page.get_by_label("Run plan", exact=True)).to_have_value(plan_path)
            expect(page.get_by_label("Run workflow", exact=True)).to_be_visible()
            expect(page.get_by_label("Run team", exact=True)).to_be_visible()
            expect(page.get_by_label("Run max turns", exact=True)).to_be_visible()
            expect(page.get_by_text("Server default: 15.", exact=True)).to_be_visible()
            expect(page.get_by_label("Effective choices for this launch", exact=True)).to_contain_text("15 — server default")
            advanced = page.get_by_role("button", name="Advanced options", exact=True)
            expect(advanced).to_have_attribute("aria-expanded", "false")
            launch_form = page.locator("section.card.start-run-form:visible").first
            preflight = launch_form.locator(".worktree-preflight")
            preflight.wait_for()
            trace_dom("preflight-panel-visible")
            initial_readiness = wait_for_current_preflight("current-preflight-ready", require_review=False)
            assert initial_readiness["preflight_status"] == "ready", initial_readiness
            confirmation = preflight.get_by_role(
                "checkbox",
                name="Continue despite uncommitted changes",
                exact=True,
            )
            if latest_preflight_readiness and latest_preflight_readiness.get("requires_confirmation"):
                expect(confirmation).to_be_visible(timeout=30_000)
                if not confirmation.is_checked():
                    confirmation.check()
                expect(confirmation).to_be_checked(timeout=30_000)
            elif confirmation.count():
                expect(confirmation).to_be_visible()
                if not confirmation.is_checked():
                    confirmation.check()
                expect(confirmation).to_be_checked()
            current_readiness = wait_for_current_preflight("current-preflight-and-review-ready", require_review=True)
            assert current_readiness["preflight_status"] == "ready", current_readiness
            assert current_readiness["review_enabled"] is True, current_readiness
            review_button = page.get_by_role("button", name="Review start…", exact=True)
            expect(page.get_by_role("button", name="Start run", exact=True)).to_have_count(0)
            preflight_status = str(current_readiness["preflight_status"])
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
                "preflight_trace": list(preflight_trace),
                "anchors": measure_named_anchors(page, {
                    "new_run": ".start-run-form.card",
                    "plan": "[aria-label='Run plan']",
                    "preflight": ".worktree-preflight",
                    "review_action": "button:has-text('Review start…')",
                }),
            }

            held_preflight_routes = []
            if held_refresh:
                held_route_released = False
                route_registered = False

                def hold_preflight(route) -> None:
                    identity = preflight_identity(route.request)
                    if not held_preflight_routes and not routed_preflights:
                        held_preflight_routes.append(route)
                        action = "held"
                    else:
                        action = "continued"
                    routed_requests.append(route.request)
                    routed_preflights.append({
                        "identity": identity,
                        "action": action,
                        "role": "held_refresh" if action == "held" else "other",
                    })
                    if action == "continued":
                        route.continue_()

                page.route("**/runs/preflight", hold_preflight)
                route_registered = True
                try:
                    with page.expect_request("**/runs/preflight") as held_request_info:
                        preflight.get_by_role("button", name="Refresh worktree inspection", exact=True).click()
                    held_identity = preflight_identity(held_request_info.value)
                    expect(preflight).to_have_attribute("data-preflight-status", "loading")
                    loading_observation = trace_dom("review-click-refresh-loading")
                    assert loading_observation["preflight_status"] == "loading", loading_observation
                    assert len(held_preflight_routes) == 1, routed_preflights
                    assert routed_requests[0] is held_request_info.value
                    assert routed_preflights[0]["identity"] == held_identity
                    assert routed_preflights[0]["action"] == "held"

                    max_turns = page.get_by_label("Run max turns", exact=True)
                    with page.expect_response(
                        lambda response: endpoint_for(response.url) == "preflight"
                        and preflight_identity(response.request)["max_turns"] == 16
                        and response.request == routed_requests[-1]
                    ) as changed_response_info:
                        max_turns.fill("16")
                    changed_response = changed_response_info.value
                    assert changed_response.status == 200
                    assert preflight_identity(changed_response.request)["max_turns"] == 16
                    changed_route_index = route_index_for(changed_response.request)
                    assert changed_route_index is not None
                    routed_preflights[changed_route_index]["role"] = "changed_turns"

                    routed_before_restore = len(routed_requests)
                    with page.expect_response(
                        lambda response: endpoint_for(response.url) == "preflight"
                        and preflight_identity(response.request) == held_identity
                        and len(routed_requests) > routed_before_restore
                        and response.request in routed_requests[routed_before_restore:]
                    ) as restored_response_info:
                        max_turns.fill("")
                    restored_response = restored_response_info.value
                    restored_result = restored_response.json()
                    assert restored_response.status == 200
                    assert restored_result["blockers"] == []
                    restored_route_index = route_index_for(restored_response.request)
                    assert restored_route_index is not None
                    routed_preflights[restored_route_index]["role"] = "restored_default"
                    assert len(routed_preflights) >= 3, routed_preflights
                    assert [item["action"] for item in routed_preflights] == [
                        "held", *(["continued"] * (len(routed_preflights) - 1))
                    ], routed_preflights
                    assert routed_preflights[-1]["identity"] == held_identity, routed_preflights
                    before_release = wait_for_current_preflight(
                        "held-refresh-restored-ready", require_review=True
                    )
                    held_preflight_routes[0].continue_()
                    held_route_released = True
                    page.unroute("**/runs/preflight", hold_preflight)
                    route_registered = False
                    page.wait_for_function(
                        "expected => window.__aflowCp8TerminalCount().then(count => count >= expected)",
                        arg=len(routed_requests),
                    )
                    outcomes = routed_outcome_records()
                    assert len(outcomes) == len(routed_preflights), outcomes
                    assert all(record["terminal_outcome"] in ("requestfinished", "requestfailed") for record in outcomes), outcomes
                    assert outcomes[changed_route_index]["terminal_outcome"] == "requestfinished", outcomes
                    assert outcomes[restored_route_index]["terminal_outcome"] == "requestfinished", outcomes
                    held_outcome = outcomes[0]["terminal_outcome"]
                    if held_outcome == "requestfinished":
                        assert any(
                            event["kind"] == "response" and event.get("route_index") == 0
                            for event in preflight_trace
                        ), outcomes
                    else:
                        assert held_outcome == "requestfailed", outcomes
                    after_release = wait_for_current_preflight(
                        "held-refresh-after-terminal", require_review=True
                    )
                    assert after_release["launch_identity"]["max_turns"] == "", after_release
                    assert after_release["review_enabled"] is True, after_release
                    assert after_release["launch_identity"]["dirty_worktree_confirmed"] is True, after_release
                    captures["states"]["held_refresh_overlap"] = {
                        "routed": outcomes,
                        "changed_response_status": changed_response.status,
                        "restored_response_status": restored_response.status,
                        "restored_readiness": {
                            "requires_confirmation": restored_result["requires_confirmation"],
                            "blocker_count": len(restored_result["blockers"]),
                        },
                        "before_release": before_release,
                        "after_release": after_release,
                    }
                except Exception as error:
                    save_preflight_failure("held-refresh-overlap", error)
                    raise
                finally:
                    try:
                        if not held_route_released:
                            for held_route in held_preflight_routes:
                                held_route.continue_()
                    finally:
                        if route_registered:
                            page.unroute("**/runs/preflight", hold_preflight)
                assert len(held_preflight_routes) == 1, routed_preflights
                assert captures["states"]["held_refresh_overlap"]["before_release"]["preflight_status"] == "ready"

            before_review_click = None
            review_region = launch_form.get_by_role("region", name="Review start", exact=True)
            try:
                before_review_click = wait_for_current_preflight("review-click-before", require_review=True)
                assert before_review_click["preflight_status"] == "ready", before_review_click
                assert before_review_click["review_enabled"] is True, before_review_click
                assert {key: before_review_click["launch_identity"][key] for key in ("plan_path", "workflow", "team", "max_turns")} == {
                    "plan_path": plan_path,
                    "workflow": "Managed",
                    "team": "No team — global roles",
                    "max_turns": "",
                }, before_review_click
                if before_review_click["latest_preflight_readiness"] and before_review_click["latest_preflight_readiness"].get("requires_confirmation"):
                    assert before_review_click["launch_identity"]["dirty_worktree_confirmed"] is True, before_review_click
                assert before_review_click["review_region_present"] is False, before_review_click
                review_button.click()
                expect(review_region).to_be_visible(timeout=30_000)
                after_review_click = trace_dom("review-click-after")
                assert after_review_click["review_region_visible"] is True, after_review_click
                stable_choices = ("plan_path", "workflow", "team", "team_stage", "max_turns", "start_step")
                assert {key: after_review_click["launch_identity"][key] for key in stable_choices} == {
                    key: before_review_click["launch_identity"][key] for key in stable_choices
                }, {
                    "before": before_review_click["launch_identity"],
                    "after": after_review_click["launch_identity"],
                }
            except Exception as review_error:
                failure_stem = f"cp8-review-start-failure-{theme}-{width}x{height}"
                failure_screenshot = _fidelity_artifact_dir(tmp_path) / f"{failure_stem}.png"
                failure_manifest = _fidelity_artifact_dir(tmp_path) / f"{failure_stem}.json"
                failure_screenshot.parent.mkdir(parents=True, exist_ok=True)
                screenshot_error = None
                try:
                    page.screenshot(path=str(failure_screenshot), full_page=True, timeout=10_000)
                except Exception as error:
                    screenshot_error = type(error).__name__
                evidence = {
                    "error_type": type(review_error).__name__,
                    "error": str(review_error).splitlines()[0][:240],
                    "before_click": before_review_click,
                    "after_click": trace_dom("review-click-failure"),
                    "trace": list(preflight_trace),
                    "start_call_count": len(units.start_calls),
                    "screenshot": failure_screenshot.name if screenshot_error is None else None,
                    "screenshot_error": screenshot_error,
                }
                failure_manifest.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                print("AFLOW_CP8_REVIEW_START_FAILURE", json.dumps(evidence, sort_keys=True))
                raise
            captures["states"]["review_start_transition"] = {
                "before_click": before_review_click,
                "after_click": after_review_click,
                "preflight_trace": list(preflight_trace),
            }
            expect(review_region).to_contain_text("Read-only check — no run has been allocated.")
            expect(review_region).to_contain_text("Ready for the final start action.")
            expect(review_region).to_contain_text(plan_path)
            expect(review_region).to_contain_text("Managed")
            expect(review_region).to_contain_text("no team — global role assignments apply")
            expect(review_region).to_contain_text("15 · server default")
            expect(review_region.locator(".launch-review-consequence")).to_be_visible()
            review_heading = review_region.locator("h4.launch-review-heading")
            expect(review_heading).to_be_visible()
            expect(review_heading).to_have_attribute("tabindex", "-1")
            expect(review_heading).to_be_focused()
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
                "focused_heading": review_heading.evaluate("element => document.activeElement === element"),
                "consequence_visible": review_region.locator(".launch-review-consequence").is_visible(),
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
        pytest.param(1280, 720, "dark", id="desktop-dark"),
        pytest.param(390, 844, "light", id="mobile-light"),
        pytest.param(390, 844, "dark", id="mobile-dark"),
    ),
)
def test_ui_demo_cp9_settings_effective_values_and_disclosures(
    control_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Capture each populated Settings section against the frozen surface."""
    _, root, _, _ = control_client
    _seed_team_family_fixture(root)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.fail("The CP9 capture requires the real built web app; run the web build first.")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    manifest = load_reference_manifest()
    captures: dict[str, object] = {
        "reference_sha256": manifest["demo_sha256"],
        "viewport": {"width": width, "height": height},
        "theme": theme,
        "sections": {},
    }
    sections = ("Teams", "Agents & Roles", "Workflows", "Prompts", "Skills", "General")
    sidebar_sections = {"Teams", "Workflows", "Prompts", "Skills"}

    def visible_box(selector: str) -> dict[str, float] | None:
        return page.evaluate(
            """selector => {
              const element = document.querySelector(selector);
              if (!element) return null;
              const style = getComputedStyle(element);
              const box = element.getBoundingClientRect();
              if (style.display === 'none' || style.visibility === 'hidden' || box.width <= 0 || box.height <= 0) return null;
              return {x: box.x, y: box.y, width: box.width, height: box.height};
            }""",
            selector,
        )

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            reference_path = tmp_path / f"cp9-reference-{theme}-{width}x{height}.png"
            reference = capture_reference_surface(
                page,
                width=width,
                height=height,
                theme=theme,
                screenshot_path=reference_path,
            )
            _assert_reference_capture(reference)
            captures["reference"] = {"screenshot": reference_path.name, "anchors": reference["anchors"]}

            page.goto(f"{url}/?project={PROJECT_ID}&view=settings", wait_until="load")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            for section in sections:
                _select_settings_section(page, section)
                panel = page.locator("#settings-domain-panel")
                panel.wait_for()
                expect(panel).to_have_attribute("data-settings-tab", section)
                if section in sidebar_sections and width < 960 and section != "Teams":
                    navigation = page.locator(".sidebar-editor-navigation:visible").last
                    navigation.wait_for()
                    entry = navigation.locator("[data-sidebar-editor-item]:visible").first
                    if entry.count():
                        entry.click()
                if section == "Skills" and width >= 960:
                    page.locator(".sidebar-editor-navigation:visible [data-sidebar-editor-item]").first.wait_for()
                _assert_theme(page, theme)
                _assert_header_and_flow(page)
                save = page.get_by_role("button", name="Save all changes", exact=True)
                expect(save).to_be_visible()
                first_disclosure = panel.locator("details:visible").first
                disclosure_count = panel.locator("details").count()
                first_summary = (
                    first_disclosure.locator(":scope > summary").text_content()
                    if disclosure_count and first_disclosure.locator(":scope > summary").count()
                    else None
                )
                image = tmp_path / f"cp9-settings-{section.lower().replace(' & ', '-')}-{theme}-{width}x{height}.png"
                page.screenshot(path=str(image), full_page=True)
                content_box = visible_box("#settings-domain-panel")
                save_box = visible_box("[data-ui-fidelity-anchor='settings-save']")
                assert content_box is not None
                assert save_box is not None
                assert content_box["y"] >= save_box["y"]
                if section == "Teams":
                    expect(panel.locator(".team-family-list-entry").first).to_be_visible()
                    expect(panel.get_by_text("Add standalone team", exact=True)).to_be_visible()
                elif section == "Agents & Roles":
                    expect(panel.get_by_role("heading", name="Profiles", exact=True)).to_be_visible()
                    expect(panel.locator("details.settings-disclosure > summary", has_text="Add profile")).to_be_visible()
                    expect(panel.get_by_role("heading", name="Global roles", exact=True)).to_be_visible()
                elif section == "Workflows":
                    expect(panel.locator(".sidebar-editor-detail:visible").first).to_be_visible()
                elif section == "Prompts":
                    expect(panel.locator("details.settings-disclosure > summary", has_text="Create prompt")).to_be_visible()
                    expect(panel.locator(".text-editor:visible").first).to_be_visible()
                elif section == "Skills":
                    expect(panel.locator(".skill-editor-header:visible").first).to_be_visible()
                elif section == "General":
                    expect(panel.get_by_role("heading", name="Server settings", exact=True)).to_be_visible()
                    expect(panel.get_by_text("Change password", exact=True)).to_be_visible()
                captures["sections"][section] = {
                    "screenshot": image.name,
                    "save_enabled": not save.is_disabled(),
                    "content_box": content_box,
                    "save_box": save_box,
                    "disclosure_count": disclosure_count,
                    "first_disclosure": first_summary,
                    "settings_tab": panel.get_attribute("data-settings-tab"),
                }
        finally:
            browser.close()
    _write_artifact_manifest(tmp_path / f"cp9-settings-{theme}-{width}x{height}.json", captures)


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
            first_disclosure.locator(":scope > summary").click()
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

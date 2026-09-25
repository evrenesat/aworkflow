"""Frozen reference and production capture harness smoke tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

from aflow.control_plane.units import InMemoryUnitManager, UnitState
from test_control_plane_api import PROJECT_ID, control_client, live_server  # noqa: F401
from test_responsive_browser import _assert_header_and_flow, _assert_theme, _browser, _login, _open_destination, _seed_team_family_fixture, _select_settings_section, _set_theme_preference
from ui_demo_fidelity import (
    CAPTURE_THEMES,
    CAPTURE_VIEWPORTS,
    PRODUCTION_ANCHORS,
    PRODUCTION_PROGRESS_SURFACE,
    capture_dom_identity,
    capture_refresh_probe,
    capture_reference_surface,
    install_mutation_observer,
    install_refresh_probe,
    load_reference_manifest,
    measure_named_anchors,
    read_mutation_records,
    read_refresh_probe_mutations,
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
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Capture populated project and All runs surfaces at reference breakpoints."""
    _, root, _, _ = control_client
    seed_demo_fidelity_fixture(root)
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
    control_client,  # noqa: F811
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
        page.expose_function(
            "__aflowCp8NamedRoutesSettled",
            lambda indexes: all(
                any(event["route_index"] == index for event in routed_terminals)
                for index in indexes
            ),
        )
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
            sections = plan_list.locator(":scope > section")
            expect(sections.locator("h3")).to_have_text(["Draft", "Ready", "Done"])
            expect(sections.nth(0).locator("button.content-button")).to_have_count(2)
            expect(sections.nth(1).locator("button.content-button")).to_have_count(2)
            expect(sections.nth(2).locator("button.content-button")).to_have_count(1)
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
                    try:
                        held_preflight_routes[0].continue_()
                    except PlaywrightError:
                        # A newer inspection can cancel the held request.
                        pass
                    held_route_released = True
                    page.unroute("**/runs/preflight", hold_preflight)
                    route_registered = False
                    named_route_indexes = [0, changed_route_index, restored_route_index]
                    page.wait_for_function(
                        "indexes => window.__aflowCp8NamedRoutesSettled(indexes)",
                        arg=named_route_indexes,
                    )
                    outcomes = routed_outcome_records()
                    assert len(outcomes) == len(routed_preflights), outcomes
                    assert outcomes[0]["identity"] == held_identity, outcomes
                    assert outcomes[changed_route_index]["identity"] == preflight_identity(changed_response.request), outcomes
                    assert outcomes[restored_route_index]["identity"] == held_identity, outcomes
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
                                try:
                                    held_route.continue_()
                                except PlaywrightError:
                                    # Preserve the original failure if the request was cancelled.
                                    pass
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
    ("width", "height", "theme", "text_scale"),
    (
        pytest.param(1280, 720, "light", 1, id="desktop-light"),
        pytest.param(1280, 720, "dark", 1, id="desktop-dark"),
        pytest.param(390, 844, "light", 1, id="mobile-light"),
        pytest.param(390, 844, "dark", 1, id="mobile-dark"),
        pytest.param(320, 568, "light", 1, id="small-mobile-light"),
        pytest.param(844, 390, "dark", 1, id="mobile-landscape-dark"),
        pytest.param(1440, 900, "light", 1, id="wide-desktop-light"),
        pytest.param(390, 420, "dark", 1, id="short-mobile-dark"),
        pytest.param(390, 844, "light", 1.25, id="mobile-enlarged-text-light"),
    ),
)
def test_ui_demo_cp9_settings_effective_values_and_disclosures(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
    text_scale: float,
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
        "text_scale": text_scale,
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
        page_errors: list[str] = []
        writes: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("request", lambda request: writes.append(f"{request.method} {request.url}") if request.method in {"PATCH", "PUT", "DELETE"} else None)
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
            # The frozen file has a separate Settings screen. Capture its
            # matching section before navigating to the authenticated app.
            page.locator('[data-view="settings"]').evaluate("element => element.click()")
            page.locator("#af-frame .af-footer").evaluate("element => { element.style.display = 'none' }")
            reference_sections = {}
            for section in sections:
                page.locator(f'[data-setting="{section}"]').evaluate("element => element.click()")
                reference_section_path = tmp_path / f"cp9-reference-settings-{section.lower().replace(' & ', '-')}-{theme}-{width}x{height}.png"
                page.locator("#af-frame").screenshot(path=str(reference_section_path))
                reference_sections[section] = reference_section_path.name
            captures["reference_sections"] = reference_sections

            page.goto(f"{url}/?project={PROJECT_ID}&view=settings", wait_until="load")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            if text_scale != 1:
                page.evaluate("scale => { document.documentElement.style.fontSize = `${scale * 100}%` }", text_scale)
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
                install_refresh_probe(page, {"panel": "#settings-domain-panel"})
                assert capture_refresh_probe(page)["roots"]["panel"]["sameNode"]
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
                navigation_control = page.get_by_role("combobox", name="Settings section", exact=True) if width < 1200 else page.get_by_role("tab", name=section, exact=True)
                navigation_control.focus()
                expect(navigation_control).to_be_focused()
            assert not page_errors, page_errors
            assert not writes, writes
        finally:
            browser.close()
    _write_artifact_manifest(tmp_path / f"cp9-settings-{theme}-{width}x{height}.json", captures)


@pytest.mark.parametrize(
    ("width", "height", "theme", "text_scale"),
    (
        pytest.param(1280, 720, "light", 1, id="desktop-light"),
        pytest.param(1280, 720, "dark", 1, id="desktop-dark"),
        pytest.param(390, 844, "light", 1, id="mobile-light"),
        pytest.param(390, 844, "dark", 1, id="mobile-dark"),
        pytest.param(320, 568, "light", 1, id="small-mobile-light"),
        pytest.param(844, 390, "dark", 1, id="mobile-landscape-dark"),
        pytest.param(1440, 900, "light", 1, id="wide-desktop-light"),
        pytest.param(390, 420, "dark", 1, id="short-mobile-dark"),
        pytest.param(390, 844, "light", 1.25, id="mobile-enlarged-text-light"),
    ),
)
def test_ui_demo_cp9_combined_six_screen_matrix(
    control_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    width: int, height: int, theme: str, text_scale: float,
) -> None:
    """Compare six populated app screens to their frozen-demo counterparts."""
    _, root, units, _ = control_client
    fixtures = seed_demo_fidelity_fixture(root)
    _prepare_disposable_fidelity_config(root, monkeypatch)
    running = fixtures["running"]
    assert isinstance(running, dict)
    units.units[f"aflow-run-{running['run_id']}.service"] = UnitState(
        name=f"aflow-run-{running['run_id']}.service", active_state="active", sub_state="running",
    )
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    plan_name = Path(running["plan"]).name
    snapshots: dict[str, object] = {"viewport": [width, height], "theme": theme,
                                    "text_scale": text_scale, "demo_sha256": load_reference_manifest()["demo_sha256"],
                                    "screens": {}}
    screens = (("Runs", "runs", ".run-detail"),
               ("All runs", "all", ".global-run-results"),
               ("Projects", "projects", ".project-picker"),
               ("Settings", "settings", "#settings-domain-panel"),
               ("Plans", "plans", ".plan-editor"),
               ("New run", "new", "section.card.start-run-form"))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page_errors: list[str] = []
        writes: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("request", lambda request: writes.append(f"{request.method} {urlsplit(request.url).path}")
                if request.method in {"PATCH", "PUT", "DELETE"}
                or (request.method == "POST" and urlsplit(request.url).path.endswith("/runs")) else None)
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            capture_reference_surface(page, width=width, height=height, theme=theme,
                                      screenshot_path=tmp_path / f"cp9-six-demo-runs-{theme}-{width}x{height}.png")
            page.locator("#af-frame .af-footer").evaluate("element => { element.style.display = 'none' }")
            for name, demo_view, _ in screens:
                demo_button = (page.locator('[data-page="plans"] button[data-view="new"]').first
                               if demo_view == "new" else page.locator(f'#af-nav [data-view="{demo_view}"]'))
                demo_button.evaluate("element => element.click()")
                reference_image = tmp_path / f"cp9-six-demo-{demo_view}-{theme}-{width}x{height}.png"
                page.locator("#af-frame").screenshot(path=str(reference_image))
                snapshots["screens"][name] = {"demo": reference_image.name}

            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={running['run_id']}", wait_until="load")
            if text_scale != 1:
                page.evaluate("scale => { document.documentElement.style.fontSize = `${scale * 100}%` }", text_scale)
            for name, _, selector in screens:
                if name == "All runs":
                    _open_destination(page, "All runs")
                    expect(page.locator(".global-run-row")).to_have_count(3)
                elif name in {"Projects", "Settings", "Plans"}:
                    _open_destination(page, name)
                if name == "Plans":
                    ready = page.locator(".plan-list > section").nth(1).locator("button.content-button").filter(has_text=plan_name).first
                    ready.click()
                    page.get_by_label("Plan content", exact=True).wait_for()
                elif name == "New run":
                    page.get_by_role("button", name="More", exact=True).click()
                    page.get_by_role("menuitem", name="Configure run…", exact=True).click()
                    page.get_by_label("Run plan", exact=True).wait_for()
                if name == "Settings":
                    _select_settings_section(page, "Agents & Roles")
                page.locator(selector).wait_for(state="visible")
                _assert_theme(page, theme)
                _assert_header_and_flow(page)
                install_refresh_probe(page, {"screen": selector})
                probe = capture_refresh_probe(page)
                assert probe["roots"]["screen"]["sameNode"]
                assert probe["roots"]["screen"]["text"].strip()
                if name == "Runs":
                    expect(page.locator(".run-detail:visible").first).to_contain_text(plan_name)
                elif name == "Projects":
                    expect(page.locator("ul[aria-label='Added projects'] > li")).not_to_have_count(0)
                elif name == "Plans":
                    expect(page.get_by_label("Plan content", exact=True)).to_be_visible()
                elif name == "New run":
                    expect(page.get_by_label("Run plan", exact=True)).not_to_have_value("")
                image = tmp_path / f"cp9-six-app-{name.lower().replace(' ', '-')}-{theme}-{width}x{height}.png"
                page.screenshot(path=str(image), full_page=True)
                snapshots["screens"][name]["app"] = image.name
                snapshots["screens"][name]["probe"] = {"token": probe["roots"]["screen"]["token"],
                                                          "box": probe["roots"]["screen"]["box"]}
                focus_target = page.get_by_role("button", name="Menu", exact=True) if width < 960 or height < 600 else page.get_by_role("button", name="More", exact=True)
                if focus_target.count() and focus_target.is_visible() and focus_target.is_enabled():
                    focus_target.focus()
                    expect(focus_target).to_be_focused()
                if name == "Projects":
                    page.locator("ul[aria-label='Added projects'] .compact-row-main").first.click()
                    page.wait_for_function("() => document.querySelector('.app-header')?.textContent?.includes('Test project')")
            assert not page_errors, page_errors
            assert not writes, writes
        finally:
            browser.close()
    _write_artifact_manifest(tmp_path / f"cp9-six-screens-{theme}-{width}x{height}.json", snapshots)


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    (
        pytest.param(1280, 720, "light", id="desktop-light"),
        pytest.param(390, 844, "dark", id="mobile-dark"),
    ),
)
def test_ui_demo_equal_refresh_retains_detail_and_changed_rows_update(
    control_client,  # noqa: F811
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


@pytest.mark.parametrize("variant", ("running", "paused"))
@pytest.mark.parametrize(
    ("width", "height", "theme"),
    ((1280, 720, "light"), (390, 844, "dark")),
)
def test_ui_demo_selected_run_background_refresh_probe(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
    width: int,
    height: int,
    theme: str,
) -> None:
    """The real event refresh holds list, status, events and context in place."""
    _, root, units, _ = control_client
    selected = seed_demo_fidelity_fixture(root)[variant]
    assert isinstance(selected, dict)
    if variant == "running":
        assert isinstance(units, InMemoryUnitManager)
        unit_name = f"aflow-run-{selected['run_id']}.service"
        units.units[unit_name] = UnitState(name=unit_name, active_state="active", sub_state="running")
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    base = f"/api/control-plane/projects/{PROJECT_ID}/runs"
    selected_path = f"{base}/{selected['run_id']}"
    phase = {"value": "initial"}
    held: list[tuple[object, bytes, int, dict[str, str]]] = []
    requested: list[str] = []
    writes: list[str] = []
    page_errors: list[str] = []

    def intercept(route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        if request.method != "GET" or not (path == base or path in {
            selected_path, f"{selected_path}/events", f"{selected_path}/context"
        }):
            route.continue_()
            return
        if phase["value"] == "initial":
            route.continue_()
            return
        requested.append(path)
        if phase["value"] == "failure" and path == selected_path:
            route.fulfill(status=503, content_type="application/json", body='{"detail":"read unavailable"}')
            return
        response = route.fetch()
        if phase["value"] == "equal":
            held.append((route, response.body(), response.status, response.headers))
        elif phase["value"] == "changed" and path == f"{selected_path}/events":
            payload = json.loads(response.body())
            payload["events"].append({
                "sequence": max((event["sequence"] for event in payload["events"]), default=0) + 1,
                "event_type": "checkpoint_progress_recorded",
                "schema_version": 1,
                "timestamp": "2026-09-25T17:00:00Z",
                "data": {"summary": "Refresh probe changed event"},
            })
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        else:
            route.fulfill(status=response.status, headers=response.headers, body=response.body())

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        # The dashboard uses POST /config/form for a read-only projection.
        page.on("request", lambda request: writes.append(request.url) if phase["value"] != "initial" and request.method not in {"GET", "OPTIONS"} and "/api/" in request.url and not urlsplit(request.url).path.endswith("/config/form") else None)
        page.route("**/api/control-plane/projects/*/runs**", intercept)
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={selected['run_id']}")
            detail = page.locator(".run-detail:visible").first
            detail.wait_for()
            if variant == "running":
                expect(detail).to_contain_text("Running")
            disclosure = detail.locator("details[data-ui-fidelity-anchor='first-disclosure']")
            disclosure.locator(":scope > summary").click()
            expect(disclosure).to_have_attribute("open", "")
            actions = detail.get_by_role("button", name="Actions", exact=True)
            actions.focus()
            page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight - innerHeight, 180))")
            # Progress, context and diagnostics settle on independent reads.
            # Do not attribute their initial paint to a later refresh.
            page.wait_for_timeout(2_500)
            install_refresh_probe(page, {
                "detail": ".run-detail",
                "disclosure": "details[data-ui-fidelity-anchor='first-disclosure']",
            })
            before = capture_refresh_probe(page)
            assert before["roots"]["disclosure"]["open"] is True
            # A viewport screenshot does not resize a long page, which would
            # itself toggle the compact Back control during observation.
            page.screenshot(path=str(tmp_path / f"selected-{variant}-before-{theme}-{width}x{height}.png"))
            phase["value"] = "equal"
            page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            for _ in range(100):
                if any(urlsplit(route.request.url).path == base for route, *_ in held):
                    break
                page.wait_for_timeout(25)
            assert held, "background run list read was not held"
            held_snapshot = capture_refresh_probe(page)
            assert held_snapshot["roots"]["detail"]["sameNode"] is True
            assert held_snapshot["roots"]["disclosure"]["sameNode"] is True
            assert held_snapshot["roots"]["disclosure"]["open"] is True
            assert held_snapshot["roots"]["disclosure"]["box"] == before["roots"]["disclosure"]["box"]
            assert held_snapshot["roots"]["disclosure"]["disclosures"] == before["roots"]["disclosure"]["disclosures"]
            assert held_snapshot["focused"] == before["focused"]
            assert held_snapshot["scrollY"] == before["scrollY"]
            held_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in held_mutations if item.get("classification") in {"subtree", "root-replacement"}], held_mutations
            while held:
                route, body, status, headers = held.pop(0)
                route.fulfill(status=status, headers=headers, body=body)
                page.wait_for_timeout(40)
                if selected_path in requested and f"{selected_path}/events" in requested and f"{selected_path}/context" in requested:
                    break
            for _ in range(100):
                if selected_path in requested and f"{selected_path}/events" in requested and f"{selected_path}/context" in requested:
                    break
                page.wait_for_timeout(25)
            assert {base, selected_path, f"{selected_path}/events", f"{selected_path}/context"}.issubset(requested)
            phase["value"] = "released"
            while held:
                route, body, status, headers = held.pop(0)
                route.fulfill(status=status, headers=headers, body=body)
            page.wait_for_timeout(250)
            after_equal = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after_equal["roots"].values())
            assert after_equal["roots"]["disclosure"]["open"] is True
            assert after_equal["roots"]["disclosure"]["box"] == before["roots"]["disclosure"]["box"]
            assert after_equal["roots"]["disclosure"]["text"] == before["roots"]["disclosure"]["text"]
            assert after_equal["focused"] == before["focused"]
            assert after_equal["scrollY"] == before["scrollY"]
            equal_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in equal_mutations if item.get("classification") in {"subtree", "root-replacement"}], equal_mutations
            page.screenshot(path=str(tmp_path / f"selected-{variant}-equal-{theme}-{width}x{height}.png"))

            phase["value"] = "changed"
            requested.clear()
            for _ in range(5):
                page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
                for _ in range(30):
                    if f"{selected_path}/events" in requested:
                        break
                    page.wait_for_timeout(50)
                if f"{selected_path}/events" in requested:
                    break
            assert f"{selected_path}/events" in requested, requested
            expect(detail).to_contain_text("Refresh probe changed event")
            after_changed = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after_changed["roots"].values())
            assert after_changed["roots"]["detail"]["text"] != after_equal["roots"]["detail"]["text"]
            assert after_changed["roots"]["disclosure"]["open"] is True
            assert after_changed["focused"] == before["focused"]
            assert after_changed["scrollY"] == before["scrollY"]
            page.wait_for_timeout(300)

            phase["value"] = "failure"
            requested.clear()
            for _ in range(5):
                page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
                for _ in range(30):
                    if selected_path in requested:
                        break
                    page.wait_for_timeout(50)
                if selected_path in requested:
                    break
            assert selected_path in requested, requested
            expect(page.get_by_role("alert").filter(has_text="read unavailable")).to_be_visible()
            after_failure = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after_failure["roots"].values())
            assert after_failure["roots"]["disclosure"]["text"] == after_changed["roots"]["disclosure"]["text"]
            assert disclosure.get_attribute("open") == ""
            assert actions.is_visible()
            assert after_failure["focused"] == before["focused"]
            assert after_failure["scrollY"] == before["scrollY"]
            assert page_errors == []
            assert writes == []
            _write_artifact_manifest(tmp_path / f"selected-{variant}-{theme}-{width}x{height}.json", {
                "before": before, "held": held_snapshot, "equal": after_equal,
                "changed": after_changed,
                "failure": after_failure, "held_mutations": held_mutations,
                "equal_mutations": equal_mutations,
                "requested": requested, "page_errors": page_errors, "writes": writes,
            })
        finally:
            for route, body, status, headers in held:
                try:
                    route.fulfill(status=status, headers=headers, body=body)
                except Exception:
                    pass
            page.unroute_all(behavior="ignoreErrors")
            browser.close()


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    ((1280, 720, "dark"), (390, 844, "light")),
)
def test_ui_demo_all_runs_background_refresh_probe(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Visibility/event refresh traverses cursors and retains partial rows."""
    _, root, _, _ = control_client
    first = seed_demo_fidelity_fixture(root)
    second_id = "refresh-second"
    second_root = tmp_path / second_id
    second_root.mkdir()
    subprocess.run(("git", "init", "-q", str(second_root)), check=True)
    second = seed_demo_fidelity_fixture(second_root)
    changed_run_id = second["running"]["run_id"]
    # The real history API must issue a second page. These are disposable
    # records; the approved reference and any live run artifacts stay intact.
    for index in range(101):
        run_dir = second_root / ".aflow" / "runs" / f"refresh-history-{index:03}"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({
            "status": "completed", "activity": "inactive",
            "plan_path": second["completed"]["plan"],
            "run_started_at": f"2026-09-20T10:{index % 60:02}:00Z",
        }))
    from aflow_app_server import main
    assert main._project_registry is not None
    main._project_registry.register(second_id, "Second project", second_id)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    base = "/api/control-plane/projects/"
    phase = {"value": "initial"}
    held: list[tuple[object, bytes, int, dict[str, str]]] = []
    requests: list[str] = []
    detail_requests: list[str] = []
    changed_snapshot: dict[str, object] = {}
    writes: list[str] = []
    page_errors: list[str] = []

    def row_selector(project_id: str, run_id: str) -> str:
        key = json.dumps([project_id, run_id], separators=(",", ":"))
        return f".global-run-row[data-run-key={json.dumps(key)}]"

    def intercept(route) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        if request.method == "GET" and parsed.path == f"{base}{second_id}/runs/{changed_run_id}":
            response = route.fetch()
            payload = json.loads(response.body())
            if phase["value"] == "changed":
                payload.update({key: value for key, value in changed_snapshot.items() if key != "progress"})
                progress = payload.get("progress")
                if progress and progress.get("approved_checkpoints", {}).get("value") is not None:
                    progress["approved_checkpoints"]["value"] += 1
            route.fulfill(status=response.status, headers=response.headers, body=json.dumps(payload).encode())
            return
        if request.method != "GET" or not re.fullmatch(rf"{base}[^/]+/runs", parsed.path):
            route.continue_()
            return
        requests.append(request.url)
        project_id = parsed.path.split("/")[-2]
        if phase["value"] == "failed" and project_id == second_id:
            route.fulfill(status=503, content_type="application/json", body='{"detail":"project unavailable"}')
            return
        response = route.fetch()
        body = response.body()
        if phase["value"] == "held" and project_id == second_id and "cursor=" not in parsed.query:
            held.append((route, body, response.status, response.headers))
            return
        if phase["value"] == "changed" and project_id == second_id:
            payload = json.loads(body)
            for run in payload["runs"]:
                if run["run_id"] == changed_run_id:
                    run.update(turns_completed=(run.get("turns_completed") or 0) + 1)
                    changed_snapshot.clear()
                    changed_snapshot.update(run)
            body = json.dumps(payload).encode()
        route.fulfill(status=response.status, headers=response.headers, body=body)

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("request", lambda request: detail_requests.append(request.url) if request.method == "GET" and re.fullmatch(rf"{base}[^/]+/runs/[^/]+", urlsplit(request.url).path) else None)
        # Treat the read-only form projection separately from actual writes.
        page.on("request", lambda request: writes.append(request.url) if phase["value"] != "initial" and request.method not in {"GET", "OPTIONS"} and "/api/" in request.url and not urlsplit(request.url).path.endswith("/config/form") else None)
        page.route("**/api/control-plane/projects/*/runs?*", intercept)
        page.route(f"**/api/control-plane/projects/{second_id}/runs/{changed_run_id}", intercept)
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            page.goto(f"{url}/?view=all-runs")
            page.get_by_role("heading", name="All runs", exact=True).wait_for()
            first_selector = row_selector(PROJECT_ID, first["paused"]["run_id"])
            second_selector = row_selector(second_id, changed_run_id)
            first_row = page.locator(first_selector)
            second_row = page.locator(second_selector)
            expect(first_row).to_be_visible()
            expect(second_row).to_be_visible()
            expect(page.locator(".global-run-row[data-enrichment-state='loading']:visible")).to_have_count(0, timeout=15_000)
            assert any("cursor=" in url for url in requests), "second project never traversed its next cursor"
            toggle = first_row.locator(".run-row-preview-toggle")
            toggle.click()
            expect(first_row.get_by_role("dialog")).to_be_visible()
            toggle.focus()
            page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight - innerHeight, 180))")
            page.mouse.move(1, 1)
            page.wait_for_timeout(350)
            install_refresh_probe(page, {
                "results": ".global-run-results",
                "selected_row": first_selector,
                "preview": f"{first_selector} .run-row-preview",
                "changed_row": second_selector,
            })
            before = capture_refresh_probe(page)
            row_order = page.locator(".global-run-row").evaluate_all("rows => rows.map(row => row.getAttribute('data-run-key'))")
            page.screenshot(path=str(tmp_path / f"all-runs-before-{theme}-{width}x{height}.png"))
            requests.clear()
            detail_requests.clear()
            phase["value"] = "held"
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(25)
            assert held, "visibility refresh did not reach the second project"
            during = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in during["roots"].values())
            assert page.locator(".global-run-row").evaluate_all("rows => rows.map(row => row.getAttribute('data-run-key'))") == row_order
            assert during["roots"]["selected_row"]["box"] == before["roots"]["selected_row"]["box"]
            assert during["roots"]["preview"]["box"] == before["roots"]["preview"]["box"]
            assert during["focused"] == before["focused"]
            assert during["scrollY"] == before["scrollY"]
            held_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in held_mutations if item.get("classification") in {"subtree", "root-replacement"}], held_mutations
            route, body, status, headers = held.pop()
            route.fulfill(status=status, headers=headers, body=body)
            page.wait_for_timeout(250)
            equal = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in equal["roots"].values())
            assert page.locator(".global-run-row").evaluate_all("rows => rows.map(row => row.getAttribute('data-run-key'))") == row_order
            assert equal["roots"]["selected_row"]["box"] == before["roots"]["selected_row"]["box"]
            assert equal["roots"]["preview"]["box"] == before["roots"]["preview"]["box"]
            assert equal["roots"]["selected_row"]["text"] == before["roots"]["selected_row"]["text"]
            assert equal["roots"]["preview"]["text"] == before["roots"]["preview"]["text"]
            assert equal["focused"] == before["focused"]
            assert equal["scrollY"] == before["scrollY"]
            assert detail_requests == [], detail_requests
            equal_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in equal_mutations if item.get("classification") in {"subtree", "root-replacement"}], equal_mutations
            page.screenshot(path=str(tmp_path / f"all-runs-equal-{theme}-{width}x{height}.png"))

            phase["value"] = "changed"
            requests.clear()
            page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            for _ in range(100):
                if detail_requests:
                    break
                page.wait_for_timeout(50)
            expect(second_row).to_have_attribute("data-enrichment-state", "settled", timeout=15_000)
            assert len(detail_requests) == 1 and detail_requests[0].endswith(f"/{changed_run_id}"), detail_requests
            changed = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in changed["roots"].values()), [name for name, root in changed["roots"].items() if not root["sameNode"]]
            assert changed["roots"]["selected_row"]["text"] == before["roots"]["selected_row"]["text"]
            assert changed["roots"]["changed_row"]["text"] != before["roots"]["changed_row"]["text"]
            assert "2/10 approved" in changed["roots"]["changed_row"]["text"]
            assert changed["roots"]["preview"]["text"] == before["roots"]["preview"]["text"]
            assert changed["focused"] == before["focused"]
            assert changed["scrollY"] == before["scrollY"]
            changed_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in changed_mutations if item.get("classification") == "root-replacement"], changed_mutations
            assert not [item for item in changed_mutations if item.get("root") in {"selected_row", "preview"}], changed_mutations
            page.screenshot(path=str(tmp_path / f"all-runs-changed-{theme}-{width}x{height}.png"))

            for _ in range(100):
                if any("cursor=" in request and f"/{second_id}/runs" in request for request in requests):
                    break
                page.wait_for_timeout(25)
            assert any("cursor=" in request and f"/{second_id}/runs" in request for request in requests)
            requests.clear()
            for _ in range(240):
                if any(f"/{second_id}/runs" in request for request in requests):
                    break
                page.wait_for_timeout(50)
            assert any(f"/{second_id}/runs" in request for request in requests), "10-second background timer did not refresh All runs"
            after_timer = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after_timer["roots"].values())
            assert after_timer["roots"]["selected_row"]["text"] == before["roots"]["selected_row"]["text"]
            assert after_timer["focused"] == before["focused"]
            assert len(detail_requests) == 1, detail_requests
            refresh_detail_requests = detail_requests.copy()
            for _ in range(100):
                if any("cursor=" in request and f"/{second_id}/runs" in request for request in requests):
                    break
                page.wait_for_timeout(25)
            assert any("cursor=" in request and f"/{second_id}/runs" in request for request in requests)
            page.wait_for_timeout(250)

            phase["value"] = "failed"
            page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            expect(page.get_by_role("alert").filter(has_text="Partial or stale results")).to_be_visible()
            failed = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in failed["roots"].values())
            assert failed["focused"] == before["focused"]
            assert failed["scrollY"] == before["scrollY"]
            expect(second_row).to_have_attribute("data-enrichment-state", "settled")
            assert first_row.get_by_role("dialog").is_visible()
            assert page_errors == []
            assert writes == []
            page.keyboard.press("Escape")
            expect(first_row.get_by_role("dialog")).to_have_count(0)
            first_row.locator(".run-list-select").click()
            expect(page.locator(".run-detail:visible").first).to_contain_text(first["paused"]["title"])
            assert f"run={first['paused']['run_id']}" in page.url
            assert page_errors == []
            assert writes == []
            _write_artifact_manifest(tmp_path / f"all-runs-{theme}-{width}x{height}.json", {
                "before": before, "held": during, "equal": equal,
                "changed": changed, "timer": after_timer, "failed": failed,
                "held_mutations": held_mutations, "equal_mutations": equal_mutations,
                "changed_mutations": changed_mutations,
                "row_order": row_order,
                "navigated_run_id": first["paused"]["run_id"],
                "requests": requests, "refresh_detail_requests": refresh_detail_requests,
                "detail_requests": detail_requests,
                "page_errors": page_errors, "writes": writes,
            })
        finally:
            for route, body, status, headers in held:
                try:
                    route.fulfill(status=status, headers=headers, body=body)
                except Exception:
                    pass
            page.unroute_all(behavior="ignoreErrors")
            browser.close()


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    ((1280, 720, "light"), (390, 844, "dark"), (390, 420, "light")),
)
def test_ui_demo_projects_refresh_preserves_add_draft(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """A real registry read leaves a focused creation draft and selected row in place."""
    _, root, units, _ = control_client
    seed_demo_fidelity_fixture(root)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    artifact_dir = _fidelity_artifact_dir(tmp_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cp2-projects-{theme}-{width}x{height}"
    phase = {"value": "initial"}
    held: list[tuple[object, bytes, int, dict[str, str]]] = []
    read_phases: list[str] = []
    writes: list[str] = []
    page_errors: list[str] = []

    def intercept(route) -> None:
        request = route.request
        if request.method != "GET" or urlsplit(request.url).path != "/api/projects":
            route.continue_()
            return
        current = phase["value"]
        read_phases.append(current)
        if current == "initial":
            route.continue_()
        elif current == "failure":
            route.fulfill(status=503, content_type="application/json", body='{"detail":"Project read unavailable"}')
        else:
            response = route.fetch()
            body = response.body()
            if current == "changed":
                payload = json.loads(body)
                for project in payload:
                    if project["id"] == PROJECT_ID:
                        project["display_name"] = "Refresh changed project"
                body = json.dumps(payload).encode()
            if current == "equal":
                held.append((route, body, response.status, response.headers))
            else:
                route.fulfill(status=response.status, headers=response.headers, body=body)

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("request", lambda request: writes.append(request.url) if phase["value"] != "initial" and request.method not in {"GET", "OPTIONS"} and "/api/" in request.url else None)
        page.route("**/api/projects", intercept)
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            page.goto(f"{url}/?project={PROJECT_ID}&view=projects")
            row = page.locator("ul[aria-label='Added projects'] .compact-row-main[aria-pressed='true']").first
            row.wait_for()
            page.get_by_role("button", name="Add project", exact=True).click()
            form = page.get_by_role("form", name="Create or register a project")
            path = form.get_by_label("Relative project path")
            path.fill("draft/kept-project")
            name = form.get_by_label("Display name")
            name.fill("Unsaved project draft")
            name.focus()
            install_refresh_probe(page, {"picker": ".project-picker", "row": "ul[aria-label='Added projects'] .compact-row-main[aria-pressed='true']", "form": ".project-create-form", "draft": ".project-create-form input[aria-label='Display name']"})
            before = capture_refresh_probe(page)
            page.screenshot(path=str(artifact_dir / f"{stem}-before.png"), full_page=True)
            # Full-page capture may set temporary inline styles on inputs.
            # Drain that instrumentation before observing the network read.
            read_refresh_probe_mutations(page)

            def refresh() -> None:
                page.get_by_role("button", name="More", exact=True).click()
                page.get_by_role("menu", name="More project actions").get_by_role("menuitem", name="Refresh", exact=True).click()
                # Opening the explicit menu moves focus by design. Return to
                # the draft before judging what the asynchronous read did.
                name.focus()

            phase["value"] = "equal"
            refresh()
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(25)
            assert held, read_phases
            during = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in during["roots"].values()), during
            assert during["focused"] == before["focused"]
            held_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in held_mutations if item["root"] in {"form", "row"} and item["classification"] in {"subtree", "root-replacement", "visibility"}], held_mutations
            route, body, status, headers = held.pop()
            route.fulfill(status=status, headers=headers, body=body)
            page.wait_for_timeout(150)
            equal = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in equal["roots"].values()), equal
            assert equal["roots"]["form"]["text"] == before["roots"]["form"]["text"]
            assert equal["focused"] == before["focused"]
            assert name.input_value() == "Unsaved project draft"
            equal_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in equal_mutations if item["root"] in {"form", "row"} and item["classification"] in {"subtree", "root-replacement"}], equal_mutations
            page.screenshot(path=str(artifact_dir / f"{stem}-equal.png"), full_page=True)
            read_refresh_probe_mutations(page)

            phase["value"] = "changed"
            refresh()
            expect(row).to_contain_text("Refresh changed project")
            changed = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in changed["roots"].values()), changed
            assert changed["focused"] == before["focused"]
            assert name.input_value() == "Unsaved project draft"
            changed_mutations = read_refresh_probe_mutations(page)
            assert not [item for item in changed_mutations if item["root"] == "form" and item["classification"] in {"subtree", "root-replacement", "visibility", "value"}], changed_mutations
            page.screenshot(path=str(artifact_dir / f"{stem}-changed.png"), full_page=True)
            read_refresh_probe_mutations(page)

            phase["value"] = "failure"
            refresh()
            expect(page.get_by_role("alert").filter(has_text="Project read unavailable")).to_be_visible()
            failed = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in failed["roots"].values()), failed
            assert failed["focused"] == before["focused"]
            expect(row).to_contain_text("Refresh changed project")
            assert path.input_value() == "draft/kept-project"
            assert name.input_value() == "Unsaved project draft"
            page.set_viewport_size({"width": 390 if width >= 960 else 1280, "height": 844 if width >= 960 else 720})
            assert form.is_visible() and row.is_visible()
            assert row.get_attribute("aria-pressed") == "true"
            assert name.input_value() == "Unsaved project draft"
            assert page_errors == [] and writes == [] and units.start_calls == []
            page.screenshot(path=str(artifact_dir / f"{stem}-failure-resized.png"), full_page=True)
            _write_artifact_manifest(artifact_dir / f"{stem}.json", {"before": before, "held": during, "equal": equal, "changed": changed, "failure": failed, "held_mutations": held_mutations, "equal_mutations": equal_mutations, "changed_mutations": changed_mutations, "read_phases": read_phases, "page_errors": page_errors, "writes": writes})
        finally:
            for route, body, status, headers in held:
                try:
                    route.fulfill(status=status, headers=headers, body=body)
                except Exception:
                    pass
            page.unroute_all(behavior="ignoreErrors")
            browser.close()


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    ((1280, 720, "dark"), (390, 844, "light")),
)
def test_ui_demo_plan_save_followup_reads_preserve_new_draft(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """The real post-save plan-list and queue reads cannot replace a newer draft."""
    _, root, units, _ = control_client
    plan = root / "plans" / "in-progress" / "refresh-probe.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    initial_content = "# Refresh probe\n\n" + "A long editable line for scroll continuity.\n" * 40
    plan.write_text(initial_content, encoding="utf-8")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    artifact_dir = _fidelity_artifact_dir(tmp_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cp2-plans-{theme}-{width}x{height}"
    list_path = f"/api/projects/{PROJECT_ID}/plans"
    queue_path = f"/api/projects/{PROJECT_ID}/queue"
    phase = {"value": "initial"}
    held: list[tuple[object, str, int, bytes, dict[str, str]]] = []
    read_phases: list[tuple[str, str]] = []
    changed_list_sizes: list[int] = []
    writes: list[str] = []
    page_errors: list[str] = []

    def intercept(route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        if request.method != "GET" or path not in {list_path, queue_path}:
            route.continue_()
            return
        current = phase["value"]
        read_phases.append((current, path))
        if current == "initial":
            route.continue_()
            return
        response = route.fetch()
        body = response.body()
        if current == "changed":
            payload = json.loads(body)
            if path == queue_path:
                payload["plans"] = [item for item in payload["plans"] if item["path"] != "plans/in-progress/refresh-probe.md"]
                payload["plans"].append({
                    "name": "refresh-probe.md", "path": "plans/in-progress/refresh-probe.md",
                    "status": "in_progress", "identity": "refresh-probe", "outcome": "blocked",
                    "reason": "capacity", "dependency": None, "run_id": None,
                    "revision": "changed-queue-revision",
                })
            else:
                    for item in payload:
                        if item["name"] == "refresh-probe.md":
                            item["size_bytes"] += 1
                            changed_list_sizes.append(item["size_bytes"])
            body = json.dumps(payload).encode()
        held.append((route, path, 503 if current == "failure" else response.status,
                     b'{"detail":"Plan read unavailable"}' if current == "failure" else body,
                     response.headers))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("request", lambda request: writes.append(urlsplit(request.url).path) if phase["value"] != "initial" and request.method not in {"GET", "OPTIONS"} and "/api/" in request.url else None)
        page.route(f"**/api/projects/{PROJECT_ID}/plans", intercept)
        page.route(f"**/api/projects/{PROJECT_ID}/queue", intercept)
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            page.goto(f"{url}/?project={PROJECT_ID}&view=plans")
            page.get_by_role("button", name=re.compile("refresh-probe.md")).click()
            editor = page.get_by_label("Plan content", exact=True)
            expect(editor).to_have_value(initial_content)
            captures: dict[str, object] = {}

            for current in ("equal", "changed", "failure"):
                phase["value"] = current
                page.get_by_role("button", name="Save", exact=True).click()
                for _ in range(100):
                    if {path for _, path, *_ in held} == {list_path, queue_path}:
                        break
                    page.wait_for_timeout(25)
                assert {path for _, path, *_ in held} == {list_path, queue_path}, read_phases
                next_draft = editor.input_value() + f"\nUnsaved after {current} read."
                editor.fill(next_draft)
                editor.evaluate("node => { node.setSelectionRange(node.value.length - 8, node.value.length - 3); node.scrollTop = node.scrollHeight; node.focus(); }")
                install_refresh_probe(page, {"editor": ".plan-editor", "textarea": ".plan-editor-textarea"})
                before = capture_refresh_probe(page)
                selection = editor.evaluate("node => [node.selectionStart, node.selectionEnd, node.scrollTop]")
                page.screenshot(path=str(artifact_dir / f"{stem}-{current}-held.png"), full_page=True)
                read_refresh_probe_mutations(page)
                while held:
                    route, _, status, body, headers = held.pop(0)
                    route.fulfill(status=status, headers=headers, body=body)
                if current == "changed":
                    expect(page.get_by_text("Waiting for an implementation slot", exact=True)).to_be_visible()
                elif current == "failure":
                    expect(page.get_by_role("alert").filter(has_text="Plan read unavailable")).to_be_visible()
                    expect(page.get_by_role("alert").filter(has_text="Current queue reasons are unavailable")).to_be_visible()
                else:
                    page.wait_for_timeout(150)
                after = capture_refresh_probe(page)
                assert all(root["sameNode"] for root in after["roots"].values()), (current, after)
                assert after["focused"] == before["focused"]
                assert after["roots"]["textarea"]["scrollTop"] == before["roots"]["textarea"]["scrollTop"]
                assert editor.evaluate("node => [node.selectionStart, node.selectionEnd, node.scrollTop]") == selection
                assert editor.input_value() == next_draft
                mutations = read_refresh_probe_mutations(page)
                assert not [item for item in mutations if item["root"] == "textarea" and item["classification"] in {"subtree", "root-replacement", "value", "visibility"}], mutations
                if current == "equal":
                    assert not [item for item in mutations if item["root"] == "editor" and item["classification"] in {"subtree", "root-replacement", "visibility"}], mutations
                page.screenshot(path=str(artifact_dir / f"{stem}-{current}-settled.png"), full_page=True)
                captures[current] = {"held": before, "settled": after, "selection": selection, "mutations": mutations}

            assert writes.count(f"{list_path}/in_progress/refresh-probe.md") == 3
            # An external revision change is different from a passive read:
            # only the explicit discard action may replace the local draft.
            server_copy = "# Server copy after conflict\n"
            plan.write_text(server_copy, encoding="utf-8")
            phase["value"] = "initial"
            preserved_draft = editor.input_value()
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_role("alert").filter(has_text="The plan changed on the server")).to_be_visible()
            expect(editor).to_have_value(preserved_draft)
            reload_button = page.locator(".plan-editor").get_by_role("button", name="Reload from server…")
            reload_button.click()
            page.get_by_role("button", name="Keep editing", exact=True).click()
            expect(editor).to_have_value(preserved_draft)
            reload_button.click()
            page.get_by_role("button", name="Discard edits and reload", exact=True).click()
            expect(editor).to_have_value(server_copy)
            page.screenshot(path=str(artifact_dir / f"{stem}-explicit-conflict-reload.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 420})
            page.get_by_role("button", name="← Back to Plans", exact=True).click()
            expect(page.locator(".plan-list")).to_be_visible()
            list_row = page.get_by_role("button", name=re.compile("refresh-probe.md"))
            assert len(changed_list_sizes) == 1
            expect(list_row).to_contain_text(f"{changed_list_sizes[0]} bytes")
            list_row.click()
            expect(editor).to_have_value(server_copy)
            install_refresh_probe(page, {"editor": ".plan-editor", "textarea": ".plan-editor-textarea"})
            compact = capture_refresh_probe(page)
            page.set_viewport_size({"width": 1280, "height": 720})
            expanded = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in expanded["roots"].values()), (compact, expanded)
            expect(editor).to_have_value(server_copy)
            page.screenshot(path=str(artifact_dir / f"{stem}-back-resized.png"), full_page=True)
            assert page_errors == [] and units.start_calls == []
            _write_artifact_manifest(artifact_dir / f"{stem}.json", {"captures": captures, "read_phases": read_phases, "changed_list_sizes": changed_list_sizes, "writes": writes, "page_errors": page_errors})
        finally:
            for route, _, status, body, headers in held:
                try:
                    route.fulfill(status=status, headers=headers, body=body)
                except Exception:
                    pass
            page.unroute_all(behavior="ignoreErrors")
            browser.close()


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    ((1280, 720, "light"), (390, 844, "dark"), (844, 390, "light")),
)
def test_ui_demo_new_run_background_preflight_preserves_review(
    control_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
    theme: str,
) -> None:
    """Background revalidation keeps a prepared launch mounted and exact."""
    _, root, units, _ = control_client
    _seed_team_family_fixture(root)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    artifact_dir = _fidelity_artifact_dir(tmp_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stem = f"cp2-new-run-{theme}-{width}x{height}"
    preflight_path = f"/api/control-plane/projects/{PROJECT_ID}/runs/preflight"
    start_path = f"/api/control-plane/projects/{PROJECT_ID}/runs"
    phase = {"value": "initial"}
    held: list[tuple[object, int, bytes, dict[str, str]]] = []
    preflight_requests: list[dict[str, object]] = []
    start_requests: list[dict[str, object]] = []
    page_errors: list[str] = []

    def intercept_preflight(route) -> None:
        request = route.request
        if request.method != "POST" or urlsplit(request.url).path != preflight_path:
            route.continue_()
            return
        current = phase["value"]
        preflight_requests.append({"phase": current, "body": request.post_data_json})
        if current == "initial":
            route.continue_()
            return
        response = route.fetch()
        body = response.body()
        if current == "changed":
            payload = json.loads(body)
            payload["blockers"] = ["Changed checkout requires review"]
            body = json.dumps(payload).encode()
        held.append((route, 503 if current == "failure" else response.status,
                     b'{"detail":"Inspection read unavailable"}' if current == "failure" else body,
                     response.headers))

    def intercept_start(route) -> None:
        request = route.request
        if request.method == "POST" and urlsplit(request.url).path == start_path:
            start_requests.append(request.post_data_json)
            held.append((route, 503, b'{"detail":"Disposable start withheld"}', {}))
        else:
            route.continue_()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.route(f"**{preflight_path}", intercept_preflight)
        page.route(f"**{start_path}", intercept_start)
        try:
            _login(page, url)
            page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
            _set_theme_preference(page, theme)
            page.goto(f"{url}/?project={PROJECT_ID}&view=new-run")
            plan = page.get_by_label("Run plan", exact=True)
            plan.wait_for()
            plan.click()
            page.get_by_role("option", name=re.compile("ready-launch-plan.md")).click()
            workflow = page.get_by_label("Run workflow", exact=True)
            workflow.click()
            workflow.fill("managed")
            workflow.press("ArrowDown")
            workflow.press("Enter")
            team = page.get_by_label("Run team", exact=True)
            team.click()
            team.fill("Product development")
            page.get_by_role("listbox", name="Run team suggestions").locator("li[role='option']:not(.combobox-default-option)").filter(has_text="Product development").first.click()
            turns = page.get_by_label("Run max turns", exact=True)
            turns.fill("7")
            advanced = page.get_by_role("button", name="Advanced options", exact=True)
            advanced.click()
            stage = page.get_by_label("Run team stage", exact=True)
            stage.click()
            stage.fill("Stronger worker")
            page.get_by_role("listbox", name="Run team stage suggestions").locator("li[role='option']:not(.combobox-default-option)").filter(has_text="Stronger worker").first.click()
            page.get_by_label("Run start step", exact=True).select_option("review")
            instructions = page.get_by_label("Run extra instructions", exact=True)
            instructions.fill("Keep this exact reviewed instruction.")
            preflight = page.locator(".worktree-preflight")
            expect(preflight).to_have_attribute("data-preflight-status", "ready", timeout=30_000)
            checkbox = preflight.get_by_role("checkbox", name="Continue despite uncommitted changes", exact=True)
            if checkbox.count():
                checkbox.check()
            review_button = page.get_by_role("button", name="Review start…", exact=True)
            expect(review_button).to_be_enabled()
            review_button.click()
            review = page.get_by_role("region", name="Review start", exact=True)
            expect(review).to_be_visible()
            expect(review).to_contain_text("Ready for the final start action")
            # The explicit review action scrolls smoothly to its heading.
            # Establish a settled baseline before any background read.
            page.evaluate("window.__aflowCp2ScrollSample = {last: null, stable: 0}")
            page.wait_for_function("""() => {
                const sample = window.__aflowCp2ScrollSample;
                const current = window.scrollY;
                sample.stable = sample.last === current ? sample.stable + 1 : 0;
                sample.last = current;
                return sample.stable >= 6;
            }""", polling=50)
            install_refresh_probe(page, {"form": "section.card.start-run-form", "review": ".launch-review", "preflight": ".worktree-preflight", "instructions": "textarea[aria-label='Run extra instructions']"})
            before = capture_refresh_probe(page)
            choices = {"plan": plan.input_value(), "workflow": workflow.input_value(), "team": team.input_value(), "stage": stage.input_value(), "turns": turns.input_value(), "step": page.get_by_label("Run start step", exact=True).input_value(), "instructions": instructions.input_value()}

            def restore_after_full_page_capture() -> None:
                # Playwright's full-page capture can move the document while
                # composing a sticky action row. Reset only that capture side
                # effect before the next read begins.
                page.evaluate("target => window.scrollTo(0, target)", before["scrollY"])
                page.wait_for_function("target => window.scrollY === target", arg=before["scrollY"])
                read_refresh_probe_mutations(page)

            page.screenshot(path=str(artifact_dir / f"{stem}-before.png"), full_page=True)
            restore_after_full_page_capture()
            captures: dict[str, object] = {}

            def assert_choices() -> None:
                assert choices == {"plan": plan.input_value(), "workflow": workflow.input_value(), "team": team.input_value(), "stage": stage.input_value(), "turns": turns.input_value(), "step": page.get_by_label("Run start step", exact=True).input_value(), "instructions": instructions.input_value()}

            for current in ("equal", "changed", "failure"):
                phase["value"] = current
                page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
                for _ in range(200):
                    if held:
                        break
                    page.wait_for_timeout(25)
                assert held and preflight_requests[-1]["phase"] == current, preflight_requests
                during = capture_refresh_probe(page)
                assert all(root["sameNode"] for root in during["roots"].values()), (current, during)
                assert during["scrollY"] == before["scrollY"], (current, before, during)
                assert during["focused"] == before["focused"]
                assert_choices()
                if current == "equal":
                    assert preflight.get_attribute("data-preflight-status") == "ready"
                    assert review.is_visible()
                    assert during["roots"]["form"]["text"] == before["roots"]["form"]["text"]
                route, status, body, headers = held.pop(0)
                route.fulfill(status=status, headers=headers, body=body)
                if current == "changed":
                    expect(preflight.get_by_role("alert").filter(has_text="Changed checkout requires review")).to_be_visible()
                    expect(page.get_by_role("button", name="Start run", exact=True)).to_be_disabled()
                elif current == "failure":
                    expect(preflight.get_by_role("alert").filter(has_text="Inspection read unavailable")).to_be_visible()
                    expect(preflight.get_by_text("Last successful inspection:", exact=False)).to_be_visible()
                    expect(preflight.get_by_text("Last successful inspection reported blockers:", exact=True)).to_be_visible()
                    expect(preflight.locator("details.worktree-checkout-details")).to_be_visible()
                    if checkbox.count():
                        expect(checkbox).to_be_disabled()
                    expect(page.get_by_role("button", name="Start run", exact=True)).to_be_disabled()
                else:
                    expect(preflight).to_have_attribute("data-preflight-status", "ready")
                after = capture_refresh_probe(page)
                assert all(root["sameNode"] for root in after["roots"].values()), (current, after)
                if current == "equal":
                    assert after["scrollY"] == before["scrollY"], (current, before, after)
                    assert abs(after["roots"]["review"]["box"]["y"] - before["roots"]["review"]["box"]["y"]) <= 2
                if current == "failure":
                    # The warning can add height, but the last inspection and
                    # its space must remain instead of collapsing the page.
                    assert after["roots"]["preflight"]["box"]["height"] >= before["roots"]["preflight"]["box"]["height"]
                assert after["focused"] == before["focused"]
                assert_choices()
                mutations = read_refresh_probe_mutations(page)
                assert not [item for item in mutations if item["root"] == "instructions" and item["classification"] in {"subtree", "root-replacement", "value", "visibility"}], mutations
                if current == "equal":
                    assert not [item for item in mutations if item["root"] in {"form", "review"} and item["classification"] in {"subtree", "root-replacement", "visibility"}], mutations
                page.screenshot(path=str(artifact_dir / f"{stem}-{current}.png"), full_page=True)
                restore_after_full_page_capture()
                captures[current] = {"held": during, "settled": after, "mutations": mutations}

            # A user-requested inspection may show local pending feedback.
            phase["value"] = "explicit"
            preflight.get_by_role("button", name="Refresh worktree inspection").click()
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(25)
            assert held
            expect(preflight).to_have_attribute("data-preflight-status", "loading")
            route, status, body, headers = held.pop(0)
            route.fulfill(status=status, headers=headers, body=body)
            expect(preflight).to_have_attribute("data-preflight-status", "ready")
            assert_choices()
            # The capability GET is another real read in the same background
            # refresh. Its failure must report stale data without removing the
            # prepared review or silently submitting a launch.
            capabilities_path = f"/api/control-plane/projects/{PROJECT_ID}/capabilities"
            capability_reads: list[str] = []

            def fail_capabilities(route) -> None:
                capability_reads.append(urlsplit(route.request.url).path)
                route.fulfill(status=503, content_type="application/json", body='{"detail":"Capabilities unavailable"}')

            page.route(f"**{capabilities_path}", fail_capabilities)
            phase["value"] = "initial"
            page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            expect(page.get_by_role("alert").filter(has_text="Capabilities unavailable")).to_be_visible()
            assert capability_reads == [capabilities_path]
            assert all(root["sameNode"] for root in capture_refresh_probe(page)["roots"].values())
            assert_choices()
            assert review.is_visible() and start_requests == []
            page.screenshot(path=str(artifact_dir / f"{stem}-capabilities-failure.png"), full_page=True)
            restore_after_full_page_capture()
            page.unroute(f"**{capabilities_path}", fail_capabilities)
            def change_capabilities(route) -> None:
                payload = route.fetch().json()
                payload["service_features"].append("refresh-probe-read-only")
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

            page.route(f"**{capabilities_path}", change_capabilities)
            with page.expect_response(lambda response: urlsplit(response.url).path == capabilities_path) as changed_capability_response:
                page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            assert changed_capability_response.value.status == 200
            expect(page.get_by_role("alert").filter(has_text="Capabilities unavailable")).to_have_count(0)
            assert all(root["sameNode"] for root in capture_refresh_probe(page)["roots"].values())
            assert_choices()
            assert review.is_visible() and start_requests == []
            page.screenshot(path=str(artifact_dir / f"{stem}-capabilities-changed.png"), full_page=True)
            restore_after_full_page_capture()
            page.unroute(f"**{capabilities_path}", change_capabilities)
            # Reopen the review after the changed and failed inspection cues.
            page.get_by_role("button", name="Cancel review", exact=True).click()
            expect(review_button).to_be_enabled()
            review_button.click()
            expect(review).to_contain_text("Ready for the final start action")
            phase["value"] = "start"
            page.get_by_role("button", name="Start run", exact=True).evaluate("node => { node.click(); node.click(); }")
            for _ in range(200):
                if start_requests:
                    break
                page.wait_for_timeout(25)
            assert len(start_requests) == 1, start_requests
            request = start_requests[0]
            assert request["plan_path"] == choices["plan"]
            assert request["workflow_name"] == "managed"
            assert request["team"] == "product_stronger"
            assert request["max_turns"] == 7
            assert request["start_step"] == "review"
            assert request["extra_instructions"] == ["Keep this exact reviewed instruction."]
            route, status, body, headers = held.pop(0)
            route.fulfill(status=status, headers=headers, body=body)
            assert page_errors == [] and units.start_calls == []
            _write_artifact_manifest(artifact_dir / f"{stem}.json", {"before": before, "choices": choices, "captures": captures, "preflight_requests": preflight_requests, "start_requests": start_requests, "page_errors": page_errors})
        finally:
            for route, status, body, headers in held:
                try:
                    route.fulfill(status=status, headers=headers, body=body)
                except Exception:
                    pass
            page.unroute_all(behavior="ignoreErrors")
            browser.close()

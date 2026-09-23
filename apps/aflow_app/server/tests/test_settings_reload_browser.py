"""Real-browser coverage for Settings reload continuity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

from playwright.sync_api import sync_playwright

from test_control_plane_api import TOKEN, control_client, live_server  # noqa: F401
from test_responsive_browser import _browser, _login, _select_settings_section


def _open_more(page):
    page.get_by_role("button", name="More", exact=True).click()


def _reload_from_more(page):
    _open_more(page)
    page.get_by_role("menuitem", name="Reload server settings", exact=True).click()


def _toggle_advanced(page):
    _open_more(page)
    label = "Guided settings" if page.get_by_role("menuitem", name="Guided settings", exact=True).count() else "Advanced TOML"
    page.get_by_role("menuitem", name=label, exact=True).click()


def _wait_for_held_route(page, held):
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert held, "the reload GET /api/config was not intercepted"


def test_settings_reload_preserves_visible_editors(control_client, monkeypatch, tmp_path):
    """Keep loaded Settings visible across equal, failed, and changed reads."""
    from aflow_app_server import config as config_module, main

    _, root, _, _ = control_client
    config_dir = root.parent / "global"
    config_path = config_dir / "aflow.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'model = "test"\n', 'model = "test"\neffort = "high"\n'
        )
        + "\n[teams.alpha.roles]\nworker = \"codex.test\"\n"
        + "\n[teams.beta.roles]\nworker = \"codex.test\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "global_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: config_dir)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    state = {"mode": "hold"}
    held = []
    config_writes = []

    def intercept_config(route):
        if route.request.method != "GET":
            config_writes.append(route.request.method)
            route.continue_()
            return
        if state["mode"] == "hold":
            held.append(route)
            return
        if state["mode"] == "fail":
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"detail": "browser reload failed"}),
            )
            return
        response = route.fetch()
        payload = response.json()
        payload["aflow_toml"] = payload["aflow_toml"].replace(
            'effort = "high"', 'effort = "changed-effort"'
        )
        route.fulfill(response=response, json=payload)

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            page.get_by_role("button", name="Settings", exact=True).click()
            _select_settings_section(page, "Teams")
            page.route("**/api/config", intercept_config)

            beta = page.locator('[data-sidebar-editor-list="Team families"] [data-sidebar-editor-item="beta"]')
            beta.wait_for(state="visible")
            beta.click()
            assert beta.get_attribute("aria-pressed") == "true"
            advanced = page.locator(
                '[data-settings-tab="Teams"] details.team-family-advanced-details'
            )
            advanced.locator(":scope > summary").click()
            identity = advanced.locator(
                ".team-family-advanced-content > details"
            ).filter(has_text="Name and identity")
            identity.locator(":scope > summary").click()
            assert identity.get_attribute("open") is not None

            page.evaluate(
                """() => {
                    window.__settingsTeamBefore = document.querySelector('[data-settings-tab="Teams"] fieldset.team-family-detail');
                }"""
            )
            _reload_from_more(page)
            _wait_for_held_route(page, held)
            assert beta.get_attribute("aria-pressed") == "true"
            assert identity.get_attribute("open") is not None
            assert page.evaluate(
                """() => window.__settingsTeamBefore === document.querySelector('[data-settings-tab="Teams"] fieldset.team-family-detail')"""
            )
            page.screenshot(
                path=str(tmp_path / f"settings-reload-{os.environ.get('AFLOW_TEST_BROWSER', 'chromium').strip().lower()}-guided-during.png"),
                full_page=True,
            )
            for route in held:
                route.continue_()
            held.clear()
            page.wait_for_function(
                "() => ![...document.querySelectorAll('.app-header-row-two button')].some(button => button.textContent?.trim() === 'Working…')"
            )
            assert beta.get_attribute("aria-pressed") == "true"
            assert identity.get_attribute("open") is not None

            _toggle_advanced(page)
            aflow = page.get_by_label("aflow.toml contents", exact=True)
            workflows = page.get_by_label("workflows.toml contents", exact=True)
            aflow.wait_for(state="visible")
            assert aflow.input_value()
            assert workflows.input_value()
            page.evaluate(
                """() => {
                    const area = document.querySelector('textarea[aria-label="aflow.toml contents"]');
                    const root = document.scrollingElement;
                    if (!(area instanceof HTMLTextAreaElement) || !root) throw new Error('Settings editor not mounted');
                    area.focus();
                    area.setSelectionRange(2, Math.min(10, area.value.length));
                    root.scrollTop = Math.min(180, Math.max(0, root.scrollHeight - root.clientHeight));
                    window.__settingsReloadBefore = {
                        selectionStart: area.selectionStart,
                        selectionEnd: area.selectionEnd,
                        documentScroll: root.scrollTop,
                        area,
                    };
                }"""
            )
            before = page.evaluate(
                """() => ({
                    selectionStart: window.__settingsReloadBefore.selectionStart,
                    selectionEnd: window.__settingsReloadBefore.selectionEnd,
                    documentScroll: window.__settingsReloadBefore.documentScroll,
                    aflow: document.querySelector('textarea[aria-label="aflow.toml contents"]').value,
                    workflows: document.querySelector('textarea[aria-label="workflows.toml contents"]').value,
                })"""
            )

            _reload_from_more(page)
            _wait_for_held_route(page, held)
            assert aflow.input_value() == before["aflow"]
            assert workflows.input_value() == before["workflows"]
            assert page.evaluate(
                """() => window.__settingsReloadBefore.area === document.querySelector('textarea[aria-label="aflow.toml contents"]')"""
            )
            during = page.evaluate(
                """() => {
                    const area = document.querySelector('textarea[aria-label="aflow.toml contents"]');
                    const root = document.scrollingElement;
                    return {
                        selectionStart: area.selectionStart,
                        selectionEnd: area.selectionEnd,
                        documentScroll: root.scrollTop,
                    };
                }"""
            )
            assert during["selectionStart"] == before["selectionStart"]
            assert during["selectionEnd"] == before["selectionEnd"]
            assert during["documentScroll"] == before["documentScroll"]
            browser_name = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
            page.screenshot(
                path=str(tmp_path / f"settings-reload-{browser_name}-during.png"),
                full_page=True,
            )

            for route in held:
                route.continue_()
            held.clear()
            page.wait_for_function(
                "() => ![...document.querySelectorAll('.app-header-row-two button')].some(button => button.textContent?.trim() === 'Working…')"
            )
            assert aflow.input_value() == before["aflow"]
            assert workflows.input_value() == before["workflows"]
            assert page.evaluate(
                """() => window.__settingsReloadBefore.area === document.querySelector('textarea[aria-label="aflow.toml contents"]')"""
            )

            state["mode"] = "fail"
            _reload_from_more(page)
            page.get_by_text("Some settings could not be loaded. Reload to retry.", exact=True).wait_for()
            assert aflow.input_value() == before["aflow"]
            assert workflows.input_value() == before["workflows"]

            state["mode"] = "changed"
            _reload_from_more(page)
            page.wait_for_function(
                '() => document.querySelector(\'textarea[aria-label="aflow.toml contents"]\')?.value.includes("changed-effort")'
            )
            assert workflows.input_value() == before["workflows"]
            _toggle_advanced(page)
            _select_settings_section(page, "Agents & Roles")
            effort = page.get_by_label("Effort codex.test", exact=True)
            effort.wait_for(state="visible")
            assert effort.input_value() == "changed-effort"
            _select_settings_section(page, "Teams")
            beta = page.locator('[data-sidebar-editor-list="Team families"] [data-sidebar-editor-item="beta"]')
            assert beta.get_attribute("aria-pressed") == "true"
            assert not config_writes
        finally:
            browser.close()


def test_skill_reload_preserves_draft_revision_and_conflict(control_client, monkeypatch):
    """A pending skill read never rebases an edit onto the replacement revision."""
    summary = {
        "name": "aflow-manager",
        "default": True,
        "revision": "c" * 64,
        "source": "bundled",
        "edited": False,
        "installed": True,
        "links": [],
        "detected_harnesses": [],
    }
    first_content = "---\nname: aflow-manager\ndescription: Test skill.\n---\n\nOriginal content.\n"
    revised_content = first_content + "Concurrent server change.\n"
    first_revision = summary["revision"]
    revised_revision = "f" * 64
    edited_content = first_content + "My edit.\n"
    state = {"list_calls": 0, "detail_calls": 0, "held": [], "validation": [], "writes": []}

    def intercept_skills(route):
        request = route.request
        path = request.url.split("/api/skills", 1)[-1].split("?", 1)[0]
        if request.method == "GET" and path == "":
            state["list_calls"] += 1
            listed = {**summary, "revision": revised_revision} if state["list_calls"] > 1 else summary
            route.fulfill(json=[listed])
            return
        if request.method == "GET" and path == "/aflow-manager":
            state["detail_calls"] += 1
            if state["detail_calls"] <= 2:
                route.fulfill(json={**summary, "content": first_content})
            else:
                state["held"].append(route)
            return
        if request.method == "POST" and path == "/validate":
            payload = request.post_data_json
            state["validation"].append(payload)
            route.fulfill(json={"entries": [{
                "name": "aflow-manager",
                "ok": True,
                "current_revision": revised_revision,
                "error_code": None,
                "error": None,
            }]})
            return
        if request.method == "PUT" and path == "/aflow-manager":
            payload = request.post_data_json
            state["writes"].append(payload)
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": {"code": "revision_conflict", "current_revision": revised_revision}}),
            )
            return
        route.continue_()

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.route("**/api/skills**", intercept_skills)
            _login(page, url)
            page.get_by_role("button", name="Settings", exact=True).click()
            _select_settings_section(page, "Skills")
            area = page.get_by_label("SKILL.md for aflow-manager", exact=True)
            area.wait_for(state="visible")
            assert area.is_enabled()
            assert area.input_value() == first_content

            _reload_from_more(page)
            deadline = time.monotonic() + 5
            while not state["held"] and time.monotonic() < deadline:
                page.wait_for_timeout(25)
            assert state["held"], "the skill detail refresh was not held"
            page.wait_for_function(
                """() => {
                    const panel = document.querySelector('#settings-domain-panel');
                    return panel instanceof HTMLFieldSetElement && !panel.disabled;
                }"""
            )
            assert area.is_visible() and area.is_enabled()
            area.fill(edited_content)
            for route in state["held"]:
                route.fulfill(json={**summary, "revision": revised_revision, "content": revised_content})
            state["held"].clear()
            assert area.input_value() == edited_content

            page.get_by_role("button", name="Save all changes", exact=True).click()
            page.get_by_text("Skill aflow-manager was not saved", exact=False).wait_for()
            assert state["validation"] == [{"entries": [{
                "name": "aflow-manager", "content": edited_content, "expected_revision": first_revision,
            }]}]
            assert state["writes"] == [{"content": edited_content, "expected_revision": first_revision}]
            assert area.input_value() == edited_content
            assert page.get_by_role("button", name="Save all changes", exact=True).is_enabled()
        finally:
            for route in state["held"]:
                try:
                    route.abort()
                except Exception:
                    pass
            browser.close()

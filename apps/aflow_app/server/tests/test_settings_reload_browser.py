"""Real-browser coverage for Settings reload continuity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import pytest

from playwright.sync_api import sync_playwright

from test_control_plane_api import PROJECT_ID, TOKEN, control_client, live_server  # noqa: F401
from test_responsive_browser import _browser, _login, _seed_team_family_fixture, _select_settings_section
from ui_demo_fidelity import capture_refresh_probe, install_refresh_probe, read_refresh_probe_mutations


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


@pytest.mark.parametrize("mode", ["equal", "changed", "failed"])
def test_guided_workflow_edit_survives_pending_equal_reload(control_client, monkeypatch, mode):
    """An edit made while a genuine settings read is pending stays in its editor."""
    from aflow_app_server import config as config_module, main

    _, root, _, _ = control_client
    config_dir = root.parent / "global"
    config_path = config_dir / "aflow.toml"
    monkeypatch.setattr(main, "global_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: config_dir)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    held = []
    writes = []

    def intercept(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            writes.append(route.request.method)
            route.continue_()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            page.get_by_role("button", name="Settings", exact=True).click()
            _select_settings_section(page, "Workflows")
            field = page.get_by_label("Max turns", exact=True)
            field.wait_for(state="visible")
            page.route("**/api/config", intercept)
            install_refresh_probe(page, {"panel": "#settings-domain-panel", "editor": "#settings-domain-panel input[type=number][min='1']"})
            before = capture_refresh_probe(page)
            _reload_from_more(page)
            _wait_for_held_route(page, held)
            field.fill("17")
            field.focus()
            read_refresh_probe_mutations(page)
            if mode == "changed":
                config_path.write_text(config_path.read_text(encoding="utf-8") + "\n# concurrent edit\n", encoding="utf-8")
            for route in held:
                if mode == "failed":
                    route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "reload failed"}))
                elif mode == "changed":
                    response = route.fetch()
                    payload = response.json()
                    payload["aflow_toml"] += "\n# changed on server\n"
                    route.fulfill(response=response, json=payload)
                else:
                    route.continue_()
            held.clear()
            page.wait_for_function("() => ![...document.querySelectorAll('.app-header-row-two button')].some(button => button.textContent?.trim() === 'Working…')")
            page.wait_for_timeout(150)
            after = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after["roots"].values())
            assert after["roots"]["editor"]["token"] == before["roots"]["editor"]["token"]
            assert field.input_value() == "17"
            assert page.get_by_role("button", name="Save all changes", exact=True).is_enabled()
            assert not writes
            if mode == "changed":
                page.get_by_role("button", name="Save all changes", exact=True).click()
                page.get_by_text("Your remaining edits are retained", exact=False).wait_for()
                assert field.input_value() == "17"
        finally:
            for route in held:
                route.abort()
            browser.close()


@pytest.mark.parametrize("domain", ["workflow", "server", "scheduling"])
@pytest.mark.parametrize("mode", ["equal", "changed", "failed"])
def test_confirmed_discard_protects_new_edits_during_read(control_client, monkeypatch, tmp_path, domain, mode):
    """A discard applies to the old draft, not edits entered during its pending GET."""
    from aflow_app_server import config as config_module, main

    _, root, _, _ = control_client
    config_dir = root.parent / "global"
    config_path = config_dir / "aflow.toml"
    monkeypatch.setattr(main, "global_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: config_dir)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    paths = {
        "workflow": "/api/config",
        "server": "/api/settings",
        "scheduling": f"/api/projects/{PROJECT_ID}/scheduling",
    }
    held = []
    writes = []

    def intercept(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            writes.append(route.request.method)
            route.continue_()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            errors = []
            _login(page, url)
            if domain == "scheduling":
                page.goto(f"{url}/?project={PROJECT_ID}&view=settings")
            else:
                page.get_by_role("button", name="Settings", exact=True).click()
            _select_settings_section(page, "Workflows" if domain == "workflow" else "General")
            field = page.get_by_label({"workflow": "Max turns", "server": "Bind host", "scheduling": "Concurrent implementations"}[domain], exact=True)
            field.wait_for(state="visible")
            page.on("pageerror", lambda error: errors.append(str(error)))
            original = field.input_value()
            old = "13" if domain != "server" else "discard-old.invalid"
            fresh = "17" if domain != "server" else "pending-new.invalid"
            assert original != old and original != fresh
            field.fill(old)
            page.route(f"**{paths[domain]}", intercept)
            page.once("dialog", lambda dialog: dialog.accept())
            _reload_from_more(page)
            _wait_for_held_route(page, held)
            assert field.input_value() == original, "the pre-confirmation draft was not discarded"
            assert field.is_enabled()
            field.evaluate("element => element.setAttribute('data-refresh-test-editor', '')")
            install_refresh_probe(page, {"panel": "#settings-domain-panel", "editor": "#settings-domain-panel [data-refresh-test-editor]"})
            field.fill(fresh)
            field.focus()
            during = capture_refresh_probe(page)
            read_refresh_probe_mutations(page)
            if mode == "changed" and domain == "workflow":
                config_path.write_text(config_path.read_text(encoding="utf-8") + "\n# concurrent edit\n", encoding="utf-8")
            for route in held:
                if mode == "failed":
                    route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "reload failed"}))
                elif mode == "changed" and domain != "workflow":
                    response = route.fetch()
                    payload = response.json()
                    payload["revision"] = "f" * 64
                    if domain == "server":
                        payload["bind_host"] = "changed-on-server.invalid"
                    else:
                        payload["max_concurrent_implementations"] = 4
                    route.fulfill(response=response, json=payload)
                else:
                    route.continue_()
            held.clear()
            page.wait_for_function("() => ![...document.querySelectorAll('.app-header-row-two button')].some(button => button.textContent?.trim() === 'Working…')")
            after = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after["roots"].values())
            assert after["roots"]["editor"]["token"] == during["roots"]["editor"]["token"]
            assert after["focused"] == during["focused"]
            assert field.input_value() == fresh
            assert page.get_by_role("button", name="Save all changes", exact=True).is_enabled()
            if mode == "changed":
                page.get_by_text("Your draft is retained", exact=False).wait_for()
            if mode == "failed":
                page.get_by_text("Some settings could not be loaded. Reload to retry.", exact=True).wait_for()
            if domain == "workflow" and mode == "changed":
                page.get_by_role("button", name="Save all changes", exact=True).click()
                page.get_by_text("Your remaining edits are retained", exact=False).wait_for()
                assert field.input_value() == fresh
            else:
                assert not writes
            assert not errors
            if domain == "workflow" and mode == "changed":
                page.screenshot(path=str(tmp_path / "confirmed-discard-changed.png"), full_page=True)
        finally:
            for route in held:
                route.abort()
            browser.close()


@pytest.mark.parametrize("domain", ["workflow", "server", "scheduling"])
def test_confirmed_discard_without_new_edit_accepts_changed_read(control_client, monkeypatch, domain):
    """A clean confirmed reload still takes changed server values and revisions."""
    from aflow_app_server import config as config_module, main

    _, root, _, _ = control_client
    config_dir = root.parent / "global"
    monkeypatch.setattr(main, "global_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: config_dir)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    paths = {"workflow": "/api/config", "server": "/api/settings", "scheduling": f"/api/projects/{PROJECT_ID}/scheduling"}
    held = []

    def intercept(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            route.continue_()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            if domain == "scheduling":
                page.goto(f"{url}/?project={PROJECT_ID}&view=settings")
            else:
                page.get_by_role("button", name="Settings", exact=True).click()
            _select_settings_section(page, "Workflows" if domain == "workflow" else "General")
            field = page.get_by_label({"workflow": "Max turns", "server": "Bind host", "scheduling": "Concurrent implementations"}[domain], exact=True)
            field.wait_for(state="visible")
            old = "13" if domain != "server" else "discard-old.invalid"
            field.fill(old)
            page.route(f"**{paths[domain]}", intercept)
            page.once("dialog", lambda dialog: dialog.accept())
            _reload_from_more(page)
            _wait_for_held_route(page, held)
            assert field.input_value() != old
            for route in held:
                response = route.fetch()
                payload = response.json()
                payload["revision"] = "f" * 64
                if domain == "workflow":
                    payload["aflow_toml"] += "\n# clean changed reload\n"
                elif domain == "server":
                    payload["bind_host"] = "changed-on-server.invalid"
                else:
                    payload["max_concurrent_implementations"] = 4
                route.fulfill(response=response, json=payload)
            held.clear()
            page.wait_for_function("() => ![...document.querySelectorAll('.app-header-row-two button')].some(button => button.textContent?.trim() === 'Working…')")
            if domain == "workflow":
                _toggle_advanced(page)
                assert "# clean changed reload" in page.get_by_label("aflow.toml contents", exact=True).input_value()
            else:
                assert field.input_value() == ("changed-on-server.invalid" if domain == "server" else "4")
            assert page.get_by_role("button", name="Save all changes", exact=True).is_disabled()
        finally:
            for route in held:
                route.abort()
            browser.close()


@pytest.mark.parametrize("section", ["Teams", "Agents & Roles", "Prompts", "General"])
@pytest.mark.parametrize("mode", ["equal", "changed", "failed"])
def test_guided_edit_survives_pending_reads(control_client, monkeypatch, section, mode):
    """Each visible guided editor remains usable as real settings reads settle."""
    from aflow_app_server import config as config_module, main

    _, root, _, _ = control_client
    _seed_team_family_fixture(root)
    config_dir = root.parent / "global"
    monkeypatch.setattr(main, "global_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: config_dir)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    held = []
    writes = []

    def intercept(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            writes.append((route.request.method, route.request.url))
            route.continue_()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            _login(page, url)
            page.get_by_role("button", name="Settings", exact=True).click()
            _select_settings_section(page, section)
            if section == "Teams":
                page.get_by_role("button", name="New family", exact=True).click()
                selector = "input[aria-label='Family display name']"
                field = page.get_by_label("Family display name", exact=True)
                edited = "Pending family"
            elif section == "Agents & Roles":
                selector = "input[aria-label='Model codex.global_worker']"
                field = page.get_by_label("Model codex.global_worker", exact=True)
                edited = "pending-worker"
            elif section == "Prompts":
                page.locator('[data-sidebar-editor-list="Prompts"] [data-sidebar-editor-item="named:implement"]').click()
                selector = "textarea[aria-label='Prompt text']"
                field = page.get_by_label("Prompt text", exact=True)
                edited = "Pending prompt text."
            else:
                page.locator("#settings-domain-panel details.settings-disclosure > summary", has_text="Change password").click()
                selector = "input[type='password'][autocomplete='new-password']"
                field = page.get_by_label("New password (leave empty to keep current)", exact=True)
                edited = "pending-password"
            field.wait_for(state="visible")
            page.route("**/api/config", intercept)
            page.route("**/api/settings", intercept)
            install_refresh_probe(page, {"panel": "#settings-domain-panel", "editor": selector})
            _reload_from_more(page)
            deadline = time.monotonic() + 5
            while len(held) < 2 and time.monotonic() < deadline:
                page.wait_for_timeout(25)
            assert len(held) == 2, "the real configuration and server-settings reads were not held"
            assert field.is_enabled()
            field.fill(edited)
            field.focus()
            if section == "Prompts":
                field.evaluate("element => element.setSelectionRange(2, 7)")
            during = capture_refresh_probe(page)
            selection = field.evaluate("element => element instanceof HTMLTextAreaElement ? [element.selectionStart, element.selectionEnd] : null")
            read_refresh_probe_mutations(page)
            for route in held:
                if mode == "failed":
                    route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "reload failed"}))
                elif mode == "changed":
                    response = route.fetch()
                    payload = response.json()
                    if route.request.url.endswith("/api/config"):
                        payload["aflow_toml"] += "\n# changed on server\n"
                    else:
                        payload["bind_host"] = "127.0.0.2"
                        payload["revision"] = "f" * 64
                    route.fulfill(response=response, json=payload)
                else:
                    route.continue_()
            held.clear()
            page.wait_for_function("() => ![...document.querySelectorAll('.app-header-row-two button')].some(button => button.textContent?.trim() === 'Working…')")
            after = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after["roots"].values())
            assert after["roots"]["editor"]["token"] == during["roots"]["editor"]["token"]
            assert after["focused"] == during["focused"]
            if mode == "equal":
                assert after["documentScrollTop"] == during["documentScrollTop"]
            else:
                # A visible warning may extend the page above the focused
                # field; scroll anchoring must keep that field in place.
                assert abs(after["roots"]["editor"]["box"]["y"] - during["roots"]["editor"]["box"]["y"]) <= 1
            if selection is not None:
                assert field.evaluate("element => [element.selectionStart, element.selectionEnd]") == selection
            assert field.input_value() == edited
            save_enabled = page.get_by_role("button", name="Save all changes", exact=True).is_enabled()
            assert save_enabled == (section != "Teams"), "an unfinished family remains in the wizard until added to the draft"
            if mode == "failed":
                page.get_by_text("Some settings could not be loaded. Reload to retry.", exact=True).wait_for()
            if mode == "changed":
                page.get_by_text("changed", exact=False).first.wait_for()
            assert not [record for record in read_refresh_probe_mutations(page) if record["classification"] == "root-replacement"]
            assert not writes
            assert not page_errors
        finally:
            for route in held:
                route.abort()
            browser.close()


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


@pytest.mark.parametrize("mode", ["equal", "changed", "failed"])
def test_skill_reload_keeps_named_drafts_and_editor(control_client, monkeypatch, mode):
    """A selected skill remains editable while its real detail refresh settles."""
    first = "---\nname: aflow-manager\ndescription: Test.\n---\n\n" + "Original line.\n" * 45
    second = "---\nname: aflow-plan\ndescription: Test.\n---\n\nPlan.\n"
    revisions = {"aflow-manager": "c" * 64, "aflow-plan": "d" * 64}
    held = []
    writes = []
    state = {"reload": False}

    def summary(name):
        return {"name": name, "default": True, "revision": revisions[name], "source": "bundled",
                "edited": False, "installed": True, "links": [], "detected_harnesses": []}

    def intercept(route):
        request = route.request
        path = request.url.split("/api/skills", 1)[-1].split("?", 1)[0]
        if request.method != "GET":
            writes.append((request.method, path))
            route.continue_()
        elif path == "":
            route.fulfill(json=[summary("aflow-manager"), summary("aflow-plan")])
        elif path == "/aflow-manager" and state["reload"]:
            held.append(route)
        elif path in ("/aflow-manager", "/aflow-plan"):
            name = path[1:]
            route.fulfill(json={**summary(name), "content": first if name == "aflow-manager" else second})
        else:
            route.continue_()

    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.route("**/api/skills**", intercept)
            _login(page, url)
            page.get_by_role("button", name="Settings", exact=True).click()
            _select_settings_section(page, "Skills")
            area = page.get_by_label("SKILL.md for aflow-manager", exact=True)
            area.wait_for(state="visible")
            assert area.input_value() == first
            install_refresh_probe(page, {"panel": "#settings-domain-panel", "editor": "textarea[aria-label='SKILL.md for aflow-manager']"})
            state["reload"] = True
            _reload_from_more(page)
            _wait_for_held_route(page, held)
            edited = first + "Local edit.\n"
            area.fill(edited)
            area.focus()
            area.evaluate("element => element.setSelectionRange(5, 12)")
            area.evaluate("element => { element.scrollTop = 180 }")
            during = capture_refresh_probe(page)
            selection = area.evaluate("element => [element.selectionStart, element.selectionEnd]")
            editor_scroll = area.evaluate("element => element.scrollTop")
            read_refresh_probe_mutations(page)
            for route in held:
                if mode == "failed":
                    route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "skill unavailable"}))
                else:
                    detail = {**summary("aflow-manager"), "content": first}
                    if mode == "changed":
                        detail.update({"revision": "f" * 64, "content": first + "Server edit.\n"})
                    route.fulfill(json=detail)
            held.clear()
            page.wait_for_timeout(150)
            after = capture_refresh_probe(page)
            assert all(root["sameNode"] for root in after["roots"].values())
            assert after["roots"]["editor"]["token"] == during["roots"]["editor"]["token"]
            assert after["focused"] == during["focused"]
            assert after["documentScrollTop"] == during["documentScrollTop"]
            assert area.evaluate("element => [element.selectionStart, element.selectionEnd]") == selection
            assert area.evaluate("element => element.scrollTop") == editor_scroll
            assert area.input_value() == edited
            if mode == "failed":
                page.get_by_text("Skills could not be loaded:", exact=False).wait_for()
            page.locator('[data-sidebar-editor-list="Skills"] [data-sidebar-editor-item="aflow-plan"]').click()
            plan_area = page.get_by_label("SKILL.md for aflow-plan", exact=True)
            plan_area.wait_for(state="visible")
            plan_area.fill(second + "Other local edit.\n")
            page.locator('[data-sidebar-editor-list="Skills"] [data-sidebar-editor-item="aflow-manager"]').click()
            assert area.input_value() == edited
            assert page.get_by_role("button", name="Save all changes", exact=True).is_enabled()
            assert not writes
            assert not page_errors
        finally:
            for route in held:
                route.abort()
            browser.close()

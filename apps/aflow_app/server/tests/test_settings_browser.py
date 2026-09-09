"""Real Chromium assertions for independently scrolling settings editors."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_control_plane_api import control_client, live_server, TOKEN, PROJECT_ID  # noqa: F401


HARNESS_EXECUTABLES = (
    "claude",
    "codex",
    "copilot",
    "dsh",
    "gemini",
    "kiro-cli",
    "muse",
    "opencode",
    "pi",
    "reasonix",
    "zcode",
)


def pane_metrics(page):
    return page.evaluate('''() => {
        const nav = document.querySelector('.sidebar-editor-navigation');
        const detail = document.querySelector('.sidebar-editor-detail');
        const heading = detail.querySelector('h3, h2, legend').getBoundingClientRect();
        const box = detail.getBoundingClientRect();
        return {navScroll: nav.scrollTop, detailScroll: detail.scrollTop,
            detailTop: box.top, detailBottom: box.bottom, headingTop: heading.top,
            headingBottom: heading.bottom, navHeight: nav.clientHeight,
            navContent: nav.scrollHeight, detailHeight: detail.clientHeight,
            detailContent: detail.scrollHeight, overflow: document.documentElement.scrollWidth > innerWidth};
    }''')


def test_settings_toolbar_stays_visible_through_long_scroll(control_client, monkeypatch):
    from aflow_app_server import main, config as config_module
    _, root, _, _ = control_client
    config_dir = root.parent / 'global'
    config = config_dir / 'aflow.toml'
    config.write_text(config.read_text() + '\n' + '\n'.join(
        f'scroll_test_{index:02} = {json.dumps("Long prompt content. " * 100)}' for index in range(40)
    ) + '\n' + '\n'.join(f'[harness.codex.profiles.profile_{index:02}]\nmodel = "test"\neffort = "high"' for index in range(30)) + '\n' + '\n'.join(f'[teams.team_{index:02}.roles]\nworker = "codex.test"' for index in range(40)) + '\n')
    workflows = config_dir / 'workflows.toml'
    workflows.write_text(workflows.read_text() + '\n' + '\n'.join(
        f'[workflow.workflow_{index:02}.steps.implement]\nrole = "worker"\nprompts = ["p"]\ngo = [{{ to = "END", when = "DONE" }}]' for index in range(40)
    ))
    monkeypatch.setattr(main, 'global_config_dir', lambda: config_dir)
    monkeypatch.setattr(config_module, 'global_config_dir', lambda: config_dir)
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1365, 'height': 900})
            page.goto(url)
            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            page.get_by_role('button', name='Settings', exact=True).click()
            for theme in ('light', 'dark'):
                page.get_by_role('tab', name='General', exact=True).click()
                page.get_by_label('Color theme').select_option(theme)
                for width, height in ((1365, 900), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.get_by_role('tab', name='Agents & Roles', exact=True).click()
                    page.get_by_label('Effort codex.profile_29', exact=True).wait_for()
                    metrics = page.evaluate("""() => {
                        const pane = document.querySelector('.settings-guided-content > .settings-body');
                        const cards = [...pane.querySelectorAll('.card')];
                        return {overflow: pane.scrollHeight > pane.clientHeight,
                            cards: cards.map(card => ({height: card.clientHeight, content: card.scrollHeight}))};
                    }""")
                    assert metrics['overflow'], metrics
                    assert all(card['height'] >= card['content'] - 1 for card in metrics['cards']), metrics
                    last = page.get_by_label('Effort codex.profile_29', exact=True)
                    last.scroll_into_view_if_needed()
                    last.click()
                    box = last.bounding_box()
                    assert box and 0 <= box['y'] < height
                    save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                    assert save and 0 <= save['y'] < height
                    for tab, name in [('Teams', 'team_39'), ('Workflows', 'workflow_39'), ('Prompts', 'scroll_test_39')]:
                        page.get_by_role('tab', name=tab, exact=True).click()
                        nav = page.locator('.sidebar-editor-navigation')
                        nav.get_by_role('button', name=name, exact=True).click()
                        metrics = pane_metrics(page)
                        assert metrics['navContent'] > metrics['navHeight'], metrics
                        assert metrics['navScroll'] > 0, metrics
                        assert metrics['detailTop'] <= metrics['headingTop'] < metrics['detailBottom'], metrics
                        assert not metrics['overflow'], metrics
                        save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                        assert save and 0 <= save['y'] < height and save['x'] + save['width'] <= width
                        # A selected editor can grow without moving navigation.
                        page.add_style_tag(content='.sidebar-editor-detail::after { content: ""; display: block; height: 1600px; }')
                        page.locator('.sidebar-editor-detail').evaluate("el => { el.scrollTop = 500 }")
                        after = pane_metrics(page)
                        assert after['detailScroll'] >= 499
                        assert after['navScroll'] == metrics['navScroll']
                        nav.get_by_role('button', name=name.replace('39', '38'), exact=True).click()
                        assert pane_metrics(page)['detailScroll'] == 0
            page.get_by_role('button', name='Advanced TOML', exact=True).click()
            assert page.get_by_role('tab').count() == 0
            assert page.locator('.sidebar-editor-layout').count() == 0
            page.get_by_role('button', name='Guided settings', exact=True).click()
            assert page.get_by_role('tab').count() == 6
        finally:
            browser.close()


def test_new_draft_plan_template_smoke(control_client, monkeypatch):
    """Create a draft from the web UI and prove it starts from the skeleton.

    Uses the disposable registered project: New draft sends a name-only
    request, the editor shows one open checkpoint with implementation and
    verification tasks plus blank Git Tracking fields, and the normal
    save/promote flow still accepts the untouched placeholders (no new
    validator blocks the lifecycle).
    """
    from aflow_app_server import main, config as config_module
    _, root, _, _ = control_client
    config_dir = root.parent / 'global'
    monkeypatch.setattr(main, 'global_config_dir', lambda: config_dir)
    monkeypatch.setattr(config_module, 'global_config_dir', lambda: config_dir)
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1365, 'height': 900})
            page.goto(url)
            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            page.goto(f'{url}/?project={PROJECT_ID}&view=plans')
            page.get_by_label('New plan filename').fill('draft-smoke.md')
            page.get_by_role('button', name='Create plan', exact=True).click()
            area = page.get_by_label('Plan content')
            area.wait_for()
            content = area.input_value()
            assert '### [ ] Checkpoint 1:' in content
            assert '- [ ] Specify the files and concrete changes needed' in content
            assert '- [ ] Specify and run the checks that demonstrate' in content
            assert '- Plan Branch: ``' in content
            assert '- Pre-Handoff Base HEAD: ``' in content
            # Normal save flow works on the untouched skeleton.
            page.get_by_role('button', name='Save', exact=True).click()
            page.wait_for_timeout(500)
            assert page.get_by_label('Plan content').input_value() == content
            # Untouched placeholders do not block promotion: no new validator.
            page.get_by_role('button', name='Move to Ready', exact=True).click()
            page.get_by_text('Ready — runnable').wait_for()
            assert (root / 'plans' / 'in-progress' / 'draft-smoke.md').read_text() == content
        finally:
            browser.close()


def test_skills_edit_save_and_install_through_links(control_client, tmp_path, monkeypatch):
    """Edit bundled Markdown in Settings and prove bytes flow through real links.

    Every HOME, PATH executable, store, destination, and link lives beneath a
    disposable directory: stub harness executables stand in for the eleven
    supported harnesses and no live home is ever mutated.
    """
    from aflow_app_server import main, config as config_module
    _, root, _, _ = control_client
    home = tmp_path / "skills-home"
    home.mkdir()
    bindir = tmp_path / "skills-bin"
    bindir.mkdir()
    for executable in HARNESS_EXECUTABLES:
        stub = bindir / executable
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    # Pin browser discovery before HOME is replaced: the disposable HOME must
    # not relocate Playwright's own cache.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bindir))
    config_dir = root.parent / 'global'
    monkeypatch.setattr(main, 'global_config_dir', lambda: config_dir)
    monkeypatch.setattr(config_module, 'global_config_dir', lambda: config_dir)
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1365, 'height': 900})
            page.goto(url)
            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            page.get_by_role('button', name='Settings', exact=True).click()
            page.get_by_role('tab', name='Skills', exact=True).click()
            nav = page.locator('.sidebar-editor-navigation')
            nav.get_by_role('button', name='aflow-plan', exact=True).click()
            area = page.get_by_label('SKILL.md for aflow-plan', exact=True)
            area.wait_for()
            original = area.input_value()
            assert 'aflow-plan' in original
            for theme in ('light', 'dark'):
                page.get_by_role('tab', name='General', exact=True).click()
                page.get_by_label('Color theme').select_option(theme)
                page.get_by_role('tab', name='Skills', exact=True).click()
                for width, height in ((1365, 900), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                    assert save and 0 <= save['y'] < height and save['x'] + save['width'] <= width
            # One shared install action on the clean registry, then an edit.
            page.get_by_role('button', name='Install/reinstall all', exact=True).click()
            page.get_by_text('Install finished').wait_for()
            area.fill(original + '\n\nBrowser edit.\n')
            install = page.get_by_role('button', name='Install/reinstall all', exact=True)
            assert install.is_disabled()
            assert page.get_by_text('Save your skill edits first').count() > 0
            page.get_by_role('button', name='Save all changes', exact=True).click()
            page.get_by_text('the next manager invocation uses it').wait_for()
            for destination in ('.claude/skills', '.agents/skills', '.kiro/skills', '.zcode/skills'):
                linked = home / destination / 'aflow-plan' / 'SKILL.md'
                assert linked.read_text() == original + '\n\nBrowser edit.\n', linked
                assert os.readlink(home / destination / 'aflow-plan') == str(
                    home / '.config' / 'aflow' / 'skills' / 'aflow-plan'
                )
        finally:
            browser.close()

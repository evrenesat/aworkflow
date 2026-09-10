"""Real Chromium assertions for document scrolling and reachable settings."""
import json
import os
import sys
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


def document_metrics(page):
    return page.evaluate('''() => {
        const root = document.scrollingElement;
        const nav = document.querySelector('.sidebar-editor-navigation');
        const detail = document.querySelector('.sidebar-editor-detail')
            ?? document.querySelector('.settings-guided-content > .settings-body');
        const heading = detail?.querySelector('h3, h2, legend');
        const box = detail?.getBoundingClientRect();
        return {documentScroll: root?.scrollTop ?? window.scrollY,
            documentHeight: root?.scrollHeight ?? document.documentElement.scrollHeight,
            navScroll: nav?.scrollTop ?? 0, detailScroll: detail?.scrollTop ?? 0,
            detailTop: box?.top ?? null, detailBottom: box?.bottom ?? null,
            headingTop: heading?.getBoundingClientRect().top ?? null,
            headingBottom: heading?.getBoundingClientRect().bottom ?? null,
            navHeight: nav?.clientHeight ?? 0, navContent: nav?.scrollHeight ?? 0,
            detailHeight: detail?.clientHeight ?? 0, detailContent: detail?.scrollHeight ?? 0,
            overflow: document.documentElement.scrollWidth > innerWidth};
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
                for width, height in ((1365, 900), (390, 844), (844, 390)):
                    page.set_viewport_size({'width': width, 'height': height})
                    compact = width < 960 or height < 600
                    page.get_by_role('tab', name='Agents & Roles', exact=True).click()
                    page.get_by_label('Effort codex.profile_29', exact=True).wait_for()
                    metrics = document_metrics(page)
                    assert metrics['documentHeight'] > height, metrics
                    assert metrics['detailHeight'] > 0, metrics
                    assert metrics['detailContent'] <= metrics['detailHeight'] + 1, metrics
                    assert metrics['detailScroll'] == 0, metrics
                    assert not metrics['overflow'], metrics
                    page.evaluate('window.scrollTo(0, 0)')
                    page.mouse.move(width // 2, min(250, height - 1))
                    before = page.evaluate('() => document.scrollingElement.scrollTop')
                    page.mouse.wheel(0, 500)
                    page.wait_for_timeout(50)
                    after = page.evaluate('() => document.scrollingElement.scrollTop')
                    assert after > before, {'before': before, 'after': after, **metrics}
                    last = page.get_by_label('Effort codex.profile_29', exact=True)
                    last.scroll_into_view_if_needed()
                    last.click()
                    box = last.bounding_box()
                    assert box and 0 <= box['y'] < height
                    save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                    if height < 600:
                        page.get_by_role('button', name='Save all changes', exact=True).scroll_into_view_if_needed()
                        save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                    assert save and 0 <= save['y'] < height
                    for tab, display_name in [('Teams', 'Team 39'), ('Workflows', 'Workflow 39'), ('Prompts', 'Scroll test 39')]:
                        page.get_by_role('tab', name=tab, exact=True).click()
                        nav = page.locator('.sidebar-editor-navigation')
                        nav.wait_for(state='visible')
                        if tab == 'Workflows':
                            nav.get_by_role('button', name='Defaults', exact=True).click()
                            default_workflow = page.get_by_role('combobox', name='Default workflow', exact=True)
                            default_workflow.focus()
                            default_workflow.fill('Workflow 00')
                            default_workflow.press('ArrowDown')
                            default_workflow.press('Enter')
                            assert default_workflow.input_value() == 'Workflow 00'
                            default_workflow.fill('Managed')
                            default_workflow.press('ArrowDown')
                            default_workflow.press('Enter')
                            assert default_workflow.input_value() == 'Managed'
                            if compact:
                                page.get_by_role('button', name='← Back to Workflows', exact=True).click()
                                nav.wait_for(state='visible')
                        row = nav.get_by_role('button', name=display_name, exact=True)
                        row.scroll_into_view_if_needed()
                        item_id = row.get_attribute('data-sidebar-editor-item')
                        before_list_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                        row.click()
                        page.locator('.sidebar-editor-detail').wait_for(state='visible')
                        metrics = document_metrics(page)
                        assert metrics['detailHeight'] > 0, metrics
                        assert metrics['detailContent'] <= metrics['detailHeight'] + 1, metrics
                        assert metrics['detailScroll'] == 0, metrics
                        assert not metrics['overflow'], metrics
                        if width >= 960 and height >= 600:
                            assert metrics['navContent'] > metrics['navHeight'], metrics
                        else:
                            assert not nav.is_visible()
                            assert metrics['navScroll'] == 0, metrics
                            assert page.evaluate("() => document.activeElement?.closest('.sidebar-editor-detail') !== null")
                        save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                        if height < 600:
                            page.get_by_role('button', name='Save all changes', exact=True).scroll_into_view_if_needed()
                            save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                        assert save and 0 <= save['y'] < height and save['x'] + save['width'] <= width
                        if compact:
                            page.get_by_role('button', name=f'← Back to {tab}', exact=True).click()
                            nav.wait_for(state='visible')
                            page.wait_for_timeout(50)
                            after_back_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                            assert abs(after_back_scroll - before_list_scroll) <= 2, {
                                'before': before_list_scroll, 'after': after_back_scroll, 'tab': tab,
                            }
                            assert page.evaluate('() => document.activeElement?.dataset.sidebarEditorItem') == item_id
                            # The same selected row is a valid re-entry point.
                            row.click()
                            page.locator('.sidebar-editor-detail').wait_for(state='visible')
                        nav = page.locator('.sidebar-editor-navigation')
                        if compact:
                            page.get_by_role('button', name=f'← Back to {tab}', exact=True).click()
                            nav.wait_for(state='visible')
                            page.wait_for_timeout(50)
                        nav.get_by_role('button', name=display_name.replace('39', '38'), exact=True).click()
                        assert document_metrics(page)['detailScroll'] == 0
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
    for index in range(40):
        long_plan = root / 'plans' / 'todo' / f'long-plan-{index:02}.md'
        long_plan.write_text(
            f'# Long plan {index}\n\n'
            'This plan keeps the list naturally scrollable. ' * 40
            + '\n'
        )
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
            page.get_by_role('button', name='long-plan-39.md', exact=False).wait_for()
            plan_list_height = page.evaluate('() => document.scrollingElement.scrollHeight')
            assert plan_list_height > page.viewport_size['height']
            page.evaluate('window.scrollTo(0, 0)')
            page.mouse.move(600, 300)
            before = page.evaluate('() => document.scrollingElement.scrollTop')
            page.mouse.wheel(0, 500)
            page.wait_for_timeout(50)
            after = page.evaluate('() => document.scrollingElement.scrollTop')
            assert after > before, {'before': before, 'after': after, 'height': plan_list_height}
            page.get_by_label('New plan filename').scroll_into_view_if_needed()
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
            # Long plan text stays exact in the native editor while the page
            # continues to own ordinary vertical movement.
            long_content = content + '\n' + ('Long plan content. ' * 2000) + '\n'
            area.fill(long_content)
            page.get_by_role('button', name='Save', exact=True).click()
            page.wait_for_timeout(500)
            assert page.get_by_label('Plan content').input_value() == long_content
            # Skeleton placeholders do not block promotion: no new validator.
            page.get_by_role('button', name='Move to Ready', exact=True).click()
            page.get_by_text('Ready — runnable').wait_for()
            assert (root / 'plans' / 'in-progress' / 'draft-smoke.md').read_text() == long_content
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
    cache = Path.home() / "Library" / "Caches" if sys.platform == "darwin" else Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(cache / "ms-playwright")))
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
            page.add_init_script("""
                (() => {
                    const nativeFetch = window.fetch.bind(window);
                    let skillsListRequests = 0;
                    window.__releaseAflowSkillsList = null;
                    window.fetch = async (input, init) => {
                        const response = await nativeFetch(input, init);
                        const url = typeof input === 'string' ? input : input.url;
                        if (new URL(url, window.location.href).pathname === '/api/skills') {
                            skillsListRequests += 1;
                            if (skillsListRequests === 2) {
                                await new Promise(resolve => {
                                    window.__releaseAflowSkillsList = resolve;
                                });
                                window.__releaseAflowSkillsList = null;
                            }
                        }
                        return response;
                    };
                })();
            """)
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
                    compact = width < 960 or height < 600
                    if compact:
                        nav = page.locator('.sidebar-editor-navigation')
                        nav.wait_for(state='visible')
                        row = nav.get_by_role('button', name='aflow-plan', exact=True)
                        row.scroll_into_view_if_needed()
                        before_list_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                        row.click()
                        page.locator('.sidebar-editor-detail').wait_for(state='visible')
                        assert page.evaluate("() => document.activeElement?.closest('.sidebar-editor-detail') !== null")
                        page.get_by_role('button', name='← Back to Skills', exact=True).click()
                        nav.wait_for(state='visible')
                        page.wait_for_timeout(50)
                        assert abs(page.evaluate('() => document.scrollingElement.scrollTop') - before_list_scroll) <= 2
                        assert page.evaluate('() => document.activeElement?.dataset.sidebarEditorItem') == 'aflow-plan'
                        row.click()
                        page.locator('.sidebar-editor-detail').wait_for(state='visible')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                    assert save and 0 <= save['y'] < height and save['x'] + save['width'] <= width
            # One shared install action on the clean registry, then an edit.
            page.get_by_role('button', name='Install/reinstall all', exact=True).click()
            page.wait_for_function("typeof window.__releaseAflowSkillsList === 'function'")
            area = page.get_by_label('SKILL.md for aflow-plan', exact=True)
            assert area.is_disabled()
            assert page.get_by_text('Install finished', exact=False).count() == 0
            page.evaluate("window.__releaseAflowSkillsList()")
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

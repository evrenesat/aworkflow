"""Real browser assertions for document scrolling and reachable settings."""
import json
import os
import pytest
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
        const nav = [...document.querySelectorAll('.sidebar-editor-navigation')]
            .find(element => !element.closest('[hidden]') && !element.hasAttribute('hidden'));
        const detail = [...document.querySelectorAll('.sidebar-editor-detail')]
            .find(element => !element.closest('[hidden]') && !element.hasAttribute('hidden'))
            ?? document.querySelector('.settings-guided-content > .settings-body');
        const rowTwo = document.querySelector('.app-header-row-two');
        const mainChild = document.querySelector('.workspace-main')?.firstElementChild;
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
            headerBottom: rowTwo?.getBoundingClientRect().bottom ?? null,
            contentTop: mainChild?.getBoundingClientRect().top ?? null,
            overflow: document.documentElement.scrollWidth > innerWidth};
    }''')


def select_settings_section(page, name):
    selector = page.get_by_role('combobox', name='Settings section', exact=True)
    if selector.count():
        selector.select_option(label=name)
    else:
        page.get_by_role('tab', name=name, exact=True).click()


def open_settings_more(page):
    page.get_by_role('button', name='More', exact=True).click()


def wait_for_changelog_read_only(page):
    page.wait_for_function("""() => ![...document.querySelectorAll('button')]
        .some(button => button.textContent?.trim() === 'Save all changes')""")


def launch_test_browser(playwright):
    browser_name = os.environ.get('AFLOW_TEST_BROWSER', 'chromium').strip().lower()
    if browser_name not in {'chromium', 'webkit'}:
        raise pytest.UsageError('AFLOW_TEST_BROWSER must be chromium or webkit')
    browser_type = getattr(playwright, browser_name)
    launch = {'headless': True}
    if browser_name == 'chromium':
        launch['args'] = ['--no-sandbox']
    return browser_type.launch(**launch)


def test_changelog_settings_responsive_journey(control_client, tmp_path, monkeypatch):
    """Exercise the release view and draft ownership in real browsers."""
    _, root, _, _ = control_client
    from aflow_app_server import config as config_module, main

    config_dir = root.parent / 'global'
    monkeypatch.setattr(main, 'global_config_dir', lambda: config_dir)
    monkeypatch.setattr(config_module, 'global_config_dir', lambda: config_dir)
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))
    generated = json.loads((dist.parent / 'src' / 'generated' / 'changelog.json').read_text())
    entries = generated['entries']
    assert len(entries) > 20
    viewports = ((320, 568), (390, 844), (768, 1024), (844, 390), (1280, 720), (1440, 900), (390, 420))

    with live_server() as url, sync_playwright() as playwright:
        browser = launch_test_browser(playwright)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 720})
            page.goto(url)
            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            page.get_by_role('button', name='Settings', exact=True).click()
            draft_value = 'unsaved-changelog-round-trip.example'

            select_settings_section(page, 'Changelog')
            open_settings_more(page)
            assert page.get_by_role('menuitem', name='Reload server settings', exact=True).count() == 0
            assert page.get_by_role('menuitem', name='Install skills', exact=True).count() == 0
            assert page.get_by_role('menuitem', name='Advanced TOML', exact=True).count() == 1
            page.get_by_role('menuitem', name='Advanced TOML', exact=True).click()
            page.get_by_label('aflow.toml contents').wait_for()
            assert page.get_by_role('button', name='Save all changes', exact=True).count() == 1
            open_settings_more(page)
            page.get_by_role('menuitem', name='Guided settings', exact=True).click()
            page.get_by_role('heading', name='Changelog', exact=True).wait_for()
            wait_for_changelog_read_only(page)
            assert page.get_by_role('button', name='Save all changes', exact=True).count() == 0

            for theme in ('light', 'dark'):
                for width, height in viewports:
                    page.set_viewport_size({'width': width, 'height': height})
                    page.wait_for_timeout(75)
                    select_settings_section(page, 'General')
                    page.get_by_label('Color theme').select_option(theme)
                    if theme == 'light' and (width, height) == viewports[0]:
                        page.get_by_label('Bind host').fill(draft_value)
                    select_settings_section(page, 'Changelog')
                    page.get_by_role('heading', name='Changelog', exact=True).wait_for()
                    page.get_by_text('Changes included in this version', exact=True).wait_for()
                    wait_for_changelog_read_only(page)
                    if width < 1200 or height < 600:
                        assert page.get_by_role('combobox', name='Settings section', exact=True).input_value() == 'Changelog'
                    else:
                        assert page.get_by_role('tab', name='Changelog', exact=True).get_attribute('aria-selected') == 'true'
                    assert page.get_by_role('button', name='Save all changes', exact=True).count() == 0
                    assert page.get_by_role('button', name='Reload server settings', exact=True).count() == 0
                    open_settings_more(page)
                    assert page.get_by_role('menuitem', name='Reload server settings', exact=True).count() == 0
                    assert page.get_by_role('menuitem', name='Install skills', exact=True).count() == 0
                    assert page.get_by_role('menuitem', name='Advanced TOML', exact=True).count() == 1
                    page.keyboard.press('Escape')

                    titles = page.locator('[data-changelog-title]')
                    assert titles.count() == 20
                    assert titles.first.inner_text() == entries[0]['title']
                    assert titles.nth(19).inner_text() == entries[19]['title']
                    assert titles.nth(20).count() == 0
                    show_more = page.get_by_role('button', name='Show more', exact=True)
                    show_more.focus()
                    assert page.evaluate('() => document.activeElement?.getAttribute("aria-controls") === "changelog-entries"')
                    page.keyboard.press('Enter')
                    assert titles.count() == 40
                    assert titles.nth(39).inner_text() == entries[39]['title']

                    metrics = page.evaluate('''() => {
                        const root = document.scrollingElement;
                        const rowTwo = document.querySelector('.app-header-row-two');
                        const main = document.querySelector('.workspace-main');
                        const scrollers = [...document.querySelectorAll('*')]
                          .filter(element => {
                            if (element === root) return false;
                            const style = getComputedStyle(element);
                            return /(auto|scroll)/.test(style.overflowY)
                              && element.scrollHeight > element.clientHeight + 1;
                          })
                          .filter(element => !['TEXTAREA', 'SELECT'].includes(element.tagName)
                            && element.getAttribute('role') !== 'menu')
                          .map(element => String(element.className || ''));
                        return {
                            documentHeight: root?.scrollHeight ?? 0,
                            viewportHeight: innerHeight,
                            documentWidth: document.documentElement.scrollWidth,
                            viewportWidth: innerWidth,
                            headerBottom: rowTwo?.getBoundingClientRect().bottom ?? null,
                            contentTop: main?.getBoundingClientRect().top ?? null,
                            scrollers,
                        };
                    }''')
                    assert metrics['documentHeight'] > metrics['viewportHeight'], metrics
                    assert metrics['documentWidth'] <= metrics['viewportWidth'] + 1, metrics
                    assert not metrics['scrollers'], metrics
                    if width >= 960 and height >= 600:
                        assert metrics['headerBottom'] <= 112, metrics
                        assert metrics['contentTop'] <= 128, metrics
                    else:
                        page.get_by_role('button', name='Menu', exact=True).wait_for()
                        assert page.evaluate('''() => [...document.styleSheets].some(sheet => {
                            try { return [...sheet.cssRules].some(rule => rule.cssText.includes('safe-area-inset-top')) }
                            catch { return false }
                        })''')

                    page.evaluate('window.scrollTo(0, 0)')
                    before_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                    page.mouse.move(width // 2, min(250, height - 1))
                    page.mouse.wheel(0, max(240, height // 2))
                    page.wait_for_timeout(50)
                    after_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                    assert after_scroll > before_scroll, {'before': before_scroll, 'after': after_scroll, **metrics}
                    assert page.locator('[data-changelog-title]').evaluate_all('''elements => elements.every(element => {
                        const style = getComputedStyle(element);
                        return style.overflowWrap === 'anywhere' || style.overflowWrap === 'break-word';
                    })''')

                    page.screenshot(path=str(tmp_path / f'changelog-{theme}-{width}x{height}.png'))
                    select_settings_section(page, 'General')
                    assert page.get_by_label('Bind host').input_value() == draft_value
                    save = page.get_by_role('button', name='Save all changes', exact=True)
                    save.wait_for()
                    assert save.count() == 1
                    assert save.is_enabled()
        finally:
            browser.close()


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
            for width, height in ((960, 720), (1024, 720), (1280, 720), (1440, 900)):
                page.set_viewport_size({'width': width, 'height': height})
                page.wait_for_timeout(100)
                page.evaluate('window.scrollTo(0, 0)')
                select_settings_section(page, 'Agents & Roles')
                if width < 1200:
                    section_selector = page.get_by_role('combobox', name='Settings section', exact=True)
                    section_selector.wait_for()
                    assert page.get_by_role('tab').count() == 0
                else:
                    page.get_by_role('tab', name='Agents & Roles', exact=True).wait_for()
                    assert page.get_by_role('combobox', name='Settings section', exact=True).count() == 0
                metrics = document_metrics(page)
                assert metrics['headerBottom'] <= 112, metrics
                assert metrics['contentTop'] <= 128, metrics
                if width == 960:
                    page.get_by_label('Effort codex.profile_29', exact=True).fill('dirty header test')
                    page.wait_for_timeout(50)
                    dirty_metrics = document_metrics(page)
                    assert dirty_metrics['headerBottom'] <= 112, dirty_metrics
                    assert dirty_metrics['contentTop'] <= 128, dirty_metrics
            for theme in ('light', 'dark'):
                select_settings_section(page, 'General')
                page.get_by_label('Color theme').select_option(theme)
                for width, height in ((1365, 900), (390, 844), (844, 390)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.wait_for_timeout(100)
                    compact = width < 960 or height < 600
                    select_settings_section(page, 'Agents & Roles')
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
                        select_settings_section(page, tab)
                        nav = page.get_by_role('navigation', name=tab, exact=True)
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
                        page.locator(f'[data-sidebar-editor-list="{tab}"] .sidebar-editor-detail').wait_for(state='visible')
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
                            page.locator(f'[data-sidebar-editor-list="{tab}"] .sidebar-editor-detail').wait_for(state='visible')
                        nav = page.get_by_role('navigation', name=tab, exact=True)
                        if compact:
                            page.get_by_role('button', name=f'← Back to {tab}', exact=True).click()
                            nav.wait_for(state='visible')
                            page.wait_for_timeout(50)
                        nav.get_by_role('button', name=display_name.replace('39', '38'), exact=True).click()
                        assert document_metrics(page)['detailScroll'] == 0
            open_settings_more(page)
            page.get_by_role('menuitem', name='Advanced TOML', exact=True).click()
            page.get_by_label('aflow.toml contents', exact=True).wait_for()
            assert page.get_by_role('tab').count() == 0
            assert page.locator('.sidebar-editor-layout').count() == 0
            aflow_editor = page.get_by_label('aflow.toml contents', exact=True)
            long_toml = aflow_editor.input_value() + '\n# CP4 long TOML comment ' + ('t' * 20000) + '\n'
            aflow_editor.fill(long_toml)
            wrap_toggle = aflow_editor.locator('xpath=..').get_by_role(
                'checkbox', name='Wrap lines', exact=True
            )
            assert wrap_toggle.is_checked()
            wrap_toggle.uncheck()
            assert aflow_editor.get_attribute('wrap') == 'off'
            assert aflow_editor.evaluate('(element) => element.scrollWidth > element.clientWidth')
            wrap_toggle.check()
            assert aflow_editor.get_attribute('wrap') == 'soft'
            page.get_by_role('button', name='Save all changes', exact=True).click()
            page.get_by_text('Workflow settings saved; new runs use the saved configuration', exact=False).wait_for()
            assert ('# CP4 long TOML comment ' + ('t' * 20000)) in config.read_text()
            open_settings_more(page)
            page.get_by_role('menuitem', name='Guided settings', exact=True).click()
            if compact:
                section_selector = page.get_by_role('combobox', name='Settings section', exact=True)
                section_selector.wait_for()
                section_labels = section_selector.locator('option').all_text_contents()
                assert {'General', 'Agents & Roles', 'Skills'}.issubset(
                    {label.strip() for label in section_labels}
                )
            else:
                page.get_by_role('tab', name='Agents & Roles', exact=True).wait_for()
                assert page.get_by_role('tab', name='General', exact=True).count() == 1
                assert page.get_by_role('tab', name='Skills', exact=True).count() == 1
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
            open_settings_more(page)
            page.get_by_role('menuitem', name='Move to Ready', exact=True).click()
            page.locator('.header-context-title').filter(has_text='Ready').wait_for()
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
            select_settings_section(page, 'Skills')
            nav = page.get_by_role('navigation', name='Skills', exact=True)
            nav.get_by_role('button', name='aflow-plan', exact=True).click()
            area = page.get_by_label('SKILL.md for aflow-plan', exact=True)
            area.wait_for()
            original = area.input_value()
            assert 'aflow-plan' in original
            for theme in ('light', 'dark'):
                select_settings_section(page, 'General')
                page.get_by_label('Color theme').select_option(theme)
                select_settings_section(page, 'Skills')
                for width, height in ((1280, 720), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.wait_for_timeout(100)
                    compact = width < 960 or height < 600
                    assert page.get_by_role('heading', name='Install skills', exact=True).count() == 0
                    if compact:
                        nav = page.get_by_role('navigation', name='Skills', exact=True)
                        if nav.is_hidden():
                            page.get_by_role('button', name='← Back to Skills', exact=True).click()
                        nav.wait_for(state='visible')
                        row = nav.get_by_role('button', name='aflow-plan', exact=True)
                        row.scroll_into_view_if_needed()
                        before_list_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                        row.click()
                        page.locator('[data-sidebar-editor-list="Skills"] .sidebar-editor-detail').wait_for(state='visible')
                        assert page.evaluate("() => document.activeElement?.closest('.sidebar-editor-detail') !== null")
                        page.get_by_role('button', name='← Back to Skills', exact=True).click()
                        nav.wait_for(state='visible')
                        page.wait_for_timeout(50)
                        assert abs(page.evaluate('() => document.scrollingElement.scrollTop') - before_list_scroll) <= 2
                        assert page.evaluate('() => document.activeElement?.dataset.sidebarEditorItem') == 'aflow-plan'
                        row.click()
                        page.locator('[data-sidebar-editor-list="Skills"] .sidebar-editor-detail').wait_for(state='visible')
                    area_box = page.get_by_label('SKILL.md for aflow-plan', exact=True).bounding_box()
                    assert area_box and area_box['y'] <= (280 if compact else 208), area_box
                    assert area_box['height'] >= 280, area_box
                    assert page.get_by_label('SKILL.md for aflow-plan', exact=True).evaluate(
                        '(element) => element.clientHeight >= 280'
                    )
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    save = page.get_by_role('button', name='Save all changes', exact=True).bounding_box()
                    assert save and 0 <= save['y'] < height and save['x'] + save['width'] <= width
            # One shared install action on the clean registry, then an edit.
            open_settings_more(page)
            page.get_by_role('menuitem', name='Install skills', exact=True).click()
            page.get_by_role('button', name='Install/reinstall all', exact=True).click()
            page.wait_for_function("typeof window.__releaseAflowSkillsList === 'function'")
            area = page.get_by_label('SKILL.md for aflow-plan', exact=True)
            assert area.is_disabled()
            assert page.get_by_text('Install finished', exact=False).count() == 0
            page.evaluate("window.__releaseAflowSkillsList()")
            page.get_by_text('Install finished').wait_for()
            page.get_by_role('button', name='Hide', exact=True).click()
            assert page.get_by_role('heading', name='Install skills', exact=True).count() == 0
            open_settings_more(page)
            page.get_by_role('menuitem', name='Install skills', exact=True).click()
            page.get_by_role('heading', name='Install skills', exact=True).wait_for()
            page.get_by_text('Install finished').wait_for()
            long_markdown = original + '\n\n' + ('Browser long Markdown line ' + ('x' * 20000) + '\n')
            area.fill(long_markdown)
            wrap_toggle = page.get_by_role('checkbox', name='Wrap lines', exact=True)
            editor_box = area.bounding_box()
            toolbar_box = page.locator('.skill-editor-control').bounding_box()
            assert editor_box and toolbar_box
            intersects = (
                toolbar_box['x'] < editor_box['x'] + editor_box['width']
                and toolbar_box['x'] + toolbar_box['width'] > editor_box['x']
                and toolbar_box['y'] < editor_box['y'] + editor_box['height']
                and toolbar_box['y'] + toolbar_box['height'] > editor_box['y']
            )
            assert not intersects, {'toolbar': toolbar_box, 'editor': editor_box}
            assert wrap_toggle.is_checked()
            assert area.get_attribute('wrap') == 'soft'
            area.evaluate('''element => {
                element.focus();
                element.scrollTop = element.scrollHeight;
                element.setSelectionRange(element.value.length, element.value.length);
            }''')
            assert area.evaluate('(element) => element.scrollTop > 0')
            caret_before = area.evaluate('(element) => element.selectionStart')
            area.press('ArrowLeft')
            assert area.evaluate('(element) => element.selectionStart') == caret_before - 1
            area.press('ArrowRight')
            assert area.input_value() == long_markdown
            for offset in (12, min(80, editor_box['height'] - 12)):
                assert page.evaluate('''({x, y}) => {
                    const target = document.elementFromPoint(x, y);
                    return target instanceof HTMLTextAreaElement || target?.closest('textarea') !== null;
                }''', {'x': editor_box['x'] + min(24, editor_box['width'] / 2), 'y': editor_box['y'] + offset})
            screenshot_path = tmp_path / f'skills-scrolled-{theme}.png'
            page.screenshot(path=str(screenshot_path))
            print('SKILLS_SCROLLED_SCREENSHOT', screenshot_path)
            wrap_toggle.uncheck()
            assert area.get_attribute('wrap') == 'off'
            assert area.evaluate('(element) => element.scrollWidth > element.clientWidth')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            wrap_toggle.check()
            assert area.get_attribute('wrap') == 'soft'
            assert area.input_value() == long_markdown
            assert area.evaluate('(element) => element.scrollHeight > element.clientHeight')
            area.evaluate('(element) => { element.scrollTop = 0 }')
            before_document_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
            area.evaluate('(element) => { element.scrollTop = element.scrollHeight }')
            assert area.evaluate('(element) => element.scrollTop > 0')
            assert page.evaluate('() => document.scrollingElement.scrollTop') == before_document_scroll
            install = page.get_by_role('button', name='Install/reinstall all', exact=True)
            assert install.is_disabled()
            assert page.get_by_text('Save your skill edits first').count() > 0
            page.get_by_role('button', name='Save all changes', exact=True).click()
            page.get_by_text('the next manager invocation uses it').wait_for()
            for destination in ('.claude/skills', '.agents/skills', '.kiro/skills', '.zcode/skills'):
                linked = home / destination / 'aflow-plan' / 'SKILL.md'
                assert linked.read_text() == long_markdown, linked
                assert os.readlink(home / destination / 'aflow-plan') == str(
                    home / '.config' / 'aflow' / 'skills' / 'aflow-plan'
                )

            failed_save = {'enabled': True}

            def fail_plan_save(route):
                if route.request.method == 'PUT' and failed_save['enabled']:
                    route.fulfill(status=500, content_type='application/json', body=json.dumps({'detail': 'browser partial failure'}))
                else:
                    route.continue_()

            page.route('**/api/skills/aflow-plan', fail_plan_save)
            page.get_by_role('button', name='← Back to Skills', exact=True).click()
            nav = page.get_by_role('navigation', name='Skills', exact=True)
            nav.wait_for(state='visible')
            assistant_row = nav.get_by_role('button', name='aflow-assistant (optional)', exact=True)
            assistant_row.click()
            assistant_area = page.get_by_label('SKILL.md for aflow-assistant', exact=True)
            assistant_area.wait_for()
            assistant_partial = assistant_area.input_value() + '\n\nPartial-save assistant edit.\n'
            assistant_area.fill(assistant_partial)
            page.get_by_role('button', name='← Back to Skills', exact=True).click()
            nav.wait_for(state='visible')
            nav.get_by_role('button', name='aflow-plan', exact=True).click()
            area = page.get_by_label('SKILL.md for aflow-plan', exact=True)
            partial_plan = long_markdown + '\nPartial-save plan edit.\n'
            area.fill(partial_plan)
            page.get_by_role('button', name='Save all changes', exact=True).click()
            page.get_by_text('Skill aflow-plan was not saved', exact=False).wait_for()
            assert area.input_value() == partial_plan
            page.get_by_role('button', name='← Back to Skills', exact=True).click()
            nav.wait_for(state='visible')
            assert 'unsaved' not in nav.get_by_role('button', name='aflow-assistant (optional)', exact=True).inner_text()
            plan_row = nav.get_by_role('button', name='aflow-plan · unsaved', exact=True)
            assert plan_row.count() == 1
            plan_row.click()
            area = page.get_by_label('SKILL.md for aflow-plan', exact=True)
            failed_save['enabled'] = False
            page.get_by_role('button', name='Save all changes', exact=True).click()
            page.get_by_text('the next manager invocation uses it').wait_for()
            assert (home / '.claude' / 'skills' / 'aflow-plan' / 'SKILL.md').read_text() == partial_plan
        finally:
            browser.close()


def test_dirty_mobile_skills_presentation_survives_changelog(control_client, tmp_path, monkeypatch):
    """Retain one unsaved bundled editor through Settings sections without writes."""
    from aflow_app_server import main, config as config_module

    _, root, _, _ = control_client
    home = tmp_path / 'skills-home'
    home.mkdir()
    cache = Path.home() / 'Library' / 'Caches' if sys.platform == 'darwin' else Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache')))
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', os.environ.get('PLAYWRIGHT_BROWSERS_PATH', str(cache / 'ms-playwright')))
    monkeypatch.setenv('HOME', str(home))
    config_dir = root.parent / 'global'
    monkeypatch.setattr(main, 'global_config_dir', lambda: config_dir)
    monkeypatch.setattr(config_module, 'global_config_dir', lambda: config_dir)
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))

    with live_server() as url, sync_playwright() as playwright:
        browser = launch_test_browser(playwright)
        try:
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.goto(url)
            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            page.get_by_role('button', name='Menu', exact=True).click()
            page.get_by_role('menuitem', name='Settings', exact=True).click()
            select_settings_section(page, 'Skills')

            nav = page.get_by_role('navigation', name='Skills', exact=True)
            row = nav.get_by_role('button', name='aflow-plan', exact=True)
            row.wait_for()
            row.click()
            area = page.get_by_label('SKILL.md for aflow-plan', exact=True)
            area.wait_for()
            original = area.input_value()
            dirty = original + '\n' + ('Long retained mobile Markdown. ' * 600) + '\n'
            area.fill(dirty)
            wrap = page.get_by_role('checkbox', name='Wrap lines', exact=True)
            wrap.uncheck()
            assert area.input_value() == dirty
            assert area.get_attribute('wrap') == 'off'
            editor_box = area.bounding_box()
            geometry = page.evaluate('''() => Object.fromEntries([
                ['heading', '.sidebar-editor-detail-heading'],
                ['back', '.sidebar-editor-back'],
                ['title', '.skill-editor-title'],
                ['control', '.skill-editor-control'],
                ['editor', 'textarea[aria-label="SKILL.md for aflow-plan"]'],
            ].map(([name, selector]) => {
                const box = document.querySelector(selector)?.getBoundingClientRect();
                return [name, box && { y: box.y, height: box.height, width: box.width }];
            }))''')
            assert editor_box and editor_box['y'] <= 280 and editor_box['height'] >= 280, geometry
            assert page.locator('.skill-editor-title [role="status"]').is_visible()
            assert page.locator('.skill-editor-control').is_visible()
            page.get_by_role('button', name='← Back to Skills', exact=True).wait_for()

            for theme in ('light', 'dark'):
                select_settings_section(page, 'General')
                page.get_by_label('Color theme').select_option(theme)
                select_settings_section(page, 'Skills')
                area.wait_for()
                assert area.input_value() == dirty
                assert area.get_attribute('wrap') == 'off'
                page.screenshot(path=str(tmp_path / f'dirty-skills-{theme}-390x844.png'), full_page=True)

            select_settings_section(page, 'Changelog')
            page.get_by_role('heading', name='Changelog', exact=True).wait_for()
            page.get_by_role('button', name='Show more', exact=True).click()
            assert page.locator('[data-changelog-title]').count() == 40
            before = page.evaluate('() => document.scrollingElement.scrollTop')
            page.evaluate('''() => document.querySelector('textarea[aria-label="SKILL.md for aflow-plan"]')?.focus()''')
            assert page.evaluate('() => document.activeElement?.getAttribute("aria-label")') != 'SKILL.md for aflow-plan'
            assert page.evaluate('() => document.scrollingElement.scrollTop') == before

            select_settings_section(page, 'Skills')
            area.wait_for()
            assert area.input_value() == dirty
            assert area.get_attribute('wrap') == 'off'
            assert not nav.is_visible()
            page.get_by_role('button', name='← Back to Skills', exact=True).click()
            nav.wait_for(state='visible')
            assert page.locator('[data-sidebar-editor-list="Skills"] .sidebar-editor-detail').is_hidden()
            assert nav.locator('[data-sidebar-editor-item="aflow-plan"]').get_attribute('aria-pressed') == 'true'

            select_settings_section(page, 'Changelog')
            page.get_by_role('heading', name='Changelog', exact=True).wait_for()
            select_settings_section(page, 'Skills')
            nav.wait_for(state='visible')
            assert page.locator('[data-sidebar-editor-list="Skills"] .sidebar-editor-detail').is_hidden()
            assert nav.locator('[data-sidebar-editor-item="aflow-plan"]').get_attribute('aria-pressed') == 'true'
        finally:
            browser.close()

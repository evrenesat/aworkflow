"""Real run navigation uses disposable history records, never workflow launches."""
import json
from pathlib import Path
import re
from playwright.sync_api import expect, sync_playwright
from test_control_plane_api import control_client, live_server, TOKEN, PROJECT_ID  # noqa: F401
from test_responsive_browser import (
    _assert_document_moves,
    _assert_global_run_row,
    _assert_header_and_flow,
    _assert_last_action_hit_test,
    _assert_run_detail,
    _browser,
    _compact,
    _assert_theme,
    _login,
    _register_responsive_worktree,
    _run_history_detail,
    _run_history_navigation,
    _select_settings_section,
    _set_theme_preference,
)
from test_settings_browser import document_metrics


def open_global_destination(page, name: str, compact: bool) -> None:
    if compact:
        page.get_by_role('button', name='Menu', exact=True).click()
        page.get_by_role('menuitem', name=name, exact=True).click()
    else:
        page.get_by_role('button', name=name, exact=True).click()


def refresh_from_header(page) -> None:
    page.get_by_role('button', name='More', exact=True).click()
    page.get_by_role('menuitem', name='Refresh', exact=True).click()


def wait_for_refresh_settled(page) -> None:
    page.get_by_role('button', name='More', exact=True).click()
    expect(page.get_by_role('menuitem', name='Refresh', exact=True)).to_be_enabled()
    page.get_by_role('button', name='More', exact=True).click()


def wait_for_restored_document_scroll(page, top: float) -> None:
    page.wait_for_function(
        """(top) => {
            const scrolling = document.scrollingElement
            if (!scrolling) return false
            const target = Math.min(top, Math.max(0, scrolling.scrollHeight - innerHeight))
            return Math.abs(scrolling.scrollTop - target) <= 2
        }""",
        arg=top,
    )


def test_run_navigation_scroll_selection_and_history(control_client, monkeypatch):
    _, root, _, _ = control_client
    for index in range(130):
        path = root / '.aflow' / 'runs' / f'history-{index:03}' / 'run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'status': 'completed', 'plan_path': f'plans/history-{index:03}.md', 'workflow_name': 'managed', 'max_turns': 5}))
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={'width': 1365, 'height': 900})
            page.add_init_script("""
                (() => {
                    const nativeFetch = window.fetch.bind(window)
                    window.__restoreResponseHeld = false
                    window.__releaseRestoreResponse = null
                    window.fetch = async (input, init) => {
                        const response = await nativeFetch(input, init)
                        const requestUrl = typeof input === 'string' ? input : input.url
                        const method = init?.method ?? input?.method ?? 'GET'
                        if (method === 'POST' && new URL(requestUrl, window.location.href).pathname.endsWith('/restore')) {
                            window.__restoreResponseHeld = true
                            await new Promise(resolve => { window.__releaseRestoreResponse = resolve })
                            window.__restoreResponseHeld = false
                            window.__releaseRestoreResponse = null
                        }
                        return response
                    }
                })()
            """)
            page.goto(url)
            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            # All runs is an explicit cross-project entry. Its first compact
            # selection, and a repeat after local Back, must expose that exact
            # run detail while retaining the canonical URL identity.
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(f'{url}/?view=all-runs')
            all_run = _assert_global_run_row(
                page,
                'history-000',
                project_label='Test project',
                title='History 000',
            )
            all_run.wait_for()
            for attempt in range(2):
                all_run.click()
                page.wait_for_function("new URL(location.href).searchParams.get('project') === 'test-project' && new URL(location.href).searchParams.get('view') === 'runs'")
                selected_id = page.evaluate("() => new URL(location.href).searchParams.get('run')")
                assert selected_id == 'history-000'
                detail = _run_history_detail(page)
                detail.wait_for(state='visible')
                assert not _run_history_navigation(page).is_visible()
                _assert_run_detail(page, 'History 000', 'history-000')
                page.get_by_role('button', name='← Back to Run history', exact=True).click()
                _run_history_navigation(page).wait_for(state='visible')
                assert page.evaluate("() => new URL(location.href).searchParams.get('run')") == selected_id
                if attempt == 0:
                    open_global_destination(page, 'All runs', compact=True)
                    page.get_by_role('heading', name='All runs', exact=True).wait_for()
            # An ordinary Runs URL has no explicit run entry. The dashboard
            # still reports its default selection so the URL stays truthful,
            # but that passive replacement must leave compact history open.
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(f'{url}/?project={PROJECT_ID}&view=runs')
            nav = _run_history_navigation(page)
            nav.wait_for(state='visible')
            page.wait_for_function("new URL(location.href).searchParams.get('run') !== null")
            detail = _run_history_detail(page)
            assert not detail.is_visible()
            row = nav.locator("[data-sidebar-editor-item='history-069']")
            row.scroll_into_view_if_needed()
            before_list_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
            item_id = row.get_attribute('data-sidebar-editor-item')
            assert item_id == 'history-069'
            row.click()
            detail.wait_for(state='visible')
            assert not nav.is_visible()
            assert page.evaluate("""() => {
                const detail = document.querySelector(
                    '[data-sidebar-editor-list="Run history"] > .sidebar-editor-detail'
                )
                return detail?.contains(document.activeElement) === true
            }""")
            assert page.evaluate("() => new URL(location.href).searchParams.get('run')") == item_id
            page.get_by_role('button', name='← Back to Run history', exact=True).click()
            nav.wait_for(state='visible')
            wait_for_restored_document_scroll(page, before_list_scroll)
            restored_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
            assert abs(restored_scroll - min(before_list_scroll, page.evaluate('(top) => Math.min(top, Math.max(0, document.scrollingElement.scrollHeight - innerHeight))', before_list_scroll))) <= 2
            assert page.evaluate('() => document.activeElement?.dataset.sidebarEditorItem') == item_id
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => { document.documentElement.dataset.theme = theme }', theme)
                for width, height in ((1365, 900), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.goto(f'{url}/?project={PROJECT_ID}&view=runs&run=history-000')
                    nav = _run_history_navigation(page)
                    compact = width < 960 or height < 600
                    if compact:
                        page.get_by_role('button', name='← Back to Run history', exact=True).click()
                        nav.wait_for(state='visible')
                    page.get_by_role('button', name='Load more runs', exact=True).click()
                    page.wait_for_function("document.querySelectorAll('.run-list-item').length === 130")
                    row = nav.locator("[data-sidebar-editor-item='history-069']")
                    row.scroll_into_view_if_needed()
                    before_list_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                    item_id = row.get_attribute('data-sidebar-editor-item')
                    assert item_id == 'history-069'
                    row.click()
                    _assert_run_detail(page, 'History 069', 'history-069')
                    metrics = document_metrics(page)
                    assert metrics['detailHeight'] > 0, metrics
                    assert metrics['detailContent'] <= metrics['detailHeight'] + 1, metrics
                    assert metrics['detailScroll'] == 0, metrics
                    assert not metrics['overflow'], metrics
                    if width >= 960 and height >= 600:
                        assert metrics['navContent'] > metrics['navHeight'], metrics
                        assert metrics['navScroll'] > 0, metrics
                    else:
                        assert not nav.is_visible()
                        assert metrics['navScroll'] == 0, metrics
                        assert metrics['documentScroll'] == 0, metrics
                        # The history list, rather than the detail surface,
                        # proves ordinary document movement on compact screens.
                        assert before_list_scroll > 0, metrics
                        assert page.evaluate("""() => {
                            const detail = document.querySelector(
                                '[data-sidebar-editor-list="Run history"] > .sidebar-editor-detail'
                            )
                            return detail?.contains(document.activeElement) === true
                        }""")
                    actions = page.get_by_role('button', name='More run actions', exact=True)
                    actions.scroll_into_view_if_needed()
                    action_box = actions.bounding_box()
                    assert action_box and 0 <= action_box['y'] < height and action_box['x'] + action_box['width'] <= width
                    # Keep the current document position while exercising the
                    # existing refresh handler; a normal locator click would
                    # first scroll its off-screen trigger into view on compact
                    # stacked layouts.
                    refresh_from_header(page)
                    wait_for_refresh_settled(page)
                    assert page.locator('.run-list-item').count() == 130
                    refreshed = document_metrics(page)
                    if width >= 960 and height >= 600:
                        assert refreshed['navScroll'] == metrics['navScroll'], refreshed
                    else:
                        assert refreshed['documentScroll'] >= metrics['documentScroll'] - 2, refreshed
                        if compact:
                            page.get_by_role('button', name='← Back to Run history', exact=True).click()
                            nav.wait_for(state='visible')
                            wait_for_restored_document_scroll(page, before_list_scroll)
                            after_back_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                        restored_target = page.evaluate(
                            '(top) => Math.min(top, Math.max(0, document.scrollingElement.scrollHeight - innerHeight))',
                            before_list_scroll,
                        )
                        assert abs(after_back_scroll - restored_target) <= 2, {
                            'before': before_list_scroll, 'after': after_back_scroll, 'target': restored_target,
                        }
                        assert page.evaluate('() => document.activeElement?.dataset.sidebarEditorItem') == item_id
                    second_row = nav.locator("[data-sidebar-editor-item='history-068']")
                    assert second_row.get_attribute('data-sidebar-editor-item') == 'history-068'
                    second_row.click()
                    _assert_run_detail(page, 'History 068', 'history-068')
                    assert document_metrics(page)['detailScroll'] == 0
                    page.go_back()
                    _assert_run_detail(page, 'History 069', 'history-069')
                    assert document_metrics(page)['detailScroll'] == 0
                    page.reload()
                    _assert_run_detail(page, 'History 069', 'history-069')
            page.get_by_role('button', name='More run actions').click()
            page.get_by_role('menuitem', name='Archive', exact=True).click()
            page.get_by_role('button', name='Restore', exact=True).wait_for()
            assert page.locator("[data-sidebar-editor-item='history-069']").count() == 0
            page.get_by_role('button', name='Restore', exact=True).click()
            page.wait_for_function('window.__restoreResponseHeld === true')
            page.get_by_role('button', name='More run actions').click()
            delete_item = page.get_by_role('menuitem', name='Delete record…')
            expect(delete_item).to_be_disabled()
            page.evaluate('window.__releaseRestoreResponse()')
            page.wait_for_function('window.__restoreResponseHeld === false')
            expect(page.get_by_role('button', name='Restore', exact=True)).to_be_hidden()
            expect(delete_item).to_be_enabled()
            delete_item.click()
            confirm_delete = page.get_by_role('button', name='Confirm delete')
            expect(confirm_delete).to_be_enabled()
            confirm_delete.click()
            page.get_by_role('heading', name='Deleted record').wait_for()
            page.reload()
            page.get_by_role('heading', name='Deleted record').wait_for()
        finally:
            browser.close()


def test_complete_navigation_and_launch_journeys(control_client, monkeypatch, tmp_path):
    """Exercise the launch handoff and shell journeys in both compact modes."""
    _, root, _, _ = control_client
    ready_path = root / 'plans' / 'in-progress' / 'journey-ready.md'
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_text('# Journey ready\n\n### [ ] Checkpoint 1: Browser fixture\n- [ ] verify\n')
    run_id = 'journey-navigation-run'
    run_dir = root / '.aflow' / 'runs' / run_id
    run_dir.mkdir(parents=True)
    (run_dir / 'run.json').write_text(json.dumps({
        'status': 'completed',
        'plan_path': 'plans/in-progress/journey-ready.md',
        'workflow_name': 'managed',
        'team': 'base',
        'max_turns': 5,
    }))

    # Keep the fixture's exact global pair disposable while exposing a real
    # default team, upgrade chain, and reviewer for the launch preview.
    config_path = root.parent / 'global' / 'aflow.toml'
    config_text = config_path.read_text()
    config_text = config_text.replace(
        '[roles]\nworker = "codex.test"',
        '[roles]\nworker = "codex.test"\nreviewer = "codex.test"',
    )
    config_text += (
        '\n[teams.base]\n'
        'upgrade_to = "explicit"\n'
        '\n[teams.base.roles]\n'
        'worker = "codex.test"\n'
        'reviewer = "codex.test"\n'
        '\n[teams.explicit.roles]\n'
        'worker = "codex.test"\n'
        'reviewer = "codex.test"\n'
    )
    config_path.write_text(config_text)
    workflows_path = config_path.with_name('workflows.toml')
    workflow_text = workflows_path.read_text().replace(
        '[workflow.managed.steps.implement]',
        '[workflow.managed]\nteam = "base"\n\n[workflow.managed.steps.implement]',
    )
    workflows_path.write_text(workflow_text)

    # A linked worktree makes the long selected-context assertion use the
    # same registry projection as the shipped Projects UI.
    from aflow_app_server import main

    worktree_id = _register_responsive_worktree(root)
    assert main._project_registry is not None
    parent_name = 'AFlow long parent context'
    worktree_name = 'Doublangu worktree with a long selected context'
    assert main._project_registry.rename(PROJECT_ID, parent_name) is not None
    assert main._project_registry.rename(worktree_id, worktree_name) is not None

    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))

    def assert_document_contract(page):
        metrics = page.evaluate('''() => ({
            viewportWidth: innerWidth,
            documentWidth: document.documentElement.scrollWidth,
            viewportHeight: innerHeight,
            documentHeight: document.scrollingElement?.scrollHeight ?? 0,
            scrollTop: document.scrollingElement?.scrollTop ?? 0,
        })''')
        assert metrics['documentWidth'] <= metrics['viewportWidth'] + 1, metrics
        assert metrics['documentHeight'] >= metrics['viewportHeight'], metrics

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        page = None
        try:
            for theme in ('light', 'dark'):
                for width, height in ((1440, 900), (390, 844)):
                    if page is not None:
                        page.close()
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    _login(page, url)
                    _set_theme_preference(page, theme)
                    page.goto(f'{url}/?view=all-runs')
                    _assert_theme(page, theme)
                    page.get_by_role('heading', name='All runs', exact=True).wait_for()
                    page.get_by_label('Search loaded runs', exact=True).fill(run_id)
                    exact_row = _assert_global_run_row(
                        page,
                        run_id,
                        project_label=parent_name,
                        title='Journey ready',
                    )
                    exact_row.wait_for()
                    page.get_by_label('Search loaded runs', exact=True).fill('')
                    page.get_by_label('Run history', exact=True).select_option('all')
                    exact_row.wait_for()
                    exact_row.click()
                    _assert_run_detail(page, 'Journey ready', run_id)
                    page.wait_for_function(
                        "new URL(location.href).searchParams.get('project') === 'test-project' && "
                        f"new URL(location.href).searchParams.get('run') === '{run_id}'"
                    )
                    if _compact(page):
                        page.get_by_role('button', name='← Back to Run history', exact=True).click()
                        _run_history_navigation(page).wait_for()
                    else:
                        page.go_back()
                        page.get_by_role('heading', name='All runs', exact=True).wait_for()
                    _assert_header_and_flow(page)
                    assert_document_contract(page)

                    # Plans → Run this plan preserves the exact Ready path,
                    # resolves the workflow's default team, and accepts a
                    # keyboard-selected explicit team without changing IDs.
                    page.goto(f'{url}/?project={PROJECT_ID}&view=plans')
                    page.get_by_role('button', name='journey-ready.md', exact=False).first.click()
                    page.get_by_role('button', name='More', exact=True).click()
                    page.get_by_role('menuitem', name='Run this plan', exact=True).click()
                    plan_input = page.get_by_label('Run plan', exact=True)
                    plan_input.wait_for()
                    expect(plan_input).to_have_value('plans/in-progress/journey-ready.md')
                    workflow_input = page.get_by_label('Run workflow', exact=True)
                    expect(workflow_input).to_have_value('Managed')
                    team_family_input = page.get_by_label('Run team', exact=True)
                    expect(team_family_input).to_have_value('Base')
                    team_family_input.focus()
                    team_family_input.fill('Base')
                    team_family_input.press('ArrowDown')
                    team_family_input.press('Enter')
                    expect(team_family_input).to_be_focused()
                    expect(team_family_input).to_have_value('Base')
                    stage_input = page.get_by_label('Run team stage', exact=True)
                    expect(stage_input).to_have_value('Base')
                    stage_input.focus()
                    stage_input.fill('explicit')
                    stage_input.press('ArrowDown')
                    stage_input.press('Enter')
                    expect(stage_input).to_have_value('Explicit')
                    expect(team_family_input).to_have_value('Base')
                    expect(page.get_by_label('Effective choices for this launch')).to_contain_text(
                        'Base — your selection · baseline stage Explicit (explicit)'
                    )
                    details = page.locator('details.launch-preview-details')
                    expect(details).to_be_visible()
                    expect(details).not_to_have_attribute('open', '')
                    page.screenshot(path=str(tmp_path / f'cp3-{theme}-{width}x{height}-launch-summary.png'), full_page=True)
                    print('CP3_SCREENSHOT', tmp_path / f'cp3-{theme}-{width}x{height}-launch-summary.png')
                    details.locator('summary').first.focus()
                    details.locator('summary').first.press('Enter')
                    expect(details).to_have_attribute('open', '')
                    expect(details.locator('table').first).to_be_visible()
                    advanced = page.get_by_role('button', name='Advanced options', exact=True)
                    advanced.focus()
                    advanced.press('Enter')
                    expect(page.get_by_label('Run start step', exact=True)).to_be_visible()
                    expect(page.get_by_role('button', name='Start run', exact=True)).to_be_visible()
                    _assert_last_action_hit_test(page)
                    _assert_document_moves(page)
                    _assert_header_and_flow(page)
                    assert_document_contract(page)

                    # Settings edit → section change → save → reload retains
                    # the committed server draft through the shared owner.
                    page.goto(f'{url}/?project={PROJECT_ID}&view=settings')
                    page.get_by_role('heading', name='Settings', exact=True).wait_for()
                    _select_settings_section(page, 'Agents & Roles')
                    model = page.get_by_label('Model codex.test', exact=True)
                    model.wait_for()
                    next_model = f'browser-model-{theme}-{width}'
                    model.fill(next_model)
                    model.press('Tab')
                    save = page.locator('.app-header-row-two').get_by_role('button', name='Save all changes', exact=True)
                    save.click()
                    page.get_by_text('Workflow settings saved; new runs use the saved configuration', exact=False).wait_for()
                    _select_settings_section(page, 'General')
                    _select_settings_section(page, 'Agents & Roles')
                    expect(page.get_by_label('Model codex.test', exact=True)).to_have_value(next_model)
                    page.get_by_role('button', name='More', exact=True).click()
                    page.get_by_role('menuitem', name='Reload server settings', exact=True).click()
                    expect(page.get_by_label('Model codex.test', exact=True)).to_have_value(next_model)
                    _assert_header_and_flow(page)
                    assert_document_contract(page)

                    # The selected worktree context remains exact while the
                    # ordinary mobile Projects label stays one readable line.
                    page.goto(f'{url}/?view=projects')
                    parent = page.get_by_role('button', name=re.compile(rf'^{re.escape(parent_name)}'))
                    parent.wait_for()
                    page.locator('summary').filter(has_text='Worktrees (1)').click()
                    child = page.get_by_role('button', name=re.compile(rf'^{re.escape(worktree_name)}'))
                    child.wait_for()
                    child.click()
                    page.wait_for_function(
                        f"new URL(location.href).searchParams.get('project') === '{worktree_id}'"
                    )
                    page.goto(f'{url}/?project={worktree_id}&view=projects')
                    page.get_by_role('button', name=re.compile(rf'^{re.escape(worktree_name)}')).wait_for()
                    brand = page.locator('.app-brand-title')
                    expect(brand).to_have_attribute('aria-label', f'AFlow · {parent_name} · Worktree: {worktree_name}')
                    if _compact(page):
                        page_context = page.locator('.mobile-page-context .header-context-title')
                        page_context.wait_for(state='visible')
                        expect(page_context).to_have_text('Projects')
                        context_box = page_context.bounding_box()
                        assert context_box and context_box['height'] <= 32, context_box
                    context_image = tmp_path / f'cp3-{theme}-{width}x{height}-project-context.png'
                    page.screenshot(path=str(context_image), full_page=True)
                    print('CP3_SCREENSHOT', context_image)
                    _assert_header_and_flow(page)
                    assert_document_contract(page)
        finally:
            browser.close()


def test_remaining_journeys_keep_compact_actions_and_drafts_reachable(control_client, monkeypatch, tmp_path):
    """Exercise the remaining authenticated journeys with disposable data.

    The launch request is intercepted after the user has selected a real Ready
    plan and workflow.  That keeps this acceptance check focused on form,
    startup-question, and responsive presentation behavior without starting a
    worker, while the plan save and project/run navigation use the disposable
    server and filesystem fixture normally.
    """
    _, root, _, _ = control_client
    (root / 'plans' / 'in-progress').mkdir(parents=True, exist_ok=True)
    (root / 'plans' / 'in-progress' / 'ready-cp5.md').write_text(
        '# Ready CP5\n\n### [ ] Checkpoint 1: Disposable launch fixture\n- [ ] verify\n'
    )
    (root / '.aflow' / 'runs' / 'global-cp5').mkdir(parents=True)
    (root / '.aflow' / 'runs' / 'global-cp5' / 'run.json').write_text(json.dumps({
        'status': 'completed',
        'plan_path': 'plans/in-progress/ready-cp5.md',
        'workflow_name': 'managed',
        'max_turns': 5,
    }))
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))

    def assert_compact_geometry(page, width: int, height: int) -> None:
        metrics = page.evaluate('''() => ({
            overflow: document.documentElement.scrollWidth > innerWidth,
            documentHeight: document.scrollingElement?.scrollHeight ?? 0,
            viewportHeight: innerHeight,
        })''')
        assert not metrics['overflow'], metrics
        assert metrics['documentHeight'] >= height, metrics

    def assert_preview_captions_fill_tables(page) -> None:
        captions = page.locator('.responsive-data-table caption')
        assert captions.count() >= 2
        for index in range(captions.count()):
            metrics = captions.nth(index).evaluate('''(caption) => {
                const table = caption.closest('table')
                const captionBox = caption.getBoundingClientRect()
                const tableBox = table.getBoundingClientRect()
                return {
                    captionWidth: captionBox.width,
                    tableWidth: tableBox.width,
                    display: getComputedStyle(caption).display,
                }
            }''')
            assert metrics['display'] == 'block', metrics
            assert metrics['captionWidth'] > 0, metrics
            assert abs(metrics['captionWidth'] - metrics['tableWidth']) <= 1.5, metrics

    def assert_desktop_tables_remain_ordinary(page) -> None:
        layouts = page.locator('.responsive-data-table caption').evaluate_all('''(captions) => captions.map((caption) => ({
            captionDisplay: getComputedStyle(caption).display,
            tableDisplay: getComputedStyle(caption.closest('table')).display,
        }))''')
        assert layouts and all(
            layout['captionDisplay'] == 'table-caption' and layout['tableDisplay'] == 'table'
            for layout in layouts
        ), layouts

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={'width': 320, 'height': 844})
            page.goto(url)
            page.get_by_placeholder('Auth token').fill('wrong-token')
            page.get_by_role('button', name='Login', exact=True).click()
            login_error = page.get_by_role('alert').first
            login_error.wait_for()
            assert 'Login failed' in login_error.inner_text()
            assert login_error.evaluate('(node) => document.activeElement === node')

            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            page.goto(f'{url}/?project={PROJECT_ID}&view=plans')
            page.get_by_label('New plan filename', exact=True).wait_for()
            assert_compact_geometry(page, 320, 844)

            page.get_by_label('New plan filename', exact=True).fill('compact-cp5.md')
            page.get_by_role('button', name='Create plan', exact=True).click()
            plan_editor = page.get_by_label('Plan content', exact=True)
            plan_editor.wait_for()
            plan_editor.fill('# Compact CP5\n\nEdited on a narrow screen.\n')
            page.get_by_role('button', name='Save', exact=True).click()
            page.wait_for_timeout(150)
            assert plan_editor.input_value() == '# Compact CP5\n\nEdited on a narrow screen.\n'
            assert (root / 'plans' / 'todo' / 'compact-cp5.md').read_text() == plan_editor.input_value()
            plan_editor.scroll_into_view_if_needed()
            save_box = page.get_by_role('button', name='Save', exact=True).bounding_box()
            assert save_box and 0 <= save_box['y'] < 844 and save_box['x'] + save_box['width'] <= 320

            # The shared Back action preserves the clean editor/list flow; a
            # later dirty navigation is owned by App's existing guard.
            page.get_by_role('button', name='← Back to Plans', exact=True).click()
            page.get_by_label('New plan filename', exact=True).wait_for()
            page.get_by_label('New plan filename', exact=True).fill('dirty-cp5.md')
            page.get_by_role('button', name='Create plan', exact=True).click()
            dirty_editor = page.get_by_label('Plan content', exact=True)
            dirty_editor.wait_for()
            dirty_editor.fill('# Keep this draft\n')
            page.get_by_role('button', name='Menu', exact=True).click()
            page.get_by_role('menuitem', name='Projects', exact=True).click()
            guard = page.get_by_role('alertdialog', name='Unsaved editor edits')
            guard.wait_for()
            assert guard.evaluate('(node) => document.activeElement === node')
            page.get_by_role('button', name='Stay', exact=True).click()
            assert dirty_editor.input_value() == '# Keep this draft\n'
            page.get_by_role('button', name='Menu', exact=True).click()
            page.get_by_role('menuitem', name='Projects', exact=True).click()
            page.get_by_role('button', name='Leave anyway', exact=True).click()
            page.get_by_role('button', name='Test project', exact=False).first.wait_for()
            assert_compact_geometry(page, 320, 844)
            page.get_by_role('button', name='Add project', exact=True).click()
            project_path = page.get_by_label('Relative project path', exact=True)
            project_path.wait_for()
            project_path.fill('disposable/cp5-project')
            create_project = page.get_by_role('button', name='Create project', exact=True)
            create_project.scroll_into_view_if_needed()
            create_box = create_project.bounding_box()
            assert create_box and 0 <= create_box['y'] < 844 and create_box['x'] + create_box['width'] <= 320
            page.get_by_role('button', name='Cancel', exact=True).click()
            assert_compact_geometry(page, 320, 844)

            # Project selection returns to the project-owned Runs view without
            # replacing the selected identity with a different project.
            page.get_by_role('button', name='Test project', exact=False).first.click()
            page.wait_for_function("new URL(location.href).searchParams.get('project') === 'test-project' && new URL(location.href).searchParams.get('view') === 'runs'")
            page.get_by_role('button', name='New run', exact=True).wait_for()
            assert_compact_geometry(page, 320, 844)

            # Launch only through the real form; the disposable interception
            # returns the server-shaped startup question and never starts a
            # worker or changes a production run.
            def launch_route(route):
                if route.request.method != 'POST':
                    route.continue_()
                    return
                request = json.loads(route.request.post_data or '{}')
                assert request['plan_path'] == 'plans/in-progress/ready-cp5.md'
                assert request['workflow_name'] == 'managed'
                assert request['dirty_worktree_confirmed'] is True
                route.fulfill(status=202, content_type='application/json', body=json.dumps({
                    'result': None,
                    'startup_question': {
                        'question_id': 'cp5-question',
                        'kind': 'pick_step',
                        'message': 'Choose a step for this disposable launch',
                        'options': {},
                        'choices': ['implement'],
                        'run_id': 'cp5-pending',
                        'schema_version': 1,
                    },
                }))

            page.route(f'**/api/control-plane/projects/{PROJECT_ID}/runs', launch_route)
            page.set_viewport_size({'width': 320, 'height': 568})
            page.goto(f'{url}/?project={PROJECT_ID}&view=new-run')
            page.get_by_label('Run plan', exact=True).wait_for()
            page.get_by_label('Run plan', exact=True).click()
            page.get_by_role('option', name='ready-cp5.md', exact=False).click()
            workflow = page.get_by_label('Run workflow', exact=True)
            workflow.click()
            workflow.fill('managed')
            workflow.press('ArrowDown')
            workflow.press('Enter')
            dirty_confirmation = page.get_by_role('checkbox', name='Continue despite uncommitted changes')
            dirty_confirmation.wait_for()
            assert not dirty_confirmation.is_checked()
            dirty_confirmation.check()
            assert dirty_confirmation.is_checked()
            page.wait_for_function("""() => [...document.querySelectorAll('button')].some(
                (button) => button.textContent?.trim() === 'Start run' && !button.disabled
            )""")
            assert page.get_by_role('button', name='Start run', exact=True).is_enabled()
            launch_details = page.locator('details.launch-preview-details')
            launch_details.locator('summary').first.press('Enter')
            expect(launch_details).to_have_attribute('open', '')
            page.locator('.responsive-data-table caption').first.wait_for()
            assert_preview_captions_fill_tables(page)
            assert_compact_geometry(page, 320, 568)
            page.screenshot(path=str(tmp_path / 'cp06-compact-launch.png'), full_page=True)
            page.get_by_role('button', name='Start run', exact=True).click()
            page.get_by_text('Choose a step for this disposable launch', exact=True).wait_for()
            answer = page.get_by_role('button', name='implement', exact=True)
            answer.scroll_into_view_if_needed()
            answer_box = answer.bounding_box()
            assert answer_box and 0 <= answer_box['y'] < 844 and answer_box['x'] + answer_box['width'] <= 320
            assert_compact_geometry(page, 320, 844)

            # Short landscape uses the same compact contract and keeps the
            # launch controls in one natural column.
            page.set_viewport_size({'width': 844, 'height': 390})
            page.wait_for_timeout(100)
            assert_preview_captions_fill_tables(page)
            assert page.locator('.dashboard-form-grid').evaluate('(node) => getComputedStyle(node).gridTemplateColumns.split(" ").length === 1')
            assert_compact_geometry(page, 844, 390)

            page.set_viewport_size({'width': 1280, 'height': 720})
            page.locator('.responsive-data-table caption').first.wait_for()
            assert_desktop_tables_remain_ordinary(page)
            page.set_viewport_size({'width': 844, 'height': 390})

            # All-runs remains a populated cross-project list and explicit
            # selection enters the same exact detail/back surface.
            page.unroute(f'**/api/control-plane/projects/{PROJECT_ID}/runs', launch_route)
            page.get_by_role('button', name='Menu', exact=True).click()
            page.get_by_role('menuitem', name='All runs', exact=True).click()
            exact_global_row = _assert_global_run_row(
                page,
                'global-cp5',
                project_label='Test project',
                title='Ready cp5',
            )
            exact_global_row.wait_for()
            assert_compact_geometry(page, 844, 390)
            exact_global_row.click()
            _assert_run_detail(page, 'Ready cp5', 'global-cp5')
            page.wait_for_function(
                "new URL(location.href).searchParams.get('project') === 'test-project' && "
                "new URL(location.href).searchParams.get('run') === 'global-cp5'"
            )
            page.get_by_role('button', name='← Back to Run history', exact=True).wait_for()
            page.get_by_role('button', name='← Back to Run history', exact=True).click()
            _run_history_navigation(page).wait_for()
            assert_compact_geometry(page, 844, 390)

            # Logout failure stays actionable in the document and focus moves
            # to the newly shown alert instead of hiding it behind the header.
            def logout_route(route):
                if route.request.method == 'DELETE':
                    route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': 'disposable logout failure'}))
                else:
                    route.continue_()

            page.route('**/api/session', logout_route)
            page.get_by_role('button', name='Menu', exact=True).click()
            page.get_by_role('menuitem', name='Logout', exact=True).click()
            logout_error = page.get_by_role('alert').first
            logout_error.wait_for()
            assert logout_error.evaluate('(node) => document.activeElement === node')
            assert page.get_by_role('button', name='Retry logout', exact=True).is_visible()
            assert_compact_geometry(page, 844, 390)
        finally:
            browser.close()

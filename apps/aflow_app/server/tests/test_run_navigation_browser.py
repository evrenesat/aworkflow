"""Real run navigation uses disposable history records, never workflow launches."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_control_plane_api import control_client, live_server, TOKEN, PROJECT_ID  # noqa: F401
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


def test_run_navigation_scroll_selection_and_history(control_client, monkeypatch):
    _, root, _, _ = control_client
    for index in range(130):
        path = root / '.aflow' / 'runs' / f'history-{index:03}' / 'run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'status': 'completed', 'plan_path': f'plans/history-{index:03}.md', 'workflow_name': 'managed', 'max_turns': 5}))
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    monkeypatch.setenv('AFLOW_APP_WEB_DIST', str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1365, 'height': 900})
            page.goto(url)
            page.get_by_placeholder('Auth token').fill(TOKEN)
            page.get_by_role('button', name='Login', exact=True).click()
            # All runs is an explicit cross-project entry. Its first compact
            # selection, and a repeat after local Back, must expose that exact
            # run detail while retaining the canonical URL identity.
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(f'{url}/?view=all-runs')
            for attempt in range(2):
                page.get_by_role('button', name='Test project · Completed', exact=False).first.click()
                page.wait_for_function("new URL(location.href).searchParams.get('project') === 'test-project' && new URL(location.href).searchParams.get('view') === 'runs'")
                selected_id = page.evaluate("() => new URL(location.href).searchParams.get('run')")
                assert selected_id
                detail = page.locator('.sidebar-editor-detail')
                detail.wait_for(state='visible')
                assert not page.locator('.sidebar-editor-navigation').is_visible()
                assert detail.locator('.run-detail h3').is_visible()
                page.get_by_role('button', name='← Back to Run history', exact=True).click()
                page.locator('.sidebar-editor-navigation').wait_for(state='visible')
                assert page.evaluate("() => new URL(location.href).searchParams.get('run')") == selected_id
                if attempt == 0:
                    open_global_destination(page, 'All runs', compact=True)
                    page.get_by_role('heading', name='All runs', exact=True).wait_for()
            # An ordinary Runs URL has no explicit run entry. The dashboard
            # still reports its default selection so the URL stays truthful,
            # but that passive replacement must leave compact history open.
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(f'{url}/?project={PROJECT_ID}&view=runs')
            nav = page.locator('.sidebar-editor-navigation')
            nav.wait_for(state='visible')
            page.wait_for_function("new URL(location.href).searchParams.get('run') !== null")
            detail = page.locator('.sidebar-editor-detail')
            assert not detail.is_visible()
            row = nav.get_by_role('button', name='history-069.md', exact=False)
            row.scroll_into_view_if_needed()
            before_list_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
            item_id = row.get_attribute('data-sidebar-editor-item')
            row.click()
            detail.wait_for(state='visible')
            assert not nav.is_visible()
            assert page.evaluate("() => document.activeElement?.closest('.sidebar-editor-detail') !== null")
            assert page.evaluate("() => new URL(location.href).searchParams.get('run')") == item_id
            page.get_by_role('button', name='← Back to Run history', exact=True).click()
            nav.wait_for(state='visible')
            page.wait_for_timeout(50)
            restored_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
            assert abs(restored_scroll - min(before_list_scroll, page.evaluate('(top) => Math.min(top, Math.max(0, document.scrollingElement.scrollHeight - innerHeight))', before_list_scroll))) <= 2
            assert page.evaluate('() => document.activeElement?.dataset.sidebarEditorItem') == item_id
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => { document.documentElement.dataset.theme = theme }', theme)
                for width, height in ((1365, 900), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.goto(f'{url}/?project={PROJECT_ID}&view=runs&run=history-000')
                    nav = page.locator('.sidebar-editor-navigation')
                    compact = width < 960 or height < 600
                    if compact:
                        page.get_by_role('button', name='← Back to Run history', exact=True).click()
                        nav.wait_for(state='visible')
                    page.get_by_role('button', name='Load more runs', exact=True).click()
                    page.wait_for_function("document.querySelectorAll('.run-list-item').length === 130")
                    row = nav.get_by_role('button', name='history-069.md', exact=False)
                    row.scroll_into_view_if_needed()
                    before_list_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                    item_id = row.get_attribute('data-sidebar-editor-item')
                    row.click()
                    page.locator('.run-detail h3').filter(has_text='history-069.md').wait_for()
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
                        assert page.evaluate("() => document.activeElement?.closest('.sidebar-editor-detail') !== null")
                    actions = page.get_by_role('button', name='More run actions', exact=True)
                    actions.scroll_into_view_if_needed()
                    action_box = actions.bounding_box()
                    assert action_box and 0 <= action_box['y'] < height and action_box['x'] + action_box['width'] <= width
                    # Keep the current document position while exercising the
                    # existing refresh handler; a normal locator click would
                    # first scroll its off-screen trigger into view on compact
                    # stacked layouts.
                    refresh_from_header(page)
                    page.wait_for_timeout(250)
                    assert page.locator('.run-list-item').count() == 130
                    refreshed = document_metrics(page)
                    if width >= 960 and height >= 600:
                        assert refreshed['navScroll'] == metrics['navScroll'], refreshed
                    else:
                        assert refreshed['documentScroll'] >= metrics['documentScroll'] - 2, refreshed
                        if compact:
                            page.get_by_role('button', name='← Back to Run history', exact=True).click()
                            nav.wait_for(state='visible')
                            page.wait_for_timeout(50)
                            after_back_scroll = page.evaluate('() => document.scrollingElement.scrollTop')
                        restored_target = page.evaluate(
                            '(top) => Math.min(top, Math.max(0, document.scrollingElement.scrollHeight - innerHeight))',
                            before_list_scroll,
                        )
                        assert abs(after_back_scroll - restored_target) <= 2, {
                            'before': before_list_scroll, 'after': after_back_scroll, 'target': restored_target,
                        }
                        assert page.evaluate('() => document.activeElement?.dataset.sidebarEditorItem') == item_id
                    nav.get_by_role('button', name='history-068.md', exact=False).click()
                    if compact:
                        page.locator('.run-detail h3').filter(has_text='history-068.md').wait_for()
                    assert document_metrics(page)['detailScroll'] == 0
                    page.go_back()
                    page.locator('.run-detail h3').filter(has_text='history-069.md').wait_for()
                    assert document_metrics(page)['detailScroll'] == 0
                    page.reload()
                    page.locator('.run-detail h3').filter(has_text='history-069.md').wait_for()
            page.get_by_role('button', name='More run actions').click()
            page.get_by_role('menuitem', name='Archive', exact=True).click()
            page.get_by_role('button', name='Restore', exact=True).wait_for()
            assert page.locator('.run-list-item').filter(has_text='history-069.md').count() == 0
            page.get_by_role('button', name='Restore', exact=True).click()
            page.get_by_role('button', name='More run actions').click()
            page.get_by_role('menuitem', name='Delete record…').click()
            page.get_by_role('button', name='Confirm delete').click()
            page.get_by_role('heading', name='Deleted record').wait_for()
            page.reload()
            page.get_by_role('heading', name='Deleted record').wait_for()
        finally:
            browser.close()

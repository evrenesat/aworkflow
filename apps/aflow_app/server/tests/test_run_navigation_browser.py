"""Real run navigation uses disposable history records, never workflow launches."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_control_plane_api import control_client, live_server, TOKEN, PROJECT_ID  # noqa: F401
from test_settings_browser import pane_metrics


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
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => { document.documentElement.dataset.theme = theme }', theme)
                for width, height in ((1365, 900), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.goto(f'{url}/?project={PROJECT_ID}&view=runs&run=history-000')
                    nav = page.locator('.sidebar-editor-navigation')
                    page.get_by_role('button', name='Load more runs', exact=True).click()
                    page.wait_for_function("document.querySelectorAll('.run-list-item').length === 130")
                    nav.get_by_role('button', name='history-069.md', exact=False).click()
                    page.locator('.run-detail h3').filter(has_text='history-069.md').wait_for()
                    metrics = pane_metrics(page)
                    assert metrics['navScroll'] > 0 and metrics['navContent'] > metrics['navHeight'], metrics
                    assert metrics['detailTop'] <= metrics['headingTop'] < metrics['detailBottom'], metrics
                    assert not metrics['overflow']
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    page.wait_for_timeout(250)
                    assert page.locator('.run-list-item').count() == 130
                    assert pane_metrics(page)['navScroll'] == metrics['navScroll']
                    page.add_style_tag(content='.sidebar-editor-detail::after { content: ""; display: block; height: 1600px; }')
                    page.locator('.sidebar-editor-detail').evaluate("el => { el.scrollTop = 400 }")
                    assert pane_metrics(page)['navScroll'] == metrics['navScroll']
                    nav.get_by_role('button', name='history-068.md', exact=False).click()
                    assert pane_metrics(page)['detailScroll'] == 0
                    page.go_back()
                    page.locator('.run-detail h3').filter(has_text='history-069.md').wait_for()
                    assert pane_metrics(page)['detailScroll'] == 0
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

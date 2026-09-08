"""Real layout regression: sticky containment cannot be verified in jsdom."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from test_control_plane_api import control_client, live_server, TOKEN  # noqa: F401


def test_settings_toolbar_stays_visible_through_long_scroll(control_client, monkeypatch):
    from aflow_app_server import main, config as config_module

    _, root, _, _ = control_client
    config_dir = root.parent / "global"
    config = config_dir / "aflow.toml"
    config.write_text(config.read_text() + "\n" + "\n".join(
        f"scroll_test_{index} = {json.dumps('Long prompt content. ' * 40)}"
        for index in range(20)
    ) + "\n")
    monkeypatch.setattr(main, "global_config_dir", lambda: config_dir)
    monkeypatch.setattr(config_module, "global_config_dir", lambda: config_dir)
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    assert (dist / "index.html").exists(), "Build the web client before browser tests"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    with live_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1365, "height": 900})
            page.goto(url)
            page.get_by_placeholder("Auth token").fill(TOKEN)
            page.get_by_role("button", name="Login", exact=True).click()
            page.get_by_role("button", name="Settings", exact=True).click()
            for theme in ("light", "dark"):
                page.get_by_role("tab", name="General", exact=True).click()
                page.get_by_label("Color theme").select_option(theme)
                page.get_by_role("tab", name="Prompts", exact=True).click()
                page.get_by_label("Prompt text scroll_test_19", exact=True).wait_for()
                for width in (1365, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    for fraction in (.25, .5, .9, 1):
                        metrics = page.evaluate("""fraction => {
                            const main = document.querySelector('.workspace-main');
                            main.scrollTop = (main.scrollHeight - main.clientHeight) * fraction;
                            const bar = document.querySelector('.settings-toolbar').getBoundingClientRect();
                            return { top: bar.top, bottom: bar.bottom, mainTop: main.getBoundingClientRect().top, scroll: main.scrollTop };
                        }""", fraction)
                        assert metrics["scroll"] > 500
                        assert 0 <= metrics["top"] - metrics["mainTop"] <= 17, metrics
                        assert metrics["bottom"] < 900
                        save = page.get_by_role("button", name="Save all changes", exact=True).bounding_box()
                        assert save and save["y"] >= metrics["mainTop"]
                        assert save["x"] >= 0 and save["x"] + save["width"] <= width
        finally:
            browser.close()

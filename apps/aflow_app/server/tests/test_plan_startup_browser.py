"""Chromium coverage for truthful Ready state and startup correction."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from playwright.sync_api import expect, sync_playwright

from test_control_plane_api import (
    PROJECT_ID,
    TOKEN,
    _commit_fixture_repository,
    control_client as _control_client_fixture,  # noqa: F401
    live_server,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run_ids(root: Path) -> set[str]:
    launches = root / ".aflow" / "launches"
    runs = root / ".aflow" / "runs"
    launch_ids = {
        path.stem
        for path in launches.glob("*.json")
        if not path.stem.endswith(".state")
    } if launches.is_dir() else set()
    record_ids = {
        path.parent.name
        for path in runs.glob("*/run.json")
        if not path.parent.name.endswith(".state")
    } if runs.is_dir() else set()
    return launch_ids | record_ids


def _login(page, url: str) -> None:
    page.goto(url)
    page.get_by_placeholder("Auth token").fill(TOKEN)
    page.get_by_role("button", name="Login", exact=True).click()
    page.locator(".app-header").wait_for()


def _open_plan(page, name: str) -> None:
    page.get_by_role("button", name=name, exact=False).first.click()
    page.get_by_label("Plan content", exact=True).wait_for()


def _open_run_for_selected_plan(page) -> None:
    page.get_by_role("button", name="More", exact=True).click()
    page.get_by_role("menuitem", name="Run this plan", exact=True).click()
    page.get_by_label("Run plan", exact=True).wait_for()


def _choose_plan(page, name: str) -> None:
    plan = page.get_by_label("Run plan", exact=True)
    plan.click()
    page.get_by_role("option", name=name, exact=False).click()


def _choose_workflow(page) -> None:
    workflow = page.get_by_label("Run workflow", exact=True)
    workflow.click()
    workflow.fill("managed")
    workflow.press("ArrowDown")
    workflow.press("Enter")
    expect(workflow).to_have_value("Managed")


def _acknowledge_dirty_worktree_if_needed(page) -> None:
    page.locator(".worktree-preflight").wait_for()
    page.wait_for_function(
        """() => {
            const panel = document.querySelector('.worktree-preflight')
            if (!panel) return false
            return panel.dataset.preflightStatus === 'ready'
        }"""
    )
    confirmation = page.get_by_role(
        "checkbox",
        name="Continue despite uncommitted changes",
        exact=True,
    )
    if confirmation.count():
        confirmation.check()
    expect(page.get_by_role("button", name="Start run", exact=True)).to_be_enabled()


def test_chromium_preserves_ready_state_and_retries_a_corrected_plan(
    _control_client_fixture,  # noqa: F811
    monkeypatch,
) -> None:
    _, root, units, _ = _control_client_fixture
    workflow_config = root.parent / "global" / "aflow.toml"
    workflow_config.write_text(
        workflow_config.read_text(encoding="utf-8").replace(
            'p = "Work."', 'p = "Use aflow-review-checkpoint."'
        ),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".aflow/\n", encoding="utf-8")
    _commit_fixture_repository(root)
    branch = _git(root, "symbolic-ref", "--short", "HEAD")
    base_head = _git(root, "rev-parse", "HEAD")

    numbered = (
        "# Numbered ready plan\n\n"
        "## 3. Git Tracking\n\n"
        f"- Plan Branch: `{branch}`\n"
        f"- Pre-Handoff Base HEAD: `{base_head}`\n\n"
        "### [ ] Checkpoint 1: Start\n"
        "- [ ] verify the numbered heading\n"
    )
    numbered_path = root / "plans" / "in-progress" / "numbered-ready.md"
    numbered_path.parent.mkdir(parents=True, exist_ok=True)
    numbered_path.write_text(numbered, encoding="utf-8")

    duplicate = (
        "# Duplicate tracking plan\n\n"
        "## Git Tracking\n\n"
        f"- Plan Branch: `{branch}`\n"
        f"- Pre-Handoff Base HEAD: `{base_head}`\n\n"
        "## 3. Git Tracking\n\n"
        f"- Plan Branch: `{branch}`\n"
        f"- Pre-Handoff Base HEAD: `{base_head}`\n\n"
        "### [ ] Checkpoint 1: Start\n"
        "- [ ] verify the numbered heading\n"
    )
    corrected = (
        "# Duplicate tracking plan\n\n"
        "## Git Tracking\n\n"
        f"- Plan Branch: `{branch}`\n"
        f"- Pre-Handoff Base HEAD: `{base_head}`\n\n"
        "### [ ] Checkpoint 1: Start\n"
        "- [ ] verify the numbered heading\n"
    )
    duplicate_path = root / "plans" / "in-progress" / "duplicate-ready.md"

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))

    with live_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=plans")

            _open_plan(page, "numbered-ready.md")
            expect(page.locator(".header-context-title").first).to_contain_text("Ready")
            expect(page.get_by_text(
                "Ready is a plan lifecycle state; startup checks run when you start the plan.",
                exact=True,
            )).to_be_visible()
            assert page.get_by_text("Ready — runnable", exact=True).count() == 0
            _open_run_for_selected_plan(page)
            expect(page.get_by_label("Run plan", exact=True)).to_have_value(
                "plans/in-progress/numbered-ready.md"
            )
            _choose_workflow(page)
            _acknowledge_dirty_worktree_if_needed(page)
            page.get_by_role("button", name="Start run", exact=True).click()
            assert page.locator(".run-detail h3").inner_text() == "Numbered ready"
            assert len(units.start_calls) == 1
            assert numbered_path.read_text(encoding="utf-8") == numbered

            duplicate_path.write_text(duplicate, encoding="utf-8")
            page.get_by_role("button", name="Plans", exact=True).click()
            page.get_by_label("New plan filename", exact=True).wait_for()
            _open_plan(page, "duplicate-ready.md")
            _open_run_for_selected_plan(page)
            expect(page.get_by_label("Run plan", exact=True)).to_have_value(
                "plans/in-progress/duplicate-ready.md"
            )
            _choose_workflow(page)
            _acknowledge_dirty_worktree_if_needed(page)
            before_rejection = _run_ids(root)
            page.get_by_role("button", name="Start run", exact=True).click()
            expect(page.get_by_role("alert").filter(has_text="Plan validation failed")).to_contain_text(
                "Add or correct exactly one Git Tracking section"
            )
            assert _run_ids(root) == before_rejection
            assert len(units.start_calls) == 1
            expect(page.get_by_label("Run plan", exact=True)).to_have_value(
                "plans/in-progress/duplicate-ready.md"
            )
            expect(page.get_by_label("Run workflow", exact=True)).to_have_value("Managed")
            assert page.get_by_role("button", name="View failed request", exact=True).count() == 0

            page.get_by_role("button", name="Plans", exact=True).click()
            page.get_by_label("New plan filename", exact=True).wait_for()
            _open_plan(page, "duplicate-ready.md")
            editor = page.get_by_label("Plan content", exact=True)
            editor.fill(corrected)
            save = page.get_by_role("button", name="Save", exact=True)
            with page.expect_response(
                lambda response: response.request.method == "PUT"
                and "/plans/in_progress/duplicate-ready.md" in response.url
            ) as save_response:
                save.click()
            assert save_response.value.status == 200
            request_body = json.loads(save_response.value.request.post_data or "{}")
            assert request_body["content"] == corrected
            assert request_body["expected_revision"]
            assert duplicate_path.read_text(encoding="utf-8") == corrected
            assert editor.input_value() == corrected

            _open_run_for_selected_plan(page)
            _choose_workflow(page)
            _acknowledge_dirty_worktree_if_needed(page)
            page.get_by_role("button", name="Start run", exact=True).click()
            assert page.locator(".run-detail h3").inner_text() == "Duplicate ready"
            assert len(units.start_calls) == 2
            assert len(_run_ids(root)) == len(before_rejection) + 1
            assert duplicate_path.read_text(encoding="utf-8") == corrected
        finally:
            browser.close()

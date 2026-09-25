"""Chromium coverage for truthful Ready state and startup correction."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from playwright.sync_api import expect, sync_playwright
import pytest

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
    page.get_by_role("menuitem", name="Configure run…", exact=True).click()
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


def _start_after_review(page) -> None:
    review = page.get_by_role("button", name="Review start…", exact=True)
    expect(review).to_be_enabled()
    review.click()
    page.get_by_role("region", name="Review start", exact=True).wait_for()
    start = page.get_by_role("button", name="Start run", exact=True)
    expect(start).to_be_enabled()
    start.click()


def _capture_startup_gate_failure(page) -> None:
    """Print bounded browser state without changing the original failure."""
    try:
        evidence = page.evaluate(
            """() => {
                const maxTextLength = 768
                const maxTotalTextLength = 4096
                let remainingTextLength = maxTotalTextLength

                function boundedText(value) {
                    if (remainingTextLength <= 0 || value == null) return null
                    const normalized = String(value).replace(/\\s+/g, ' ').trim()
                    if (!normalized) return null
                    const text = normalized.slice(0, Math.min(maxTextLength, remainingTextLength))
                    remainingTextLength -= text.length
                    return text
                }

                function isVisible(element) {
                    if (!element || element.getAttribute('aria-hidden') === 'true') return false
                    const style = window.getComputedStyle(element)
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && element.getClientRects().length > 0
                }

                function valueForLabel(label) {
                    const control = document.querySelector(`[aria-label="${label}"]`)
                    return control instanceof HTMLInputElement || control instanceof HTMLSelectElement
                        ? boundedText(control.value)
                        : null
                }

                function visibleTexts(selector) {
                    return Array.from(document.querySelectorAll(selector))
                        .filter(isVisible)
                        .slice(0, 4)
                        .map((element) => boundedText(element.textContent))
                        .filter((text) => text !== null)
                }

                const preflight = document.querySelector('.worktree-preflight')
                const confirmation = document.querySelector('.worktree-confirmation input[type="checkbox"]')
                const startButtons = Array.from(document.querySelectorAll('button.btn.btn-primary'))
                    .filter((button) => button.textContent?.trim() === 'Start run')
                    .slice(0, 4)

                return {
                    selected: {
                        plan: valueForLabel('Run plan'),
                        workflow: valueForLabel('Run workflow'),
                    },
                    preflight: {
                        exists: preflight !== null,
                        status: preflight?.getAttribute('data-preflight-status') ?? null,
                        text: boundedText(preflight?.textContent),
                    },
                    dirty_confirmation: {
                        exists: confirmation !== null,
                        checked: confirmation instanceof HTMLInputElement ? confirmation.checked : null,
                    },
                    start_button: {
                        exists: startButtons.length > 0,
                        disabled: startButtons.map((button) => button.disabled),
                    },
                    visible_messages: {
                        alerts: visibleTexts('.error-message[role="alert"], .notice[role="alert"]'),
                        launch_validation: visibleTexts('.start-run-form .notice[role="note"]'),
                        confirmations: visibleTexts('.confirmation'),
                    },
                }
            }"""
        )
        serialized = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        print(f"STARTUP_GATE_FAILURE_EVIDENCE {serialized}")
    except Exception as diagnostic_error:
        print(f"STARTUP_GATE_FAILURE_EVIDENCE capture_failed={type(diagnostic_error).__name__}")


def _acknowledge_required_dirty_worktree(page) -> None:
    try:
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
        confirmation.wait_for(state="visible")
        expect(confirmation).to_be_enabled()
        confirmation.check()
        expect(confirmation).to_be_checked()
        expect(page.get_by_role("button", name="Review start…", exact=True)).to_be_enabled()
    except Exception:
        _capture_startup_gate_failure(page)
        raise


def _assert_tracked_modification(root: Path, path: Path) -> None:
    relative_path = path.relative_to(root).as_posix()
    assert _git(root, "ls-files", "--error-unmatch", "--", relative_path) == relative_path
    assert _git(root, "diff", "--name-only", "HEAD", "--", relative_path) == relative_path


def test_failed_plan_review_and_queue_evidence_in_browser(
    _control_client_fixture, monkeypatch, tmp_path,  # noqa: F811
) -> None:
    _, root, _, _ = _control_client_fixture
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(dist))
    failed = {
        "project_id": PROJECT_ID, "name": "failed-cycle.md",
        "path": "plans/failed/failed-cycle.md", "status": "failed",
        "revision": "f" * 64, "size_bytes": 18,
        "lifecycle": {"reason_code": "execution_failed", "source_run_id": "source-run"},
    }
    needs = {
        "project_id": PROJECT_ID, "name": "needs-fix.md",
        "path": "plans/needs-plan-change/needs-fix.md", "status": "needs_plan_change",
        "revision": "e" * 64, "size_bytes": 12,
        "lifecycle": {"reason_code": "invalid_plan"},
    }
    ready = {**failed, "path": "plans/in-progress/failed-cycle.md", "status": "in_progress"}
    state = {"requeued": False, "requests": []}

    def fulfill(route, payload):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def plan_route(route):
        path = route.request.url.split("?", 1)[0]
        if path.endswith("/plans"):
            fulfill(route, [ready if state["requeued"] else failed, needs])
        elif path.endswith("/failed/failed-cycle.md/requeue"):
            state["requests"].append(route.request.post_data_json)
            state["requeued"] = True
            fulfill(route, {"plan": {**ready, "content": "# Corrected plan\n"}})
        elif path.endswith("/failed/failed-cycle.md"):
            fulfill(route, {**failed, "content": "# Corrected plan\n"})
        else:
            route.continue_()

    def queue_route(route):
        current = ready if state["requeued"] else failed
        fulfill(route, {
            "project_id": PROJECT_ID,
            "settings": {"auto_consume_plans": True, "max_concurrent_implementations": 2,
                         "revision": "q" * 64, "persisted": False, "source": "defaults"},
            "capacity": {"limit": 2, "available_slots": 1, "reserved_count": 1,
                         "starting_count": 0, "active_count": 0, "uncertain_count": 0},
            "plans": [{"name": current["name"], "path": current["path"],
                       "status": current["status"], "identity": "plan-identity",
                       "outcome": "queued" if state["requeued"] else "held",
                       "reason": None if state["requeued"] else "execution_failed",
                       "dependency": None, "run_id": "source-run", "revision": current["revision"]}],
        })

    with live_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            page.route(f"**/api/projects/{PROJECT_ID}/plans**", plan_route)
            page.route(f"**/api/projects/{PROJECT_ID}/queue", queue_route)
            page.goto(f"{url}/?project={PROJECT_ID}&view=plans")
            expect(page.get_by_role("heading", name="Failed", exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name="Needs plan change", exact=True)).to_be_visible()
            expect(page.get_by_text("Implementation slots: 1 of 2 available", exact=True)).to_be_visible()
            for theme in ("light", "dark"):
                page.evaluate("""theme => {
                    localStorage.setItem('aflow.appearance', theme)
                    window.dispatchEvent(new StorageEvent('storage', {key: 'aflow.appearance', newValue: theme}))
                }""", theme)
                for width, height in ((1280, 720), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    image = tmp_path / f"cp7-plans-{theme}-{width}x{height}.png"
                    page.screenshot(path=str(image), full_page=True)
                    print("CP7_PLANS_SCREENSHOT", image)
            page.set_viewport_size({"width": 390, "height": 844})
            _open_plan(page, "failed-cycle.md")
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Review requeue…", exact=True).click()
            expect(page.get_by_role("alertdialog", name="Review plan requeue")).to_be_visible()
            assert state["requests"] == []
            page.get_by_role("button", name="Cancel", exact=True).click()
            editor = page.get_by_label("Plan content", exact=True)
            editor.fill("# Unsaved correction\n")
            page.get_by_role("button", name="← Back to Plans", exact=True).click()
            expect(page.get_by_role("alertdialog", name="Unsaved plan edits")).to_be_visible()
            page.get_by_role("button", name="Keep editing", exact=True).click()
            expect(editor).to_have_value("# Unsaved correction\n")
            editor.fill("# Corrected plan\n")
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Review requeue…", exact=True).click()
            page.get_by_role("button", name="Confirm requeue and resume", exact=True).click()
            expect(page.get_by_text("The corrected plan was requeued to Ready.", exact=True)).to_be_visible()
            assert state["requests"] == [{"expected_revision": failed["revision"], "source_run_id": "source-run"}]
        finally:
            browser.close()


@pytest.mark.parametrize("ignore_plan_files", [False, True], ids=("normal", "ignored-plans"))
def test_chromium_preserves_ready_state_and_retries_a_corrected_plan(
    _control_client_fixture,  # noqa: F811
    monkeypatch,
    ignore_plan_files: bool,
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
    sentinel_path = root / "startup-dirty-sentinel.txt"
    sentinel_path.write_text("fixture baseline\n", encoding="utf-8")
    _git(root, "add", "-f", "--", sentinel_path.name)
    _commit_fixture_repository(root)
    branch = _git(root, "symbolic-ref", "--short", "HEAD")
    base_head = _git(root, "rev-parse", "HEAD")
    sentinel_path.write_text("fixture baseline\nfixture dirty\n", encoding="utf-8")
    _assert_tracked_modification(root, sentinel_path)

    if ignore_plan_files:
        exclude_path = root / ".git" / "info" / "exclude"
        existing_exclude = exclude_path.read_bytes()
        separator = b"" if not existing_exclude or existing_exclude.endswith(b"\n") else b"\n"
        exclude_path.write_bytes(existing_exclude + separator + b"/plans/\n")

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
            expect(page.locator(".plan-header-context .status-pill").first).to_have_text("Ready")
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
            _assert_tracked_modification(root, sentinel_path)
            _acknowledge_required_dirty_worktree(page)
            _start_after_review(page)
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
            _assert_tracked_modification(root, sentinel_path)
            _acknowledge_required_dirty_worktree(page)
            before_rejection = _run_ids(root)
            _start_after_review(page)
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
            _assert_tracked_modification(root, sentinel_path)
            _acknowledge_required_dirty_worktree(page)
            _start_after_review(page)
            assert page.locator(".run-detail h3").inner_text() == "Duplicate ready"
            assert len(units.start_calls) == 2
            assert len(_run_ids(root)) == len(before_rejection) + 1
            assert duplicate_path.read_text(encoding="utf-8") == corrected
        finally:
            browser.close()

"""Browser journey for turning a failed run into an explicit follow-up launch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from playwright.sync_api import expect, sync_playwright

from test_control_plane_api import (
    PROJECT_ID,
    _commit_fixture_repository,
    control_client,  # noqa: F401
    live_server,
)
from test_responsive_browser import _browser, _login


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _artifact_path(tmp_path: Path, name: str) -> Path:
    root = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def test_failed_run_creates_edits_promotes_and_explicitly_launches_followup(
    control_client,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, root, units, _ = control_client
    source_run_id = "followup-browser-failed"
    source_plan = root / "plans" / "in-progress" / "source.md"
    source_plan.parent.mkdir(parents=True, exist_ok=True)
    source_plan.write_text(
        "# Source plan\n\n### [ ] Checkpoint 1: Existing source\n- [ ] source step\n",
        encoding="utf-8",
    )
    source_dir = root / ".aflow" / "runs" / source_run_id
    source_dir.mkdir(parents=True, exist_ok=True)
    source_run = {
        "status": "failed",
        "plan_path": "plans/in-progress/source.md",
        "workflow_name": "managed",
        "current_step_name": "implement",
        "turns_completed": 2,
        "max_turns": 5,
        "reason": "worker stopped before the checkpoint boundary",
        "launch_phase": "failed",
        "worker_exit": {
            "stage": "worker",
            "reason": "worker stopped before the checkpoint boundary",
            "exit_code": 17,
            "exited_at": "2026-09-11T00:00:00Z",
        },
    }
    source_json = source_dir / "run.json"
    source_json.write_text(json.dumps(source_run, sort_keys=True) + "\n", encoding="utf-8")
    _commit_fixture_repository(root)
    source_bytes = source_json.read_bytes()
    branch = _git(root, "symbolic-ref", "--short", "HEAD")
    base_head = _git(root, "rev-parse", "HEAD")
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            _login(page, url)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={source_run_id}")

            create = page.get_by_role("button", name="Create follow-up draft", exact=True)
            create.wait_for()
            filename = page.get_by_label("Follow-up draft filename", exact=True)
            expect(filename).to_have_value(f"followup-{source_run_id}.md")
            expect(page.get_by_text("does not change the source run or start a workflow", exact=False)).to_be_visible()
            create.click()
            page.wait_for_function("new URL(location.href).searchParams.get('view') === 'plans'")
            editor = page.get_by_label("Plan content", exact=True)
            editor.wait_for()
            ready_path = f"plans/in-progress/followup-{source_run_id}.md"
            content = editor.input_value()
            assert "## Source Evidence" in content
            assert f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_run_id}" in content
            assert f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_run_id}/context?level=lite" in content
            assert f"/api/control-plane/projects/{PROJECT_ID}/runs/{source_run_id}/events" in content
            assert "Cause: `Unknown`" in content
            assert len(units.start_calls) == 0

            edited = content.replace(
                "Owner: describe the observable conditions that establish completion; the source run is evidence, not approval.",
                "Completion criteria: source run bytes remain unchanged and the owner explicitly starts the promoted plan.",
                1,
            ).replace(
                "- Plan Branch: ``",
                f"- Plan Branch: `{branch}`",
                1,
            ).replace(
                "- Pre-Handoff Base HEAD: ``",
                f"- Pre-Handoff Base HEAD: `{base_head}`",
                1,
            )
            assert "Completion criteria: source run bytes remain unchanged" in edited
            editor.fill(edited)
            with page.expect_response(
                lambda response: response.request.method == "PUT" and "/plans/todo/" in response.url
            ) as save_response:
                page.get_by_role("button", name="Save", exact=True).click()
            assert save_response.value.status == 200
            assert len(units.start_calls) == 0

            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Move to Ready", exact=True).click()
            page.get_by_role("button", name="More", exact=True).click()
            page.get_by_role("menuitem", name="Run this plan", exact=True).wait_for()
            assert len(units.start_calls) == 0
            page.screenshot(path=str(_artifact_path(tmp_path, "followup-plan-edited.png")), full_page=True)

            page.get_by_role("menuitem", name="Run this plan", exact=True).click()
            run_plan = page.get_by_label("Run plan", exact=True)
            expect(run_plan).to_have_value(ready_path)
            preflight = page.locator(".worktree-preflight")
            preflight.wait_for()
            page.wait_for_function(
                """() => document.querySelector('.worktree-preflight')?.dataset.preflightStatus === 'ready'"""
            )
            confirmation = page.get_by_role(
                "checkbox",
                name="Continue despite uncommitted changes",
                exact=True,
            )
            if confirmation.count():
                confirmation.check()
            start = page.get_by_role("button", name="Start run", exact=True)
            expect(start).to_be_enabled()
            assert len(units.start_calls) == 0
            start.click()
            page.locator(".run-detail").wait_for()
            assert len(units.start_calls) == 1
            assert source_json.read_bytes() == source_bytes
            page.screenshot(path=str(_artifact_path(tmp_path, "followup-launched.png")), full_page=True)
        finally:
            browser.close()

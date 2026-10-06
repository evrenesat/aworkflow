"""Disposable, real-browser journeys for pre-execution startup context."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time

from playwright.sync_api import expect, sync_playwright

from aflow.control_plane import LaunchManifest, create_launch_manifest
from aflow.api.models import StartupQuestion, StartupQuestionKind
from test_control_plane_api import (
    PROJECT_ID,
    _commit_fixture_repository,
    control_client as control_client,
    live_server,
)
from test_responsive_browser import (
    _assert_document_moves,
    _assert_header_and_flow,
    _browser,
    _double_visible_text,
    _login,
    _set_theme_preference,
)


VIEWPORTS = ((320, 568), (390, 844), (768, 1024), (844, 390),
             (1280, 720), (1440, 900), (390, 420))
PLAN_PATH = "plans/in-progress/test-plan.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *args), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _partial_plan(root: Path, *, title: str = "Build the next feature") -> Path:
    plan = root / PLAN_PATH
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "# Partial browser plan\n\n"
        + "".join(f"### [x] Checkpoint {number}: Done\n- [x] task\n\n" for number in range(1, 4))
        + f"### [ ] Checkpoint 4: {title}\n- [x] first task\n- [ ] keep prior work safe\n\n"
        + "### [ ] Checkpoint 5: Follow up\n- [ ] later task\n\n"
        + "### [ ] Checkpoint 6: Verify\n- [ ] owner check\n",
        encoding="utf-8",
    )
    return plan


def _open_new_run(page, url: str) -> None:
    page.goto(f"{url}/?project={PROJECT_ID}&view=runs")
    page.get_by_role("button", name="New run", exact=True).click()
    plan = page.get_by_label("Run plan", exact=True)
    plan.click()
    plan.fill("test-plan")
    option = page.get_by_role("option", name="test-plan.md", exact=False)
    try:
        option.wait_for(timeout=5000)
    except Exception:
        print("STARTUP_PLAN_PICKER", page.locator("body").inner_text()[:3500])
        raise
    option.click()
    expect(plan).to_have_value(PLAN_PATH)
    workflow = page.get_by_label("Run workflow", exact=True)
    workflow.click()
    workflow.fill("managed")
    workflow.press("ArrowDown")
    workflow.press("Enter")
    expect(workflow).to_have_value("Managed")
    expect(page.locator(".worktree-preflight")).to_have_attribute("data-preflight-status", "ready")


def _artifact_dir(tmp_path: Path) -> Path:
    path = Path(os.environ.get("AFLOW_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed_previous(root: Path, *, run_id: str = "previous-run") -> tuple[Path, Path]:
    plan = root / PLAN_PATH
    worktree = root.parent / f"{run_id}-worktree"
    _git(root, "worktree", "add", "-b", f"feature/{run_id}", str(worktree), "main")
    (worktree / "implementation.txt").write_text("committed prior work\n", encoding="utf-8")
    _git(worktree, "add", "implementation.txt")
    _git(worktree, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "preserve prior implementation")
    dirty = worktree / "unfinished.txt"
    dirty.write_text("uncommitted prior work\n", encoding="utf-8")
    create_launch_manifest(root, LaunchManifest(
        run_id=run_id, project_root=str(root), plan_path=str(plan.resolve()),
        workflow_name="managed", max_turns=5,
    ))
    run_dir = root / ".aflow" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "schema_version": 1, "status": "failed", "repo_root": str(root),
        "original_plan_path": str(plan.resolve()), "current_step_name": "implement",
        "feature_branch": f"feature/{run_id}", "main_branch": "main",
        "worktree_path": str(worktree), "execution_repo_root": str(worktree),
        "failure_reason": "Native macOS acceptance was previously unavailable.",
    }), encoding="utf-8")
    return worktree, dirty


def _worktree_contents(worktree: Path) -> tuple[str, str, str]:
    return (
        _git(worktree, "rev-parse", "HEAD"),
        _git(worktree, "status", "--short"),
        (worktree / "unfinished.txt").read_text(encoding="utf-8"),
    )


def _configure_new_worktree(root: Path) -> None:
    config = root.parent / "global" / "aflow.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'default_workflow = "managed"',
            f'default_workflow = "managed"\nworktree_root = "{root.parent / "new-worktrees"}"\nteam_lead = "worker"',
        ), encoding="utf-8",
    )
    workflow = root.parent / "global" / "workflows.toml"
    workflow.write_text(
        '[workflow.managed]\nsetup = ["worktree", "branch"]\n'
        'teardown = ["merge", "rm_worktree"]\nmain_branch = "main"\n'
        '[workflow.managed.steps.implement]\nrole = "worker"\n'
        'prompts = ["p"]\ngo = [{ to = "END", when = "DONE" }]\n',
        encoding="utf-8",
    )


def _reserve_legacy_step(client, monkeypatch) -> str:
    monkeypatch.setattr(
        "aflow.daemon.prepare_startup",
        lambda _request: StartupQuestion(
            kind=StartupQuestionKind.PICK_STEP,
            message="Choose a step", choices=["implement"],
        ),
    )
    response = client.post(
        f"/api/control-plane/projects/{PROJECT_ID}/runs",
        headers={"Idempotency-Key": "legacy-browser-reservation"},
        json={"plan_path": PLAN_PATH, "workflow_name": "managed"},
    )
    assert response.status_code == 202, response.text
    return response.json()["startup_question"]["run_id"]


def test_partial_plan_starts_once_at_workflow_default_without_step_choice(
    control_client, monkeypatch,
) -> None:
    _, root, units, _ = control_client
    _partial_plan(root)
    _commit_fixture_repository(root)
    workflow_path = root.parent / "global" / "workflows.toml"
    workflow_path.write_text(
        "[workflow.managed.steps.implement]\nrole = \"worker\"\n"
        "prompts = [\"p\"]\ngo = [{ to = \"review\", when = \"DONE\" }]\n"
        "[workflow.managed.steps.review]\nrole = \"worker\"\n"
        "prompts = [\"p\"]\ngo = [{ to = \"END\", when = \"DONE\" }]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            _open_new_run(page, url)
            panel = page.get_by_role("region", name="Startup context preparation")
            expect(panel).to_contain_text("Next in plan: Checkpoint 4 of 6")
            expect(panel).to_contain_text("3 checkpoints marked complete")
            expect(panel).to_contain_text("keep prior work safe")
            expect(panel).to_contain_text("Configured starting step: Implement")
            assert page.get_by_role("region", name="Startup question").count() == 0
            page.get_by_role("button", name="Review start…", exact=True).click()
            expect(page.get_by_role("region", name="Startup context review")).to_contain_text("Checkpoint 4 of 6")
            page.get_by_role("button", name="Start run", exact=True).click()
            expect(page.locator(".run-detail")).to_be_visible()
            assert len(units.start_calls) == 1
            manifests = [path for path in (root / ".aflow" / "launches").glob("*.json")
                         if not path.name.endswith(".state.json")]
            assert len(manifests) == 1
            assert json.loads(manifests[0].read_text(encoding="utf-8"))["start_step"] is None
            assert page.get_by_role("region", name="Startup question").count() == 0
            page.reload()
            assert len(units.start_calls) == 1
        finally:
            browser.close()


def test_inconsistent_plan_reaches_recovery_question_and_confirms_without_rewrite(
    control_client, monkeypatch,
) -> None:
    _, root, units, _ = control_client
    plan = root / PLAN_PATH
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "# Inconsistent browser plan\n\n"
        "### [x] Checkpoint 1: Done\n- [x] task\n\n"
        "### [x] Checkpoint 2: Broken\n- [ ] leftover task\n",
        encoding="utf-8",
    )
    _commit_fixture_repository(root)
    workflow_path = root.parent / "global" / "workflows.toml"
    workflow_path.write_text(
        "[workflow.managed.steps.implement]\nrole = \"worker\"\n"
        "prompts = [\"p\"]\ngo = [{ to = \"END\", when = \"DONE\" }]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    original = plan.read_bytes()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            _open_new_run(page, url)
            panel = page.get_by_role("region", name="Startup context preparation")
            # Recorded tolerant facts stay visible next to the explicit warning.
            expect(panel).to_contain_text("Next in plan: Checkpoint 2 of 2")
            expect(panel).to_contain_text("leftover task")
            expect(panel.get_by_role("alert")).to_contain_text(
                "A completed checkpoint has unchecked tasks. Correct the plan before starting."
            )
            expect(panel).to_contain_text("Explicit recovery confirmation is required before starting.")
            # The reviewed start path stays reachable with no prior work.
            expect(page.get_by_role("button", name="Review start\u2026", exact=True)).to_be_enabled()
            page.get_by_role("button", name="Review start\u2026", exact=True).click()
            review = page.get_by_role("region", name="Startup context review")
            expect(review).to_contain_text("Checkpoint 2 of 2")
            expect(review.get_by_role("alert")).to_contain_text(
                "A completed checkpoint has unchecked tasks"
            )
            page.get_by_role("button", name="Start run", exact=True).click()
            question = page.get_by_role("region", name="Startup question")
            expect(question).to_contain_text("Plan checkpoint state is inconsistent")
            expect(question).to_contain_text("No agent started")
            assert units.start_calls == []
            question.get_by_role("button", name="Confirm and continue").click()
            expect(page.locator(".run-detail")).to_be_visible()
            deadline = time.monotonic() + 20
            while not units.start_calls and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert len(units.start_calls) == 1
            # Confirmation prepares the run without rewriting the plan source.
            assert plan.read_bytes() == original
        finally:
            browser.close()


def test_inconsistent_plan_recovery_decline_stops_without_starting(
    control_client, monkeypatch,
) -> None:
    client, root, units, _ = control_client
    plan = root / PLAN_PATH
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "# Inconsistent browser plan\n\n"
        "### [x] Checkpoint 1: Done\n- [x] task\n\n"
        "### [x] Checkpoint 2: Broken\n- [ ] leftover task\n",
        encoding="utf-8",
    )
    _commit_fixture_repository(root)
    workflow_path = root.parent / "global" / "workflows.toml"
    workflow_path.write_text(
        "[workflow.managed.steps.implement]\nrole = \"worker\"\n"
        "prompts = [\"p\"]\ngo = [{ to = \"END\", when = \"DONE\" }]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    original = plan.read_bytes()

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            _open_new_run(page, url)
            panel = page.get_by_role("region", name="Startup context preparation")
            expect(panel.get_by_role("alert")).to_contain_text(
                "A completed checkpoint has unchecked tasks. Correct the plan before starting."
            )
            page.get_by_role("button", name="Review start\u2026", exact=True).click()
            page.get_by_role("button", name="Start run", exact=True).click()
            question = page.get_by_role("region", name="Startup question")
            expect(question).to_contain_text("Plan checkpoint state is inconsistent")
            assert units.start_calls == []

            # Decline stops: no worker starts and the plan source is untouched.
            question.get_by_role("button", name="Decline and stop").click()
            expect(page.get_by_role("alert").filter(has_text="Startup recovery declined")).to_be_visible()
            assert units.start_calls == []
            assert plan.read_bytes() == original

            # The run records the declined recovery as needs_attention.
            rows = client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs").json()["runs"]
            declined = [row for row in rows if row["status"] == "needs_attention"]
            assert len(declined) == 1
            detail = client.get(
                f"/api/control-plane/projects/{PROJECT_ID}/runs/{declined[0]['run_id']}"
            ).json()
            failure = (detail.get("evidence") or {}).get("startup_failure") or {}
            assert failure.get("message") == "Startup recovery declined"
            assert units.start_calls == []
        finally:
            browser.close()


def test_previous_dirty_work_blocks_start_and_cancel_opens_exact_run(
    control_client, monkeypatch,
) -> None:
    client, root, units, _ = control_client
    _git(root, "checkout", "-b", "main")
    plan = _partial_plan(root)
    _commit_fixture_repository(root)
    base = _git(root, "rev-parse", "HEAD")
    plan.write_text(plan.read_text(encoding="utf-8").replace(
        "# Partial browser plan\n\n",
        "# Partial browser plan\n\n## Git Tracking\n\n"
        "- Plan Branch: `feature/previous-run`\n"
        f"- Pre-Handoff Base HEAD: `{base}`\n\n",
    ), encoding="utf-8")
    _git(root, "add", "-f", PLAN_PATH)
    _commit_fixture_repository(root)
    _configure_new_worktree(root)
    assert client.get(f"/api/control-plane/projects/{PROJECT_ID}/capabilities").status_code == 200
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            _open_new_run(page, url)
            worktree, dirty = _seed_previous(root)
            before = _worktree_contents(worktree)
            assert dirty.is_file()
            page.get_by_role("button", name="Refresh worktree inspection").click()
            panel = page.get_by_role("region", name="Startup context preparation")
            expect(panel).to_contain_text("Previous work needs recovery")
            expect(panel).to_contain_text("previous-run")
            expect(panel).to_contain_text("Work is not merged")
            expect(panel).to_contain_text("Uncommitted implementation work is preserved")
            expect(panel).to_contain_text("Previously reported: Native macOS acceptance")
            expect(page.get_by_role("button", name="Review start…", exact=True)).to_be_disabled()
            assert units.start_calls == []
            assert _worktree_contents(worktree) == before

            pending_id = _reserve_legacy_step(client, monkeypatch)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={pending_id}")
            pending = page.get_by_role("region", name="Startup context pending")
            expect(pending).to_contain_text("Previous work needs recovery")
            expect(pending).to_contain_text("No agent started")
            expect(pending).to_contain_text("Checkpoint 4 of 6")
            expect(pending).to_contain_text("previous-run")
            assert _worktree_contents(worktree) == before
            page.get_by_role("button", name="Cancel pending start and open previous run…").click()
            review = page.get_by_role("region", name="Review pending start cancellation")
            expect(review).to_contain_text("does not resume the previous run")
            assert _worktree_contents(worktree) == before
            held_stop = []
            resume_requests = []
            start_requests = []
            stop_pattern = f"**/api/control-plane/projects/{PROJECT_ID}/runs/{pending_id}/owner-stop"
            page.route(stop_pattern, lambda route: held_stop.append(route))
            page.route("**/api/control-plane/projects/*/runs/*/resume", lambda route: resume_requests.append(route.request))
            page.route(f"**/api/control-plane/projects/{PROJECT_ID}/runs",
                       lambda route: (start_requests.append(route.request), route.continue_())
                       if route.request.method == "POST" else route.continue_())
            review.get_by_role("button", name="Confirm cancel pending start").click()
            deadline = time.monotonic() + 10
            while not held_stop and time.monotonic() < deadline:
                page.wait_for_timeout(25)
            assert len(held_stop) == 1
            assert held_stop[0].request.post_data_json["expected_revision"] >= 0
            assert _worktree_contents(worktree) == before
            assert units.start_calls == []
            with page.expect_response(re.compile(rf"/runs/{pending_id}(?:\?|$)")):
                page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            expect(review).to_be_visible()
            assert len(held_stop) == 1
            assert _worktree_contents(worktree) == before
            held_stop[0].fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "simulated failure"}))
            expect(page.get_by_role("alert").filter(has_text="Could not cancel the pending start")).to_be_visible()
            assert f"run={pending_id}" in page.url
            assert _worktree_contents(worktree) == before
            page.unroute(stop_pattern)
            review.get_by_role("button", name="Confirm cancel pending start").click()
            expect(page).to_have_url(re.compile(r"[?&]run=previous-run(?:&|$)"))
            assert _worktree_contents(worktree) == before
            assert units.start_calls == []
            assert start_requests == []
            assert resume_requests == []
            assert client.get(f"/api/control-plane/projects/{PROJECT_ID}/runs/{pending_id}").json()["status"] == "owner_stopped"
        finally:
            browser.close()


def test_pending_reads_show_changed_missing_invalid_and_old_server_context(
    control_client, monkeypatch,
) -> None:
    client, root, units, _ = control_client
    _git(root, "checkout", "-b", "main")
    plan = _partial_plan(root)
    _commit_fixture_repository(root)
    _configure_new_worktree(root)
    pending_id = _reserve_legacy_step(client, monkeypatch)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    original = plan.read_bytes()
    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            mutation_requests = []

            def observe_mutation(route):
                if route.request.method == "POST":
                    mutation_requests.append(route.request.url)
                route.continue_()

            page.route("**/api/control-plane/projects/*/runs**", observe_mutation)
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={pending_id}")
            panel = page.get_by_role("region", name="Startup context pending")
            expect(panel).to_contain_text("Checkpoint 4 of 6")
            expect(page.get_by_role("button", name="Continue from checkpoint 4")).to_be_visible()

            plan.write_text(original.decode().replace("Build the next feature", "Revised checkpoint work"), encoding="utf-8")
            page.reload()
            expect(panel).to_contain_text("Revised checkpoint work")
            assert units.start_calls == []

            plan.write_bytes(b"\xff\xfeinvalid")
            page.reload()
            expect(panel).to_contain_text("not valid UTF-8")
            assert "Checkpoint 4 of 6" not in panel.inner_text()
            assert units.start_calls == []

            plan.unlink()
            page.reload()
            expect(panel).to_contain_text("plan file", ignore_case=True)
            assert "Checkpoint 4 of 6" not in panel.inner_text()
            assert units.start_calls == []

            plan.write_bytes(original)
            status_pattern = f"**/api/control-plane/projects/{PROJECT_ID}/runs/{pending_id}*"

            def old_server(route):
                response = route.fetch()
                payload = response.json()
                if route.request.url.split("?", 1)[0].endswith("/context"):
                    payload.get("data", {}).pop("startup_context", None)
                else:
                    payload.pop("startup_context", None)
                route.fulfill(status=response.status, content_type="application/json", body=json.dumps(payload))

            page.route(status_pattern, old_server)
            page.reload()
            expect(page.get_by_text("Plan startup context is unavailable from this server.", exact=False)).to_be_visible()
            assert units.start_calls == []
            page.unroute(status_pattern, old_server)
            page.reload()
            expect(panel).to_contain_text("Checkpoint 4 of 6")
            first_worktree, _ = _seed_previous(root)
            second_worktree, _ = _seed_previous(root, run_id="other-run")
            first_before = _worktree_contents(first_worktree)
            second_before = _worktree_contents(second_worktree)
            page.reload()
            expect(panel).to_contain_text("Inspect earlier runs")
            expect(panel).to_contain_text("previous-run")
            expect(panel).to_contain_text("other-run")
            assert _worktree_contents(first_worktree) == first_before
            assert _worktree_contents(second_worktree) == second_before
            panel.get_by_role("button", name="Inspect run").first.click()
            expect(page).to_have_url(re.compile(r"[?&]run=(?:previous-run|other-run)(?:&|$)"))
            assert _worktree_contents(first_worktree) == first_before
            assert _worktree_contents(second_worktree) == second_before
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={pending_id}")
            expect(panel).to_contain_text("Inspect earlier runs")
            assert _worktree_contents(first_worktree) == first_before
            assert _worktree_contents(second_worktree) == second_before
            assert mutation_requests == []
        finally:
            plan.write_bytes(original)
            browser.close()


def test_pending_context_visual_and_navigation_matrix(
    control_client, monkeypatch, tmp_path,
) -> None:
    client, root, units, _ = control_client
    _partial_plan(root, title="Finish the exceptionally long checkpoint title that must wrap on a narrow phone without hiding the next action")
    _commit_fixture_repository(root)
    pending_id = _reserve_legacy_step(client, monkeypatch)
    monkeypatch.setenv("AFLOW_APP_WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))
    artifacts = _artifact_dir(tmp_path)
    browser_name = os.environ.get("AFLOW_TEST_BROWSER", "chromium").strip().lower()
    captures = []

    with live_server() as url, sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _login(page, url)
            for theme in ("light", "dark"):
                _set_theme_preference(page, theme)
                for width, height in VIEWPORTS:
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={pending_id}")
                    panel = page.get_by_role("region", name="Startup context pending")
                    expect(panel).to_contain_text("Checkpoint 4 of 6")
                    expect(panel).to_contain_text("No agent started")
                    expect(panel).to_contain_text("keep prior work safe")
                    assert page.locator("html").get_attribute("data-theme") == theme
                    _assert_header_and_flow(page)
                    primary = page.get_by_role("button", name="Continue from checkpoint 4")
                    expect(primary).to_be_visible()
                    box = primary.bounding_box()
                    assert box and box["height"] >= 43, (width, height, box)
                    next_line = panel.locator(".startup-context-next").bounding_box()
                    assert next_line and next_line["x"] >= 0 and next_line["x"] + next_line["width"] <= width + 1
                    primary.focus()
                    assert primary.evaluate("element => document.activeElement === element")
                    outline = panel.locator("details").first
                    outline.locator("summary").click()
                    assert outline.get_attribute("open") is not None
                    outline.locator("summary").click()
                    assert outline.get_attribute("open") is None
                    _assert_document_moves(page)
                    page.evaluate("window.scrollTo(0, 0)")
                    image = artifacts / f"startup-context-{browser_name}-{theme}-{width}x{height}.png"
                    page.screenshot(path=str(image), full_page=True)
                    captures.append({"theme": theme, "viewport": [width, height], "screenshot": image.name,
                                     "primary_height": box["height"]})

            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={pending_id}")
            panel = page.get_by_role("region", name="Startup context pending")
            back = page.get_by_role("button", name="← Back to Run history")
            expect(back).to_be_visible()
            back.click()
            expect(panel).not_to_be_visible()
            page.goto(f"{url}/?project={PROJECT_ID}&view=runs&run={pending_id}")
            expect(panel).to_contain_text("Checkpoint 4 of 6")
            panel.evaluate("element => element.dataset.refreshProbe = 'retained'")
            focus_target = panel.locator("details").first.locator("summary")
            focus_target.focus()
            before_scroll = page.evaluate("() => document.scrollingElement.scrollTop")
            with page.expect_response(re.compile(rf"/runs/{pending_id}(?:\?|$)")):
                page.evaluate("window.dispatchEvent(new Event('aflow-history-changed'))")
            expect(panel).to_have_attribute("data-refresh-probe", "retained")
            assert focus_target.evaluate("element => document.activeElement === element")
            assert abs(page.evaluate("() => document.scrollingElement.scrollTop") - before_scroll) < 2

            page.get_by_role("button", name="Expand all", exact=True).click()
            expect(page.get_by_role("button", name="Collapse all", exact=True)).to_be_visible()
            page.get_by_role("button", name="Collapse all", exact=True).click()
            expect(page.get_by_role("button", name="Expand all", exact=True)).to_be_visible()

            _double_visible_text(page)
            _assert_document_moves(page)
            page.evaluate("window.scrollTo(0, 0)")
            enlarged = artifacts / f"startup-context-{browser_name}-dark-390x844-enlarged.png"
            page.screenshot(path=str(enlarged), full_page=True)
            captures.append({"theme": "dark", "viewport": [390, 844], "screenshot": enlarged.name,
                             "text": "enlarged"})
            assert units.start_calls == []
        finally:
            browser.close()
    (artifacts / f"startup-context-{browser_name}.json").write_text(
        json.dumps({"pending_run_id": pending_id, "captures": captures}, indent=2) + "\n",
        encoding="utf-8",
    )

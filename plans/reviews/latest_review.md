# Issue 6 — Cumulative review approved

Review base: `7dbb8eb07db5b7bf49ab8706974ee41e6d77226a` (unchanged Pre-Handoff Base HEAD).
Reviewed implementation HEAD: `dc86dd7f451a980c62482bf5cb83a2fa3df8f1fa`.
Coverage: 1 new / 2 total commits, `cp1 v01` and `cp1 v02`, including all seven changed files and the original checkpoint requirements. The active overlay was `issue-6-modern-multiproject-acceptance-20260909-cp01-v01.md`.

## Previous finding disposition

R1 (P2, high confidence), first reported against `b8a406ecd6df0b9ab6ca86ec02d9f29584856e05`, is resolved by `dc86dd7f451a980c62482bf5cb83a2fa3df8f1fa`. Successful launches immediately retain exact project/run identities. Guaranteed teardown cleans owned runs before UI shutdown; API owner-stop uses a fresh revision and invocation key. UI-unavailable cleanup uses the private installed interpreter and existing persistent-unit manager, validating exact receipt identity and nonce. Original failures and private HOME evidence survive teardown.

## Cumulative assessment

No material findings. Reviewed authoring and launch isolation, registry admission, shared current configuration and server-selected runtime identity, redacted malformed-request responses, REST/MCP histories, disposable installed-wheel restart/Chromium coverage, and successful/failed resource cleanup. Existing project-root resolution and per-project repositories remain authoritative. No production lifecycle, startup readiness, live loading, replacement/scope recovery, or stop-after-turn implementation changed. Documentation explicitly maps retired inventory/standalone/frozen-configuration requirements as superseded.

## Verification evidence

Retained evidence was inspected rather than repeating unchanged tests or installed journeys. Source: `/root/code/agent_flow/.aflow/runs/20260911t153328z-8a877c7e/turns/turn-001/transport.stdout` and `turn-003/transport.stdout`, with completed worker result records.

- Original focused REST two-project launch/redaction, MCP two-project history, MCP schema/auth parity, REST unknown/traversal rejection, and five registry rejection cases passed. Build and Ruff passed.
- `uv run pytest -q tests/test_smoke_ui.py --basetemp=/root/code/evidence/aflow-dogfood-20260909/issue6-review-20260911/cleanup-regression`: 3 passed against final worker changes. `uv run ruff check scripts/smoke_ui.py tests/test_smoke_ui.py`: passed.
- Installed journey: `TMPDIR=/root/code/evidence/aflow-dogfood-20260909/issue6-review-20260911 uv run python scripts/smoke_ui.py --wheel dist/aworkflow-0.1.12-py3-none-any.whl --browser --playwright-python apps/aflow_app/server/.venv/bin/python`: exit 0; REST/MCP history, held worker survival across UI restart, Chromium login/reload/two-project-history/logout, and teardown passed.
- Retained wheel SHA-256 prefix: `d0a305249e8f32bb`. Independently compared packaged server main and persistent-unit cleanup source bytes with reviewed checkout: identical.
- Temporary evidence-root drivers `smoke-failure-driver.py` and `smoke-fallback-failure-driver.py` each exited 1 with the injected assertion preserved. Their JSON records identify exact private HOME and runs; worker process checks found no surviving owned processes. Independent review verified nonce-matched exit/stopped receipts for both runs in each probe.
- Independent cumulative `git diff --check 7dbb8eb0 HEAD`: passed. Full suites remain CI-owned; no shared tool retarget or live UI restart was performed.

## Approval and delivery boundary

Approve the full cumulative handoff and squash both implementation commits plus this tracked reviewer record into one unpublished final commit after the unchanged base. Preserve implementation blobs and the single existing handoff DEVLOG entry. Remove the resolved private fix overlay; leave the original plan in place and record the final approved SHA there, outside the commit. Finalization verifies exactly one handoff commit and clean tracked state. No ignored plans or evidence are force-added.

Publication, exact-SHA CI, and live activation remain the controller/coordinator's normal serialized delivery steps; this review does not claim them complete.

---

# Accepted main review history retained during integration

# Stop after current turn — cumulative review v04

Approved. No material findings under the material-code-review admission gate,
exclusions and proportionate-fix discipline.

Base: `05bf725040bc03fee2a42eca151f609a8d82b10b` (unchanged).
Reviewed HEAD: `86e6170b3a782f70e62f0976a0a46c570e6c22f4`.
Branch: `aflow-stop-after-current-turn-20260909-20260911-135157`.
1 new / 4 total commits: cp1 v01 `5d2d9d00`, browser cp1 v02 `7f93cb33`,
resume cp1 v02 `ecedc88c`, complete-review cp1 v03 `86e6170b`.
Reviewed original checkpoint 1 and all follow-ups cumulatively across the
20 changed files and surrounding boundary, resume, transport and UI paths.

## Prior finding disposition

F1 resolved. The browser uses a real controller and delayed provider, observes
pending intent after navigation, and verifies finalized worker evidence with
no unit stop or subsequent invocation. Canonical continuation now admits the
validated pending reviewer for both incomplete and complete worker outputs.
The complete-plan daemon fixture proves ordinary resume preview and mutation,
scope/evidence binding, reviewer-first execution and unchanged source artifacts.
Completed runs without pending review remain rejected. No additional material
findings survived the cumulative review.

## Verification

Independently passed at reviewed HEAD: 11 focused runtime/CLI/daemon tests,
3 REST/MCP tests, Ruff and cumulative whitespace validation. Exact commands:

```sh
uv run pytest -q tests/test_run_state.py::test_owner_stop_waits_for_a_blocked_turn_and_never_launches_the_next_one tests/test_run_state.py::test_owner_stop_terminalizes_at_the_existing_pre_turn_boundary tests/test_run_state.py::test_graceful_stop_resume_reviews_finalized_worker_before_new_implementation tests/test_run_state.py::test_graceful_stop_resume_reviews_finalized_complete_worker_before_new_implementation tests/test_control_plane_resume.py::test_daemon_resume_preview_and_start_reviews_complete_owner_stopped_source tests/test_cli.py::WorkflowStartupFlowTests::test_resume_complete_prior_run_suppresses_prompt tests/test_cli.py::WorkflowCliTests::test_resume_explicit_start_step_correction_replaces_interrupted_step tests/test_cli.py::WorkflowStartupFlowTests::test_resume_restores_step_from_unfinished_active_turn tests/test_cli.py::WorkflowStartupFlowTests::test_resume_recovers_completed_turn_before_manager_boundary tests/test_aflowd.py::test_daemon_owner_stop_persists_terminal_phase_and_requires_event_authorization tests/test_aflowd.py::test_daemon_restart_successor_requires_owner_stopped_inactive_source_and_keeps_lineage
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_control_plane_api.py::test_control_events_context_controls_owner_stop_and_resume apps/aflow_app/server/tests/test_mcp.py::test_mcp_stateless_http_auth_metadata_resources_and_rest_parity apps/aflow_app/server/tests/test_mcp.py::test_mcp_startup_control_and_resume_are_idempotent_and_match_rest
uv run ruff check aflow tests/test_run_state.py tests/test_control_plane_resume.py
git diff --check 05bf7250 HEAD
```

Reused retained real-controller Chromium/WebKit logs (one passing journey each),
worker v03 verification record, and prior independent browser, 101 dashboard-test
and production-build evidence. Web sources have not changed since those checks;
the worker also reran both browsers after the latest runtime change. Evidence:
`/root/code/evidence/aflow-dogfood-20260909/stop-turn-review-20260911/`.
Existing transport deprecation warning only; CI owns full suites. The supplied
turn-007 result path is absent here; code, retained proof and independent tests
provide sufficient evidence for the decision.

## Finalization

Squash all four handoff commits onto the unchanged base, including this tracked
reviewer record and one compact DEVLOG entry. Preserve all other reviewed blobs.
Prior latest review rotated byte-for-byte; superseded v03 fix plan archived
privately and removed. No new fix plan or force-added private artifacts.
Record final approved SHA in original-plan tracking after commit creation;
leave that ignored original plan in place for engine finalization. Verify one
handoff commit and clean tracked state before returning approval.

Publication, integration with scope-recovery `54f2ef28`, exact-SHA CI and live
activation remain controller/coordinator delivery steps; this approval does
not claim deployment or remove the active managed worktree.

No material findings


---

# Accepted main review history retained during integration

# Issue 4 — final cumulative review, 2026-09-11

Original plan: `plans/in-progress/issue-4-deterministic-guard-reports-20260909.md`.
Active overlay reviewed: `issue-4-deterministic-guard-reports-20260909-cp01-v02.md`.
Branch: `aflow-issue-4-deterministic-guard-reports-20260909-20260911-102356`.
Unchanged Pre-Handoff Base HEAD: `fcfb7ec4450165682d1b031e3e20809821315442`.
Reviewed implementation HEAD: `e01b9c0d58a2cf626311ebf9f467ae526db60e8c`.
Coverage: **1 new / 5 total commits**, CP1 v01 `c4994757`, CP2 v01 `e792dd4b`, CP3 v01 `4ed22753`, follow-up v01 `d7b33012`, follow-up cp01 v02 `e01b9c0d`.

Read the active overlay and prior findings first, verified their disposition, then reviewed the complete original-base-through-HEAD implementation across all 21 changed files and directly affected canonical/snapshot contracts. Applied material-code-review admission, exclusions and proportionate-fix discipline; the explicit AFlow contract governs finalization.

## Previous findings

- F1 resolved: unmodified schema-1 canonical get_run responses pass through explicit pinned CLI metadata to the renderer; wrong run/ownership are rejected.
- F2 resolved: compact results require terminal status and an aware finished_at, reject explicit identity conflicts, and exclude starting worker evidence.
- F3 resolved: repeated legacy orphan reports retain duplicate-operation risk without repeating cause inspection.
- F4 resolved: failed/incomplete/needs_attention reports request bounded owner inspection and a decision.
- F5 resolved: line-limit truncation always shows an ellipsis and preserves bounded JSON.
- F6 resolved: header and checkpoint fields have disjoint drawn extents; retained layout evidence remains valid.
- F7 resolved in e01b9c0d: real project_activity startup/input/override projections request the required owner action through the existing owning interface; healthy/success controls remain sensible and unknown states no longer assert no action.
- F8 resolved in e01b9c0d: the canonical running-metadata/unit_missing condition retains unresolved duplicate-operation risk and explicit no-blind-relaunch guidance without inventing a cause; known terminal provider evidence remains distinct.

No unresolved previous findings. No new material findings.

## Verification

- `uv run --with pillow pytest -q tests/test_guard_report_input.py tests/test_guard_report_routing.py --basetemp /root/code/evidence/aflow-dogfood-20260909/issue4-review-20260911/final-review-pytest`: **29 passed in 5.26s**. Includes real canonical projections, canonical/legacy CLI-to-PNG journeys, repeat PNG/JSON bytes, identity rejection, and repository nonmutation.
- `uv run ruff check aflow`, `git diff --check fcfb7ec4 HEAD`, and working diff check passed.
- Reused valid prior 40-test renderer/snapshot/ownership/install/refresh evidence and CP3 edited-tree preservation checks recorded in the previous review and `docs/reviews/issue-4-deterministic-guard-reports-cp3.md`. The final fix does not change renderer, snapshot, installer or refresh code. CI owns full suites.
- Visually inspected retained fix-v02 startup-answer and missing-unit PNGs: readable owner action, visible duplicate risk, correct unknown cause, no overlap. Current renderer bytes independently match retained startup-answer, missing-unit, long-field and failed PNGs; each retained repeated PNG/JSON pair matches exactly.
- Retained current PNG SHA-256: startup-answer `ad3edf7e6e54aad586be46f28a0a4f8f9aaed85edd498cbe26d043aa9f066815`; missing-unit `241b44f13f5c9b12471db36fd3f852f5813111c974a0365131002b4b9dc8ecce`.
- Private evidence remains external under the supplied issue4 evidence root. Portable tests use tmp_path; no private test paths were committed.
- Prompt-supplied worktree-relative turn-004/result.json was absent. Review used committed source/history, retained evidence and independent checks without altering run lineage or durable state.

## Approval and delivery boundary

Approved for one final accumulated handoff commit after the unchanged base, covering all five implementation/fix commits. Finalization includes this already-tracked reviewer record and one compact DEVLOG entry; the prior review is archived byte-for-byte without force-adding private artifacts. The superseded fix overlay is removed. No empty fix plan is created.

The original plan stays in place for engine finalization and records the final approved SHA after commit creation. Final verification must establish exactly one commit after the base, unchanged implementation blobs, and no remaining tracked reviewer edits. Publication to origin/main, exact-SHA CI and live activation belong to the existing controller/coordinator delivery gate and are not claimed by this local review. Publication configuration is origin/main. Unrelated integration work and active execution lineage are preserved.

No material findings

---

# Issue 39 — Cumulative squash approval

Original/active plan: `plans/in-progress/issue-39-manual-acceptance-handoff-20260911.md`.
Branch: `aflow-issue-39-manual-acceptance-handoff-20260911-20260911-151803`.
Unchanged Pre-Handoff Base HEAD: `db97b15b32802b59dc8badeb8a6498494ceb9e62`.
Reviewed HEAD: `59679819fe1e0edcdb58a9cf0593cf220dfd8200`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all checkpoint 1 requirements and all nine changed files. No previous findings or fix overlays belong to this handoff; the prior latest review concerns issue 40.

## Findings

No material findings under the finding admission gate, exclusions, and proportionate-fix discipline. Bundled planning guidance separates agent-owned tasks from ordinary-bullet owner acceptance under a level-two heading after checkpoints. Execution/review guidance prevents retries for pending owner-only checks while preserving implementation defects, required automated verification and explicit approval/release gates. Handoffs must report both statuses and require owner evidence before claiming acceptance. README, CLI documentation and the single DEVLOG entry agree. Parser, controller and account-local skills are unchanged.

## Verification evidence

Reused successful completed command evidence from the parent repository's `.aflow/runs/20260911t151803z-d3ccdf43/turns/turn-001/transport.stdout` and completed `result.json`; relevant implementation files did not change after these checks:

```sh
uv run pytest -q tests/test_skill_install.py::test_manual_destination_links_default_skills_into_the_store
# 1 passed, exit 0
uv run pytest -q tests/test_plan.py::PlanParserTests::test_parser_global_section_after_last_checkpoint_does_not_affect_completion tests/test_plan.py::PlanParserTests::test_parser_non_checkpoint_heading_ends_step_counting
# 2 passed, exit 0
uv run pytest -q tests/test_docs.py::SkillDocsTests::test_plan_skill_uses_semantic_checkpoint_shaping_without_hard_stop tests/test_docs.py::SkillDocsTests::test_bundled_skills_keep_plan_worker_facing_and_commit_ownership_in_execution_roles
# 2 passed, exit 0
git diff --check db97b15b32802b59dc8badeb8a6498494ceb9e62 HEAD
# independently passed
```

Inspected existing parser boundary regressions and isolated installation assertions, including packaged aflow-plan byte equality. No duplicate parser regression is needed. Worker probe is retained at `/root/code/evidence/aflow-dogfood-20260909/issue39-review-20260911/representative-parse-probe.txt`. An independent `uv run python` probe, retained as `reviewer-parser-probe.txt` in the same external root, uses an exact automated test command and physical phone Settings acceptance actions. It confirms incomplete before, complete after checking only the automated checkpoint/task, byte-identical pending manual text, and rejection of a checked checkpoint with an unchecked automated step. The probe checks parsing, not physical-device acceptance. No full suites run; CI owns those. Evidence paths occur only in invocation-owned artifacts, never portable test code.

## Disposition and handoff

Approved for one unpublished accumulated commit after the unchanged base, including this already-tracked reviewer record. Preserve all nine reviewed implementation blobs. DEVLOG already contains one issue-39 entry. No fix plan is needed and no stale issue-39 fix plans exist. Preserve the ignored original plan for engine finalization and record the final approved SHA there after committing. Finalization checks require one commit after base, unchanged reviewed blobs, and clean tracked state.

Publication, exact-SHA CI, live activation and Settings refresh/install remain engine/coordinator delivery work. After reviewed deployment, the coordinator must use current Settings to refresh/install the bundled skill while preserving owner edits; verify the effective aflow-plan guidance includes User Acceptance Pending. Account-local skills were not changed by this review. Owner-only checks in generated plans remain pending until their named owner records actual evidence; the retained phone scenario is an example, not a claimed pass.

Prior latest review rotated byte-for-byte to `plans/reviews/260911_1531.md`. No ignored private archive, plan or evidence is force-added.

No material findings


---

# CI stop/header readiness — cumulative approval, 2026-09-11

Base: `0114ea130b1a771d9710d5ddf6bc69e57b816565` (unchanged).
Reviewed HEAD: `65301b446775e7936a23297a979033ed49d6eb20`.
Branch: `aflow-ci-stop-header-readiness-20260911-20260911-163044`.
Coverage: 1 new / 1 total commit, `cp1 v01`, the complete original checkpoint
and all three changed files. No prior findings or fix overlays for this handoff.

No material findings under the admission gate, exclusions and proportionate-fix
discipline. App registry resolution is explicitly deferred; compact Projects
context is checked before resolution and exact full identity/title/ARIA after.
The browser holds capabilities on a fresh navigation, checks Loading runs and
zero exact Stop now controls, then releases and awaits exactly one control
before releasing the real fake-provider hold. Gate/context/server/provider
cleanup and status/revision/override/one-worker/no-stop/pending-review evidence
remain intact. Exact role/name selectors and count-one assertions still reject
missing/wrong controls or identities; no permanent negative product mutation.
Production and concurrent stop/recovery/progress interfaces are unchanged.

Inspected loadDashboard Promise.all and safe owner_stop admission: holding
capabilities blocks overall loading, not merely the control beside an already
visible pending notice. The controlled check establishes a real prerequisite,
not the precise historical macOS scheduling cause. Bounded original CI failure
excerpts confirm the immediate Stop now count and synchronous identity query.

Verification: reused completed final worker evidence from
`/root/code/agent_flow/.aflow/runs/20260911t163044z-b46b9a97/turns/turn-001/transport.stdout`
and its completed result.json (the supplied worktree-relative path is absent).
Final staged diff matches reviewed source; no source edits followed final tests.

- `npm --prefix apps/aflow_app/web test -- --run src/App.test.tsx -t 'keeps compact page context separate'`: 1 passed, 51 filtered out.
- `npm --prefix apps/aflow_app/web run build`: passed; production sources unchanged since build.
- `AFLOW_TEST_BROWSER=chromium uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_responsive_browser.py::test_stop_after_current_turn_delayed_worker_journey --basetemp=/root/code/evidence/aflow-dogfood-20260909/ci-stop-header-review-20260911/chromium`: 1 passed, 6.31s.
- Same exact node with `AFLOW_TEST_BROWSER=webkit` and `/webkit` basetemp: 1 passed, 7.74s.
- Independent `git diff --check 0114ea1 HEAD`: passed. Full suites remain CI-owned; no timeout inflation.

Approve one unpublished accumulated commit including this tracked reviewer
record. Preserve all three implementation blobs and the single handoff DEVLOG
entry. No fix plan exists or is needed. Leave ignored original plan in place
for engine finalization and record final approved SHA there after committing.
Verify one commit after base and clean tracked state. Publication, exact-SHA
CI and live activation remain controller/coordinator delivery steps, not
claimed by this local approval.

No material findings

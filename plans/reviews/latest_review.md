# Issue 6 — Cumulative review approved

<!-- Earlier accepted review records retained below; latest review is appended. -->

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

---

# Issue 10 — Cumulative review v01: rejected

Reviewed unchanged base `e224689f08986fdd0f96c0ceae60158f5b8b590c` through HEAD `1e1b83f19a93c791723ea73586abc51487cc3e12`: **2 new / 2 total commits**, CP1 v01 `0f945b7b` and CP2 v01 `1e1b83f1`, all 20 changed files across composition, REST/MCP, UI navigation, tests and documentation. No previous issue-10 findings or fix overlays existed. Material finding gate and proportionate-fix discipline applied; full original-plan range reviewed.

- F1 P1/high: `plan_service.py:128-131` ignores indented closing tilde fences, allowing copied log headings/instructions outside quotation; markdown-it reproduction confirms.
- F2 P2/high: `RunDashboard.tsx:1536-1537` skips busy-state cleanup after run selection invalidates a pending creation response, leaving creation/recovery controls disabled; deferred-promise regression fails.
- F3 P2/high: `plan_service.py:144-152` labels current/unfinalized turns or current_step as finalized when no finalized evidence exists; canonical-shaped probe confirms.
- F4 P2/high: `plan_service.py:168-173` reads flat diagnostics instead of canonical nested receipt fields, dropping available failure detail; canonical-shaped probe confirms.

All findings, commit references, minimal corrections, exact tests and acceptance criteria are in the single non-checkpoint overlay `plans/in-progress/issue-10-evidence-to-plan-20260909-cp01-v01.md`. No implementation changes, squash, DEVLOG compaction, publication or approval. Original plan and base remain in place.

Verification: `uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_plan_store.py -k 'from_run' --basetemp /root/code/evidence/aflow-dogfood-20260909/issue10-review-20260911/reviewer-server` passed 6 tests (24 deselected). Extended stale-response assertion failed as expected, one test / 104 skipped. Composer probe confirms all three evidence defects. `git diff --check e224689 HEAD` passed. Reused worker CP1 server/MCP/auth/discovery and CP2 142-component-test/build/Chromium/WebKit successful result records from the parent repository's run directory; those checks cover happy paths but do not contradict the reproduced defects. No full suites run. External evidence is under the supplied issue10 review directory. Temporary probe removed from the worktree and only reviewer-created uv lock noise restored; reviewed implementation blobs preserved.

Material fixes required

---

# Issue 10 — Cumulative review v02: approved

Unchanged Pre-Handoff Base HEAD: `e224689f08986fdd0f96c0ceae60158f5b8b590c`.
Reviewed HEAD: `7ee675b71fc52f2782aa6b8cc29ac44396ba4f29`.
Branch: `aflow-issue-10-evidence-to-plan-20260909-20260911-162131`.
Coverage: **1 new / 3 total commits**, CP1 v01 `0f945b7b`, CP2 v01
`1e1b83f1`, and follow-up CP1 v02 `7ee675b7`. Read the active fix overlay
and prior findings first, then reviewed all 20 changed files cumulatively
from the original base, including canonical receipt/context, ownership,
PlanService lifecycle and UI navigation contracts. No incremental-only review.

## Previous findings

- F1 resolved: outer fences account for 0–3-space-indented tilde runs.
  Independent CommonMark rendering verifies copied headings remain quoted;
  the AFlow parser retains one unchecked checkpoint with three unchecked steps.
- F2 resolved: pending-operation cleanup has separate ownership from selected-run
  response relevance. Both stale success and rejection release the busy state,
  suppress stale navigation/errors and permit the next submission.
- F3 resolved: finalized labels use only last_finished_turn. Missing context and
  unfinished first turns stay explicit; current-step evidence is unfinalized.
- F4 resolved: allowed canonical nested diagnostic, worker_error and wrapper_error
  prose is included with redaction, safe fences and the total 4 KiB evidence bound.

No unresolved prior findings and no new material findings under the admission
gate, exclusions and proportionate-fix discipline. The original two checkpoints
pass cumulative review. Creation uses the existing template and PlanService,
canonical authenticated reads and exact source links; promotion and launch
remain explicit. No controller or global configuration changes are introduced.

## Verification

Independent checks at reviewed HEAD:

```sh
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_plan_store.py -k from_run --basetemp /root/code/evidence/aflow-dogfood-20260909/issue10-review-20260911/review-v02-server
npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx src/components/PlanPanel.test.tsx src/api.test.ts
git diff --check e224689 HEAD
```

13 server tests and 143 affected component/API tests passed. A separate
markdown-it probe checked all four indentation cases and parser state;
`review-v02-markdown.txt` records the results. Existing Starlette deprecation
warning only. No full suites run; CI owns them.

Reused successful worker CP1 v02 evidence for the 3 MCP/auth tests, production
build and exact follow-up browser journey in Chromium and WebKit (one pass each).
Their code remains unchanged. Exact commands, exit codes and results extracted
from parent run turn-004/transport.stdout are retained in
`review-v02-retained-evidence.json`. Inspected the retained Chromium editor
screenshot: readable source-plan editing, Ready state and explicit Run menu.
Browser assertions cover source links, save/promote, unchanged source bytes and
no launch until Start run. Prior CP1 core/web discovery evidence remains valid.
Evidence root: `/root/code/evidence/aflow-dogfood-20260909/issue10-review-20260911/`.

## Approval finalization

Squash all three handoff commits onto the unchanged original base. Include this
already-tracked reviewer record (including prior review progress) and one compact
Issue 10 DEVLOG entry. Preserve every other reviewed implementation blob.
Archive the superseded fix overlay externally and remove it; create no new fix
plan and force-add no private files. Preserve the ignored original plan for
engine finalization, recording the final approved SHA there after commit creation.
Verify exactly one final commit after base and no dirty tracked files.
Reviewer-generated uv lock metadata noise was restored to reviewed HEAD.

Publication configuration is origin/main. Publication, exact-SHA CI and live
activation remain controller/coordinator delivery gates; this local approval
does not claim deployment. Preserve the active execution worktree and lineage.

No material findings


---

# CI immediate-stop admission — cumulative approval, 2026-09-11

Original/active plan: `plans/in-progress/ci-immediate-stop-admission-20260911.md`.
Branch: `aflow-ci-immediate-stop-admission-20260911-20260911-171136`.
Unchanged base: `92c3f442cfd27f27f90f02acb2129eebf52500ec`.
Reviewed HEAD: `933efe68f2ae31a7d6d298a1069d6f440ca68d34`.
Coverage: 1 new / 1 total commit, `cp1 v01`, the entire original checkpoint and
both changed files. No prior findings or fix overlays belong to this handoff.

No material findings under the admission gate, exclusions and proportionate-fix
discipline. Deferred capabilities block loadDashboard admission; after release,
the existing readiness helper observes Control team populated by selected-run
initialization before the confirmation click. Required confirmation text/button,
zero premature calls, one exact project/run/revision/idempotency request,
terminal response and zero boundary calls remain asserted. Missing or wrong
confirmation fails required lookups. Production source-change guards and
concurrent stop/recovery interfaces are unchanged. No timeout inflation.
Historical CI confirms missing confirmation; its precise scheduling cause
remains unproven, as correctly recorded in DEVLOG.

## State reconciliation

The supplied relative artifact is absent; the completed result is under
`/root/code/agent_flow/.aflow/runs/20260911t171135z-cbe60d6d/turns/turn-001/result.json`.
Three unchecked steps belong to snapshot_before; snapshot_after has zero and
DONE=true. Both current controller-root and worktree original plans have the
checkpoint and all three steps checked. Code and verification support those
marks. The manager-note discrepancy is absent from current plan copies.
No controller recovery or durable-state mutation is required.

## Verification

```sh
npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx -t 'keeps Stop now on the immediate endpoint|requests a boundary stop through revisioned control'
git diff --check 92c3f442 HEAD
```

Independent result: 2 passed, 100 filtered; whitespace check passed. Evidence:
`/root/code/evidence/aflow-dogfood-20260909/ci-immediate-stop-review-20260911/reviewer-component.log`.
The worker's command passed one case; its second filter matches no current name,
so review used the actual boundary-stop case. Reused accepted base App/build/
Chromium/WebKit evidence recorded above; those sources are unchanged. No whole
suite or build rerun. Private paths are absent from portable test code.

## Finalization

Approve one unpublished final handoff commit including this tracked review.
Preserve both implementation blobs and the single DEVLOG entry. No fix plan is
needed or created. Keep the ignored original plan for engine finalization and
record the final approved SHA there after committing. Verify one commit after
the unchanged base and clean tracked state. Publication, exact-SHA CI and live
activation remain controller/coordinator steps, not claimed by this approval.

No material findings


---

# Run-summary phone wrapping — cumulative approval, 2026-09-11

Original/active plan: `plans/in-progress/run-summary-phone-wrapping-20260911.md`.
Branch: `aflow-run-summary-phone-wrapping-20260911-20260911-174613`.
Unchanged Pre-Handoff Base HEAD: `72c84f9d900f6003347e21374158bd82426b55fc`.
Reviewed HEAD: `57c2326e1113d2fae55e138b0f1034f0171d561a`.
Coverage: 1 new / 1 total commit, `cp1 v01`, the complete original checkpoint
and all three changed files. No previous findings or fix overlays for this handoff.

No material findings under the admission gate, exclusions and proportionate-fix
discipline. The only production change adds overflow-wrap:anywhere to direct
run-detail dashboard-section paragraphs. The accepted Latest progress markup
matches this boundary, including decision/current turn/finalized summary prose.
There is no clipping, text mutation, font reduction or API/state change. Native
inputs, explicit raw report scrolling, visual progress and follow-up/stop/recovery
behavior retain their existing owners and markup.

Verification reuses valid completed worker evidence from
`/root/code/agent_flow/.aflow/runs/20260911t174612z-ce0f5a0a/turns/turn-001/transport.stdout`
and its successful result.json; the supplied worktree-relative artifact is absent.
The final source matches the tested diff. Inspected the complete browser journey,
fixture isolation, retained six final geometry JSON files and command exit codes.

- `npm --prefix apps/aflow_app/web run build`: exit 0.
- `AFLOW_TEST_BROWSER=chromium AFLOW_BROWSER_ARTIFACT_DIR=/root/code/evidence/aflow-dogfood-20260909/run-summary-wrapping-review-20260911/chromium-final-artifacts uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_run_progress_browser.py::test_run_summary_wraps_without_document_overflow --basetemp=/root/code/evidence/aflow-dogfood-20260909/run-summary-wrapping-review-20260911/chromium-final-basetemp`: 3 passed, 7.75s, exit 0.
- Same exact node with WebKit and corresponding `webkit-final-artifacts` / `webkit-final-basetemp`: 3 passed, 8.72s, exit 0.
- Required params: phone-320 (320x568), phone-390 (390x844), desktop (1280x720).
- Controlled document widths: Chromium 1521→320, 1521→390, 2005→1280;
  WebKit 1730→320, 1730→390, 2244→1280. Body widths match viewport;
  paragraph scroll/client widths match at 246, 316 and 934px respectively.
- Exact synthetic token preservation and actionable run menu open/Escape close
  are asserted. Private source text and host paths are absent from portable tests.
- Independent `git diff --check 72c84f9d HEAD` and clean initial tracked state passed.
  No full suites repeated; CI owns them.

The preliminary active fixture exposed an unchanged WebKit 320px diagnostics
hint at right=344.421875 while summary scroll/client widths already matched at
246px. The final completed-run fixture isolates the requested finalized-summary
case. That pre-existing hint is outside this fix; approval does not claim every
active-run phone state is free of overflow or physical-device acceptance.

Approve one unpublished final handoff commit, including this tracked reviewer
record. Preserve all three reviewed implementation blobs and the single DEVLOG
entry. No fix plan is needed or created. Leave the ignored original plan in place
for engine finalization; record final approved SHA there after committing.
Verify exactly one commit after the unchanged base and no dirty tracked edits.
Publication, exact-SHA CI and live activation remain controller/coordinator
steps and are not claimed by this local review.

No material findings

---

# Cumulative visual progress review — v19

Approved for engine-managed integration under material-code-review and aflow-review-squash. No material findings.

## Reviewed range

- Unchanged Pre-Handoff Base HEAD: `0290a03de7c3ed760422db0f3dba77a60a82be87`.
- Reviewed implementation HEAD: `5565ac752e852b01cc9b239957cf7a32c79b3cfd`.
- One new commit since `280b08924617891bcef9e2413485936dfbacd292`; 23 total handoff commits.
- Covers CP1–CP5 v01 plus cp01 v01–v18, cumulatively from the original base, not merely the latest fix.
- Scope reviewed: contained bounded evidence reads/cache; lineage, approval, counts, executor and change identities; canonical/REST/MCP serialization; global/project summaries; selected checkpoint timeline, refresh and responsive navigation; delivery honesty and documentation.
- Branch matches original-plan tracking; base remains reachable. No remote branch contains the reviewed HEAD.

## Prior findings

F1 containment; F2 deduplication/association; F3 stable IDs; F4 bounded reads/discovery; F5 pending routing; F6 repartition generations; F7 active/closed reviewer association; F8 unreadable-history uncertainty; F9 ordinal markers; F10 Merge evidence; F11 manager approval exclusion; F12 distinct change IDs; F13 successor executor lineage; F14 successful reviewer approvals; F15 actual hotplug models; F16 retry deduplication; F17 retry roles: resolved. Inspected their current cumulative code and reran the complete focused progress test module.

F18 is resolved by cp01 v18: canonical completion_turn_number links only to a unique invocation with exact source run, turn and worker/reviewer role. Missing/ambiguous targets retain the recorded completion turn as text; missing evidence remains unknown; explicit event-ID compatibility remains. Component and actual browser coverage verify this producer/consumer contract.

A candidate concerning generic failed-review classification was excluded: the inspected production process-failure path writes harness-failed, not the proposed generic failed status. No demonstrated supported failure justified admitting it.

## Verification

Fresh local checks at the reviewed HEAD:

- `uv run pytest -q tests/test_run_progress.py --basetemp=/root/code/evidence/aflow-dogfood-20260909/visual-progress-review-20260911/review-v19-core --tb=short`: 38 passed.
- `uv run pytest -q tests/test_control_plane_repository.py::test_progress_projection_failure_does_not_hide_base_status tests/test_control_plane_repository.py::test_progress_cache_is_separated_by_project_and_refreshes_new_turn_evidence apps/aflow_app/server/tests/test_control_plane_api.py::test_progress_transport_models_keep_optional_status_and_full_detail_shapes tests/test_control_plane_services.py::test_context_detail_reuses_status_summary_without_full_context_escalation apps/aflow_app/server/tests/test_mcp.py::test_mcp_run_context_progress_matches_authenticated_rest --basetemp=/root/code/evidence/aflow-dogfood-20260909/visual-progress-review-20260911/review-v19-contracts --tb=short`: five passed.
- `npm --prefix apps/aflow_app/web test -- --run src/components/CheckpointHistory.test.tsx src/components/RunProgress.test.tsx`: 25 passed.
- `npm --prefix apps/aflow_app/web run build`: passed.
- `uv run ruff check aflow/control_plane apps/aflow_app/server/src`: passed.
- `git diff --check 0290a03 HEAD`: passed.
- Exact browser node `apps/aflow_app/server/tests/test_run_progress_browser.py::test_checkpoint_history_review_evidence_and_generation_refresh`: one passed in Chromium, one in WebKit, via `uv run --project apps/aflow_app/server pytest -q ... --tb=short`. Invocation-owned AFLOW_BROWSER_ARTIFACT_DIR and --basetemp paths use review-v19-chromium / review-v19-chromium-tmp and review-v19-webkit / review-v19-webkit-tmp under the evidence root above. WebKit uses AFLOW_TEST_BROWSER=webkit. Fresh Chromium screenshot inspected.
- Reused retained CP5 viewport/theme and v17 GlobalRunOverview/RunDashboard proof for unchanged implementations, together with previous focused regression evidence. CI owns full suites.
- Prompt-supplied worktree-relative turn-041/result.json is absent; the decision relies on code/history, retained evidence and the independent checks above.

## Approval finalization

Squash all 23 unpublished handoff commits into one after the unchanged base, including this tracked review record and the compacted DEVLOG entry. Preserve implementation blobs, delete the superseded private v18 overlay, and leave the ignored original plan in place for engine finalization. Record the final approved SHA in original-plan tracking only, avoiding a self-referential commit.

The test invocation's incidental server uv.lock refresh was restored to reviewed bytes. No implementation, controller, shared runtime installation, account or live-state edits were made by review.

Publication, integration with current main, exact-SHA CI, live activation and physical-device acceptance remain downstream and unclaimed. Preserve loading, recovery/pending-review/startup guards and concurrent stop-after-turn controls during integration.

No material findings

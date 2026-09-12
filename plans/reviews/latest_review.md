# Team families — cumulative review v01 rejected

Reviewed `010c71964317d1a2d072ce3496def47e07d3e2e2` through
`76d90a507d4fdec0042c5741110c09bd6a98b59e`: 7 new / 7 total commits,
`cp1 v01` through `cp7 v01`, against the original seven-checkpoint plan.
No previous findings apply to this handoff. Implementation/history unchanged;
no squash, approval, publication or live activation.

Five admitted findings, all P2/high confidence:

- R1: `GlobalSettings.tsx:711-718` sends family changes straight to setDraft;
  effective/source projections remain stale after unsaved Base/global edits or
  override removal. The canonical coordinator exists but is not wired to the
  owner. Connect it with stale-response handling and declaration preservation.
- R2: `TeamFamiliesSettings.tsx:433-434` trims the controlled display-name value
  on every keystroke. Typing Product development yields Productdevelopment.
  Retain editing text and normalize at a commit boundary.
- R3: `guided_config.py:526-531` removes roles only from nested roles tables.
  A valid legacy inline worker override survives selector=null with unchanged
  source bytes. Remove from the actual declaration representation and preserve
  valid legacy conversion/round-trip behavior.
- R4: `TeamFamilyWizard.tsx:550` keys the fieldset by editable ID and position.
  Typing an ID remounts the focused input and closes Technical details. Use an
  immutable UI identity retained across edits/reorder, never stored in TOML.
- R5: `TeamFamilyWizard.tsx:371-381` rejects generated-ID collisions before
  creating any editable stage. Add two stages, remove the first, then add:
  Strongest worker collides with the survivor and no correction input appears.
  Allow explicit correction before acceptance without suffixing existing IDs.

Concrete scenarios, commit references, implementation instructions and exact
verification commands are in the single private fix overlay:
`plans/in-progress/team-families-inheritance-and-creation-20260911-cp01-v01.md`.
Original-plan tracking carries the review range and unresolved finding IDs.

Retained worker transport evidence confirms 15 focused Python, 87 server,
5 selected web cases, build, and 7 Chromium + 7 WebKit viewport journeys passed.
The browser inheritance checks save/reload first and do not cover R1. Four
reviewer UI probes reproduced R1/R2/R4/R5; a pure authoring probe reproduced R3.
Probe assertions describe defective behavior. Artifacts are external under
`/root/code/evidence/aflow-dogfood-20260909/team-families-review-20260911/review-v01/`.
The temporary probes and probe-triggered lockfile update were removed.
`git diff --check 010c7196 HEAD` passed. No full suite was repeated.

The next review must resolve all five findings and review the full accumulated
range from the unchanged Pre-Handoff Base HEAD before any squash approval.

Material fixes required

---

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

# Team families — cumulative review v02: rejected

Unchanged base: `010c71964317d1a2d072ce3496def47e07d3e2e2`.
Reviewed HEAD: `15644e6530e021953d224130861107f49c916de0`.
Coverage: **1 new / 8 total commits**, cp1–cp7 v01 plus follow-up cp01 v01.
Read the active overlay and prior findings first; inspected cumulative core,
authoring, draft/editor/wizard, launch, acceptance and documentation changes
from the original base. R2–R5 resolved. R1 remains partially unresolved.

- **R1 P2/high:** GlobalSettings.tsx:277–281 invalidates on a no-op draft change;
  the action-key effect at 404–437 does not rerun. Change a worker, hold its
  preview, blur an unchanged display name and release the reply: copying the
  source is allowed with the stale worker. Match invalidation and copy eligibility
  to current semantic/source identity; preserve matching pending/error state.
- **R6 P2/high:** RunDashboard.tsx:2495–2499 substitutes complete family membership
  for the selected team's upgrade chain. With inheritance but no upgrade edges,
  the preview claims a configured Base → child escalation. Keep every member
  selectable, but show only actual declared upgrade routes. Fix the related
  route hints in runPresentation.ts:97–105 without changing runtime semantics.

Both claims reproduced through actual owner/component tests with desired-behavior
assertions. Probes, final logs, prior overlay and extracted worker evidence are
external under `team-families-review-20260911/review-v02/`. Temporary test copies
were removed. No production edits or history rewrite.

Verification: affected six-file web run had 114 passes and one transient existing
save-test failure; exact retry passed, then GlobalSettings passed 58/58. Reused
completed final worker server 91/web 115/build and Chromium 7/7 + WebKit 7/7
results. Retained core/runtime/MCP evidence remains applicable. Cumulative diff
whitespace check passed. Inspected retained Chromium phone light screenshot.
No physical keyboard, publication, CI or live activation claim.

Created exactly one non-checkpoint fix overlay:
`plans/in-progress/team-families-inheritance-and-creation-20260911-cp01-v02.md`.
Archived and removed superseded v01; original plan/base remain in place.
No squash, DEVLOG compaction, approval or publishing. Prior tracked reviewer
progress is preserved. Next review must resolve R1/R6 and review the full
original-base range before any approval/squash.

Material fixes required


---

# Integrated refresh delivery repair — cumulative approval, 2026-09-11

Original/active plan: `plans/in-progress/visual-progress-refresh-delivery-20260911.md`.
Unchanged Pre-Handoff Base HEAD: `f0ceacc4749d9a8490b81cec3177247ce5952fd6`.
Reviewed HEAD: `1d6ae9368b8cd42cf1ff86c63acbbc974fabaae4`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all original checkpoint requirements
and both changed files. Branch matches plan tracking. No prior findings or fix
overlays belong to this repair; the integrated visual feature is the approved base.

No material findings under the admission gate, exclusions and proportionate-fix
discipline. Exact Stop now… and Stop after current turn assertions replace only
the obsolete Owner stop… lookup. Full diagnostics, selected checkpoint and
Restart with changes assertions survive. Existing boundary and immediate cases
retain distinct endpoints, revision/idempotency requests and immediate confirmation.
Production controls and source failure lineage `20260911t154751z-382a2f5f` are unchanged.

## Verification evidence

Reused inspected completed worker evidence from
`/root/code/agent_flow/.aflow/runs/20260911t231826z-021934df/turns/turn-001/transport.stdout`
(item_40, exit 0) and successful result.json. The supplied worktree-relative
artifact is absent; the controller-root receipt records DONE=true and zero
unchecked checkpoints. The tested patch matches reviewed HEAD.

```sh
npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx -t 'inspects selected checkpoint history while preserving full diagnostics on refresh|requests a boundary stop through revisioned control and keeps the immediate stop separate|keeps Stop now on the immediate endpoint and confirms its terminal response'
git diff --check f0ceacc4749d9a8490b81cec3177247ce5952fd6 HEAD
```

All three named cases passed; 106 filtered, 109 total. Initial missing Vitest
was resolved with npm ci before this successful run. Independent cumulative
whitespace check passed. No full suite, build or browser repeat; retained approved
product evidence remains applicable to this test-only change. No private paths
were added to portable test code.

## Approval finalization

Approve one unpublished final handoff commit after the unchanged base, including
this tracked review record. Preserve both reviewed implementation blobs and the
single repair DEVLOG entry. No fix plan is needed or created. Keep the ignored
original plan in place for engine finalization and record final approved SHA
there after committing. Verify exactly one final commit and clean tracked state.
Publication, exact-SHA CI and live activation remain downstream engine/coordinator
gates and are not claimed by this review. Preserve concurrent family work.

No material findings

---

# Visual CI contract integration — cumulative approval, 2026-09-12

Original/active plan: `plans/in-progress/visual-ci-contract-integration-20260911.md`.
Unchanged Pre-Handoff Base HEAD: `50b2e3fb0e678b8228ff9abc7a285b926e5891ca`.
Reviewed implementation HEAD: `9622d945588f7940fc3a4b735092c2c5f4ada7b1`.
Coverage: 3 new / 3 total commits: `cp1 v01` (`d3579fd1`), `cp2 v01`
(`57dc8d5d`), and `cp3 v01` (`9622d945`). Reviewed all 13 changed files
and the full original plan from base through HEAD. Branch matches tracking;
all checkpoints are checked. No prior findings or fix overlays belong to this
handoff. The approved visual feature and source run382 lineage remain in the base.

No material findings under the material-code-review admission gate, exclusions
and proportionate-fix discipline. Outer ContextBundle keys use the existing
secret rule before progress dispatch; typed progress retains its independent
bounds. Context builds the existing bounded execution projection using the
already-built manager context and leaves canonical history/status unchanged.
Dashboard compatibility preserves current/finished and recovery prose without
deriving canonical approvals from it; old-only and canonical-only inputs remain
distinct. Direct-child Run history selectors exclude nested checkpoint layouts.
Existing recovery, exact summary token, geometry, focus, history, controls and
menu assertions survive; checkpoint selection and compact Back gain coverage.
No portable test embeds coordinator HOME or evidence paths.

## Verification evidence

Reused inspected command executions and exit codes in the completed worker
transports at `/root/code/agent_flow/.aflow/runs/20260911t233541z-f641894b/turns/`
(`turn-001`, `turn-002`, `turn-003`, each `transport.stdout` and `result.json`).
The prompt's worktree-relative result is absent; these controller-root records
retain the exact commands and successful results. Tested source matches the
reviewed cumulative implementation. No full suites or redundant build rerun.

- CP1: required `tests/test_api.py::test_public_control_plane_models_are_versioned_and_redact_secrets`
  failed before the patch and passed afterward. It and exact new nodes
  `test_context_bundle_redacts_nested_sensitive_keys_and_values` and
  `test_context_bundle_preserves_large_progress_detail_history` passed together
  (3 tests) under `uv run pytest -q ... --tb=short`.
- CP2: `tests/test_control_plane_services.py::test_context_detail_reuses_status_summary_without_full_context_escalation`
  and `tests/test_run_progress.py::test_finished_review_stays_last_finished_when_new_turn_is_starting`
  passed (2 tests). CP3 repeated the required API and context-builder cases (2 passed).
- Dashboard: `npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx -t 'renders verified repair progress|keeps the bounded execution summary beside canonical history|does not reinterpret canonical-only progress as the legacy summary|renders a known partial scope|renders unavailable evidence|advances from a repairing scope|renders canonical list progress|inspects selected checkpoint history'`
  passed all 8 selected cases; 103 cases were filtered out, not browser skips.
- All ten exact browser node IDs and viewport parameters in the original plan
  passed under both `AFLOW_TEST_BROWSER=chromium` (72.65s) and `webkit` (88.09s),
  using `uv run --project apps/aflow_app/server pytest -q --tb=short --basetemp=...`.
  Separate disposable HOME/XDG directories and random server ports were used.
  The initial Chromium attempt lacked binaries after HOME isolation; setting
  PLAYWRIGHT_BROWSERS_PATH to the installed cache resolved startup without
  changing tests, timeouts or assertions.
- Browser basetemps: `/tmp/aflow-cp3-chromium-retry.GxGrgV` and
  `/tmp/aflow-cp3-webkit.zzxzc7`. Reviewer inspected retained 390px WebKit light
  live-control and Chromium dark unknown-successor screenshots. Exact retry,
  control and run-navigation surfaces remain readable.
- Retained geometry under `/root/code/evidence/aflow-dogfood-20260909/`
  `cp3-chromium-retry-artifacts.BbjZhV` and `cp3-webkit-artifacts.0k452I`
  confirms document/body widths of 320, 390 and 1280 in both engines;
  paragraph scroll/client widths match at 246, 316 and 934 respectively.
- `npm --prefix apps/aflow_app/web run build` and
  `uv run ruff check aflow/control_plane apps/aflow_app/server/src` passed.
  Optional wider lint reported unchanged fixture redefinitions and two baseline
  web lint errors; these are outside the introduced changes.
- Independent `git diff --check 50b2e3f HEAD` passed; initial tracked state was
  clean and no remote branch contains the reviewed HEAD.

## Approval finalization

Squash all three unpublished handoff commits onto the unchanged base, including
this tracked reviewer record. Preserve all 13 reviewed implementation blobs.
Only one DEVLOG entry belongs to this handoff, so no compaction is needed.
No fix overlay exists and no new plan is created. Keep the ignored original plan
for engine finalization and record the final approved SHA there after committing.
Verify one final commit after base and no remaining tracked edits.
Publication, exact-SHA CI  repair confirmation, live activation and physical-device
acceptance remain downstream engine/coordinator gates, unclaimed by local approval.
No controller, owner state, shared installation or concurrent checkout is changed.

# Team families — review v03: rejected

Unchanged base: `010c71964317d1a2d072ce3496def47e07d3e2e2`.
Reviewed HEAD: `aa1347ce5c700a389ad7515bc84ff9318a475ae5`.
Coverage: **1 new / 9 total commits**, cp1–cp7 v01, cp01 v01/v02.
Prior findings read first; original-base cumulative source was inspected across
canonical runtime/config, server authoring, draft/editor/wizard and launch.
R2–R5 remain resolved; R6 is resolved. R1 still fails, so this is not an
approval-grade cumulative pass; full original-base review remains mandatory
after the repair.

**R1 P2/high — GlobalSettings.tsx:483–485:** Change Product Worker to codex.deep,
hold its automatic preview, open Advanced and allow its separate preview to
succeed, return to Guided, and copy Product into New family. The copy uses
codex.worker. The transition marks the stale closure draft current instead of
applying the canonical response, then preserves that state on return; the
semantic effect key does not change. This can save unintended assignments.
Merge matching canonical projection fields before marking current, preserve
declarations, and test editor-transition recovery plus obsolete replies.

Independent actual-owner probe fails its desired assertion; 179 existing tests
in GlobalSettings/RunDashboard/runPresentation pass. Cumulative whitespace
check passes. Evidence: external team-families-review-20260911/review-v03/
(probe source/log, affected.log and archived v02 overlay). Completed worker
turn-011 transport confirms retained focused settings/launch/build checks;
its browser matrix includes successful Chromium phone-landscape retry.
No full suites or live configuration/service operations.

Created exactly one non-checkpoint fix plan at
`plans/in-progress/team-families-inheritance-and-creation-20260911-cp01-v03.md`.
Archived and removed superseded v02; original remains in place with unchanged
base. Temporary probe removed. All nine implementation commits and production
blobs preserved; tracked reviewer progress intentionally remains for eventual
approval finalization. No squash, DEVLOG compaction, publishing, CI, live
activation or physical-keyboard acceptance claimed.

Material fixes required


---

# Team families — review v04: rejected

Base `010c71964317d1a2d072ce3496def47e07d3e2e2` → HEAD
`46a624608202415fb7840042e88c537035d2edb9`: **2 new / 11 total commits**,
cp1–cp7 v01 and cp01 v01/v02/v03 (two v03 commits).
Prior findings read first; original-base cumulative source inspected across
configuration/runtime, server authoring, draft/family/editor/wizard and launch.
R2–R6 remain resolved. R1 stale-value cases pass, but recovery remains incomplete.

**R1 P2/high — GlobalSettings.tsx:457–469, 482–490:** Open Advanced from a
valid clean draft, return to Guided, then copy Product in New family. Copying is
blocked by “Wait for a current settings preview” with no pending request.
Unconditional invalidation loses current readiness; zero actions return no
response, and the unchanged semantic key never schedules recovery. Preserve a
matching valid projection for unchanged input or preview the actual document
pair; keep all stale/pending/error guards. This is a local transition fix.

Reviewer probe fails the desired round-trip assertion; initial-state copying
control passes. Four affected web files: 94 passed / 1 existing save-test failure;
exact isolated retry passed. Both v03 transition regression cases passed.
Cumulative whitespace check passed. Retained build/core/server/runtime and
seven-viewport Chromium/WebKit evidence reused; no full suites or live edits.
Evidence: external team-families-review-20260911/review-v04/.

Created the one self-contained non-checkpoint v04 fix overlay, archived/removed
v03, and updated original-plan tracking. Temporary probe removed. All 11 commits
and implementation blobs preserved. No approval-grade cumulative pass, squash,
DEVLOG compaction, publication, CI or live activation claimed. The next review
must resolve R1 and repeat the complete original-base review before approval.

Material fixes required


---

# Team families — cumulative review v05: rejected

Base `010c71964317d1a2d072ce3496def47e07d3e2e2` → HEAD
`53f0da0b45b5c10d8f80f32ffbdd694902531942`: **1 new / 12 total commits**,
cp1–cp7 v01 plus cp01 v01/v02/v03/v04 (two v03 commits).
Read prior overlay/findings first, verified their fixes, then inspected full
original-base implementation across config/runtime, server authoring/contracts,
family draft/conversion/editor/wizard, launch, acceptance and documentation.
The prior clean Advanced round-trip defect is repaired; R2–R6 remain resolved.

- **R1 P2/high — GlobalSettings.tsx:584–588:** Save only General/Bind host, then
  Teams → New family → copy Product. Unconditional workflow-preview invalidation
  loses readiness; no configuration acknowledgement or semantic identity change
  restores it. Copying stays blocked with no request pending. Introduced by
  `15644e65`; preserve matching preview state for unrelated saves or explicitly
  recover the actual pair. This is a remaining instance of R1's readiness loss.
- **R7 P2/high — TeamFamilyWizard.tsx:291–315:** With global defaults selected,
  edit Worker and Reviewer prompt, then choose a copy source. Manual assignments
  are replaced immediately because copyEdited stays false until a source was
  already copied. Introduced by `6dbefe04`. Mark Base edits independently of
  copySource and reuse explicit Keep/Replace confirmation, as CP5 requires.

Both reproduced with desired-behavior assertions. Existing affected tests passed
96/96; cumulative whitespace check passed. Reused final turn-015 build and
unchanged core/server/runtime/MCP and seven-viewport Chromium/WebKit evidence.
External `team-families-review-20260911/review-v05/` retains probes, logs, build
records and archived v04. Initial save probe's wrong tab selector was corrected;
the final save probe reaches the copy failure. No full suites or live operations.

Created exactly one self-contained non-checkpoint v05 fix plan at the requested
path, carrying R1 and new R7; removed superseded v04 and temporary probes. Original
plan/base and all twelve implementation commits remain intact. No squash,
DEVLOG compaction, publishing, CI/live activation or physical-keyboard claim.
Tracked reviewer edits remain for eventual approval-grade finalization.

Material fixes required

---

# Team families — cumulative review v06: rejected

Base `010c71964317d1a2d072ce3496def47e07d3e2e2` → reviewed HEAD
`f265fb8cd686c97edc166c1db5feb2d3e6856146`: **1 new / 13 total commits**,
cp1–cp7 v01 and follow-ups cp01 v01/v02/v03/v04/v05 (two v03 commits).
Read the prior overlay/findings first and inspected original-base cumulative
config/runtime, server authoring/contracts, draft/conversion/editor/wizard,
launch, responsive acceptance and documentation. No incremental-only review.
The v05 server-only save recovery and R7 confirmation fixes pass; R2–R7 are
resolved. R1 remains partially unresolved in rejected configuration saves.

- **R1 P2/high — GlobalSettings.tsx:587–591:** Change Product Worker to codex.deep,
  finish preview, receive a rejected configuration Save all, then try copying
  Product into New family. Copying remains blocked by an idle preview wait.
  Eager invalidation cancels readiness, rejection skips acknowledgement, and
  unchanged semantic identity schedules no replacement. Preserve the matching
  current/pending draft projection until actual source/declaration changes or
  acknowledgement; never blindly mark stale data current. Root introduced by
  `15644e65`, only partially repaired by `f265fb8c`.
- **R8 P2/high — TeamFamilyWizard.tsx:586–589:** With Base A → stage B → stage B,
  the wizard omits the required ineligibility warning on the second stage;
  with Base A → stage B → inherited A, it falsely displays one. It compares
  every stage to Base instead of the preceding stage, contrary to invariant 8
  and manager selector-equality eligibility. Introduced by `6dbefe04`. Compare
  the first stage to Base and later stages to their actual predecessor; retain
  Base inheritance and Base-relative difference counts.

Both defects independently reproduced. Existing affected four-file run:
99 passed / 1 existing effort-blur save-test failure; exact isolated retry
passed. Corrected owner probe reaches the copy-selection failure; two wizard
probes reach wrong-eligibility assertions. Initial owner probe lacked setup
mocks and is excluded from defect evidence. Reused final turn-017 100-test and
build evidence, plus unchanged core/server/runtime/MCP and seven-viewport
Chromium/WebKit evidence. Original-base whitespace check passed. Evidence and
archived v05 are in external `team-families-review-20260911/review-v06/`.

Created exactly one non-checkpoint overlay at
`plans/in-progress/team-families-inheritance-and-creation-20260911-cp01-v06.md`.
Archived/removed v05, removed temporary probes, and updated original tracking.
All thirteen commits and implementation blobs remain intact. No squash,
DEVLOG compaction, publication, CI/live or physical-device acceptance claim.
Next review must resolve R1/R8 and repeat the full original-base review.

Material fixes required


---

# Team families — cumulative review v07: rejected

Base `010c71964317d1a2d072ce3496def47e07d3e2e2` → reviewed HEAD
`c34078f9bcf30217672ff2809c61cd2f961c4013`: **1 new / 14 total commits**,
cp1–cp7 v01 and cp01 v01/v02/v03/v04/v05/v06 (two v03 commits).
Read active overlay and prior findings first, then inspected the full original-base
configuration/runtime, authoring/contracts, draft/conversion/editor/wizard,
launch, responsive acceptance and documentation. R2–R8 are resolved; v06 fixes
rejected-save and candidate-preview recovery. R1 has a remaining transition path.

**R1 P2/high — GlobalSettings.tsx:462–465 (catch:511–513):** Change Product Worker
to codex.deep, finish its preview, then fail the read-only Advanced TOML preview.
The unchanged Guided draft remains, but New family cannot copy Product: the
coordinator was invalidated eagerly and no semantic/source change schedules
replacement. The owner probe expects product and receives Use global defaults.
This extends R1's preview-ownership root introduced by `15644e65`; move
invalidation to accepted transition/source replacement, preserving matching
current/pending state on rejection and rejecting obsolete replies on success.
No arbitrary current-state restoration or new state framework is needed.

Independent focused run: 106 passed / 1 previously recorded effort-save failure;
exact isolated retry passed. Retained completed turn-019 evidence confirms final
107/107 and production build. Unchanged core/server/runtime/MCP and the required
seven-viewport Chromium/WebKit matrix remain applicable. Original-base whitespace
check passed. External `team-families-review-20260911/review-v07/` retains the
failing actual-owner probe, logs, results, worker receipt and archived v06 overlay.
Temporary repository probe removed; no full suites or live operations performed.

Created exactly one self-contained non-checkpoint fix plan at
`plans/in-progress/team-families-inheritance-and-creation-20260911-cp01-v07.md`.
Archived/removed v06 and updated original tracking. All 14 implementation commits
and implementation blobs remain intact; no squash, DEVLOG compaction or approval.
Tracked reviewer records remain for eventual approval finalization. Original plan
stays in place. Next review must resolve R1 and repeat the full original-base range.
Publication, exact-SHA CI, live activation and physical mobile keyboard acceptance
are not claimed.

Material fixes required


---

# Team families — cumulative review v08: approved

Unchanged Pre-Handoff Base HEAD: `010c71964317d1a2d072ce3496def47e07d3e2e2`.
Reviewed HEAD: `86c6a23a40bfd2fe875b32702acf45119b5a5704`.
Previous reviewed HEAD: `c34078f9bcf30217672ff2809c61cd2f961c4013`.
Coverage: **1 new / 15 total commits**, cp1–cp7 v01 and follow-ups
cp01 v01/v02/v03/v04/v05/v06/v07, including both v03 commits.

Read the active v07 overlay and prior findings first. R1 is resolved: current
and matching pending projections survive a rejected Advanced transition;
successful transitions accept canonical fields and supersede obsolete replies.
Retained R1 no-op blur, clean/dirty editor round trips, failed previews,
unrelated saves, rejected configuration saves and rejected family candidates
remain covered. R2 display-name typing, R3 inline-role editing/removal,
R4 stable component identity, R5 generated-ID correction, R6 declared-edge launch
routes, R7 replacement confirmation and R8 adjacent-stage eligibility remain
resolved. No unresolved earlier findings.

Repeated the full cumulative original-base review of all seven checkpoints and
all follow-ups: canonical inheritance/validation and runtime precedence;
revisioned typed authoring, final-pair validation and sparse declarations;
family topology, conversion parity and reference-safe removal; shared draft,
preview, editor and wizard ownership; exact-ID/default launch presentation;
responsive acceptance, runtime regression and documentation. No material
finding passes the admission gate. Existing shell/scroll owners, visual-progress
interfaces and evidence-to-plan navigation are preserved.

Independent verification:

```sh
npm --prefix apps/aflow_app/web test -- --run src/components/GlobalSettings.test.tsx src/components/TeamFamilyWizard.test.tsx src/components/TeamFamiliesSettings.test.tsx src/settingsDraft.test.ts
npm --prefix apps/aflow_app/web test -- --run src/components/ReviewV08Owner.test.tsx -t 'review v08'
git diff --check 010c719 HEAD
```

109 affected tests passed. The additional actual-owner probe passed: guided
worker change → Advanced raw edit → Guided → copy Product retains codex.deep
without persistence. Temporary probe archived externally and removed. Whitespace
check passed. Reused completed turn-021's 109-test and TypeScript/Vite build
receipts, unchanged canonical/runtime/MCP and 91-test server evidence, plus the
retained seven-viewport Chromium/WebKit family journeys (including recorded
successful retries). Inspected retained light/dark screenshots; the dark image
is a compact resized state despite its original viewport filename. No whole
suite rerun or live owner configuration/service operation. Evidence is under
`/root/code/evidence/aflow-dogfood-20260909/team-families-review-20260911/review-v08/`;
earlier referenced evidence remains intact. Physical mobile keyboard acceptance
is unverified.

Approval finalization: squash all fifteen handoff commits onto the unchanged
base, including this already-tracked reviewer record and prior reviewer progress.
DEVLOG already contains exactly one handoff entry, so no compaction is needed.
Preserve every other reviewed blob. Archive/remove the v07 overlay; create no
v08 fix plan. Keep the ignored original plan in place and record the final SHA
there after committing. Verify one accumulated commit, no stale fix plans and
clean tracked Git state. Do not force-add private plans/evidence. Publication,
exact-SHA CI and live activation remain controller/coordinator delivery gates;
this approval is local and does not claim deployment.

No material findings


---

# Dirty settings header — cumulative review v01: approved

Base `a4ca848966cc422690d8b382cdd6c2424e60ba3c` → reviewed HEAD
`f9867a03bef1baa11f39412e7e3d5052b75815fa`: **3 new / 3 total commits**,
cp1 v01 (`fde2cbb8`), v02 (`92e264d4`), v03 (`f9867a03`).
Reviewed the full original-base diff and surrounding header ownership, media
listener, selector/tab keyboard behavior, browser fixtures and launch assertions.
Both source test commits were imported in order with matching test blobs.
Read the source v01/v02 reviews: R1 (clearing dirty effort) remains resolved;
the source WebKit layout blocker is resolved within this plan's authorized scope.
No material findings remain. Production changes only the independent header
width threshold to 1399px, retaining 599px height and list/detail behavior.

Reused raw successful worker command receipts from
`/root/code/agent_flow/.aflow/runs/20260912t011927z-ffcf1747/turns/turn-001/transport.stdout`
and its completed result. The prompt's worktree-relative receipt is absent;
the controller-root receipt is available. Focused command:
`npm --prefix apps/aflow_app/web test -- --run src/components/GlobalSettings.test.tsx -t 'preserves a dirty section and draft across header presentation resize'`
passed 1 selected case (71 filtered); initial missing dependencies were installed
before this successful run. The single `npm --prefix apps/aflow_app/web run build`
passed. Final exact browser commands used `AFLOW_TEST_BROWSER=chromium` and
`webkit` with `uv run --project apps/aflow_app/server pytest -q`, both nodes
`test_run_navigation_browser.py::test_complete_navigation_and_launch_journeys`
and `test_settings_browser.py::test_settings_toolbar_stays_visible_through_long_scroll`,
`--tb=short` and separate external `--basetemp` directories. Final results:
Chromium 2 passed (28.55s); actual WebKit 2 passed (36.33s).

Final artifacts: `/root/code/evidence/aflow-settings-header-chromium-final2-dNu8H5`
and `/root/code/evidence/aflow-settings-header-webkit-final2-yv8V8R`.
Reviewer inspected WebKit 1280/1440 dirty screenshots and Chromium dark 390px
compact screenshot: selector/tabs, unsaved indicator, Save and More remain visible.
Retained assertions enforce 112/128 geometry and existing viewport/theme,
scroll/focus and exact-ID checks. No reset removes effort before these checks.
Independent cumulative `git diff --check` passed. No redundant build, full suite,
shared installation change or live owner-state mutation during review.

Approval finalization combines the two handoff DEVLOG entries and includes this
tracked reviewer record in one unpublished commit after the unchanged base.
Preserve every reviewed implementation/test blob. No fix overlay exists or is
created. Keep original ignored plan in place and record final approved SHA there,
then verify exactly one commit after base and clean tracked Git state.
Publication, exact-SHA CI and live activation remain downstream engine delivery
gates, not claims of this local approval. Physical-device acceptance is unverified.

No material findings


---

# Startup error observation — cumulative review v01: approved

Unchanged Pre-Handoff Base HEAD: `837fdc458ea0aa5b5124e6f58241cc20073c4f8c`.
Reviewed HEAD: `0767a332a15e6c13408dc73bfddaca4d742b9d38`.
Coverage: 1 new / 1 total commit, `cp1 v01`, original checkpoint 1 in full.
Original and active plan are `ci-startup-error-observation-20260912.md`;
no previous findings or fix overlays apply to this handoff.

Reviewed the complete two-file diff, fixture ownership and cleanup, manager
startup/observation, process-birth identity, and actual wrapper error contract.
The original actionable-error and final inactivity assertions are preserved.
Bounded observation permits actual wrapper exit; the explicit release barrier
proves the same owned wrapper remains active/start-post after its error receipt.
Finally releases the private barrier and guards fallback signaling by recorded
process-birth identity. Production lifecycle and schema are unchanged.
No material findings pass the admission gate.

Retained completed worker transport command receipts at
`/root/code/agent_flow/.aflow/runs/20260912t014644z-490f4a4b/turns/turn-001/transport.stdout`
confirm both changed tests passed (2 in 0.46s), followed by
`uv run pytest -q tests/test_persistent_units.py --tb=short`
(9 passed in 2.15s, exit 0). The controller-root result confirms completion;
the prompt's worktree-relative result is absent. Reused valid evidence without
repeating tests. Independent `git diff --check 837fdc458ea0aa5b5124e6f58241cc20073c4f8c HEAD`
passed. No full suite, build, or shared runtime changes.

Approval finalization includes this tracked record in exactly one unpublished
handoff commit after the unchanged base. Preserve both reviewed implementation
blobs and the single handoff DEVLOG entry; no compaction or fix plan is needed.
Leave the ignored original plan in place for engine finalization and record
the approved SHA there after committing. Verify one commit and clean tracked
state. Publication, exact-SHA CI and live activation remain controller delivery
gates and are not claimed by this local review.

---

# CI UI readiness — cumulative review v01: approved

Base `837fdc458ea0aa5b5124e6f58241cc20073c4f8c` → reviewed HEAD
`1fb7708b00b5c936c1701e2de8be58711936d52f`: **1 new / 1 total commit**,
`cp1 v01`. No prior findings or follow-up overlays for this handoff.
Reviewed the full base-to-HEAD diff, original plan, both component owners and
applicable UI instructions. Scope remains two component test files and DEVLOG.
Exact corrected filename, single-flight mutation, returned plan path and all
save/blur/Enter effort-null assertions remain intact.

Settings' held server response demonstrates the disabled owning fieldset and
Working action despite visible projected fields. Follow-up detail visibility
alone is not an enabled-state boundary: the field is already usable before the
held detail resolves. An independent temporary probe edited that connected,
enabled field under awaited act before releasing detail, then verified the edit
survived hydration and the exact submission/path/single-flight assertions passed.
Thus no production lost-edit defect was demonstrated; this approval covers test
synchronization, not a production bug fix or a proven detail-load reset cause.

Reused final worker evidence from
`/root/code/agent_flow/.aflow/runs/20260912t014630z-4d8f1499/turns/turn-001/transport.stdout`
and completed `result.json` (the prompt's worktree-relative receipt is absent).
Final post-edit bounded command:
`npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx src/components/GlobalSettings.test.tsx`
passed 187/187 at 01:54:50 UTC. Focused effort set passed 4 cases.
Independent temporary `ReadinessReviewProbe.test.tsx` used the existing follow-up
case with the pre-response edit checks; its exact-name filtered run passed
1 case at 01:57:58 UTC. Probe removed. Cumulative `git diff --check` passed.
No full suites, browser/build reruns, portable-test host paths or live writes.

Approval finalization includes this already-tracked reviewer record in the one
unpublished accumulated commit after the unchanged base. Preserve all other
reviewed blobs; DEVLOG already has one handoff entry. No fix plan is created.
Keep the ignored original plan for engine finalization and record approved SHA
there after committing. Verify one final commit and clean tracked Git state.
Publication, exact-SHA CI and live activation remain downstream delivery gates.

No material findings

---

# Stable family preview controls — cumulative review approved

Reviewed unchanged base `6e379a9c15b8d1a9c4d0760debf1e946f25b58d6`
through `d7d3633a3eec765e9372e9bda6a9ce0a8a96b86f`: 1 new / 1 total
commit, `cp1 v01`, covering the entire original handoff. No previous findings
or fix overlays apply to this handoff. Earlier records concern other handoffs.

The shared hosted/fallback status retains a constant indicator footprint,
dirty wording, reduced-motion feedback and the full accessible pending message.
Actionable errors and draft/save ownership are preserved. The new held-response
regression checks pointer geometry, successful stage selection and distinct
child/Base reviewer declarations. The existing family journey is unchanged.

Verification:

- Reviewer reran the exact focused GlobalSettings preview/resize selection:
  15 passed, 60 skipped, including both added pending/error tests.
- Retained worker command receipts confirm the production build passed;
  Chromium and WebKit each passed 3 controlled pointer cases and all 7 family
  journey viewports. Receipts are in run `20260912t022113z-83b39f89`, turn 001,
  under the registered parent checkout's `.aflow` directory; the prompt's
  worktree-relative receipt is absent.
- Inspected retained desktop pending/settled and reduced-motion phone images.
  All six controlled geometry JSON artifacts show identical pointerdown and
  settled bounds, successful child selection, `review_child` on the child and
  `review_final` on Base. External evidence directories are
  `family-preview-cp1-{chromium,webkit}-final-*` and
  `family-preview-cp1-journey-{chromium,webkit}-*`.
- Cumulative `git diff --check` passed. Only the five planned implementation
  files changed before this reviewer record. Private artifact paths are passed
  through pytest options, not embedded in portable test code.

Approved for one final handoff commit at the unchanged base, preserving reviewed
implementation blobs and including this tracked record. The original private
plan stays in place for engine finalization and records the final approved SHA.
No fix plan or DEVLOG compaction is needed. Publication, exact-SHA CI and live
activation remain downstream gates; physical mobile checks are unverified.

No material findings


---

# Restart action readiness — cumulative review v01: approved

Unchanged base `378278a8e3974cc6eda9bb8c7411a585ca305a19` → reviewed HEAD
`4f237dbe93cfb8fd51f20ccf7114f5046413aeb4`: 1 new / 1 total commit,
`cp1 v01`, covering the entire original checkpoint and cumulative implementation.
Original and active plan are restart-action-readiness-20260912.md. No previous
findings or follow-up overlays apply to this handoff.

Reviewed the complete two-file diff, every readiness caller, successor recovery
and negative assertions, and production preflight/confirmation rendering.
The helper requires the exact connected accessible action and its enabled state;
all seven successor paths select successor explicitly, ordinary launches select
Start, and existing exact preflight request assertions remain. Source ownership,
stop-before-start ordering, inactivity, lineage and idempotency checks survive.
No production changes, relaxed assertions, sleeps or timeout inflation.
The retained CI log confirms the missing confirmation at the original line 1749;
this is a test-readiness correction, not a reproduced production restart defect
or proof of the precise asynchronous scheduling that caused CI failure.

Reused completed worker result and raw command receipt from
`/root/code/agent_flow/.aflow/runs/20260912t030941z-5b40bc70/turns/turn-001/`.
The prompt's worktree-relative receipt is absent; the parent controller receipt
is available. After resolving initially absent vitest dependencies, the final
`npm --prefix apps/aflow_app/web test -- --run src/components/RunDashboard.test.tsx`
passed 114/114 at 03:13:24 UTC (exit 0). Independent cumulative
`git diff --check 378278a8e3974cc6eda9bb8c7411a585ca305a19 HEAD` passed.
No repeated full suite, build/browser run, or shared runtime change.

Approval finalization includes this tracked review record in one unpublished
handoff commit above the unchanged base, preserving both reviewed blobs.
DEVLOG has one handoff entry; no compaction or fix plan is needed. Keep the
ignored original plan in place and record the final approved SHA there after
committing. Verify exactly one commit and clean tracked state. Publication,
exact-SHA CI and live activation remain downstream engine delivery gates.

No material findings


---

# Worker receipt exit race — cumulative review v01: approved

Unchanged Pre-Handoff Base HEAD: `e77470fb59fc6aa990a351a0f0e126e085764d72`.
Reviewed implementation HEAD: `bae3a2638864dbdca90e97efcfa345c4853a5584`.
Range: 1 new / 1 total commit, `cp1 v01`, covering the full original plan
and all three changed files. Original and active plan are
`worker-receipt-exit-race-20260912.md`; no previous findings or fix overlays apply.

The same Popen is polled before signalling and after a signalling OSError.
Terminal observations allow the existing wait/pipe close and nonce-bound
error/exit receipts; a live child signal failure propagates without waiting
or fabricating terminal evidence. Receipt failure still returns 1 when the
child exits 0. Owned-group targeting and receipt schema remain unchanged.
The three controlled regressions stub every signal and cover exit-before,
exit-during PermissionError, and live PermissionError. Original real-child
cases remain intact. No material defects admitted under material-code-review.

Verification: reused final worker command evidence from
`/root/code/agent_flow/.aflow/runs/20260912t032724z-45174513/turns/turn-001/transport.stdout`
and its completed result.json (the worktree-relative receipt is absent).
`uv run pytest -q tests/test_worker_diagnostics.py --tb=short` passed all
17 tests on final content (8.97 seconds, exit 0). Worker Ruff passed after
removing the unused import. Reviewer independently ran
`uv run ruff check aflow/ui_cli.py tests/test_worker_diagnostics.py`,
`git diff --check`, and cumulative `git diff --check` successfully.
No full suites, shared-runtime changes, or live services were needed.
Exact historical macOS timing remains unproven; macOS CI is a separate gate.

Approval finalization includes this tracked reviewer record in one final
handoff commit above the unchanged base and preserves all three reviewed
implementation blobs. One DEVLOG entry requires no compaction. No fix plan
is created; the ignored original plan stays in place for engine finalization
and records the final approved SHA outside the commit. Publication, exact-SHA
CI and live activation remain downstream controller delivery gates.

No material findings

---

# Dashboard selection ownership — cumulative review v01: approved

Unchanged Pre-Handoff Base HEAD: `e77470fb59fc6aa990a351a0f0e126e085764d72`.
Reviewed HEAD: `8d771794f5dc086047ba34286b5608af5ddb1c36`.
Coverage: 2 new / 2 total commits, cp1 v01 (`d776ed7d`) and cp2 v01
(`8d771794`). Original and active plan: dashboard-selection-ownership-20260912.md.
No previous findings or follow-up overlays apply to this handoff.

Reviewed the full original-base diff and surrounding restart lifecycle,
checkpoint ownership/reconciliation, run-switch coverage, and diagnostics Refresh
assertions. The restart change removes only the competing defaults owner;
explicit cancellation, completion, rejection and frozen-successor owners remain.
Checkpoint reconciliation reads the latest functional state and keeps selection
and notice coherent without updater side effects. Same-run valid selection is
preserved; exact run changes, removed entries and Current checkpoint retain their
specified behavior. No material findings pass the admission gate.

Reused completed worker results and raw command receipts in the registered
parent checkout `/root/code/agent_flow/.aflow/runs/20260912t033545z-31c5cbd9/turns/`
(turns 001 and 002); the prompt's worktree-relative receipt is absent.
From apps/aflow_app/web, retained successful commands were:

- `./node_modules/.bin/vitest run src/components/RunDashboard.test.tsx -t 'restart|successor'`: 10 passed after generating the missing changelog artifact.
- `./node_modules/.bin/vitest run src/components/CheckpointHistory.test.tsx`: 15 passed in the final post-edit run.
- `./node_modules/.bin/vitest run src/components/RunDashboard.test.tsx -t 'inspects selected checkpoint|late checkpoint'`: 2 passed in the final post-edit run.
- `npm run build`: final TypeScript/Vite build passed, exit 0.

Reviewer cumulative `git diff --check e77470fb HEAD` passed. No redundant test
or build reruns, full suites, shared runtime changes or live writes. The CP4
regression uses controlled pure reconciliation plus component refresh coverage;
it does not establish historical CI network timing. No visual layout changes.

Approval finalization compacts the two DEVLOG entries and includes this tracked
review record in exactly one unpublished handoff commit after the unchanged base.
Preserve all reviewed implementation/test blobs; no fix plan exists or is created.
Keep the ignored original plan in place and record the final approved SHA there
without creating another commit. Verify one accumulated commit and clean tracked
state. Coordinator owns combined integration with independent worker-receipt
cleanup, publication, exact-SHA CI, and live activation/browser acceptance;
none are claimed by this local approval.

No material findings

---

# Settings clean-preview ownership — cumulative review v01: rejected

Base `6bf1f0decb91a1dce4fbcda40a2d8a065719d307` through
HEAD `ae80e5f5c0b7ea7c62c9670f7db7e103376c15da`: 1 new / 1 total
commit, cp1 v01. Reviewed all five changed files against the original plan.
No prior findings or fix overlays apply to this handoff.

R1 (P2, high confidence): settingsDraft.test.ts:89-104 never executes the
released clean reconciliation; it tests only the semantic matcher. The component
custom-effort case at GlobalSettings.test.tsx:503-514 adds readiness and an exact
save assertion but no deferred clean release. Removing both production guards
would leave the helper assertions unaffected. The diagnosed canonical-draft
loss therefore lacks the explicitly required deterministic regression. Add
bounded ordering around the actual production reconciliation (or a minimal
shared pure reconciliation transition plus integration), verify canonical effort,
Save, exact patch, preview ownership and genuine reversion. No other material
finding was admitted; production guards match the planned repair.

Reviewer commands from apps/aflow_app/web:
- ./node_modules/.bin/vitest run src/components/GlobalSettings.test.tsx -t 'custom effort|typed custom effort|initial settings load|stale and reverted previews|unchanged label blur|failed preview unavailable|drops reverted edits': 10 passed.
- ./node_modules/.bin/vitest run src/settingsDraft.test.ts: 9 passed.

Retained worker transport.stdout in the registered parent checkout at
/root/code/agent_flow/.aflow/runs/20260912t035523z-9c1e858a/turns/turn-001
records successful TypeScript/Vite build (exit 0); result.json confirms completed
cp1 v01. Worktree-relative receipt is absent. Cumulative git diff --check passed.
No full suite or live writes. Original diagnosis proves pre-fix timing mechanism,
not deterministic post-fix acceptance.

Single private fix overlay:
plans/in-progress/settings-clean-preview-ownership-20260912-cp01-v01.md.
Original-plan review tracking updated; no implementation edits, squash, history
rewrite, publication or activation. R1 must be resolved before another full
cumulative review. Coordinator owns combined delivery and live acceptance.

Material fixes required


---

# Settings clean-preview ownership — cumulative review v02: approved

Unchanged Pre-Handoff Base HEAD: `6bf1f0decb91a1dce4fbcda40a2d8a065719d307`.
Reviewed implementation HEAD: `b026603c63cba8d1bc94f03040eb031ccab33f8a`.
Coverage: 1 new / 2 total commits, cp1 v01 (`ae80e5f5`) and cp1 v02
(`b026603c`), including the original plan and active cp01-v01 fix overlay.
Read the prior finding first, then reviewed the full original-base range,
all five changed files, surrounding draft/preview ownership and affected tests.

R1 resolved: the test-local effect scheduler captures the actual production
clean callback and releases it after enabled-fieldset custom typing without
Enter/blur. Pending state survives; the subsequent rejected preview remains
owned and visible, Save remains eligible and sends exactly one effort-only
upsert_profile with expected_revision and no server-settings write. This
establishes canonical retention beyond the Combobox's local visible text.
The shared pure transition also preserves a newer functional-updater draft
independently and permits genuine baseline restoration. Existing reversion,
late-preview, unchanged-blur and failed-preview coverage remains in place.
The synchronous coordinator guard returns before invalidation/state writes;
the pure updater guard retains newer declarations without side effects.
No additional material findings passed the admission gate.

Verification reused final retained worker command receipts in
`/root/code/agent_flow/.aflow/runs/20260912t035523z-9c1e858a/turns/turn-003/transport.stdout`
and the completed result.json; the supplied worktree-relative receipt is absent.
From apps/aflow_app/web, final commands at 04:16 UTC passed:

- `./node_modules/.bin/vitest run src/components/GlobalSettings.test.tsx -t 'custom effort|typed custom effort|initial settings load|stale and reverted previews|unchanged label blur|failed preview unavailable|drops reverted edits'`: 11 passed.
- `./node_modules/.bin/vitest run src/settingsDraft.test.ts`: 9 passed.
- `npm run build`: TypeScript/Vite passed, exit 0.

Retained controlled missing-guard evidence at
`/root/code/evidence/aflow-dogfood-20260909/settings-clean-preview-ownership-20260912-cp01-v01-prefixed-regression.log`
shows the same actual-callback regression failing immediately after release:
pending became settled and Save disabled. Final production guards are restored.
Exact historical CI timing remains inferred. Reviewer cumulative
`git diff --check 6bf1f0de HEAD` passed. No full suites or live writes.

Approval includes both intentional tracked reviewer records in the single
unpublished handoff commit, preserves all five reviewed implementation blobs,
and removes the superseded private fix overlay. DEVLOG already has one handoff
entry. No new fix plan or forced addition of private artifacts. Record final
approved SHA in the ignored original plan after committing; leave it in place
for engine finalization. Verify one final commit and clean tracked state.
Coordinator owns combined delivery, publication, exact-SHA CI and live acceptance;
this local approval does not claim those gates or alter integrated dashboard
ownership and worker cleanup repairs.

No material findings


---

# Phone hit snapshot delivery — cumulative review v01: approved

Unchanged Pre-Handoff Base HEAD: `da593a810143b34d04693a33b5e097c315ce7c50`.
Reviewed implementation HEAD: `0f769342d5431aa0e67d4ec0316c98d542f55eaa`.
Coverage: 1 new / 1 total commit, cp1 v01, entire original plan and full
actual two-file implementation. Original and active plan are
phone-hit-snapshot-delivery-20260912.md. No prior findings or fix overlays.

Reviewed the complete diff, helper callers, fixture and real DOM regressions,
server instructions and UI guidelines. The exact handle survives trial-click
and the final connected check, instant scroll, rect/viewport measurement and
center hit observation occur in one synchronous browser evaluation. Positive
size, original viewport bounds and element-or-descendant identity remain strict.
The held real context response moves the same target offscreen after trial;
the repaired snapshot restores reachability. Replacement fails on the detached
original and the covered control fails normal actionability. No product changes,
geometry stubs, relaxed assertions, added timeouts or retries in the helper.
No material finding meets the material-code-review admission gate.

Reused completed worker result and raw command receipts from
`/root/code/agent_flow/.aflow/runs/20260912t044056z-332941d5/turns/turn-001/`;
the prompt's worktree-relative receipt is absent. Final post-probe commands ran
from apps/aflow_app/server with fresh mktemp directories passed as --basetemp:

- `AFLOW_TEST_BROWSER=chromium uv run pytest -q 'tests/test_responsive_browser.py::test_responsive_route_matrix[phone-portrait]' --basetemp="$test_tmp_dir" --tb=short`: 1 passed, 8.92s.
- Same exact route with `AFLOW_TEST_BROWSER=webkit`: 1 passed, 10.27s.
- `AFLOW_TEST_BROWSER=chromium uv run pytest -q tests/test_responsive_browser.py::test_responsive_action_hit_test_survives_late_context_growth tests/test_responsive_browser.py::test_responsive_action_hit_test_rejects_detached_and_covered_targets --basetemp="$test_tmp_dir" --tb=short`: 2 passed, 35.11s.
- Same two helper nodes with `AFLOW_TEST_BROWSER=webkit`: 2 passed, 35.81s.

The reversible original-helper probe failed at y=744.34375 against height 568;
final implementation was restored before those successful runs. Earlier setup
failures were addressed by the allowed missing-asset build and correcting held
route fulfillment order. Exact historical CI scheduling remains inferred.
Reviewer cumulative `git diff --check da593a810143b34d04693a33b5e097c315ce7c50 HEAD`
passed. Valid retained evidence reused; no full matrix or redundant local runs.

Approval includes this tracked reviewer record in one unpublished handoff commit
above the unchanged base, preserving both reviewed implementation blobs. DEVLOG
has one relevant entry; no compaction or fix plan is required. Keep the ignored
original plan in place for engine finalization and record the final approved SHA
there outside its own commit. Verify one final commit and clean tracked state.
Coordinator owns publication, exact-SHA CI, deployment/live check and final stop
report for the existing settings delivery. This review makes no live claims.

No material findings


---

# Held context gate ordering — cumulative review v01: approved

Unchanged Pre-Handoff Base HEAD: `dc99aa4f93a741a63edabae32e80bb3090109976`.
Reviewed implementation HEAD: `d3b109ecce4979a7819f16ab959d6647cead1e37`.
Coverage: 1 new / 1 total commit, cp1 v01, the complete original plan and
actual two-file diff. Original and active plan are
held-context-gate-ordering-20260912.md. No prior findings or fix overlays apply;
older entries in this record concern other handoffs.

The callback records the exact route before page acknowledgement without an
upstream fetch; the bounded acknowledgement wait precedes the trial helper.
Continuation follows the original geometry, identity and nonempty-route checks,
then the unchanged same-target offscreen and atomic hit assertions run against
real fixture context. Cleanup continues held routes before unroute and catches
teardown Playwright errors. The option exception requires both the exact
combobox-listbox class token and listbox role, matching the bounded production
Combobox and UI guideline. No obsolete combobox-options caller exists in web
source. Product code, atomic helper and other scroller rules are unchanged.
No material finding passes the material-code-review admission gate.

Verification reused completed worker result and raw command receipts from
`/root/code/agent_flow/.aflow/runs/20260912t052002z-6fcd9cfe/turns/turn-001/`;
the prompt's worktree-relative receipt is absent. From apps/aflow_app/server:

- `AFLOW_TEST_BROWSER=webkit uv run pytest -q tests/test_responsive_browser.py::test_responsive_action_hit_test_survives_late_context_growth --tb=short --basetemp "$test_temp_dir"`: final cleanup version, 1 passed in 4.38s.
- Same exact growth case with `AFLOW_TEST_BROWSER=chromium`: final cleanup version, 1 passed in 3.76s.
- `AFLOW_TEST_BROWSER=webkit uv run pytest -q 'tests/test_responsive_browser.py::test_responsive_team_family_journey[desktop]' --tb=short --basetemp "$test_temp_dir"`: 1 passed in 17.60s with the final scroller rule; the subsequent cleanup-only edit does not affect this journey.

Each test invocation used fresh temporary state through --basetemp; no private
host paths were added to portable tests. Initial missing-asset/login failures
and the intermediate unroute-before-continue error were corrected before final
passing receipts. Retained unaffected helper/negative and route evidence remains
in the preceding approved handoff. Reviewer cumulative `git diff --check` passed.
No full matrix, redundant browser runs or builds were repeated during review.

Approval includes this tracked reviewer record in one unpublished final handoff
commit above the unchanged base, preserving both reviewed implementation blobs.
DEVLOG has one relevant entry; no compaction or fix plan is needed. Keep the
ignored original plan in place for engine finalization and record the approved
SHA there outside the commit. Verify one final commit and clean tracked state.
Coordinator owns publication, exact-SHA CI, live activation and the owner-required
stop after existing settings delivery. This bounded review stops here and makes
no downstream delivery claims.

No material findings

---

# Startup gate failure evidence — cumulative review approved, 2026-09-12

Base: `3838406443a479859c72404a58e3de6b2a54005d` (unchanged Pre-Handoff Base HEAD).
Reviewed HEAD: `055ed86665d0c4648fb7c4ca56b524a03747d3a8`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all original checkpoint requirements
and the complete two-file implementation diff. No previous findings or fix
overlays apply to this handoff; unrelated historical records above are preserved.

Applied material-code-review admission, exclusions and proportionate-fix discipline.
No material findings. One failure-only DOM evaluation uses actual component
selectors, bounded text and message counts, selected inputs, preflight and
confirmation state, and Start disabled state. Diagnostic exceptions are guarded;
the original exception is re-raised. Original helper actions match after
normalizing embedded JavaScript indentation; timeout arguments and the full
launch/rejection/corrected retry journey remain unchanged. No product fix or
historical root cause is claimed.

Retained evidence independently inspected in
`/root/code/agent_flow/.aflow/runs/20260912t105431z-9c4cfc08/turns/turn-001/transport.stdout`
and its completed `result.json` (the prompt-relative artifact is in the parent
repository):
- Exact corrected-plan Chromium pytest node passed: 1 passed in 6.58s, using
  `--tb=short --basetemp /tmp/aflow-startup-gate-evidence.TMHJKp` with
  `uv run --project apps/aflow_app/server pytest -q`.
- External real-Chromium probe passed: forced enabled assertion preserved,
  requested JSON fields verified, 549-byte output below 8192 bytes.
- Production asset build passed after installing locked dependencies.
- Reviewer cumulative `git diff --check 3838406443a479859c72404a58e3de6b2a54005d HEAD`
  passed. Valid retained tests were reused; no full suites repeated.

Approve and finalize as one unpublished handoff commit including this tracked
record. Preserve reviewed test and DEVLOG blobs; the DEVLOG already has one
handoff entry. No stale fix plans exist and no empty fix plan is created. Leave
the ignored original plan in place for the engine and record the final approved
SHA there after commit creation. Finalization checks one commit after the base,
identical implementation blobs, and clean tracked Git state. No private artifacts
are force-added.

Publication, exact-SHA CI and live activation remain separate coordinator gates.

No material findings
## Family wizard tab retention — cumulative review v01: approved

Base: `524b002ad9d6ad68410c1675711f661f025c463f` (unchanged).
Reviewed HEAD: `0608c6435d763abe024762433e642456f20c0f84`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all five changed files and
the entire original/active family-wizard-tab-retention-20260912 plan.
No previous findings or fix overlays apply to this handoff.

Applied material-code-review admission, exclusions and proportionate-fix rules.
The single existing owner now occupies a stable guided-fragment position beside
Skills, retaining draft, preview, reset, active and dirty callbacks inside the
busy-disabled fieldset. Native hidden semantics and the existing hidden CSS
exclude inactive controls from layout and accessibility. Advanced behavior and
the shared save owner remain intact. Component coverage checks modified stages,
exact name/ID/source, tab retention, dirty state, no writes and confirmed reload.
The existing browser journey retains its subsequent identity/save/layout checks.

Reused final successful worker receipts from the registered parent checkout:
`/root/code/agent_flow/.aflow/runs/20260912t115928z-541e34c2/turns/turn-001/transport.stdout`
and completed result.json; the worktree-relative receipt is absent.

- `npm --prefix apps/aflow_app/web test -- --run src/components/GlobalSettings.test.tsx -t 'wizard'`: 1 passed, 76 skipped.
- `npm --prefix apps/aflow_app/web run build`: TypeScript/Vite passed.
- `AFLOW_TEST_BROWSER=chromium uv run --frozen --project apps/aflow_app/server pytest -q 'apps/aflow_app/server/tests/test_responsive_browser.py::test_responsive_team_family_journey[desktop]' --tb=short --basetemp "$family_cp1_chromium_tmp"`: 1 passed, 16.58s.
- Corresponding WebKit `phone-tall` (390×844) node with fresh external
  `--basetemp "$family_cp1_webkit_tmp"`: 1 passed, 19.03s.
- Pre-move regression failed at the missing retained wizard lookup. Intermediate
  assertions were corrected to inspect Base inputs on the Base step; final tests
  retain stage and exact Base/source checks.
- Reviewer inspected retained Chromium light and WebKit dark family screenshots
  under `/tmp/aflow-family-cp1-chromium.0QvOTd/` and
  `/tmp/aflow-family-cp1-webkit.RobSGX/`; cumulative diff whitespace check passed.

No full suites or live writes. One DEVLOG entry needs no compaction. Approval
includes this tracked record in one unpublished final commit above the original
base, preserving all five reviewed blobs. No fix plan is needed. Leave the
ignored original plan in place, recording the final SHA after commit creation;
verify one accumulated commit and clean tracked state. Publication, exact-SHA CI
and live acceptance remain separate coordinator gates.

No material findings

---

## Run history readable evidence — cumulative review v01: rejected

Base: `304a39fd24b2d330d2533d041f3eb2776504086c`. HEAD: `88c18a31875fa8d8d068c006066ca19faef2d3f7`. Four new / four total commits: cp1 v01 through cp4 v01. Original and active plans are identical; no earlier findings exist. Full accumulated implementation and all 16 acceptance mappings reviewed under material-code-review admission and proportionate-fix rules.

- F1, P1/high confidence, `aflow/control_plane/run_progress.py:4581-4584` (cp2 `fa3a656e`): counting now consumes approval mappings after deduplication whose fallback omits checkpoint/decision identity. Four distinct ordinal approvals collapse to one. Same disposable input returns four at base and one at HEAD. Correct approval-specific deduplication and event identity without losing conflicting-scope rejection; restore browser expectations changed to accept the regression.
- F2, P2/high confidence, `apps/aflow_app/web/src/runPresentation.ts:71-73` (cp1 `1e7af2ea`): null max_turns is rendered as no turn limit, although legacy repository projection returns null for absent evidence. Use unknown ceiling wording and retain known usage/ceiling behavior.

Reviewer verification: 56 focused web tests passed across RunProgress, CheckpointHistory, runPresentation and SidebarEditorLayout; 11 projection tests passed (28 deselected); TypeScript/Vite build passed; cumulative whitespace check passed. Differential base/HEAD reproduction confirms F1. CP4 completed parent-repository transport receipts record seven Chromium and seven WebKit passes; their approval assertions encoded the regression and must be corrected. Full suites were not run.

Exactly one non-checkpoint fix overlay written: `plans/in-progress/run-history-readable-evidence-20260912-cp01-v01.md`. Original tracking updated, base unchanged. No squash, implementation edits, publication or service changes. Coordinator retains serialized integration, exact-SHA CI and live activation.

Material fixes required

---

## Run history readable evidence — cumulative review v02: rejected

Unchanged base: `304a39fd24b2d330d2533d041f3eb2776504086c`. Reviewed HEAD: `e8ffb46f47f7d9f86fb27c12173a6af0eca25f5b`. One new / five total commits: cp1 v01 `1e7af2ea`, cp2 v01 `fa3a656e`, cp3 v01 `10741c81`, cp4 v01 `88c18a31`, cp1 v02 `e8ffb46f`. Read the active overlay/prior findings first, verified F1/F2, then reviewed the full base-through-HEAD implementation and all 16 original mappings.

F1 resolved: approval-specific identity retains checkpoint/decision dimensions, four distinct approvals/events and duplicate suppression, with contradictory scopes still unassigned. Browser expectations restored to 4/11 and 11/11. F2 resolved: absent/invalid turn ceilings are unknown, retaining known usage and ceilings. No previous finding remains unresolved.

F3, P2/high confidence, `apps/aflow_app/web/src/components/CheckpointHistory.tsx:220-222`, introduced by cp3 v01 `10741c81`: fallback timing identity includes event kind, splitting review and approval evidence for the same source-run/role/turn without an invocation ID. The committed screenshot-shaped fixture projects one turn-2 review plus its approval, each spanning 146 seconds. Calling actual buildTimeBreakdown on that canonical output gives two reviewer rows and a 292-second recorded total instead of 146 seconds; union coverage remains 373 seconds. Deduplicate timing by proven invocation identity across those evidence kinds, preserving separate timeline evidence and source-run/role separation. This fails original mapping 16 and meets the material finding admission gate.

Retained final worker command receipts inspected under `/root/code/agent_flow/.aflow/runs/20260912t123805z-e00bbfa8/turns/turn-006/`: 12 canonical tests, 48 focused web tests, TypeScript/Vite build, seven browser cases collected, Chromium desktop-short/desktop 2 passed, WebKit phone 1 passed. Inspected retained Chromium dark completed and WebKit light history screenshots. Reviewer used the existing fixture and canonical projector with disposable state, then transpiled the unchanged TS implementation in memory to call buildTimeBreakdown; output is two distinct kind-based keys for reviewer turn 2 and total 292. Disposable projection: `/tmp/aflow-review-timing-tfmdcd0y/projection.json`. Cumulative git diff --check passed; no full suites or live actions.

Created exactly one self-contained non-checkpoint overlay at `plans/in-progress/run-history-readable-evidence-20260912-cp01-v02.md`, removed superseded cp01-v01, and updated original tracking without changing the base. No implementation edits, DEVLOG compaction, squash, publication or service changes. Existing reviewer records preserved. Coordinator retains serialized integration, exact-SHA CI and live activation.

Material fixes required


---

## Run history readable evidence — cumulative review v03: approved

Unchanged Pre-Handoff Base HEAD: `304a39fd24b2d330d2533d041f3eb2776504086c`.
Reviewed implementation HEAD: `19e47db3e2bea6d70566cbdd07957ee569a02b64`.
One new / six total commits: cp1 v01 `1e7af2ea`, cp2 v01 `fa3a656e`,
cp3 v01 `10741c81`, cp4 v01 `88c18a31`, cp1 v02 `e8ffb46f`, cp1 v03
`19e47db3`. Read the active cp01-v02 overlay and prior findings first, then
reviewed the complete unchanged-base range, all 21 changed files and all four
original checkpoints under the material finding admission/exclusion rules.

F1 resolved: approval-specific checkpoint/decision identity preserves four
distinct approvals and events, duplicate suppression and conflicting-scope
rejection. F2 resolved: absent/invalid turn ceilings remain unknown. F3 resolved:
the legacy source-run/role/turn timing key merges review and approval evidence
without merging separate source runs or roles; explicit invocation IDs and both
timeline events remain intact. The canonical screenshot fixture now renders one
146-second reviewer invocation and unchanged 373-second union coverage. No prior
finding remains unresolved and no further material finding passes admission.

All 16 original mappings retained: 1 approval wording; 2 terminal fields;
3 partial-history disclosure; 4 status summary; 5 row density; 6 full identity
access/copy; 7 terminal current-action removal; 8 labelled state strip;
9 explicit selection and refresh retention; 10 independent delivery stages;
11 durable review association; 12 omitted/unassigned distinction; 13 compact
expandable timeline; 14 truthful turn budget; 15 machine dates/local timestamps;
16 deduplicated duration and partial/overlap handling. No new per-row requests,
state mutation, service changes or competing schema were introduced.

Verification: reused final raw command receipts and completed result.json under
`/root/code/agent_flow/.aflow/runs/20260912t123805z-e00bbfa8/turns/turn-008/`:

- Focused CheckpointHistory, RunProgress and runPresentation tests: 49 passed.
- TypeScript/Vite production build passed; seven browser cases collected.
- Canonical visual journey `[desktop]`, isolated temporary artifacts and
  --basetemp: Chromium 1 passed (20.50s), WebKit 1 passed (26.94s).
- Reviewer inspected retained Chromium light and WebKit dark completed-run
  screenshots in `/tmp/aflow-timing-fix-chromium-artifacts.J6iT2I/` and
  `/tmp/aflow-timing-fix-webkit-artifacts.XJHrgY/`, including expanded timing.
- Reused unchanged canonical evidence from turn-006: 12 passed for
  `review or scope or truncat or inherited or preserves_distinct_approval_records`;
  corrected Chromium desktop-short/desktop 2 passed and WebKit phone 1 passed.
- Reviewer ran `uv run --frozen --project apps/aflow_app/server pytest -q`
  on `test_control_plane_api.py::test_transport_models_match_canonical_control_plane_models`
  and `::test_progress_transport_models_keep_optional_status_and_full_detail_shapes`
  with external --basetemp: 2 passed. Cumulative `git diff --check` passed.

Approval finalization consolidates this handoff's DEVLOG entries, includes all
intentional tracked reviewer records in one unpublished commit, removes the
resolved private fix overlay, and preserves the 20 reviewed implementation/test
blobs. No private plans/evidence are force-added. Verify exactly one commit above
the unchanged base and clean tracked state. Record the final approved SHA only
in ignored original-plan tracking after committing; keep that plan in place for
engine finalization. No empty follow-up plan is created.

Coordinator owns serialized integration, publication, exact-SHA CI and live
activation, preserving concurrent ui-data-latency-discovery work. These delivery
gates and physical-mobile acceptance are not claimed by this local review.


---

## UI data latency discovery — cumulative review v01 rejected, 2026-09-12

Base: `304a39fd24b2d330d2533d041f3eb2776504086c`.
HEAD: `4bef5c55f354cfe0224610843d8675df96ae94b1`.
Coverage: 2 new / 2 total commits (`7b3f9000`, cp1 v01; `4bef5c55`, cp2 v01),
full original plan and full accumulated discovery implementation. No prior
findings or fix overlays apply. Product-source diff is empty.

Applied material-code-review admission gate, exclusions and proportionate fixes.
Four admitted findings, all high confidence:

- F1 / P1: `profile_ui_data_load.py:972-975,1060-1065` accepts empty checkpoint headings and retained pre-refresh detail. A Chromium probe returned true on zero entries and Refresh success while pending (108.41 ms); the retained first warm Refresh has all new fetches unresolved at its claimed 96.013 ms completion. Require actual records and post-trigger accepted data.
- F2 / P1: improvement plan lines 136-139,256-261 and analyzer lines 819-827 mix the only retained browser fixture (12 runs/6 events) with 100/1000-run, 8/80-event acceptance. The 27,948-byte smoke bound is applied to a 100-row list whose canonical profile payload is 232,448 bytes. Missing scale/live coverage cannot be replaced by fixture definitions or service profiles; report values are hardcoded. Correct coverage, identities and same-fixture targets.
- F3 / P2: `profile_control_plane_reads.py:294-297` adds 300 calls each to three nested projection functions as 900 projections. Count a single canonical entry point so the future one-per-row gate is meaningful.
- F4 / P2: `profile_control_plane_reads.py:352-361` drops each raw timing sample while retaining only medians/ranges. Preserve sanitized samples and derive report statistics from them.

One self-contained non-checkpoint fix plan written to
`plans/in-progress/ui-data-latency-discovery-20260912-cp01-v01.md`.
No optimization implemented, no squash or history rewrite, no DEVLOG compaction.
Original plan remains for the engine with this rejection recorded. The tracked
reviewer record is intentional bookkeeping, not unrelated worktree noise.

Verification: parsed retained baseline (46 error-free smoke samples only),
inspected CP2 per-function counts and raw-sample absence, inspected both worker
result receipts under the registered parent repository run
`20260912t123823z-4f0a10bb`, and ran bounded Chromium DOM probes using
`uv run --frozen --project apps/aflow_app/server python` with the runner's actual
route predicate and capture_refresh function. Both negative cases reproduce.
Cumulative `git diff --check` passes. Reused valid retained evidence; no full
suites, live captures, shared installation changes or service restarts.

Separate improvement plan (not ready for dispatch):
`plans/in-progress/ui-data-latency-improvements-20260912.md`. Source mapping:
run-list finding to `RunRepository.list_history/get_run_status/_with_progress`,
`AflowDaemon.run_status`, `ControlPlaneService.list_runs/_status` and progress
projection; proposed event finding to persistence `read_events`, repository
`tail_events`, daemon `poll_events`, service `events`, HTTP event models/route
and `RunDashboard.loadSelectedRun`. Revalidate the event proposal's critical-path
support in the fix pass. Coordinator owns serialized integration/publication/CI.

Material fixes required


## UI data-latency discovery — cumulative review v02 rejected

- Unchanged base: `304a39fd24b2d330d2533d041f3eb2776504086c`.
- Reviewed HEAD: `951a3d26a4ca462ff5251ff6153abc3f65b94596`.
- 2 new / 4 total commits: cp1 v01 `7b3f9000`, cp2 v01 `4bef5c55`,
  cp1 v02 tooling `1d9e7321` and evidence `951a3d26`.
- Both original checkpoints and both follow-ups were considered from the
  unchanged base; prior-finding verification prevents cumulative approval.
  No squash, implementation edits, publication or live activation.

F1 remains unresolved (P1/high confidence), at
`scripts/perf/profile_ui_data_load.py:1064-1071`: the selected-summary predicate
accepts any visible status once direct-status JSON finishes, although the real
owner waits for events before accepting that status. A real isolated Chromium
probe returned `failed` in the synthetic direct-status response, held events,
and observed predicate acceptance with `Completed` still rendered. One event
request remained held. This corrupts accepted-summary and selection timings.
Use accepted generation/data readiness and real held-response tests; retain
empty-history rejection and verify Refresh's accepted-render boundary too.

F5 is new (P2/high confidence), at
`scripts/perf/analyze_ui_data_latency.py:221-222,280-295`: Refresh and in-app
useful durations start at the action, while fetch completion timestamps start
at document creation. The retained first small-short-history Refresh reports
15510.862 ms useful versus 22981.7 ms required completion, generating a false
-7470.838 ms gap; its first in-app cycle produces -2319.026 ms. Normalize both
milestones and request comparisons to a recorded common browser clock origin.

Previous dispositions: F2's coverage, stable selected fixture, in-app cycles,
fixture-specific bounds and honest live/event gates are repaired; dependent
browser timings still need F1/F5 correction. F3 canonical projection counting
and F4 raw timing retention are resolved. No need to repeat their profiles.

Verification: focused `uv run --frozen --project apps/aflow_app/server pytest
-q scripts/perf/test_ui_data_latency.py --basetemp
/tmp/aflow-latency-review-v02-tests` passed (8 tests); Ruff and cumulative
`git diff --check 304a39f HEAD` passed. Inspected 184 retained samples with zero
reported errors: per fixture, 30 warm, 6 fresh-process, 10 in-app cycles.
Independently recomputed core medians for all 28 five-sample profile records.
Product paths have an empty base-to-HEAD diff. Read retained worker turn-004
receipt. Reviewer real-browser negative probe result is external at
`/tmp/aflow-latency-review-v02-summary-probe.json`; an initial probe diagnostic
had a strict-locator error, corrected by selecting the actual first status
pill. No full suite, live capture, shared-tool change or service restart.

One focused overlay replaces v01:
`plans/in-progress/ui-data-latency-discovery-20260912-cp01-v02.md`.
Keep the original plan in place. Preserve previous reviewer records and
resolved evidence. History remains unchanged.

Separate improvement handoff (not approved for dispatch):
`plans/in-progress/ui-data-latency-improvements-20260912.md`.
List mapping: `RunRepository.list_history/get_run_status/_with_progress`,
`AflowDaemon.run_status`, `ControlPlaneService.list_runs/_status`, progress
projection. Event gate mapping: persistence `read_events`, repository
`tail_events`, daemon `poll_events`, service `events`, HTTP event route/models,
`RunDashboard.loadSelectedRun`, web API/types. Event pagination remains blocked
on causal measurement. Owner-facing symptom remains unmeasured. Coordinator
owns serialized integration, publication and CI/live verification.

Material fixes required


## UI data latency discovery — cumulative review v03 rejected, 2026-09-12

Base: `304a39fd24b2d330d2533d041f3eb2776504086c` (unchanged).
Reviewed HEAD: `4c477de457edcc3aaa3960c117d689da5cb9b778`.
Coverage: 6 new / 10 total commits; cp1 v01, cp2 v01, both cp1 v02,
cp1 v03, all four cp1 v04 and cp1 v05. Reviewed both original checkpoints,
the active overlay, all nine cumulative changed files and surrounding request
owners. Prior findings were verified before the renewed cumulative review.

F1 resolved: retained selected-summary held-event regression plus reviewer
real Chromium Refresh probe. With direct Failed status returned and events
held, acceptance stayed false, including after unrelated DOM mutation;
after release it became true with Failed rendered. F2 coverage/fixed identities
and honest live/event gates remain resolved. F3 canonical projection counts
and F4 retained raw timing records remain resolved. F5 common-clock analysis
passes document-age invariance. No previous finding remains open.

One new admitted finding, F6 / P2 / high confidence:
`scripts/perf/analyze_ui_data_latency.py:165-168` treats configuration reads
as optional on explicit Refresh although the actual owner awaits GET config
and POST config/form inside loadDashboard before requesting selected data.
A real isolated held-config probe completed list JSON but started no selected
status/events and timed out at 45 seconds with 1/3 required reads complete.
This disproves the optional classification and hides an actual blocking
predecessor in the required discovery dependency attribution. Correct the
route/action-specific analysis and generated report, with a focused held-
prerequisite regression; reuse raw samples. Do not optimize product sequencing.

Evidence: external `/tmp/aflow-latency-review-v03-probe.{py,json}` (held config)
and `/tmp/aflow-latency-review-v03-refresh.{py,json}` (successful F1 verification).
The initial suspicion that background polling could bypass held config was
ruled out, not admitted as a finding. Four focused reviewer tests passed via
`uv run --frozen --project apps/aflow_app/server pytest -q
scripts/perf/test_ui_data_latency.py -k 'action_relative or projection_breakdown
or timing_summary or fixture_pairing' --basetemp <external-dir>/pytest`.
Inspected 184 retained samples: zero errors, 46 per fixture, all clock fields
present; independently recomputed 24 normal direct-operation core medians.
Reused worker turn-006 smoke and focused-test receipts; no full suite or scale
recollection. Cumulative whitespace check passed; product-source diff empty.
All reviewer servers used disposable state and unique loopback ports, then
stopped. No shared installation change, live read or service restart.

Sole fix overlay: `plans/in-progress/ui-data-latency-discovery-20260912-cp01-v03.md`.
Superseded v02 removed after preserving all prior dispositions. No history
rewrite, squash, approval, DEVLOG compaction or publication. Original plan
stays in place for the engine; tracked reviewer bookkeeping is intentional.

Separate improvement plan remains
`plans/in-progress/ui-data-latency-improvements-20260912.md` (dispatch pending
review). Product source: `304a39fd`; corrected browser runner: `37f83864`.
List mapping: repository list_history/get_run_status/_with_progress, daemon
run_status, service list_runs/_status, progress projection. Event mapping:
persistence read_events, repository tail_events, daemon poll_events, service
events, HTTP event routes/models, web API/types and RunDashboard.loadSelectedRun.
Event pagination and owner-facing baseline remain gated. Coordinator owns
serialized integration/publication and exact-SHA CI/live verification.

Material fixes required


## UI data latency discovery — cumulative review v04 approved, 2026-09-12

Base: `304a39fd24b2d330d2533d041f3eb2776504086c` (unchanged).
Reviewed HEAD: `4e5ff5e5480ceee648185cca0d9879ccbc9d06aa`.
Coverage: 1 new / 11 total commits: cp1 v01, cp2 v01, both cp1 v02,
cp1 v03, four cp1 v04, cp1 v05 and cp1 v06. Read the active v03 overlay
and prior findings first, then reviewed both original checkpoints and all nine
cumulative changed files, actual browser/service/repository owners and the
separate improvement handoff from the original base through HEAD.

F1–F6 resolved. F1 rejects empty/stale selected data and held event generations;
F2 retains four fixed-identity fixture shapes, full bounded browser coverage
and explicit live/event gates; F3 counts the canonical repository projection
separately from nested helpers; F4 retains raw timing samples; F5 uses a common
action clock; F6 now maps configuration/capability/plan Refresh prerequisites
as blocking without promoting initial-route configuration. No new material
finding passes the admission gate. No optimization was implemented.

Independent verification:

```sh
uv run --frozen --project apps/aflow_app/server pytest -q scripts/perf/test_ui_data_latency.py --basetemp /tmp/aflow-latency-review-v04-pytest
uv run --frozen ruff check scripts/perf
git diff --check 304a39fd24b2d330d2533d041f3eb2776504086c HEAD
git diff --check
git diff --name-only 304a39fd24b2d330d2533d041f3eb2776504086c HEAD -- aflow apps/aflow_app
```

15 tests passed in 17.93s, including real isolated Chromium held-config and
held-event cases; lint/whitespace passed and product diff is empty. Independent
JSON inspection verified unchanged 184 normal + 48 paired normal + 48 paired
counterfactual samples and all 28 profile records against v03 HEAD. Recomputed
all 28 five-sample core medians. Each fixture retains 30 warm, 6 fresh-process
and 10 in-app samples; normal samples have no errors and retain action clocks.
Every warm Refresh has 3 displayed-data, 4 blocking, 7 total required and 1
optional requests. Report regeneration agrees in values/content; loading
sorted JSON merely reorders three event/context table rows. Secret-marker
check passed. Reused retained scale/profile evidence and completed worker
turn-008 result from the parent repository; no full suite or live capture.

Approve and squash all 11 handoff commits into one unpublished commit after
the unchanged base, including this already-tracked review history and a single
compact DEVLOG entry. Preserve all other reviewed blobs. Remove the resolved
private v03 overlay; do not create v04 or force-add private artifacts. Leave
the original plan in place and record the final SHA there after the commit.
Finalization must verify one commit after base and clean tracked state.

Separate handoff: `plans/in-progress/ui-data-latency-improvements-20260912.md`.
Product evidence source: `304a39fd24b2d330d2533d041f3eb2776504086c`;
corrected browser evidence runner: `37f83864be6b97ff40e420e62b181e1e07414091`.
Run-list mapping: repository `list_history/get_run_status/_with_progress`,
daemon `run_status`, service `list_runs/_status`, progress projection.
Gated event mapping: persistence `read_events`, repository `tail_events`,
daemon `poll_events`, service `events`, HTTP event models/routes, web API/types
and `RunDashboard.loadSelectedRun`. Dispatch only the evidenced list work;
event pagination requires its measurement gate. Owner-facing baseline remains
BLOCKED. Coordinator owns dispatch, serialized integration/publication and
exact-SHA CI/live activation; none is claimed completed by this review.

No material findings
## CI readable run identity gates — cumulative review approved, 2026-09-12

Base: `fd99395b6c872e0149c51917c495adddd20fd009` (unchanged).
Reviewed HEAD: `fb19510ed5917419f15f902bf951d31635e5188f`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all original checkpoint
requirements and all four changed files. No previous findings or fix overlays
apply to this handoff; historical findings below belong to other plans.

No material findings under the admission gate, exclusions and proportionate-fix
discipline. Exact run-ID row boundaries, exact recovery source aria-label,
escaped terminal full-ID browser matching, uniqueness and visibility agree with
the production accessibility contracts. URL/back, held stale response, rejected
recovery draft/call count, authentication/cookie/reload/logout, two-project
history and worker lifetime assertions are preserved. No product changes.

Reused successful raw command receipts from the registered parent repository:
`/root/code/agent_flow/.aflow/runs/20260912t203140z-12f9b8fc/turns/turn-001/transport.stdout`.
The plan's exact focused Vitest command passed 3 tests (163 unselected), with
React act warnings; module AST and embedded-driver compilation passed.
The completed result.json confirms checkpoint completion. Reviewer independently
checked the full diff and `git diff --check fd99395b6c872e0149c51917c495adddd20fd009 HEAD`.
No full suites or installed-wheel browser journey were run locally; syntax
validation does not establish browser acceptance.

Approval includes this tracked record in one unpublished handoff commit after
the unchanged base. Preserve all four reviewed blobs and the single DEVLOG
entry. No fix plan is needed or created. Leave the ignored original plan for
engine finalization, recording the final SHA there after commit creation.
Verify one final commit and clean tracked state. Coordinator/controller owns
normal publication, exact-SHA CI and live verification; these remain pending.

No material findings

---

## CI readable browser contracts — cumulative review approved, 2026-09-12

Base: `f193dddf32e87377e77f2920804f462780c0b0be` (unchanged).
Reviewed HEAD: `3953a1b76267d1b01b943660cf539ebd5afabbb8`.
Coverage: 1 new / 1 total commit, `cp1 v01`, all eight original checkpoint
steps and the full four-file cumulative diff. No previous findings or fix
overlays apply to this handoff.

No material findings under the admission gate and proportionate-fix discipline.
Exact full-ID accessibility controls, unique terminal-ID global row matching,
separate exact context/title/status checks, canonical original-plan progress,
and explicit retry source/target disclosure agree with the current product.
The retry anchor is clicked normally and its exact hash and open target checked.
Recovery lineage, source immutability, exact requests, history, navigation,
geometry and refresh assertions remain. No production changes.

Reused raw completed command receipts in the registered parent repository:
`/root/code/agent_flow/.aflow/runs/20260912t205711z-dc203a92/turns/turn-001/transport.stdout`.
Build passed. Chromium initial run: three passed and parity failed on duplicated
canonical summary text; the scoped Current work selector correction passed its
focused rerun (1 passed, 4.36s). WebKit: 3 passed, 30.33s. A retained desktop
route rerun also passed (1 passed, 9.30s). Thus all four planned Chromium and
three planned WebKit nodes have passing evidence. Pytest used external
--basetemp options; no host-specific artifact paths entered portable test code.
Completed result.json confirms checkpoint completion. Reviewer independently
compared AST test names/decorators across the base and HEAD: unchanged inventory
and parametrization in all three modules. Cumulative git diff --check passed.
No full local suites or matrices were run; navigation/recovery CI remains pending.

Approval includes this tracked record in the single unpublished handoff commit,
preserving all four reviewed blobs and the single DEVLOG entry. No fix plan is
needed or created. Leave the ignored original plan for engine finalization and
record the final SHA there after committing. Verify one commit above the
unchanged base and clean tracked state. Coordinator/controller owns serialized
integration, normal publication, exact-SHA CI and live activation; those gates
are not claimed complete. Concurrent performance work remains untouched.

---

# Fast usable run loading — cumulative review approved (2026-09-12)

Review base: `f193dddf32e87377e77f2920804f462780c0b0be` (unchanged Pre-Handoff Base HEAD).
Reviewed HEAD: `e76b6197f29f2b799ff549d17030a01c4f5b88f4`.
Coverage: 2 new / 2 total commits: `cp1 v01` (`459aa1509d0ab8ac84a2c861c47c88e3e1b2c628`)
and `cp2 v01` (`e76b6197f29f2b799ff549d17030a01c4f5b88f4`).
Original and active plan: `plans/in-progress/fast-usable-run-loading-20260912.md`.
No previous findings or follow-up plans apply to this handoff.

Reviewed the complete cumulative diff and current implementation: history
identity filtering/cursors/revisions and absorbing deletion; compatible direct
repository projections; unchanged daemon ownership, startup and resume
reconciliation followed by final progress; server response preservation;
four-worker serial cursor traversal; copied partial-page callbacks;
per-generation acceptance; refresh overlay and per-project replacement;
failed/stale retention; loaded-only group/search and empty-state claims.
No material findings passed the material-code-review admission gate.

Verification reuses retained successful evidence in the primary repository's
`.aflow/runs/20260912t203748z-686af8b9/turns/turn-001/transport.stdout` and
`turn-002/transport.stdout`; no unchanged tests or full local suites repeated:

- Repository selection: 7 passed; daemon selection: 9 passed; API selection:
  5 passed. These include all three plan-named regression tests, optional
  malformed progress and read-only history coverage. Ruff passed.
- Web fetcher/overview selection: 24 passed, including the four required
  deferred scenarios, search beyond recent ten, exact project/run identities,
  repeated cursors, aborts and genuine complete empty results.
- Production `tsc && vite build` and targeted ESLint passed.
- Isolated profiler at `/tmp/tmp.qRd0HdFHaN/control-plane-profile.json` uses
  source `459aa1509d0ab8ac84a2c861c47c88e3e1b2c628`, unchanged backend blobs
  at reviewed HEAD. Three successful serial samples per prescribed fixture;
  100 final projections per 100-row list. Core list medians: 334.952,
  335.441, 597.696 and 586.847 ms for small-short, small-long, large-short
  and large-long histories. These are isolated core timings, not live
  navigation timings. Summary/detail reduction and resume admission remain.
- Coordinator-owned external verifier exists at the plan's exact path and
  its server-environment `--help` passed. It is preserved without modification.
- Independent cumulative `git diff --check f193dddf HEAD` passed.

Approve local implementation and squash all handoff commits plus intentional
tracked reviewer records into one unpublished commit after the unchanged base.
Compact the two handoff DEVLOG entries; preserve implementation blobs and all
prior review records. Record final SHA in the private original plan after the
commit, leaving that plan in place for engine finalization. No fix plan needed.

Publication, exact-source CI, activation, screenshot inspection and idle p100
<=2s first-row / <=5s full-coverage acceptance remain coordinator-owned and
pending. This review does not certify deployed usability or waive those targets.

No material findings

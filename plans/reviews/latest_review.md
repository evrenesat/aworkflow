# Cumulative approval — issue 35 repair progress

Original plan: `plans/in-progress/issue-35-repair-progress-20260909.md`.
Reviewed overlay: `plans/in-progress/issue-35-repair-progress-20260909-cp01-v01.md` (resolved and removed on approval).
Branch: `aflow-issue-35-repair-progress-20260909-20260910-230639`.
Unchanged Pre-Handoff Base HEAD: `a268c78b9e1dcdbebf3125c17669d42810f29414`.
Reviewed head: `00abbb0d2d603b84ed31433f3390e513e2d13c88`.
New commits since previous review: 1. Total handoff commits reviewed: 5.
Coverage: CP1 v01 (`a01bd4d4`), CP2 v01 (`55c0d9bb`), CP3 v01 (`7f17ff93`), CP4 v01 (`1b9aa1d3`), and follow-up cp01 v01 (`00abbb0d`). All four original checkpoints are checked.

## Findings and disposition

No material findings. R1 is resolved: new live boundaries persist `original_checkpoint_authority_version: 1`; pre-change captured boundaries retain legacy evidence/disclosure construction while new captures and rebuilds retain original checkpoint authority. The pre-handoff-module reproducer now completes actual historical analysis with exact payload equality and byte-identical artifacts. The new regression explicitly supplies legacy plan state, unavailable checkpoint evidence and disclosure, exercises `analyze_runs`, and checks durable artifact hashes. Runtime coverage asserts the persisted discriminator. No prior finding remains unresolved.

After checking R1, reviewed the complete cumulative implementation from the unchanged base through the reviewed head, including observer scope/path/evidence precedence and finalization, UI projection and legacy fallback, late-response handling, REST/MCP/browser fixtures, live authority/evidence selection, negative scope/digest tests, history compatibility, and documentation. Manager routing and turn selection remain unchanged. No additional candidate met the material finding admission gate.

## Verification

- Fresh `uv run python .aflow/review-issue35/reproduce_history_drift.py`: historical reconstruction preserved exactly; artifacts byte-identical. Historical output was prefixed `HISTORY:`.
- Fresh `uv run pytest -q tests/test_run_progress.py tests/test_manager_context.py tests/test_manager.py tests/test_runtime.py tests/test_live_config_runtime.py --tb=short`: 451 passed, 43 subtests passed, two known host-global-ignore lifecycle fixture failures. Output: `.aflow/review-issue35/repeat-python.log`.
- Fresh process-isolated rerun with `GIT_CONFIG_GLOBAL=/dev/null` of `tests/test_runtime.py::WorkflowLifecycleRuntimeTests::test_rejected_final_lifecycle_push_resumes_without_replaying_worker` and `tests/test_runtime.py::WorkflowLifecycleRuntimeTests::test_two_tracked_plan_runs_publish_ignored_done_lifecycle_cleanly`: 2 passed. Output: `.aflow/review-issue35/repeat-isolated.log`. Shared Git configuration was not changed.
- Fresh `uv run ruff check aflow` and `git diff --check`: passed.
- Retained prior cumulative verification, as explicitly permitted by the unchanged overlay scope: observer/repository/service/context regressions; authenticated REST/MCP 45 passed; web 336 passed across 22 files; production build passed; real Chromium 2 passed and WebKit 2 passed after that build. No browser, transport or observer implementation changed in the follow-up.
- Twelve retained screenshots: `.aflow/review-issue35/chromium/` and `.aflow/review-issue35/webkit/`. Re-inspected Chromium 390px repair and WebKit desktop next-checkpoint screenshots. Physical mobile keyboard remains unverified.
- Supplied worker result `.aflow/runs/20260910t230638z-89fc9ad0/turns/turn-006/result.json` is absent from this execution root. Review relied on committed code, independent tests and retained verification evidence.

## Approval and lifecycle

Approved for the required one-commit cumulative squash from the unchanged base. The remote main SHA was verified against `git ls-remote`; its merge base with this handoff is the original base, so none of the five rewritten feature commits is accepted main history. Only expected reviewer-owned plan progress was dirty at entry.

Approval finalization rotates the prior review byte-identically, compacts the three handoff DEVLOG entries, removes the resolved fix overlay without creating an empty replacement, and preserves implementation files. The original plan remains in `plans/in-progress/` for engine finalization and records the final squash SHA and one-commit verification. Publication, exact-SHA CI, live activation and ticket acceptance remain coordinator-owned; none is claimed by this review.

No material findings

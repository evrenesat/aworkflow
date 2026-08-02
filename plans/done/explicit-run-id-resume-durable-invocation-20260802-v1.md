# aflow Plan: Resume Exact Runs From Durable Invocation State

## Summary

Make `aflow run --resume [RUN_ID]` honor its documented plan-optional contract. Resume bootstrap must resolve the selected durable run before fresh-run plan validation, reconstruct omitted invocation identity from `run.json`, and retain every existing compatibility, worktree, scope, pending-boundary, and configuration safety check.

This plan consolidates the still-open resume portion of the 2026-08-01 `explicit-resume-plan-required` and `missing-manager-harness-orphan-resume-contract` incidents. Manager harness launch `OSError` handling is already covered by the merged harness-error normalization work and is outside this handoff.

## Git Tracking

- Plan Branch: `aflow-explicit-run-id-resume-durable-invocation-20260802-20260802-001252`
- Pre-Handoff Base HEAD: `8472d3dacf35e3b7b1180ac2b9890662c9e7ace2`

## aflow-review-final

- Status: approved
- Reviewed Through: `cp1 v06`
- Reviewed Range: `8472d3dacf35e3b7b1180ac2b9890662c9e7ace2..7a2a6f0d0254a4c0a89dfe66c73ca2caa52b409f`
- New Commits Since Last Review: `0`
- Total Implementation Commits Since Pre-Handoff Base HEAD: `1`

## Done Means

- `aflow run --resume RUN_ID` and unambiguous `aflow run --resume` can omit a plan and reconstruct the selected run's original plan and frozen invocation identity from durable metadata.
- Explicitly repeated plan, workflow, team, start-step, max-turns, and extra-instruction values are accepted only when compatible with the saved run under the existing resume contract.
- Fresh invocations without a plan still fail with `error: plan_file is required`.
- Missing, unreadable, malformed, or unsafe resume metadata fails closed before startup questions, worktree creation, or controller launch.

## Critical Invariants

- Resolve one exact durable run before deriving any omitted resume argument; never choose a plan by scanning `plans/` or by using the current default workflow as saved identity.
- Treat `original_plan_path` as authoritative and support `plan_path` only as the existing legacy metadata fallback. Require a non-empty string resolving to an existing file.
- Keep `_detect_resume_candidate` or one shared successor authoritative for repository, plan, workflow/config fingerprint, team, start-step, max-turns, extra-instruction, worktree, branch, active-plan, scope, and pending-finalized-turn validation.
- Do not change `--resume-reset-scope`, pending boundary replay, accepted review history, or lifecycle teardown semantics.
- Resume bootstrap is read-only until the normal prepared-run path has validated the reconstructed request.

## Forbidden Implementations

- Do not satisfy resume by supplying a hard-coded, newest, or first-discovered plan path.
- Do not weaken mismatch checks, silently replace conflicting caller flags, reset scope, or synthesize missing durable metadata.
- Do not make fresh no-plan runs valid or turn malformed resume metadata into an interactive guess.
- Do not rework manager harness error handling, dashboard behavior, or unrelated lifecycle policy in this handoff.

## Checkpoints

### [x] Checkpoint 1: Bootstrap plan-optional resume from one validated durable run

**Goal:**

- Reorder CLI resume bootstrap so omitted invocation fields come from the exact saved run and all existing resume validation remains fail-closed.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `aflow/cli.py`, `aflow/analyzer.py`, `aflow/api/startup.py`, `aflow/models.py`, `tests/test_cli.py`, `README.md`, and existing runtime/resume documentation.
- Preserve: fresh-run argument handling, AUTO resume selection rules, frozen-config fingerprint checks, pending finalized-turn replay, manager scope/history restoration, and lifecycle worktree safety.

**Scope:**

- May create or modify: `aflow/cli.py`, a narrowly shared run-metadata helper module only if needed, `tests/test_cli.py`, relevant existing resume documentation, and `DEVLOG.md`.
- Must not touch: manager harness execution, workflow transition semantics, dashboard code, team configuration, run retention, or managed-worktree teardown.
- Constraints: use existing `resolve_run_id` and `load_run_json` behavior; distinguish caller-supplied values from omitted values; preserve canonical path normalization and actionable `error:` messages.

**Steps:**

- [x] Add a read-only resume bootstrap result that resolves the explicit or AUTO candidate once, loads readable `run.json`, and validates the saved original-plan path before normal startup preparation.
- [x] Derive omitted plan, workflow, team, start-step, effective max-turns, and extra instructions from durable fields used by the existing resume contract; retain current defaults only for fresh runs.
- [x] Reject conflicts between repeated CLI values and saved identity with a field-specific error, then pass the resolved identity through the existing startup and `_detect_resume_candidate` safety checks without selecting a second run.
- [x] Add command-level tests for explicit and AUTO plan-free resume, compatible repeated values, conflicting repeated values, legacy plan metadata, nonexistent run, unreadable/malformed metadata, missing/nonexistent plan, and fresh no-plan behavior.
- [x] Add a regression proving plan-free resume of a finalized pending turn selects its saved worktree/branch/active plan and does not create a fresh worktree or duplicate the finalized turn.
- [x] Align existing CLI/resume documentation and record the durable-bootstrap contract in `DEVLOG.md`; do not document unsupported override behavior.

**Dependencies:**

- None. The previously merged harness launch normalization is a verified prerequisite and must not be reimplemented.

**Verification:**

- Run: `uv run pytest -q tests/test_cli.py -k 'resume and (plan or explicit or auto or durable)'`
- Run: `uv run pytest -q tests/test_cli.py`
- Run: `uv run pytest -q tests/test_runtime.py -k 'resume or pending_finalized'`
- Run: `uv run python -m compileall -q aflow tests`
- Run: `git diff --check`
- Observe: a fixture run resumed with only `--resume RUN_ID` reaches the saved resume context, while a fresh run without a plan and every malformed/conflicting resume fixture exits before creating state.

**Done When:**

- Plan-optional exact and AUTO resume behavior, negative paths, and pending-turn safety are covered by command-level tests and use one durable candidate.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if durable metadata cannot distinguish an omitted CLI field from a semantic override without changing the persisted run schema; make any schema migration a separate checkpoint rather than guessing.
- Stop and report if unrelated dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

- Given an incomplete run with valid durable invocation metadata, when invoked as `aflow run --resume RUN_ID`, then aflow resolves that exact run, uses its original plan and frozen identity, and enters the existing resume path without prompting for a plan.
- Given the same run selected unambiguously by shell context, when invoked as `aflow run --resume`, then behavior matches exact resume and does not select another candidate.
- Given a compatible explicit plan or workflow repeated by the caller, when resuming, then it is accepted after canonical comparison; given a conflicting value, the command names the mismatch and creates no state.
- Given missing, unreadable, malformed, or nonexistent saved plan metadata, when resuming, then aflow exits nonzero with an actionable error before worktree or controller creation.
- Given a fresh `aflow run` with no plan, when invoked, then it retains `error: plan_file is required`.
- Given a saved finalized turn awaiting manager routing, when resumed plan-free, then the accepted turn is not rerun and its pending boundary is routed exactly once under existing semantics.

## Plan-to-Verification Matrix

- Exact durable candidate and original-plan reconstruction: command-level explicit/AUTO and legacy-metadata tests.
- Frozen invocation compatibility: compatible and conflicting plan/workflow/team/start-step/max-turns/instruction tests.
- Fail-closed metadata handling: nonexistent run, unreadable JSON, missing field, invalid type, and nonexistent path tests with no-state assertions.
- Fresh-run compatibility: existing and new fresh no-plan CLI tests.
- Pending-turn safety: focused resume fixture plus existing runtime resume tests.
- Documentation accuracy: CLI help/example assertion or focused documentation review aligned with tested behavior.

## Assumptions And Defaults

- Existing run metadata already records the invocation fields required for current resume compatibility; this checkpoint changes bootstrap ordering, not the durable schema.
- `original_plan_path` remains stable for incomplete runs; `plan_path` is accepted only for legacy run metadata already supported by aflow.
- Caller omission means reuse saved identity. Caller-supplied conflicts fail rather than override; `--resume-reset-scope` remains the only explicit scope reset and does not relax identity checks.

## Review Log

- 2026-08-02: Worktree-fallback review after the cp01 v03 repair found that
  durable invocation and frozen-configuration checks pass, but required resume
  context fields are still decoded only after startup preparation. Checkpoint 1
  remains unapproved pending the focused cp01 v04 repair plan.
- 2026-08-02: Review of the cp01 v04 repair confirmed complete context
  construction now precedes startup and the focused CLI/runtime suites pass,
  but present pending-repartition state is still decoded tolerantly and can
  omit stage-required identity or artifact references until controller startup.
  Checkpoint 1 remains unapproved pending the focused cp01 v05 repair plan.
- 2026-08-02: Review of the cp01 v05 repair confirmed strict transaction
  decoding, pre-startup byte binding, and reset-scope opacity, but non-reset
  transactions are not yet bound to their active scope, derived generation,
  manager boundary, or workflow route. Internal symlink aliases can also bind
  bytes under paths different from those retained in the transaction.
  Checkpoint 1 remains unapproved pending the focused cp01 v06 repair plan.
- 2026-08-02: Worktree-fallback review through cp01 v06 confirmed that resume
  binds pending repartition state before startup to its restored active scope,
  validated envelope, manager boundary, producer-derived generation, and frozen
  workflow route. Durable artifact paths now retain exact canonical identity,
  and reset scope remains opaque to pending transaction metadata and paths while
  non-scoped worktree, lifecycle, invocation, and frozen identity validation
  remains mandatory. Focused CLI/runtime verification and compile/diff hygiene
  passed; the full root suite has the same 16 baseline failures as the recorded
  pre-handoff base. Checkpoint 1 is approved through cp1 v06.
- 2026-08-02: `aflow-review-final` approved the completed handoff without a
  follow-up fix plan. The final review covered 0 new commits since the latest
  checkpoint approval and 1 total implementation commit since `8472d3d`;
  exact/AUTO durable selection, invocation reconstruction, frozen configuration,
  startup ordering, reset-scope opacity, pending-boundary recovery, and canonical
  pending-artifact binding were re-inspected. Focused CLI/runtime tests,
  compilation, and diff hygiene passed. The root-package suite retained the
  same 16 failures as the pre-handoff base, and unrestricted root collection
  retained its optional app-server dependency boundary.

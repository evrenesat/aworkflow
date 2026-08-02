# Correct Correctable Manager Note-Authority Failures

## Summary

Defect 1 is a P0 orchestration reliability failure reproduced by run
`20260802T004105Z-20538c41`, manager decision 11. A syntactically valid Full
manager decision selected the legal `continue` action, but one
`next_step_notes` entry said to use a repair plan. The authority validator
correctly rejected that plan-selection phrasing; the controller then
terminalized the run instead of asking the same manager boundary to correct
only its advisory notes.

Implement one bounded, auditable correction sub-attempt for this exact
plan-selection authority category. Keep plan authority fail-closed, preserve
the original response, reuse the immutable boundary and scope, and persist only
one logical manager decision. Do not resume or mutate the historical failed
run while implementing this plan.

Source report: `plans/todo/aflow-defect-manager-note-correction-20260802.md`.

## Git Tracking

- Plan Branch: `aflow-aflow-manager-note-authority-correction-20260802-v-20260802-133234`
- Pre-Handoff Base HEAD: `a3659e14bb1a7f34e3eafa77da5b43bea41be674`

## aflow-review-final

- Status: approved
- Reviewed Through: `cp2 v01`
- Reviewed Range: `a3659e14bb1a7f34e3eafa77da5b43bea41be674..730bc561cf0dc3cee3829fc7842d2f8c247c57e1`
- New Commits Since Last Review: `0`
- Total Implementation Commits Since Pre-Handoff Base HEAD: `2`

## Done Means

1. Plan-selection note violations have a machine-readable authority category
   that is the only category eligible for immediate correction.
2. Manager prompts enumerate the forbidden plan-selection verbs and show
   compliant behavior/evidence-only alternatives without exposing new plan
   authority.
3. A correctable first response receives at most one correction invocation at
   the same decision number, level, target identity, note scope, eligible-action
   set, model, and finalized boundary.
4. The correction may change only `next_step_notes`; the corrected complete
   response is parsed and validated normally before any controller decision is
   persisted or routed.
5. Both raw attempts are durable, while `run.json`, manager history, observer
   events, and downstream routing see one logical accepted or invalid manager
   decision.
6. Invalid JSON, launch/nonzero failures, repository mutation, changed plan or
   scope identity, non-note decision changes, non-correctable authority errors,
   and a second invalid note response remain terminal.
7. Focused unit, manager-context, runtime, documentation, compilation, and diff
   hygiene checks pass; `DEVLOG.md` records the verified behavior.

## Critical Invariants

1. The active plan, checkpoint, file constraints, workflow routing, and worker
   requirements remain controller-owned; correction never weakens
   `validate_manager_note_authority()`.
2. Only the `plan_selection` authority category is correctable. File-scope
   claims, prohibitions, mandatory implementation requirements, parser errors,
   illegal actions, and mutation findings never enter this correction path.
3. Correction is a sub-attempt of the original decision, not a new manager
   decision: one decision number, one `ManagerStartedEvent`, one
   `ManagerDecidedEvent`, and one `manager_history` entry.
4. The original parsed decision's `schema_version`, `action`, `reason`, and
   `stop_report` are immutable. The correction may only replace or clear
   `next_step_notes`.
5. The exact captured `boundary.json`, manager context, proposed/retry note
   scope, eligible actions, target plan identity, model/profile, working
   directory, and repository fingerprint remain unchanged across both
   attempts.
6. The original prompt, stdout, stderr, and violation are never overwritten.
   The canonical root `result.json` reports the final logical outcome and links
   to the correction sub-attempt when present.
7. A correctable response gets exactly one correction invocation. A failed or
   still-invalid correction terminalizes directly and cannot fall through to
   Lite-to-Full escalation or another correction loop.
8. Existing pending-note resume correction under
   `_prepare_pending_manager_notes()` remains separate and behaviorally
   unchanged; it repairs already-persisted notes before worker launch, whereas
   this plan repairs a current response before decision persistence.

## Forbidden Implementations

- Do not delete, relax, bypass, or silently rewrite the plan-selection regexes
  or note-authority validation.
- Do not classify every `ManagerDecisionError` or every note-authority error as
  correctable.
- Do not implement correction as a second `decision-NNN`, a Full escalation,
  a workflow turn, a worker/reviewer retry, or a reuse of persisted pending-note
  recovery.
- Do not allow the correction response to change action, reason, stop report,
  schema version, eligible actions, route, target plan, scope, model, or
  selector.
- Do not overwrite the first response or make a corrected root `result.json`
  appear to have had only one harness attempt.
- Do not retry after invalid JSON, a correction launch/nonzero failure,
  repository mutation, identity drift, an unsafe/illegal action, or the second
  authority failure.
- Do not add a `run.json` schema migration for additive per-decision artifacts
  or copy raw correction traces into manager context/history.
- Do not resume, edit, delete, or otherwise repair
  `.aflow/runs/20260802T004105Z-20538c41` as part of implementation.
- Do not absorb unrelated current changes in installation, guard, or
  documentation files into this defect.

## Checkpoints

### [x] Checkpoint 1: Define the correction protocol and audit artifacts

**Goal:**

- Add a decision-complete, unit-tested contract that distinguishes the one
  correctable authority category, tells the manager how to repair it, and
  records an optional second attempt without changing logical decision identity.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Run: `git status --short`
- Inspect: `sed -n '1,540p' aflow/manager.py`
- Inspect: `sed -n '120,190p' aflow/runlog.py`
- Inspect: `sed -n '1,110p' aflow/bundled_skills/aflow-manager/SKILL.md`
- Inspect: `sed -n '140,270p' tests/test_manager.py`
- Inspect incident if still retained: `cat .aflow/runs/20260802T004105Z-20538c41/manager/decision-011/stdout.txt && cat .aflow/runs/20260802T004105Z-20538c41/manager/decision-011/result.json`
- Preserve: all existing accepted/rejected note-authority cases, closed manager
  JSON protocol, Lite plan-prose redaction, and current root manager-artifact
  paths for decisions without correction.

**Scope:**

- May create or modify: `aflow/manager.py`, `aflow/runlog.py`,
  `aflow/bundled_skills/aflow-manager/SKILL.md`, `tests/test_manager.py`
- Must not touch: `aflow/workflow.py`, `aflow/manager_context.py`,
  `aflow/run_state.py`, historical `.aflow/runs/`, workflow configuration,
  installation/guard files, or user plans other than checkbox synchronization
  in this plan
- Constraints: define `ManagerNoteAuthorityError` as a
  `ManagerDecisionError` subclass with a stable category value for
  `plan_selection`, `file_scope`, and `mandatory_implementation`; expose an
  explicit boolean/predicate whose only correctable value is `plan_selection`.
  Preserve existing human-readable error text for compatibility.

**Steps:**

- [x] Change each authority rejection site to raise the structured subclass
  with its machine-readable category while retaining the existing message and
  active-plan identity suffix. Confirm ordinary parser/action errors remain the
  base `ManagerDecisionError`.
- [x] Add a correction-prompt builder that accepts the immutable original
  manager context, the fully parsed rejected decision, and the structured
  violation. Its compact payload must contain the decision number, level,
  trigger, eligible actions, proposed transition, selected controller-owned
  note scope, target plan identity, original decision, and violation category;
  it must not include uncaptured filesystem or mutable run state.
- [x] Require the correction prompt to return the same complete JSON schema,
  preserve `schema_version`, `action`, `reason`, and `stop_report`, and only
  rewrite/remove `next_step_notes`. Enumerate `use`, `follow`, `switch to`,
  `replace`, `adopt`, and `work from` when they select/reference a plan. Include
  compliant examples that describe the defect, required observable behavior,
  and verification evidence without telling the worker which plan to use.
- [x] Mirror the plan-selection wording and compliant behavior-only examples in
  the bundled `aflow-manager` skill so installed and inline prompt contracts
  agree.
- [x] Add `ManagerNoteCorrectionPaths` and a runlog helper for
  `manager/decision-NNN/note-authority-correction/` containing
  `system-prompt.txt`, `user-prompt.txt`, `stdout.txt`, `stderr.txt`, and
  `result.json`. Creation must be exclusive and additive; the decision root
  continues to hold immutable context/boundary and the original attempt's
  prompt/streams.
- [x] Define the root `result.json` additive summary contract for corrected
  decisions: `attempt_count`, `correction_attempted`, original violation
  category/message, correction artifact path/status, and the final accepted or
  invalid logical decision. Legacy readers must continue to obtain top-level
  `status`, `action`, `reason`, and `decision_number` without reading the
  correction directory.
- [x] Add unit tests named for structured category/correctability, unchanged
  legacy messages, forbidden/compliant prompt language, immutable decision
  fields, exclusive correction artifact creation, and the one-decision root
  result contract.

**Dependencies:**

- None.

**Verification:**

- Run: `uv run pytest -q tests/test_manager.py -k 'note_authority or note_correction or prompts'`
- Run: `uv run python -m compileall -q aflow/manager.py aflow/runlog.py tests/test_manager.py`
- Run: `git diff --check`
- Observe: plan selection reports category `plan_selection` and is correctable;
  file-scope and mandatory-requirement errors preserve their messages but are
  not correctable; correction artifacts can coexist with the original attempt
  under one `decision-NNN`.

**Done When:**

- The protocol and artifact contract are fully implemented without changing
  runtime routing.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and
  `git diff --stat`.

**Blockers:**

- Stop and report if preserving existing error-message compatibility conflicts
  with reliable machine-readable classification.
- Stop and report if the additive correction directory would require changing
  the durable `run.json` schema or existing decision-directory names.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 2: Correct one current response before decision persistence

**Goal:**

- Integrate the protocol into the live manager boundary so the incident response
  is corrected exactly once and accepted as one decision, while every unsafe or
  second failure remains terminal and auditable.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Run: `git status --short`
- Inspect: `sed -n '4620,4865p' aflow/workflow.py`
- Inspect: `sed -n '5825,5905p' aflow/workflow.py`
- Inspect: `sed -n '640,735p' aflow/manager_context.py`
- Inspect: `sed -n '8880,9005p' tests/test_runtime.py`
- Inspect: `sed -n '9820,10110p' tests/test_runtime.py`
- Inspect: `sed -n '45,95p' ARCHITECTURE.md && sed -n '392,414p' docs/runtime-behavior.md && tail -n 100 DEVLOG.md`
- Preserve: existing non-correctable Lite invalid-output escalation, explicit
  `escalate_to_full`, pending-note resume correction, manager mutation checks,
  terminal reporting, and downstream pending-note one-shot consumption.

**Scope:**

- May create or modify: `aflow/workflow.py`, `aflow/manager_context.py`,
  `tests/test_manager_context.py`, `tests/test_runtime.py`, `ARCHITECTURE.md`,
  `docs/runtime-behavior.md`, `DEVLOG.md`, plus Checkpoint 1 files only when a
  verified integration defect requires a narrow correction
- Must not touch: `aflow/run_state.py`, CLI argument/resume selection,
  repartition behavior, workflow TOML, installer/guard work, historical run
  state, or unrelated plans
- Constraints: correction runs inside `_run_manager_call()` before incrementing
  `state.manager_decision_number`, appending history, emitting the decided
  event, persisting pending routing/notes, or terminalizing. Use an internal
  `ManagerCallOutcome` value rather than an ambiguous expanded tuple so callers
  can distinguish no correction from a consumed correction attempt.

**Steps:**

- [x] Refactor `_run_manager_call()` into one logical-decision lifecycle:
  capture context/boundary/scopes/identities/fingerprint once, invoke the normal
  attempt, parse and validate the closed protocol, then inspect a structured
  note-authority error before writing history or routing state.
- [x] Before correction, verify the first attempt did not mutate the repository
  and that proposed/retry target identities plus the selected controller-owned
  note scope still equal their captured values. Any drift is terminal and must
  not invoke correction.
- [x] When and only when the first attempt is otherwise valid and fails with
  correctable `plan_selection`, invoke the same resolved manager
  model/profile/adapter in the same execution checkout once using the compact
  correction prompt. Reuse the original decision number, level, eligible
  actions, finalized boundary, and selected proposed/retry scope.
- [x] Parse the correction as a complete manager decision; apply existing schema,
  level, action-eligibility, proposed-END, note bounds, and authority validators;
  additionally compare the four immutable non-note fields to the original
  parsed decision. Reject a changed field, target identity/scope drift,
  repository mutation, process launch/nonzero result, invalid JSON, or any
  second authority error without another manager call.
- [x] Persist the original attempt at the decision root, the second attempt in
  `note-authority-correction/`, and the canonical final summary in the root
  `result.json`. A corrected success must append/emit/persist exactly one
  accepted decision; a failed correction must append/emit/persist exactly one
  invalid decision before the existing terminal report path runs.
- [x] Prevent the outer Lite invalid-output branch from escalating after a
  correction was consumed. Preserve the current Lite-to-Full behavior for
  non-correctable first-attempt errors and preserve direct Full handling for
  all non-correctable errors.
- [x] Add manager-context coverage proving the correction payload retains the
  exact captured decision number, level, eligible actions, proposed transition,
  selected scope, and active-plan identity while excluding active-plan prose
  from Lite correction payloads.
- [x] Add runtime regressions reproducing decision 11 phrasing and asserting:
  valid correction; corrected note injection exactly once; unchanged scope and
  target identity; one decision number/history record/started+decided event;
  both attempt artifacts; and no extra workflow turn caused by correction.
- [x] Add negative runtime regressions for a second invalid response, correction
  launch exception/nonzero result, correction changing a non-note field,
  repository mutation or plan/scope identity drift between attempts, and a
  non-correctable Full authority error. Assert no worker launch and no second
  correction/Full escalation after these failures.
- [x] Update `ARCHITECTURE.md` with the same-boundary correction invariant,
  `docs/runtime-behavior.md` with the additive artifact layout and one-decision
  semantics, and `DEVLOG.md` with the verified P0 fix and focused test results.
  Do not rewrite unrelated existing entries.

**Dependencies:**

- Checkpoint 1 complete and verified.

**Verification:**

- Run: `uv run pytest -q tests/test_manager_context.py -k 'manager_note_scope or note_correction'`
- Run: `uv run pytest -q tests/test_runtime.py -k 'manager_note_correction or scope_authority_note or pending_notes'`
- Run: `uv run pytest -q tests/test_manager.py tests/test_manager_context.py`
- Run: `uv run pytest -q tests/test_runtime.py -k 'manager or pending_notes_invalid'`
- Run: `uv run pytest -q tests/test_docs.py`
- Run: `uv run python -m compileall -q aflow tests`
- Run: `git diff --check`
- Observe: the incident-shaped Full response is corrected under its original
  decision number and routes once; every negative case stops before another
  worker, another decision, or another correction is launched.

**Done When:**

- All Done Means outcomes and behavioral acceptance tests are satisfied, with
  durable artifacts proving both attempts and one logical decision.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and
  `git diff --stat`.

**Blockers:**

- Stop and report if a same-boundary correction cannot preserve exact target
  identity, scope, eligible actions, model/profile, and repository fingerprint.
- Stop and report if making both attempts auditable would require overwriting
  the original raw response or recording two logical decisions.
- Stop and report if current unrelated changes in `ARCHITECTURE.md` or
  `DEVLOG.md` cannot be separated safely from this plan's additive updates.
- Stop and report if unrelated dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

1. **Incident-shaped valid correction:** Given a valid Full `continue` decision
   whose only defect is `"Use the v04 repair plan..."`, when the correction
   returns the same decision with behavior-only notes, then the workflow accepts
   one decision, injects the corrected notes once, and launches the intended
   next worker once.
2. **No authority weakening:** Given plan-selection, restrictive file-scope,
   and mandatory-implementation notes, when validation runs, then all remain
   rejected and only plan selection is marked correctable.
3. **Immutable logical decision:** Given a correction that changes `action`,
   `reason`, `stop_report`, or `schema_version`, when it is validated, then the
   run terminalizes and never routes the changed decision.
4. **One bounded attempt:** Given a correctable first response followed by
   invalid JSON or another invalid note, when correction completes, then no
   third manager invocation, Lite-to-Full escalation, or worker launch occurs.
5. **Launch failure:** Given the correction harness cannot start or exits
   nonzero, when the controller handles the result, then both available attempt
   evidence and one invalid logical decision are persisted before terminal
   reporting.
6. **Unchanged authority:** Given the repository fingerprint, target plan
   identity, or selected note scope changes after the first attempt, when the
   controller considers correction, then it fails closed without accepting or
   routing a response from the changed boundary.
7. **Audit identity:** Given a successful correction, when artifacts and
   `run.json` are inspected, then one `decision-NNN` contains the original raw
   attempt plus `note-authority-correction/`, while history/events contain one
   accepted decision number and the root result reports two attempts.
8. **Compatibility:** Given an accepted first response, a non-correctable Lite
   invalid response, or restored invalid pending notes, when each path runs,
   then existing no-correction, Lite-to-Full, and prelaunch pending-note behavior
   remains unchanged.

## Plan-to-Verification Matrix

| Requirement | Concrete verification |
| --- | --- |
| 1. Machine-readable, narrow correctability | `tests/test_manager.py` category, message, and negative-category unit tests |
| 2. Explicit prompt guidance | Prompt unit tests plus bundled-skill assertions/search in `tests/test_manager.py` and `tests/test_docs.py` |
| 3. Same immutable boundary | Manager-context equality/redaction tests and runtime target/scope/fingerprint drift tests |
| 4. One correction only | Runtime second-invalid and launch-failure call-count assertions |
| 5. One logical decision | Runtime decision number, history, observer-event, turn-count, and routing assertions |
| 6. Both attempts durable | Runlog unit test and runtime filesystem/result JSON assertions |
| 7. Unsafe cases terminal | Runtime invalid JSON, non-note change, mutation, drift, and non-correctable Full cases |
| 8. Compatibility | Existing manager, manager-context, scope-authority, pending-note, and documentation suites |
| 9. Documentation aligned | `tests/test_docs.py`, content inspection, and `git diff --check` |

## Assumptions And Defaults

1. The supplied todo report and retained decision-11 artifacts are sufficient
   incident evidence. Implementation tests must embed the minimal response and
   must not depend on the historical run remaining unpruned.
2. `plan_selection` means the existing validator's plan-selection verb plus
   plan reference/path cases. It is correctable only after the rest of the
   response has passed JSON, schema, action, level, eligibility, and stop-report
   validation.
3. A correction may remove the offending note entirely or replace it with
   behavior/evidence-only guidance; empty notes are compliant when legal for the
   unchanged action.
4. Root decision prompt/stdout/stderr files remain the original attempt for
   audit compatibility. Root `result.json` is the canonical logical outcome and
   additively links to the second attempt; the correction directory contains
   the exact second prompt/streams/result.
5. Additive fields in per-decision `result.json` do not require a `run.json`
   schema bump. Existing readers continue to use the top-level final status,
   action, reason, and decision number and ignore the correction subdirectory.
6. No new public CLI flag or configuration toggle is needed. The safe default
   is always one correction for the sole correctable category when manager
   supervision is already enabled.
7. Current p100 dirt in installation/guard files is unrelated and must be
   preserved. Because `ARCHITECTURE.md` and `DEVLOG.md` are also currently
   modified, their ownership must be resolved or their additive hunks isolated
   before Checkpoint 2 edits them.
8. `README.md` needs no change because this is an internal supervision and
   durable-artifact behavior fix with no setup or user-facing command change.
   `AGENTS.md` needs no change because responsibilities and local instructions
   are unchanged.

## Review Log

- 2026-08-02: Checkpoint 1 worktree fallback rejected before a checkpoint
  commit. Mixed note-authority failures can be classified as correctable
  `plan_selection` because validation short-circuits before detecting
  non-correctable file-scope or mandatory-implementation violations. Focused
  repair plan: `aflow-manager-note-authority-correction-20260802-v1-cp02-v01.md`.
- 2026-08-02: Checkpoint 1 approved through `cp1 v01` (`823f582`) after the
  focused repair made mixed authority failures non-correctable in either note
  order while preserving the plan-selection-only correction contract.
- 2026-08-02: Checkpoint 2 worktree fallback approved through `cp2 v01` after
  verifying same-boundary correction, immutable decision fields, bounded
  failure handling, one-decision persistence/routing, additive artifacts, and
  the focused context, manager, runtime, documentation, compilation, and diff
  hygiene checks.
- 2026-08-02: `aflow-review-final` approved the completed handoff without a
  follow-up fix plan. The final review covered 0 new commits since the latest
  checkpoint approval and 2 total implementation commits since `a3659e1`;
  structured authority classification, one bounded same-boundary correction,
  immutable decision and scope enforcement, additive audit artifacts, terminal
  failure handling, one-decision persistence/routing, prompts, and documentation
  were re-inspected. All plan-specific checks passed. The complete core suite
  retained the same 16 failures as the pre-handoff base, and unrestricted root
  collection retained its optional app-server dependency boundary.

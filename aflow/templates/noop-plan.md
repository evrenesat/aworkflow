# AFlow NO-OP harness/workflow smoke plan

## Summary

Exercise real agents and normal workflow routing with no application changes.
External state: `{{STATE_DIR}}`. Read this contract once; keep reports brief.
Batch playbook reading, helper invocation, and verification when possible.
Do not inspect AFlow/helper source or narrate each operation. Report one short
result line on success, or the exact diagnostic on failure, plus any bookkeeping
explicitly required by the workflow.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Playbook contract

- For CP N, read `worker.playbook` or `reviewer.playbook` in the external state
  directory immediately before acting. Never edit playbooks to make a test pass.
  Missing/inaccessible files are real environment failures: stop and report them;
  never run `noop-plan`, recreate files, or substitute another directory.
- Entries are `CPN: action; action`. Blank/comment lines are ignored. Actions
  are case-insensitive; messages are literal. Missing worker entries mean
  `done`; missing reviewer entries mean `pass`, with no delay.
- Workers run `aflow noop-step worker N --state-dir '<external state directory>'`.
  Successful actions create `checkpoints/CP_N.txt` (touch). This marker is the
  simulated implementation; also update the plan's step and heading checkboxes.
- Reviewers run `aflow noop-step reviewer N --state-dir '<external state directory>'`
  for the checkpoint under review. A whole-plan review runs it for each completed
  checkpoint in ascending order. A plan/design review only checks this contract;
  do not execute playbooks or redesign the fixture during plan review.
- Helper exit 0 means success. A requested `fail MESSAGE` returns exit 1 with
  `AFLOW_STOP: AFLOW_NOOP_MOCK_FAILURE CP<N>: MESSAGE`. Only that labeled result
  is an injected hard failure. `AFLOW_STOP: NO-OP fixture: ...`, provider errors,
  missing files, and unsupported actions are real failures, never mock successes.
  Repeat the exact diagnostic and stop without marking more work done. Do not
  troubleshoot, retry, or bypass an injected failure within the same turn.
- Reviewer exit 2 (`AFLOW_NOOP_REJECT`) means reject using the workflow's normal
  fix-plan path, not a hard stop. Keep the rejected CP pending; the focused repair
  should rerun that CP's worker helper and marker check. Include its CP number,
  external state path, and these rules in the repair overlay. An operator may
  edit the reviewer playbook before re-review; never change it yourself.
- Worker exit 3 (`AFLOW_NOOP_INCOMPLETE`) is a recoverable simulated capability
  failure, not a hard stop. Leave the CP unchecked and report the exact message
  for reviewer/manager routing. Do not repair or repeat the mock in the same turn.
  Reviewers must reject a missing worker marker for the CP under review even if
  their playbook says pass. Keep real helper/provider errors distinct from mocks.
- `sleep N` executes the actual system `sleep N` command. `cleanup` removes only
  `checkpoints/CP_*.txt`; after a successful worker invocation its own marker is
  recreated. A failed worker attempt removes its own stale marker. `done` and
  `pass` add no delay; `fail MESSAGE` and reviewer `reject MESSAGE` end the action
  sequence. Helpers do not update plan checkboxes or create commits.
- Follow the selected workflow's checkpoint/whole-plan scope and Git lifecycle.
  On passing review, approve normally; if the workflow requires an approval
  commit and has no staged changes, an empty approval commit is allowed.
  Do not alter application code or create fake implementation changes.
- Rejection overrides success from marker existence. Reviewer failure leaves
  worker evidence intact. Previously approved CP markers may be removed by a
  later cleanup; do not reopen approved CPs for that reason alone.

## Scope and done means

Only this copied plan, workflow-owned plan/commit bookkeeping, and the external
fixture state may change. No repository exploration, builds, or application
tests are needed. Mark only successfully verified work complete; honor the
workflow's normal reviewer ownership and final plan move. All required CPs and
reviews passing means done. Defaults finish without sleeping. Use separate
state directories for concurrent runs; `/tmp` contents are disposable.

## Checkpoints

{{CHECKPOINTS}}

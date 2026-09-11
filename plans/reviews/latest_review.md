# Issue 9 — cumulative approval review, 2026-09-11

Original plan: `plans/in-progress/issue-9-bounded-startup-recovery-20260909.md`.
Active repair overlay: `issue-9-bounded-startup-recovery-20260909-cp01-v01.md`.
Branch: `aflow-issue-9-bounded-startup-recovery-20260909-20260911-075547`.
Unchanged Pre-Handoff Base HEAD: `b46e441ca758383df60eefe571d097c5ce597ad9`.
Reviewed HEAD: `11b4465b745b808621bb18328bf33f54ab667a2c`.
Coverage: **1 new / 3 total commits**, including `5112734e` (cp1 v01),
`1cae4c4c` (cp2 v01), and `11b4465b` (cp2 v02). Both original checkpoints
and the full base-to-HEAD implementation across all 21 changed files were reviewed.
Incremental fix inspection did not replace cumulative review.

## Previous findings

- F1 resolved: the external CLI can record acknowledged, failed and uncertain
  outcomes from a consumed claim. Callback and external recording share bounded
  validation; matching uncertainty can be acknowledged, and conflicting keys or
  successors are rejected. Actual subprocess regressions passed.
- F2 resolved: claims retain the predecessor key as lineage and persist one
  distinct replacement key before launch. The in-memory daemon regression proves
  the predecessor replays its typed failure while the replacement reaches
  preparation once and starts one unit. Repeated attempts do not relaunch.

## Cumulative findings

No material findings. Reviewed canonical missing/blank/partial Git Tracking
normalization, pristine/started admission, source-byte preservation, lifecycle
branch/base selection and bootstrap, explicit identity preservation, typed
CLI/server errors, REST/MCP admission tests, guard receipt eligibility and
one-attempt persistence, external outcomes, observer boundaries, and bundled
installer/refresh behavior. Existing startup backup wrappers and capture calls
remain intact for issue34 integration. No production edits were needed.

## Verification

Independently ran:

```text
uv run pytest -q tests/test_guard_recovery.py::test_cli_claim_records_acknowledged_successor tests/test_guard_recovery.py::test_cli_outcomes_never_enable_relaunch tests/test_guard_recovery.py::test_replacement_key_is_durable_and_distinct tests/test_aflowd.py::test_guard_replacement_uses_new_key_after_terminal_metadata_failure
4 passed in 1.23s
```

Reused retained evidence for unchanged scope:

- Prior cumulative CP1 review: 52 metadata tests passed, 493 deselected,
  27 subtests passed; worker evidence also records focused CLI, REST and MCP nodes.
- CP2 v01: 56 snapshot/ownership/issue/docs tests and three installer/refresh
  nodes passed, with Ruff and compilation checks.
- CP2 v02: 31 recovery tests, one daemon replacement test, 24 ownership/docs
  tests and three installer/refresh nodes passed; `uv run ruff check aflow` passed.
- Current working and cumulative `git diff --check` passed.

Evidence is retained outside the execution worktree at
`/root/code/evidence/aflow-dogfood-20260909/issue9-review-20260911/`.
Full suites belong to CI. No live services, providers, installed skills or
real guarded runs were modified.

## Disposition

Approved for one unpublished accumulated handoff commit after the unchanged
base. Finalization includes this tracked review report and one compact DEVLOG
handoff entry, removes the superseded private fix overlay, preserves reviewed
implementation blobs, and leaves original-plan tracking in place for engine
finalization. The final approved SHA is recorded in original-plan tracking,
not in a self-referential commit. Prior review was rotated byte-for-byte;
ignored private plans/evidence are not force-added. Merge, origin/main
publication, exact-SHA CI and live activation remain controller/coordinator
owned and are not claimed by this local review.

No material findings

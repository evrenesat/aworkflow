# Deliver reviewed resume-evidence fix to unblock the preserved core run

## Summary

AFlow run `20260924t210811z-019e7756` contains a completed Sol worker implementation for owner team routing, but the deployed controller stops before its checkpoint review with `invalid_evidence`. Its predecessor's Checkpoint 1 already received Sol checkpoint approval in commit `f47630a0`: manager context must resolve copied schema-v2 scope evidence from the current successor run, not from the source run recorded inside the immutable envelope. The web-managed controller still uses deployed release `fdc9100f`, which predates that fix. Deliver the reviewed Checkpoint 1 code as a small independent, cumulatively reviewed AFlow plan so an exact-SHA green deployment can unblock the preserved Checkpoint 2 lineage. Do not alter the failed run's durable state or claim that this plan completes owner routing.

## Git Tracking

- Plan Branch: `aflow-aflow-resume-evidence-bootstrap-delivery-20260924-20260924-235625`
- Pre-Handoff Base HEAD: `9eb433e49829bb10e72db442466b2e9f6080bec5`
- Last Reviewed HEAD: `341656d534c751e4395c7932eaabdc98e43ba899`

## Review Log

- `cp1 v01`: Sol checkpoint review approved the scoped manager-context and test patch carried from `f47630a0` onto current main. The focused suite passed (130 tests); the worker's isolated full suite passed (2,205 tests and 239 subtests). Publication, CI, and live activation remain separate delivery gates.
- `aflow-review-final` (2026-09-25): Approved the entire accumulated delivery through `cp1 v01` (`341656d5`); no material findings. At review, 0 new commits since checkpoint approval and 64 since the unchanged pre-handoff base: 63 inherited main commits from the required fast-forward to `e3156de0`, plus this plan's one implementation/approval commit. The complete source/test patch matches `f47630a0` (stable patch ID `7c896720539def657ac55c87138b29abd72be9ed`); the only additional delivery change is the scoped DEVLOG entry. Reviewed one-/two-hop current-run authority, immutable envelopes, fail-closed copied evidence and schema-v1 compatibility. Independently verified 130 focused tests and the isolated full core suite (2,205 tests and 239 subtests); diff whitespace checks passed. No fix plan is needed.

## aflow-review-final

- [x] Cumulative implementation review approved without squash or history rewrite.
- Final review verification used `XDG_CONFIG_HOME=/tmp/aflow-final-bootstrap-xdg GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0=/dev/null uv run pytest -q --basetemp=/tmp/aflow-final-bootstrap-full`; 2,205 tests and 239 subtests passed in 215.61 seconds. The focused three-module suite also passed (130 tests).
- Delivery remains pending with the coordinator: fetched `origin/main` is now `b1226743`, two browser-test/DEVLOG commits beyond the worker's integration base. A final-review `git merge --ff-only origin/main` safely refused the diverged history and changed nothing. Integrate in a separate clean checkout, reconcile DEVLOG, and verify exact-SHA CI and deployment before any preserved CP2 resume. This approval does not claim publication, activation, or owner-routing completion.
- The primary checkout, original core worktree and original run state were not modified. No superseded fix plans exist for this handoff.

## Done Means

The reviewed `f47630a0` manager-context behavior and its regression tests are present on current main without overwriting newer code. One- and two-hop resumed repair overlays resolve the original current checkpoint from validated current-run evidence. Corrupt, missing, wrong-kind, wrong-size, escaping or mismatched evidence fails closed before a manager decision. Sol checkpoint review and Astra cumulative review approve this narrow delivery; publication, CI, deployment and resuming the preserved Checkpoint 2 run are separate coordinator gates.

## Critical Invariants

- Read root and `aflow/AGENTS.md`. Work only in the new AFlow-managed worktree. Preserve the dirty primary checkout, the original core feature worktree and all `.aflow` run/evidence files. The original two-checkpoint plan remains active with Checkpoint 2 unreviewed; never mark it done or edit its durable lineage.
- Before changes, `git fetch origin main`, `git merge --ff-only origin/main`, and verify `git cat-file -e f47630a0^{commit}`. The registered primary local `main` is older than remote; do not implement on that old base. If current main changed the same manager-context contract, reconcile without dropping either behavior and record the overlap.
- Use `f47630a0` as the reviewed source of truth for this narrow code/test change. Preserve immutable envelope bytes, digest/size/kind/containment validation, schema-v1 behavior, and read-only manager supervision.
- Do not reimplement or publish the unreviewed owner-routing Checkpoint 2 diff from the original core worktree in this plan. The real successor run may resume only after this fix is reviewed, published, and activated in the web-managed runtime.

## Forbidden Implementations

- Copy predecessor run files by hand, rewrite the scope envelope, disable manager validation, trust an envelope path as an arbitrary filesystem path, or silently treat invalid evidence as an empty plan.
- Reset the primary checkout, force-push, edit the active original plan's Checkpoint 2 state, or launch a duplicate controller against its preserved worktree.
- Claim the local editable uv tool changes the web-managed controller executable; verify the deployed release and exact runtime separately.

## Checkpoints

### [x] Checkpoint 1: Carry validated current-run evidence resolution onto main

**Goal:** Deliver only the already reviewed Checkpoint 1 behavior from `f47630a0` on the latest published source with current-main compatibility.

**Context:** Run `git rev-parse --show-toplevel`; inspect `git show --stat f47630a0`, `git diff f47630a0^ f47630a0 -- aflow/manager_context.py tests/test_manager_context.py`, current `aflow/manager_context.py`, and `tests/test_manager_context.py`. Inspect the original plan `plans/in-progress/aflow-resume-evidence-and-live-team-routing-20260924.md` only for scope provenance; do not copy its tracking state. Confirm the known failure in `.aflow/runs/20260924t210811z-019e7756/manager/` read-only if needed.

**Scope:** May modify `aflow/manager_context.py`, `tests/test_manager_context.py`, directly needed focused evidence tests, and `DEVLOG.md`. Must not modify `aflow/workflow.py` owner-routing code, `.aflow` durable state, UI components or deployment files.

**Steps:**

- [x] Compare current main with the two reviewed source files. Apply the exact functional patch from `f47630a0` for those files using a three-way-aware patch or explicit line reconciliation; preserve any newer main behavior. Do not import the original plan file or its checked Checkpoint 2 state. Verify the resulting diff contains only this evidence-resolution behavior and focused tests.
- [x] Prove one- and two-hop resume of a non-checkpoint reviewer overlay resolves copied plan/checkpoint bytes under the current run and reports `active_repair_plan=true`, the original current checkpoint, and no `invalid_evidence` parse error. Repeat negative wrong-kind/hash/size, missing, symlink/escape and scope-mismatch cases; validate before a manager launch. Preserve schema-v1 embedded evidence behavior.
- [x] Run focused manager/resume suites, then the full core suite under isolated `XDG_CONFIG_HOME` and `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0=/dev/null`. Record exact counts and diff in `DEVLOG.md`; check plan boxes only after evidence passes.

**Dependencies:** Reviewed source commit `f47630a0`. No dependency on unreviewed Checkpoint 2 implementation or UI CI repairs; publication and deployment remain gated by the exact current-main CI.

**Verification:** Run `uv run pytest -q tests/test_manager_context.py tests/test_resume_relocation.py tests/test_control_plane_resume.py`; run `XDG_CONFIG_HOME=/tmp/aflow-resume-bootstrap-xdg GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0=/dev/null uv run pytest -q --basetemp=/tmp/aflow-resume-bootstrap-py313`; run `git diff --check`, `git status --short`, `git diff --name-only`, and `git diff --stat`. The full suite may be repeated under Python 3.12 if the runner has it. No real owner run is resumed during tests.

**Done When:** The positive and negative evidence cases pass on the current source; the full suite passes; only the narrow manager-context behavior is submitted for checkpoint/final review; the original CP2 worktree and run files are unchanged.

**Blockers:** Stop and report if source commit `f47630a0` is unavailable, current main has an incompatible contract change that needs broader design, or existing dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

- Given a copied schema-v2 envelope after one or two resumes, when manager context reads a non-checkpoint repair overlay, it uses validated current-run digest artifacts and identifies the original checkpoint without altering envelope bytes.
- Given a missing, corrupt, wrong-kind, wrong-size, escaping or mismatched copied artifact, manager context reports invalid evidence before any decision and leaves predecessor artifacts intact.
- Given the preserved real CP2 run, this plan does not alter its state; only a later exact-SHA deployment of the approved fix can make a managed resume safe to attempt.

## Plan-to-Verification Matrix

- Current-run authority and overlay: focused manager-context/resume regressions.
- Fail-closed evidence: negative focused cases and full core suite.
- Scope isolation: staged diff and original worktree/run snapshots.
- Delivery boundary: approved commit, origin/main receipt, exact-SHA CI, deployed runtime identity, then separate resumed CP2 observation.

## Assumptions And Defaults

The source commit has Sol checkpoint approval, but no cumulative Astra approval as a standalone delivery. This plan supplies that final review without claiming the original two-checkpoint goal is done. A green exact-SHA CI and controlled deployment are required before resuming the preserved web-managed run; a local editable tool installation alone does not change its service runtime.

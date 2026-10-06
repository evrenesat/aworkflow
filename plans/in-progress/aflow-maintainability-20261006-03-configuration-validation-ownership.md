# AFLOW-MAINT-20261006 · 03/18 — Give configuration validation and editing clear owners

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 03 of 18
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A11
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Text and guided settings use one in-memory pair parser and validator, while the global service owns editing and the old project service becomes an explicit compatibility facade.

## MVP And Scope Boundary

**Now (MVP):** Text and guided settings use one in-memory pair parser and validator, while the global service owns editing and the old project service becomes an explicit compatibility facade.

**Later / Out of scope:** Removing supported imports, restoring per-project overrides, changing settings semantics or errors, changing the transaction protocol from plan 02, and broad TOML schema redesign.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 02: aflow-maintainability-20261006-02-configuration-crash-recovery.md

No outside implementation prerequisite was identified at authoring; recheck current active work and accepted main before dispatch.

These are launch prerequisites, not instructions to implement predecessor plans in this run. Ordinal filenames do not imply serial execution. Shared-file overlap alone is not a dependency: use isolated execution roots, preserve both accepted changes and serialize integration. Do not launch this member from an obsolete main or start another controller for its existing lineage.

## Context Bootstrap

Source review: c60a980796ed35c2d31f5a2a75dc5f1219789951. Conversion inspected current p100 main d2a9a5e96fe317191a30a0da2f35c10baaf7fdeb. The reviewed report's exact bytes are preserved in the linked source; subsequent accepted behavior takes precedence over historical line numbers.

From the assigned p100 execution checkout, run:

```bash
git rev-parse --show-toplevel
git status --short
git log -1 --format='%H %s'
rg --files --hidden -g AGENTS.md -g '!**/.git/**' aflow apps tests scripts docs plans
```

Read root and nearest applicable AGENTS.md before edits. Read UI_GUIDELINES.md for affected browser/client work. Inspect these owners and their directly named tests; files introduced by prerequisite plans must come from their delivered commits:

- `aflow/config.py`
- `apps/aflow_app/server/src/aflow_app_server/project_config_service.py`
- `apps/aflow_app/server/src/aflow_app_server/global_config_service.py`
- `apps/aflow_app/server/src/aflow_app_server/guided_config.py`
- `apps/aflow_app/server/src/aflow_app_server/config_response.py`
- `tests/test_config.py`
- `apps/aflow_app/server/tests/test_project_config_service.py`
- `apps/aflow_app/server/tests/test_guided_config.py`

## Decisions And Preserved Behavior

1. Add a pure parse_workflow_pair(aflow_text, workflows_text, *, source_dir) in the existing core configuration owner; absence of a sibling stays distinguishable from an empty sibling. Relative worktree paths use the selected source directory, never a temporary directory.
2. The filesystem loader reads through plan 02's pair owner, then invokes this parser. The server validates submitted text through the same parser and one semantic pass without creating candidate files.
3. Move server report/revision/bounds helpers into config_validation.py and current file operations into the plan 02 owner or a thin config_documents.py adapter. Preserve named exception classes, report shapes, bounded messages and document/line diagnostics through compatibility re-exports.
4. Retain ProjectConfigService as a thin backwards-compatible facade because external use was not disproved. No active router imports it to own project-specific editing; remove duplicated implementation, not the import path.

## Done Means

- The stated user/developer path works through the real affected boundary and its distinguishing acceptance cases pass.
- Changed functions/modules have explicit small input/result contracts and responsibilities; moving a large closure into a wrapper or shared-all-locals context is not completion.
- The complete Final Verification gate passes for the relevant source/dependency/environment state. Local verification, remote publication, CI and live acceptance are recorded separately.
- Approved implementation reaches origin/main through the existing delivery path, with exact-commit CI and applicable activation/health evidence. This authoring task only queues the plan.

## Critical Invariants

- Preserve exact run/project/plan/checkpoint/worktree identities, durable lineage and unknown ownership states. Read projections cannot become execution authority.
- Preserve supported public signatures, wire and saved-state shapes unless a decision above explicitly names the permitted internal adapter.
- Preserve existing current-source config, explicit team/budget choices, review obligations and receipt-backed publication.
- Keep state/process/file ownership with the named domain; preserve unrelated changes and active controllers.

## Forbidden Implementations

- A parallel source of state truth, generic framework, new scheduler/database or catch-all mutable context.
- Weakened validation, cast-to-Any escapes, disabled existing checks or changed tests that merely approve a regression.
- Broad cleanup, reset, force-push, shared editable-tool repointing, live owner-setting edits or restarting another run to simplify verification.
- Treating a worker exit, completed checkbox or successful unit test as proof of review, publication or deployed usability.

## Checkpoints

### [ ] Checkpoint 1: Make candidate validation a pure shared path

**Goal:** Filesystem and submitted-text inputs produce equivalent configuration and diagnostics.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify aflow/config.py, new server config_validation.py and their focused tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Extract TOML parsing, sibling merge and semantic validation into the specified pure function. Keep legacy no-file defaults in the filesystem wrapper and preserve relative-root resolution.
- [ ] Change validate_candidate_pair to call the pure parser once; remove the temporary-directory write/read and duplicate validate_workflow_config pass.
- [ ] Test valid pair, missing sibling, cross-document missing prompt, syntax line errors, size/NUL bounds and relative path behavior. Spy on filesystem creation to prove submitted-text validation performs no candidate writes.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run pytest -q tests/test_config.py tests/test_live_config.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_project_config_service.py apps/aflow_app/server/tests/test_guided_config.py
```

**Expected result:** Equivalent inputs have equivalent reports; in-memory validation creates zero temporary candidate files.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Remove legacy implementation ownership while keeping imports compatible

**Goal:** The global editor and compatibility facade visibly delegate to current owners.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify project_config_service.py, global_config_service.py, config_response.py, guided_config.py and importing server modules. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Replace current private helper imports from project_config_service with public current-owner imports; preserve old names only as forwarding aliases/facades used by compatibility callers.
- [ ] Keep project-edit facade behavior under its existing tests; do not register new routes or reinterpret global configuration as project overrides.
- [ ] Add an import-boundary test that active global configuration code does not depend on legacy project editing. Update docs/configuration.md, ARCHITECTURE.md, nearby server AGENTS.md only where ownership changed, and DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_project_config_service.py apps/aflow_app/server/tests/test_global_config_patch.py apps/aflow_app/server/tests/test_guided_config.py apps/aflow_app/server/tests/test_mcp.py -k 'config'
```

```bash
uv run pytest -q tests/test_config_pair.py tests/test_config.py
```

**Expected result:** Global text/guided/MCP edits share one parser and transaction; legacy imports still work without duplicated state or new active routes.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run pytest -q tests/test_config_pair.py tests/test_config.py tests/test_live_config.py tests/test_run_config_snapshot.py
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_project_config_service.py apps/aflow_app/server/tests/test_global_config_patch.py apps/aflow_app/server/tests/test_guided_config.py apps/aflow_app/server/tests/test_mcp.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/03/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/03/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Open global Settings through the disposable HTTP app, validate text and guided edits of the same pair, and observe equivalent reports and revision conflicts. After deployment, verify real Settings loads without saving owner changes.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given the same cross-document invalid reference arrives as text, guided edits or filesystem input, the observable result is: One semantic validator rejects it with equivalent bounded diagnostics.
- Given old code imports ProjectConfigService or its exception types, the observable result is: The compatibility facade still works; active global routes do not use obsolete ownership.
- Given candidate contains a relative worktree root, the observable result is: It resolves against the supplied source directory without candidate files.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| The same cross-document invalid reference arrives as text, guided edits or filesystem input | Checkpoint 1 parser equivalence tests |
| Old code imports ProjectConfigService or its exception types | Checkpoint 2 facade/import tests |
| Candidate contains a relative worktree root | Checkpoint 1 pure parser test |

## Documentation Impact

Document current parse/transaction/editing owners and label the legacy import facade as compatibility-only. Preserve operational recovery notes.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/03/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

For builds/browser/dependency writes, use a conservative estimate of up to 2 GiB additional allocation per active member (wheel, isolated environment and captures, not a measured result), plus the required 20 GiB free reserve and 5% free inodes. Reuse valid caches and account for concurrent members. Before heavy writes, run:

```bash
df -B1 . "${TMPDIR:-/tmp}"
df -i . "${TMPDIR:-/tmp}"
ssh -F ~/.ssh/storage-headroom.conf storage-backing
```

Check actual output mounts and shared backing headroom; stop heavy allocation at 80% backing metadata use. If the estimate is insufficient, record a revised evidence-backed requirement before allocating. Remove only this member's disposable scratch after extracting proof; preserve failed/unique artifacts and lineage.

## Assumptions And Defaults

- The task is a behavior-preserving maintainability effort except for explicitly named correctness/quality/performance repairs.
- Python standard-library dataclasses and the existing React/FastAPI stack remain the defaults; only explicitly specified development tools may be added.
- Upstream/other-plan fixes already delivered at execution time are prerequisites to preserve, not changes to replay or undo.
- The original review is evidence and scope input, not authority to implement excluded ideas or operate production.
- This plan is self-contained for its member scope; the index supplies effort coordination, not missing behavior decisions.

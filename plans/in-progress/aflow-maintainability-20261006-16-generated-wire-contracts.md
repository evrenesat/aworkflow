# AFLOW-MAINT-20261006 · 16/18 · Group B — Generate client wire types from server schemas

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 16 of 18
- Pairing group: `AFLOW-MAINT-20261006-B`
- Group position: 06 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A09
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

A server wire-schema change updates generated TypeScript types or fails CI, while domain models and public REST/MCP meanings remain independent and compatible.

## MVP And Scope Boundary

**Now (MVP):** A server wire-schema change updates generated TypeScript types or fails CI, while domain models and public REST/MCP meanings remain independent and compatible.

**Later / Out of scope:** Generating domain models from HTTP, changing endpoints, replacing the API client, adding blanket runtime validation, another runtime schema library or deleting compatible optional fields.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md; 06: aflow-maintainability-20261006-06-supervision-state-codecs.md; 15: aflow-maintainability-20261006-15-server-app-composition.md

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group B:** Requires 06's completed scoped type-check gate and 15's app factory. Preserve that CI gate while adding schema checks; Group A has handed off CI and broad transport test edits by this point. Canonical control-plane wire models and UI types are Group B's boundary; do not change durable codec schemas or runtime APIs to make generation pass.

The preferred group position is not an extra dependency. The concierge chooses one ready member from each group; never start this member while its prerequisite is merely implementing or in review. Delivery requires accepted origin/main commits, required CI and applicable live acceptance. Check the [effort pairing index](../notes/aflow-maintainability-20261006-index.md) for the current handoff and hold evidence.

Checkpoint scope lists are upper bounds narrowed by the pairing boundary above. Do not reopen files handed to the other group or add a cross-group production edit without first recording a concrete ordering handoff. Shared domain documentation still updates with each delivery; preserve both sides and serialize integration. Use an isolated execution root and one controller for the member's lineage. These prerequisites do not authorize implementing any other plan in this run.

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

- `apps/aflow_app/server/src/aflow_app_server/models.py`
- `aflow/control_plane/models.py`
- `apps/aflow_app/web/src/types.ts`
- `apps/aflow_app/web/src/api.ts`
- `apps/aflow_app/web/package.json`
- `apps/aflow_app/web/package-lock.json`
- `.github/workflows/ci.yml`
- `apps/aflow_app/server/tests/test_control_plane_api.py`
- `apps/aflow_app/server/tests/test_mcp.py`

## Decisions And Preserved Behavior

1. The existing Pydantic transport models plus FastAPI route declarations are the wire-schema source. Export OpenAPI offline from create_app(...).openapi() without entering lifespan, reading owner secrets, scanning projects or starting the plan consumer.
2. Add only the development generator openapi-typescript@7.13.0 (registry version verified during conversion) pinned exactly in package.json/lock. Use the documented local-file CLI with --output and --check; no remote schema fetch during build. Reference: https://openapi-ts.dev/cli.
3. Create scripts/export_wire_schema.py to write deterministic sorted OpenAPI JSON to apps/aflow_app/web/openapi.json and support --check. Generate apps/aflow_app/web/src/wireTypes.ts. types.ts keeps UI-only types and stable exported aliases to generated components instead of duplicate wire interfaces.
4. Preserve omitted versus nullable optional fields, nested progress/recovery payloads, enum literals and request/response distinctions. Existing canonical-to-transport adapters remain explicit; no Any casts to hide mismatches.
5. REST and MCP share the same underlying meanings and contract fixtures. Target runtime validation only at current independently versioned/untrusted boundaries; generation does not prove JSON validity at runtime.

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

### [ ] Checkpoint 1: Export one deterministic offline schema and generate types

**Goal:** Reproducible generated wire definitions match the server's public models.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify scripts/export_wire_schema.py, web/openapi.json, web/src/wireTypes.ts, package manifests and new server tests/test_wire_schema.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Export the schema through plan 15's side-effect-free app factory. Assert offline export creates no services or registry/config files; retain stable operation/model names.
- [ ] Install the exact dev dependency and add package scripts generate:wire and check:wire invoking the local generator. Use --default-non-nullable false so defaulted optional properties are not silently strengthened.
- [ ] Commit deterministic schema/types, add --check mode that fails on drift, and test nested nullable/omitted properties and representative request/response shapes.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run --project apps/aflow_app/server python scripts/export_wire_schema.py
```

```bash
npm --prefix apps/aflow_app/web run generate:wire
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_wire_schema.py
```

```bash
uv run --project apps/aflow_app/server python scripts/export_wire_schema.py --check
```

```bash
npm --prefix apps/aflow_app/web run check:wire
```

**Expected result:** Two generations are byte-identical, offline export has no runtime side effects, and stale output fails checks.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Migrate client wire aliases and enforce drift checks

**Goal:** The compiler observes public schema changes without duplicating handwritten wire shapes.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify types.ts, api.ts, directly affected client consumers/tests and existing CI workflow. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Replace only wire interfaces in types.ts with stable aliases to generated components; retain UI drafts/presentation types locally. Keep current API wrapper and transport/domain adapters.
- [ ] Expand canonical/transport contract tests for nested progress, resume/recovery and missing optional fields across REST/MCP. Preserve partial-data UI behavior rather than casting through unknown/Any.
- [ ] Add schema export --check and npm run check:wire to existing CI after dependencies are available; retain lint/test/build/browser/package gates. Document regeneration in README and ownership in ARCHITECTURE.md/DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
npm --prefix apps/aflow_app/web run check:wire
```

```bash
npm --prefix apps/aflow_app/web test -- --run src/api.test.ts src/components/RunDashboard.test.tsx
```

```bash
npm --prefix apps/aflow_app/web run build
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_wire_schema.py apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py
```

**Expected result:** A nested schema edit either updates aliases/consumers or fails generation/type checks; absent supported fields remain supported.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src scripts/export_wire_schema.py
```

```bash
uv run --project apps/aflow_app/server python scripts/export_wire_schema.py --check
```

```bash
npm --prefix apps/aflow_app/web run check:wire
```

```bash
npm --prefix apps/aflow_app/web run lint
```

```bash
npm --prefix apps/aflow_app/web test -- --run
```

```bash
npm --prefix apps/aflow_app/web run build
```

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_wire_schema.py apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/16/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/16/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Run representative disposable REST and MCP reads of one run and compare public nested meanings; build and load the installed client against that server. Existing post-deploy read-only run detail verifies the generated client still handles current and historical data.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given a nested optional progress field is added to the server schema, the observable result is: Generation changes the committed client type or the drift gate fails.
- Given a supported old response omits an optional recovery field, the observable result is: The generated type and current client handling still accept the shape.
- Given schema export runs in an empty disposable environment, the observable result is: It creates no runtime services, reads no owner credentials and makes no network calls.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| A nested optional progress field is added to the server schema | Schema/generator check and build |
| A supported old response omits an optional recovery field | Nested compatibility fixtures and API tests |
| Schema export runs in an empty disposable environment | Offline-export side-effect test |

## Documentation Impact

Document generated source/output locations, exact regeneration/check commands, and domain-versus-wire ownership.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/16/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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

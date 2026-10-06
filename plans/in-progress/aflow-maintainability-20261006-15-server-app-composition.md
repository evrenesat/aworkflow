# AFLOW-MAINT-20261006 · 15/18 · Group B — Compose isolated server apps and cohesive routers

- Effort ID: `AFLOW-MAINT-20261006`
- Member: 15 of 18
- Pairing group: `AFLOW-MAINT-20261006-B`
- Group position: 04 of 09 (preference among ready members)
- Effort: AFlow maintainability improvement, initiated 6 October 2026
- Source findings: A13
- Priority: P2
- Effort index: [single-effort index](../notes/aflow-maintainability-20261006-index.md)
- Source: [preserved architecture review](../notes/aflow-maintainability-20261006-source-review.md)

**Authoring state:** queued in p100's in-progress stack; all checkpoints are unexecuted. This metadata identifies one shared maintainability effort, not 18 unrelated proposals. Plan checkboxes, run evidence and publication receipts determine later progress.

## Summary

Each server app owns its services and credentials through its lifespan, so independent app instances and route tests cannot leak state into one another.

## MVP And Scope Boundary

**Now (MVP):** Each server app owns its services and credentials through its lifespan, so independent app instances and route tests cannot leak state into one another.

**Later / Out of scope:** Async service rewrites, a dependency-injection framework, new authentication rules, changing scheduler/control-plane ownership, deployment topology or worker lifetime.

## Git Tracking

- Plan Branch: ``
- Pre-Handoff Base HEAD: ``

## Dependencies And Integration

Required effort predecessors: 01: aflow-maintainability-20261006-01-verification-baseline.md; 04: aflow-maintainability-20261006-04-transport-neutral-resume.md

No additional outside prerequisite is named for this member. Recheck current active work, accepted main and the pairing index before dispatch; a newly overlapping active change is a hold to reconcile, not permission to overwrite it.

**Pairing boundary — Group B:** Requires 04's delivered transport test migration before app/route extraction changes the same REST/MCP fixtures. Keep core resume/configuration/projection implementations as imported public services; do not rewrite them. Group A members 05–09/17 then pair through those stable contracts.

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

- `apps/aflow_app/server/src/aflow_app_server/main.py`
- `apps/aflow_app/server/src/aflow_app_server/mcp_adapter.py`
- `apps/aflow_app/server/src/aflow_app_server/browser_session.py`
- `apps/aflow_app/server/tests/test_auth.py`
- `apps/aflow_app/server/tests/test_control_plane_api.py`
- `apps/aflow_app/server/tests/test_mcp.py`
- `aflow/ui_cli.py`

## Decisions And Preserved Behavior

1. Add create_app(config=None, *, credential_provider=None, service_factory=None) with a lifespan-owned AppServices record on app.state. The service factory is one test/composition seam, not a plugin system. Configuration defaults are resolved for that app instance only. Constructing create_app or importing main.app is side-effect-free: defer environment defaults, credential-file reads, registry I/O and service/scanner startup until that app's lifespan.
2. Preserve main.app, run_server and configure_server compatibility for installed entry points; configure_server configures only the default app before its lifespan. Explicit create_app instances are independent. Move runtime globals, including probe-dedup state, to the owning app.
3. Dependencies obtain services from Request.app; split sibling routers into runs, projects/plans, config/skills and sessions. Keep authentication/middleware and MCP composition in the factory; route handlers continue calling existing shared services.
4. Every app creates its own MCP mount with header-only authentication. Cookie sessions still use the current credential provider, exact same-origin unsafe-method checks, current expiry/activity refresh and no-store endpoints. UI shutdown still releases scanner ownership without killing workflow workers.
5. Keep API read reconciliation read-only and SSE blocking work offloaded as already implemented. Preserve every route, response model/status, OpenAPI operation identity and static fallback.

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

### [ ] Checkpoint 1: Introduce app-local service ownership

**Goal:** Two app instances can run together without sharing services or credentials.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify main.py, new app_services.py and new tests/test_app_factory.py. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Create the app factory and lifespan container; wire config, credential provider, registry, plan/global config/control-plane services and plan consumer into one app-local record.
- [ ] Keep default entry-point wrappers compatible; migrate direct global assignment in tests to explicit app instances or the single service-factory seam.
- [ ] Add concurrent two-app tests with different roots and tokens, credential rotation, startup failure and shutdown. Assert one app's cleanup does not affect the other or kill its worker.

**Dependencies:** Required effort predecessors and external prerequisites above.

**Verification:**

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_app_factory.py apps/aflow_app/server/tests/test_auth.py apps/aflow_app/server/tests/test_mcp.py
```

```bash
uv run pytest -q tests/test_ui_cli.py tests/test_persistent_units.py
```

**Expected result:** Instances are isolated; default installed entry points still work and worker lifetime stays independent of UI lifespan.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

### [ ] Checkpoint 2: Move cohesive route families behind app dependencies

**Goal:** A route family's behavior can be read and tested without all server composition code.

**Context:** Inspect the owners named below with their existing callers/tests and the decisions above. Run `git rev-parse --show-toplevel` before editing.

**Scope:** May create/modify New sibling runs_routes.py, projects_routes.py, config_routes.py, session_routes.py; main.py and route tests. Must not touch unrelated behavior, live controllers, owner settings or another plan's implementation. Documentation changes are limited to the ownership/behavior changed here.

**Steps:**

- [ ] Move routes/adapters by the stated families, retaining exact paths, response schemas, exception/redaction handling and HTTP/MCP service equivalence.
- [ ] Keep authentication/MCP/static fallbacks at composition and shared reusable response adaptation in its current owner; route modules must not import mutable main globals.
- [ ] Run cookie/origin/header-only MCP, SSE, read-only cold reconciliation and startup/stop/resume tests. Update server AGENTS.md, ARCHITECTURE.md and DEVLOG.md.

**Dependencies:** Checkpoint 1 of this plan, plus the plan-level prerequisites.

**Verification:**

```bash
uv run --project apps/aflow_app/server pytest -q apps/aflow_app/server/tests/test_app_factory.py apps/aflow_app/server/tests/test_auth.py apps/aflow_app/server/tests/test_control_plane_api.py apps/aflow_app/server/tests/test_mcp.py apps/aflow_app/server/tests/test_plan_store.py apps/aflow_app/server/tests/test_skills_api.py
```

**Expected result:** Routes retain public meanings and isolation; no transport directly acquires workflow lifetime or repairs state on GET.

**Done When:** The goal and expected result are demonstrated, every checked step has code/test/observable evidence, and changed files remain in scope. Before handoff run `git status --short`, `git diff --name-only` and `git diff --stat`.

**Blockers:** Stop and report a genuinely unresolved contract/authority conflict, failed prerequisite or ambiguous unrelated dirty-file ownership. A recoverable test/tool failure is work to diagnose and repair within this scope; do not change acceptance or bypass it.

## Final Verification

The execution agent completes this cumulative gate after the final checkpoint's code changes and before whole-plan approval. Use focused commands within earlier checkpoints. Reuse inspectable passing evidence only when source, tests, dependencies, configuration and environment fingerprints still match; do not rerun the same broad gate at every checkpoint. A future CI run does not replace this local gate.

From the execution root, run:

```bash
uv run ruff check aflow apps/aflow_app/server/src
```

```bash
uv run --project apps/aflow_app/server pytest -q
```

```bash
uv run pytest -q tests/test_ui_cli.py tests/test_ui_assets.py tests/test_smoke_ui.py tests/test_persistent_units.py tests/test_control_plane_reconciliation.py
```

```bash
npm --prefix apps/aflow_app/web run build
```

```bash
git diff --check
```

All commands must exit successfully with the relevant tests executed, not skipped because fixtures or browsers are missing. Existing CI OS/Python/browser/packaging gates remain required; these commands do not claim cross-platform execution locally.

Verify installed packaging once for the accepted cumulative code state:

```bash
uv build --wheel --out-dir tmp/aflow-maintainability-20261006/15/dist
uv run python scripts/smoke_ui.py --wheel tmp/aflow-maintainability-20261006/15/dist/aworkflow-*.whl
```

Use a task-owned output directory containing one current wheel; the smoke helper installs into its disposable environment. Do not change the shared AFlow editable installation to run this check.

**Real user/deployed surface:** Run the installed-wheel smoke on a disposable non-production app, including login/cookie and MCP discovery. After existing deployment, confirm real login, project listing and header-authenticated MCP discovery without starting a run.

After approved publication, the integration owner checks exact-SHA CI and the existing CI-gated p100 deployment/health evidence. Use its established rollback path on a demonstrated delivery regression; do not create a replacement deployment service. No manual restart or provider work is implied by authoring this plan.

## Behavioral Acceptance Tests

- Given two apps have distinct tokens, registries and roots, the observable result is: Each accepts only its own credentials and sees only its own services.
- Given uI lifespan ends while an owned workflow runs, the observable result is: Scanner ownership is released; worker lifetime continues under existing unit ownership.
- Given cookie-authenticated request targets MCP or an unsafe cross-origin route, the observable result is: Existing header-only MCP and same-origin cookie rules still reject it.
- Given a cold GET reads old evidence, the observable result is: It projects state without repair or process launch.

## Plan-to-Verification Matrix

| Requirement | Concrete evidence |
| --- | --- |
| Two apps have distinct tokens, registries and roots | Two-app factory/auth tests |
| UI lifespan ends while an owned workflow runs | Persistent-unit and app shutdown tests |
| Cookie-authenticated request targets MCP or an unsafe cross-origin route | Auth/MCP tests |
| A cold GET reads old evidence | Reconciliation tests |

## Documentation Impact

Show app factory/lifespan/service/transport ownership and preserve existing authentication and worker-lifetime invariants.

Update documentation in the same checkpoint as its changed behavior. Root AGENTS.md is outside scope. Keep the effort ID in the plan, commits/PR description and concise verification evidence so the concierge can reconcile this effort without title guessing.

## Storage And Evidence

Owned scratch/proof: `tmp/aflow-maintainability-20261006/15/`; use one bounded verification log with commands, outcomes, source/dirty/dependency fingerprints and relevant hashes. Ordinary new proof target ≤100 MiB; retain failure evidence until diagnosed. Do not copy inherited run/session evidence or remove other owners' artifacts.

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

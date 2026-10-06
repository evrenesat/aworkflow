# AFlow architecture and maintainability analysis

Reviewed 5 October 2026. Source revision: `c60a980796ed35c2d31f5a2a75dc5f1219789951`.

AFlow has substantial, fixable architectural debt. Its main execution and interface modules have accumulated too many responsibilities, and understanding a change often requires following state through several large files. This makes maintenance harder for people and coding agents and increases the chance that a local fix breaks recovery, ownership, or another interface.

There is also a useful foundation: explicit workflow contracts, durable receipts, conservative admission checks, transport services, and extensive regression tests. Preserve those protections while making their implementation smaller and easier to understand. An incremental refactor within the existing architecture is the strongest direction supported by this review.

**The primary goal is smaller, cohesive functions, classes, and files with explicit inputs, outputs, and ownership.** Success means an agent can understand and change one behavior by reading a small, clearly related set of modules. Moving an oversized function into a class, splitting it into helpers that all mutate the same large context, or distributing it across arbitrary files does not achieve that goal.

This is analysis for subsequent AFlow checkpoint plans. It does not authorize or start implementation. Application code, runtime configuration, live controllers, and deployment were not changed.

## Main conclusions

1. **Make the workflow controller and dashboard decomposable first.** They are both large and frequently changed. Their intertwined state makes them the largest continuing maintenance cost.
2. **Move resume behavior out of the CLI and give durable state explicit codecs.** The daemon currently imports private CLI functions to reconstruct execution. State validation and reconstruction span several overlapping layers.
3. **Fix one reproduced correctness problem before broad refactoring.** Saving the two configuration files is protected against concurrent cooperating readers and ordinary replacement errors, but not process death between replacements.
4. **Reduce the work hidden behind read APIs.** Progress assembly mixes evidence discovery, compatibility, interpretation, caching, and presentation. Event reads and appends parse the complete journal even when returning one event.
5. **Make the existing quality checks dependable.** Web lint fails, CI omits it, and some integration fixtures depend on the operator's Git configuration. These are concrete gaps in the feedback developers receive.

## Scope and evidence

The review covered the major runtime boundaries through source inspection, a tracked-source inventory, Python and TypeScript syntax-tree measurements, selected change-history inspection, focused tests, and two disposable experiments. This was a repository-wide architectural review with deeper inspection of high-risk paths; it was not a line-by-line audit of every source file.

The source checkout was clean at the start and remained clean for tracked application files. Review artifacts are in `plans/research`, outside the automatic plan-consumer directories. That directory is locally ignored by Git; these are local research artifacts, not published changes.

The [evidence record](/root/code/agent_flow/plans/research/aflow-architecture-review-20261005-evidence.txt) preserves commands, results, source and dependency-file hashes, and the probe programs. The experiments used only disposable files and processes under `/tmp/aflow-architecture-audit-20261005-b4ijvzbb`.

| Area | Coverage and practical limit |
| --- | --- |
| Workflow execution | Startup, turn boundaries, manager integration, recovery, resource admission, completion and publication paths inspected. No real provider run launched. |
| Durable state and recovery | State records, serializers, strict and tolerant decoding, CLI resume reconstruction, daemon admission and worktree evidence inspected. |
| Control plane | Repository, activity projection, progress assembly, journals, unit ownership, scheduling and service composition inspected. Selected regression tests run. |
| Configuration | Loaders, live pair locking, guided/global services and legacy project service inspected. Process-death failure reproduced. |
| Web application | Dashboard, settings ownership, API client, history/refresh flows, shared presentation and build/lint configuration inspected. All client tests run. |
| Harnesses and supervision | Adapter/session contracts, driver discovery, ACP implementations, manager context and repartition boundaries sampled. Provider-specific live behavior remains unverified. |
| Packaging and operations | Manifests, CI, wheel build hook, asset resolution, deployment gates, issue intake and concierge boundaries inspected. No release build or deployment attempted. |
| Security boundaries | Existing authentication, containment, redaction and ownership responsibilities considered during architectural review. This is not a security certification. |

No current production latency or deployed usability is established by these tests. Existing latency research was consulted for context; its historical measurements are not presented as measurements of this revision. Real-browser acceptance remains necessary for future UI refactoring.

## Size and change concentration

Counts below are physical source lines, including comments and blank lines, from tracked files at the reviewed revision. They are indicators of reading and change cost, not standalone proof of poor design.

- The core package contains **86,382 Python lines across 87 files**, excluding bundled skill scripts. Six bundled Python scripts add 3,842 lines.
- The server contains **10,150 Python lines across 22 source files**.
- The client contains **19,073 TypeScript and TSX lines across 42 non-test files**, plus 3,656 CSS lines.
- Root and server Python test trees contain **138,133 lines across 119 files**, including support files. Thirty client test files add **18,129 lines**.

| High-cost source | Measured concentration |
| --- | --- |
| [Workflow](/root/code/agent_flow/aflow/workflow.py:8612) | 16,579 lines; execution function 7,885 lines |
| [CLI](/root/code/agent_flow/aflow/cli.py:1312) | 5,670 lines; repartition resume validator 607 lines |
| [Run progress](/root/code/agent_flow/aflow/control_plane/run_progress.py:4486) | 5,498 lines; detail reducer 823 lines |
| [Concierge](/root/code/agent_flow/aflow/concierge.py:1272) | 5,185 lines; triage function 592 lines |
| [Daemon](/root/code/agent_flow/aflow/daemon.py:425) | 5,038 lines; service class 3,122 lines |
| [Run dashboard](/root/code/agent_flow/apps/aflow_app/web/src/components/RunDashboard.tsx:1028) | 4,405 lines; main component 3,378 lines |
| [Manager context](/root/code/agent_flow/aflow/manager_context.py:1998) | 2,830 lines; context builder 833 lines |
| [Server entry point](/root/code/agent_flow/apps/aflow_app/server/src/aflow_app_server/main.py:566) | 2,254 lines; routes, auth and service lifecycle together |
| [Global settings](/root/code/agent_flow/apps/aflow_app/web/src/components/GlobalSettings.tsx:61) | 1,151 lines; main component 1,091 lines |

The workflow execution function includes 61 nested functions, 421 `if` nodes and 101 exception-handler nodes. These counts include its nested helpers; they are not a formal cyclomatic-complexity score. Existing extraction classes remain large: `_ManagerCallExecutor` spans 724 lines and `_ManagerGateCoordinator` spans 613.

The dashboard file contains 80 `useState` calls, 50 `useRef` calls, 28 `useEffect` calls and three layout effects. These are syntax-tree counts for the whole file, including its smaller helper components. Global settings contains 50 state calls and 11 refs.

Among the last 200 non-merge commits reachable from the reviewed revision, the dashboard changed in 33, workflow in 25, global settings in 14, and the server entry point in 12. This supports prioritizing the largest frequently edited modules over spending the first refactoring effort on small, stable adapters.

## Architecture worth preserving

The existing execution ownership is largely sensible:

- **The controller owns workflow truth.** Plans, `run.json`, finalized turn artifacts and publication receipts drive execution and recovery.
- **Admission and unit ownership are separate concerns.** A web request reserves and launches through services; it does not become the workflow process's lifetime owner. Unknown ownership is not treated as permission to start again.
- **HTTP and MCP share application services.** The shared MCP registry and control-plane services already prevent much transport divergence.
- **Read projections are intentionally weaker than execution authority.** Historical or incomplete evidence can still be displayed without authorizing unsafe recovery.
- **Publication is a distinct, receipt-backed phase.** Completing implementation, publishing code, moving the plan and activating a release are separate facts.
- **Tests exercise meaningful contracts.** Existing suites include idempotency, conflicting ownership, partial evidence, worktree recovery, process lifetime, publication and browser-state preservation.

These responsibilities require real complexity. Refactoring should make them visible and independently testable. It should preserve the uncertainty states, process-birth and nonce checks, exact plan identities, durable ordering, and explicit user decisions that protect work.

## Ranked findings

“High” identifies the most valuable early work; only findings explicitly described as reproduced failures establish incorrect behavior. Structural debt identifies a concrete maintenance mechanism without claiming an observed production incident. Effort estimates are relative: small is a bounded repair, medium spans a few interfaces, and large needs several separately reviewed plans.

### A01 Configuration pair saves do not survive process death

**Priority: High. Evidence: reproduced correctness failure. Effort: medium.**

[`_commit_pair_locked`](/root/code/agent_flow/apps/aflow_app/server/src/aflow_app_server/global_config_service.py:221) stages both files, replaces `aflow.toml`, then replaces `workflows.toml`. It restores the first file if the second replacement raises `OSError`. The [pair lock](/root/code/agent_flow/aflow/run_config_snapshot.py:74) serializes cooperating callers, but cannot run rollback after the saving process dies.

The disposable experiment used an initially valid pair, then renamed a prompt in both documents. A child process exited immediately before the second replacement. The first file contained the new prompt name; the second still referenced the old name. A subsequent service read returned `invalid`, and `load_live_config` raised `ConfigError` for the unknown prompt. The ordinary rollback tests do not cover this process-death boundary.

This can prevent launches and live configuration reloads after an interrupted settings save. If both mixed documents happen to remain valid, a different possible outcome is an unintended combination of settings; that latter outcome was not separately reproduced.

**Direction:** Give the configuration pair one shared transaction owner with durable recovery, preserving the editable TOML contract. A small write-ahead transaction record with retained prior bytes is a candidate; generation-based publication is another design option if its effect on manual editing is acceptable. The later plan must select one approach and route relevant live readers through it.

**Proof required:** Interrupt the saving process at each publication boundary and verify that recovery produces an entirely old or entirely new pair before it can drive execution. Preserve revision conflicts, rejected-write byte preservation, comments, and current-source reload behavior. Keep this correction separate from a large configuration cleanup.

### A02 The workflow controller is an oversized mutable coordinator

**Priority: High. Evidence: structural debt. Effort: large.**

[`_run_workflow_unchecked`](/root/code/agent_flow/aflow/workflow.py:8612) handles startup, lifecycle preparation, admission handoff, resume restoration, live configuration, control overrides, recovery, manager decisions, harness sessions, resource leases, transitions, terminal reporting and delivery. Its nested helpers close over shared mutable state and use `nonlocal` assignments for active paths, step selection and configuration.

The resulting problem is change coupling: a local change to when a turn starts or finishes must also respect manager accounting, a pending recovery transaction, ownership of a resource lease and the metadata written at that point. Large coordinator classes already extracted from this file still contain methods hundreds of lines long, so another wrapper class alone would preserve the problem.

**Direction:** Reduce the public workflow function to a readable sequence of phases. Extract cohesive behavior around lifecycle preparation, boundary decisions, one turn's execution, checkpoint/review progression, and completion. Give each part a small input/result contract and make state changes explicit. Keep pure decisions separate from subprocess and filesystem effects where doing so simplifies the existing code.

Begin with behavior that already has a clear boundary and tests. Keep public entry points stable while callers migrate. Avoid a new context object that merely exposes all current locals to every helper, or a coordinator constructor with dozens of callbacks.

**Proof required:** Existing turn, retry, manager, owner-stop, hotplug, exclusive-resource, budget, resume and publication behavior stays intact. Each extraction must reduce the amount of code needed to understand its responsibility and avoid adding another state authority.

### A03 Resume belongs to an application service rather than the CLI

**Priority: High. Evidence: concrete dependency inversion. Effort: medium to large.**

The [daemon resume bootstrap](/root/code/agent_flow/aflow/daemon.py:3353) imports private `_bootstrap_resume_invocation` from `cli.py`; worker preparation and recovery also import CLI reconstruction functions. The CLI in turn imports private workflow helpers. Even [startup-question validity in the observer layer](/root/code/agent_flow/aflow/control_plane/run_activity.py:44) imports private daemon parsing functions.

This means transport-independent behavior is owned by an entry-point module. Changing argument resolution or reorganizing CLI code can affect daemon recovery. Tests reinforce this dependency by patching CLI internals to control HTTP/MCP scenarios. A static import inventory found a 25-module strongly connected group when local and type-checking imports are included; this is evidence of conceptual coupling, not proof of a runtime import failure.

**Direction:** Extract a transport-neutral resume service and its typed request/result contracts. CLI argument handling translates into that service; daemon and worker paths call it directly. Move shared startup-record parsing to a lower-level record/codec owner. Preserve one implementation of plan/worktree/budget validation rather than creating separate CLI and daemon versions.

The large [resume bootstrap](/root/code/agent_flow/aflow/cli.py:1312) and [context reconstruction](/root/code/agent_flow/aflow/cli.py:3807) should themselves be separated by responsibility during this work, not moved unchanged into one giant resume module.

**Proof required:** The same saved run yields equivalent continuation decisions through CLI, REST and MCP. No application or observer module imports the CLI. Tests continue to distinguish safe continuation, uncertain liveness, incompatible scope and completion-only recovery.

### A04 Durable state has too many manual representations

**Priority: High. Evidence: structural debt. Effort: large, incremental.**

[`ControllerState`](/root/code/agent_flow/aflow/run_state.py:936) has 60 annotated fields, 34 allowing `None`. [`ResumeContext`](/root/code/agent_flow/aflow/run_state.py:779) has 62, 41 allowing `None`. Manager state is serialized, restored into a controller state, projected into resume fields, and checked again by a separate strict validator. `manager_resume_fields_strict` validates the input and then calls the tolerant projection. The [metadata writer](/root/code/agent_flow/aflow/runlog.py:1455) also contains a long series of field-specific preservation rules.

Optional fields are not inherently wrong, and strict admission versus tolerant historical display is an intentional distinction. The maintenance issue is that adding an authoritative field can require coordinated changes to multiple handwritten lists, constructors, validators, restoration paths and preservation rules. Its valid relationship with other pending state is difficult to see in one place.

**Direction:** Partition records by real responsibility: execution identity, current turn, checkpoint/review state, overrides, provider recovery, hotplug, and completion. Give each persisted record one explicit codec and version policy. A strict execution decoder should produce typed validated records; an observer decoder should produce typed partial evidence with a reason when data cannot be used.

Make pending operations explicit variants where fields are mutually dependent. Introduce an explicit “leave unchanged” value for update APIs where omission and clearing currently share `None`. Preserve existing on-disk schemas through adapters during extraction; do not combine every structural refactor with a format migration.

**Proof required:** Round trips preserve authoritative fields; historical fixtures retain supported behavior; malformed current authority fails closed; omitted updates cannot erase worktree, scope, failure or lineage evidence.

### A05 The dashboard and settings components own too many state machines

**Priority: High. Evidence: structural debt in frequently changed code. Effort: large.**

The [dashboard component](/root/code/agent_flow/apps/aflow_app/web/src/components/RunDashboard.tsx:1028) coordinates history pagination, exact-run selection, context depth, SSE, refresh serialization, startup review, idempotency keys, stop/restart/recovery, deletion tombstones, URL callbacks and presentation. The [refresh code](/root/code/agent_flow/apps/aflow_app/web/src/components/RunDashboard.tsx:1682) illustrates several promises, epochs, refs and queued refresh conditions living inside one component.

The [settings component](/root/code/agent_flow/apps/aflow_app/web/src/components/GlobalSettings.tsx:61) similarly combines cross-tab draft ownership with configuration, skills, project scheduling, connection settings and password persistence. Its save ordering and partial acknowledgements are meaningful requirements; they should remain easy to inspect as a dedicated operation.

The recurring risk is that a change to one action invalidates another action's request generation, draft, selected identity or pending mutation. Rendering changes also require navigating unrelated transport and recovery code.

**Direction:** Separate run history loading, selected-run observation, launch review, run mutations, and view rendering. Use small domain-specific hooks/services and explicit operation states. Keep shared owners above navigable views so drafts, unresolved mutation keys, focus and selection survive navigation. Extract settings draft ownership and save coordination independently from the tab rendering components.

Use the existing React stack and shared presentation helpers. Introduce another state-management library only if a concrete remaining problem justifies it.

**Proof required:** Preserve partial reads, stale-response rejection, exact identities, pending restart behavior, acknowledged-only draft clearing, hidden-view subscription suspension, and unnoticeable unchanged refresh. Run component tests plus the existing desktop/mobile browser journeys; passing component tests alone does not establish visual fidelity.

### A06 Progress projection is a large evidence interpreter

**Priority: Medium. Evidence: structural and read-cost debt. Effort: medium to large.**

[`run_progress.py`](/root/code/agent_flow/aflow/control_plane/run_progress.py:811) contains both the older execution summary and the newer canonical history model. [`build_context_bundle`](/root/code/agent_flow/aflow/control_plane/persistence.py:608) builds manager context and calls both projectors. The dashboard deliberately consumes both shapes. This is active compatibility behavior, not dead code that can simply be deleted.

Within the canonical path, evidence discovery, path containment, byte budgets, history association, counts, delivery interpretation, caching and response construction are interleaved. The main detail reducer spans 823 lines. [`project_run_progress_summary`](/root/code/agent_flow/aflow/control_plane/run_progress.py:5440) builds a full detail object and then selects summary fields. Even cache lookup first constructs an artifact-based key through filesystem discovery.

There are already useful optimizations: lightweight list requests can omit progress, and the original-plan identity projector avoids rich history work. This review does not claim those improvements are absent or establish current UI latency.

**Direction:** Separate contained evidence reading, record normalization, pure progress reduction and response projection. Share a request's validated evidence where safe. Migrate current consumers to one semantic model, retaining a narrow compatibility adapter for historical shapes. Define the actual information required by summary versus detail before optimizing their read paths.

**Proof required:** Preserve unavailable/partial/complete coverage, truthful zeroes, checkpoint identity, review authority, lineage and delivery distinctions. Readers must remain read-only. Measure file reads and parsed bytes at representative history sizes before setting performance targets.

### A07 Event limits bound response size but not processing cost

**Priority: Medium. Evidence: measured algorithmic behavior. Effort: medium.**

[`EventJournal.append` and `tail`](/root/code/agent_flow/aflow/control_plane/persistence.py:446) read and parse the entire journal. Append does so to validate history and determine the next sequence number; tail does so before slicing the requested records. The server [SSE loop](/root/code/agent_flow/apps/aflow_app/server/src/aflow_app_server/main.py:1207) polls every 0.1 seconds. Its service path also performs status reads, so journaling cost is only part of that read path.

A disposable probe requested one event from journals containing 10 and 1,000 records. The reader parsed **1,271 bytes** and **128,893 bytes**, respectively. Appending one record parsed the same full prior byte counts. These are measured bytes, not a live latency benchmark. Repeating full-prefix parsing on each append produces quadratic total parsing work as a run's event count grows.

**Direction:** Preserve the ordered append-only journal while introducing validated incremental reads or a rebuildable offset/sequence index. Establish integrity on first open and detect replacement, truncation and torn writes before reusing an offset. Share unchanged observations where justified by the existing identity rules. Keep corruption detection and replay semantics explicit.

**Proof required:** More retained history should not make an unchanged tail poll reparse all prior records. Tests must retain monotonically ordered sequences, torn-tail handling, interior-corruption detection and correct cursor behavior across process restart. A database migration is not required to address this specific problem.

### A08 Persistence primitives are copied with differing guarantees

**Priority: Medium. Evidence: concrete duplication. Effort: medium.**

Atomic-write and locking mechanics are implemented independently in [run logging](/root/code/agent_flow/aflow/runlog.py:137), [control-plane persistence](/root/code/agent_flow/aflow/control_plane/persistence.py:251), [plan backups](/root/code/agent_flow/aflow/plan_backups.py:567), [publication](/root/code/agent_flow/aflow/publication.py:72), [admission](/root/code/agent_flow/aflow/project_admission.py:1383), and [resource journals](/root/code/agent_flow/aflow/execution_resources.py:496).

The copies differ in file creation, permissions, symlink checks, temporary-file naming, directory syncing and failure handling. For example, admission syncs the containing directory after replacement; the resource-journal writer does not. This comparison establishes inconsistent durability mechanics, not a reproduced resource-admission failure.

**Direction:** Consolidate a small set of audited primitives for exclusive creation, atomic replacement, directory synchronization and bounded regular-file reads. Keep each domain's locks, schema validation and transaction ordering with that domain. Different persistence contracts should be named and tested explicitly rather than hidden behind many boolean options in a universal writer.

**Proof required:** Existing fault-injection, path-containment and crash-recovery checks pass for each migrated caller. Migrate a caller at a time. Single-file helper consolidation does not by itself solve A01's two-file transaction problem.

### A09 Wire contracts require repeated manual updates

**Priority: Medium. Evidence: structural debt with existing partial protection. Effort: medium.**

Public shapes are represented in [canonical dataclasses](/root/code/agent_flow/aflow/control_plane/models.py:496), [Pydantic transport models](/root/code/agent_flow/apps/aflow_app/server/src/aflow_app_server/models.py:26), and [client TypeScript types](/root/code/agent_flow/apps/aflow_app/web/src/types.ts:1). Canonical-to-transport tests already compare fields and exercise serialization. The client receives JSON through a generic typed wrapper whose type parameter does not validate incoming data.

Separating domain and transport representations is useful. The debt is repeated handwritten wire shape maintenance, especially nested optional progress/recovery fields. A schema addition can require changes to several distant files and fixtures before the client compiler sees it.

**Direction:** Choose a single source for public wire schemas and generate client types from it. Keep explicit adapters for genuine domain-to-wire differences. Extend contract coverage to representative nested payloads and compatibility with absent optional fields. Runtime validation should be targeted at uncertain or independently versioned inputs, rather than duplicated for every local object.

**Proof required:** A server schema change either updates generated client types or fails a contract check. REST and MCP preserve equivalent public meanings. Domain models remain independent of HTTP concerns.

### A10 Test organization and quality gates do not match the refactoring risk

**Priority: High for the small baseline repairs, medium for test restructuring. Evidence: reproduced fixture failures and failing lint. Effort: small, then incremental.**

Three distinct improvements are needed:

- **Isolate integration fixtures from operator Git settings.** `_commit_fixture_repository` uses `git add -A` without an isolated configuration. On this host, the global `/plans/` ignore rule omitted plans from fixture commits, leaving their worktree mirrors missing. Four REST/MCP durable-recovery cases failed with `recovery_evidence_unavailable`. The eight related cases all passed with `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1`. Production correctly rejected the missing evidence in these fixtures.
- **Restore the web lint gate.** `npm run lint` reports eight errors and eleven warnings. [CI](/root/code/agent_flow/.github/workflows/ci.yml:85) runs web tests and the build, but not lint. Some errors are straightforward cleanup; warnings about hooks need behavioral review. The lint result alone does not prove those warnings are user-visible defects.
- **Align tests with real module boundaries.** `tests/test_runtime.py` spans 19,147 lines. [Shared support](/root/code/agent_flow/tests/_support.py:1) imports controller and CLI internals and exports every non-dunder global; nine root test modules star-import it. Many recovery tests patch CLI internals. Refactoring currently risks fixture churn that obscures real behavior changes.

**Direction:** Fix test Git isolation locally within fixtures, retaining the operator's real configuration. Enable the repaired lint command in CI. During each production extraction, move its cohesive tests and use small explicit fixtures or injected process/storage boundaries. Preserve a smaller set of end-to-end contract tests with real temporary repositories and subprocesses.

The current Python Ruff configuration selects four correctness rules and excludes tests. That is a useful baseline, but it does not constrain complexity or check type boundaries. Add narrow architecture/size checks and incremental typing for new contracts after the baseline is dependable; avoid repository-wide warning floods.

**Proof required:** The same fixture succeeds under conflicting global ignore settings. Web lint runs in CI. A module's contract can be tested without reconstructing unrelated workflow state, while integrated recovery and publication remain covered.

### A11 Configuration code retains an obsolete ownership model

**Priority: Medium. Evidence: reachable-code and reference inspection. Effort: medium.**

The current application uses global configuration. [`ProjectConfigService`](/root/code/agent_flow/apps/aflow_app/server/src/aflow_app_server/project_config_service.py:321) remains a 279-line implementation of per-project editing. Repository reference search found its class used by its tests and definition, while the global service imports validation and private persistence helpers from that same module. This makes obsolete behavior and still-needed shared machinery look like one responsibility. External use of that class was not established.

The [candidate validator](/root/code/agent_flow/apps/aflow_app/server/src/aflow_app_server/project_config_service.py:169) also writes submitted TOML to a temporary directory to invoke the filesystem loader, then performs semantic validation again. The second pass is explicitly justified by a comment about a possible future change to the loader.

**Direction:** Separate pure pair parsing/validation from file selection, loading and transaction persistence. Keep the global application service thin. Inventory public compatibility requirements, then remove the unused project editor or retain only the necessary compatibility facade. Move shared primitives to an owner whose name describes their current role.

**Proof required:** Current text and guided-form validation produce equivalent errors without temporary-file round trips for in-memory content. Supported CLI/configuration entry points remain compatible. No active route regains per-project override semantics accidentally.

### A12 Provider-specific discovery leaks into workflow execution

**Priority: Medium. Evidence: concrete ownership leakage and duplicated mechanics. Effort: medium.**

[`_discover_session_driver`](/root/code/agent_flow/aflow/workflow.py:8529) branches on harness names, locates executables, starts ACP negotiation, and invokes Codex help commands. The workflow layer therefore needs provider-specific changes even though adapters and session-driver contracts already exist.

Reasonix, DSH and Strands also each implement JSON-RPC framing, request matching, timeout handling, stream management and shutdown. They have legitimate protocol and permission differences. The maintenance concern is that fixes to shared process/transport mechanics must be evaluated separately across all implementations. Runtime [signature inspection](/root/code/agent_flow/aflow/harnesses/session.py:98) is additionally used to decide whether a driver accepts the lifecycle seam.

**Direction:** Let adapters own discovery/capability negotiation. Define explicit contracts for an owned-session driver and exclusive-lifetime support, with a compatibility adapter for older injected drivers where necessary. Extract shared transport mechanics only after tests establish which behavior is actually common; preserve provider-specific permission, model and session rules in their adapters.

**Proof required:** Adding or changing a provider does not require provider-name branches in the workflow coordinator. Fake-process contract tests cover stream draining, malformed responses, cancellation, exact child ownership, reaping and lease retention. Use targeted live-provider checks only when the later implementation changes provider behavior.

### A13 The server has services but its composition is still global

**Priority: Medium. Evidence: structural debt. Effort: medium.**

The [server entry point](/root/code/agent_flow/apps/aflow_app/server/src/aflow_app_server/main.py:157) stores configuration, credential provider, registry, plan service, control-plane service, configuration service and plan consumer in module globals. The lifespan initializes them, dependencies read them, and tests assign and clear them directly. The same 2,254-line file contains authentication, session behavior, route declarations, response adaptation, settings parsing and process startup.

This limits isolated app composition and makes tests depend on shared cleanup discipline. It also increases the amount of unrelated code an agent must load to change one route family. The underlying service separation is already useful and should be retained.

**Direction:** Introduce an explicit app factory with one lifespan-owned service container, exposed through app state/dependencies. Split cohesive routers for runs, plans/projects, configuration/skills, and sessions. Keep authentication and MCP mounting in the composition layer with clearly defined shared contracts.

**Proof required:** Independent test app instances do not share services or credentials. HTTP cookie and origin rules, header-only MCP authentication, lifecycle ownership and read-only API reconciliation remain unchanged. Tests already confirm SSE work is offloaded appropriately; this finding does not call for an async rewrite of the services.

### A14 Architecture guidance is too large and contains stale ownership notes

**Priority: Later, alongside the relevant refactors. Evidence: documented drift. Effort: small to medium.**

`ARCHITECTURE.md` is 2,068 lines. It mixes the overall structure with detailed feature chronology and recovery invariants. The root UI guidance still says the responsive requirements are planned, while the nearer web guidance describes completed responsive ownership and its tests. Configuration documentation describes a global pair, while the legacy project service and some operational material retain the older model.

The problem for agents is choosing the current authoritative description, not merely reading a long file. Moving code without updating this map would leave the same comprehension cost in documentation.

**Direction:** Keep a compact architecture entry point showing ownership, dependency direction, state authority and supported entry points. Link deeper domain documents for recovery, admission, progress, configuration and delivery. Update the nearest guidance with each extraction and remove superseded statements. Preserve operational invariants and historical evidence instead of deleting them for brevity.

The 5,185-line concierge is another later decomposition candidate: observation, pure triage, planning and delivery checks have distinct responsibilities. Its host/operator-specific policy should be explicit at the operational boundary. The inspected deployment tooling already has exact-SHA CI gates and rollback boundaries; this review provides no basis for replacing those mechanisms.

**Proof required:** A new contributor can locate the owner of launch, resume, configuration save, progress and publication without reading the whole change history. Documentation distinguishes generic product behavior, legacy compatibility and p100-specific operation.

## Suggested decomposition boundaries

These are responsibility boundaries for later design, not a prescribed new directory tree. Prefer extending an appropriate existing module over creating another abstraction.

| Current concentration | Smaller cohesive owners |
| --- | --- |
| Workflow execution | Lifecycle preparation; boundary decision; one turn execution; checkpoint/review progression; completion delivery |
| CLI resume machinery | Resume request resolution; durable decoding; scope reconciliation; continuation preparation |
| Daemon service | Launch reservation/publication; startup question handling; run controls; continuation admission; unit observation |
| Manager context | Evidence capture; evidence reading; semantic context assembly; bounded serialization |
| Progress projection | Contained evidence reader; normalized records; progress reducer; summary/detail adapters; cache |
| Run dashboard | History loader; selected-run observer; launch review; action state; small rendering components |
| Global settings | Shared draft owner; domain validation; save coordinator; tab-specific editors |
| Server entry point | App composition; authentication/session boundary; cohesive routers and response adapters |

The desired dependency direction is:

```text
CLI, REST, MCP, automatic scheduling
                  |
          Application operations
       startup, resume, control, delivery
                  |
        Domain decisions and records
                  |
     Storage, process and provider adapters

Observers read validated evidence and project it for the UI.
They do not become another execution or recovery authority.
```

Apply this direction to concrete dependency problems such as daemon-to-CLI imports. Do not introduce interfaces around every helper merely to make the diagram look pure.

## Size and readability guidelines for future plans

The following are proposed review thresholds for new or substantially rewritten production code. They are starting points, not a reason to split coherent code mechanically.

- **Functions:** Aim for one task that fits in roughly 20–60 lines. Review a function above 80 lines for extraction. An explicit orchestration sequence may justify up to about 120 lines; document the reason when exceeding that.
- **Classes and React components:** Give each one a single owner responsibility and a small public surface. Review bodies above roughly 250–300 lines. State ownership and operation flow should be visible without following dozens of methods or hooks.
- **Files:** Aim for roughly 200–400 lines of hand-maintained behavior. Review files above 500 lines. Generated schemas, declarative tables and focused fixtures can have documented exceptions.
- **Nesting and arguments:** Deep nesting, long positional lists, many optional parameters and captured mutable locals are reasons to clarify the operation's contract. Do not hide them inside an unstructured context or `dict[str, Any]`.
- **Dependencies:** A transport should translate requests; it should not own reusable execution logic. Avoid private cross-module imports for shared domain behavior.
- **Agent comprehension:** A typical change should have one obvious owner module, a small contract, and nearby focused tests. Local documentation should state invariants and mutation authority, rather than narrate an old checkpoint.

Existing large modules should shrink incrementally under a recorded exception baseline. Avoid blocking unrelated work merely because an old file remains above the target. Review both net complexity and where responsibilities moved; a higher file count is acceptable when understanding and testing become simpler.

## Order for subsequent actionable plans

1. **Establish trustworthy verification.** Isolate Git fixtures, restore web lint, and record focused scenario tests around intended extraction boundaries. This gives later refactors useful feedback.
2. **Correct the configuration transaction gap.** Keep the change narrow and prove process-death recovery independently of structural cleanup.
3. **Extract shared resume behavior and state codecs.** Work one durable domain at a time. This removes entry-point coupling and supplies smaller contracts for the execution refactor.
4. **Decompose workflow execution in phases.** Begin with a cohesive boundary and its tests. Avoid overlapping rewrites of workflow, manager, resume and publication in one plan.
5. **Decompose UI ownership and server composition.** These can proceed independently of most runtime work if wire contracts stay stable. Preserve explicit integration ownership for shared API/type changes.
6. **Reduce observer/event read cost and consolidate compatibility.** Reuse measured fixture shapes, preserve evidence semantics, and verify actual improvement rather than inferring it from fewer lines.
7. **Finish targeted cleanup.** Remove proven-unused configuration paths, reduce repeated persistence/transport mechanics, update generated contracts and shorten the architecture entry point as their owners are stabilized.

Each later plan should name the responsibility being reduced, the interfaces it may change, the current behavior it must preserve, and the tests that will establish that preservation. Independent plans should have isolated execution worktrees and a deliberate integration owner, following the repository's AFlow pipeline rules.

Avoid a single repository-wide refactoring campaign that changes storage formats, control flow, provider behavior and UI data handling together. The first milestone should be one smaller end-to-end responsibility with unchanged external behavior and clear evidence.

## Validation recorded for this review

- **Production Python lint:** Passed with the repository's configured Ruff rules.
- **Selected core suites:** 256 passed in 31.18 seconds. Covered run state/logging, repository/services, progress, admission, publication and live configuration.
- **Selected server suites:** 169 passed and four failed in 55.05 seconds. The failures were the Git-configuration-dependent recovery fixtures described in A10.
- **Focused recovery rerun with isolated Git configuration:** Eight passed, 145 deselected, in 3.38 seconds. This reran both ordinary and durable-evidence variants; it was not a repeat of the whole server suite.
- **Complete client test suite:** 724 passed across 30 files in 42.23 seconds.
- **Production TypeScript checking:** Passed with `tsc --noEmit --incremental false --project tsconfig.json`.
- **Client lint:** Failed with eight errors and eleven warnings.
- **Configuration process-death probe:** Reproduced an invalid mixed pair after interruption between replacements.
- **Event read-cost probe:** Confirmed complete-prefix parsing for a one-record tail and an append at 10 and 1,000 retained records.

Tests used existing local project environments with `uv run --no-sync`; dependencies were not reinstalled. No full Python suite, installed-wheel build, real-browser matrix, live-provider exercise or deployment check was run for this analysis. The evidence supports the architectural conclusions and the specific reproduced gaps above, not a claim that every product path is healthy.

## Decisions to carry into plan conversion

- Preserve the current public behavior and durable evidence before pursuing size reduction.
- Make small, cohesive functions/classes/files and agent comprehension explicit acceptance criteria.
- Prefer deletion of proven-unused behavior and consolidation of genuinely shared mechanics over new frameworks.
- Keep strict execution validation distinct from tolerant observation, with explicit typed boundaries between them.
- Decide compatibility requirements before removing old public fields, saved-state readers or import paths.
- Set measurable read-cost targets using current fixtures before planning performance changes.
- Keep the configuration correctness repair and verification baseline ahead of broad refactoring.

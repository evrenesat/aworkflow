# AFLOW-MAINT-20261006 — AFlow maintainability improvement

- Effort ID: `AFLOW-MAINT-20261006`
- Started: 6 October 2026
- Members: 18 checkpoint plans, 38 checkpoints
- Authoring status: queued; no implementation run launched by this conversion.

## Purpose And Source

One coordinated attempt to make AFlow easier to understand and change: smaller cohesive functions/classes/files, explicit ownership and unchanged execution/recovery protections.

Source: [aflow-maintainability-20261006-source-review.md](aflow-maintainability-20261006-source-review.md). Exact source SHA-256: `0f0185752e327adbc4609c8f617e0b6e855454bee9862877da96b16b35b091fe`. Original review revision: `c60a980796ed35c2d31f5a2a75dc5f1219789951`. Conversion source: p100 main `d2a9a5e96fe317191a30a0da2f35c10baaf7fdeb`. The original research copy and its evidence remain preserved.

## Concierge Recognition And Scope

All and only the 18 executable members use filename prefix `aflow-maintainability-20261006-` in plans/in-progress and declare `Effort ID: AFLOW-MAINT-20261006` (the actual Markdown value is code-formatted). Search the stable ID/prefix, not the word architecture, to distinguish them from older unrelated plans. This index and the source snapshot are non-executable notes and must not be launched.

Each member has a stable 01–18 number, source-finding IDs, explicit dependencies, scope, unchecked checkpoints, exact local verification and real-surface acceptance. Keep the effort ID in commits, PR descriptions and concise run handoffs. Never infer completion from directory placement.

Scheduling at creation: .aflow/project-settings.json has auto_consume_plans=false and max_concurrent_implementations=2. No scheduling/runtime settings are changed. The new files are queued in the requested in-progress stack. Enabling automatic consumption or launching runs is a separate action.

Filename numbers are effort member IDs, not AFlow's _Pnn_ sequence syntax; the work is a dependency graph with independent lanes, not one forced serial queue. The current scheduler does not enforce these prose cross-plan prerequisites. The dispatching concierge must check predecessors and outside dependencies before each authorized start; do not enable blind automatic consumption of this bundle.

## Members

01. [Make the existing validation baseline dependable](../in-progress/aflow-maintainability-20261006-01-verification-baseline.md) — P1; A10.

    State: queued. Required predecessors: none.

02. [Make configuration pair saves recover after process death](../in-progress/aflow-maintainability-20261006-02-configuration-crash-recovery.md) — P1; A01.

    State: queued. Required predecessors: none.

03. [Give configuration validation and editing clear owners](../in-progress/aflow-maintainability-20261006-03-configuration-validation-ownership.md) — P2; A11.

    State: queued. Required predecessors: 02.

04. [Move shared resume operations out of the CLI](../in-progress/aflow-maintainability-20261006-04-transport-neutral-resume.md) — P1; A03.

    State: queued. Required predecessors: 01.

05. [Give execution identity and metadata one codec per responsibility](../in-progress/aflow-maintainability-20261006-05-execution-state-codecs.md) — P2; A04 (identity, turn, update semantics).

    State: queued. Required predecessors: 04.

06. [Make supervision and recovery records explicit and round-trippable](../in-progress/aflow-maintainability-20261006-06-supervision-state-codecs.md) — P2; A04 (manager, scope, overrides, hotplug, provider recovery).

    State: queued. Required predecessors: 05.

07. [Extract workflow startup and safe-boundary preparation](../in-progress/aflow-maintainability-20261006-07-workflow-startup-boundaries.md) — P2; A02 (startup and boundary preparation).

    State: queued. Required predecessors: 02, 06.

08. [Extract one workflow turn with explicit process and lease ownership](../in-progress/aflow-maintainability-20261006-08-workflow-turn-execution.md) — P2; A02 (one-turn execution).

    State: queued. Required predecessors: 07, 17.

09. [Extract checkpoint progression and completion delivery](../in-progress/aflow-maintainability-20261006-09-workflow-progression-delivery.md) — P2; A02 (progression and completion).

    State: queued. Required predecessors: 08.

10. [Give the run dashboard explicit history, observation and action owners](../in-progress/aflow-maintainability-20261006-10-dashboard-state-owners.md) — P2; A05 (RunDashboard).

    State: queued. Required predecessors: 01. Outside prerequisite: Wait for run-history-fast-on-demand-20261005.md to be delivered and verify its current branch/receipt before dispatch. As inspected, successor 20261006t060326z-d4fe4b41 is active. Rebase on that accepted behavior; do not restore the earlier eager history/context path.

11. [Extract the settings draft owner and save coordinator](../in-progress/aflow-maintainability-20261006-11-settings-draft-save-owners.md) — P2; A05 (GlobalSettings).

    State: queued. Required predecessors: 01, 02.

12. [Separate evidence reading from progress interpretation](../in-progress/aflow-maintainability-20261006-12-progress-evidence-pipeline.md) — P2; A06.

    State: queued. Required predecessors: 01. Outside prerequisite: Wait for run-history-fast-on-demand-20261005.md delivery and preserve its compact overview/demand-driven detail contract. This plan owns backend progress structure, not that plan's client paging.

13. [Avoid reparsing unchanged event journals](../in-progress/aflow-maintainability-20261006-13-event-journal-read-cost.md) — P2; A07.

    State: queued. Required predecessors: 01.

14. [Consolidate small persistence primitives without moving domain locks](../in-progress/aflow-maintainability-20261006-14-persistence-primitives.md) — P2; A08.

    State: queued. Required predecessors: 01.

15. [Compose isolated server apps and cohesive routers](../in-progress/aflow-maintainability-20261006-15-server-app-composition.md) — P2; A13.

    State: queued. Required predecessors: 01.

16. [Generate client wire types from server schemas](../in-progress/aflow-maintainability-20261006-16-generated-wire-contracts.md) — P2; A09.

    State: queued. Required predecessors: 01, 15.

17. [Move provider discovery and explicit session capabilities into adapters](../in-progress/aflow-maintainability-20261006-17-provider-discovery-boundaries.md) — P2; A12.

    State: queued. Required predecessors: 01.

18. [Publish a concise source-grounded maintainability map](../in-progress/aflow-maintainability-20261006-18-architecture-entry-point.md) — P2; A14; documentation closure for A01–A13.

    State: queued. Required predecessors: 03, 04, 05, 06, 07, 08, 09, 10, 11, 12, 13, 14, 15, 16, 17.

## Dependency Lanes And Integration

- Start-ready repair lane: 01 (verification baseline) and 02 (configuration crash safety) can proceed independently in isolated roots.
- Configuration: 02 → 03; settings UI 11 follows 01 and 02.
- Runtime: 01 → 04 → 05 → 06 → 07 → 08 → 09, with 02 also required before 07 and provider plan 17 before 08.
- Provider boundary: 01 → 17; it can proceed alongside configuration/resume work before turn extraction.
- Dashboard 10 and progress pipeline 12 follow 01 plus delivered run-history-fast-on-demand-20261005 work.
- Events 13 and file primitives 14 follow 01 and can proceed independently; reconcile their shared persistence imports deliberately.
- Server 15 follows 01; generated wire contracts 16 follows 01 and 15.
- Documentation consolidation 18 follows the implemented owners above; each member still updates its own docs as it goes.

Shared-file coordination: 04–09 and 14 intersect runlog/workflow/state; 10 and 12 depend on accepted on-demand history; 03/11/15/16 meet at configuration/transport; 13/14 meet at persistence. Agree narrow interfaces and integrate serially; shared files alone do not require serial implementation. Use one coordinator as integration owner when execution is authorized, with each logical run in its own workflow-managed branch/worktree. Record exact execution root/run ID in the ongoing handoff, not guessed paths here.

Current unrelated work observed at conversion: run-history successor 20261006t060326z-d4fe4b41 and inherited-hotplug repair 20261006t025109z-d5afd011 had owned worker wrappers. A predecessor's stale running field alone is not an extra controller. Recheck actual processes/receipts before dispatch; no controller was stopped or resumed.

## Source Finding Coverage

| Source finding | Effort members |
| --- | --- |
| A01 Configuration process-death correctness | 02 |
| A02 Workflow decomposition | 07, 08, 09 |
| A03 Shared resume/service dependency direction | 04 |
| A04 Durable state/codecs | 05, 06 |
| A05 Dashboard and settings ownership | 10, 11 |
| A06 Progress evidence/interpretation | 12 |
| A07 Event processing cost | 13 |
| A08 Persistence mechanics | 14 |
| A09 Wire schema maintenance | 16 |
| A10 Reliable gates and cohesive tests | 01, scoped typing in 05–06, tests with each extraction |
| A11 Configuration ownership/validation | 03 |
| A12 Provider discovery/session mechanics | 17 |
| A13 Server composition | 15 |
| A14 Concise architecture/ownership guidance | 18, incremental docs in every member |

## Current Evidence And Conversion Decisions

- The local attached source and p100 plans/research source match byte-for-byte. The p100 working main and origin/main matched at d2a9a5e9 during conversion; the Mac code checkout was older and was not used as current implementation evidence.
- The review's missing fixture-plan issue already has git add -f -A in _commit_fixture_repository (e57d69ea/87ba106c). Member 01 verifies that current repair instead of duplicating it.
- A live p100 lint run still failed with 8 errors and 11 warnings. Member 01 repairs current errors and enables the existing command in CI; this planning conversion does not claim lint is green.
- No member implementation, crash repair, refactor, browser matrix or deployment was executed by authoring these plans. Parser/structure/link validation proves the handoffs are usable documents only.

## Explicit Later / Out Of Scope

Concierge implementation decomposition and broad manager-context decomposition remain later candidates from the review, not hidden extra checkpoints. No new product features, state store, scheduler, deployment boundary, UI redesign, storage-format campaign or universal framework is authorized here. Legacy imports/wire shapes stay compatible through explicit facades; each optional removal needs demonstrated compatibility evidence in its owning scope.

## Acceptance And Durable Tracking

At authoring completion, require exactly 18 parseable unchecked member plans in p100 plans/in-progress, all sharing this effort ID, plus this index and preserved source snapshot; validate dependency acyclicity and member links, then commit/push a discoverable GitHub record. Reference copies may be synchronized to the Mac for clickable review, without overwriting unrelated plans.

During execution, track implementation/review, publication to origin/main, exact-SHA CI and live activation separately for each member. A member is delivered only when its required acceptance and existing delivery gates pass. Keep failed required checks and deferred manual checks visible. The later concierge may update statuses/links as evidence arrives; it must not treat this authoring snapshot as current run state.

## Planning Verification

Authoring validation uses the existing aflow.plan parser, checks the 18-member/38-checkpoint count, blank runtime-owned Git Tracking fields, unchecked meaningful steps, mandatory sections, exact shared ID, non-cyclic prerequisites and linked source/member existence. It also verifies only plan documents and the planning DEVLOG entry are staged. Current production lint failure is recorded above and belongs to member 01.

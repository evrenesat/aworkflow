# AFLOW-MAINT-20261006 — AFlow maintainability improvement

- Effort ID: `AFLOW-MAINT-20261006`
- Started: 6 October 2026
- Members: 18 checkpoint plans, 38 checkpoints
- Pairing groups: `AFLOW-MAINT-20261006-A` and `AFLOW-MAINT-20261006-B`; nine members and 19 checkpoints each
- Authoring status: queued; no implementation run launched by this planning work.

## Purpose And Source

One coordinated attempt to make AFlow easier to understand and change: smaller cohesive functions/classes/files, explicit ownership and unchanged execution/recovery protections.

Source: [preserved architecture review](aflow-maintainability-20261006-source-review.md). Exact source SHA-256: `0f0185752e327adbc4609c8f617e0b6e855454bee9862877da96b16b35b091fe`. Original review revision: `c60a980796ed35c2d31f5a2a75dc5f1219789951`. Conversion and pairing inspection source: p100 main `d2a9a5e96fe317191a30a0da2f35c10baaf7fdeb`. Preserve the original research copy and evidence.

## Concierge Recognition And Scope

All and only the 18 executable members use filename prefix `aflow-maintainability-20261006-` in plans/in-progress and declare this effort ID. Every member also declares exactly one pairing-group ID and a preferred group position. Search these exact identifiers, not the words architecture or maintainability, to distinguish this effort from unrelated plans. This index and the source snapshot are non-executable notes.

Keep member numbers, filenames, source coverage, checkpoints and run lineage stable. Group position is a preference among ready work, not an extra dependency and not an instruction to implement a whole group in one run. Preserve the shared effort ID and group ID in concise handoffs and PR descriptions. Never infer completion from directory placement.

The current scheduler does not enforce these Markdown group tags or prose cross-plan prerequisites. These are instructions for the dispatching concierge; no runtime queue feature, automatic pairing or resource-policy change is introduced. Filenames deliberately do not use AFlow's linear _Pnn_ series syntax. Automatic plan consumption was false and the project implementation cap was two at inspection; this planning change leaves them unchanged.

## Viability And Limits

Two complementary groups are viable with the current worker/reviewer split. The selected xtx-turns team uses an exclusive local pi Swift worker, a non-exclusive cloud Codex Sol 6.1 extra-high checkpoint reviewer and a cloud Astra medium final reviewer. The broker serializes local worker invocations; a cloud review can overlap the other group's implementation. A second controller is useful concurrency, not permission for a second simultaneous local inference invocation.

This arrangement removes the accidental single effort-wide queue. It does not make all 81 cross-group combinations start-ready, nor guarantee continuous utilization. A pair is eligible only after every listed predecessor and external hold is delivered. Worker/review durations, repair turns, cloud limits and deployment gates can still leave one resource idle. Equal checkpoint counts are a workload balance aid, not equal-duration estimates. Member 18 is a final join.

Production ownership is separated below, with explicit one-way handoffs for the few shared files. Shared documentation still needs ordinary serialized integration. A claim of zero shared files for all 18 original refactors would be false: CI, server fixtures, repository projection and persistence mechanics cross the original boundaries. The delivery gates below remove concurrent edits there without inventing another abstraction or scheduler.

## Group A — Runtime And Durable State

Preferred order: **01 → 14 → 04 → 05 → 06 → 17 → 07 → 08 → 09**. Skip a held preference when another member's actual prerequisites are satisfied; for example, 04 can proceed after 01 while 14 waits for the external resource repair.

- **A1 / 01** [Make the existing validation baseline dependable](../in-progress/aflow-maintainability-20261006-01-verification-baseline.md) — 2 checkpoints; required effort predecessors: none.
- **A2 / 14** [Consolidate small persistence primitives without moving domain locks](../in-progress/aflow-maintainability-20261006-14-persistence-primitives.md) — 3 checkpoints; required effort predecessors: 01. Also has an external delivery hold.
- **A3 / 04** [Move shared resume operations out of the CLI](../in-progress/aflow-maintainability-20261006-04-transport-neutral-resume.md) — 2 checkpoints; required effort predecessors: 01.
- **A4 / 05** [Give execution identity and metadata one codec per responsibility](../in-progress/aflow-maintainability-20261006-05-execution-state-codecs.md) — 2 checkpoints; required effort predecessors: 04, 14.
- **A5 / 06** [Make supervision and recovery records explicit and round-trippable](../in-progress/aflow-maintainability-20261006-06-supervision-state-codecs.md) — 2 checkpoints; required effort predecessors: 05. Also has an external delivery hold.
- **A6 / 17** [Move provider discovery and explicit session capabilities into adapters](../in-progress/aflow-maintainability-20261006-17-provider-discovery-boundaries.md) — 2 checkpoints; required effort predecessors: 01. Also has an external delivery hold.
- **A7 / 07** [Extract workflow startup and safe-boundary preparation](../in-progress/aflow-maintainability-20261006-07-workflow-startup-boundaries.md) — 2 checkpoints; required effort predecessors: 02, 06.
- **A8 / 08** [Extract one workflow turn with explicit process and lease ownership](../in-progress/aflow-maintainability-20261006-08-workflow-turn-execution.md) — 2 checkpoints; required effort predecessors: 07, 17.
- **A9 / 09** [Extract checkpoint progression and completion delivery](../in-progress/aflow-maintainability-20261006-09-workflow-progression-delivery.md) — 2 checkpoints; required effort predecessors: 08.

After the opening baseline, this group owns runtime/resume orchestration, durable state codecs, provider/session mechanics, workflow phases and the initial byte-persistence migration. Configuration services, UI, public wire models and later control-plane read projections stay with Group B. Exact per-member exceptions and handoffs are in each plan.

## Group B — Configuration And Operator Surfaces

Preferred order: **02 → 03 → 11 → 15 → 13 → 16 → 10 → 12 → 18**. Skip a held preference only when the selected member's actual prerequisites are satisfied. In particular, history holds on 10/12 do not hold 03/11/15/13/16.

- **B1 / 02** [Make configuration pair saves recover after process death](../in-progress/aflow-maintainability-20261006-02-configuration-crash-recovery.md) — 2 checkpoints; required effort predecessors: none.
- **B2 / 03** [Give configuration validation and editing clear owners](../in-progress/aflow-maintainability-20261006-03-configuration-validation-ownership.md) — 2 checkpoints; required effort predecessors: 02.
- **B3 / 11** [Extract the settings draft owner and save coordinator](../in-progress/aflow-maintainability-20261006-11-settings-draft-save-owners.md) — 2 checkpoints; required effort predecessors: 01, 02.
- **B4 / 15** [Compose isolated server apps and cohesive routers](../in-progress/aflow-maintainability-20261006-15-server-app-composition.md) — 2 checkpoints; required effort predecessors: 01, 04.
- **B5 / 13** [Avoid reparsing unchanged event journals](../in-progress/aflow-maintainability-20261006-13-event-journal-read-cost.md) — 2 checkpoints; required effort predecessors: 05, 14.
- **B6 / 16** [Generate client wire types from server schemas](../in-progress/aflow-maintainability-20261006-16-generated-wire-contracts.md) — 2 checkpoints; required effort predecessors: 01, 06, 15.
- **B7 / 10** [Give the run dashboard explicit history, observation and action owners](../in-progress/aflow-maintainability-20261006-10-dashboard-state-owners.md) — 3 checkpoints; required effort predecessors: 01. Also has an external delivery hold.
- **B8 / 12** [Separate evidence reading from progress interpretation](../in-progress/aflow-maintainability-20261006-12-progress-evidence-pipeline.md) — 2 checkpoints; required effort predecessors: 06, 13. Also has an external delivery hold.
- **B9 / 18** [Publish a concise source-grounded maintainability map](../in-progress/aflow-maintainability-20261006-18-architecture-entry-point.md) — 2 checkpoints; required effort predecessors: 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12, 13, 14, 15, 16, 17. Closing join; never pair with unfinished effort work.

This group owns configuration parsing/transactions, server composition, settings/dashboard state, generated wire contracts, and event/progress read projections after Group A's initial migrations. It imports the runtime's existing public boundaries instead of changing codecs, workflow behavior or provider ownership. Member 18 closes the whole effort after both groups finish.

## Shared Boundaries And Required Handoffs

These are deliberate ordering gates to prevent overlapping implementation, distinct from the preferred order above. A predecessor must be delivered, not merely implementing or awaiting review.

| Boundary | Required handoff |
| --- | --- |
| Baseline CI, UI edits and broad fixture helpers | 01 before dependent UI/server members; 02 uses separate configuration-pair transport tests so the opening pair stays independent. |
| Runlog byte writes | 14 before 05. The config-pair transaction in 02 remains separate and is never migrated by 14. |
| REST/MCP resume fixture migration | 04 before 15. Members 13/16 inherit that gate through 05/06. Members 02/03 keep new tests in configuration-focused modules. |
| Repository observer and persistence implementation | 05 and 14 before 13; 13 before 12. Group A does not reopen those handed-off projection files. |
| Supervision records and scoped type-check CI | 06 before 12/16. Group B consumes the compatible codecs; 16 preserves the completed type gate when adding schema checks. |
| Configuration recovery used by runtime/settings | 02 before 07/11. Workflow extraction treats config/live-config implementations as read-only public boundaries. |
| Whole-effort architecture consolidation | All members 01–17 before 18. |

All members may update the documentation for their own change in the same delivery. Use small domain-scoped edits to ARCHITECTURE.md, DEVLOG.md and nearest guidance, preserve both sides on merge, and serialize publication. These shared documentation paths are not a reason to serialize otherwise independent implementation. If a necessary production edit crosses a group's protected boundary, narrow the change or record an additional concrete delivery handoff before dispatch; do not silently expand the pairing claim.

## Pair Selection And Delivery

1. Reconcile existing controllers and delivery evidence first. This bundle is queued; its authors have not launched it. Existing authorized priorities and current capacity take precedence.
2. Select one ready member from each group, using the preferred orders as a tie-breaker. Check exact predecessor commits on origin/main, publication receipts, exact-SHA CI and applicable live acceptance. Resolve any outside repair/history hold. A checked box or a review in progress is not delivery.
3. The first compatible pair is **A/01 + B/02**. While A handles 14/04, useful counterparts are B/03 or B/11. After 04, A/05–06 can pair with B/11 or B/15. After the handoffs, A/17/07/08/09 can pair with ready B/13/16/10/12. These are choices, not synchronized rounds.
4. Give each logical run one workflow-managed execution root, branch and controller. Before launch, record member/group, exact run ID, execution root, branch, integration owner and current stage in the existing delivery handoff. No roots or run IDs are assigned by this planning change. The dispatching concierge owns integration unless the owner assigns someone else.
5. Keep at most one active effort member per group for these pairings, including review and repair. Let the exclusive worker broker and cloud review phases interleave naturally. When a member finishes delivery, refill that group's slot with another ready member; do not wait for the counterpart plan to finish. Do not weaken a prerequisite, launch a duplicate or switch an exclusive worker to shared mode merely to keep two runs present.
6. If one group has no ready work, report the concrete hold and continue the other ready work or another already-authorized independent plan. Do not count an idle wait as local/cloud overlap or invent filler checkpoints. Start 18 only after the complete join.
7. Serialize integration and publication, preserve both groups' intended behavior and run the affected combined checks. Prioritize a failed delivery gate before publishing another result. Record implementation/review, origin/main publication, exact-SHA CI and live acceptance separately.

No new workflow/team selection is embedded in worker checkpoints. At authorized dispatch, follow current repository lifecycle and team-selection guidance and verify the resolved runtime; the live configuration snapshot below is evidence, not a new launch instruction.

## Live AFlow Snapshot — 6 October 2026, 08:48 UTC

Read-only inspection of controller processes, run records, event journals, the exclusive-resource journal, plan checkboxes and deployed service identity found:

- **Inherited hotplug completion repair:** run `20261006t025109z-d5afd011` was on worker turn 6, implementing checkpoint repair overlay v02 after prior cloud reviews. Its live controller and exact child held the exclusive Swift resource. The original plan's checked boxes did not mean the current repair/review obligations were finished.
- **Exclusive stop and dead-owner reclamation:** run `20261006t083546z-50d6f9e9` had no completed worker turn and was queued behind that owner. This was a live second controller waiting for local capacity, not an active cloud review.
- **Fast run history:** successor `20261006t060326z-d4fe4b41` was owner_stopped after a timed-out worker attempt. Checkpoints 1–2 were checked and 3–4 unchecked; no delivery was established. Members 10/12 remain held until the existing owner resolves and delivers that work.
- Exactly two independent live controllers were identified; their UI launcher/child processes were not counted as extra runs. No run record referenced a member of this maintainability effort.
- The aflow-ui service was active on release `d2a9a5e96fe317191a30a0da2f35c10baaf7fdeb`. This confirms the service/release identity, not end-to-end health or unfinished feature usability. The older hotplug controller retained its pinned release.
- Project settings were `auto_consume_plans=false` and `max_concurrent_implementations=2`; local Swift profile exclusive=true, selected cloud checkpoint reviewer exclusive=false. No live configuration, controller, resource claim, plan-consumer setting or deployment was changed by this task.

This is a dated snapshot, not ongoing monitoring. Recheck before dispatch. Members 06 and 17 explicitly wait for hotplug repair delivery; 14 and 17 wait for resource repair delivery. Their downstream consumers inherit those holds. Do not resume, stop or recover these unrelated runs solely from this planning artifact.

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

## Current Evidence And Planning Decisions

- The original local attachment and preserved p100 research copy matched byte-for-byte. p100 main and origin/main matched at d2a9a5e9 during pairing inspection; the older Mac code checkout was not used as current implementation evidence.
- The review's missing fixture-plan issue already has git add -f -A in _commit_fixture_repository (e57d69ea/87ba106c). Member 01 verifies that current repair instead of duplicating it.
- The original conversion's p100 lint run failed with eight errors and eleven warnings. Member 01 owns that repair and the existing lint command's CI gate; this planning change does not claim lint is green.
- Repartitioning preserves all 18 member identities, all 38 checkpoints and all 121 implementation steps. It adds group ownership and concrete handoff prerequisites; it does not mark implementation, browser acceptance or deployment complete.

## Explicit Later / Out Of Scope

Concierge implementation decomposition and broad manager-context decomposition remain later candidates from the review. No new product feature, scheduler, automatic group dispatcher, state store, deployment boundary, UI redesign, storage-format campaign or universal framework is authorized here. Legacy imports/wire shapes stay compatible through explicit facades. Local inference tuning and recovery of the current unrelated runs are outside this planning revision.

## Acceptance And Durable Tracking

Require exactly 18 parseable unchecked member plans in p100 plans/in-progress, exactly one of the two group IDs in every member, nine members/19 checkpoints per group, acyclic prerequisites, valid source/index/member links and preserved source bytes. Preserve filenames and the original checkpoint ledger. Commit/push the revised plan documents to the existing discoverable GitHub record, and synchronize only this effort's reference files to the Mac.

Before implementation, read current repository guidance for lifecycle, worktree, source installation and delivery ownership. During implementation, a member is delivered only when required acceptance and existing publication/CI/live gates pass. Keep failed required checks and unverified manual checks visible. The concierge may update this index with evidence; authoring status is not a live run-status source.

## Planning Verification

Use the existing aflow.plan parser to validate every plan, empty runtime-owned Git Tracking fields, unchecked meaningful steps and the unchanged 18-member/38-checkpoint/121-step totals. Validate exact group membership/counts, dependency acyclicity, all cross-group shared-boundary ordering rules, the eligibility of opening pair 01/02 and the final 18 join. Check relative links, git diff --check, source SHA-256 and that only these documents plus the planning DEVLOG entry changed. This is document/coordination validation, not production test or deployment evidence.

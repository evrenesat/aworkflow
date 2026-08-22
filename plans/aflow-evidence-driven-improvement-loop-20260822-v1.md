# Evidence-driven workflow improvement loop for AFlow

## Summary

Add a provider-neutral improvement subsystem that turns recurring workflow friction into durable, inspectable evidence and then into scoped, verified adaptations.

AFlow already keeps task intent in checkpointed plans and controller execution in durable run artifacts. The proposed system adds a separate improvement layer for observations that span runs: papercuts, recurring themes, adaptation candidates, evaluations, applied adaptations, and retirement/removal conditions.

The intended loop is:

```text
run/user/controller observation
        ↓
structured papercut
        ↓
triage and recurrence grouping
        ↓
adaptation candidate
        ↓
isolated evaluation
        ↓
explicit accept/reject
        ↓
apply to future runs
        ↓
monitor, supersede, or retire
```

The system must keep AFlow's current external-controller model. Workers remain disposable harness processes. Durable state remains outside those processes. There is no requirement for live runtime self-modification, dynamic code evaluation, or mutation of an in-flight run.

## Problem

AFlow records detailed evidence about individual runs: plans, turns, reviews, manager decisions, recoveries, hotplugs, retries, failures, and lifecycle state. That evidence is useful for debugging a run, but there is no first-class mechanism for turning repeated friction across runs into durable improvements to AFlow itself.

Today a recurring problem usually follows this path:

1. a run encounters unnecessary work, a tool limitation, repeated reviewer feedback, a recovery path, or another avoidable failure;
2. the evidence remains inside that run or in a human's memory;
3. a future run encounters the same class of problem;
4. eventually someone changes AFlow code, configuration, workflow policy, prompts, or bundled skills;
5. the reason, supporting evidence, verification, and future removal condition are only partially connected to the change.

This makes improvement dependent on memory and ad-hoc diagnosis. It also encourages permanent accumulation of rules whose original problem may disappear.

AFlow needs a feedback loop that can answer these questions from durable state:

- What recurring friction are AFlow workflows encountering?
- Which runs and turns demonstrate it?
- Is this one incident or a repeated pattern?
- What change is proposed to address it?
- What scope does that change have?
- How will the change be evaluated before installation?
- Which evidence justified accepting or rejecting it?
- Which future runs used the accepted adaptation?
- When can the adaptation be removed or replaced?

## Design principles

### 1. Observation carries no mutation authority

Reporting a papercut must never grant authority to edit AFlow, its configuration, workflow definitions, source tree, prompts, or active run state.

Workers, reviewers, managers, deterministic controller logic, and users may all produce observations. The controller validates and records them as evidence. Applying an adaptation is a separate operation with separate authorization.

### 2. Keep improvement state separate from task and run state

The system should maintain explicit state categories:

| State | Purpose | Authority |
| --- | --- | --- |
| Plan | Intended task work and checkpoint progress | Existing plan semantics |
| Run state | What the controller actually executed | Existing `.aflow/runs/<run-id>/` state |
| Papercut | One observed instance of workflow friction | Improvement subsystem |
| Theme | A grouping of related papercuts | Improvement subsystem |
| Adaptation | A proposed or installed behavioral change | Improvement subsystem plus explicit owner action |
| Evaluation | Evidence about a candidate adaptation | Improvement subsystem |
| Installed change | Actual code/config/workflow/prompt/skill change | Existing source/config mechanisms |

Papercuts must not become plan tasks automatically. Improvement analysis must not rewrite historical run state. A run may reference improvement IDs, while historical run interpretation must remain possible without consulting mutable future improvement records.

### 3. Evidence precedes adaptation

An adaptation should link to concrete source evidence whenever that evidence exists. The source may be a run, turn, review rejection, recovery, manager decision, process failure, user report, or another bounded diagnostic artifact.

Evidence references should prefer durable artifact paths, stable IDs, structured outcomes, and hashes over copied transcript text.

### 4. Changes are scoped

Every adaptation must state where it applies. Expected target classes include:

- workflow graph or transition policy;
- manager/routing policy;
- harness adapter or controller behavior;
- configuration defaults;
- AFlow prompts or bundled skills;
- deterministic validation or utility code;
- repository-specific AFlow behavior where a project needs a local rule.

A local problem should not silently become installation-wide behavior. A provider-specific problem should not be generalized into provider-neutral controller policy without evidence.

### 5. Evaluate candidates away from active control paths

Candidate adaptations should be evaluated in a clean execution context. Use existing worktree, fresh-process, configuration snapshot, and run-artifact concepts where applicable.

A candidate must not alter a controller or worker that is currently executing the run from which the candidate was derived.

### 6. Application is explicit and revision-gated

Initial versions of this feature should require explicit owner approval before an adaptation changes tracked source or effective configuration.

The apply operation must verify that its target still matches the revision against which the candidate was proposed and evaluated. A stale candidate returns to proposal/evaluation instead of overwriting newer work.

### 7. Every lasting adaptation has a removal condition

Accepted adaptations must record why they can eventually be removed or replaced. Examples include:

- an upstream harness gains the missing capability;
- a provider bug is fixed;
- AFlow gains a deterministic mechanism that replaces prompt guidance;
- the workflow or configuration target is deleted;
- the triggering friction does not recur across an agreed observation window;
- a newer adaptation supersedes the same behavior.

This keeps old improvements reviewable instead of allowing permanent instruction growth.

### 8. Provider-neutral core

The improvement model belongs to AFlow. It must not depend on one harness or model provider. Provider-specific evidence and adaptations are allowed when their scope explicitly says so.

## Core concepts

### Papercut

A papercut is one durable report of avoidable workflow friction. It is evidence, not a proposed fix.

A papercut should contain enough information to inspect it later without loading the original conversation context. The exact schema is an implementation decision, but it must cover at least:

- stable ID and schema version;
- creation time;
- source kind, such as manual report, controller-derived event, worker/reviewer/manager advisory report, or imported historical run;
- optional run ID, turn ID, role/step, and artifact references;
- short problem statement;
- category and severity/impact hint;
- intended scope or affected component when known;
- bounded evidence references or excerpts;
- status such as open, grouped, addressed, dismissed, or obsolete;
- links to related papercuts, themes, or adaptations;
- attribution sufficient to distinguish user/controller/model-generated observations.

Papercut text must be treated as data. It must never be executed or interpolated into shell/config/code paths without the normal validation for that target.

### Sources of papercuts

The system should support several sources without making all of them part of the first implementation.

#### Manual reports

A user or operator can explicitly record friction and attach it to a run, turn, or repository. This is the simplest source and should work even when no workflow is active.

#### Offline extraction from existing run artifacts

An improvement analysis command can scan durable runs and derive candidate papercuts from objective events already recorded by AFlow. Useful signals include:

- repeated harness recovery;
- repeated review rejection inside one implementation scope;
- worker upgrades after convergence problems;
- automatic checkpoint repartition;
- same-step or max-turn pressure;
- environment preflight blockers;
- unexpected process exits;
- hotplug failures or ambiguous reconciliation;
- repeated manager stop causes;
- user cancellation/override patterns where the durable reason is known;
- recurring lifecycle or merge failures.

Offline extraction is a good first implementation because it does not change live workflow protocols.

#### Live deterministic controller signals

After the offline model proves useful, the controller may emit the same structured events as papercuts at run boundaries. The event should be derived from controller-owned facts rather than model interpretation where possible.

#### Advisory model reports

Workers, reviewers, or managers may later be allowed to recommend a papercut through a strict structured response. Such a report remains advisory. The model receives no new mutation authority.

The existing manager's read-only authority must remain intact.

### Theme

A theme groups papercuts believed to represent the same underlying friction.

Grouping should retain exact membership and never rewrite source papercuts. Deterministic fingerprints can handle obvious cases such as the same failure kind and component. Semantic grouping may use a configured analysis role later, but its output must remain reviewable and reversible.

A theme should capture:

- stable ID;
- title and normalized problem statement;
- member papercut IDs;
- first/last occurrence;
- recurrence count and affected runs;
- affected components/scopes;
- current disposition;
- related or superseding themes.

Frequency alone must not create an adaptation. A severe one-off problem may deserve immediate promotion, while frequent low-value noise may be dismissed.

### Adaptation

An adaptation is a versioned proposal to change AFlow behavior in response to one or more papercuts/themes.

Every adaptation must record:

- stable ID and version;
- problem being addressed;
- proposed behavior;
- target kind and scope;
- source papercuts/themes and evidence;
- expected effect;
- exact target revision, hash, or config snapshot used for the proposal;
- verification/evaluation plan;
- rollback strategy;
- removal condition;
- lifecycle status;
- relationship to earlier/superseded adaptations;
- accepted/rejected/applied/retired timestamps and decision reasons when relevant.

The adaptation record describes behavior. The actual implementation remains an ordinary source/config/workflow/prompt/skill change that can be reviewed with existing mechanisms.

### Evaluation

An evaluation records what happened when a candidate adaptation was tested.

The evaluation model needs to support both deterministic changes and agent-behavior changes.

For deterministic changes, normal tests and focused reproduction cases should dominate the evidence.

For behavior changes, the system may compare baseline and candidate runs against the same or representative plans. Exact model behavior is nondeterministic, so the system should record evidence rather than claim statistical certainty from one paired run.

Useful outcome measurements include, when available:

- completion/failure result;
- turn count;
- repeated same-step count;
- review rejection count;
- recovery count;
- manager escalation or worker upgrade count;
- repartition/scope-pressure events;
- wall-clock duration;
- provider-reported usage/cost data when already available;
- human/reviewer acceptance result;
- regression test result.

An evaluation must identify the baseline revision/configuration and candidate revision/configuration. A later change to either makes the evaluation historical evidence rather than proof for a different candidate.

## Adaptation lifecycle

Use an explicit lifecycle so that observations, proposals, installed behavior, and historical evidence cannot be confused.

```text
papercut(s)
   ↓
theme / triage
   ↓
adaptation proposed
   ↓
evaluation pending
   ↓
┌─────────────┴─────────────┐
│                           │
rejected                 accepted
│                           ↓
retained evidence         applied
                            ↓
                         monitored
                            ↓
                    superseded/retired
```

Required behavior:

- rejection preserves the proposal and evaluation evidence;
- acceptance does not imply that a stale target may be overwritten;
- application records the exact installed revision/version;
- new runs can record which effective adaptation set/version was active;
- retirement does not rewrite historical runs;
- superseding an adaptation links both records;
- an adaptation whose removal condition becomes true is reported for review rather than silently deleted.

## Storage and durability

The exact directory structure should be chosen during implementation, but the design should use a controller-owned, schema-versioned improvement store separate from run directories. A likely repository-local home is under `.aflow/improvements/`.

The store needs durable records for:

- papercuts;
- themes/grouping membership;
- adaptation versions;
- evaluations;
- apply/retire decisions;
- any indexes needed for efficient inspection.

Requirements:

- atomic replacement for mutable controller-owned indexes/state;
- immutable or append-safe evidence where practical;
- explicit schema versions and migrations;
- no dependence on modification-time scans to determine identity or ownership;
- bounded retention policies for copied excerpts, while IDs and evidence references remain durable;
- run records may reference improvement IDs without embedding the entire improvement database;
- improvement-store corruption must fail mutating improvement operations safely and must not corrupt unrelated historical run state;
- existing AFlow workflows must continue to run when the improvement feature is unused.

Accepted tracked changes remain normal Git content. The improvement store explains why a change exists and how it was evaluated. It is not a replacement for Git history.

Repository-local improvement state should be the first scope. Installation-wide aggregation can be added later once ownership, privacy, and cross-repository semantics are defined.

## Evidence hygiene and privacy

Improvement analysis can easily become an excuse to copy large prompts, transcripts, environment variables, or provider state into a new database. Avoid that.

Default evidence should use:

- run/turn IDs;
- structured controller outcomes;
- artifact paths and hashes;
- bounded plain-text summaries/excerpts;
- stable failure categories;
- explicit user reports.

Do not copy secrets, raw environment snapshots, provider session identifiers, hidden reasoning, or complete prompts by default. Existing AFlow redaction and bounded-artifact principles should carry into this subsystem.

## Applying an adaptation

The application mechanism depends on target kind, but all targets share the same rules.

1. Verify the adaptation is accepted and has a completed evaluation or an explicitly recorded reason for manual acceptance.
2. Verify the target revision/config snapshot still matches the proposal.
3. Apply in a normal isolated source/config path rather than mutating an in-flight controller.
4. Run the adaptation's verification steps.
5. Record the installed revision/version and rollback information.
6. Make the change effective only at a safe future-run boundary unless an existing AFlow mechanism already provides a separately validated live boundary for that exact setting.
7. If verification fails, retain evidence and leave the previous effective behavior intact where possible.

The initial design should favor future-run application. Live self-reconfiguration is outside this plan.

## Monitoring accepted adaptations

An applied adaptation should remain connected to subsequent evidence.

Future papercuts can reference the adaptation they appear to contradict. The analysis layer should be able to report:

- the same friction recurring after installation;
- new failures correlated with an adaptation;
- adaptations with no recent supporting evidence;
- removal conditions that appear to have become true;
- multiple adaptations affecting the same target;
- adaptations superseded by source/config changes outside the improvement subsystem.

This monitoring is advisory in the initial system. Automatic rollback or automatic retirement is a later decision.

## User-facing workflow

Exact command names are provisional. The capability should support a workflow similar to:

```text
aflow papercut report ...
aflow papercut list/show ...

aflow improve analyze ...
aflow improve themes ...
aflow improve propose ...
aflow improve evaluate ...
aflow improve apply ...
aflow improve status ...
aflow improve retire ...
```

The important behavior is:

- reporting is cheap;
- inspection shows source evidence;
- analysis can scan one run, selected runs, or a bounded recent set;
- proposal generation never applies changes;
- evaluation is distinct from acceptance;
- apply/retire are explicit owner actions;
- status can answer what adaptations are active and why.

`aflow analyze <run-id>` should eventually show related papercut/adaptation IDs without requiring the full improvement database to explain the historical run.

The daemon/control-plane interface can later expose equivalent read/report operations and owner-authorized adaptation actions. Control-plane integration should reuse the same core service rather than introduce a second state model.

## Interaction with manager, recovery, and hotplug

The improvement subsystem must respect existing controller authority boundaries.

- The interstep manager remains read-only with respect to repository, plan, configuration, and run-control state.
- A manager may eventually emit an advisory improvement observation, but cannot apply it.
- Harness recovery remains concerned with completing/recovering the current run. It should not rewrite global improvement policy while recovering a failure.
- Hotplug remains a run-local role-selector transaction. Repeated hotplug failures may become papercut evidence, but the improvement subsystem must not change an in-flight hotplug transaction.
- Improvement analysis should primarily consume already-finalized durable artifacts.
- A failure in the improvement subsystem must not create a second controller for a workflow run or take ownership of existing run recovery.

## Preventing instruction/configuration sediment

The system should make obsolete adaptations visible.

Every applied adaptation has a removal condition and should carry enough information to answer:

- Is the triggering problem still present?
- Does the target still exist?
- Has a deterministic mechanism replaced this guidance?
- Has a provider/upstream capability removed the need?
- Has another adaptation superseded it?
- Has the adaptation itself caused repeated corrective work?

A periodic or explicit review can mark adaptations as `review_due`, `superseded`, or `retirement_candidate`. The first version does not need autonomous retirement.

## Non-goals

This plan does not require:

- a mutable saved runtime or process image;
- live code redefinition inside the AFlow controller;
- arbitrary dynamic code evaluation;
- changing the current plan because a papercut was reported;
- allowing a worker/reviewer/manager to directly edit improvement policy;
- automatic installation of model-proposed changes;
- generic personal/assistant long-term memory;
- provider-specific self-tuning as a core feature;
- perfect statistical optimization of model behavior;
- replacement of Git issues, tests, code review, or ordinary source history;
- replacement of AFlow's existing plan, run-state, resume, manager, recovery, lifecycle, or hotplug semantics.

## Checkpoints

### [ ] Checkpoint 1: Define the improvement state model and papercut capture

**Outcome:** AFlow has a durable, provider-neutral representation for papercuts and can create/query them without changing normal workflow execution.

- [ ] Define schema-versioned papercut records, evidence references, categories, scope, attribution, status, and stable IDs.
- [ ] Define a repository-local improvement store with atomic/durable write rules and migration/versioning rules.
- [ ] Add explicit manual report/list/show/close behavior.
- [ ] Add offline extraction from a bounded set of existing run artifacts for objective controller events.
- [ ] Link papercuts to runs/turns without mutating historical turn artifacts.
- [ ] Define evidence redaction/bounding rules and tests for secrets/provider-session data.
- [ ] Ensure improvement-store failures cannot corrupt normal run state or create duplicate controller ownership.

### [ ] Checkpoint 2: Add recurrence grouping and adaptation records

**Outcome:** Repeated papercuts can be grouped and promoted into a complete adaptation proposal with enough context to evaluate later.

- [ ] Define theme/group records with exact membership and reversible grouping.
- [ ] Support deterministic grouping for obvious structured failure classes.
- [ ] Leave semantic/model-assisted grouping behind a provider-neutral analysis interface and keep it advisory.
- [ ] Define adaptation records with mandatory problem, behavior, target/scope, source evidence, expected effect, target revision, verification, rollback, and removal condition.
- [ ] Support manual promotion of a papercut/theme even when recurrence thresholds are not met.
- [ ] Preserve rejected/dismissed candidates as historical evidence.
- [ ] Detect conflicting or overlapping active candidates for the same target.

### [ ] Checkpoint 3: Add isolated evaluation and evidence comparison

**Outcome:** A candidate can be tested without changing the active controller or production/default configuration.

- [ ] Define evaluation records that bind baseline and candidate revisions/config snapshots.
- [ ] Support deterministic reproduction/tests as first-class evidence.
- [ ] Support representative baseline/candidate workflow runs where behavioral evaluation is needed.
- [ ] Reuse fresh harness processes and isolated worktree/config concepts rather than sharing hidden worker state between baseline and candidate.
- [ ] Record available workflow outcome measurements such as completion, turns, rejections, recoveries, upgrades, repartitions, duration, and existing usage data.
- [ ] Make weak or inconclusive evidence explicit instead of coercing every evaluation into pass/fail.
- [ ] Reject stale evaluations when the candidate target changes.

### [ ] Checkpoint 4: Add explicit apply, rollback metadata, and adaptation lifecycle

**Outcome:** An accepted candidate can become future AFlow behavior through a revision-gated operation with traceable rollback and retirement state.

- [ ] Add explicit acceptance/rejection and apply states separate from evaluation state.
- [ ] Revision-gate every application against the evaluated target.
- [ ] Apply through normal source/config/workflow/prompt/skill update mechanisms, never dynamic runtime code injection.
- [ ] Run required verification before recording the adaptation as active.
- [ ] Record installed revision/version, effective scope, and rollback data.
- [ ] Record the effective adaptation-set/version in new runs in a bounded reproducible form.
- [ ] Add supersede/retire/review-due states and removal-condition reporting.
- [ ] Keep automatic rollback and automatic retirement out of the first implementation unless a later focused plan justifies them.

### [ ] Checkpoint 5: Integrate observation and inspection across AFlow surfaces

**Outcome:** Improvement information becomes useful during normal AFlow operation without weakening existing authority boundaries.

- [ ] Extend run analysis/status output with related papercut/adaptation identifiers and concise state.
- [ ] Add read/report capabilities to the daemon/control-plane service using the same improvement store and service layer.
- [ ] Keep apply/retire operations owner-authorized and revision-gated.
- [ ] Add deterministic live papercut emission for selected controller events after offline extraction has proven the schema.
- [ ] Consider strict advisory papercut output from manager/reviewer/worker roles without granting mutation authority.
- [ ] Add reporting for recurrence after adaptation, stale adaptations, overlapping targets, and triggered removal conditions.
- [ ] Document the state taxonomy, lifecycle, privacy rules, and operator workflow.

## Open implementation decisions

These decisions can be made while implementing the checkpoints. They do not change the required behavior above.

1. Exact file layout inside the improvement store: one record per file, append log plus indexes, or a small embedded database.
2. Whether theme grouping should use only deterministic rules initially or also introduce an `improvement_analyst` role in Checkpoint 2.
3. The exact boundary between a proposed adaptation record and the concrete Git/config patch used for evaluation.
4. How many baseline/candidate runs are useful by default for behavioral evaluation and which cost/time limits should apply.
5. Which deterministic run events are high-signal enough for live automatic papercut creation rather than offline discovery.
6. Whether installation-wide aggregation belongs in the same store or a later separate user-level store.
7. How active adaptation-set identity is summarized in `run.json` without making run interpretation depend on mutable external state.

## Done means

The feature is complete at the architectural level described by this plan when:

- AFlow can record one instance of workflow friction as a structured papercut linked to durable evidence.
- It can identify repeated related papercuts without destroying the individual records.
- It can turn those observations into a scoped adaptation proposal containing problem, behavior, verification, rollback, and removal condition.
- It can evaluate a candidate in isolation and keep baseline/candidate evidence.
- Applying a candidate requires explicit authority and an unchanged target revision.
- Applied adaptations affect safe future boundaries and are traceable from later runs.
- An adaptation can be superseded or retired without rewriting historical evidence.
- Existing plan progress, run-state authority, manager read-only behavior, recovery, hotplug, resume, and lifecycle behavior remain intact when the improvement subsystem is unused.
- The implementation and documentation use provider-neutral AFlow concepts and require no external project or prior conversation to explain the design rationale.

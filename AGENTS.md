# AFlow Repository Guidance

## Development runtime

- On p100, use the uv tool installation as the development entry point for AFlow itself.
- Before starting or resuming an AFlow workflow, install the intended source checkout as an editable tool with `uv tool install -e . --force` from that checkout.
- Launch workflows through the installed entry point (`aflow run ...`). Never launch the AFlow CLI with `uv run aflow ...`; that selects the project environment instead of validating the editable tool installation used for normal operation.
- `uv run` remains appropriate for project-scoped development commands such as `uv run pytest`, linters, and one-off Python checks.
- Because the editable tool is shared by AFlow processes on p100, verify the intended source checkout and active controllers before changing its editable target. Concurrent workflows must use one deliberate installed AFlow source.

## Runtime safety

- Treat plan files and `.aflow` durable state as authoritative when starting or resuming runs.
- Before launching, distinguish independent AFlow controller roots from their launcher/child processes and avoid duplicate controllers for one logical run.
- Preserve managed worktrees, unrelated dirty files, and run lineage during recovery.

## Completed-work delivery

- The owner authorizes publishing completed, reviewed AFlow work to `origin/main`
  and its existing CI-gated deployment. Do not leave completed plans only on local
  main or feature branches, or ask again for this already-granted publication.
- In-place workflows do not merge by themselves. The controller publication
  boundary uses the repository-local Git settings `aflow.publishRemote=origin`
  and `aflow.publishBranch=main`; configure them in the working repository.
- Complete a plan only after its approved commits reach origin/main. Report
  publication, CI, and live activation separately; do not claim deployed usability
  from a local approval. Fix a failed delivery gate before accumulating more
  completed local plans. Preserve active execution worktrees and never force-push.

## Parallel development and integration

These rules apply when coordinating an authorized multi-plan development goal,
not to a worker assigned one bounded checkpoint.

- Prefer concurrent execution of independent, ready plans over serial queue
  processing. Start with two runs when capacity permits; expand only within
  actual provider, host and controller limits. Do not wait for unrelated work
  to finish merely because it was started first. Respect explicit owner priority.
- Before dispatch, identify each plan's scope, dependencies, execution root and
  integration owner in the existing handoff ledger. Each logical run gets one
  workflow-managed branch/worktree and one controller. Never duplicate a run or
  let concurrent workers edit the same checkout. Keep worktrees internal to the
  registered parent project; ordinary execution requires no extra UI project.
- Use web/MCP and a workflow with worktree/branch setup and merge teardown;
  `checkpoint_delivery` is the current normal choice. Select the authorized
  worker/reviewer team explicitly. Verify resolved lifecycle and publication
  settings rather than relying on a name. Reserve in-place workflows for
  existing-lineage recovery with an explicit coordinator-owned delivery step.
- Shared files alone do not prohibit parallel work: agree interfaces and let
  independent changes proceed in isolated worktrees. Sequence genuine producer/
  consumer dependencies and incompatible schema changes. Do not invent stubs,
  duplicate APIs, or broad rewrites merely to make parallelism possible.
- Keep tests, temporary HOME/state, ports and artifacts isolated. A worktree does
  not isolate shared installations or services. Use one deliberate AFlow source
  or verified pinned runtimes; never repoint a shared tool or restart another
  run's controller as a side effect of concurrent work.
- The coordinator owns integration. Serialize updates to local main and remote
  main, fetch accepted history before integrating, and resolve conflicts in an
  integration checkout or at a clean owned boundary. Preserve both intended
  behaviors; inspect overlapping changes even when Git merges cleanly. Never
  blanket-select ours/theirs, reset active trees, discard another run's work,
  or force-push to make integration pass.
- Test affected combined behavior after reconciliation; run relevant browser
  checks for UI integration. Publish each independently completed, reviewed
  plan promptly to origin/main without waiting for the whole batch. Confirm the
  publication receipt and exact-SHA CI/deployment; a rejected push is an
  integration task to resolve, not a completed delivery.
- Independent implementation may continue while another result passes CI.
  Prioritize repairing failed delivery before publishing more completed work;
  do not stack new releases over a known failing gate. Distinguish local review,
  merge, remote publication, CI and live verification in the handoff.
- Use event-driven completion signals or bounded scheduled checks. Notify on
  meaningful progress, failure or required decisions; avoid repeated unchanged
  transcript/status polling. If parallelism is blocked, record the concrete
  dependency or resource limit and continue other useful authorized work.

## UI and interaction work

- Read and follow [UI_GUIDELINES.md](UI_GUIDELINES.md) for every web UI change.
  It defines the owner-approved two-row desktop header, mobile hamburger/list-detail
  navigation, document scrolling, state preservation, and browser acceptance rules.
- These requirements supersede older bounded-pane layout guidance. Implementing
  them remains planned work; do not describe the current UI as already compliant.

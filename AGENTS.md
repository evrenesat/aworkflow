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

## UI and interaction work

- Read and follow [UI_GUIDELINES.md](UI_GUIDELINES.md) for every web UI change.
  It defines the owner-approved two-row desktop header, mobile hamburger/list-detail
  navigation, document scrolling, state preservation, and browser acceptance rules.
- These requirements supersede older bounded-pane layout guidance. Implementing
  them remains planned work; do not describe the current UI as already compliant.

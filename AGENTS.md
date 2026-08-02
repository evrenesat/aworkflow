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

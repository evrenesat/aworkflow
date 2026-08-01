# DEVLOG

## 2026-08-01 — Codex prompt stdin transport

- Checkpoint 1 moves Codex effective prompts from argv into stdin while keeping
  prompt artifacts and injected-runner behavior observable.
- Large-prompt and early-stdin-close runtime fixtures pass; launch-failure
  normalization remains planned for Checkpoint 2.

## 2026-08-01 — Harness launch failures use terminal paths

- Checkpoint 2 normalizes process-creation `OSError`s from default and injected
  harness execution into bounded nonzero results: 127 for missing executables
  and 126 for other launch failures.
- Manager artifacts, worker turn artifacts, and lifecycle failure metadata now
  use their existing nonzero-result handling without storing a traceback or
  prompt-bearing launch diagnostic.

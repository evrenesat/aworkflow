# DEVLOG

## 2026-08-01 — Codex prompt stdin transport

- Checkpoint 1 moves Codex effective prompts from argv into stdin while keeping
  prompt artifacts and injected-runner behavior observable.
- Large-prompt and early-stdin-close runtime fixtures pass; launch-failure
  normalization remains planned for Checkpoint 2.

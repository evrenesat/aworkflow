# Skill hardening: leave worktrees clean

Date: 2026-08-03

Harden `aflow-execute-*`, `aflow-review-*`, `aflow-merge`, and
`aflow-guard-development-run` so clean worktree handoff is an explicit terminal
invariant:

- classify remaining dirt by ownership and preserve owned work in a commit or
  exact handoff before finishing;
- synchronize plan/checkpoint state, archive completed plans, and verify both
  managed-worktree and primary-checkout status;
- never discard unrelated dirt; report exact blocking paths, branch, and run;
- cover successful and failed terminal paths with regressions that detect
  uncommitted owned changes or unexpectedly dirty managed worktrees.

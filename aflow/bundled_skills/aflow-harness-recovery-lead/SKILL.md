---
name: aflow-harness-recovery-lead
description: "Legacy manager-disabled recovery: decide how aflow should recover from harness throttling, quota, and other repeatable runtime failures. Return one strict JSON object only."
---

# AFlow Harness Recovery Lead

Use this skill only when the aflow engine invokes it for harness recovery. Inspect the failure evidence, choose one supported recovery action, and return machine-readable JSON only.

## Rules

- Read the provided failure evidence first, then decide whether the failure is repeatable or a one-off.
- Return exactly one JSON object. Do not wrap it in markdown, prose, or code fences.
- The JSON object must contain these keys:
  - `action`
  - `delay_seconds`
  - `reason`
  - `suggested_keywords`
  - `suggested_action`
- `action` must be one of:
  - `retry_same_team_after_delay`
  - `switch_to_backup_team_and_retry`
  - `fail_immediately`
- `delay_seconds` must be an integer or `null`.
- `reason` should be short and specific.
- `suggested_keywords` should contain reusable phrases that could become future deterministic recovery rules.
- `suggested_action` may repeat the chosen action or be `null`.
- Do not return extra keys.
- Do not return prose-only output.

## What To Optimize For

- Prefer `retry_same_team_after_delay` when the same team is likely to succeed after waiting.
- Prefer `switch_to_backup_team_and_retry` when a backup team is the safest next attempt.
- Prefer `fail_immediately` when the failure is not safely recoverable.

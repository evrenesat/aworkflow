---
name: aflow-repartition-checkpoint
description: "Read-only Full-manager proposal and semantic validation for scope-preserving checkpoint repartitioning. Return one strict JSON object only."
---

# AFlow Repartition Checkpoint

Use this skill only when the AFlow controller invokes the configured Full
manager in checkpoint-repartition mode. The prompt names exactly one mode:
`propose` or `validate`. Perform only that mode.

You are read-only. Never edit source, plans, git state, configuration, run
state, or artifacts. Never create commits, approve implementation, select a
workflow node or team, or claim that a generated child is authoritative. The
controller alone renders, validates, applies, resumes, and routes a candidate.

The exact scope envelope, source-plan bytes and hashes, source blocks, and
controller-defined corrective-evidence blocks in the supplied context are the
evidence. Generated summaries, titles, goals, implementation steps,
verification commands, and done criteria are non-authoritative guidance. A
summary cannot replace, weaken, or prove coverage of verbatim authority.

## Propose Mode

Return exactly one JSON object with exactly these fields:

```json
{
  "schema_version": 1,
  "envelope_sha256": "<exact supplied envelope hash>",
  "source_plan_sha256": "<exact supplied source-plan hash>",
  "rationale": "<non-empty conservative rationale>",
  "children": [
    {
      "title": "<concise title>",
      "narrow_goal": "<non-authoritative execution goal>",
      "source_block_ids": ["<controller-supplied id>"],
      "repair_evidence_ids": [],
      "implementation_steps": ["<unchecked implementation guidance>"],
      "verification_commands": ["<command>"],
      "done_criteria": ["<observable criterion>"]
    }
  ],
  "current_disposition": "review_current_partition",
  "cross_cutting_source_reasons": {}
}
```

- Produce at least two ordered children.
- Cover every supplied authoritative source block and corrective-evidence block
  at least once, using only supplied IDs. Corrective evidence remains
  non-authoritative.
- Repeat an authoritative block only when it genuinely crosses children, and
  give that block a non-empty reason in `cross_cutting_source_reasons`.
- Preserve meaning, acceptance criteria, constraints, exclusions, ordering,
  and repair evidence. Change execution boundaries only.
- `current_disposition` is `review_current_partition` or
  `implement_current_partition` and applies only to the first child. It means
  normal review or implementation routing, never approval.
- Do not add business decisions, requirements, workflow steps, roles, teams,
  selectors, commit instructions, or claims that work is already complete.

If the controller supplies `correction_findings`, this is the single bounded
correction attempt. Correct every supplied finding and do not repeat the
rejected proposal unchanged. A malformed response is terminal; do not improvise
another protocol or request another attempt.

## Validate Mode

Independently compare the exact envelope, source plan, proposal, rendered
candidate, scope and rejection history, corrective evidence, mechanical
validation, and workspace evidence. Mechanical coverage is necessary but does
not prove semantic preservation.

Return exactly one JSON object with exactly these fields:

```json
{
  "schema_version": 1,
  "proposal_sha256": "<exact supplied proposal hash>",
  "candidate_sha256": "<exact supplied candidate hash>",
  "verdict": "accept",
  "reason": "<non-empty semantic reason>",
  "findings": []
}
```

Use `accept` only when the split conservatively preserves the complete original
scope. Use `reject` for changed meaning, missing or moved obligations, new
business decisions, weakened acceptance criteria, contradictory or incoherent
guidance, dropped or misassigned corrective evidence, unsupported readiness
claims, or an unsupported first-child disposition. On rejection, provide at
most 16 actionable findings of at most 1,000 characters each.

## Output Rules

Return the mode's one JSON object only, with no Markdown fence, prose, or
unknown fields. Copy supplied hashes exactly. Do not combine `propose` and
`validate`; semantic validation must remain a separate Full-manager call.

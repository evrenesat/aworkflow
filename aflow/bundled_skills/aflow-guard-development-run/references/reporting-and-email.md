# Guard Reporting and Email

Use this workflow only when the user requested email delivery. Default to
completion and a decision/action only the owner can provide. Also allow one
email after every finalized reviewer turn when the user separately opts into
that mode. Do not email routine implementation progress, healthy waiting, or a
condition the guard can safely recover itself.

## Evidence input

Prepare one bounded JSON object for `scripts/aflow_guard_report.py`:

```json
{
  "schema_version": 1,
  "state": "completed",
  "title": "Project guard report",
  "generated_at": "2026-08-07T15:00:00Z",
  "run_id": "exact-run-id",
  "repository": "/absolute/repository/path",
  "summary": "Observable outcome.",
  "checkpoints": [
    {"name": "CP1", "status": "approved", "detail": "Focused tests passed."}
  ],
  "lanes": [
    {"name": "Deployment", "status": "blocked", "detail": "Owner must approve DNS."}
  ],
  "completed": ["Verified result"],
  "remaining": ["Next bounded step"],
  "owner_actions": ["Exact action and why it is required"],
  "model_outcomes": [
    {"model": "model-name", "attempts": 2, "approved": 1, "rejected": 1,
     "median_seconds": 240, "note": "Small sample"}
  ],
  "worker_strategy": {
    "entry_team": "luna-xhigh",
    "entry_worker": "codex.lunaxhigh",
    "configured_chain": [
      {"team": "luna-xhigh", "worker": "codex.lunaxhigh"},
      {"team": "luna-max", "worker": "codex.lunamax"}
    ],
    "observed_workers": [
      {"worker": "codex.lunaxhigh", "attempts": 2},
      {"worker": "codex.lunamax", "attempts": 1}
    ],
    "note": "The selected team is the entry point, not a fixed worker roster."
  },
  "evidence": ["Exact bounded artifact or command result"]
}
```

Set `state` to `in_progress`, `completed`, `needs_owner_action`, or
`reviewer_turn`. Use `in_progress` only for the initiating-task artifact bundle;
never email healthy progress. For a reviewer report, also include non-empty
`notification_id`, `checkpoint`, and `verdict`; derive `notification_id` from
the exact finalized reviewer turn ID plus verdict. Omit unknown fields rather
than estimating them. Keep secrets, passwords, API keys, authorization headers,
cookies, private key material, and temporary credential paths out of the input.

## Worker strategy evidence

Treat the selected team as the starting worker strategy, not as proof that one
worker will handle the whole run. Resolve the entry team's effective worker and
follow every frozen `teams.<team>.upgrade_to` edge until the chain ends. Record
that complete configured chain under `worker_strategy.configured_chain`.

Build `observed_workers` and `model_outcomes` only from finalized turns whose
role is `worker`. Architect, senior-architect, reviewer, reworker, and manager
turns are not worker samples. An eligible configured worker with zero observed
turns is an unsampled candidate, not an unsuccessful model. Never compare model
success rates unless each compared worker has a non-zero stated sample size.

Record every applied manager team override or upgrade in the evidence. When the
same observed worker receives repeated material review rejections, distinguish:

1. an upgrade was configured and applied;
2. an upgrade was configured but not applied; or
3. the configured chain was already exhausted.

Do not summarize all three cases as a deliberate single-worker team.

Render all in-task and email artifacts with one command:

```bash
uv run --quiet --with pillow --with reportlab python \
  <skill-dir>/scripts/aflow_guard_report.py \
  --input <bounded-report.json> \
  --bundle-dir <artifact-directory> \
  --basename <safe-report-name>
```

This writes Markdown, interactive standalone HTML, a mobile-readable single-A4
dashboard PNG, a matching one-page A4 PDF, narrow-screen email HTML, and a
compact JSON manifest. The command prints only the compact manifest, so the
calling agent does not need to read or reproduce the rendered report.

In the initiating task, deliver exactly the minimum useful wrapper:

```markdown
![AFlow guard report](/absolute/path/report.png)

[Interactive desktop report](/absolute/path/report.html) · [Download PDF](/absolute/path/report.pdf)
```

Add prose only for an owner action, unsafe state, recovery, or terminal result
that is not already unambiguous in the image. Never rely on a visualization
content reference as the only report; Android Remote may not render it.

## Report content

Lead with the outcome and current state. Include the exact run ID, checkpoint
or deployment position, verified achievements, the current bottleneck, model
outcomes only with sample sizes, remaining acceptance work, and exact owner
action when blocked. Distinguish observed facts from inference. Do not claim a
deployment usable before its external acceptance checks pass.

Render the PNG and PDF from the same fixed A4 dashboard layout. Use a compact
multi-box grid with at least two truthful graphs when the evidence exists:
status/progress distribution, configured upgrade-chain flow, worker-attempt
bars, and approved/rejected outcome bars are preferred. Derive every count from
the supplied evidence; omit a graph rather than estimate missing values. Bound
each box, truncate with an explicit ellipsis or `+N more in HTML`, and never
overflow onto another PDF page or extend the PNG below A4. Keep critical state,
current bottleneck, next action, sample sizes, and exact run ID visible without
opening HTML.

Use the standalone interactive HTML as the expandable full-detail surface. The
single-page PDF must require no network access and visually match the PNG.

## Gmail delivery

1. Install or authorize the Gmail connector only after an explicit user request.
2. Resolve the recipient only from the current request or a previously
   authorized destination. Confirm ambiguous recipients before sending.
3. Use the authenticated Gmail profile only to verify the sender or a clearly
   requested send-to-self destination. Do not inspect inbox contents.
4. Send Markdown as the plain-text fallback and the generated email HTML as
   `html_body`. Attach the standalone HTML only when the user wants the
   interactive/detailed artifact. Do not use Markdown tables in the email body.
5. Use a short subject containing the project and `completed`, `owner action
   required`, or `<checkpoint> review: <verdict>`.
6. Verify the connector returned a successful message ID, then show the same
   report in the initiating task and state the exact recipient.
7. Never include one-time application passwords or infrastructure credentials
   in email. Return one-time credentials only through the user-approved secure
   channel after their independent acceptance condition is satisfied.

If Gmail authorization or sending fails, report the failure in the initiating
task. Do not retry repeatedly or switch recipients/delivery providers without
owner direction.

## Reviewer-turn deduplication

Inspect only the newest finalized reviewer result after the bounded guard
snapshot indicates a new review boundary. Before sending, compare its exact
turn-ID-plus-verdict fingerprint with the guardian delivery state. After Gmail
returns a successful message ID, atomically record the fingerprint, recipient,
message ID, and timestamp beside guardian state. Never mark delivery before the
send succeeds. Review rejection is reportable in this opt-in mode but remains a
normal AFlow workflow outcome, not automatically an anomaly or owner block.

## Narrow-screen email rules

- Keep the email body one column and at most 640 px wide.
- Use inline styles; assume media queries, CSS variables, and embedded style
  blocks may be removed by Android clients.
- Use normal paragraphs and stacked sections instead of wide tables.
- Allow long run IDs, paths, and evidence to wrap anywhere.
- Put the verdict and next action near the top so the report is useful without
  opening an attachment.

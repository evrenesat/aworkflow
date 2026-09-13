# Inactive GitHub issue intake relay

These files are reviewed examples, not an installed GitHub Actions workflow or
service. The owner-only gate is fixed to GitHub user ID `591691`; public issue
creation remains unrestricted. All trigger states for the initially approved
repositories (`evrenesat/aworkflow`, `evrenesat/doublangu`,
`evrenesat/ailocals`, and `evrenesat/AudioVentura`) are **not activated**.
Existing backlog is not admitted.

`relay.py` reads the Actions event file and forwards only repository/issue IDs,
author/sender IDs, action, `GITHUB_RUN_ID`, and the canonical UTF-8 SHA-256 of
`[title, body-or-empty-string]`. It sends that JSON on stdin to an
operator-approved absolute command. It never prints issue text or lets event
data form a shell command. The host command re-fetches and independently
checks canonical author and source hash before planning or starting.

Choose one private transport only after approval:

- A self-hosted runner with the approved identity and filesystem ACL invokes
  the fixed local command. API/provider credentials and the receipt root remain
  local to that host.
- A hosted runner joins the approved Tailscale network and uses private SSH to
  invoke the same fixed command. Require a restricted SSH identity, host ACL,
  `BatchMode`, and no public listener. The host retains credentials and receipts.

Before per-repository default-branch installation, verify the exact registered
repository ID/full name/project mapping, workflow, team, deferred labels,
absolute config path, credential ownership, and state-root permissions. Copy
the disabled workflow example only after these owner gates; do not invent a
runner label, credential, or reachability path.

The concurrency key is repository ID plus issue number and never cancels an
active job. GitHub may replace an older pending job for the same issue. A later
job is usable only when the current canonical source matches its retained
owner-originated event hash; otherwise intake records attention. Non-owner
events fail the job condition before executable concurrency. A prior intake
claim still requires reconciliation. Different issues retain separate Actions
groups and host receipt locks. External cancellation or failure needs a manual
Actions rerun; no daemon rescans or creates a new generation.

Host receipts persist source claims, artifact identity, planner state, and the
saved start key. A rerun reuses that state and cannot reset uncertain planning.
The command supports one adjacent marker pair:

```text
AFlow-Plan: plans/todo/<name>.md
AFlow-Plan-SHA256: <64 lowercase hex>
```

`plans/in-progress/<name>.md` and same-repository, commit-pinned GitHub blob
paths are also supported. Private HTTP operations use an initial request plus
at most three transient retries with 1/5/15-second backoff. Read-back confirms
only exact bytes and revisions; uncertainty remains actionable.

Code/template publication and CI are separate from activation. Do not enable
the TOML, install a workflow, restart a service, or exercise a live issue until
the owner authorizes the pending private transport, configuration, credentials,
and per-repository default-branch installation.

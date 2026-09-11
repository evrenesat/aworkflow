# Guard report input and deterministic rendering

`aflow_guard_report_input.py` turns one ownership-matched observation into the
validated JSON consumed by `aflow_guard_report.py`. It is a report-input
builder, not a controller, recovery helper, transcript reader, or notification
mechanism.

The helper accepts either a legacy snapshot with the pinned absolute `repo` (or
`repository`) and `run_id`, its `observed_at`, and the existing snapshot schema
version; or an unmodified schema-1 `RunStatus.to_dict()` response captured from
authenticated `get_run`. A canonical response has no repository or observation
timestamp, so `--repo`, `--run-id`, `--ownership-mode`, and `--observed-at`
bind it to the selected guard without fabricating fields inside the response.
Canonical mode accepts only matching `run_id`, `ownership: control_plane`, and
an explicit `ui-server` or `aflowd` caller mode. It emits report-input schema
version `1` with these bounded fields:

- identity and observation: `repository`, `run_id`, `observed_at`, `status`,
  `activity`, and `ownership`;
- progress and worker facts: nullable `checkpoint_index`,
  `checkpoint_name`, `checkpoint_total`, `step`,
  `finalized_turn_summary`, `worker_selector`, `worker_model`, and
  `worker_effort`; and
- `diagnosis`: `confirmed_facts`, `likely_cause`, `supporting_evidence`,
  `contradicting_evidence`, `unknowns`, `confidence`,
  `duplicate_operation_risk`, and `owner_action`.

Canonical pending-input states direct the owner to the existing pinned startup
question, requested input, or requested override through the owning interface;
the report never supplies or executes that input. A precise `unit_missing`
observation with no known terminal provider outcome retains duplicate-operation
risk and an inspect-exact-operation warning without claiming a cause.

All strings are at most 1,024 characters, evidence arrays are at most eight
items, and the serialized result is at most 64 KiB. Missing facts remain
`null`, `unknown`, or an explicit unavailable explanation. Existing repository
redaction and contained artifact checks are reused where available; raw
prompts, environment values, bearer text, and complete stdout/stderr bodies
are never copied into the result.

For a new `orphaned_controller` anomaly only, the optional structured cause
bundle may contain identity-matched `controller_exit`, `host_termination`,
`harness_session`, and `failure_boundary` records. Each record needs the pinned
run/repository identity, a timezone-aware incident timestamp within ten
minutes of the recorded incident, and is capped at 4 KiB. A denied or missing
source is unavailable evidence, not proof of a cause. The ladder never makes a
provider request and never recommends a blind relaunch. Unknown provider
completion is surfaced as duplicate-operation risk with an inspect-and-choose
owner action.

Legacy example invocation:

```bash
python3 <skill-dir>/scripts/aflow_guard_report_input.py \
  --snapshot <external-snapshot.json> \
  --output <external-report-input.json>
```

The output path must resolve outside the guarded repository. The helper does
not write the snapshot, run metadata, turns, controller, or any other guarded
artifact.

Canonical `get_run` example invocation:

```bash
python3 <skill-dir>/scripts/aflow_guard_report_input.py \
  --canonical-observation <absolute-external-directory>/get-run.json \
  --repo <guarded-repo> \
  --run-id <run-id> \
  --ownership-mode <ui-server|aflowd> \
  --observed-at <UTC-ISO-8601-response-time> \
  --output <absolute-external-directory>/guard-report-input.json
```

## Render the explicit `:vr` artifact

`:vr` is report-only for the already pinned repository and run. A legacy report
uses one snapshot with `--no-write`; a `ui-server` or `aflowd` report uses one
authenticated canonical `get_run` observation from its advertised MCP owner
with the explicit canonical command above. In either case, run this builder
before the renderer and keep all paths absolute, external to the guarded
repository:

```bash
uv run --script <skill-dir>/scripts/aflow_guard_report.py \
  --input <absolute-external-directory>/guard-report-input.json \
  --output-dir <absolute-external-directory>
```

The command writes exactly `guard-report.png` and `guard-report.json` in the
external output directory. The PNG is a deterministic 1240×1754 A4 portrait
artifact; the JSON is the normalized schema emitted by this helper. Return the
PNG inline and offer JSON only as optional evidence:

```markdown
![AFlow guard report](/absolute/external/path/guard-report.png)

[Normalized report JSON](/absolute/external/path/guard-report.json)
```

The renderer rejects malformed or wrong-identity input and output locations
inside the guarded repository. It does not create HTML or PDF, send email, use
network assets, invoke generic visualization or image-generation tools, or
authorize recovery. Healthy scheduled observations remain silent; the normal
new-anomaly path still permits only one bounded report per fingerprint before
the exact owner decision is requested.

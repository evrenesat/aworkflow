# Cumulative review — CLI test run isolation

Original and active plan: `plans/in-progress/test-run-isolation-20260909.md`.
Branch: `aflow-test-run-isolation-20260909-20260910-225610`.
Review base: `0ae6c587975731c3065ae1385fdc44a35e776b0f` (unchanged Pre-Handoff Base HEAD).
Reviewed head: `e20c67bf504f50f577611b766177859c72918935`.
New commits: 1. Total handoff commits: 1. Coverage: `cp1 v01`, all of Checkpoint 1 and the full original plan.

No prior findings exist for this handoff; the previous latest review belongs to a different plan and is archived unchanged. The supplied worker result artifact was absent, so verification was independently repeated against actual code.

The full diff changes only tests/test_cli.py and DEVLOG.md. The real workflow override invocation, startup probe mock and published cwd/HOME try/finally remain intact. Assertions read actual run.json and verify one run, temporary repository/run ownership, workflow other, and original/active/current plan paths. Adjacent real run invocations were inspected: they use temporary roots, mock the runner, or exit before run creation. No demonstrated equivalent leak requires an additional change. Production discovery, startup, retention, existing history and concurrent parser changes are preserved.

Independent verification on this execution root:

- `uv run pytest -q tests/test_cli.py -k test_cli_workflow_override`: 1 passed, 156 deselected.
- `uv run pytest -q tests/test_cli.py`: 157 passed, 129 subtests passed.
- Disposable outer proof: initialize a fresh temporary Git repository, invoke the project environment's `sys.executable -m pytest -q <absolute execution-root>/tests/test_cli.py -k test_cli_workflow_override` with subprocess cwd set to it, require exit 0, and assert `(outer / '.aflow').exists()` is false. Passed: 1 test; actual inner durable-run assertions passed. Confirmed imported aflow belongs to this execution root.
- `git diff --check`: passed. Pre-finalization `git status --short`, `git diff --name-only` and `git diff --stat` were empty.

Findings: none admitted by the material-code-review gate. Approved for the final accumulated squash on this managed feature branch. Exactly one final handoff commit must follow the unchanged base, preserving reviewed implementation. No fix overlay is needed. Original plan stays in progress for engine finalization; coordinator owns merge/publication and exact-SHA CI/live checks. Local approval does not establish publication or deployment.

No material findings

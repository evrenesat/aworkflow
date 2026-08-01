# AFlow guard incident: Codex manager prompt exceeds exec argv limit

Date: 2026-08-01T21:24:50Z
Incident fingerprint: fa3c7bcb804f923092df2b34
Guarded repository: /root/code/cdx
AFlow source repository: /root/code/agent_flow
Run: 20260801T212449Z-58d26c0d
Continuation lineage: 20260801T201401Z-94f90816 -> 20260801T212449Z-58d26c0d

## Summary

Make Codex harness prompts independent of operating-system argv size limits and make process-launch failures terminate through AFlow's durable failure paths. The accepted cdx work, active repair plan, lifecycle identity, manager history, and resume semantics must remain unchanged.

The immediate trigger is a resumed pending manager boundary whose effective prompt exceeded Linux's per-argument limit. `CodexAdapter` appended that complete prompt to `argv`; `subprocess.Popen` raised `OSError: [Errno 7] Argument list too long` before Codex started, and the continuation remained recorded as `running` with no controller.

## Git Tracking

- Plan Branch: `aflow-guard-discovered-20260801-codex-manager-prompt-e2b-20260801-223957`
- Pre-Handoff Base HEAD: `afb55b387269832adb1d57ab49251692b3d0e1c3`
- Last Reviewed Checkpoint: `cp2 v01`

## Review Log

- 2026-08-01: Approved Checkpoint 1 through `cp1 v01` using the current-worktree fallback from `afb55b3`; Codex stdin transport, injected-runner parity, large-prompt streaming, and prompt-free argv artifacts passed focused verification. The 10 lifecycle failures in the combined harness/runtime files reproduce unchanged at the pre-handoff base and remain outside this checkpoint.
- 2026-08-01: Approved Checkpoint 2 through `cp2 v01`; default and injected launch errors normalize to bounded nonzero results, and manager, worker, and lifecycle fixtures persist terminal failure evidence. Focused runtime tests, the app-server suite, compilation, and diff hygiene passed. The same 10 pre-existing lifecycle failures remain in the combined harness/runtime files, while the root-level pytest command also encounters the unchanged nested app-server environment collection boundary; the server-local suite passes independently.

## Done Means

- Codex invocations pass the complete effective prompt through stdin using the documented `codex exec -` contract; prompt content is not present in process argv.
- Default subprocess execution and injected runners receive identical prompt bytes and preserve existing stdout, stderr, exit-code, environment, cwd, and final-output behavior.
- An `OSError` raised while starting any harness is normalized into the existing nonzero harness-result path without a raw traceback or a stale `running` run.
- Manager, worker, lifecycle, and recovery call sites preserve their existing response to a nonzero harness result; this plan does not alter workflow routing policy.
- A fixture with a prompt larger than 128 KiB reaches a stub Codex process intact and a fixture whose process launch raises `E2BIG` ends with durable failure evidence.
- Focused harness/runtime tests, the app-server suite, compilation, and diff checks pass. Broader pre-existing failures are reported separately.

## Objective

Remove prompt size from the Codex argv boundary and ensure that a harness executable which cannot be launched produces an auditable AFlow failure instead of escaping the controller and leaving orphaned `running` metadata.

## Impact And Recovery Status

- Guarded checkpoint and workflow step: Checkpoint 2 repair scope, pending `review_cp_implementation` manager boundary before `implement_plan`.
- Controller/process state: the authorized resume created continuation `20260801T212449Z-58d26c0d`; it exited before turn 1 with status 1. No AFlow or Codex controller remains.
- Preserved worktree and plan state: the existing feature branch, `/root/code/worktrees/aflow-cdx-v0-local-session-search-navigation-20260801-201401`, original plan, and `cdx-v0-local-session-search-navigation-cp03-v02.md` remain intact.
- Operational workaround attempted: the owner-authorized resume supplied all frozen original arguments and successfully resolved the source run, then failed at the first manager process launch.
- Scheduler state: heartbeat `guard-cdx-20260801t201401z-94f90816` remains paused. No further automatic recovery is authorized.

## Evidence

- Source run: `/root/code/cdx/.aflow/runs/20260801T201401Z-94f90816/run.json`; its finalized turn 4 selected `implement_plan` and wrote `cp03-v02`.
- Continuation: `/root/code/cdx/.aflow/runs/20260801T212449Z-58d26c0d/run.json` records `resumed_from_run_id=20260801T201401Z-94f90816`, `turns_completed=0`, `active_plan_path=...cp03-v02.md`, and stale `status=running` / `status_message=initializing`.
- Large durable input: `/root/code/cdx/.aflow/runs/20260801T212449Z-58d26c0d/scopes/851e8e0d34841ed38c5f14957a7a3a5154a97a1b40c3741b4e882ed1ca09172f/envelope.json` is 194,991 bytes.
- Bounded failure: tmux pane `cdx-aflow-luna-xhigh:0.0` exited status 1 at 2026-08-01T21:24:50Z; its tail shows `OSError: [Errno 7] Argument list too long: 'codex'` from `aflow/workflow.py::_run_process` during `_run_manager_call`.
- Process evidence: zero matching `aflow run` or `codex exec` processes after the failure.
- Source: `aflow/harnesses/codex.py` appends `effective_prompt` to argv; `aflow/workflow.py::_run_process` passes that argv directly to `subprocess.Popen` and does not normalize launch-time `OSError`.
- Local CLI contract: `codex exec --help` states that an omitted prompt or `-` reads instructions from stdin.

## Classification

Harness adapter/recovery. The cdx reviewer completed normally and produced a valid repair plan. AFlow then failed before Codex started because its adapter chose an OS-size-limited prompt transport, and its subprocess boundary allowed the launch exception to bypass durable terminal-state handling.

## Minimal Reproduction

1. Build a `CodexAdapter` invocation with a combined system and user prompt larger than 128 KiB.
2. Execute it through `_run_process` on Linux, or monkeypatch `subprocess.Popen` to raise `OSError(errno.E2BIG, "Argument list too long")` for the deterministic unit case.
3. Current result: the prompt is one argv element, process creation raises, and a workflow that is still initializing can retain `status=running`.
4. Required result: Codex argv contains `-` rather than prompt content, stdin contains the exact effective prompt, and a forced launch failure becomes a nonzero completed harness result handled by the existing durable failure path.

## Root-Cause Hypothesis

- Fact: `CodexAdapter.build_invocation()` stores the complete effective prompt as `argv[-1]`.
- Fact: `_run_process()` calls `subprocess.Popen(list(invocation.argv), ...)` without stdin prompt transport or an `OSError` boundary.
- Fact: the failed continuation has a 194,991-byte scope envelope and `Popen` raised `E2BIG` before any manager artifacts or workflow turn were finalized.
- Inference: the effective manager prompt exceeded Linux's single-argument limit even though the total process environment and argv remained below aggregate `ARG_MAX`.
- This hypothesis is falsified if a captured reproduction shows the effective prompt below the platform's single-argument limit or shows another oversized argv element. The implementation must measure the constructed fixture and assert which element is removed.

## Critical Invariants

- Preserve `system_prompt`, `user_prompt`, and `effective_prompt` exactly; only transport changes.
- Keep prompt logging in the existing dedicated prompt artifacts. Do not reintroduce prompt content into `argv.json`, environment variables, temporary files, shell command strings, or process titles.
- Never use `shell=True`, shell redirection, a fixed temporary filename, prompt truncation, or environment variables as a prompt transport.
- Feed stdin concurrently with stdout/stderr draining so prompts larger than pipe capacity cannot deadlock.
- Close stdin deterministically and tolerate `BrokenPipeError` only when the child has already exited; preserve its real return code and stderr.
- Normalize only process-creation `OSError`; exceptions in AFlow's own persistence, parsing, or state logic must continue to fail visibly.
- Do not change manager decisions, fallback-team selection, retry counts, scope lineage, resume matching, or cdx content.
- A launch failure must not be classified as a successful harness turn and must not consume or mutate the active plan.

## Forbidden Implementations

- Do not raise Linux `ARG_MAX`, shorten manager context, discard history, truncate plans, or special-case this run ID.
- Do not write prompts to repository files or pass their paths through an undocumented Codex behavior.
- Do not catch broad `Exception` around workflow execution or silently return success after launch failure.
- Do not add a Codex-only branch to manager logic; harness-specific transport belongs in the adapter/invocation contract.
- Do not retain both inline and stdin copies of the Codex prompt.
- Do not automatically resume the guarded cdx run after implementation. A new owner authorization is required because the authorized retry has been consumed.

## Proposed Changes

- `aflow/harnesses/base.py`: extend `HarnessInvocation` with optional stdin payload metadata that survives `for_final_output()`.
- `aflow/harnesses/codex.py`: use the documented `codex exec -` argv form, set the exact effective prompt as stdin, and identify the prompt mode as stdin.
- `aflow/workflow.py`: feed invocation stdin safely in `_run_process`; centralize equivalent keyword construction for injected runners; normalize process-creation `OSError` into a nonzero `CompletedProcess` with a concise prompt-free diagnostic.
- `aflow/runlog.py`: inspect only; preserve separate effective-prompt logging and ensure `argv.json` records `-`, not prompt content. Modify only if its existing serializer needs explicit stdin metadata redaction.
- `tests/test_harnesses.py`: update Codex adapter contract tests for stdin transport and prompt-free argv.
- `tests/test_runtime.py`: cover large stdin delivery, no-deadlock behavior, injected-runner parity, launch-time `E2BIG`, manager artifact persistence, and durable failed status.
- `ARCHITECTURE.md`: document Codex stdin transport and the normalized process-launch boundary.
- `DEVLOG.md`: if still absent, create it before production edits with one concise entry for this resilience change; do not backfill project history.
- `README.md`: no change expected because the user-facing CLI contract is unchanged. Update only if implementation changes documented setup or troubleshooting behavior.

## Checkpoints

### [x] Checkpoint 1: Make Codex prompt transport argv-size-safe

**Goal:**

- Deliver every Codex effective prompt through stdin without changing prompt content or harness behavior.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `aflow/harnesses/base.py`, `aflow/harnesses/codex.py`, `aflow/workflow.py::_run_process`, all direct injected `runner(...)` call sites, `aflow/runlog.py`, `tests/test_harnesses.py`, and `tests/test_runtime.py`.
- Preserve: adapter selection, model/effort flags, cwd, environment, final-output mode, stdout/stderr capture, banner updates, and prompt artifacts.

**Scope:**

- May create or modify: `aflow/harnesses/base.py`, `aflow/harnesses/codex.py`, `aflow/workflow.py`, `aflow/runlog.py` only if required, `tests/test_harnesses.py`, `tests/test_runtime.py`, and `DEVLOG.md` if absent.
- Must not touch: other harness adapters, workflow routing configuration, manager prompt/context construction, resume semantics, guarded cdx files, or generated app assets.
- Constraints: use a typed optional stdin field on `HarnessInvocation`; Codex argv must end in `-`; the stdin value must equal `effective_prompt` byte-for-byte after Python text encoding.

**Steps:**

- [x] If `DEVLOG.md` is absent, create a concise current-status log and record this incident without inventing prior history.
- [x] Add an optional `stdin_text: str | None` field to `HarnessInvocation`, defaulting to `None`, and prove `for_final_output()` preserves it.
- [x] Change `CodexAdapter` to append `-` instead of the effective prompt, set `stdin_text=effective_prompt`, and set `prompt_mode="stdin"`; keep every existing flag and its ordering stable.
- [x] Update `_run_process` to request `stdin=PIPE` only when `stdin_text` exists, start stdout/stderr drains, feed and close stdin on a dedicated thread, join all streams, and avoid deadlock for payloads larger than pipe capacity.
- [x] Add one helper for injected runners so every invocation path passes `input=stdin_text` when present and omits `input` otherwise; replace repeated runner calls without changing their other kwargs.
- [x] Preserve dedicated prompt artifacts and assert that Codex `argv.json` contains `-` but no prompt text.
- [x] Update adapter/runtime tests for the new contract, including a greater-than-128-KiB sentinel prompt whose length and digest are verified by a stub child.

**Dependencies:**

- None.

**Verification:**

- Run: `uv run pytest -q tests/test_harnesses.py -k codex`
- Run: `uv run pytest -q tests/test_runtime.py -k 'run_process or prompt or injected_runner'`
- Run: `uv run python -m compileall -q aflow`
- Observe: the large sentinel is received exactly through stdin, Codex argv contains no sentinel bytes, stdout/stderr capture remains concurrent, and all other adapters retain `stdin_text=None`.

**Done When:**

- Codex prompt size no longer contributes to argv length, default and injected runners agree on stdin delivery, and focused tests pass.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if the installed Codex CLI no longer supports stdin prompt input with `-`.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 2: Persist process-launch failures through normal terminal paths

**Goal:**

- Ensure a harness that cannot be created yields bounded artifacts and terminal run metadata instead of an uncaught traceback and orphaned `running` state.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: every `_run_process` and injected-runner caller in `aflow/workflow.py`, manager result persistence around `_run_manager_call`, turn finalization, lifecycle handoffs, `write_run_metadata`, and existing nonzero-return tests.
- Preserve: the existing policy decisions made after an ordinary nonzero harness exit.

**Scope:**

- May create or modify: `aflow/workflow.py`, `tests/test_runtime.py`, `ARCHITECTURE.md`, and `DEVLOG.md`.
- Must not touch: manager action schema, recovery rule configuration, retry limits, plan contents, worktree teardown rules, CLI resume parsing, or app-server APIs.
- Constraints: normalize only `OSError` raised by process creation; produce a nonzero `CompletedProcess` with empty stdout and a concise stderr containing harness label, errno, and OS message but no prompt or full argv.

**Steps:**

- [x] Add a small process-launch normalization helper used by both default `Popen` execution and injected runners. Map `FileNotFoundError` to return code 127 and other launch-time `OSError` values, including `EACCES` and `E2BIG`, to return code 126.
- [x] Route the synthetic nonzero result through each caller's existing nonzero-harness handling rather than adding new manager or workflow transitions.
- [x] Prove manager invocation failure writes manager result/error artifacts and ends the run durably instead of escaping before persistence; if Lite-to-Full fallback is existing behavior, keep it and make both launch failures auditable.
- [x] Prove worker and lifecycle launch failures finalize their existing artifacts and set `run.json.status=failed`; they must not record plan progress or successful turns.
- [x] Add regression coverage for `errno.E2BIG`, missing executable, and a child that closes stdin early. Assert no raw prompt, full argv, or traceback is stored in the concise launch diagnostic.
- [x] Update `ARCHITECTURE.md` at the harness adapter and subprocess execution sections. Record the completed behavior in `DEVLOG.md`; leave `README.md` unchanged unless a user-facing contract actually changes.

**Dependencies:**

- Checkpoint 1.

**Verification:**

- Run: `uv run pytest -q tests/test_runtime.py -k 'launch or popen or manager or harness_failed'`
- Run: `uv run pytest -q tests/test_harnesses.py tests/test_runtime.py`
- Run: `(cd apps/aflow_app/server && uv run pytest -q)`
- Run: `uv run pytest -q`
- Run: `uv run python -m compileall -q aflow`
- Run: `git diff --check`
- Observe: a forced `E2BIG` never escapes as an uncaught traceback; the run is terminally failed with bounded artifacts and zero controllers.

**Done When:**

- Launch failures from all harness invocation paths are represented as existing nonzero results, durable run state is terminal, documentation matches behavior, and focused verification passes.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if any lifecycle caller cannot safely reuse existing nonzero-result handling without changing workflow meaning.
- Stop and report if unrelated dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

1. Given a Codex system/user prompt whose effective text exceeds 128 KiB, when AFlow launches the harness, then argv contains the normal flags plus `-`, stdin contains the exact effective prompt, and the child starts without `E2BIG`.
2. Given a small Codex prompt, when AFlow launches it, then it uses the same stdin path and produces the same effective instructions and output as before.
3. Given any non-Codex adapter, when its invocation is built and run, then its existing argv and prompt behavior remain unchanged and stdin is omitted unless that adapter explicitly supports it in a future change.
4. Given a child that emits enough stdout/stderr to fill pipe buffers while consuming a large stdin prompt, when `_run_process` waits, then all three streams complete without deadlock and output is captured exactly.
5. Given `Popen` raises `OSError(errno.E2BIG, ...)` during a manager boundary, when the controller handles the result, then bounded manager failure artifacts exist, the run becomes terminally failed, and no controller remains.
6. Given a missing harness executable or permission error during a worker/lifecycle handoff, when launch fails, then the existing nonzero path records return code 127 or 126 respectively and never reports plan progress.
7. Given the failed cdx continuation artifacts, when a disposable fixture reconstructs an equivalent large manager context after the fix, then the manager process starts and receives the complete prompt. Do not run the live cdx continuation as this acceptance test.

## Plan-to-Verification Matrix

- Prompt removed from Codex argv -> `tests/test_harnesses.py` exact argv/stdin assertions and `argv.json` regression.
- Large prompt delivered intact -> stub-child length/digest test in `tests/test_runtime.py`.
- No pipe deadlock -> concurrent stdin/stdout/stderr stress test with a bounded timeout.
- Injected runner parity -> capturing-runner tests assert `input`, cwd, env, capture, text, and check kwargs.
- Launch errors become nonzero results -> deterministic `Popen`/runner `OSError` tests for errno 7, 13, and missing executable.
- Durable manager/worker/lifecycle failure -> fixture-backed `run.json` and artifact assertions.
- Other harnesses unchanged -> full `tests/test_harnesses.py` and runtime suite.
- App-server compatibility -> server-local pytest suite.
- Syntax and patch hygiene -> compileall and `git diff --check`.

## Recovery And Rollout

1. Implement and verify this plan in the AFlow source checkout without touching the cdx worktree.
2. Reinstall from the verified checkout with `uv tool install -e . --force` and confirm `readlink -f "$(command -v aflow)"` resolves through the editable tool environment to this checkout.
3. Before any live cdx resume, verify zero AFlow/Codex controllers, the preserved worktree/branch, source-to-continuation lineage, `luna-xhigh`, `implement_plan`, and active `cp03-v02`.
4. Obtain fresh owner authorization. The authorization consumed in incident `fa3c7bcb804f923092df2b34` does not permit another resume.
5. Launch once in `cdx-aflow-luna-xhigh`, require exactly one controller and a validated new continuation, then retarget and unpause the single heartbeat. If verification fails, keep the heartbeat paused and preserve all cdx state.
6. Roll back only the AFlow source changes and editable installation; never reset or discard the cdx worktree or plans.

## Non-Goals And Owner Boundary

- Do not alter manager context size, accepted cdx implementation, plan scope, review findings, resume lineage, model/team configuration, or recovery-attempt policy.
- Do not implement a general stdin migration for other harnesses without a separate verified CLI contract.
- Do not resume or reset the guarded workflow automatically after this fix.
- Ask the owner only if preserving exact prompt semantics or existing nonzero-result routing proves impossible, or before another operational resume.

## Assumptions And Defaults

- The p100 Codex CLI contract observed on 2026-08-01 remains authoritative for this repair: no prompt or `-` means read prompt from stdin.
- UTF-8 text mode remains the transport default because AFlow already uses `text=True` and stores prompt artifacts as UTF-8 text.
- Always using stdin for Codex is safer and simpler than a platform-specific size threshold; there is no dual transport path.
- Existing manager and harness recovery behavior for a normal nonzero return code remains the policy source of truth.
- `plans/todo/` is used for this file because the owner explicitly requested guard-discovered AFlow fixes there, overriding the planning skill's usual `plans/in-progress/` destination.

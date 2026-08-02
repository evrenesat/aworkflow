# AFlow SSH Dashboard Cadence, Flat Rendering, And Scrolling

## Summary

Make AFlow's Rich live dashboard calmer over SSH/GNU Screen, clean to copy, and usable when its complete content exceeds the terminal height. Keep every currently displayed datum, but replace the outer panel, turn cards, rejection-history panel, and workflow-step boxes with one borderless single-column document whose headings and explicit state labels preserve the meaning currently carried by borders, color, and side-by-side placement.

Disable Rich auto-refresh and retain one AFlow-owned render loop with a three-second steady-state cadence. On an interactive POSIX terminal, show that document through an in-process, alternate-screen viewport with less-style line/page/top/bottom navigation. The renderer owns terminal input only after startup questions finish; harness subprocesses become explicitly non-interactive on stdin so they cannot race dashboard keystrokes. Non-TTY and unsupported terminals keep a borderless live-display fallback without raw-mode input. Stop, pause, failure, and interruption paths restore terminal attributes and leave one complete borderless snapshot in normal scrollback before any terminal report.

This handoff changes AFlow itself. It does not change workflow routing, public CLI/configuration, run artifacts, manager behavior, harness prompts, GNU Screen attachment semantics, or external launcher scripts.

## Git Tracking

- Plan Branch: `aflow-ssh-dashboard-refresh-cadence-20260801-214511`
- Pre-Handoff Base HEAD: `c9616c50d93106024ebdfd79b892c174136fb0de`
- Last Reviewed Checkpoint: `cp5 v01`
- Review Log:
  - `cp1 v02`: approved the fresh pause snapshot repair; pause joins the render thread, installs the newest coalesced state and context without an eager refresh, and relies on `Live.stop()` for the single lifecycle repaint.
  - `2026-08-01`: Checkpoint 3 reviewed via current-worktree fallback because no `cp3 vNN` commit boundary exists. Outcome: changes requested. Unsupported CSI sequences can be ignored as one chunk but decoded as navigation when split immediately before an ASCII navigation final byte; focused repairs are in `plans/in-progress/ssh-dashboard-refresh-cadence-cp04-v01.md`.
  - `cp3 v02`: approved the chunk-invariant decoder repair; unsupported `ESC [ 2 G` and `ESC [ 9 j` sequences are discarded atomically across every split, pending escape input is bounded at 64 bytes with recovery, and the 36 viewport plus 22 banner/status test baseline passes.
  - `cp4 v01`: approved via current-worktree fallback because no Checkpoint 4 commit boundary existed; real harness children receive `stdin=subprocess.DEVNULL`, the EOF/capture regression and all 27 adapter tests pass, and the 10 combined-suite lifecycle failures are outside this checkpoint's real-subprocess path.
  - `2026-08-02`: Checkpoint 5 reviewed via current-worktree fallback from `cp4 v01`; changes requested because repeated navigation/resize wakes can indefinitely bypass an already-due Git poll, background render failure leaves the interactive Live/session attached, and session-only `atexit` cleanup restores termios without exiting the alternate screen or showing the cursor. The same 12 lifecycle failures reproduce at `cp4 v01`, so they are unrelated baseline failures. Focused repairs are in `plans/in-progress/ssh-dashboard-refresh-cadence-cp01-v01.md`.
  - `2026-08-02`: The Checkpoint 5 `cp01-v01` repair was reviewed from the current worktree; changes remain requested because overlapping normal cleanup and a background Rich failure deadlock while each waits on the other, and refresh-thread startup failure leaves a started interactive Live/session attached. All focused checks pass, and the combined suite remains at 421 passed with the same 12 unrelated lifecycle failures. Focused repairs are in `plans/in-progress/ssh-dashboard-refresh-cadence-cp01-v02.md`.
  - `cp5 v01`: approved the deadlock-free cleanup ownership handoff and transactional startup repair; coordinated Rich-failure cleanup and both render-thread startup rollback paths terminate without stranded resources or duplicate snapshots. All focused checks pass, and the combined suite reports 424 passed with only the same 12 unrelated lifecycle baseline failures.

## Done Means

- Rich automatic refresh is disabled; one AFlow render loop owns every steady-state, interaction, resize, and lifecycle repaint.
- Ordinary state/context changes coalesce into the next repaint, with no more than one steady-state repaint every three seconds; intentional key, resize, start/resume, pause/stop, and final-snapshot paints are explicit exceptions.
- The live document contains every field and artifact path currently emitted by `build_banner()`, in deterministic single-column reading/copy order and without box borders.
- Workflow active/inactive/excluded/skipped state and turn/review status remain understandable in plain exported text without relying on color.
- Interactive POSIX TTY runs support `k`/Up, `j`/Down, `b`/PageUp, `f`/Space/PageDown, `g`/Home, and `G`/End; manual scrolling remains stable while content updates, and `G`/End resumes follow-tail.
- Terminal resize clamps the viewport and redraws once without corrupting the screen. Unknown keys are ignored, and `q` is not bound to workflow termination.
- Interactive mode uses the alternate screen and restores the original terminal attributes, cursor, and normal screen exactly once on normal stop, pause/resume, setup failure, renderer failure, and interruption cleanup.
- A complete borderless final snapshot is printed once in normal scrollback after leaving the alternate screen; manager reports remain visible exactly once after it.
- Harness prompt delivery and output capture remain unchanged, while real harness subprocesses receive `stdin=subprocess.DEVNULL` so viewport input has one owner.
- Elapsed time remains current to within three seconds, Git summaries retain their 10-second polling default, and Rich-unavailable behavior remains a no-op.
- Focused renderer, viewport, subprocess, CLI-report, harness, and documentation checks pass.

## Critical Invariants

- `BannerRenderer` is the only owner of Rich `Live` lifecycle. Its render thread is the only background thread allowed to call `Live.update()` or `Live.refresh()`; the input thread may only enqueue navigation/resize work and wake that render thread.
- There is at most one steady-state repaint per `refresh_interval_seconds`; the production default is `3.0` seconds. An interaction/resize repaint resets the next periodic deadline so it cannot be followed by an immediate duplicate tick.
- The newest `ControllerState`, mutable banner context, Git summary, viewport action, and terminal size available at a repaint are rendered together. Multiple ordinary state updates inside an interval coalesce and the newest state wins.
- Git collection remains independently due every `git_poll_interval_seconds` (default `10.0`), remains non-fatal, and is never accelerated merely by scrolling.
- The flattened document preserves every conditional row, history record, workflow transition/condition, artifact link, and summary value currently produced by `_render_review_rejection_history()`, `_render_turn_history()`, `_render_workflow_graph()`, and `_build_summary_table()`.
- Plain exported text must identify workflow-step visual state explicitly. Color may reinforce state but must not be the only carrier of active, inactive, excluded, or skipped meaning.
- Interactive viewport mode is enabled only when Rich is available, the dashboard console is a terminal, stdin is a POSIX TTY with a valid file descriptor, and terminal attributes can be saved. Any failed capability/setup check falls back without failing the workflow.
- POSIX cbreak mode disables canonical input and echo but preserves `ISIG`, so Ctrl-C remains an operating-system interrupt rather than a dashboard command.
- The input reader never invokes workflow transitions, stops the run, mutates controller state, polls Git, prints, or calls Rich directly.
- Terminal/session cleanup is idempotent. Refresh and input threads stop before `Live.stop()`; saved terminal attributes are restored before control returns to workflow/CLI reporting.
- Interactive stop/pause exits the alternate screen before printing one full borderless snapshot to normal scrollback. Non-interactive `Live` stop must not print a duplicate snapshot.
- Harness adapters continue to place their effective prompt in existing argv/flag forms. Closing child stdin must not remove, rewrite, or duplicate prompt text.
- Manager stop reports remain visible exactly once and after the final dashboard snapshot.
- Public flags, TOML schemas, workflow topology, manager decisions, run metadata, artifact contents, and files-limit semantics remain unchanged.

## Forbidden Implementations

- Do not retain `refresh_per_second=4`, enable another Rich auto-refresh rate, call Rich from both render and input threads, or add a second repaint scheduler.
- Do not wake/repaint synchronously for ordinary `BannerRenderer.update()` or `set_context()` calls.
- Do not remove, truncate beyond existing limits, hide behind a collapsed section, or conditionally omit existing dashboard information to make the viewport fit.
- Do not replace borders with equally noisy ASCII/Unicode boxes, or retain a two-column layout whose exported text interleaves dashboard and workflow content.
- Do not rely on color alone for workflow state or introduce Rich markup interpretation for controller/reviewer-owned text.
- Do not shell out to `less`, `more`, `tmux`, `screen`, `tput`, `stty`, or another pager/process. Do not add Textual, prompt-toolkit, curses wrappers, or a new third-party dependency.
- Do not use terminal scrollback control sequences as the viewport model, retain an unbounded `vertical_overflow="visible"` render in interactive mode, or redraw from an input callback.
- Do not install global SIGWINCH/SIGINT handlers. Detect size changes by comparing `Console.size` from the input-wait loop, and preserve terminal `ISIG` for Ctrl-C.
- Do not bind `q`, Ctrl-C, or Escape to workflow termination. Unknown/incomplete escape sequences must be ignored safely.
- Do not let both the child harness and dashboard read the same TTY. Do not modify harness argv, effective prompts, environment, stdout/stderr capture, or result parsing when setting child stdin to `DEVNULL`.
- Do not activate cbreak/alternate-screen mode for non-TTY, non-POSIX, Rich-unavailable, injected-recording-console, or terminal-setup-failure cases.
- Do not leave cbreak mode, hidden cursor, alternate screen, live threads, or input threads active after stop, pause, failed start, or interpreter cleanup.
- Do not detect SSH, branch on `TERM`/GNU Screen names, change Screen attachment behavior, add a public refresh/scroll setting, or modify external launcher files.
- Do not create a general terminal UI framework or move workflow/control logic into the viewport module.
- Do not modify root `AGENTS.md`.

## Checkpoints

### [x] Checkpoint 1: Make live-banner repainting AFlow-owned and three-second bounded

**Goal:**

- Replace Rich's independent four-FPS loop with one explicit periodic repaint path while preserving current banner content and lifecycle behavior.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `cat aflow/AGENTS.md; git status --short; git diff --name-only`
- Inspect: `sed -n '800,1010p' aflow/status.py; sed -n '1060,1130p' tests/test_harnesses.py`
- Inspect: `rg -n "BannerRenderer|refresh_per_second|auto_refresh|manager_report_remains_visible" aflow tests docs ARCHITECTURE.md`
- Preserve: all existing `build_banner()` content, Rich-unavailable no-ops, the independent 10-second Git poll, and exactly-once manager-report output.

**Scope:**

- May create or modify: `aflow/status.py`, `tests/test_harnesses.py`, `tests/test_cli.py`, `docs/runtime-behavior.md`, `ARCHITECTURE.md`, `devlog/DEVLOG.md`
- Must not touch: `aflow/workflow.py`, `aflow/cli.py`, manager/config/run-state code, harness adapters, web code, bundled skills, external files, or root `AGENTS.md`
- Constraints: use `Live(auto_refresh=False)` for start and resume; default `refresh_interval_seconds=3.0`; wait one interval before the first periodic repaint; poll Git only when its own deadline is due; perform one explicit `Live.update(..., refresh=False)` plus `Live.refresh()` per repaint; ordinary `update()`/`set_context()` only store the newest state/context; stop the thread before pause/stop and perform at most one final lifecycle paint.

**Steps:**

- [x] Change start/resume construction and the refresh loop to disable Rich auto-refresh, wait before the first tick, and explicitly refresh once per due tick.
- [x] Make ordinary state/context updates coalescing-only while preserving safe lock/thread ordering and one correct final stop render.
- [x] Replace the timing regression with deterministic fake-`Live` coverage for constructor options, no immediate duplicate repaint, coalescing, explicit tick refresh, start/resume parity, and no calls after pause/stop.
- [x] Retain the real-banner CLI regression proving manager reports remain visible once.
- [x] Update cadence descriptions in runtime/architecture docs and add a concise dated devlog entry; leave README and `aflow/AGENTS.md` unchanged because neither owns detailed refresh mechanics.

**Dependencies:**

- None.

**Verification:**

- Run: `uv run python -m compileall -q aflow tests`
- Run: `uv run pytest -q tests/test_harnesses.py -k banner_renderer`
- Run: `uv run pytest -q tests/test_cli.py -k manager_report_remains_visible`
- Run: `uv run pytest -q tests/test_harnesses.py tests/test_cli.py`
- Run: `test "$(rg -n "refresh_per_second=4" aflow tests | wc -l | tr -d ' ')" = "0"`
- Run: `rg -n "auto_refresh=False|refresh_interval_seconds: float = 3.0" aflow/status.py tests/test_harnesses.py`
- Observe: state updates do not repaint immediately; the first periodic repaint occurs only after the interval; elapsed time continues advancing without controller events.

**Done When:**

- One render thread owns steady-state repainting, Rich auto-refresh is absent, and lifecycle/report behavior is unchanged.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if Rich cannot provide `auto_refresh=False` with explicit refresh under the declared dependency.
- Stop and report if correct final/report ordering requires workflow, manager, or CLI control-flow changes in this checkpoint.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 2: Flatten the dashboard without losing displayed information

**Goal:**

- Produce one borderless, single-column Rich document whose exported text remains complete, ordered, and semantically understandable.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `sed -n '200,810p' aflow/status.py`
- Inspect: `sed -n '500,1020p' tests/test_harnesses.py; sed -n '1,220p' tests/test_status.py`
- Inspect: `sed -n '360,405p' docs/runtime-behavior.md; rg -n "Panel\(|border_style|build_banner|workflow graph|turn history|review history" aflow/status.py tests docs ARCHITECTURE.md`
- Preserve: every existing conditional row/value, existing file-list limit, literal untrusted text handling, workflow transition conditions, history order, and styling as optional reinforcement.

**Scope:**

- May create or modify: `aflow/status.py`, `tests/test_harnesses.py`, `tests/test_status.py`, `docs/runtime-behavior.md`, `ARCHITECTURE.md`, `devlog/DEVLOG.md`
- Must not touch: renderer scheduling/input lifecycle, `aflow/workflow.py`, CLI/config/run-state/manager/harness code, workflow-show output (`build_workflow_show()` and its panel-based presentation), web code, or root `AGENTS.md`
- Constraints: change only the live dashboard returned by `build_banner()`; retain the plan title as a text heading; render review history, turns, workflow graph, and summary as labeled vertical sections separated by whitespace or short headings, not frames; replace side-by-side root columns with deterministic single-column order: title, review history when present, chronological turn history, workflow graph, summary/status; add explicit `[active]`, `[inactive]`, `[excluded]`, or `[skipped]` workflow-step text; retain turn outcome and rejection ordinals in plain text; keep controller-owned strings as literal `Text`.

**Steps:**

- [x] Refactor the live-only render helpers to return borderless `Group`/`Table.grid`/`Text` sections and make `build_banner()` return the single-column document rather than an outer `Panel`.
- [x] Preserve all prior rows and paths, and add explicit workflow-state labels wherever the old border/color carried meaning not present in exported text.
- [x] Add a maximal fixture covering resumed identity, override state, checkpoint/turn fields, rejection and reimplementation history, manager/repartition state, Git/files, issues, workflow transitions, and final status; assert every sentinel appears once in exported text.
- [x] Add structural assertions that the live document contains no `Panel`/box-border glyphs and that section ordering is stable at narrow and wide console widths.
- [x] Update live-status/architecture documentation and devlog; explicitly state that `build_workflow_show()` remains unchanged because this request targets the live dashboard only.

**Dependencies:**

- Checkpoint 1.

**Verification:**

- Run: `uv run python -m compileall -q aflow tests`
- Run: `uv run pytest -q tests/test_harnesses.py -k "banner and not renderer" tests/test_status.py`
- Run: `uv run pytest -q tests/test_harnesses.py tests/test_status.py`
- Run: `rg -n "\[active\]|\[inactive\]|\[excluded\]|\[skipped\]" aflow/status.py tests/test_harnesses.py`
- Run: `test "$(rg -n "return Panel\(root|title=title, border_style=\"blue\"" aflow/status.py | wc -l | tr -d ' ')" = "0"`
- Observe: exported live-dashboard text is linear and borderless, while maximal-fixture sentinels prove no prior field or artifact path disappeared.

**Done When:**

- Copying rendered dashboard text yields clean section headings/labels without box borders or interleaved columns, and every prior datum remains present.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if a current visual state cannot be represented explicitly without changing controller/run-state contracts.
- Stop and report if flattening would require changing the separate workflow-show command.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 3: Add a pure scrollable viewport model and Rich renderable

**Goal:**

- Implement and unit-test scrolling, follow-tail, line slicing, footer, and key decoding without yet taking terminal control.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `sed -n '1,1040p' aflow/status.py; python -c "from rich.console import ConsoleOptions; import inspect; print(inspect.signature(ConsoleOptions.update))"`
- Inspect: `python -c "from rich.live import Live; from rich.screen import Screen; import inspect; print(inspect.signature(Live)); print(inspect.signature(Screen))"`
- Preserve: Rich as an optional dependency and the borderless document from Checkpoint 2 as the authoritative content source.

**Scope:**

- May create or modify: `aflow/terminal_viewport.py`, `tests/test_terminal_viewport.py`, `aflow/status.py` only for type/import seams if required
- Must not touch: `BannerRenderer` lifecycle/scheduling behavior, termios/select/thread code, workflow subprocesses, docs, config, CLI, or run-state code
- Constraints: import the viewport module only inside the Rich-available path so importing AFlow still works without Rich; represent navigation as typed actions; reserve exactly one interactive footer row; render the complete source document to Rich segment lines before slicing; never stringify/reparse ANSI; clamp offsets after content/size changes; default to follow-tail; line/page/top actions disable follow-tail; bottom action sets `follow_tail=True`; page distance is `max(1, viewport_body_height - 1)`; unknown/incomplete key sequences produce no action.

**Steps:**

- [x] Add a small viewport state/model with `offset`, `follow_tail`, content-height/viewport-height clamping, line/page/top/bottom actions, and explicit bottom-follow semantics.
- [x] Add a Rich renderable that preserves segment styles, slices only complete rendered lines, pads/crops to the current terminal body height, and appends a one-line controls/position footer.
- [x] Add an incremental key decoder for `k`/Up, `j`/Down, `b`/PageUp, `f`/Space/PageDown, `g`/Home, and `G`/End; ignore `q`, Escape alone, malformed/unknown sequences, and unrelated UTF-8 safely.
- [x] Unit-test short/long content, narrow widths, every navigation action, appended content in follow/manual modes, resize up/down, offset clamping, footer position, style preservation, and split escape sequences.

**Dependencies:**

- Checkpoint 2.

**Verification:**

- Run: `uv run python -m compileall -q aflow tests`
- Run: `uv run pytest -q tests/test_terminal_viewport.py`
- Run: `uv run pytest -q tests/test_harnesses.py -k banner tests/test_status.py`
- Observe: a synthetic document taller than the console can reach every source line; manual position stays stable when lines append; `G` resumes following newly appended tail content; no ANSI round-trip occurs.

**Done When:**

- Viewport/key behavior is deterministic and fully testable without a real terminal or background thread.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if preserving Rich segments requires ANSI string capture/reparse or private Rich internals rather than public render protocols.
- Stop and report if the footer or slicing path drops/truncates source lines beyond the selected viewport.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 4: Give dashboard input exclusive ownership during harness execution

**Goal:**

- Make the already non-interactive harness subprocess boundary explicit so future dashboard key reads cannot race a child process on stdin.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `sed -n '1280,1335p' aflow/workflow.py; sed -n '1,100p' aflow/harnesses/*.py`
- Inspect: `sed -n '1325,1405p' tests/test_runtime.py; rg -n "HarnessInvocation|prompt_mode|_run_process|Popen" aflow tests`
- Preserve: each adapter's argv/environment/effective-prompt contract, stdout/stderr pipe draining, return codes, polling, artifact capture, and injected `runner` behavior.

**Scope:**

- May create or modify: `aflow/workflow.py`, `tests/test_runtime.py`, `docs/runtime-behavior.md`, `ARCHITECTURE.md`, `devlog/DEVLOG.md`
- Must not touch: harness adapter argv/prompt construction, `BannerRenderer`, viewport code, CLI/config/run-state/manager code, or external harness configuration
- Constraints: set `stdin=subprocess.DEVNULL` only on the real `subprocess.Popen` path in `_run_process()`; do not add `input=`, write prompts to stdin, or alter injected runner calls; verify all declared adapters already deliver prompts by argv/flags before making the change.

**Steps:**

- [x] Add `stdin=subprocess.DEVNULL` to `_run_process()` and document why the controller/dashboard, not captured harnesses, owns the terminal after startup.
- [x] Extend the subprocess regression with a child that reads stdin and proves immediate EOF while stdout/stderr capture and banner updates remain unchanged.
- [x] Run existing adapter tests to prove prompt argv/flag construction is unchanged, and update architecture/devlog with this terminal-ownership boundary.

**Dependencies:**

- Checkpoint 1; may be implemented after Checkpoint 3 as ordered here.

**Verification:**

- Run: `uv run python -m compileall -q aflow tests`
- Run: `uv run pytest -q tests/test_runtime.py -k run_process`
- Run: `uv run pytest -q tests/test_harnesses.py -k adapter`
- Run: `uv run pytest -q tests/test_runtime.py tests/test_harnesses.py`
- Run: `rg -n "stdin=subprocess.DEVNULL" aflow/workflow.py tests/test_runtime.py`
- Observe: the child receives EOF on stdin, prompts remain present in adapter argv/flags, captured output and return status are unchanged, and no terminal key can be consumed by a harness.

**Done When:**

- Real harness subprocesses cannot read the dashboard TTY, with no prompt/output/runtime regression.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if any configured adapter actually relies on stdin for its effective prompt or supported authentication flow.
- Stop and report if closing stdin changes an adapter's tested invocation/result behavior.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 5: Integrate safe in-process TTY scrolling with the live renderer

**Goal:**

- Activate the viewport on supported interactive terminals, safely own keyboard/resize events, and restore/render the terminal correctly across every lifecycle path.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `sed -n '800,1040p' aflow/status.py; sed -n '1,320p' aflow/terminal_viewport.py`
- Inspect: `sed -n '1060,1145p' tests/test_harnesses.py; sed -n '1,360p' tests/test_terminal_viewport.py; sed -n '1,190p' tests/test_cli.py`
- Inspect: `python -c "from rich.live import Live; import inspect; print(inspect.signature(Live)); print(inspect.getsource(Live.stop))"`
- Preserve: Checkpoint 1 cadence/coalescing, Checkpoint 2 document completeness/order, Checkpoint 3 viewport behavior, Checkpoint 4 stdin ownership, Git polling, manager-report ordering, and Rich-unavailable/no-TTY fallback.

**Scope:**

- May create or modify: `aflow/status.py`, `aflow/terminal_viewport.py`, `tests/test_terminal_viewport.py`, `tests/test_harnesses.py`, `tests/test_cli.py`, `docs/runtime-behavior.md`, `ARCHITECTURE.md`, `devlog/DEVLOG.md`
- Must not touch: workflow routing beyond the completed Checkpoint 4 stdin boundary, public CLI/config, run state/artifacts, manager/harness adapters, web code, external launchers/Screen config, README, or root `AGENTS.md`
- Constraints: capability-check `console.is_terminal`, `sys.stdin.isatty()`, POSIX, valid `fileno()`, and `termios.tcgetattr()`; use `tty.setcbreak()` while preserving `ISIG`; read with `select.select()` plus bounded timeout from one daemon input thread; compare `Console.size` on timeout and enqueue one resize wake only when changed; enqueue actions/wakes but never render in that thread; use `Live(screen=True, auto_refresh=False, vertical_overflow="crop")` only in interactive mode and normal `screen=False` fallback otherwise; reset the next periodic deadline after an interaction/resize render; make setup/close/restore idempotent and register an emergency `atexit` restore; do not install signal handlers.

**Steps:**

- [x] Add a POSIX terminal-input session that saves attributes, enters cbreak with `ISIG`, incrementally decodes bytes, detects real size changes, enqueues work, and restores attributes idempotently on close/atexit.
- [x] Integrate capability detection into start/resume. If any interactive setup step fails, restore any partial state and continue with normal borderless `Live`; never fail the workflow solely because scrolling is unavailable.
- [x] Extend the renderer loop to wake for queued interaction/resize work, apply all pending viewport actions, build once, refresh once, and advance the periodic deadline. Keep ordinary state/context updates coalescing-only and Git deadlines independent.
- [x] Make pause/stop ordering explicit: stop input, stop render thread, paint the latest/final viewport once, stop `Live`, restore terminal/alternate screen, then print one complete borderless document to normal scrollback only for sessions that used the alternate screen. Resume creates a fresh session/thread/`Live` instance.
- [x] Add fake-session/fake-`Live` tests proving supported versus fallback constructor policies, no Rich calls from the input thread, immediate-but-single action redraw, resize redraw/clamping, follow-tail/manual behavior during live updates, start/resume parity, and no calls after pause/stop.
- [x] Add Linux PTY tests for cbreak flags (`ICANON`/`ECHO` off, `ISIG` on), arrow/page byte decoding, alternate-screen enter/exit, cursor/attribute restoration after normal close and injected start failure, and idempotent cleanup. Avoid assertions on unrelated Rich escape-code ordering.
- [x] Extend the real CLI regression to assert one final borderless dashboard snapshot precedes one manager report and terminal cleanup codes do not swallow report text.
- [x] Update runtime/architecture docs with controls, follow-tail/manual rules, TTY fallback, final snapshot, resize behavior, stdin ownership, and cleanup guarantees; add a concise devlog entry. Leave README unchanged because it links to the detailed runtime guide.

**Dependencies:**

- Checkpoints 1-4.

**Verification:**

- Run: `uv run python -m compileall -q aflow tests`
- Run: `uv run pytest -q tests/test_terminal_viewport.py`
- Run: `uv run pytest -q tests/test_harnesses.py -k banner_renderer`
- Run: `uv run pytest -q tests/test_cli.py -k manager_report_remains_visible`
- Run: `uv run pytest -q tests/test_runtime.py -k run_process`
- Run: `uv run pytest -q tests/test_terminal_viewport.py tests/test_harnesses.py tests/test_status.py tests/test_cli.py tests/test_runtime.py`
- Run: `rg -n "k.*Up|j.*Down|PageUp|PageDown|follow.tail|alternate screen|DEVNULL|three seconds" docs/runtime-behavior.md ARCHITECTURE.md devlog/DEVLOG.md`
- Observe manually in a disposable TTY/Screen session: start a long synthetic dashboard, scroll line/page/top/bottom, append state while manually positioned and while following tail, resize narrower/wider and shorter/taller, interrupt/stop, then confirm the shell prompt echoes normally and one complete borderless final snapshot remains copyable.

**Done When:**

- Supported TTYs provide responsive in-process scrolling without render corruption; unsupported terminals degrade safely; all lifecycle exits restore the terminal and preserve final/report output.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if deterministic scrolling requires competing Rich callers, global signal handlers, terminal-name detection, or a third-party TUI dependency.
- Stop and report if a supported harness still needs interactive stdin after Checkpoint 4 verification.
- Stop and report if any tested exception/interruption path leaves terminal flags, cursor, alternate screen, or threads unrestored.
- Stop and report if exact final-dashboard/report ordering cannot be preserved within renderer/terminal lifecycle boundaries.
- Stop and report if unrelated dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

- Given several controller updates within three seconds, no synchronous repaint occurs; the next due repaint shows the newest state/context.
- Given a quiet harness, elapsed time advances within three seconds and Git polling remains on its independent 10-second cadence.
- Given a state containing every optional dashboard field, exported text contains every value/path exactly once in title, review history, turn history, workflow, then summary order, without border glyphs or side-by-side interleaving.
- Given active/inactive/excluded/skipped workflow steps in a no-color export, each state is named explicitly and each transition/condition remains visible.
- Given content shorter than the terminal, interactive mode shows all content plus its one-line controls/footer without scrolling away information.
- Given content taller than the terminal, `k`/Up and `j`/Down move one line; `b`/PageUp and `f`/Space/PageDown move one viewport minus one line; `g`/Home reaches the first line; `G`/End reaches and follows the last line.
- Given follow-tail mode and appended turn/history lines, the next repaint remains at the bottom. Given manual mode, appended lines do not move the selected top line; `G`/End resumes follow-tail.
- Given a terminal resize, the next size check wakes one render, recomputes body height/width, clamps offset, and shows a valid footer without stale/duplicated rows.
- Given unknown, malformed, partial, `q`, or unrelated UTF-8 input, the workflow continues and no navigation or terminal corruption occurs. Given Ctrl-C, `ISIG` remains enabled so normal interruption semantics are preserved.
- Given a real harness child that attempts to read stdin, it immediately receives EOF while its argv prompt, stdout/stderr capture, return code, and banner update remain correct.
- Given a supported POSIX TTY, interactive start enters cbreak/alternate screen and stop/pause/start failure restores the original attributes, cursor, and normal screen exactly once.
- Given non-TTY stdin/stderr, non-POSIX execution, Rich absence, invalid `fileno`, or `tcgetattr` failure, the workflow continues with the borderless non-interactive display and no terminal mutation.
- Given pause then resume, the old input/render threads are stopped, one fresh session and one fresh render thread start, viewport defaults to follow-tail, and no stale thread renders afterward.
- Given normal completion, failure, manager stop, or invalid manager decision, the alternate screen closes, one full borderless final snapshot remains in scrollback, and any manager report appears once after it.
- Given an SSH connection through GNU Screen, the same capability rules and controls apply without SSH/`TERM`/Screen-specific branches.

## Plan-to-Verification Matrix

| Requirement | Concrete verification |
| --- | --- |
| Rich auto-refresh removed | Fake-`Live` constructor tests and zero-match search for `refresh_per_second=4` |
| Three-second coalesced cadence | Deterministic renderer timing/wake tests with injected short intervals |
| One Rich-calling render thread | Thread-identity assertions on fake `Live.update`/`refresh`; lifecycle calls only after thread join |
| All old fields preserved | Maximal-state exported-text sentinel test against every live render helper/summary row |
| Borderless linear copy output | Render at narrow/wide widths; assert section order and absence of live-dashboard `Panel`/border glyphs |
| Visual state survives no color | Explicit workflow-state label assertions in `Console(force_terminal=False)` output |
| Viewport reaches every line | Pure model/render tests for line/page/top/bottom and exact first/last sentinels |
| Follow-tail/manual stability | Append-content tests in both modes; `G`/End re-enables following |
| Resize safety | Pure resize/clamp tests plus renderer wake-on-real-size-change test |
| Key decoding safety | Split escape-sequence, malformed, unknown, UTF-8, and `q` decoder tests |
| Exclusive stdin ownership | `_run_process` child-EOF test plus existing adapter argv/prompt tests |
| POSIX terminal restoration | PTY termios assertions before/during/after normal and injected-failure paths |
| Alternate-screen cleanup | PTY smoke assertion for enter/exit controls and normal shell output after stop |
| Non-TTY/Rich-unavailable fallback | Capability-matrix tests with no termios/session construction and `screen=False` |
| Start/resume/pause/stop safety | Fake-session/fake-`Live` thread/lifecycle tests and no-post-stop-call assertions |
| Final snapshot and manager report ordering | Real CLI banner regression with one snapshot sentinel followed by one report sentinel |
| Git/elapsed behavior preserved | Focused renderer tests and full harness/status regressions |
| Documentation matches behavior | Targeted `rg` over runtime guide, architecture, and devlog |
| No external/config/control changes | `git diff --name-only` and scoped diff review per checkpoint |

## Assumptions And Defaults

- “Less-like” means in-process line/page/top/bottom keyboard navigation over retained live content, not invoking the `less` executable and not reproducing every less command.
- The live dashboard opens in follow-tail mode because current status and the summary are at the bottom of the flattened document. Any manual upward/top/page navigation disables following; only `G`/End re-enables it.
- `q` is deliberately ignored because quitting the viewer must not quit or hide a still-running workflow. Ctrl-C retains its existing signal behavior.
- One footer row is reserved only in interactive viewport mode. The final static snapshot and non-interactive fallback do not include navigation help.
- Interactive key/resize repaint is intentionally immediate and resets the next three-second periodic deadline; it is not a second steady-state scheduler.
- Startup selection/recovery/dirty-worktree questions finish before `BannerRenderer.start()`, so cbreak mode does not affect existing prompts.
- Existing adapters deliver their effective prompt through argv/flags; Checkpoint 4 verifies this before child stdin is closed. An adapter that genuinely requires stdin is a blocker, not a silent exception.
- Alternate-screen mode is the safe default for a fixed-height scrollable live viewport; the final full document is printed once after leaving it so scrollback/copy remains available.
- POSIX termios/select support is implemented with the standard library. Other platforms and failed capability checks use the existing normal-screen behavior without scrolling.
- The current `config_banner_files_limit` behavior is preserved; “no information loss” means no additional omissions beyond limits already applied before this handoff.
- `build_workflow_show()` remains panel-based because the request targets the live dashboard, not the separate static workflow-inspection command.
- `README.md` remains unchanged because its documentation index links to the detailed runtime guide; `aflow/AGENTS.md` remains unchanged because its interactive-first/TTY guidance still applies.
- The original plan remains under `plans/in-progress/` as the progress ledger; execution tooling, not an implementer, owns its eventual move.

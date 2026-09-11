# DEVLOG

## 2026-09-11 — Simplify New Run and verify browser journeys (Checkpoint 3)

- Kept Plan, Workflow, and Team as labelled launch choices with resolved
  defaults; added concise worker-upgrade and reviewer summaries and moved
  verbose membership/step tables under keyboard-accessible Details. Pending
  configuration now says Loading configuration instead of implying a default.
- Extended disposable Chromium/WebKit journeys at 1440×900 and 390×844 in
  light/dark modes for exact run back navigation, history search/filter,
  Ready-plan launch/default and explicit team selection, settings edit/tab/save/
  reload, long mobile Projects context, document scrolling, focus, and
  overflow. Screenshots were inspected; physical mobile keyboard remains
  unverified.

## 2026-09-11 — Preserve action feedback and recover passive read failures

- HISTORY: CI evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-36c7b483-failed.log` showed a rejected control alert disappearing during a later passive snapshot refresh and a landscape hit test using coordinates measured before its DOM query.
- `RunDashboard` keeps project discovery, dashboard reads, selected-run reads, and mutation/action feedback in separate local state. Guarded successful current responses clear only their own read error; passive SSE/poll refreshes preserve rejected control feedback and its draft. Intentional actions and explicit selection clear stale action feedback and failed-start links.
- Refresh retries failed or unresolved discovery through the existing guarded discovery effect. Deferred recovery and repeated-failure regressions verify that only successful discovery clears its error and rejected action feedback remains visible.
- The controlled regression uses deferred subscription refresh delivery: an accepted max-turns 12 control is followed by rejected draft 13, a late snapshot, an independent refresh failure and recovery, and an explicit retry. It preserves the exact `expected_revision` 1/2 payloads and draft 13. The new browser helper waits for real actionability with `trial=True` and checks bounds plus exact target/descendant hit identity in one DOM observation.
- Reviewer verification passed: focused dashboard suite (94 tests), full web suite (341 tests across 22 files), production build, and the exact `phone-landscape` plus `phone-live-controls` cases in Chromium (2 passed) and WebKit (2 passed), with isolated browser-test homes and ports. `git diff --check` passed. Private source/output is retained in `/root/code/evidence/aflow-dogfood-20260909/passive-refresh-review-v02-20260911/`.
- HISTORY: The controlled late-refresh regression fails against the unchanged pre-handoff base at the missing rejection alert. The earlier discovery-recovery proof remains in `/root/code/evidence/aflow-dogfood-20260909/passive-refresh-review-20260911/`. The full macOS timing failure was not locally reproduced; physical mobile keyboard behavior remains unverified.

## 2026-09-11 — Truthful repair progress and historical authority compatibility

- Added read-only original-checkpoint progress to REST/MCP context and the run
  dashboard: repair overlays remain separate, missing evidence has no invented
  total, and current turns stay distinct from the last finalized result.
- New live manager contexts use validated original checkpoint authority while
  retaining overlay task identity. A durable boundary version preserves exact
  pre-change historical reconstruction; routing and turn selection are intact.
- Verified observer, context and runtime regressions, authenticated REST/MCP
  parity, 336 web tests, the production build, and desktop/390px Chromium and
  WebKit progress journeys. Repeat review passed 451 Python tests and 43
  subtests; two host-ignore fixture failures passed with process-local Git
  configuration isolation. Historical analysis preserved payloads and bytes.
- Physical mobile keyboard remains unverified. Publication, exact-SHA CI,
  live activation and issue acceptance remain coordinator-owned.

## 2026-09-11 — Align detached UI MCP discovery (Checkpoint 1)

- HISTORY: CI evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-2053c498-failed.log` showed the real detached web server returning seven approved plan/config authoring tools beyond the stale 14-name UI discovery expectation.
- Updated only the detached server's exact `tools/list` expectation to 21 names. The 14 core names, authenticated request, three resource templates, health, second-start, and owned-stop assertions remain unchanged; no production or core-registry contract changed.
- Verification passed: `uv run pytest -q tests/test_ui_cli.py -k test_daemon_start_health_and_stop` (`1 passed, 14 deselected in 4.62s`) and `uv run pytest -q tests/test_ui_cli.py` (`15 passed in 5.05s`).

## 2026-09-11 — Align CLI startup diagnostic acceptance (Checkpoint 1)

- HISTORY: CI evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-1ae095c-failed.log` showed the non-TTY startup-recovery test still expected the former `inconsistent checkpoint state` wording; production emitted the approved `Plan checkpoint state is inconsistent...` safe message.
- Updated only that stderr assertion to the approved `plan checkpoint state is inconsistent` phrase. The explicit interactive-confirmation, exit-1, no-input, and neighboring parser/recovery wording assertions remain unchanged.
- Verification passed: `uv run pytest -q tests/test_cli.py -k test_cli_requires_tty_for_startup_recovery` (`1 passed, 156 deselected in 0.56s`); `uv run pytest -q tests/test_cli.py` (`157 passed, 129 subtests passed in 5.92s`).

## 2026-09-11 — Complete web MCP authoring (Checkpoint 3)

- Added the authenticated HTTP journey covering tool discovery, global settings
  edits, draft creation, revisioned update, promotion, both plan-list views,
  and launch through the existing fixture-controlled run service.
- Documented the seven web authoring tools, approval and revision behavior,
  stale-write recovery by rereading, global safe-boundary/resume semantics,
  and the separate browser-only project registration and skill installation
  actions. No arbitrary file or credential-bearing MCP surface was added.
- Verification used disposable config/HOME/projects/ports; the current web
  bundle was rebuilt before the server regression suite. Publication, CI, and
  live activation remain coordinator-owned.

## 2026-09-10 — Isolate real CLI test run artifacts (Checkpoint 1)

- `test_cli_workflow_override` now inspects its durable `run.json` to verify
  the run stays beneath the temporary root, records workflow `other`, and
  references the test-owned plan; its existing cwd/HOME restoration remains
  unchanged.
- Verification passed: targeted and full CLI tests (157 tests, 129 subtests),
  `git diff --check`, and a disposable outer-Git-checkout subprocess proof
  (1 passed, with no caller `.aflow` created).

## 2026-09-10 — Await Settings navigation and header actions (Checkpoint 1)

- HISTORY: CI failure evidence in `/root/code/evidence/aflow-dogfood-20260909/ci-fdc9ff2-failed.log` showed the mobile Skills journey waiting for a desktop `tab` after the responsive combobox count was observed as zero, and the macOS Changelog journey observed zero Save buttons immediately after the Advanced TOML editor appeared.
- HISTORY: Source inspection established both as render-order boundaries: `GlobalSettings` conditionally renders the named combobox or tabs, while `useHeaderSlots` registers the hosted header actions in an effect that can settle after the editor itself is visible. No local reproduction is claimed.
- The browser helper now waits for the visible union of the requested combobox and tab before selecting the actual rendered control. The Advanced TOML journey waits for the visible Save action in the hosted header before retaining its exact count assertion; paging, draft bytes, geometry, and read-only assertions are unchanged.
- Verification passed with `cd apps/aflow_app/web && npm ci && npm run build`, `cd apps/aflow_app/server && uv sync --frozen --group dev`, and the complete `tests/test_settings_browser.py` file under both `AFLOW_TEST_BROWSER=chromium` (5 passed) and `AFLOW_TEST_BROWSER=webkit` (5 passed). Only the Changelog and dirty mobile Skills journeys use the selected engine; the toolbar, draft-template, and skill-save/install tests in this same file remain hard-coded Chromium. `git diff --check` also passed.
## 2026-09-10 — Make Ready state and startup corrections truthful (Checkpoint 3)

- Ready remains a lifecycle state: the hosted plan editor now explains that startup
  checks run when the user starts the plan, without claiming an earlier validation.
- Start failures keep the existing plan/workflow draft and show the bounded structured
  server message; a reserved run remains linkable, while a new attempt clears any stale
  failure link when no new run identity is returned.
- Bundled `aflow-plan` guidance and the draft template recommend the exact unnumbered
  `## Git Tracking` heading and its two controller-owned fields, while documenting the
  supported single numbered form for existing plans.

## 2026-09-10 — Stabilize hosted browser ordering and Dashboard CI Python (Checkpoint 1)

- HISTORY: A controlled browser probe held the Restore fetch after the server
  response and before the client acknowledgement. `RunDashboard.tsx` left
  Delete enabled, allowed its confirmation to open, then unconditionally cleared
  it when Restore settled; the output was Delete enabled and Confirm delete
  visible/disabled before release, then hidden after release. This established a
  reachable busy/confirmation ownership race.
- History actions now remain unavailable during a history mutation, and Restore
  cannot supersede an open confirmation. The browser journey retains the exact
  archive disappearance, restore identity, explicit delete, deleted-record
  reload, navigation, geometry, and revision/idempotency contracts while waiting
  on observable settled state instead of fixed delays in the touched flow.
- Live-control admission now uses retrying enabled/value predicates. Dashboard
  CI retains root 3.11/3.12, runs the server on 3.12/3.13 with `UV_PYTHON`
  pinned from the matrix, and fails before server tests if the executed minor
  differs from the declared job.
- Verification corrections were retained: the first hosted probe required a
  fresh web build because `dist` was absent; the first browser pair exposed the
  positional `wait_for_function` argument and then a stale bundle, both fixed
  before rerunning. The component regression used the repository's native
  `.disabled` assertion style after its unsupported matcher failed, and the
  labeled YAML check corrected an initial shell-quoting-only validator failure;
  the final shell-safe pass also used the workflow's actual `Build web app`
  step name before passing.
- HISTORY: The later Ubuntu WebKit evidence from CI `34523507986` (changelog
  source `main884b899`) failed when workflow selection was followed immediately
  by the Advanced options click and the extra-instructions control was absent.
  The route matrix now waits for the exact selected workflow and the disclosure
  `aria-expanded`/textarea visibility predicates. Chromium and WebKit each
  passed all seven route-matrix cases, so the controlled evidence did not
  justify a production disclosure change.
- HISTORY: The original restore-ordering source and output remain in
  `/root/code/evidence/aflow-dogfood-20260909/STATUS.md` and
  `ci-be19c4f-failed.log`. The earlier 13-test WebKit invocation was mixed
  engine coverage because both navigation tests hard-coded Chromium; both now
  use the existing `AFLOW_TEST_BROWSER` selector and its Chromium-only
  `--no-sandbox` behavior.
- Final evidence: the rebuilt web assets passed; the explicit Chromium and
  WebKit commands each ran all 13 selected tests with no skips, including both
  navigation tests and the held restore/delete/reload path. Focused RunDashboard
  85 tests, full web 316 tests, YAML matrix/interpreter checks, and
  `git diff --check` also pass. `actionlint` was unavailable in the environment.

## 2026-09-10 — Complete receipt-backed plan delivery (Checkpoint 2)

- Routed approved publication, CP1 lifecycle finalization, and any resulting
  bookkeeping publication through one terminal delivery boundary; a missing
  bookkeeping commit does not trigger a redundant push.
- Recorded the completion phase on delivery failure so resume can retry the
  exact receipt-backed terminal work without replaying checkpoints or a removed
  worktree. Repeated resumes follow the complete predecessor lineage to the one
  receipt owner and retain every required run under low run-history limits. The
  local bare-remote regression covers the original final-push failure plus
  three rejected terminal retries, clean Done storage, remote receipt identity,
  and preserved failure status.

## 2026-09-10 — Record completed-plan lifecycle ownership (Checkpoint 1)

- Added a receipt-backed completed-plan lifecycle helper that verifies exact
  source and Done bytes, preserves ignored Done storage, and records only the
  owned source/destination Git paths in a narrow `chore/plans` commit.
- Refuses unrelated staged changes and ambiguous ownership while preserving
  unrelated unstaged data; recorded move and commit phases resume idempotently.
- Exact pathspecs are now literal, and receipt-backed staging intent verifies
  index identities after interruptions at either lifecycle staging boundary.
- Destination identity follows Git's clean conversion (including autocrlf)
  while raw on-disk plan bytes remain unchanged through move and retry.
- Focused publication/lifecycle coverage passes, including unusual filenames,
  pre-existing Done copies, conflicts, untracked plans, and interruption cases.

## 2026-09-10 — Responsive release CI readiness repair, Checkpoint 1

- Reproduced the starter ordering with a deferred pure-form response: the empty
  form remains in its loading state until the response settles, then the React
  default effect populates `implement` and `trunk`. The production component
  remains correct; its test now waits for the settled defaults before preserving
  the exact starter action and draft callbacks.
- Controlled live-control ordering with list/detail revision 0, an acknowledged
  revision 1 response, and a delayed old detail read settling afterward. The
  exact first payload remains `expected_revision: 0`, `max_turns: 12`,
  `team: fast__team`, and worker selector `reasonix.new`; the second remains
  only `expected_revision: 1`, `max_turns: 13`. `RunDashboard` now ignores a
  lower revision only for the same run identity; equal revisions still carry
  progress/status updates, and different run IDs are never compared.
- The responsive fixture now persists the acknowledged override and its
  `control_changed` event, so list/detail/mutation and background event sources
  no longer disagree about revision 0 after the first write.
- The first exact full web command recorded 313/315. Its isolated green runs
  were diagnostic only and did not establish unrelatedness or readiness. The
  two demonstrated boundaries were readiness races: the logout case asserted
  the hosted `Runs` heading before the workspace finished mounting, and the
  colliding-role case saved before the existing control-admission wait observed
  an enabled selector. The follow-up adds only those observable waits; exact
  assertions and raw request keys remain unchanged.
- Verification after the correction: `npm --prefix apps/aflow_app/web test --
  --run src/App.test.tsx src/components/GuidedConfigForm.test.tsx
  src/components/RunDashboard.test.tsx` passed 163 tests; the required
  `npm --prefix apps/aflow_app/web test -- --run` passed 315 tests across 18
  files; and `npm --prefix apps/aflow_app/web run build` plus `git diff --check`
  passed. The already-valid Chromium and WebKit commands
  (`uv run --project apps/aflow_app/server pytest -q
  apps/aflow_app/server/tests/test_responsive_browser.py` and its
  `AFLOW_TEST_BROWSER=webkit` variant) each passed 11 tests and were reused
  because this follow-up changes only unit-test readiness fixtures. Durable
  review evidence remains in `plans/reviews/latest_review.md`,
  `plans/reviews/responsive-readiness-cp01-ordering.txt`, and
  `/root/code/agent_flow/.aflow/runs/20260910t185404z-3b1d522d/turns/turn-001/transport.stdout`
  with its adjacent `result.json`. No sleeps, retries, assertion weakening, UI
  redesign, live configuration, publication, CI, or deployment was changed.

## 2026-09-10 — Show release changes in Settings

- Added a read-only Settings → Changelog tab backed by the generated release
  artifact, with date groups, escaped titles, and 20-entry paging.
- Preserved the shared settings/skills draft owner across Changelog navigation
  and covered the responsive desktop/mobile journeys in both browser engines.

## 2026-09-10 — Keep Changelog actions read-only

- Hide the shared Save, reload, and skill-install actions while Changelog is
  displayed, while retaining the truthful unsaved indicator and Advanced TOML
  navigation.
- Preserve General and Skills drafts without writes, then restore their shared
  Save owner when returning to an editable settings section.

## 2026-09-10 — Generate compact release changelog from DEVLOG

- Added deterministic build-time JSON and Markdown changelog generation from
  dated DEVLOG headings, with the source and generator included in sdist builds.
- Added cache fingerprint coverage and focused generator/asset tests without
  changing the existing UI deployment pipeline.

## 2026-09-10 — Approve issue38 checkpoint 1

- Review reran the retained four-case comparison: all passed; both setups preserved
  confirmation before/after detail settlement. No setup-specific failure reproduced.
- Retained complete command output, verified baseline setup provenance, and passed
  diff check. Reused documented post-comparison focused83/full296/build results.
- Zero material findings; checkpoint approved locally. Publication, exact-SHA CI
  and live verification remain coordinator-owned.

## 2026-09-10 — Retain issue38 baseline/pending readiness comparison

- Added four controlled comparison cases to `RunDashboard.test.tsx`: accepted
  baseline versus pending location/clipboard setup, each exercised before and after
  direct-detail settlement. Every case observes list settlement, selected-detail
  settlement, visible/enabled Resume, and Confirm visibility after clicking.
- The comparison source/provenance is recorded in
  `plans/notes/issue-38-stable-dashboard-clipboard-cp01-v03-comparison-source.md`,
  with exact verbose output in the paired `-output.txt` artifact. Both setups behaved
  identically; the setup-specific failure did not reproduce, so no clipboard causal
  link or new production correction is claimed.
- Location and clipboard state are restored per case. Existing URL/privacy, source
  and continuation identity, resume-key, API-call, and confirmation assertions remain
  intact. The pending clipboard implementation and responsive work are unchanged.
- Verification after the added comparison coverage passed once: focused RunDashboard
  tests (83), full web tests (296 across 16 files), web production build, and
  `git diff --check`.

## 2026-09-10 — Resolve issue38 confirmed-selection readiness

- The uncertain-resume test now controls list and direct-detail promises, waits for
  the settled selected-run output (`Review · 2`), verifies that Resume is enabled,
  and enters confirmation only after confirmed selection readiness.
- Existing source/continuation identities, confirmation requirements, uncertain-error
  assertion, API call count, and reused idempotency key remain intact. The correction
  is test-only; the production diff remains limited to clipboard request generations.
- `HISTORY:` The earlier review's 291-pass/one-failure result was the missing Confirm
  resume transition. The corrected controlled journey now passes without retries,
  sleeps, timeout changes, or weakened assertions.
- Verification: focused RunDashboard tests passed (79); full web tests passed (292
  across 16 files); web production build passed; `git diff --check` passed. Responsive
  work, exact sanitized URLs, error/privacy assertions, and cleanup ownership remain
  preserved.

## 2026-09-10 — Isolate issue38 full-suite resume verification gap

- HISTORY: The current-worktree review reported 291 full-web tests passing and
  one failure in `reuses a resume key after an uncertain failure` at the
  missing `Confirm resume` assertion; the focused RunDashboard file passed 79.
- A temporary controlled experiment deferred the list and direct-detail
  promises, waited for the visible Resume action, clicked it, and verified the
  visible Confirm action survived direct-detail settlement. It passed 1 test
  with 79 skipped, so the reported failure was not reproduced by the relevant
  readiness ordering.
- The failing resume journey precedes the modified clipboard test in the file;
  the production diff only guards clipboard completion identity, and the test
  cleanup restores location and clipboard descriptors. No causal clipboard
  fixture defect was demonstrated, so no restart/control repair was made.
  Approval remains withheld pending the required full-suite verification.

## 2026-09-10 — Bind dashboard clipboard feedback to the selected run (Issue 38, Checkpoint 1)

- The focused clipboard journey now releases list, direct-detail, and
  clipboard promises in order, waits for the observed selected-run callback
  and settled detail output, and restores location and clipboard ownership in
  test cleanup. It still asserts the exact success text, one exact sanitized
  project/run URL, call count, rejection text, and secret/URL exclusions.
- A controlled pending-copy selection change reproduced stale success from the
  old run. `RunDashboard` now invalidates copy request generations on selection
  changes and ignores late success or failure from an older request. No URL
  contract, responsive work, or control/restart behavior changed.
- Verification: focused RunDashboard tests passed (79); full web tests passed
  (292 across 16 files); web build passed; `git diff --check` passed. The
  pre-fix stale-feedback reproduction is retained by the regression test.

## 2026-09-10 — Wait for admitted dashboard controls before interaction (Checkpoint 2)

- The rejected-control and capability-admitted selector tests now wait for the
  canonical control baseline and enabled admission, confirm the selected draft,
  and wait for the Save action to be enabled before interacting. The rejection
  test still checks the exact run, revision, error, retained draft, and running
  status.
- Shared launch readiness now requires the clean preflight result and an
  enabled actionable button. The demonstrated successor-restart test also
  verifies the latest preflight request matches its selected plan, workflow,
  and source before preserving the owner-stop, inactive-proof, and lineage
  assertions. No production UI behavior changed.
- Verification: focused RunDashboard tests passed (78); full web tests passed
  (291); the web build passed; the full Python suite passed (1,748 tests and
  223 subtests); `git diff --check` passed.

## 2026-09-10 — Keep synthetic Git maintenance inside fixture ownership (Checkpoint 1)

- CI34493456633 at `24ab54e` failed
  `WorkflowArtifactTests.test_terminal_backup_recovery_uses_original_team_for_merge_teardown`
  during temporary-repository cleanup because `.git/objects` remained nonempty.
  The recorded process trace showed `git maintenance run --auto --no-quiet`
  children; the targeted test itself passed, so this was isolated as a fixture
  lifetime race rather than a production failure.
- Shared synthetic repository builders now set `maintenance.auto=false` and
  `gc.auto=0` with repository-local Git configuration immediately after init.
  Existing user identity, normal Git operations, and workflow/recovery/merge
  assertions remain unchanged; no production architecture change is claimed.
- Verification: clean-Git-config targeted test passed; the process trace showed
  zero automatic-maintenance children and 20 local fixture-config commands.
  The full runtime suite passed with 289 tests and 43 subtests, followed by a
  clean diff check.

## 2026-09-10 — Supply engine-owned merge handoff context (Checkpoint 1)

- Merge teardown now always gives the `aflow-merge` worker exact branch, root,
  worktree, and original/active/new plan paths in a labelled JSON block,
  including when `merge_prompt` is empty. Optional custom instructions remain
  appended in their existing order and cannot replace lifecycle identity.
- Added disposable captured-invocation coverage for ordinary completion,
  terminal integration-only resume, custom prompt ordering, and escaped unusual
  path values. No real provider, live service, or global configuration is used.
- Verification: runtime/config `422 passed` plus `50 subtests passed`, docs
  `23 passed`, Ruff for `aflow`, and `git diff --check` passed.

## 2026-09-10 — Keep MCP on the UI server after standalone removal

- The standalone `aflow daemon` command and `aflowd` executable are removed;
  the existing authenticated MCP registry remains mounted by the UI server at
  `/mcp` and `/mcp/`.
- All 14 tools, three resource templates, bearer-header authentication,
  idempotency/revision contracts, live configuration behavior, UI background
  controls, and independently owned workflow workers remain in the existing
  shared services.
- The retained `aflowd.service` deployment still runs `aflow-app-server`; no
  standalone stdio transport or compatibility alias is provided.

## 2026-09-10 — Exempt untracked lifecycle backups from confirmation (Checkpoint 1)

- Phase-B branch-only preflight was classifying AFlow's newly created
  `plans/backups/` copy as ordinary plan dirtiness, so clean Git environments
  stopped before the first turn. Confirmation classification now ignores only
  untracked records wholly under that exact boundary while retaining raw status
  items, dirty facts, conflicts and tracked/rename protection.
- Added synthetic preflight coverage for backup admission, source and plan
  dirtiness, sibling-path boundaries, tracked/renamed backups and conflicts;
  strengthened branch-only resume coverage to verify the original source bytes
  and backup survive the failed first turn and successful resume.
- Verification passed: 97 focused tests plus 21 subtests, 1,779 full-suite
  tests plus 219 subtests, Ruff and the diff check. Exact-SHA CI and live
  deployment remain post-review coordinator gates; local verification makes no
  deployment claim.

## 2026-09-10 — Prefer parallel development with owned integration

- Persist coordinator guidance to run independent plans in isolated worktrees,
  preserve both sides of overlapping changes, and serialize main integration.
- Require prompt per-plan publication and exact CI/live evidence; isolate shared
  runtime/test resources and keep checkpoint workers within their assigned scope.
- This is operating guidance, not a new scheduler or workflow implementation.

## 2026-09-10 — Record compact UI acceptance requirements

- Record the owner-approved two-row desktop header, mobile hamburger navigation,
  document scrolling and full mobile editing in UI_GUIDELINES.md. Root/web agent
  guidance now references this contract; architecture distinguishes the current
  bounded-pane limitation from the planned replacement. No UI implementation is
  claimed by this documentation update.

## 2026-09-10 — Expose dirty-worktree preflight (CP8 implementation)

- Added authenticated REST and read-only MCP preflight over the shared CP7
  status result, with bounded pages and repository-relative status items.
- Fresh daemon launches now accept the explicit dirty-worktree choice across
  transport, replay, and preparation boundaries while retaining the existing
  structured startup question and conflict/in-progress-operation refusals.

## 2026-09-10 — Repair dirty-worktree lifecycle preflight (CP7 review)

- Lifecycle startup now defers strict Git status inspection only for the
  existing non-Git/unborn bootstrap path, while inspection failures in real
  checkouts remain blocking.
- In-progress Git operation markers are scoped to the selected checkout, so a
  clean linked worktree is not blocked by a merge in the primary checkout;
  conflicts and operations in the selected checkout remain blocking.

## 2026-09-10 — Approve live turn configuration (Checkpoint 3)

- Each source-backed turn reloads configuration before controls and limits; persistent partial choices and retry context survive refresh and resume.
- Limit-driven termination preserves incomplete plan progress; independent incomplete END still fails. Owner stop precedes configuration parsing.
- CP3 approved after 341 runtime/state/runlog tests and 43 subtests, plus 2 focused CLI tests and 8 subtests. Session and supervision refresh remains CP4.

## 2026-09-10 — Approve live launch and resume configuration (Checkpoint 2)

- CLI and daemon startup/resume use the current source; snapshots are optional diagnostics. Explicit choices retain provenance, while execution identity and resume progress remain authoritative.
- Resolved the CP2 metadata writer regression and explicit start-step correction equal to the original start. Verified 293 tests and 131 subtests across CP2/API and focused metadata/replay checks.
- CP2 approved; per-turn reload remains subsequent checkpoint work.

## 2026-09-10 — Close the publication gap and exercise CI portability

- Add explicitly repository-authorized publication before successful workflow
  completion, including in-place and resumed boundaries. Preserve active trees
  and remote history; record the delivered SHA or failed delivery in the run.
- Build web assets and install Chromium before server browser tests; preserve
  the correct platform browser cache when using disposable HOME. Drain PTY output before closing
  its slave, handle protected macOS executables in the no-Node packaging smoke,
  use the macOS boot-session identity, and confirm listener conflicts while
  preserving address reuse for clean restarts. Filesystem identity tests now exercise aliases on case-insensitive disks.
- Probe live loopback listeners before wildcard binding on BSD, and give the
  workspace-shell plan-list mock a valid empty response after project creation.

## 2026-09-10 — Release approved work through main

- Publish the completed Skills/settings, manager-context, and readable CLI work
  to main. Remove the invalid empty CI matrix exclusion so CI can run and the
  existing exact-SHA deployment gate can advance the live UI.
- The active live-configuration plan remains in its execution worktree until
  complete. Local plan approval alone is not evidence of publication or deployment.

## 2026-09-10 — Store manager history outside the live context (Checkpoint 4)

- Added an additive content-addressed JSON evidence kind for complete manager
  history, including turn/decision coverage, implementation attempts,
  active-scope rejections, repartition records, and detailed latest-turn
  semantic diagnostics without embedding raw stdout/stderr bodies.
- Unrecognized structured-stream fallbacks remain reference-only with truthful
  extraction metadata, and legacy decisions without turn associations sort
  deterministically without inventing a turn.
- Schema-v3 live contexts now retain only current decision facts, numeric
  `history_summary`, immutable references, and byte-safe latest-turn/error
  summaries; historical compatibility containers are explicitly empty. Read-only
  reconstruction reuses exact recorded references and reports missing or
  tampered artifacts without writing.
- Updated the bundled manager contract and runtime architecture documentation
  to distinguish the 16 KiB normal target from the 40 KiB hard prelaunch guard.

## 2026-09-10 — Integrate readable CLI status output

- Wired the readable status renderer into the real runner lifecycle and
  removed only the duplicate pre-banner run-ID line; existing resume and error
  reporting paths remain unchanged.
- Added synthetic runner coverage for normal and two-checkpoint progress,
  stderr-only harness failure, pre-turn failure, waiting, owner stop, turn
  limits, observer ordering, and durable run metadata.
- Documented the block layout, TTY-only heading emphasis, plain fallback,
  artifact paths, machine interfaces, no-refresh behavior, and fixture-based
  before/after examples.

## 2026-09-09 — Document prompt template variables (Checkpoint 5)

- Added read-only template-variable metadata to the guided configuration
  projection, sourced from both ordinary and merge prompt renderers. The web
  prompt editors now show scope, fallback behavior, and illustrative examples;
  role/team overrides explicitly remain literal text.
- Verification: guided-config 65 passed; renderer prompt regression 7 passed;
  focused web settings tests 75 passed plus 8 save-draft tests; web build and
  `git diff --check` clean.
  Disposable Chromium checks confirmed the reference below the editor and
  within both 1280px desktop and 390px mobile viewports with no project selected.

## 2026-09-09 — Verify complete manager boundaries (Checkpoint 4)

- Reviewer approved CP4 through `cp4 v01`: 153 manager/context/runlog tests,
  43 runtime manager tests and 7 subtests passed with disposable configuration.
  Read-only incident decision-20 reconstruction measured 37,979 pretty bytes
  versus 32,324 current wire bytes; decision artifact hashes/mtimes stayed
  unchanged. These are reconstruction measurements, not original wire bytes.
- Added sanitized later-boundary fixtures with multibyte summaries, long
  artifact references, active review rejection, Lite-to-Full escalation, and
  executor-provided repartition history. Read-only decision-20 reconstruction
  now asserts unchanged artifacts and exact prompt bytes before and after.
- Added a real deterministic fake-harness worktree integration proving that
  manager prompts stay within the hard limit while primary repository/run
  evidence and turn references remain resolvable.

## 2026-09-09 — Approve budget-failure diagnostics (Checkpoint 3)

- Retain rejected manager input with bounded atomic diagnostics and accurate prelaunch budget reports, preserving checkpoint/turn references and terminal incident evidence.
- Approved CP3 through `cp3 v02`; 203 tests and 7 subtests passed with disposable configuration, plus the diff check. CP4 integration coverage remains pending.

## 2026-09-09 — Bound optional manager history (Checkpoint 2)

- Approved deterministic v3 history projection, bounded omission descriptors,
  and exact-byte reduction while retaining current boundary authority.
  Repartition history participates in final prompt measurement.
- Verification: 111 manager/context tests, 39 runtime manager tests and 7
  subtests passed with disposable configuration roots; no material findings.
  Budget-failure diagnostics remain assigned to Checkpoint 3.

## 2026-09-09 — Keep Skills edits safe during install refresh

- Settings keeps the Skills editor disabled until the shared install result,
  refreshed registry, and selected skill content have all settled. Refresh
  failures remain visible and controls recover without discarding a draft.
- Added deferred unit and Chromium coverage for the post-install list boundary,
  selected-content refresh, failed reload recovery, and real linked saves.

## 2026-09-09 — Replace stale instructions during CLI resume

- Explicit CLI resume now replaces saved extra instructions when text follows
  `--`, clears them with a bare `--`, and inherits them when omitted. Keep
  automatic candidate matching strict and preserve predecessor evidence.
- Doublangu's CP7 reviewer repeated CP6 review after checkpoint-specific
  restart instructions were replayed as run-wide text. This change permits
  correction without a fresh worktree or manual run-state edits. Explicit
  review targeting and scoped recovery notes are planned separately.
- Verification: 153 CLI tests and 127 subtests passed, including replacement,
  clearing, preserved predecessor text and downstream resume admission.

## 2026-09-09 — Validate resumed launches against their frozen configuration

- Prepared-launch validation now loads the verified per-run configuration
  snapshot, preserving its origin for fingerprint computation, instead of
  comparing a resumed run against the daemon's current global configuration.
- Regression covers a global model edit after reservation and continued
  rejection when the saved snapshot is modified.
- Verification: 74 snapshot/control-plane/daemon CLI tests passed; Ruff and
  `git diff --check` passed.

## 2026-09-09 — Bind copied scope evidence on repeated resume

- Rebind inherited schema-v2 envelope paths in memory to the immediate source
  run's copied evidence before resume validation. Envelopes remain unchanged;
  digest, size, containment, UTF-8 and checkpoint-span validation still apply.
- Doublangu's second resume otherwise looked for its first run's paths and
  rejected valid copied evidence. Extend the continuation regression to remove
  the ancestor evidence, bind the local copy, and reject a corrupted copy.
- Verification: 191 relocation/resume/CLI tests and 128 subtests passed;
  Ruff and `git diff --check` passed.

## 2026-09-09 — Resume recorded controller failures

- Allow an explicit daemon resume when the controller has durably finished,
  even if the detached launch receipt still says `launch_started`. The former
  guard incorrectly classified Doublangu's recorded checkpoint-review failure
  as a killed process; reconciliation intentionally retained its terminal state.
- Keep inactive-worker/unit checks and resume bootstrap validation. A launched
  process without controller terminal evidence still requires reconciliation.
- Extend resume tests across failed/interrupted controller states and verify
  unreconciled killed launches cannot allocate a successor.
- Verification: 28 resume/repository/reconciliation/service tests passed;
  Ruff and `git diff --check` passed.

## 2026-09-08 — Avoid formatting-induced manager context failures

- Serialize schema-v3 manager prompts as compact UTF-8 JSON. Preserve every
  field and the 32768-byte hard limit; legacy serialization remains unchanged.
- Reconstructed Doublangu decision 20 from its saved boundary: pretty JSON was
  33611 bytes, compact JSON 28754 bytes, with identical decoded content. The
  original failure reported 33646 bytes; the read-only reconstruction is not
  claimed byte-identical to the original prompt.
- Added ASCII/Unicode regressions where formatting alone exceeds the cap and
  verified existing oversized-input rejection. Manager/context tests: 98 passed.

## 2026-09-09 — Skills settings, workflow supervision, and draft template

- Manager instructions now come from live skill Markdown: the three prompt
  builders contribute only structured runtime data while `aflow-manager`
  (ordinary, note-correction) and `aflow-repartition-checkpoint` (proposal,
  validation, bounded correction) skills supply the system text, read once per
  invocation with no caching. Missing/invalid skills fail before any provider
  starts. Verified with builder parity tests and a fake run proving a save
  between two decisions affects only the later invocation/artifact.
- Replaced global `[manager].enabled` with per-workflow `manager_enabled`
  (optional declared flag, `False` shipped default, workflow override →
  concrete base → defaults → `false` presence-based precedence). Only real
  TOML booleans are accepted; step-level flags and the old global are
  rejected with a targeted message. `resolve_manager_role` takes the selected
  workflow explicitly; frozen run snapshots keep their value while live edits
  affect new runs. Covered by parser/precedence/validation, fake-run routing,
  and snapshot-freeze tests.
- Authenticated Skills API (`GET /api/skills`, detail, `PUT`, batched
  `POST /api/skills/validate` with per-entry verdicts, `POST
  /api/skills/install`) shares the canonical store and the CLI installer
  service (absolute directory symlinks over the exact eleven-harness map,
  refresh-preserving-edits, structured partial results). Lone-surrogate
  validate content returns a bounded per-entry `skill_invalid` verdict (HTTP
  200) instead of an opaque error; covered by an ASCII-escaped `\ud800`
  regression test.
- Settings gained a Skills editor (bundled registry, `SKILL.md` textarea,
  parent-owned drafts/revisions, save coordinator ordering workflow config →
  skills → credentials with per-domain acknowledgement) plus Workflows
  supervision controls (Defaults Enabled/Disabled; per-workflow
  Inherit/Enabled/Disabled with effective value and source). New web drafts
  start from a packaged parser-valid checkpoint template; explicit content
  stays byte-for-byte and no new plan validator exists.
- Verified with the handoff suites (core, server incl. Chromium skills/draft
  smokes, full web suite, production build, ruff, `git diff --check`) except
  pre-existing environmental failures proven identical on the untouched base.
  Automated checks prove filesystem propagation through links, not native
  discovery inside the external harness CLIs (not installed here).

## 2026-09-08 — Refresh unedited skill bundles without losing edits

- Added an explicit `SkillStore.refresh_skills` path alongside revisioned
  saves: missing canonical trees initialize from the package, trees matching
  their recorded baseline refresh to incoming package files (including added
  and removed resources with permission intent), and the baseline advances
  only after a successful refresh.
- Edited skill trees are preserved whole — saved `SKILL.md` plus every
  supporting file — and reported `preserved_edited`; exact reversion to the
  baseline makes a skill unedited again. Trees without metadata are adopted
  only on an exact package match and otherwise protected with an unknown
  baseline.
- Refreshes stage and validate the incoming tree first, mutate under a
  store-owned transaction marker with rollback material for touched files and
  metadata, and restore the prior tree and baseline on ordinary I/O errors.
  Interrupted transactions make reads and saves of that skill fail with an
  incomplete-refresh error until the next explicit refresh verifies the
  rollback; uncertain markers are never silently retired.
- Documented the preserve-whole-edited-skill policy in `docs/installation.md`.
  Verified 41 skill-store tests, 18 refresh tests, and ruff on the store;
  the full store/install suites pass with the injected v1→v2 loader and the
  real bundled package.

## 2026-09-08 — Preserve history browsing and acknowledgement retries

- Refresh every loaded run-history page, reconcile removals, and retain the
  refreshed range cursor; discard superseded list responses.
- Clear history mutation intents after definitive rejection so a newly
  confirmed active-workflow acknowledgement is sent. Uncertain failures retain
  the original key and payload for exact replay.
- Verified 222 web tests, production build, and Chromium with 130 history rows
  retained across refresh at desktop/mobile sizes in both themes; diff check passed.

## 2026-09-08 — Fix Agents & Roles profile overflow

- Narrowed the settings overflow selector to top-level panels and prevented
  profile cards from shrinking inside the shared Agents & Roles scroller.
- Chromium reproduced 236px content clipped into 48px cards before the fix.
  Expanded coverage now checks full card height and access to the last profile
  at desktop/mobile sizes in both themes.
- Verified 220 web tests, both Chromium layout tests, web build and diff check.

## 2026-09-08 — Run history controls and sidebar navigation

- Added revisioned Archive/Restore/Delete record metadata without removing
  workflow evidence or changing launch/recovery idempotency. Active work requires
  explicit acknowledgement; deleted external reads return 410.
- Projected current activity and status reasons from controller/worker evidence
  and versioned preparation-owner identity. Cold and cached API reads stay
  read-only; uncertain outcomes have their own overview group.
- Added separate Runs/settings navigation and editor scrolling, settings-wide
  Advanced TOML, parent-owned editor selections, and discovery deduplication.
  Team/max turns are visible directly; clearing effort preserves the unset action.
- Verified: 78 core tests, 108 server/API tests, 220 web tests, two Chromium
  regressions (desktop/mobile, light/dark), production web build, and diff check.
  Additional project-registry coverage passed with the server suite. Existing
  user workflows and records were not changed; no deployment or tool reinstall.

## 2026-09-08 — Resolve manager evidence outside the execution worktree

- Schema-v3 manager context now declares absolute primary-repository and run
  artifact roots. Prompt instructions resolve repository-relative evidence and
  run-relative reviewer output against those roots, not the execution worktree.
- Verified the stopped Doublangu run's checkpoint hash and reviewer approval
  through the corrected roots without changing historical artifacts. Both Lite
  and Full regressions read exact evidence from a separate working directory.
- Verification: 96 manager/context tests plus 37 manager runtime tests and
  7 subtests passed. No review result is fabricated or inlined as a workaround.

## 2026-09-08 — Preserve frozen identity during worker Resume

- Fixed controller Resume rejecting a copied snapshot because it compared the
  predecessor's snapshot path against the original global configuration path.
  Validated snapshots preserve the predecessor's recorded identity path, while
  configuration fingerprints and continuation identity remain checked.
- Added controller-entry regressions for both CLI/original and worker/snapshot
  identity paths, with invalid live configuration and rejected fingerprint drift.
  The running Doublangu workflow and its historical receipts remain untouched.
- Verification: snapshot, runtime, control-plane Resume and daemon suites passed
  (309 tests, 43 subtests). Reinstating the old path selection in an isolated
  test plugin reproduced the worker/snapshot regression; `git diff --check` passed.

## 2026-09-08 — Global Settings, prompt editing, recovery, and All runs

- Added global domain tabs with one Save all action and changed-only revisioned
  configuration PATCH. Server fields save after workflow configuration; partial
  failures retain unsaved drafts without resending acknowledged changes.
- Added custom model/effort entry, named prompt CRUD/reference-aware rename, and
  role/team text overrides. Advanced TOML shares the draft and preserves invalid
  text for correction. No prompt store or workflow graph editor was introduced.
- Fixed retained-dashboard handoffs and hidden streams. Added failed-source
  restart admission/options, immutable UI source identity, durable sibling
  reservation locking, and same-workflow retries with unchanged plan progress.
- Added the default all-project run view with complete pagination, bounded fetch
  concurrency, ongoing/recent classification, and browser-local recent count.
- Verification uses disposable repositories and in-memory units. Existing user
  runs and backups remain untouched; Reset Plan stays deferred to issue #34.

## 2026-09-08 — Runs navigation, durable startup failures and appearance

- Split New run from run history, removed Overview, and kept global Settings
  available without project selection. Retained drafts, idempotency keys,
  startup-answer generations and successor recovery across navigation.
- Persisted bounded, redacted startup errors and exposed their reason and
  reserved request identity through REST. Run timing now uses controller start
  and terminal evidence; preparation failures never receive a running timer.
- Added system-aware Light/Dark palettes, browser persistence and cross-tab
  synchronization. Simplified details, diagnostics and relevant run controls.
- Verified a real dirty-checkout rejection through the browser and after reload,
  then completed a separate installed-entry-point NO-OP workflow in one turn.
  All smoke artifacts belong to a disposable project; Doublangu was untouched.

## 2026-09-08 — Reusable NO-OP test plan

- Added `aflow noop-plan` with a packaged checkpoint template and preserved-by-
  default external playbooks. Output/count/state overrides and explicit reset
  support repeated and isolated harness/team/workflow experiments.
- Added `aflow noop-step` for deterministic sleep, marker, cleanup, failure,
  reviewer-rejection, and recoverable worker-incompletion actions. Real agents
  still perform normal plan and
  review bookkeeping; the fixture makes no application changes.
- Added CLI/action tests for defaults, live playbook edits, failure diagnostics,
  preservation/reset, validation-before-actions, and scoped file handling.
- The first live test exposed DSH's private sandbox `/tmp`. DSH now uses
  full-access mode, matching AFlow's VM-oriented unattended harness policy.
  The fixture explicitly forbids agents from recreating inaccessible state.
- Validation: 297 tests plus wheel-resource execution passed. Live
  `AstraGLMMuse` clean run `20260908t125833z-5ce674eb` completed DSH implementation
  and Astra approval. Separate run `20260908t125833z-6ff0ddf3` completed the
  labeled incomplete-worker → Astra rejection → manager-selected Muse upgrade
  → Muse success → Astra approval path. Real fixture/setup failures were
  diagnosed separately and were not counted as mock-test passes.

## 2026-09-08 — DSH ACP model selection

- Added DSH ACP session discovery and execution with provider-qualified models,
  dependent effort selection, exact-session output, and capability-gated resume.
- Added timed pipe reads, unattended permission responses, session cleanup,
  and contract tests for selection failures, resume, and transport bursts.
- Cross-harness target preflight now checks the selected session invocation,
  allowing ACP-only model selection without a headless fallback.
- p100 model/team configuration remains host-local; DSH ACP requires its own
  provider bundle and an inherited ZAI_API_KEY for Z.AI Coding Plan requests.

## 2026-09-06 — Bounded continuous-deployment poller for validated main

- Added `deploy/aflowd/continuous-deploy.py`, a standard-library one-shot
  poller that deploys origin main of the fixed public repository only when the
  exact fetched commit descends from the current release and GitHub Actions
  shows a completed successful `ci.yml` push/main run for that exact SHA.
  Missing, pending, failed, or identity-mismatched CI, divergence, unknown
  releases, and network errors defer with a truthful sanitized status and
  leave the running service untouched; candidate code never executes before
  every gate passes.
- The poller keeps its own dedicated clone under
  `/var/lib/aflowd/deploy/source`, refuses unexpected or dirty sources without
  ever resetting or cleaning, holds an exclusive nonblocking invocation lock,
  and reuses the candidate's own `preflight.sh` and `install.sh` with the
  production defaults (release root `/opt/aflowd`, loopback backend, registry
  and managed root unchanged). Attempted deployments preserve the preflight
  snapshot and installer log under `/var/lib/aflowd/deploy/attempts/`; blocked
  preflight temporary directories are discarded. A failed rollout records
  `last_failed_commit` in the atomic `status.json` and is never retried
  automatically until a new candidate appears or an operator passes
  `--retry-failed`.
- Review repairs to the same slice: a successful install records the
  installed candidate as `current_commit` immediately; a nonzero preflight
  defers only with a valid schema-1 snapshot recording
  `safe_to_rollout: false`, while missing, malformed, or safe-but-failed
  output fails the poll; an installer that outlives its bounded timeout has
  its whole process group terminated before the poll reports failure, even
  for a direct `--retry-failed` run; and a failed final status write makes
  the poll exit nonzero instead of reporting nominal success over a stale
  `status.json`.
- Added `aflowd-deploy.service`/`aflowd-deploy.timer` (oneshot;
  `OnBootSec=2min`, `OnUnitInactiveSec=5min`) plus
  `install-continuous-deploy.sh`, which is dry-run by default and with
  `--apply` installs exactly those two units and enables the timer — no broad
  service restarts. `status.sh` now reports the timer state and the saved
  deployment status, gracefully handling their absence. Tests cover candidate
  gates, deferrals, suppression, exclusive invocation, and installer dry-run
  behavior with temporary repositories and mocked CI responses only.

## 2026-09-06 — Rolling browser session login for the remote web client

- The web client now logs in once by posting the deployment bearer to
  `POST /api/session`; the server sets the signed HttpOnly session cookie and
  the token is cleared from memory and never stored in the browser.
- On load the client checks `GET /api/session`: a valid cookie restores the
  workspace without a prompt, a definitive 401 shows Login, and a network
  failure offers Retry instead of a false signed-out state.
- Visible page restoration renews the session before loading projects; real
  pointer/keyboard/focus activity marks the next authenticated
  REST request `X-AFlow-Activity: 1` (at most once per minute, in memory only),
  which the server uses to roll the 30-day session forward; polling and SSE
  never renew it.
- A 401 during ordinary use or the event stream switches to a session-expired
  sign-in that preserves the current project and view after re-login; logout
  waits for `DELETE /api/session` confirmation before clearing the workspace
  and aborts pending requests so late results cannot refill a signed-out
  workspace. Failed logout offers retryable feedback. Header-only REST and MCP clients
  are unchanged.

## 2026-09-06 — Remove remote agent planning and retain plan management

- Removed the remote provider client, interactive planning surface, and its
  transport dependencies; provider choice remains an engine harness concern.
- Added registry-scoped Markdown plan CRUD with SHA-256 expected revisions,
  bounded regular-file checks, atomic updates, and one-way lifecycle moves.
- Rebuilt the web plan editor around that contract and kept durable run REST,
  SSE, and MCP behavior unchanged.

## 2026-09-06 -- Preserve uncomposable project save guards

- Uncomposable project run snapshots now include the durable frozen config path,
  keeping failed migrated runs and nonterminal blockers on the same save path.

## 2026-09-06 -- Preserve historical config-path resume safety

- Project config saves now ignore a failed or interrupted control-plane run only
  when its durable frozen configuration path is known and differs from the
  canonical project config path. Matching or missing paths remain blocked.

## 2026-09-06 — Run progress, live controls, and guided workflow restart UI

- Rebuilt the web run dashboard on the typed checkpoint-5 contracts: the
  overview now renders `selected_start_step`/`skipped_steps`,
  `restarted_from_run_id` lineage, checkpoint name/index/count and bounded
  manager/harness outcomes from the lite context, and live overrides read from
  the `control_changed` journal event.
- The start form gained capability-admitted per-workflow start-step choices
  with explicit skip explanations, bounded extra-instruction lines, and
  boolean answers for `confirm_recovery`/`confirm_worktree_dirty` questions;
  idempotency-key reuse and draft preservation are unchanged.
- Live controls offer only capability-admitted team/selector values, keep
  local edits on stale revisions, and describe next-safe-boundary semantics.
- A workflow change is a guided confirmation, owner stop with CAS, a bounded
  wait for exact `owner_stopped` status and launch phase, then a successor
  start with `restarted_from_run_id`; normal guided restart is available only
  for active owned runs. A lost successor response freezes the exact request and
  idempotency key for successor-only recovery without another owner stop.
- SSE handling keeps the last snapshot through disconnects, resumes from the
  last sequence, deduplicates replay, refreshes canonical status once on
  reconnect, and never maps a network failure to a run transition.


## 2026-09-06 - Retain uncertain successor recovery across navigation

- Keep the exact pending successor request in the workspace shell until the server resolves it. Inspecting another run or switching views preserves the original project, request and idempotency key, with a visible route back to recovery.

## 2026-09-06 - Stop daemon-owned runs before unit startup

- The daemon owner-stop path now creates the standard run artifact directory
  only after an exact control-plane manifest, caller scope, and initial
  revision are verified. It then uses the existing revisioned control,
  event-journal, unit-stop, and terminal launch-phase path without fabricating
  run metadata.
- An owner-stopped launch phase remains authoritative over a persisted startup
  question during status reads, idempotent start replay, and later answers, so
  a stopped pre-start run cannot create a workflow unit.

## 2026-09-05 - Align manager contract and prelaunch failures

- Updated the bundled manager contract and inline precedence rules for live
  schema-v3 reference-only contexts: read the declared checkpoint evidence
  first, inspect the active/full plan reference only when needed, and never
  search alternate plan files.
- Lite manager contexts now persist the already-supported escalate_to_full
  action alongside the prompt's effective eligibility; Full routing remains
  controller-owned and unchanged.
- Context, evidence-capture, and prompt-budget failures now produce one
  bounded invalid manager decision artifact before the controller records the
  truthful run failure. No provider-start event is emitted and failed
  artifacts do not copy plan bodies.

## 2026-09-05 -- Complete explicit resume rehome and team continuation

- Added the fail-closed `--resume RUN_ID --resume-rehome-worktree PATH` path.
  It validates the current primary branch, recorded feature branch and base
  commit, exact registered replacement worktree, and Git operation state before
  allocation. Only contained resume paths are remapped; the source run remains
  byte-identical and the continuation records `resume_relocation`.
- Bound schema-v2 plan/checkpoint evidence before `keep_runs` pruning and
  copied it into the continuation with in-memory reference rebasing, preserving
  envelope bytes, hashes, scope IDs, and source artifacts.
- Added the named configured `--team` continuation override with exact blockers
  for pending routing state and `resumed_from_team`/`resume_team_override`
  provenance. Automatic and ordinary resumes retain strict team matching.

## 2026-09-06 — Add typed workflow starts and successor lineage

- Added canonical launch options across REST, MCP, daemon, and worker boundaries,
  including validated workflow steps, bounded ephemeral instructions, and
  durable request digests without prompt content.
- Expanded capabilities and status with declared, executable, excluded, and
  skipped steps plus configured teams, roles, admitted selectors, and the
  public status vocabulary.
- Added owner-stopped successor starts with immutable predecessor lineage while
  preserving strict resume checks, idempotency, unit exclusivity, and frozen
  configuration controls.

## 2026-09-05 — Project, config, and plan web workspace

- Rebuilt the web shell around one canonical registered project with
  Projects / Configuration / Plans / Runs navigation, registry-backed
  readiness pills (`ready`, `configuration_required`, `blocked`), and guided
  setup: a new or newly registered project lands in the configuration view,
  and a ready configuration links on to plan selection.
- Added typed create/register and non-destructive unregister flows: relative
  managed-root path, display name, main branch, optional initial
  workflow/team, explicit initialize-Git confirmation for register mode, and
  confirmations that state files, history, and plans are preserved.
- Added a two-tab plain-text editor for `aflow.toml` / `workflows.toml` with
  the shared combined revision, validate-only and atomic save calls,
  stale-revision recovery that keeps local text until an explicit
  discard-and-reload, active-run blocker lists, and unsaved-edit guards
  (navigation confirmation and beforeunload).
- Plan create/read/edit/promote now recover from revision conflicts the same
  way, and every failure path preserves the local draft.
- Plan drafts now use the same shell navigation guard as configuration edits;
  list return requires an explicit discard, and lifecycle moves stay disabled
  until the draft is saved. Project creation and ready-config navigation keep
  their successful server result when a follow-up registry refresh fails.
- Removed the dead `transcribeAudio` client method that targeted the deleted
  transcription route; no session/provider/audio UI remains.

## 2026-09-05 - Add revisioned remote project configuration

- Added the authenticated two-document project configuration contract for
  aflow.toml and workflows.toml, with combined revisions, private-pair
  validation, bounded diagnostics, placeholder readiness, and redacted audit
  metadata.
- Saves use per-project compare-and-swap locking and rollback-safe staged
  replacement. Control-plane-owned active or resume-compatible runs block
  edits; terminal and legacy history remain read-only and do not block idle
  configuration.
- Successful saves reload future-run capabilities while preserving existing
  workflow units and durable run state. Focused server/API and frozen-config
  tests cover validation, concurrency, lockout, rollback, and capability
  reload.

## 2026-09-06 - Preserve failed-turn logs in plain output

- Emit the complete stderr artifact path in final plain status records, including
  failures with no stdout artifact. Added a failed-turn regression with a long path.

## 2026-09-05 — Plain append-only CLI status output

- Replaced the Rich live dashboard, alternate-screen viewport, and cbreak
  terminal input with one plain renderer: append-only `key=value` status
  records on stderr, emitted on meaningful transitions, turn finalizations,
  and one final summary, deduplicating identical consecutive snapshots.
- `aflow show` now prints plain ASCII workflow graphs, roles, and teams with
  explicit `[executable]`/`[excluded]` step words, `[terminal]` END markers,
  and `when` transition conditions instead of panels and color.
- Removed `aflow/terminal_viewport.py`, every Rich import and render object,
  cursor restoration, background refresh/input threads, and all dual-mode
  fallbacks. Dropped the direct `rich` dependency from `pyproject.toml`;
  `rich` remains in the lock only as a transitive dependency of the MCP
  transport and is never imported by AFlow.
- Records bound display values without truncating durable artifact references,
  flatten control bytes, preserve Unicode content, and produce identical
  ordered output for TTY, redirected, `TERM=dumb`, and narrow terminals.
  Engine exit codes, stdout machine results, observer events, and workflow
  state transitions are unchanged. The `BannerRenderer` name is retained
  because workflow call sites and the `banner_files_limit` config option are
  unchanged; the implementation is the single plain renderer.

## 2026-09-06 — Native ZCode harness adapter

- Registered ZCode for noninteractive workflow turns with explicit workspace,
  yolo mode, plaintext final output, and literal prompt arguments.
- ZCode 0.16.5 has no per-invocation model/effort flag. Empty AFlow profiles use
  ZCode's own project configuration; unsupported overrides fail validation and
  direct invocation construction rather than silently selecting another model.
- Verified the public runner with a fixture CLI, configuration rejection,
  literal prompt arguments, and recorded turn identity. Full suite: 1,403 tests
  plus 216 subtests; production Ruff, compilation, and wheel/sdist build pass.
  This does not claim a paid live provider or model-selection test.
- Provider usage accounting and the delegation benchmark remain separate work.


## 2026-09-02 — Manager context references and prompt budget

- A 340 KB manager context (incident `20260902t053828z-5cfe3386`, decision
  `manager/decision-005`) failed before Reasonix started with `errno 7:
  Argument list too long`. The same 63,580-byte plan was duplicated across
  `active_plan_content`, `original_plan_content`, `envelope.plan_text`,
  envelope base64, and checkpoint payloads.
- Added a run-local content-addressed evidence store
  (`.aflow/runs/<run-id>/evidence/{plans,checkpoints}/<sha256>.md`) with
  idempotent atomic writes and fail-closed reads; each distinct plan/checkpoint
  byte sequence is stored once per run.
- Scope envelopes now have a reference-only schema v2 (no plan/checkpoint text,
  no copied source-block text); new scopes write v2, v1 artifacts remain
  strictly parseable and readable. Drift validation and repartition rendering
  resolve referenced evidence bytes and materialize source blocks by verified
  byte spans.
- Manager-context schema v3 (selector >= 4) is reference-only: no plan bodies,
  no base64 evidence, no raw reviewer transcript. Runtime boundaries capture
  evidence once by content hash; historical rebuilds never write.
- The inline manager prompt targets 16 KiB with a deterministic 32 KiB hard
  limit enforced before any provider process starts; non-sensitive prompt
  metrics persist in the manager result and analysis labels referenced
  artifact bytes as not model input.
- Manager adapters must advertise the fail-closed `manager_workspace_read`
  capability for reference-only contexts; Reasonix one-shot prompts moved from
  argv to stdin so they can never fail `execve` with `E2BIG`.


## 2026-09-02 — Respect ACP exact-resume capability

- Owned session execution no longer implies that a harness can resume a prior
  session in a new process. AFlow now supplies a persisted session ID only when
  the driver explicitly advertises `resume_with_model`.
- Same-harness hotplug retains its exact-resume requirement, while ordinary
  Reasonix turns start fresh ACP sessions and replace the persisted role
  session reference after success.
- Added a workflow-level regression for a process-local owned ACP executor.

## 2026-09-01 — Allow bounded Reasonix ACP workspace initialization

- Raised Reasonix session-open and configuration-update requests from the
  transport's 10-second default to a bounded 60 seconds, preventing larger
  worktrees from failing before a prompt while retaining the separate
  long-running prompt timeout.
- Enforced one overall request deadline across ACP notifications so progress
  traffic cannot reset the timeout indefinitely.
- Accepted Reasonix's exact same-session `config_option_update` notification
  as the configuration result, while retaining complete-state acknowledgement
  checks before any prompt.
- Ignore the single late correlated response for a request already settled by
  that notification; all unknown response IDs remain terminal mismatches.
- Added focused coverage for new-session and resumed-session negotiation.

## 2026-09-01 — Canonical dynamic project registry

- Replaced runtime static control-plane project tables and broad catalog
  discovery with one atomic, versioned exact-root registry under a canonical
  managed-projects directory.
- Centralized release executable, identity, environment file, and child
  environment at server composition; project records can no longer override
  process inputs.
- Made project daemons lazy and isolated per-project readiness failures so a
  registry update is usable without restarting the remote server.
- Restored exact-root validation clears transient registration failures, so a
  temporarily unavailable cached project recovers without a server restart.

## 2026-09-01 — Audit public documentation scope and accuracy

- Removed the host-specific p100 deployment from public README navigation and
  remote-app setup guidance while retaining it as an explicitly scoped operator
  runbook beside the environment-specific scripts.
- Corrected command and harness inventories, lifecycle-neutral resume behavior,
  automatic startup metadata refresh, current profile examples, subprocess
  stdin behavior, library exports, architecture layout, package metadata, and
  remote-app configuration and authentication boundaries.
- Reframed daemon and deployment documentation around portable product
  boundaries instead of one host's paths, address, and operating procedure;
  removed private deployment assumptions from the bundled guard guidance.

## 2026-08-31 — Enforce production static hygiene (PR-16)

- Base SHA: `ab56e821`; the measured baseline was `38 F401 / 0 F811 / 6 F821
  / 10 F841`.
- Preserved the intentional seven-model API re-exports with an explicit
  `__all__` contract.
- Added pinned Ruff `0.16.5` as a production-only CI gate.
- Final verification: root `1,354 passed, 216 subtests passed`; app server
  `186 passed`; web `34 passed`; compile, package build, lock, and clean-wheel
  checks passed.

## 2026-08-31 — Stop shipping test support and pytest at runtime (PR-15)

- Base SHA: `059b1fb5ad6654f18935b20db7538663361310ad`.
- Moved the shared helper bundle from `aflow/_test_support.py` to
  `tests/_support.py`, removed its 7,369 blank padding lines and dead direct
  execution guard, and preserved the 19-helper wildcard re-export contract.
  The helper changed from 7,644 physical/275 nonblank lines to 504/457.
- Moved `pytest>=8.0.0` from runtime dependencies to the default `dev` group;
  the lockfile keeps it development-only. Root collection stayed at `1,353`
  and the full suite passed with `1353 passed, 216 subtests passed`.
- The rebuilt wheel omits the test helper and `pytest` runtime requirement; a
  fresh Python 3.11 wheel-only install printed `runtime-package-clean`.

## 2026-08-30 — Lift repartition application coordinator out of the workflow closure (PR-14)

- Lifted the 274-line pending-repartition reconciliation and application
  closure, replacing its 12 captured names with eight stable coordinator
  fields and five per-call path/step/team values.
- Added a small live-state bridge returning and assigning the active plan path
  and current step, preserving the unchanged manager-gate and startup-resume
  consumers. Consolidated cycle and application persistence through one
  explicit `_persist_pending_repartition()` helper, preserving the
  post-application path/step-before-final-write and event ordering.
- Base SHA: `3474eeb65e4b80b2561844e72378f666c184dcb2`.
- Verification: repartition `100 passed`; repartition-filtered runtime `7
  passed, 249 deselected, 6 subtests passed`; control-plane resume `4 passed`;
  manager `42 passed`; manager-filtered runtime `35 passed, 221 deselected, 7
  subtests passed`; root `1353 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency warnings; web `34 passed`; web lint
  zero errors with five known React hook warnings; structural AST and
  behavioral-equivalence ledgers passed; Python compileall, package build, and
  web production build passed.

## 2026-08-28 — Lift repartition cycle executor out of the workflow closure (PR-13)

- Lifted the 462-line nested repartition proposal, mechanical-validation, and
  semantic-validation cycle into one private, frozen, module-level
  `_RepartitionCycleExecutor`.
- Replaced its ten captured names with eight stable constructor fields plus the
  two per-call plan paths, `original_plan_path` and `active_plan_path`.
- Kept proposal, correction, candidate, verdict, artifact, persistence, and
  durable transaction contracts unchanged. Invocation, persistence, and plan
  application remain live nested callbacks; automatic repartition remains
  opt-in.
- Base SHA: `388838a794bc713ae9f7c8da8126273a7ff72eb0`.
- Verification: repartition `100 passed`; repartition-filtered runtime
  `7 passed, 249 deselected, 6 subtests passed`; control-plane resume `4
  passed`; manager `42 passed`; manager-filtered runtime `35 passed, 221
  deselected, 7 subtests passed`; root `1353 passed, 216 subtests passed`;
  app server `186 passed` with three existing deprecation warnings; web `34
  passed`; web lint zero errors with five existing React hook warnings; Python
  compileall, package build, and web production build passed. Structural
  ledger and diff checks passed: one executor construction and callback
  injection, eight executor fields, manager gate 11 fields and three manager
  calls, manager executor 11 fields and five call sites, unchanged 274-line
  application helper, and zero legacy closure references.
- No live workflow, editable-tool retarget, service restart, deployment, or
  unrelated file change occurred.

## 2026-08-28 — Refresh manager executor team after live overrides

- Removed `baseline_team_name` from the frozen `_ManagerCallExecutor`
  constructor and passed the current boundary team into all five manager calls,
  so manager metadata and Lite/Full profile resolution follow a team override
  accepted after the workflow has started.
- Strengthened the manager team-override regression to apply the override after
  turn one and verify the manager model changes from the base team to the live
  team while upgrade routing remains correct.

## 2026-08-28 — Refresh manager gate baseline after team overrides (PR-12 follow-up)

- Removed `baseline_team_name` from the frozen `_ManagerGateCoordinator`
  constructor and passed the current boundary value into all three `run()`
  call sites, preserving team-override updates for manager context, upgrade
  eligibility, and target routing.
- Added a manager-enabled team-override regression covering the hotplug chain
  and the executed upgrade selector.

## 2026-08-28 — Lift manager gate coordinator out of the workflow closure (PR-12)

- Lifted the 436-line nested manager gate closure into one private, frozen,
  module-level `_ManagerGateCoordinator`. Its 11 stable constructor fields
  exclude the mutable baseline team; the five boundary inputs are passed per
  call: `baseline_team_name`, `original_plan_path`, `active_plan_path`,
  `new_plan_path`, and `runtime_current_step_name`.
- Kept the manager level helper module-level with an explicit stalled-turn
  threshold, while repartition execution remains nested and automatic
  repartition remains opt-in. Manager action eligibility, Lite/Full selection,
  recovery, resume, durable artifacts, events, and routing contracts are
  unchanged.
- Base SHA: `03992439d006fda19a86f96c73e86ccaae4a8b3b`.
- Focused verification: manager `42 passed`; manager-filtered runtime `34
  passed, 221 deselected, 7 subtests passed`; repartition/control-plane resume
  `104 passed`; runtime/runlog/CLI `434 passed, 171 subtests passed`;
  compileall, structural ledger, callback-equivalence, and diff checks passed.
- Full verification: root `1352 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency deprecation warnings; web `34 passed`;
  web lint zero errors with five existing React hook warnings; Python
  compileall, package build, and web production build passed. Structural
  ledger: one coordinator class/construction and three coordinator calls with
  11 fields and five per-call mutable inputs; one module helper; unchanged
  nested repartition callbacks; one executor construction and five executor
  calls.

## 2026-08-27 — Lift manager call execution out of the workflow closure (PR-11)

- Lifted the 388-line nested `_run_manager_call()` closure into one private,
  frozen, module-level `_ManagerCallExecutor`. Its 16 captured names are now
  12 stable constructor fields plus three per-call mutable inputs:
  `original_plan_path`, `active_plan_path`, and `current_step_name`.
- Lifted the repository fingerprint helper with explicit repository and plan
  paths. Lite/Full decisions, correction eligibility, note scopes, artifacts,
  events, history, failure handling, repartition, and resume contracts remain
  unchanged; gate policy remains inside `run_workflow()`.
- Base SHA: `b674915759b6389498bf370a74fa1e02bf2b92f1`.
- Verification: manager `42 passed`; manager-filtered runtime `34 passed,
  221 deselected, 7 subtests passed`; repartition/control-plane resume
  `104 passed`; runtime/runlog/CLI `434 passed, 171 subtests passed`; root
  `1352 passed, 216 subtests passed`; app server `186 passed` with three
  existing dependency deprecation warnings; web `34 passed`; web lint zero
  errors with five existing React hook warnings; compileall, package build, and
  web production build passed. Structural ledger: one executor class and
  construction, five executor calls, four fingerprint calls, and no
  `_run_manager_call` references.

## 2026-08-27 — Consolidate standard workflow failure finalization (PR-10)

- Consolidated the 13 standard post-context terminal failure sequences into one
  private finalizer, preserving summaries, plan identity, snapshots, banner
  ordering, and all five exception cause chains; the two writer-fallback
  snapshots remain implicit at their original call sites.
- Left the 15 special failed writes explicit, including startup, preflight,
  manager/event, merge, pruning, and max-turn responsibilities.
- Base SHA: `1a9e8b1df478d0ccf8d79b3e2234da80f07a43a4`.
- Focused verification: runtime/runlog/CLI `434 passed, 171 subtests passed`;
  manager/repartition/control-plane resume `146 passed`; compileall and diff
  checks passed.
- Full verification: root `1352 passed, 216 subtests passed`; app server `186
  passed` with three existing dependency deprecation warnings; web `34 passed`;
  web lint completed with zero errors and five existing React hook warnings;
  package and web production builds passed.

## 2026-08-26 — Bind workflow run-metadata persistence (PR-09)

- Replaced 56 workflow calls that repeated run paths, controller config/state,
  workflow name, and predecessor run ID with one frozen `RunMetadataWriter`;
  mutable plan paths, execution context, current step, snapshots, counters,
  failures, and terminal metadata remain explicit at every write.
- Preserved schema-v2 validation and atomic replacement, resolved-team
  precedence, lifecycle fields, manager/hotplug state, repartition, resume, and
  terminal persistence contracts. The standalone writer function and alias
  surface were removed.
- Base SHA: `aaa8dbc67fcac522023d910c4213679e18cd909d` (merged PR #21).
- Focused verification: runlog `28 passed`; runtime/CLI `405 passed, 171
  subtests passed`; manager/repartition/control-plane/runlog `174 passed`;
  compileall and diff checks passed.
- Full verification: root `1351 passed, 216 subtests passed`; app server `186
  passed`; web `34 passed`; web lint completed with zero errors and five known
  hook warnings; Python/package and web production builds passed.

## 2026-08-23 — Require complete current resume state (PR-08)

- Bumped durable controller metadata to schema version 2. Fresh writers now
  emit the authoritative original plan, explicit team/lifecycle fields, frozen
  configuration identity, complete manager state, hotplug state, and active
  scope envelope references.
- Explicit, `--resume` auto-selected, and daemon resumes admit only exact
  integer schema 2 with complete metadata. Older metadata remains readable for
  analysis but is rejected before plan lookup, startup questions, allocation,
  or continuation; no migration or rewrite is attempted.
- Base SHA after PR 7: `30213fc4ed70b5847e1cf78154f943f5b3a957f9`; PR 7 merge SHA:
  `30213fc4ed70b5847e1cf78154f943f5b3a957f9` (PR-19).

## 2026-08-23 — Restore the supported cross-platform test matrix (F-07)

- The documented root suite now has a truthful Linux/macOS boundary: Linux runs
  the p100 deployment tests, while macOS skips only that 15-test Linux module.
- Repaired ownership guidance, executable Reasonix, Rich transition, and
  AFlow-owned terminal-state test expectations without changing production
  behavior.
- Added reusable Ubuntu/macOS Python 3.11 CI and made package publication wait
  for the same workflow before building or uploading.

## 2026-08-23 — Make local daemon worker status portable and truthful (F-06)

- Local daemon status preserves Linux procfs ownership inspection and uses one
  bounded, strict process-table snapshot on systems without usable procfs.
- Only exact direct `daemon-worker` children for the verified repository are
  counted; malformed or untrusted ownership evidence returns explicit
  ambiguity instead of a false zero, with a daemon birth-identity recheck
  before success output.
- Added real forced-fallback and deterministic parser coverage for spaced paths,
  multiple workers, unrelated/legacy processes, malformed snapshots, and
  ownership races.

## 2026-08-22 — Match resumed legacy controllers by durable lineage (F-05)

- Resumed legacy guards now bind a successor's durable predecessor to one exact
  explicit `--resume` CLI option.
- Free run-ID substrings and post-`--` text no longer establish ownership.
- Wrapper deduplication, descendant selection, classifications, and observer-only
  authority remain unchanged.
- Deterministic tests cover lineage positives, wrappers, descendants, duplicates,
  and collision/spoof cases.

## 2026-08-22 — Bootstrap Git Tracking before run allocation (F-03)

- Fresh pristine review plans now receive one minimal Git Tracking section
  before run identity, launch persistence, start events, or run paths exist.
- The controller preserves the exact pre-insertion backup, atomically writes
  and reloads the plan, verifies its checkpoint snapshot, and records either
  current `HEAD` or the verified empty-repository bootstrap commit.
- Startup rejects prepared-plan snapshot drift before writing, lifecycle branch
  synchronization rewrites only the parsed live section, and daemon starts run
  the same normalization boundary before reserving launch identity. Workers
  recompute an empty-repository deferred base fill after daemon preparation.
- Removed the unreachable base-HEAD confirmation enum and dispatch paths;
  existing pristine-section refresh remains automatic and noninteractive.
- Added deterministic plan, runtime, lifecycle, public-library, and CLI
  coverage with injected runners only; no model or provider call is required.

## 2026-08-22 — Fail-closed headless Reasonix ACP (F-02)

- Owned new and resumed Reasonix sessions now negotiate the exact
  `tool_approval=yolo` select option before model, effort, and prompt.
- Every configuration update must return a complete, unambiguous state that
  still acknowledges all applied values; missing, rejected, or reset approval
  closes and terminalizes the run before prompting without retry.
- Added deterministic new/resume ordering, malformed and rejected state, reset
  detection, process-close, and durable workflow-failure coverage. No live or
  paid model prompt was run.

## 2026-08-22 — Truthful turn and run termination (F-01/F-04)

- Finalize ordinary catchable post-start exceptions into one terminal turn and
  a failed, resumable run with bounded, redacted evidence and no generic retry.
- Require a complete post-turn original-plan snapshot before `END`, merge,
  worktree removal, or successful completion; incomplete max-turn and ordinary
  `END` preserve routing evidence and lifecycle state but fail.
- Added owned-session, observer, hotplug-resume, manager-on/off, CLI, and real
  worktree regressions. Harness negotiation, Git Tracking bootstrap, guard,
  daemon/API/UI, and architecture rewrites remain intentionally excluded.

## 2026-08-23 — Remove deprecated unscoped execution routes

- Removed `POST /api/executions`, `GET /api/executions/{run_id}`, and
  `GET /api/executions/{run_id}/events`, including the `find_run()`
  cross-project compatibility scan and adapter models; no redirect, tombstone,
  or compatibility alias remains.
- Lifecycle REST is now project-scoped under `/api/control-plane`; clients
  carry `project_id`, while REST and MCP continue to share the same durable
  control-plane application and services.

## 2026-08-11 — Add lightweight stdio MCP daemon

- Added `aflow daemon start|status|stop` for a single local project, with stdio
  MCP by default and optional loopback HTTP, separate from production `aflowd`.
- Added exact subprocess-unit ownership, process-group drain/escalation, and an
  atomic mode-0600 pidfile bound to process-birth identity.
- Moved the 13-tool, three-resource MCP registry into the core package while
  retaining the FastAPI adapter's header-auth and safe-error boundary.
- Added config validation, split-config preservation, public FastMCP parity,
  and stdio initialization/EOF cleanup regressions.
- Live restart proof fixed reconciliation precedence so a durable terminal
  controller record and matching launch phase supersede an older daemon
  observation instead of being misreported as needs_attention.

## 2026-08-10 — Document daemon-backed control-plane boundary

- Documented the release-pinned p100 service, strict project allowlist,
  header-only bearer transport, and the separation between daemon workflow
  ownership and HTTP/MCP/UI transports.
- Recorded reconciliation semantics: a collected or failed workflow unit is
  `needs_attention`, never automatic completion or restart; owner stop and
  explicit linked resume remain durable operator actions.
- Corrected remote-app documentation that described the retired in-memory
  execution map and query-token SSE behavior.
- Production-installed the exact release on p100 and exercised authenticated
  REST/MCP parity, idempotent start, safe-boundary CAS steering, startup
  questions, daemon restart, hard workflow death and explicit resume, owner
  stop, token rotation, legacy-run read integrity, and versioned rollback.
- Live failures led to focused fixes for writable daemon state, Tailscale
  address inspection, TOML validation, readiness rollback, Python safe-path
  entrypoints, hard-kill reconciliation, startup-question status,
  cross-surface bearer ownership, and release-pinned rollback.
- Review transitions now enter the rejection ledger only when a new repair
  plan exists, preventing approvals from influencing worker-upgrade routing.


## 2026-08-09 — Durable live worker role hotplug

- Added provider-neutral worker selector hotplug with durable transaction
  stages, exact same-harness resume, and cross-harness read-only operational
  handover.
- Resume copies and verifies handover/projection/Full-context artifacts before
  pruning; ambiguous provider boundaries remain waiting unless a durable
  operation result proves a matching target completion or not-started retry.
- Status/events/analyze expose safe transaction stages, selectors, capability
  path, relative artifact references, and hashes while omitting prompts, notes,
  environment, and raw session identifiers.
- Capability evidence was protocol/fixture based; no paid live Reasonix prompt
  smoke is claimed.

## 2026-08-08 — Forward Reasonix effort independently

- Reasonix profiles now pass their configured effort through the CLI's native
  `--effort` option instead of requiring a nonexistent model-name variant.
- Adapter coverage fixes the DeepSeek V4 Flash/max invocation contract; the
  deployment configuration selects Sol 5.6/medium for `ds4_flash_max` reviews.

## 2026-08-08 — Prefer fastest-safe guard completion

- Guarded recovery now chooses the smallest reversible, scope-reducing option
  when it preserves the plan's usable outcome and does not weaken safety,
  authorization, acceptance, or approved cost boundaries.
- Plan-less exact `aflow run --resume RUN_ID` controllers are recognized only
  when the resume ID matches durable lineage and the controller cwd matches the
  guarded repository; self-tests reject the same argv from another repository.

## 2026-08-07 — Add role-scoped workflow prompts and material review

- Added validated global and team role prompts with team replacement, global
  fallback, retry persistence, and frozen-run identity coverage.
- Ordinary workflow turns now persist resolved role guidance as their system
  prompt; manager and lifecycle invocations retain their existing prompt paths.
- Bundled `material-code-review` as a default skill and enabled its guidance for
  the packaged reviewer role.

## 2026-08-07 — Complete and resume every lifecycle mode

- Branch-only merge teardown now switches a clean primary checkout from the
  expected feature branch to `main` before attempting the engine-owned
  fast-forward, while unrelated checked-out branches still fail closed.
- Explicit and interactive resume now reconstruct no-lifecycle, branch-only,
  and linked-worktree execution contexts from mode-specific durable metadata.
- Regression coverage exercises ordinary branch-only completion plus
  environment-preflight recovery without synthetic worktree paths.

## 2026-08-07 — Preserve guard replacement lineage

- Replacement linkage now retains the original recovery fingerprint, validates
  complete predecessor/successor identity and one live successor controller,
  and migrates unsafe same-successor linkage without rewriting observation
  history.
- Missing or malformed identity fields and absent original plans fail closed
  before guardian state is written.

## 2026-08-07 — Add adapter-neutral harness environment preflight

- Added a bounded, secret-safe executable check for real harness launches and
  an optional adapter capability for local prerequisites.
- Reasonix now uses `reasonix doctor --json` to detect enforced bash sandboxing
  without guessing configuration files; missing `bwrap` is reported with fixed
  remediation and no synthetic turn or manager artifact.
- Workflow, manager, correction, repartition, recovery, bootstrap, and
  agent-required merge boundaries share terminal run-level handling. Injected
  runners remain ready by default; explicit resume re-evaluates the pending
  invocation. AFlow reports prerequisites but does not install packages or
  validate provider health.
- The guardian remains the fallback for legacy and unsupported failures.

## 2026-08-06 — Make clean-END manager eligibility deterministic

- Clean controller-proposed `END` boundaries now omit `stop` from Lite
  eligibility while preserving the existing action set for Full.
- Ineligible or invalid Lite output and explicit Lite escalation restore Full
  eligibility for one decision from the same finalized turn; accepted Full and
  non-clean-boundary `stop` decisions still fail through the existing report,
  event, and banner path.
- Focused manager and runtime regressions cover Lite `stop` to Full `continue`,
  both Full-stop fallback routes, direct Full selection, and ordinary non-END
  Lite `stop` without interpreting manager prose.
## 2026-08-02 — Correct manager plan-selection notes at one boundary

- An otherwise valid manager response whose only authority defect is
  plan-selection wording now receives one same-profile correction sub-attempt
  before persistence; only advisory notes may change.
- Original and corrected traces remain under one decision directory, while
  routing, history, observer events, and turn accounting retain one logical
  manager decision. Mutation, boundary drift, immutable-field changes, launch
  failure, invalid JSON, and a second authority failure stop without retry.
- Verification passed: focused context (`1` test), focused runtime (`6` tests,
  `5` subtests), manager/context (`74` tests), broader manager runtime (`26`
  tests, `7` subtests), and documentation (`20` tests), plus compilation and
  diff hygiene.

## 2026-08-02 — Trust turn diagnostics by stream and outcome

- Text-signal evidence now records semantic stdout separately from
  failure-eligible stderr. A successful turn's stderr transcript cannot turn
  echoed plan, branch, merge, or owner-action phrases into structural
  failures; explicit stops and structured failure evidence remain unchanged.
- Lite manager context intentionally omits plan prose, labels that redaction,
  and gives durable controller-owned plan/workspace facts precedence over
  contradictory text. The additive provenance and disclosure fields preserve
  existing sorted signal-name lists and schema-v1 output.
- A managed-worktree runtime regression confirms the incident-shaped turn gets
  one accepted post-turn decision, launches checkpoint review, and retains the
  implementation change and worktree without a false stop or cleanup.

## 2026-08-02 — Bundle same-task AFlow guard

- Moved `aflow-guard-development-run` into AFlow's canonical bundled-skill
  inventory and made it a default install for supported harnesses.
- Guard heartbeats and actionable reports now stay in the task that requested
  supervision; the bundled helper retains pinned-run and provenance checks.

## 2026-08-02 — Bind pending repartition resume identity and paths

- Non-reset explicit and AUTO resume now require the restored active scope,
  envelope, manager decision boundary, derived generation ID, executable target,
  and canonical artifact keys before startup; internal symlink aliases fail
  closed, while reset scope keeps pending state opaque.

## 2026-08-02 — Fail-closed pending repartition resume validation

- Explicit and AUTO resume now reject present malformed or stage-incomplete
  pending repartition state, validate every carried artifact reference, and
  bind its bytes before startup; reset-scope resume continues to discard this
  checkpoint-scoped state without interpreting its metadata or artifacts.

## 2026-08-02 — Complete resume context before startup

- Explicit and AUTO resume now decode the selected run's complete worktree,
  lifecycle, manager, scope, pending-artifact, and override context before
  startup questions; post-startup checks reuse that same loaded context.

## 2026-08-02 — Plan-optional durable resume bootstrap

- Resume now resolves one explicit or shell-selected durable run before startup
  preparation, reconstructs omitted plan and invocation identity from `run.json`,
  and rejects conflicting repeats or unsafe metadata without creating state.
- `original_plan_path` is authoritative, with `plan_path` retained only as a
  legacy fallback. Fresh runs still require `plan_file`.
- Resume also preserves an explicitly empty `--` instruction suffix as caller
  input and rejects path-shaped run IDs before loading durable metadata.

## 2026-08-02 — Frozen configuration identity on resume

- Schema-versioned resume metadata now requires a complete `frozen_config` and
  rejects workflow, canonical config-path, or configuration-fingerprint drift
  before startup questions or new durable state.
- The execution boundary repeats the comparison after reloading configuration,
  while schema-less legacy metadata without a frozen identity retains its older
  scalar/lifecycle compatibility path.

## 2026-08-02

- Standardized p100 self-hosted development on an editable uv tool installation:
  run `uv tool install -e . --force` from the intended AFlow checkout, then invoke
  `aflow` directly. `uv run aflow` is not a supported development launcher;
  `uv run` remains available for tests and other project-scoped checks.

## 2026-08-01 — Codex prompt stdin transport

- Checkpoint 1 moves Codex effective prompts from argv into stdin while keeping
  prompt artifacts and injected-runner behavior observable.
- Large-prompt and early-stdin-close runtime fixtures pass; launch-failure
  normalization remains planned for Checkpoint 2.

## 2026-08-01 — Harness launch failures use terminal paths

- Checkpoint 2 normalizes process-creation `OSError`s from default and injected
  harness execution into bounded nonzero results: 127 for missing executables
  and 126 for other launch failures.
- Manager artifacts, worker turn artifacts, and lifecycle failure metadata now
  use their existing nonzero-result handling without storing a traceback or
  prompt-bearing launch diagnostic.

### Browser verification: live status and manager display

- Config saves immediately update the shell readiness badge.
- Healthy event streams refresh canonical run and bounded context summaries, coalescing bursts and ignoring stale requests while preserving the last snapshot on failure.
- Manager outcomes read the canonical run-extract schema; completed plans display completed checkpoints.
- Verified real-provider browser start, live controls, owner stop and successor completion; added regressions for the observed display gaps.

- Final browser screenshot check: native dropdowns now inherit text styling and use the dark color scheme, keeping selected values readable.

### 2026-09-06 — Clarify remote interfaces and private entry point

- Documented canonical REST/SSE, optional MCP, deferred remote ACP, and Codex as an optional engine harness. Root usage and architecture now point to the private deployment runbook and MagicDNS HTTPS discovery through Tailscale Serve status.

### 2026-09-06 — Report applied live turn limits

- HTTPS browser verification found that status retained the initial turn limit after a safe override. Control-plane projections now use the engine-recorded effective limit; pending override writes alone do not change the reported value.

## 2026-09-06: Combine guided settings with rolling login

- Integrated approved guided configuration backend e59721c with deployed browser login 6076caf. Retained both session routes and guided-form routes when resolving their shared insertion point.
- Verified the combined server suite (204 passed), engine configuration tests (135 passed, 7 subtests), and Ruff. UI and deployment automation integration remain pending.

### 2026-09-06 — Dashboard changes participate in CI

- CI gained a required Ubuntu dashboard job (Python 3.12, Node 22 with npm cache
  keyed to the web lockfile) so main's success also covers the shipped app server
  and web UI.
- The job runs the server's frozen dev sync and full pytest suite, then npm ci,
  the full vitest suite, and the production web build in their project
  directories; it runs for PRs, main pushes, and reusable workflow calls.
- Existing engine test matrix and package build job are unchanged; no publishing
  trigger or credentials were added.
- Local verification baseline: server `uv run pytest -q` 163 passed (3 known
  deprecation warnings); web tests 87 passed with known React `act(...)` stderr
  notices; `npm run build` and `git diff --check` clean.

- Deployment owner repair: all timed-out poller subprocesses now terminate their process group, including children left after parent exit; focused regressions cover TERM-ignoring descendants and both error/deferral results.

- Browser preview found that pure empty-draft validation could override missing-file readiness. The settings report now preserves saved readiness for unchanged documents and explains the starter/save step before offering Plans.

- Browser acceptance repairs: label ZCode models as externally configured, distinguish unspecified values from missing profiles, and preserve the selected project name on narrow screens.
### 2026-09-06 — Preview exact dashboard runs (checkpoint 5)

- Plans now explain Draft, Ready and Done, and a saved Ready plan opens the exact launch draft.
- The compact launch form offers configured workflow/team choices and previews each materialized step's role, selected profile, team/global source and model/effort details. ZCode settings are identified as external.
- Only Ready plans with a resolved workflow preview and valid turn limit can start. Invalid extra-instruction guidance stays visible when Advanced options is closed.
- Retained startup-answer, idempotency and stop-before-replacement behavior. The owner corrected a test callback option typo after stopping the repair controller; validation and approval are recorded in the original checkpoint ledger.

- Narrow-browser inspection found the selected Runs tab clipped offscreen. Mobile navigation now shows Projects on its own row and all four project views together without horizontal scrolling.

- Plan promotion buttons now use the same Ready/Done names as the surrounding guidance; settings explain the shared save in plain language.

- Run setup and live-control help now explain Ready plans, available choices, and Pending changes in plain language.

- Run lists now show the newest returned run first using canonical launch time (or the timestamp in older run IDs), while refresh preserves an explicit selection. The API itself remains stably paginated in ascending identity order.
- Checkpoint 6 adds project/run links, progress-first run views and Technical details. Owner repair removes URL fragments from canonical links, constructs clipboard links from validated identities, and retains the selected run across oldest-first page refreshes.

- Run progress and editable settings now use plain labels; internal bounded-event and revision terminology remains in technical details.

- Completed the dashboard App journey contract test: discovery, Add/Open, guided role assignment, failed-save recovery, Ready plan preview/launch and exact-link remount. Updated the remote-app guide and architecture for discovery, shared drafts, suggestions, run links and the separate deployed owner acceptance recipe.

- CI registration preservation checks now compare the pre-existing Git status with global excludes disabled; a user’s ignored .aflow directory no longer hides the fixture’s untracked configuration.
- Fixed the continuous deployer’s real preflight handoff: reserve a unique snapshot path, then release the empty directory so preflight creates it. The deployment fixture now rejects existing paths like the production script; the success regression fails before the fix.

- CI process-cleanup assertions now recognize both a missing proc file and a process disappearing during the read as termination, with deterministic coverage of both Linux outcomes.

## 2026-09-08 — Web UI usability follow-up (plan: aflow-web-ui-usability-review-followup-20260908)

- Implemented the full review follow-up in `apps/aflow_app`: settings repair (mistyped prompt tables keep Advanced TOML usable with production diagnostics; confirmed reload truly discards every pending edit including the password, with epoch-guarded late responses), secondary prompt actions (More menu, Delete prompt… with confirm/Undo, "Used by" disclosures), one-click Diagnostics full context (acknowledgement removed, `level=full&full_scope=true` sent directly, stale responses rejected), compact project/run lists (semantic rows, row-as-open, Unregister behind a More menu), `AFlow · <project>` header without the project bar, sticky Settings toolbar with tabs+Save, visible launch defaults (`checkpoint_delivery · Default` style) with request-omission semantics for followed defaults, Team members + full worker upgrade chain previews, an explicit Add team form (draft-only until Save all, Enter support, inline validation), and typed `set_team_upgrade` edits of `[teams.<name>].upgrade_to` with cycles/missing targets surfaced field-level and validated by production graph checks at save.
- Server: guided projection guards malformed prompt shapes (form omitted with unchanged documents instead of HTTP 500), hardened prompt-reference traversal (step arrays and string/array `merge_prompt`), `GuidedTeamSummary.upgrade_to`, and the closed `set_team_upgrade` action; `add_team` actions are ordered before dependent edits in net batches.
- Verification: 212 web tests, 155 app-server tests across the prescribed suites, 145 core tests (+7 subtests), full server+core sweep 1762 passed (+222 subtests), `git diff --check` clean, `ruff` clean, `tsc`/vite build clean, and 33 headless-Chromium checks against the live deployment at `http://p100.tail0fad23.ts.net:8766` (header, compact lists, sticky tabs, prompt menus, Add team draft/discard, launch defaults before focus, one-click Diagnostics, mobile tablist). No real configuration was saved, no password rotated, no project unregistered, no run started during checks.

### 2026-09-08 — Worker failure diagnostics follow-up

Implemented exit-aware read-only status and guarded recovery, bounded redacted detached-child capture, early exception stages, unified dashboard refresh and summary/raw diagnostics. Fixed prompt Undo across tabs/multiple deletions and moved recent count editing exclusively to General. Preserved the existing dirty UI work and historical run artifacts.

Verification exercises installed `aflow ui-worker` → real controlled child → receipts → repository/service → authenticated REST and Chromium, without restarting the service or page. Also exercises installed `aflow daemon-worker` configuration-load failure, high-volume stdout/stderr, redaction/caps, exit zero, signal termination, stale/malformed receipts and controller authority. No real owner workflow was launched or resumed.

Owner follow-up: fixed the Settings toolbar disappearing on long scrolls by preventing its containing flex item from shrinking below its content. Chromium reproduced the original failure and verifies tabs/Save at 25%, 50%, 90% and full scroll on 390px/1365px light/dark layouts. Added `test_settings_browser.py` for the real layout boundary.

Final verification: web tests (213), all server tests including Chromium layout (233), required worker/control-plane tests plus real-process diagnostics (82), and required API/auth/MCP/manager-context tests (89) passed. Web production build, Ruff and diff whitespace checks passed.

## 2026-09-09 — Manager context recovery headroom

- Raise the manager inline user-prompt hard limit from 32 to 40 KiB so the completed-checkpoint continuation can reach review after a 33,576-byte context rejection. Retain the 16 KiB target and prelaunch UTF-8 byte enforcement. Compact summaries with disk-backed history are planned separately.

- Recovery also recognizes a durable manager context-budget prelaunch failure after a completed worker turn. Resume replays its validated manager boundary rather than rejecting the completed plan or skipping pending review; unrelated completed-run and consumed-boundary gates remain intact.
- Preserve original configuration identity during direct CLI resume from a validated predecessor snapshot; retain fingerprint checks and reject unrelated paths.

## 2026-09-09 — Readable machine labels

- Added one shared sentence-style formatter for snake_case identifiers, including
  repeated-underscore collapsing, empty values, custom names and explicit acronym
  casing.
- Applied it at prompt, workflow, role, step, event and generated-settings label
  boundaries while keeping prompt keys, selector values, API payloads, URLs,
  DOM identity and technical details exact. Search accepts both raw identifiers
  and readable labels; colliding readable labels retain raw secondary text.
- Verification: 270 web tests, the production build, and four disposable
  Chromium checks across desktop/mobile settings and run layouts passed. The
  browser check covered keyboard selection of a readable workflow label and
  restored the original draft value; no live configuration was changed.

## 2026-09-10 — CP6 collision disambiguation repair

- Added sibling-aware presentation labels so colliding team, workflow and role
  names remain separately identifiable in settings navigation, native selects,
  prompt override destinations, guided configuration and live-run controls.
  Raw option values, keys, callbacks and save actions remain unchanged; custom
  display names remain intact.
- Added regression coverage for colliding and unique labels, including the
  exact `fast__team` upgrade action and native selector values.
- Verification: 277 web tests, the production build, and four disposable
  Chromium/browser checks passed; `git diff --check` passed and no live
  configuration was changed.

- Follow-up repair disambiguates colliding `code_review`/`code__review` role
  controls in global settings, team settings and live-run controls, while
  retaining raw keys in exact save and control payloads. Verification: 279 web
  tests, production build and four disposable Chromium checks passed; no live
  configuration was changed.

## 2026-09-10 — Shared dirty-worktree preflight

- Added one typed, NUL-aware Git status preflight shared by startup and both
  lifecycle allocation checks. It preserves rename/source paths and ordered
  status items, excludes plan/lifecycle-owned dirt for fresh worktrees, and
  reports conflicts, in-progress operations, and inspection failures explicitly.
- Dirty non-plan paths now use the existing startup confirmation for eligible
  worktree runs. The acknowledgment is carried through prepared daemon and
  controller inputs; final pre-allocation validation rechecks current dirt
  without relaxing branch, identity, conflict, or teardown protections.
- Verification: focused dirty-worktree tests (8), Git-status tests (26), CLI
  tests (154 plus 125 subtests), runtime tests (276 plus 43 subtests), and
  library startup tests (33 plus 6 subtests) passed; Ruff and diff whitespace
  checks passed. No real provider or global configuration was used.

## 2026-09-10 — Live configuration without snapshot gates

- Completed the legacy recovery pass: CLI and daemon resume now use the
  relocated current configuration source, while malformed, absent, edited, or
  hash-disagreeing compatibility snapshots remain diagnostic only. Removed
  stale config-save and frozen-configuration admission contracts while
  retaining lifecycle, plan, ownership, idempotency, and security checks.
- Added synthetic CLI/daemon resume and snapshot-corruption coverage, updated
  user-facing and bundled-engine documentation, and isolated ordinary tests
  from this checkout's explicit publication grant without weakening the
  dedicated publication tests.
- Verification: 2,058 Python tests plus 219 subtests, 291 web tests, Ruff,
  web production build, wheel inspection, and browser smoke passed. No public
  push, real provider, or live configuration change was used.

## 2026-09-10 — Responsive document-scroll checkpoint 1

- Removed root/body/shell/workspace viewport locks and the detail/editor scroll
  ownership rules. Runs, Settings and plan content now grow in document flow;
  wide navigation alone retains a sticky bounded local list exception, while
  compact and short layouts stack natural-height blocks until the next
  list/detail checkpoint.
- Removed `SidebarEditorLayout` selection-time detail resets and PlanPanel
  inline height/overflow ownership. Added targeted minimum-width and wrapping
  rules without changing draft, save, history or request identities.
- Replaced independent-pane browser expectations with document wheel movement,
  positive detail dimensions, reachable final controls, natural long Settings
  and plan-list scrolling, and exact long-plan text persistence.
- Verification: 279 web tests, production build, four disposable Chromium
  browser checks and `git diff --check` passed. No live configuration or
  production run was changed.

## 2026-09-10 — Responsive list/detail checkpoint 2

- Added compact list → detail → Back presentation to the shared
  `SidebarEditorLayout` for Skills, Teams, Workflows, Prompts and Run history.
  The compact helper follows the CSS media query through a cleaned-up
  `matchMedia` listener; wide layouts retain both columns. Domain consumers
  continue to own exact selections, drafts, saves, URL state and requests.
- Compact selection captures document position and the clicked machine identity
  before opening, focuses the detail heading once, and Back restores the row or
  the labelled list surface if refresh removed it. Both surfaces stay mounted,
  with the inactive one removed from keyboard/accessibility traversal. A run URL
  opens detail explicitly; passive refresh does not reset focus or scroll, and
  resize preserves an opened detail.
- Added seven focused layout tests and extended disposable Chromium Settings and
  run journeys for compact deep links, list/detail/Back, focus, position and
  re-entry. Verification for this checkpoint: 174 targeted web tests, a
  production build, four Chromium browser checks and `git diff --check` passed.
  No live configuration, production run, publication or CI deployment was
  changed.

## 2026-09-10 — CP2 run-entry intent repair

- Separated App-owned explicit run URL/browser-navigation intent from the
  synchronized selected-run URL. Ordinary default selection therefore remains
  list-first on compact screens while direct links and browser navigation still
  open the exact run detail; existing selection, URL and request behavior is
  unchanged.
- All runs and successful launch handoffs now mark that existing explicit
  intent before moving to their exact Runs URL, so compact detail opens even
  when the dashboard was not visible or was locally backed out of.
- Added compact App and disposable Chromium regressions for fresh and repeated
  All runs selection, alongside passive default synchronization, explicit
  initial links, later browser navigation, row Back, identity and focus
  restoration.
- Verification: the CP2 subset passed 178 web tests; the production build and
  all four disposable Chromium browser checks passed; and `git diff --check`
  passed. No live configuration, production run, publication, CI, or live
  activation was changed.

## 2026-09-10 — Responsive shell checkpoint 3

- Added registered header slots so the shell owns compact two-row navigation
  while pages retain their existing action handlers and draft/request owners.
  Settings now places section navigation, Save all changes and secondary actions
  in the shared row; Skills installation is an in-flow disclosure opened from
  More instead of a permanent card.
- Compact navigation uses an accessible in-flow Menu with current context,
  existing destinations and Logout, including Escape/focus return. Settings
  uses one active native section selector on compact layouts and keyboard tabs
  on wide layouts without fixed section-count assumptions.
- Verification: the CP3 web tests, production build, disposable Settings
  Chromium suite and `git diff --check` passed. No live configuration,
  production run, publication, CI or live activation was changed.

## 2026-09-10 — CP3 repair evidence

- Kept the list/detail breakpoint unchanged while switching Settings to its
  labelled section selector before the shared header can wrap at 960–1024px.
  More → Install skills now completes the guarded Advanced TOML → Guided
  transition, preserving valid and invalid raw drafts; Hide controls only the
  disclosure while retained success/failure outcomes remain available on reopen.
- Updated compact run-navigation coverage for the shared Menu and header More
  actions. Verification: 100 targeted web tests, production build, both
  disposable Chromium modules (4 passed), and `git diff --check` passed. No
  live configuration, production run, publication, CI or live activation was
  changed.

## 2026-09-10 — CP3 hosted recovery selector correction

- Updated the hosted successor-recovery regression to exercise the shared
  More → Cancel action after the header migration; standalone dashboard Cancel
  coverage remains unchanged and exact request/owner-stop assertions remain
  intact.
- Verification: the focused recovery test, full 295-test web suite, production
  build, two disposable Chromium modules, and `git diff --check` passed. No
  live configuration, publication, CI or live activation changed.

## 2026-09-10 — Responsive editor checkpoint 4

- Added a shared native text editor for settings, skills, prompts, plans and
  connection TOML. It defaults to soft visual wrapping with a labelled
  `Wrap lines` control; disabling wrapping keeps horizontal overflow inside
  the editor, while native vertical scrolling, desktop resize and exact draft
  bytes remain owned by the existing callers.
- Reflowed profile rows into readable name/model/effort columns with compact
  stacked fields, and kept combobox suggestions bounded and lower-edge safe.
  Existing draft, conflict, partial-save, acknowledgement and Undo paths stay
  in ordinary document flow.
- Verification: the exact CP4 web suite passed 120 tests, the full web suite
  passed 297 tests, the production build passed, the Settings Chromium module
  passed 3 tests including 20,000-character Markdown/TOML and partial-save
  journeys, and `git diff --check` passed. No publication, CI, or live
  activation was performed.

## 2026-09-10 — CP4 native editor overlay repair

- Moved the shared `Wrap lines` control into normal document flow and removed
  the overlay-only textarea padding. Skills places the shared toolbar beside
  its existing title/`SKILL.md` label, leaving the native textarea as the sole
  editor scroll and hit-test surface while preserving the 44px control and
  16px editor text requirements.
- Extended the disposable Skills Chromium journey to verify 1280×720 and
  390×844 geometry, actual client editing height, toolbar/editor non-overlap,
  post-scroll hit-testing, caret movement, exact wrap-toggle bytes, saved
  content, and retained partial-save acknowledgements. Inspected the captured
  dark-theme scrolled-editor screenshot.
- Verification: full web suite (297 tests), production build, both required
  Chromium modules (4 tests), and `git diff --check` passed. Original CP4
  approval remains with the reviewer; no publication, CI, or live activation
  was performed.

## 2026-09-10 — Responsive browser acceptance checkpoint 6

- Added the disposable responsive-browser module covering 320×568, 390×844,
  768×1024, 844×390, 1280×720, 1440×900 and 390×420. It exercises the shared
  header budget, document scrolling, list/detail/Back and focus restoration,
  long plans/runs/configuration, 200% text reflow, keyboard menu behavior,
  resize-retained drafts, validation and failed-save retention, touch-sized
  primary controls, safe-area-aware sticky controls, and light/dark screenshots.
- The module rejects unsupported browser names and runs unchanged with
  `AFLOW_TEST_BROWSER=webkit`. The Ubuntu/Python 3.12 dashboard job installs
  WebKit, runs this module, and uploads its screenshot artifacts. Physical
  mobile keyboard/browser-toolbar behavior remains unverified.
- Repair revalidation persists the existing `aflow.appearance` preference
  before every screenshot navigation and asserts the loaded theme. Its test
  zoom snapshots computed visible text/control sizes before applying doubled
  inline sizes, proving the Skills editor text—not only the root—reflows at
  200%. Chromium and WebKit screenshots were regenerated and inspected.
- Repair verification: full web tests (297), production build, combined
  Chromium browser checks (13), responsive Chromium (8), responsive WebKit
  (8), and `git diff --check` passed. Original CP6 remains unapproved; no
  publication, remote CI receipt, or live activation was performed.

## 2026-09-10 — Responsive UI integration checkpoint 1

- HISTORY: Integrated approved responsive source `61d5988` with accepted main
  `5259740` using a no-commit merge. `MERGE_HEAD` remains the source commit for
  reviewer-owned approval; the source worktree and the separate issue38 work
  remain untouched.
- HISTORY: Preserved both implementation histories and retained current main's
  live configuration, dirty-worktree preflight, restart/successor controls,
  API semantics, merge context and delivery gates alongside the responsive
  header, document scrolling, list/detail navigation and native editors.
- HISTORY: The first combined backend/browser attempt exposed the fixture's
  unacknowledged dirty-worktree preflight. The acceptance test now waits for a
  loaded inspection result, checks the real confirmation control, and then
  asserts that Start is enabled; no sleep, timeout inflation or retry-only
  acceptance was added.
- Combined verification passed: web suite (309 tests), production build, full
  backend suite (2043 passed, 223 subtests), Chromium combined journeys (13),
  WebKit responsive journeys (8), Ruff, and `git diff --check`.
- Fresh Chromium and WebKit screenshots for Skills, run detail, New Run and
  Plans were inspected in light/dark and landscape states; New Run evidence
  shows the loaded preflight result rather than a loading state. Physical mobile
  keyboard/browser-toolbar behavior remains unverified. Publication, exact CI
  and live activation remain downstream delivery checks.

## HISTORY: 2026-09-10 — Combined responsive integration repair

- HISTORY: Added a rendered-plan-row readiness wait and hosted-shell browser coverage for live max-turn, team and role-selector controls, rejected drafts, list/detail navigation, resize retention, owner stop, dirty preflight and exact unresolved-successor retry.
- HISTORY: Responsive Chromium and WebKit each passed 11 tests; the combined Settings, run-navigation and responsive Chromium command passed 16 tests. Fresh light/dark live-control and restart-state captures, plus existing Skills, profile, Plans and New Run captures, were inspected. Physical keyboard/browser-toolbar behavior remains unverified.
- HISTORY: Web build and full backend verification passed (`2046` tests, `223` subtests); Ruff passed. The first parallel Vitest suite invocation had one existing RunDashboard worker-order failure at 308/309, while the exact rerun, focused file, and serial diagnostic all passed 309/309. No skip, timeout inflation, retry-only dismissal, production change or external mutation was used.
- HISTORY: This repair remains uncommitted; the earlier pending-merge state is superseded by the authorized recovery snapshot and the current verification record below. No publication, CI receipt or live activation is claimed.

## HISTORY: 2026-09-10 — Recovery snapshot acceptance follow-up

HISTORY: The authorized snapshot `5b52d3d` preserves accepted base `5259740` and responsive source `61d5988`; `.git/MERGE_HEAD` is intentionally absent and no history rewrite, checkpoint commit or second merge was performed.
HISTORY: The focused acceptance repair now waits for the hosted capabilities to expose `No team`, both colliding labels and raw `fast__team` before selecting it, and asserts successor confirmation through its rendered container and enabled action.
HISTORY: The first post-edit full backend invocation recorded a real readiness gap: native `<option>` locators were awaited as visible and all three new live-control viewports timed out, with `2043 passed`, `3 failed`, `223 subtests`. The waits were corrected to exact attachment state without delay, timeout, skip or assertion weakening.
HISTORY: The corrected combined Chromium browser command passed 16 tests, responsive WebKit passed 11 tests, the full backend command passed `2046`, `223 subtests`, the web suite passed `309`, the production build passed and Ruff passed. Physical keyboard and browser-toolbar behavior remain unverified.
HISTORY: Published clipboard history `c14f1f2`/`cd78d53` remains separate and must be preserved by coordinator-owned delivery; no publication, CI receipt or live activation is claimed.

## 2026-09-10 — Dashboard recovery transition readiness

- The CI failure was a cross-surface ordering mistake: after `Resolve pending successor`, the preserved retry control can render before the destination's effect-registered `Start run` header. The recovery test now waits for that destination button, then asserts its disabled state without changing exact request, body, key, retry, owner-stop, or existing-run assertions.
- `openNewRun` now performs one navigation action and awaits the New-run form. Removed 18 conditional second-navigation fallbacks in RunDashboard tests; App New-run handoffs already wait for their destination heading and selected-plan field, so no App or production changes were needed.
- Verification: focused App/RunDashboard tests passed (136), the normal parallel web suite passed (324), production build passed, and `git diff --check` passed. Browser/server/CI jobs were not rerun because the change is test-only; no publication or live activation occurred.

## 2026-09-10 — Preserve live mobile Skills editor

- Retained the single Skills presentation instance across Settings sections
  under native hidden semantics, preserving its open/list state and wrap choice
  without moving draft or save ownership out of `GlobalSettings`.
- Shared compact Back with the Skills title/status heading. The full Back name
  and 44px touch target remain, while Chromium and WebKit both keep a dirty
  390×844 editor at or above the required geometry with no horizontal overflow.
- Added disposable dirty `aflow-plan` Changelog round-trip coverage, including
  hidden-focus/scroll rejection, Back/list retention, light/dark screenshots,
  all required viewports, enlarged text, and read-only Changelog paging.
- Verification: web suite (325), production build, Chromium browser journeys
  (16), WebKit browser journeys (16), and `git diff --check` passed. Physical
  mobile keyboard behavior, live CI, activation, and the coordinator's live
  unsaved-draft smoke remain downstream checks.

## 2026-09-10 — Registered worktree project presentation (Checkpoint 2)

- Grouped only read-projected registered worktrees beneath their registered
  primary project in a collapsed, keyboard-accessible Worktrees disclosure.
  Selected and search-matching children reveal automatically; ungroupable
  registrations remain independent, and child selection/actions retain exact
  project IDs and paths.
- Added parent/worktree context to the app header and global run rows without
  changing URL identity or flattening global run enumeration. Documented the
  normal internal-checkout launch path and legacy registration recovery.
- Verification is recorded with the checkpoint handoff after web unit tests,
  production build, Chromium/WebKit responsive journeys, and `git diff --check`.

## 2026-09-10 — Dashboard control-refresh readiness

- The named `RunDashboard.test.tsx` saved-options regression now controls the
  initial run list, capabilities, and selected-run detail independently. Its
  observed sequence is exact: with list/capabilities pending the max-turns
  control is absent; after the list settles but capabilities remain pending it
  is still absent; after capabilities settle while detail remains pending the
  selected `run-owned` control is enabled at baseline `8`; after detail settles
  it remains `8`; the real change handler then sets draft `17`.
- On visibility restoration the test waits for rendered `saved-team` and
  `harness/saved` options before confirming exact draft `17` and `run-owned`
  remain. The controlled ordering did not reproduce the historical `17` to `8`
  failure, so it establishes safe preconditions and post-readiness preservation,
  not a causal explanation of that historical incident. Production control
  ownership is unchanged.
- Verification passed for focused RunDashboard (85 tests), the normal parallel
  web suite (331 tests), the production build, and `git diff --check`.
  Backend/browser evidence is reused; CI, publication, activation, and
  physical-device behavior remain downstream.

## 2026-09-10 — Canonical CLI isolation fixture paths

- Canonicalized the existing `test_cli_workflow_override` temporary home before
  deriving its plan path. The exact run ownership, run-count, workflow and
  durable plan-path assertions remain unchanged; this aligns the fixture with
  macOS's `/private/var` resolution of its `/var` temporary-directory alias.
- The independent disposable proof ran from an outer Git directory with
  `TMPDIR` set to a symlink, using the checkout's `.venv/bin/python` and the
  absolute `tests/test_cli.py` path. `tempfile` used the symlink alias, the
  focused test passed, and the outer caller remained free of `.aflow`.
- Verification: focused CLI test passed (`1 passed, 156 deselected`); full CLI
  module passed (`157 passed, 129 subtests`); no production changes were made.

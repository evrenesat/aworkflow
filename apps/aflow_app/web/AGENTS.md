# Web client

- `GlobalSettings` owns the cross-tab draft; `settingsDraft` emits net changes.
  Keep one save coordinator, preserve conflicts and accurately clear only
  acknowledged persistence domains. Browser preferences never enter server writes.
  Skills drafts and baselines live in `GlobalSettings` indexed by exact skill
  name (`SkillsSettings` only renders); they join dirty/navigation guards, never
  enter TOML, stay usable without the config projection, and save after workflow
  config in sorted name order with per-skill acknowledgement. Install runs the
  shared installer once, stays disabled while any skill is dirty, and reloads
  clean baselines afterwards.
  New teams are created in the draft by the Add team form (`add_team` actions
  precede dependent edits); `set_team_upgrade` edits only the team's own link.
- Keep destructive prompt actions secondary: referenced prompts offer no
  deletion and disclose usages; unreferenced ones delete only through the
  More menu with confirm/Undo, and the server stays authoritative.
- Launch selectors show the resolved default with an indicator before focus;
  an empty field means "follow default" and stays an omission in requests.
  Diagnostics opens with one click (no acknowledgement) and discards stale
  context responses on run change or panel close.
- `GlobalRunOverview` aggregates authorized project run pages without per-row
  streams. Hidden dashboards preserve drafts but suspend subscriptions and clocks.
- Preserve exact project/run identities and unresolved mutation keys across
  navigation. Restart source identity is independent of the selected history row.
- Format snake_case machine identifiers only at user-facing label boundaries with
  the shared label helper. Keep raw keys in values, callbacks, API requests,
  URLs, DOM identity and technical/raw details; retain custom display names and
  explicitly supplied acronym casing.
- Verify changes with `npm --prefix apps/aflow_app/web test -- --run` and
  `npm --prefix apps/aflow_app/web run build` from the repository root.

- Run context has one loader; status refresh must not overwrite detailed context with Lite. One page Refresh coordinates list/status/events/context and retains valid partial results. Diagnostics summary uses structured fields; Raw details stays collapsed until requested.
- Prompt-deletion recovery belongs to GlobalSettings, survives tab/editor navigation, and clears only after acknowledged configuration save or explicit discard. Keep the recent-count editor only in General and commit its separate input draft on blur/Enter.

- Follow [UI_GUIDELINES.md](../../../UI_GUIDELINES.md): two compact desktop
  header rows, mobile hamburger navigation and list → detail → Back, document
  scrolling, and outcome-based browser checks. Checkpoints 1–2 now implement
  the document-scroll and list/detail ownership boundary: the shell, workspace,
  editor details and settings fields flow with the document; only the named wide
  navigation, option-list and raw-payload exceptions may scroll locally. The
  shared SidebarEditorLayout keeps inactive surfaces mounted under actual
  `hidden` semantics, captures list position/item identity before compact
  selection, and restores focus on Back. Consumers still own exact selections,
  drafts, saves, and run URL state; later checkpoints own shell compaction and
  the remaining evidence. Preserve those owners while migrating presentation.

- Keep history mutation keys by exact project/run/action until acknowledged or
  definitively rejected (including acknowledgement-required validation). Deletion tombstones suppress late rows and
  close streams; archive retains opened details while removing default-list rows.

- Profile forms and detail content belong in document flow; do not introduce
  panel/card scrollers. Keep only the explicit local-scroll exceptions in the
  UI guidelines, and include Agents & Roles in real-browser coverage.

- Refresh the full loaded history page range, not just its first page. Keep
  the next cursor from the refreshed range and discard superseded responses.

# Web client

- `GlobalSettings` owns the cross-tab draft; `settingsDraft` emits net changes.
  Keep one save coordinator, preserve conflicts and accurately clear only
  acknowledged persistence domains. Browser preferences never enter server writes.
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
- Verify changes with `npm --prefix apps/aflow_app/web test -- --run` and
  `npm --prefix apps/aflow_app/web run build` from the repository root.

- Run context has one loader; status refresh must not overwrite detailed context with Lite. One page Refresh coordinates list/status/events/context and retains valid partial results. Diagnostics summary uses structured fields; Raw details stays collapsed until requested.
- Prompt-deletion recovery belongs to GlobalSettings, survives tab/editor navigation, and clears only after acknowledged configuration save or explicit discard. Keep the recent-count editor only in General and commit its separate input draft on blur/Enter.

- Keep `.global-settings` non-shrinking: sticky containment must span the complete form. Verify long-page toolbar visibility with the real Chromium regression in server tests after building the web client; DOM-only tests cannot establish sticky positioning.

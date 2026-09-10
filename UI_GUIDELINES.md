# AFlow UI and interaction guidelines

## Status and scope

Owner-approved design requirements, 2026-09-10, for the entire application.
These are acceptance rules for future changes, not a claim that the current UI
already satisfies them. The responsive document-scrolling plan implements the
migration. Preserve existing domain behavior while replacing the old layout.

## Spend space on the task

- On desktop, fit persistent app navigation, page context, section navigation,
  and primary actions into **two compact header rows**, approximately 56 CSS px
  each (112px combined at default text size). Content begins directly below,
  within 128px of the viewport top at 1280×720 and 1440×900.
- Row one: app identity, project context, global destinations, account action.
  Row two: concise page context, local sections/filters, primary action, More.
  Do not repeat a large page title or toolbar below these rows.
- Keep frequent navigation and the page's main action visible. Put Reload,
  Advanced/raw modes, installation/maintenance, and long help in labelled menus
  or disclosures. Skills installation must not occupy a permanent content card
  or a separate toolbar row. Relevant failures remain visible and actionable.
- Use the same shell/action slots on every page. Pages supply context/actions;
  they do not invent additional persistent toolbar stacks or parallel save logic.
- When controls do not fit, use a labelled section selector and secondary-action
  menu before wrapping into a third row. Preserve readable labels, full identity
  access, and touch targets. At enlarged text sizes, allow natural flow or the
  compact presentation rather than clipping to enforce a pixel budget.

## Small and short screens

- Full functionality remains available on mobile, including editing and launch.
- Use an accessible hamburger Menu for global navigation. Show the current page
  and primary action; local sections may use a labelled selector or menu. Avoid
  several adjacent hamburger icons with ambiguous ownership.
- Menus expose expanded state, have meaningful accessible names, work with
  keyboard/touch, close with Escape, and restore trigger focus. Prefer in-flow
  expansion; do not add body locking or a new modal navigation framework.
- At width below 960px or height below 600px, use list → detail → Back rather
  than stacking independently scrolling panes. Back restores list position;
  drafts, selection, and pending operations survive navigation and resizing.
- Inputs remain at least 16px; primary touch targets remain at least 44px high.
  Compactness comes from hierarchy and spacing, not smaller text or hit areas.

## Scrolling and editing

- The document is the primary vertical scroll owner on every route. No fixed
  body/root height, global overflow suppression, scrolling detail wrappers,
  profile-card scrollboxes, or viewport-height list above a phone editor.
- Allowed local scrolling: native textareas, bounded option/menu popovers,
  explicitly expanded raw data, and a long desktop navigation sidebar. Each
  exception must have a purpose and must not trap ordinary page scrolling.
- At most one action row may stick on tall screens; reserve its space and
  protect focused controls. Below 600px height use static controls. Account for
  keyboard-reduced space, browser toolbar changes, and safe-area insets.
- Wrap prose and long labels. Editor soft wrapping must not change stored bytes.
  Unwrapped code/raw data may scroll horizontally only within its own control.
- Keep one draft/save owner. Preserve conflicts, partial-save acknowledgement,
  Undo, dirty navigation guards, exact request identities, and passive-refresh
  scroll/focus stability. Presentation changes do not justify domain changes.

## Required evidence

- Test loaded task states at 320×568, 390×844, 768×1024, 844×390, 1280×720,
  1440×900, and a reduced 390×420 viewport. Include text enlargement, long labels,
  long editor content, expanded menus, failures, and keyboard focus.
- On desktop at default text size with help closed, persistent chrome is at most
  112px and main content starts by y=128. An opened Skills textarea starts by
  y=208. On 390×844, its top is at most y=280 with at least 280px initially
  visible editable height. Enlarged text/errors flow; never hide them to pass.
- Verify document movement, positive usable editor dimensions, last-action
  reachability, hit-testing, list/detail/Back, and draft retention after resize
  or failed save. Absence of horizontal overflow alone proves very little.
- Use real Chromium/WebKit journeys and inspect light/dark screenshots. Report
  physical mobile keyboard checks separately from emulation. Tests must verify
  user outcomes, not enshrine nested scroll panes as the desired implementation.
- A visual change is incomplete when it regresses these requirements. Document
  any owner-approved exception explicitly; do not silently weaken the rules.

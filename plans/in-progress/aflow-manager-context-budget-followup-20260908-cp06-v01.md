# Fix CP6 ambiguous role-assignment controls

Status: Resolved and reviewer-approved through `cp6 v02` on 2026-09-10. Non-checkpoint repair overlay for original CP6 only.

## Git Tracking

- Plan Branch: `codex/aflow-dogfood-20260909`
- Review Base HEAD: `754331bfbfd1937ba1452c9c9e0d42ef19edd386`
- Original Plan: `plans/in-progress/aflow-manager-context-budget-followup-20260908.md`

## Objective and confirmed finding

Finish CP6 collision disambiguation without changing role identities. Preserve all compatible uncommitted CP6 implementation, completed team/workflow/native-option repairs, CP1–CP5 approvals and the original pre-handoff base.

P2, high confidence: `GlobalSettings.tsx:546` and `:567` format role-assignment control labels with `formatMachineLabel` alone. Supported roles `code_review` and `code__review`, both assigned the same profile, produce two indistinguishable “Role Code review” controls under Global roles and two “Code review” controls under Teams. A disposable component reproduction confirmed both pairs and absence of raw-role text in the global editor. The controls mutate distinct raw roles, so a user can edit the wrong assignment. `aflow/config.py:_parse_role_map` accepts these distinct keys. The same root cause appears in `RunDashboard.tsx:1893–1896` for live role-selector labels. This is a remaining regression from CP6, not a request to broaden collision handling into unrelated read-only summaries.

## Implementation sequence

1. Confirm hostname, checkout, branch and dirty state. Read root `AGENTS.md`, web `AGENTS.md`, the original CP6 section and `git diff -- apps/aflow_app/web/src`. Preserve all current compatible work. Read `label.ts`, the GlobalSettings global/team role controls and RunDashboard `roleChoices` controls.
2. Reuse `formatMachineChoice` for each global role control label, comparing against `Object.keys(draft.roles)`. For the selected team's controls, compare against the existing union of global and team-specific role keys. Keep any prefix such as “Role” outside the helper. Unique labels remain compact.
3. Use the same sibling-aware role label for visible wording and `aria-label` of live run role-selector controls, comparing against `roleChoices`. Preserve raw keys, option values, callbacks, state and payloads. Do not format profile selectors or model/provider names.
4. Extend `GlobalSettings.test.tsx` with `code_review` and `code__review` initially assigned the same profile plus a second selectable profile. Assert distinct visible/accessibility labels for global and team controls, change the intended colliding role, save, and assert only that exact raw role key changes. Include a team-only role in the union coverage and verify unique roles gain no raw suffix.
5. Extend `RunDashboard.test.tsx` with colliding admitted role keys. Assert distinct controls and the exact raw role key in the mutation payload. Retain all existing collision tests and compatible behavior; no new component abstraction or production API change is needed.
6. Run the verification below. After it passes, mark only the original CP6 collision step complete (leave checkpoint approval to the reviewer). Append a concise repair/verification entry to `DEVLOG.md` without compacting it. Leave implementation and approval commits to the reviewer.

## Verification

From the assigned checkout:

```sh
npm --prefix apps/aflow_app/web test -- --run
npm --prefix apps/aflow_app/web run build
```

After the build, run disposable Chromium layout/keyboard checks:

```sh
review_root=$(mktemp -d /tmp/aflow-cp6-role-browser.XXXXXX)
mkdir -p "$review_root/.cache"
ln -s /root/.cache/ms-playwright "$review_root/.cache/ms-playwright"
HOME="$review_root" XDG_CONFIG_HOME="$review_root/.config" PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright UV_CACHE_DIR=/root/.cache/uv uv run pytest -q apps/aflow_app/server/tests/test_settings_browser.py apps/aflow_app/server/tests/test_run_navigation_browser.py
git diff --check
git status --short
git diff --stat
```

Expected: all web tests, build and four browser checks pass; colliding assignment controls are distinguishable and save the exact intended role key. Existing team/workflow/prompt collisions remain fixed.

## Boundaries

No live service, real global configuration, installed skills, runtime/controller changes, public push or history rewrite. Preserve the completed CP3 cp04-v01 overlay; it belongs to another checkpoint. Prefix every displayed historical evidence line with `HISTORY:` and inspect only bounded relevant evidence. This plan supersedes the CP6 cp01-v01 repair overlay.

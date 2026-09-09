# Bundled plan templates

- Templates ship as package data and are loaded through importlib.resources.
- Keep NO-OP instructions short, model-independent, and confined to the copied
  plan, external fixture state, and normal workflow-owned Git bookkeeping.
- The NO-OP template uses literal {{STATE_DIR}} and {{CHECKPOINTS}} tokens.
- `draft-plan.md` is the editable new-draft skeleton served when a plan create
  request omits content (or passes explicit null). It is a starting point to
  edit, not a fixture: it carries no playbook tokens and introduces no
  readiness validator or placeholder blocking — saving, promoting, and
  launching it follow the existing unchanged rules.

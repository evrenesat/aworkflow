# Bundled plan templates

- Templates ship as package data and are loaded through importlib.resources.
- Keep NO-OP instructions short, model-independent, and confined to the copied
  plan, external fixture state, and normal workflow-owned Git bookkeeping.
- The NO-OP template uses literal {{STATE_DIR}} and {{CHECKPOINTS}} tokens.

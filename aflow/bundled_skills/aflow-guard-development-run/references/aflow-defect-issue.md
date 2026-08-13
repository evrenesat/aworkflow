# Sanitized AFlow defect issues

File only confirmed AFlow engine defects in `evrenesat/aworkflow`.
Project implementation and test failures stay in the initiating task.

Require two agreeing evidence sources, or one deterministic generic
reproduction, before filing. Examples include durable state plus process state,
REST plus MCP disagreement, or a disposable fixture reproducing the behavior.

Use this issue structure:

- title: short engine behavior, without project identity;
- fingerprint marker for deduplication;
- AFlow version or commit;
- expected and actual engine behavior;
- user-visible impact;
- generic minimal reproduction;
- bounded redacted evidence; and
- workaround status, including none.

Before network access, reject content containing credentials, private keys,
authorization headers, absolute user paths, private URLs, project/repository
names, plan contents, prompts, branch names, user data, or supplied redaction
terms. Never attach raw logs or transcripts.

Run the helper with sanitized JSON on stdin:

```bash
python3 <skill-dir>/scripts/aflow_guard_issue.py --dry-run < issue.json
python3 <skill-dir>/scripts/aflow_guard_issue.py --create < issue.json
```

Use `--dry-run` to validate and preview. Use `--create` only
after the defect is confirmed. The helper searches open and closed issues before
creation and returns an existing issue when the fingerprint already exists.

If validation, GitHub authentication, or issue creation fails, report the block
in the initiating task. Do not create a local defect report.

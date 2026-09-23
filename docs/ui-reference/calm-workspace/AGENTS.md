# Calm workspace fidelity reference

This directory owns the immutable, owner-approved calm-workspace demo used by
the UI fidelity checkpoints.

- `demo.html` is a literal reference fragment. Its bytes and recorded SHA-256
  must not change without a newly approved reference and coordinator review.
- Render the fragment directly in a local browser document. Compare only
  `#af-frame`; exclude `.af-demo-bar`, `.af-footer`, and host conversation
  spacing. The fragment's simulated records and controls are not production
  features and must never be imported by runtime code.
- `fixtures.json`, `measurements.json`, and `manifest.json` are versioned
  harness inputs. Browser screenshots and capture logs belong in disposable
  pytest artifacts, not this directory.
- The fidelity test may capture the real built application against a disposable
  authenticated fixture server. It must not start, stop, or mutate a live run.
- Only the owner/coordinator may replace the reference or update its checksum;
  preserve the existing UI guideline source and record intentional differences
  separately from implementation defects.

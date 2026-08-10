#!/usr/bin/env bash
set -euo pipefail

# Emergency containment intentionally leaves releases, durable runs, manifests,
# worktrees, plans, and /etc/aflowd secrets untouched for later recovery.
systemctl stop aflowd.service || true
systemctl disable aflowd.service || true
printf 'aflowd stopped and disabled; no durable AFlow data or secrets were removed\n'

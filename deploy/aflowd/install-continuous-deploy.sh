#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
release_root=/opt/aflowd
unit_dir=/etc/systemd/system
apply=0

usage() {
  cat <<'USAGE'
Usage: install-continuous-deploy.sh [--root PATH] [--unit-dir PATH] [--apply|--dry-run]

Installs exactly aflowd-deploy.service and aflowd-deploy.timer, then enables
aflowd-deploy.timer. It is dry-run by default; --apply is required before any
change. The script never restarts aflowd.service, never deploys a release, and
never touches the registry, project roots, or Tailscale.

  --root PATH     Release state root owning the current release (default /opt/aflowd)
  --unit-dir PATH Destination directory for the two unit files (default /etc/systemd/system)
  --apply         Install the two units and enable aflowd-deploy.timer
  --dry-run       Print the plan and do not mutate (default)

Disable polling again with:
  sudo systemctl disable --now aflowd-deploy.timer

Reattempt a candidate that previously failed rollout:
  sudo /opt/aflowd/current/src/deploy/aflowd/continuous-deploy.py --retry-failed
USAGE
}

fail() {
  printf 'aflowd continuous-deploy install failed: %s\n' "$1" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --root) release_root=$2; shift 2 ;;
    --unit-dir) unit_dir=$2; shift 2 ;;
    --apply) apply=1; shift ;;
    --dry-run) apply=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$release_root" == /* && "$unit_dir" == /* ]] || fail "paths must be absolute"
for command in python3 git systemctl; do
  command -v "$command" >/dev/null || fail "$command is unavailable"
done

current_release=""
if [[ -L "$release_root/current" ]]; then
  current_release=$(realpath -e -- "$release_root/current") || fail "current release link is broken"
else
  fail "current release link is missing; install a release first"
fi
release_id=$(basename -- "$current_release")
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || fail "current release identity is invalid"
manifest="$current_release/release-manifest.sha256"
[[ -f "$manifest" && ! -L "$manifest" ]] || fail "current release manifest is unavailable"
grep -Fxq "source_commit=$release_id" "$manifest" || fail "current release manifest is stale"
tail -n +2 "$manifest" | (cd "$current_release" && sha256sum --check --status) || fail "current release snapshot is stale"

runtime_script="$current_release/src/deploy/aflowd/continuous-deploy.py"
[[ -f "$runtime_script" && ! -L "$runtime_script" ]] || fail "continuous-deploy runtime script is missing from the current release"
python3 - "$runtime_script" <<'PY' || fail "continuous-deploy runtime script does not parse"
import ast
import sys
try:
    ast.parse(open(sys.argv[1], encoding="utf-8").read())
except (OSError, SyntaxError, UnicodeDecodeError):
    raise SystemExit(1)
PY
for unit in aflowd-deploy.service aflowd-deploy.timer; do
  [[ -f "$script_dir/$unit" && ! -L "$script_dir/$unit" ]] || fail "$unit is missing from this checkout"
done

printf 'runtime script: %s\n' "$runtime_script"
printf 'service unit: %s -> %s\n' "$script_dir/aflowd-deploy.service" "$unit_dir/aflowd-deploy.service"
printf 'timer unit: %s -> %s\n' "$script_dir/aflowd-deploy.timer" "$unit_dir/aflowd-deploy.timer"
printf 'after install: systemctl daemon-reload && systemctl enable --now aflowd-deploy.timer\n'

if (( ! apply )); then
  printf 'dry-run: no units were installed and no timers were changed\n'
  exit 0
fi

install -D -m 0644 -- "$script_dir/aflowd-deploy.service" "$unit_dir/aflowd-deploy.service"
install -D -m 0644 -- "$script_dir/aflowd-deploy.timer" "$unit_dir/aflowd-deploy.timer"
systemctl daemon-reload
systemctl enable --now aflowd-deploy.timer
printf 'aflowd-deploy.timer is enabled; polls follow OnBootSec=2min and OnUnitInactiveSec=5min\n'
printf 'disable with: sudo systemctl disable --now aflowd-deploy.timer\n'

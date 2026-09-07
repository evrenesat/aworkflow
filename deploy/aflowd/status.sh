#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
state_root=/opt/aflowd
service_name=aflowd.service
tailscale_bin=tailscale
deploy_timer_name=aflowd-deploy.timer
deploy_status_path=/var/lib/aflowd/deploy/status.json

usage() {
  printf 'Usage: status.sh [--root PATH] [--service NAME] [--tailscale PATH] [--deploy-timer NAME] [--deploy-status-path PATH]\n'
}
while (($#)); do
  case "$1" in
    --root) state_root=$2; shift 2 ;;
    --service) service_name=$2; shift 2 ;;
    --tailscale) tailscale_bin=$2; shift 2 ;;
    --deploy-timer) deploy_timer_name=$2; shift 2 ;;
    --deploy-status-path) deploy_status_path=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ ! -L "$state_root/current" ]]; then
  printf 'current release: none\n' >&2
  exit 1
fi
current_release=$(realpath -e -- "$state_root/current")
release_id=$(basename -- "$current_release")
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || { printf 'current release identity is invalid\n' >&2; exit 1; }
manifest="$current_release/release-manifest.sha256"
[[ -f "$manifest" && ! -L "$manifest" ]] || { printf 'current release manifest is unavailable\n' >&2; exit 1; }
grep -Fxq "source_commit=$release_id" "$manifest" || { printf 'current release manifest is stale\n' >&2; exit 1; }
tail -n +2 "$manifest" | (cd "$current_release" && sha256sum --check --status) || { printf 'current release snapshot is stale\n' >&2; exit 1; }
printf 'current release: %s\n' "$current_release"

deploy_timer_state=$(systemctl is-enabled "$deploy_timer_name" 2>/dev/null || true)
if [[ -n "$deploy_timer_state" ]]; then
  deploy_timer_active=$(systemctl is-active "$deploy_timer_name" 2>/dev/null || true)
  printf 'deployment timer: %s/%s\n' "$deploy_timer_state" "${deploy_timer_active:-unknown}"
else
  printf 'deployment timer: not installed\n'
fi
if [[ ! -f "$deploy_status_path" ]]; then
  printf 'deployment status: none\n'
elif deployment_line=$(python3 - "$deploy_status_path" <<'PY'
import json
import sys
from pathlib import Path
try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    phase = payload.get("phase")
    reason = payload.get("reason")
    if not isinstance(phase, str) or not isinstance(reason, str) or phase not in {
        "checking", "up_to_date", "waiting_for_ci", "deferred", "deploying", "deployed", "failed"
    }:
        raise ValueError("invalid deployment status")
    fields = [f"deployment status: phase={phase}", f"reason={reason}"]
    for key in ("current_commit", "candidate_commit", "checked_at"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            fields.append(f"{key}={value}")
    print(" ".join(fields))
except Exception:
    raise SystemExit(1)
PY
); then
  printf '%s\n' "$deployment_line"
else
  printf 'deployment status: unreadable\n'
fi

systemctl status --no-pager --lines=20 "$service_name"

socket_status=$(ss -ltnp '( sport = :8765 )')
printf '%s\n' "$socket_status"
printf '%s\n' "$socket_status" | grep -Eq '127\.0\.0\.1:8765([[:space:]]|$)' || { printf 'aflowd is not listening on loopback port 8765\n' >&2; exit 1; }
if printf '%s\n' "$socket_status" | grep -Eq '100\.[0-9]+\.[0-9]+\.[0-9]+:8765'; then
  printf 'aflowd must not listen directly on a Tailscale IPv4 address\n' >&2
  exit 1
fi
serve_status=$(mktemp /tmp/aflowd-serve-status.XXXXXX)
tailscale_status=$(mktemp /tmp/aflowd-tailscale-status.XXXXXX)
trap 'rm -f -- "$serve_status" "$tailscale_status"' EXIT
"$tailscale_bin" serve status --json >"$serve_status"
"$tailscale_bin" status --json >"$tailscale_status"
private_url=$(python3 "$script_dir/validate-serve-status.py" "$serve_status" "$tailscale_status")
printf 'private URL: %s\n' "$private_url"

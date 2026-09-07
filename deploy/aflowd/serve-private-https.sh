#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
snapshot=""
expected_current=""
evidence_dir=""
tailscale_bin=tailscale
apply=0
rollback=0

usage() {
  cat <<'USAGE'
Usage: serve-private-https.sh --snapshot FILE [--evidence-dir PATH] \
  [--tailscale PATH] [--apply]
       serve-private-https.sh --rollback --snapshot FILE \
  --expected-current FILE [--tailscale PATH] [--apply]

Apply adds private HTTPS 443 to 127.0.0.1:8765 and records the exact all-service
post-change config. Rollback first proves the current all-service config still
matches that post-change snapshot, then restores the pre-change snapshot. Drift
blocks rollback, preserving unrelated mappings. This command never resets Serve
or enables Funnel. Dry-run is the default.
USAGE
}

fail() {
  printf 'aflowd private Serve failed: %s\n' "$1" >&2
  exit 1
}

json_equal() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
assert json.load(open(sys.argv[1], encoding="utf-8")) == json.load(open(sys.argv[2], encoding="utf-8"))
PY
}

while (($#)); do
  case "$1" in
    --snapshot) snapshot=$2; shift 2 ;;
    --expected-current) expected_current=$2; shift 2 ;;
    --evidence-dir) evidence_dir=$2; shift 2 ;;
    --tailscale) tailscale_bin=$2; shift 2 ;;
    --apply) apply=1; shift ;;
    --rollback) rollback=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
[[ -n "$snapshot" && "$snapshot" == /* ]] || fail "--snapshot must be an absolute path"
[[ -f "$snapshot" && ! -L "$snapshot" ]] || fail "Serve snapshot must be a regular file"
python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$snapshot" >/dev/null || fail "Serve snapshot must be valid JSON"
command -v "$tailscale_bin" >/dev/null || fail "tailscale CLI is unavailable"
if (( rollback )); then
  [[ -n "$expected_current" && "$expected_current" == /* && -f "$expected_current" && ! -L "$expected_current" ]] || fail "--expected-current must name the post-change all-service snapshot"
  printf 'Serve rollback: compare current with %s, then %s serve set-config --all %s\n' "$expected_current" "$tailscale_bin" "$snapshot"
else
  printf 'Serve update: %s serve --bg --https=443 127.0.0.1:8765\n' "$tailscale_bin"
fi
if (( ! apply )); then
  printf 'dry-run: Tailscale Serve was not changed\n'
  exit 0
fi

if (( rollback )); then
  current=$(mktemp /tmp/aflowd-serve-current.XXXXXX)
  restored=$(mktemp /tmp/aflowd-serve-restored.XXXXXX)
  trap 'rm -f -- "$current" "$restored"' EXIT
  "$tailscale_bin" serve get-config --all >"$current"
  json_equal "$expected_current" "$current" || fail "Serve config drifted after aflowd update; refusing all-config restore"
  "$tailscale_bin" serve set-config --all "$snapshot"
  "$tailscale_bin" serve get-config --all >"$restored"
  json_equal "$snapshot" "$restored" || fail "Serve rollback did not restore the snapshot"
  printf 'Tailscale Serve snapshot restored\n'
  exit 0
fi

[[ -n "$evidence_dir" && "$evidence_dir" == /* && ! -e "$evidence_dir" ]] || fail "--evidence-dir must be a new absolute path"
mkdir -m 0700 -- "$evidence_dir"
post_snapshot="$evidence_dir/tailscale-serve.after-config.json"
rollback_snapshot() {
  status=$?
  trap - EXIT
  if (( status != 0 )) && [[ -f "$post_snapshot" ]]; then
    current=$(mktemp /tmp/aflowd-serve-current.XXXXXX)
    if "$tailscale_bin" serve get-config --all >"$current" && json_equal "$post_snapshot" "$current"; then
      "$tailscale_bin" serve set-config --all "$snapshot" || true
      printf 'Tailscale Serve update failed; unchanged post-config restored to snapshot\n' >&2
    else
      printf 'Tailscale Serve update failed and config drifted; snapshot was not restored\n' >&2
    fi
    rm -f -- "$current"
  fi
  exit "$status"
}
trap rollback_snapshot EXIT
"$tailscale_bin" serve --bg --https=443 127.0.0.1:8765
"$tailscale_bin" serve get-config --all >"$post_snapshot"
"$tailscale_bin" serve status --json >"$evidence_dir/tailscale-serve.after.json"
"$tailscale_bin" status --json >"$evidence_dir/tailscale-status.after.json"
health_url=$(python3 "$script_dir/validate-serve-status.py" "$evidence_dir/tailscale-serve.after.json" "$evidence_dir/tailscale-status.after.json")
printf '%s\n' "$health_url" >"$evidence_dir/private-url.txt"
chmod 0600 -- "$evidence_dir"/*
trap - EXIT
printf 'private HTTPS URL: %s\n' "$health_url"

#!/usr/bin/env bash
set -euo pipefail

state_root=/opt/aflowd
service_path=/etc/systemd/system/aflowd.service
release_id=""
service_snapshot=""

usage() {
  printf 'Usage: rollback.sh --release COMMIT --service-snapshot PATH [--root PATH] [--service-path PATH]\n'
}

fail() {
  printf 'aflowd rollback failed: %s\n' "$1" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --release) release_id=$2; shift 2 ;;
    --service-snapshot) service_snapshot=$2; shift 2 ;;
    --root) state_root=$2; shift 2 ;;
    --service-path) service_path=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 2; }
[[ -n "$service_snapshot" ]] || { usage >&2; exit 2; }
release_dir="$state_root/releases/$release_id"
[[ -d "$release_dir" && ! -L "$release_dir" ]] || fail "selected release is unavailable"
[[ -x "$release_dir/bin/aflow-app-server" ]] || fail "selected release executable is unavailable"
[[ -f "$release_dir/config/config.toml" && ! -L "$release_dir/config/config.toml" ]] || fail "selected release config is unavailable"
manifest="$release_dir/release-manifest.sha256"
[[ -f "$manifest" && ! -L "$manifest" ]] || fail "selected release manifest is unavailable"
grep -Fxq "source_commit=$release_id" "$manifest" || fail "selected release identity does not match"
tail -n +2 "$manifest" | (cd "$release_dir" && sha256sum --check --status) || fail "selected release manifest validation failed"
[[ -f "$service_path" && ! -L "$service_path" ]] || fail "active service unit must be a regular file"
[[ -f "$service_snapshot" && ! -L "$service_snapshot" ]] || fail "service snapshot must be a regular file"

previous_target=$(realpath -e -- "$state_root/current") || fail "current release link is broken"
[[ -d "$previous_target" && ! -L "$previous_target" ]] || fail "current release target is unavailable"
temporary_current="$state_root/.current.$release_id.rollback"
previous_service=$(mktemp /tmp/aflowd.rollback.service.previous.XXXXXX)
pending_service=""
cp -- "$service_path" "$previous_service"

cleanup() {
  rm -f -- "$temporary_current" "$previous_service"
  [[ -z "$pending_service" ]] || rm -f -- "$pending_service"
}

validate_service_release() {
  python3 - "$1" "$2" "$state_root/releases" <<'PY'
import re
import shlex
import sys
from pathlib import Path

unit_path = Path(sys.argv[1])
release = sys.argv[2]
release_root = sys.argv[3]
try:
    lines = unit_path.read_text(encoding="utf-8").splitlines()
except (OSError, UnicodeError) as exc:
    raise SystemExit(f"service unit is unreadable: {type(exc).__name__}") from exc

def values(name: str) -> list[str]:
    prefix = name + "="
    return [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]

working_directories = values("WorkingDirectory")
exec_starts = values("ExecStart")
exec_start_pres = values("ExecStartPre")
if working_directories != [release]:
    raise SystemExit("service unit WorkingDirectory is not pinned to selected release")
if len(exec_starts) != 1 or f"{release}/bin/aflow-app-server" not in shlex.split(exec_starts[0]):
    raise SystemExit("service unit executable is not pinned to selected release")
if len(exec_start_pres) != 1:
    raise SystemExit("service unit validator is missing or ambiguous")
validator_args = shlex.split(exec_start_pres[0])
if f"{release}/src/deploy/aflowd/validate-runtime.sh" not in validator_args:
    raise SystemExit("service unit validator is not pinned to selected release")
for option, expected in (
    ("--release", release),
    ("--config", f"{release}/config/config.toml"),
):
    if validator_args.count(option) != 1:
        raise SystemExit(f"service unit validator {option} is missing or ambiguous")
    option_index = validator_args.index(option)
    if option_index + 1 == len(validator_args) or validator_args[option_index + 1] != expected:
        raise SystemExit(f"service unit validator {option} is not pinned to selected release")

release_pattern = re.compile(re.escape(release_root) + r"/[0-9a-f]{40}")
references = set(release_pattern.findall("\n".join(lines)))
if references != {release}:
    raise SystemExit("service unit contains mixed or missing immutable release references")
PY
}

validate_service_release "$service_path" "$previous_target" || fail "active service is not pinned to current release"
validate_service_release "$service_snapshot" "$release_dir" || fail "service snapshot is not pinned to selected release"

atomic_replace_service() {
  source=$1
  service_parent=$(dirname -- "$service_path")
  pending_service=$(mktemp "$service_parent/.aflowd.service.rollback.XXXXXX") || return 1
  if ! install -m 0644 -- "$source" "$pending_service"; then
    return 1
  fi
  if ! mv -Tf -- "$pending_service" "$service_path"; then
    return 1
  fi
  pending_service=""
}

restore_previous() {
  restore_status=0
  atomic_replace_service "$previous_service" || restore_status=1
  rm -f -- "$temporary_current"
  if ! ln -s -- "$previous_target" "$temporary_current" || ! mv -Tf -- "$temporary_current" "$state_root/current"; then
    restore_status=1
  fi
  systemctl daemon-reload || restore_status=1
  systemctl restart aflowd.service || restore_status=1
  return "$restore_status"
}

rollback_started=0
rollback_on_exit() {
  status=$?
  trap - EXIT
  if (( rollback_started )); then
    set +e
    if restore_previous; then
      printf 'aflowd rollback failed; restored %s\n' "$previous_target" >&2
    else
      printf 'aflowd rollback failed; restoration of %s also failed\n' "$previous_target" >&2
    fi
  fi
  cleanup
  exit "$status"
}
trap rollback_on_exit EXIT

rollback_started=1
atomic_replace_service "$service_snapshot"
ln -s -- "$release_dir" "$temporary_current"
mv -Tf -- "$temporary_current" "$state_root/current"
systemctl daemon-reload
systemctl restart aflowd.service
rollback_started=0

trap - EXIT
cleanup
printf 'aflowd rolled back to %s\n' "$release_dir"

#!/usr/bin/env bash
set -euo pipefail

state_root=/opt/aflowd
service_path=/etc/systemd/system/aflowd.service
release_id=""

usage() {
  printf 'Usage: rollback.sh --release COMMIT [--root PATH] [--service-path PATH]\n'
}

while (($#)); do
  case "$1" in
    --release) release_id=$2; shift 2 ;;
    --root) state_root=$2; shift 2 ;;
    --service-path) service_path=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || exit 2
release_dir="$state_root/releases/$release_id"
[[ -d "$release_dir" && ! -L "$release_dir" ]] || exit 1
[[ -x "$release_dir/bin/aflow-app-server" ]] || exit 1
[[ -f "$release_dir/config/config.toml" && ! -L "$release_dir/config/config.toml" ]] || exit 1
[[ -f "$service_path" && ! -L "$service_path" ]] || exit 1

previous_target=$(realpath -e -- "$state_root/current")
[[ -d "$previous_target" && ! -L "$previous_target" ]] || exit 1
temporary_current="$state_root/.current.$release_id.rollback"
previous_service=$(mktemp /tmp/aflowd.rollback.service.previous.XXXXXX)
rendered_service=$(mktemp /tmp/aflowd.rollback.service.rendered.XXXXXX)
cp -- "$service_path" "$previous_service"

cleanup() {
  rm -f -- "$temporary_current" "$previous_service" "$rendered_service"
}
trap cleanup EXIT

python3 - "$service_path" "$rendered_service" "$previous_target" "$release_dir" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
previous = sys.argv[3]
selected = sys.argv[4]
if previous not in source:
    raise SystemExit("aflowd rollback failed: active service is not pinned to current release")
Path(sys.argv[2]).write_text(source.replace(previous, selected), encoding="utf-8")
PY

restore_previous() {
  install -m 0644 -- "$previous_service" "$service_path"
  ln -s -- "$previous_target" "$temporary_current"
  mv -Tf -- "$temporary_current" "$state_root/current"
  systemctl daemon-reload || true
  systemctl restart aflowd.service || true
}

install -m 0644 -- "$rendered_service" "$service_path"
ln -s -- "$release_dir" "$temporary_current"
mv -Tf -- "$temporary_current" "$state_root/current"
if ! systemctl daemon-reload || ! systemctl restart aflowd.service; then
  restore_previous
  printf 'aflowd rollback failed; restored %s\n' "$previous_target" >&2
  exit 1
fi

trap - EXIT
cleanup
printf 'aflowd rolled back to %s\n' "$release_dir"

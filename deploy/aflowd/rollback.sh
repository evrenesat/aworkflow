#!/usr/bin/env bash
set -euo pipefail

state_root=/opt/aflowd
release_id=""
while (($#)); do
  case "$1" in
    --release) release_id=$2; shift 2 ;;
    --root) state_root=$2; shift 2 ;;
    --help|-h) printf 'Usage: rollback.sh --release COMMIT [--root PATH]\n'; exit 0 ;;
    *) printf 'Usage: rollback.sh --release COMMIT [--root PATH]\n' >&2; exit 2 ;;
  esac
done
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || exit 2
release_dir="$state_root/releases/$release_id"
[[ -d "$release_dir" && ! -L "$release_dir" ]] || exit 1
temporary_current="$state_root/.current.$release_id.rollback"
ln -s -- "$release_dir" "$temporary_current"
mv -Tf -- "$temporary_current" "$state_root/current"
systemctl daemon-reload
systemctl restart aflowd.service
printf 'aflowd rolled back to %s\n' "$release_dir"

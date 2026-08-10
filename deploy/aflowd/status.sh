#!/usr/bin/env bash
set -euo pipefail

state_root=/opt/aflowd
if (($#)); then
  state_root=$1
fi
if [[ -L "$state_root/current" ]]; then
  printf 'current release: %s\n' "$(realpath -e -- "$state_root/current")"
else
  printf 'current release: none\n'
fi
systemctl status --no-pager --lines=20 aflowd.service

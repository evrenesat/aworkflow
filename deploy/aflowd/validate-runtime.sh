#!/usr/bin/env bash
set -euo pipefail

release=""
config=""
environment_file=""
project_root=""
project_config=""
bind_address="100.103.69.9"
interface=tailscale0
skip_interface_check=0

usage() {
  cat <<'EOF'
Usage: validate-runtime.sh --release PATH --config PATH --environment-file PATH \
  --project-root PATH --project-config PATH [--interface NAME] [--bind-address ADDRESS] [--skip-interface-check]
EOF
}

fail() {
  printf 'aflowd runtime validation failed: %s\n' "$1" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --release) release=${2:?}; shift 2 ;;
    --config) config=${2:?}; shift 2 ;;
    --environment-file) environment_file=${2:?}; shift 2 ;;
    --project-root) project_root=${2:?}; shift 2 ;;
    --project-config) project_config=${2:?}; shift 2 ;;
    --interface) interface=${2:?}; shift 2 ;;
    --bind-address) bind_address=${2:?}; shift 2 ;;
    --skip-interface-check) skip_interface_check=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ -n "$release" && -n "$config" && -n "$environment_file" && -n "$project_root" && -n "$project_config" ]] || {
  usage >&2
  exit 2
}

[[ -d "$release" && ! -L "$release" ]] || fail "release must be a real directory"
release=$(realpath -e -- "$release")
[[ -f "$config" && ! -L "$config" ]] || fail "rendered control-plane config must be a regular file"
[[ -f "$environment_file" && ! -L "$environment_file" ]] || fail "token environment file must be a regular file"
[[ "$project_root" != / && "$project_root" != *'*'* && "$project_root" != *'?'* ]] || fail "allowlisted project root must be explicit and non-wildcard"
[[ "$project_config" != *'*'* && "$project_config" != *'?'* ]] || fail "allowlisted project config must be explicit and non-wildcard"
[[ -d "$project_root" && -f "$project_config" ]] || fail "allowlisted project root/config is unavailable"

for entrypoint in aflow aflowd aflow-app-server; do
  path="$release/bin/$entrypoint"
  [[ -f "$path" && ! -L "$path" && -x "$path" ]] || fail "release entrypoint is not a regular executable: $entrypoint"
done

mode=$(stat -c '%a' -- "$environment_file")
[[ "$mode" == "600" ]] || fail "token environment file must have mode 0600"
grep -Eq '^AFLOW_APP_TOKEN=[A-Za-z0-9._~-]+$' "$environment_file" || fail "token environment file must contain one opaque AFLOW_APP_TOKEN value"
python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$config" >/dev/null 2>&1 || fail "rendered control-plane config must be valid TOML"
grep -Fq "root = \"$project_root\"" "$config" || fail "control-plane allowlist does not name the configured project root"
grep -Fq "config_path = \"$project_config\"" "$config" || fail "control-plane config path is not explicit"
grep -Fq "aflow_executable = \"$release/bin/aflow\"" "$config" || fail "daemon executable is not release-pinned"
grep -Fq "release_identity = \"$(basename -- "$release")\"" "$config" || fail "release identity does not match release path"
! grep -Fq '/current/' "$config" || fail "rendered config must not resolve entrypoints through current"

if (( ! skip_interface_check )); then
  ip -4 -o addr show dev "$interface" | grep -Eq "[[:space:]]${bind_address}/" || fail "${bind_address} is not assigned to ${interface}"
fi

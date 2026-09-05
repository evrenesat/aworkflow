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
[[ ! -L "$project_root" && ! -L "$project_config" ]] || fail "allowlisted project root/config must not be symlinks"
project_root=$(realpath -e -- "$project_root")
project_config=$(realpath -e -- "$project_config")
[[ "$project_config" == "$project_root"/* ]] || fail "allowlisted project config must be contained by the project root"

for entrypoint in aflow aflowd aflow-app-server; do
  path="$release/bin/$entrypoint"
  [[ -f "$path" && ! -L "$path" && -x "$path" ]] || fail "release entrypoint is not a regular executable: $entrypoint"
done

mode=$(stat -c '%a' -- "$environment_file")
[[ "$mode" == "600" ]] || fail "token environment file must have mode 0600"
grep -Eq '^AFLOW_APP_TOKEN=[A-Za-z0-9._~-]+$' "$environment_file" || fail "token environment file must contain one opaque AFLOW_APP_TOKEN value"
python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$config" >/dev/null 2>&1 || fail "rendered control-plane config must be valid TOML"
if ! control_paths=$(python3 - "$config" "$release/bin/aflow" "$environment_file" "$(basename -- "$release")" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    payload = tomllib.load(handle)
control = payload.get("control_plane")
assert isinstance(control, dict)
assert "projects" not in control
expected = {
    "aflow_executable": sys.argv[2],
    "environment_file": sys.argv[3],
    "release_identity": sys.argv[4],
}
assert all(control.get(key) == value for key, value in expected.items())
environment = control.get("environment")
assert isinstance(environment, dict)
assert all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())
managed_root = control.get("managed_projects_root")
registry_path = control.get("project_registry_path")
assert isinstance(managed_root, str) and "\n" not in managed_root
assert isinstance(registry_path, str) and "\n" not in registry_path
print(managed_root)
print(registry_path)
PY
); then
  fail "rendered control-plane shared fields are invalid"
fi
mapfile -t shared_paths <<<"$control_paths"
(( ${#shared_paths[@]} == 2 )) || fail "rendered control-plane shared paths are invalid"
managed_projects_root=${shared_paths[0]}
project_registry_path=${shared_paths[1]}
[[ -d "$managed_projects_root" && ! -L "$managed_projects_root" ]] || fail "managed projects root must be a real directory"
managed_projects_root=$(realpath -e -- "$managed_projects_root")
[[ "$project_registry_path" == /* && "$project_registry_path" != *'*'* && "$project_registry_path" != *'?'* && ! -L "$project_registry_path" ]] || fail "project registry path must be explicit and non-symlink"
[[ -d "$(dirname -- "$project_registry_path")" ]] || fail "project registry parent is unavailable"
if [[ -e "$project_registry_path" ]]; then
  [[ -f "$project_registry_path" ]] || fail "project registry must be a regular file when present"
fi
[[ "$project_root" == "$managed_projects_root"/* ]] || fail "allowlisted project root must be contained by the managed projects root"
! grep -Fq '/current/' "$config" || fail "rendered config must not resolve entrypoints through current"

if (( ! skip_interface_check )); then
  ip -4 -o addr show dev "$interface" | grep -Eq "[[:space:]]${bind_address}/" || fail "${bind_address} is not assigned to ${interface}"
fi

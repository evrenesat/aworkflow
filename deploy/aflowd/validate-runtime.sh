#!/usr/bin/env bash
set -euo pipefail

release=""
config=""
environment_file=""
managed_projects_root=""
project_registry_path=""
bind_address="127.0.0.1"

usage() {
  cat <<'USAGE'
Usage: validate-runtime.sh --release PATH --config PATH --environment-file PATH \
  --managed-projects-root PATH --project-registry-path PATH [--bind-address 127.0.0.1]
USAGE
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
    --managed-projects-root) managed_projects_root=${2:?}; shift 2 ;;
    --project-registry-path) project_registry_path=${2:?}; shift 2 ;;
    --bind-address) bind_address=${2:?}; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ -n "$release" && -n "$config" && -n "$environment_file" && -n "$managed_projects_root" && -n "$project_registry_path" ]] || {
  usage >&2
  exit 2
}
[[ "$bind_address" == 127.0.0.1 ]] || fail "backend bind address must be 127.0.0.1"
[[ -d "$release" && ! -L "$release" ]] || fail "release must be a real directory"
release=$(realpath -e -- "$release")
release_id=$(basename -- "$release")
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || fail "release directory must use a 40-character commit identity"
[[ -f "$config" && ! -L "$config" ]] || fail "rendered control-plane config must be a regular file"
[[ -f "$environment_file" && ! -L "$environment_file" ]] || fail "token environment file must be a regular file"
[[ "$managed_projects_root" == /* && "$managed_projects_root" != / && "$managed_projects_root" != *'*'* && "$managed_projects_root" != *'?'* ]] || fail "managed projects root must be an explicit non-wildcard absolute path"
[[ -d "$managed_projects_root" && ! -L "$managed_projects_root" ]] || fail "managed projects root must be a real directory"
managed_projects_root=$(realpath -e -- "$managed_projects_root")
[[ "$project_registry_path" == /* && "$project_registry_path" != *'*'* && "$project_registry_path" != *'?'* && ! -L "$project_registry_path" ]] || fail "project registry path must be explicit and non-symlink"
registry_parent=$(dirname -- "$project_registry_path")
[[ -d "$registry_parent" && ! -L "$registry_parent" ]] || fail "project registry parent must be a real directory"

for entrypoint in aflow aflowd aflow-app-server; do
  path="$release/bin/$entrypoint"
  [[ -f "$path" && ! -L "$path" && -x "$path" ]] || fail "release entrypoint is not a regular executable: $entrypoint"
done
manifest="$release/release-manifest.sha256"
[[ -f "$manifest" && ! -L "$manifest" ]] || fail "release manifest must be a regular file"
grep -Fxq "source_commit=$release_id" "$manifest" || fail "release manifest identity is stale"
tail -n +2 "$manifest" | (cd "$release" && sha256sum --check --status) || fail "release snapshot hashes are stale"

mode=$(stat -c '%a' -- "$environment_file")
[[ "$mode" == "600" ]] || fail "token environment file must have mode 0600"
grep -Eq '^AFLOW_APP_TOKEN=[A-Za-z0-9._~-]+$' "$environment_file" || fail "token environment file must contain one opaque AFLOW_APP_TOKEN value"

python3 - "$config" "$release/bin/aflow" "$environment_file" "$release_id" "$managed_projects_root" "$project_registry_path" <<'PY' || fail "rendered control-plane configuration is invalid"
import sys
import tomllib
from pathlib import Path

with open(sys.argv[1], "rb") as handle:
    payload = tomllib.load(handle)
server = payload.get("server")
control = payload.get("control_plane")
assert isinstance(server, dict)
assert server.get("bind_host") == "127.0.0.1"
assert server.get("bind_port") == 8765
assert "repo_registry_path" not in server
assert "project_catalog" not in payload
assert "planning" not in payload
assert isinstance(control, dict)
assert "projects" not in control
expected = {
    "aflow_executable": sys.argv[2],
    "environment_file": sys.argv[3],
    "release_identity": sys.argv[4],
    "managed_projects_root": sys.argv[5],
    "project_registry_path": sys.argv[6],
}
assert all(control.get(key) == value for key, value in expected.items())
environment = control.get("environment")
assert isinstance(environment, dict)
assert all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())
assert "/current/" not in Path(sys.argv[1]).read_text(encoding="utf-8")
PY

if [[ -e "$project_registry_path" ]]; then
  [[ -f "$project_registry_path" ]] || fail "project registry must be a regular file when present"
  python3 - "$project_registry_path" "$managed_projects_root" <<'PY' || fail "project registry is invalid"
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

registry = Path(sys.argv[1])
managed = Path(sys.argv[2])
payload = json.loads(registry.read_text(encoding="utf-8"))
assert set(payload) == {"schema_version", "projects"}
assert payload["schema_version"] == 1
assert isinstance(payload["projects"], list)
ids: set[str] = set()
roots: list[Path] = []
for item in payload["projects"]:
    assert set(item) == {"schema_version", "id", "display_name", "relative_root", "created_at", "updated_at"}
    assert item["schema_version"] == 1 and item["id"] not in ids
    relative = PurePosixPath(item["relative_root"])
    assert not relative.is_absolute() and relative.parts and all(part not in {"", ".", ".."} for part in relative.parts)
    candidate = managed.joinpath(*relative.parts)
    cursor = managed
    for part in relative.parts:
        cursor = cursor / part
        assert not cursor.is_symlink()
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(managed)
    assert resolved.is_dir()
    observed = subprocess.run(("git", "-C", str(resolved), "rev-parse", "--show-toplevel"), check=True, capture_output=True, text=True, timeout=5)
    assert os.path.normcase(str(Path(observed.stdout.strip()).resolve(strict=True))) == os.path.normcase(str(resolved))
    assert all(resolved not in other.parents and other not in resolved.parents for other in roots)
    ids.add(item["id"])
    roots.append(resolved)
PY
fi

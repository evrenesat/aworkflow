#!/usr/bin/env bash
set -euo pipefail

output_dir=""
current_backend_url=""
state_root=/opt/aflowd
service_path=/etc/systemd/system/aflowd.service
environment_file=/etc/aflowd/aflowd.env
registry_path=/var/lib/aflowd/projects.json
project_config_root=""

usage() {
  cat <<'USAGE'
Usage: preflight.sh --output-dir PATH --current-backend-url URL \
  --project-config-root PATH [--root PATH] [--service-path PATH] \
  [--environment-file PATH] [--registry-path PATH]

The command uses the existing mode-0600 bearer file only for bounded read-only
requests to /ready and canonical project/run APIs. The token and unbounded run
content are never written to the snapshot.
USAGE
}

fail() {
  printf 'aflowd preflight failed: %s\n' "$1" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --output-dir) output_dir=$2; shift 2 ;;
    --current-backend-url) current_backend_url=$2; shift 2 ;;
    --root) state_root=$2; shift 2 ;;
    --service-path) service_path=$2; shift 2 ;;
    --environment-file) environment_file=$2; shift 2 ;;
    --registry-path) registry_path=$2; shift 2 ;;
    --project-config-root) project_config_root=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
[[ -n "$output_dir" && -n "$current_backend_url" && -n "$project_config_root" ]] || { usage >&2; exit 2; }
[[ "$output_dir" == /* && ! -e "$output_dir" ]] || fail "output directory must be a new absolute path"
[[ "$current_backend_url" =~ ^http://(127\.0\.0\.1|100\.[0-9]+\.[0-9]+\.[0-9]+):[0-9]+$ ]] || fail "current backend URL must be explicit loopback or Tailscale IPv4 HTTP"
[[ -f "$service_path" && ! -L "$service_path" ]] || fail "current service unit must be a regular file"
[[ -f "$environment_file" && ! -L "$environment_file" ]] || fail "bearer environment must be a regular file"
[[ $(stat -c '%a' -- "$environment_file") == 600 ]] || fail "bearer environment must have mode 0600"
[[ -d "$project_config_root" && ! -L "$project_config_root" ]] || fail "project config root must be a real directory"
for name in aflow.toml workflows.toml; do
  [[ -f "$project_config_root/$name" && ! -L "$project_config_root/$name" ]] || fail "project config pair is incomplete"
done
for command in systemctl tailscale pgrep python3; do
  command -v "$command" >/dev/null || fail "$command is unavailable"
done

current_release=""
if [[ -L "$state_root/current" ]]; then
  current_release=$(realpath -e -- "$state_root/current") || fail "current release link is broken"
elif [[ -e "$state_root/current" ]]; then
  fail "current release path must be a symlink"
fi

mkdir -m 0700 -- "$output_dir"
cp --preserve=mode,timestamps -- "$service_path" "$output_dir/aflowd.service.before"
cp --preserve=mode,timestamps -- "$project_config_root/aflow.toml" "$output_dir/aflow.toml.before"
cp --preserve=mode,timestamps -- "$project_config_root/workflows.toml" "$output_dir/workflows.toml.before"
if [[ -e "$registry_path" ]]; then
  [[ -f "$registry_path" && ! -L "$registry_path" ]] || fail "registry must be a regular file"
  cp --preserve=mode,timestamps -- "$registry_path" "$output_dir/projects.json.before"
fi
stat -c 'mode=%a uid=%u gid=%g size=%s path=%n' -- "$environment_file" >"$output_dir/environment.metadata"
printf '%s\n' "$current_release" >"$output_dir/current-release.txt"
tailscale serve get-config --all >"$output_dir/tailscale-serve.before.json"
tailscale status --json >"$output_dir/tailscale-status.before.json"
systemctl cat aflowd.service >"$output_dir/systemd-unit.before.txt"
systemctl show aflowd.service --property=ActiveState,SubState,MainPID >"$output_dir/systemd-status.before.txt"
systemctl list-units --all --no-legend --plain 'aflow-run-*.service' >"$output_dir/workflow-units.before.txt"
pgrep -a -f '(^|[ /])aflow (run|daemon)( |$)' >"$output_dir/controllers.before.txt" || true

python3 - "$current_backend_url" "$environment_file" "$output_dir/canonical-state.json" <<'PY'
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

base, environment_path, output_path = sys.argv[1:]
token = ""
for line in Path(environment_path).read_text(encoding="utf-8").splitlines():
    if line.startswith("AFLOW_APP_TOKEN="):
        token = line.removeprefix("AFLOW_APP_TOKEN=")
if not token or any(character.isspace() for character in token):
    raise SystemExit("bearer environment has no valid token")

def get_json(path: str):
    request = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"canonical control-plane read failed: {type(exc).__name__}") from exc

ready = get_json("/ready")
projects_payload = get_json("/api/control-plane/projects")
projects = projects_payload.get("projects")
if not isinstance(projects, list) or len(projects) > 100:
    raise SystemExit("canonical project list is invalid or unbounded")
result = {"ready": ready.get("ready") is True, "project_errors": ready.get("project_errors", {}), "projects": []}
for project in projects:
    project_id = project.get("project_id")
    if not isinstance(project_id, str):
        raise SystemExit("canonical project identity is invalid")
    runs: list[dict[str, object]] = []
    cursor = None
    for _ in range(20):
        query = "?limit=100"
        if cursor is not None:
            query += "&" + urllib.parse.urlencode({"cursor": cursor})
        page = get_json(f"/api/control-plane/projects/{urllib.parse.quote(project_id, safe='')}/runs{query}")
        page_runs = page.get("runs")
        if not isinstance(page_runs, list):
            raise SystemExit("canonical run list is invalid")
        for run in page_runs:
            selected = {key: run.get(key) for key in ("run_id", "status", "ownership", "launch_phase")}
            if not isinstance(selected["run_id"], str) or not isinstance(selected["status"], str):
                raise SystemExit("canonical run status is invalid")
            runs.append(selected)
        cursor = page.get("next_cursor")
        if cursor is None:
            break
        if not isinstance(cursor, str):
            raise SystemExit("canonical run cursor is invalid")
    else:
        raise SystemExit("canonical run list exceeds the bounded preflight")
    result["projects"].append({"project_id": project_id, "runs": runs})
Path(output_path).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 - "$output_dir" "$current_release" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
root = Path(sys.argv[1])
canonical = json.loads((root / "canonical-state.json").read_text(encoding="utf-8"))
active_units = [
    line.split()[0]
    for line in (root / "workflow-units.before.txt").read_text(encoding="utf-8").splitlines()
    if len(line.split()) >= 4 and line.split()[2] in {"active", "activating", "reloading", "deactivating"}
]
controllers = [line for line in (root / "controllers.before.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
terminal = {"completed", "failed", "interrupted", "owner_stopped"}
unsafe_runs = [
    {"project_id": project["project_id"], **run}
    for project in canonical["projects"]
    for run in project["runs"]
    # Older servers can project a saved startup question over a durable owner stop.
    # Unit/controller checks below still reject any active execution.
    if run["status"] not in terminal and run.get("launch_phase") != "owner_stopped"
]
project_errors = canonical.get("project_errors")
safe = canonical.get("ready") is True and not project_errors and not active_units and not controllers and not unsafe_runs
payload = {
    "schema_version": 1,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "current_release": sys.argv[2] or None,
    "active_workflow_units": active_units,
    "active_controllers": controllers,
    "unsafe_runs": unsafe_runs,
    "project_errors": project_errors,
    "safe_to_rollout": safe,
}
(root / "preflight.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if not safe:
    raise SystemExit("preflight found active or ambiguous workflow ownership")
PY
chmod 0600 -- "$output_dir"/*
printf 'aflowd preflight snapshot: %s\n' "$output_dir"

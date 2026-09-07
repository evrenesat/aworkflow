#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_root=$(git -C "$script_dir/../.." rev-parse --show-toplevel)
commit=""
state_root=/opt/aflowd
service_path=/etc/systemd/system/aflowd.service
environment_file=/etc/aflowd/aflowd.env
managed_projects_root=/root/code
project_registry_path=/var/lib/aflowd/projects.json
bind_address=127.0.0.1
bind_port=8765
preflight_snapshot=""
uv_bin=uv
npm_bin=npm
apply=0
skip_service=0
skip_readiness=0
stage_only=0

usage() {
  cat <<'USAGE'
Usage: install.sh [options]

Stages one exact Git commit as /opt/aflowd/releases/<commit>. It is dry-run by
default; --apply is required before it changes a release root or service.

  --source PATH                  Git checkout to archive (default: this checkout)
  --commit REV                   Commit-ish to resolve (default: HEAD)
  --root PATH                    Release state root (default: /opt/aflowd)
  --service-path PATH            Rendered systemd unit path
  --environment-file PATH        Mode-0600 AFLOW_APP_TOKEN EnvironmentFile
  --managed-projects-root PATH   Canonical managed root (default: /root/code)
  --project-registry-path PATH   Registry JSON (default: /var/lib/aflowd/projects.json)
  --preflight-snapshot PATH      Safe snapshot directory from preflight.sh
  --uv PATH                      uv executable used for release build
  --npm PATH                     npm executable used for web build
  --stage-only                   Build/validate release without switching current
  --skip-service                 Stage/switch without touching systemd
  --skip-readiness               Skip readiness (only with --skip-service)
  --apply                        Permit local installation mutations
  --dry-run                      Print the plan and do not mutate (default)
USAGE
}

fail() {
  printf 'aflowd install failed: %s\n' "$1" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --source) source_root=$2; shift 2 ;;
    --commit) commit=$2; shift 2 ;;
    --root) state_root=$2; shift 2 ;;
    --service-path) service_path=$2; shift 2 ;;
    --environment-file) environment_file=$2; shift 2 ;;
    --managed-projects-root) managed_projects_root=$2; shift 2 ;;
    --project-registry-path) project_registry_path=$2; shift 2 ;;
    --preflight-snapshot) preflight_snapshot=$2; shift 2 ;;
    --uv) uv_bin=$2; shift 2 ;;
    --npm) npm_bin=$2; shift 2 ;;
    --stage-only) stage_only=1; skip_service=1; skip_readiness=1; shift ;;
    --skip-service) skip_service=1; shift ;;
    --skip-readiness) skip_readiness=1; shift ;;
    --apply) apply=1; shift ;;
    --dry-run) apply=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ "$state_root" == /* && "$state_root" != / && "$state_root" != *'*'* && "$state_root" != *'?'* ]] || fail "state root must be an explicit absolute path"
[[ "$managed_projects_root" == /* && "$managed_projects_root" != / && "$managed_projects_root" != *'*'* && "$managed_projects_root" != *'?'* ]] || fail "managed projects root must be an explicit absolute path"
[[ "$project_registry_path" == /* && "$project_registry_path" != *'*'* && "$project_registry_path" != *'?'* ]] || fail "project registry path must be explicit"
[[ "$bind_address" == 127.0.0.1 ]] || fail "backend must bind to loopback"
(( ! skip_readiness || skip_service )) || fail "--skip-readiness requires --skip-service"
source_root=$(realpath -e -- "$source_root")
git -C "$source_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "source is not a Git checkout"
[[ -n "$commit" ]] || commit=HEAD
commit=$(git -C "$source_root" rev-parse --verify "$commit^{commit}") || fail "commit cannot be resolved"
release_root="$state_root/releases"
release_dir="$release_root/$commit"
current_link="$state_root/current"
previous_target=""
if [[ -L "$current_link" ]]; then
  previous_target=$(realpath -e -- "$current_link") || fail "current release link is broken"
elif [[ -e "$current_link" ]]; then
  fail "current release path must be a symlink"
fi

printf 'release source: %s@%s\n' "$source_root" "$commit"
printf 'release destination: %s\n' "$release_dir"
printf 'service name: aflowd.service (%s)\n' "$service_path"
printf 'backend bind: %s:%s (Tailscale Serve target)\n' "$bind_address" "$bind_port"
printf 'managed projects root: %s\n' "$managed_projects_root"
printf 'project registry: %s\n' "$project_registry_path"
if [[ -n "$previous_target" ]]; then
  printf 'rollback target: %s\n' "$previous_target"
else
  printf 'rollback target: none (stop and disable aflowd.service)\n'
fi

if (( ! apply )); then
  printf 'dry-run: no files, services, Tailscale mappings, or secrets were changed\n'
  exit 0
fi

[[ -d "$managed_projects_root" && ! -L "$managed_projects_root" ]] || fail "managed projects root must be a real directory"
managed_projects_root=$(realpath -e -- "$managed_projects_root")
[[ -f "$environment_file" && ! -L "$environment_file" ]] || fail "token environment file must be a regular file"
[[ $(stat -c '%a' -- "$environment_file") == 600 ]] || fail "token environment file must have mode 0600"
grep -Eq '^AFLOW_APP_TOKEN=[A-Za-z0-9._~-]+$' "$environment_file" || fail "token environment file must contain one opaque AFLOW_APP_TOKEN"
command -v "$uv_bin" >/dev/null || fail "uv executable is unavailable"
command -v "$npm_bin" >/dev/null || fail "npm executable is unavailable"
if (( ! skip_service )); then
  command -v systemctl >/dev/null || fail "systemctl is unavailable"
  command -v curl >/dev/null || fail "curl is unavailable"
  [[ -n "$preflight_snapshot" ]] || fail "--preflight-snapshot is required before service rollout"
  [[ -f "$preflight_snapshot/preflight.json" && ! -L "$preflight_snapshot/preflight.json" ]] || fail "preflight snapshot is unavailable"
  observed_preflight_target=$(python3 - "$preflight_snapshot/preflight.json" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload.get("schema_version") == 1
assert payload.get("safe_to_rollout") is True
assert payload.get("active_workflow_units") == []
assert payload.get("active_controllers") == []
created = datetime.fromisoformat(payload["created_at"])
assert created.tzinfo is not None
assert 0 <= (datetime.now(timezone.utc) - created).total_seconds() <= 3600
print(payload.get("current_release") or "")
PY
) || fail "preflight snapshot is invalid, unsafe, or stale"
  [[ "$observed_preflight_target" == "$previous_target" ]] || fail "current release changed after preflight"
fi

mkdir -p -- "$release_root" "$state_root"
registry_parent=$(dirname -- "$project_registry_path")
[[ -d "$registry_parent" && ! -L "$registry_parent" ]] || fail "project registry parent must already be a real directory"
stage=$(mktemp -d "$release_root/.$commit.stage.XXXXXX")
temporary_current="$state_root/.current.$commit.new"
previous_service=$(mktemp /tmp/aflowd.service.previous.XXXXXX)
service_candidate=$(mktemp /tmp/aflowd.service.candidate.XXXXXX)
release_config="$release_dir/config/config.toml"
service_existed=0
service_rendered=0
installed=0
curl_config=""
intended_config=""

cleanup_files() {
  rm -f -- "$temporary_current" "$previous_service" "$service_candidate"
  [[ -z "$curl_config" ]] || rm -f -- "$curl_config"
  [[ -z "$intended_config" ]] || rm -f -- "$intended_config"
  [[ -z "$stage" ]] || rm -rf -- "$stage"
}

rollback_install() {
  status=$?
  trap - EXIT
  if (( installed || service_rendered )); then
    if (( ! skip_service )); then
      systemctl stop aflowd.service || true
    fi
    if (( service_existed )); then
      install -D -m 0644 -- "$previous_service" "$service_path"
    else
      rm -f -- "$service_path"
    fi
    if [[ -n "$previous_target" ]]; then
      ln -s -- "$previous_target" "$temporary_current"
      mv -Tf -- "$temporary_current" "$current_link"
    else
      rm -f -- "$current_link"
    fi
    if (( ! skip_service )); then
      systemctl daemon-reload || true
      if [[ -n "$previous_target" ]] && (( service_existed )); then
        systemctl restart aflowd.service || true
      else
        systemctl disable aflowd.service || true
      fi
    fi
    printf 'aflowd install rolled back to %s\n' "$previous_target" >&2
  fi
  cleanup_files
  exit "$status"
}
trap rollback_install EXIT

render_template() {
  source=$1
  target=$2
  RELEASE_DIR="$release_dir" RELEASE_ID="$commit" MANAGED_PROJECTS_ROOT="$managed_projects_root" PROJECT_REGISTRY_PATH="$project_registry_path" ENVIRONMENT_FILE="$environment_file" python3 - "$source" "$target" <<'PY'
import os
import sys
from pathlib import Path
values = {
    "@RELEASE_DIR@": os.environ["RELEASE_DIR"],
    "@RELEASE_ID@": os.environ["RELEASE_ID"],
    "@MANAGED_PROJECTS_ROOT@": os.environ["MANAGED_PROJECTS_ROOT"],
    "@PROJECT_REGISTRY_PATH@": os.environ["PROJECT_REGISTRY_PATH"],
    "@ENVIRONMENT_FILE@": os.environ["ENVIRONMENT_FILE"],
}
rendered = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker, value in values.items():
    rendered = rendered.replace(marker, value)
if "@" in rendered:
    raise SystemExit("unresolved deployment template marker")
target = Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(rendered, encoding="utf-8")
PY
}

if [[ ! -d "$release_dir" ]]; then
  mkdir -p -- "$stage/src"
  git -C "$source_root" archive --format=tar "$commit" | tar -x -C "$stage/src"
  "$uv_bin" venv --python 3.12 "$stage/venv"
  (
    cd "$stage/src/apps/aflow_app/server"
    UV_PROJECT_ENVIRONMENT="$stage/venv" "$uv_bin" sync --locked --no-dev --no-editable
  )
  (
    cd "$stage/src/apps/aflow_app/web"
    "$npm_bin" ci --ignore-scripts
    "$npm_bin" run build
  )
  mkdir -p -- "$stage/bin" "$stage/config"
  write_entrypoint() {
    name=$1
    module=$2
    function=$3
    cat >"$stage/bin/$name" <<ENTRYPOINT
#!/bin/sh
exec "$release_dir/venv/bin/python" -P -c 'from $module import $function; raise SystemExit($function())' "\$@"
ENTRYPOINT
    chmod 0755 -- "$stage/bin/$name"
  }
  write_entrypoint aflow aflow.cli main
  write_entrypoint aflowd aflow.daemon main
  write_entrypoint aflow-app-server aflow_app_server.main run_server
  render_template "$script_dir/aflow-app.toml" "$stage/config/config.toml"
  {
    printf 'source_commit=%s\n' "$commit"
    (
      cd "$stage"
      sha256sum bin/aflow bin/aflowd bin/aflow-app-server config/config.toml src/apps/aflow_app/web/dist/index.html
    )
  } >"$stage/release-manifest.sha256"
  mv -- "$stage" "$release_dir"
  stage=""
elif [[ ! -f "$release_config" || -L "$release_config" ]]; then
  fail "existing release is incomplete or mutable"
else
  intended_config=$(mktemp /tmp/aflowd.config.candidate.XXXXXX)
  render_template "$script_dir/aflow-app.toml" "$intended_config"
  cmp -s -- "$intended_config" "$release_config" || fail "existing release config differs; use a new source commit"
  rm -f -- "$intended_config"
  intended_config=""
fi

"$script_dir/validate-runtime.sh" \
  --release "$release_dir" \
  --config "$release_config" \
  --environment-file "$environment_file" \
  --managed-projects-root "$managed_projects_root" \
  --project-registry-path "$project_registry_path" \
  --bind-address "$bind_address"

if (( stage_only )); then
  trap - EXIT
  cleanup_files
  printf "aflowd release %s is staged at %s; current was not changed\n" "$commit" "$release_dir"
  exit 0
fi

if [[ -e "$service_path" ]]; then
  [[ -f "$service_path" && ! -L "$service_path" ]] || fail "service path must be a regular non-symlink file"
  cp -- "$service_path" "$previous_service"
  service_existed=1
fi
if (( ! skip_service )); then
  render_template "$script_dir/aflowd.service" "$service_candidate"
  install -D -m 0644 -- "$service_candidate" "$service_path"
  service_rendered=1
fi

ln -s -- "$release_dir" "$temporary_current"
mv -Tf -- "$temporary_current" "$current_link"
installed=1

if (( ! skip_service )); then
  systemctl daemon-reload
  systemctl enable aflowd.service
  systemctl restart aflowd.service
fi
if (( ! skip_readiness )); then
  curl_config=$(mktemp /tmp/aflowd-curl.XXXXXX)
  token=$(sed -n 's/^AFLOW_APP_TOKEN=//p' "$environment_file")
  printf 'header = "Authorization: Bearer %s"\nurl = "http://127.0.0.1:%s/ready"\n' "$token" "$bind_port" >"$curl_config"
  chmod 0600 -- "$curl_config"
  readiness_delay=${AFLOWD_READINESS_DELAY_SECONDS:-0.5}
  ready=0
  for ((attempt = 1; attempt <= 20; attempt++)); do
    if curl --fail --silent --max-time 2 --config "$curl_config" >/dev/null; then
      ready=1
      break
    fi
    (( attempt == 20 )) || sleep "$readiness_delay"
  done
  (( ready )) || fail "authenticated loopback readiness did not succeed after 20 attempts"
fi

installed=0
trap - EXIT
cleanup_files
printf 'aflowd release %s is active at %s on 127.0.0.1:%s\n' "$commit" "$release_dir" "$bind_port"

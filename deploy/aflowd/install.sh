#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_root=$(git -C "$script_dir/../.." rev-parse --show-toplevel)
commit=""
state_root=/opt/aflowd
service_path=/etc/systemd/system/aflowd.service
environment_file=/etc/aflowd/aflowd.env
project_root=/root/code/aflow-control-plane
project_config=/root/code/aflow-control-plane/aflow/aflow.toml
bind_address=100.103.69.9
bind_port=8765
uv_bin=uv
npm_bin=npm
apply=0
skip_service=0
skip_readiness=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Stages one exact Git commit as /opt/aflowd/releases/<commit>. It is dry-run by
default; --apply is required before it changes a release root or service.

  --source PATH             Git checkout to archive (default: this checkout)
  --commit REV              Commit-ish to resolve to one commit (default: HEAD)
  --root PATH               State root (default: /opt/aflowd)
  --service-path PATH       Rendered systemd unit path
  --environment-file PATH   Mode-0600 AFLOW_APP_TOKEN EnvironmentFile
  --project-root PATH       Explicit control-plane project root
  --project-config PATH     Workflow config inside the explicit project root
  --uv PATH                 uv executable used to build the isolated release
  --npm PATH                npm executable used to build the frontend
  --skip-service            Stage/switch a release without touching systemd
  --skip-readiness          Skip authenticated readiness (only with --skip-service)
  --apply                   Permit the requested local installation mutations
  --dry-run                 Print the plan and do not mutate (default)
EOF
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
    --project-root) project_root=$2; shift 2 ;;
    --project-config) project_config=$2; shift 2 ;;
    --uv) uv_bin=$2; shift 2 ;;
    --npm) npm_bin=$2; shift 2 ;;
    --skip-service) skip_service=1; shift ;;
    --skip-readiness) skip_readiness=1; shift ;;
    --apply) apply=1; shift ;;
    --dry-run) apply=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ "$state_root" != / && "$state_root" != . ]] || fail "state root must not be / or ."
[[ "$project_root" == /* && "$project_config" == /* ]] || fail "project paths must be absolute"
[[ "$project_root" != / && "$project_root" != *'*'* && "$project_root" != *'?'* ]] || fail "project allowlist must be an explicit non-wildcard root"
[[ "$project_config" != *'*'* && "$project_config" != *'?'* ]] || fail "project configuration path must be explicit"
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
fi

printf 'release source: %s@%s\n' "$source_root" "$commit"
printf 'release destination: %s\n' "$release_dir"
printf 'service name: aflowd.service (%s)\n' "$service_path"
printf 'bind address: %s:%s on tailscale0\n' "$bind_address" "$bind_port"
printf 'allowlist path: %s (config %s)\n' "$project_root" "$project_config"
if [[ -n "$previous_target" ]]; then
  rollback_target=$previous_target
else
  rollback_target='none (stop and disable aflowd.service)'
fi
printf 'rollback target: %s\n' "$rollback_target"

if (( ! apply )); then
  printf 'dry-run: no files, service units, firewall rules, or secrets were changed\n'
  exit 0
fi

[[ -d "$project_root" && -f "$project_config" ]] || fail "explicit allowlisted project/config is unavailable"
[[ -f "$environment_file" && ! -L "$environment_file" ]] || fail "token environment file must be a regular file"
[[ $(stat -c '%a' -- "$environment_file") == 600 ]] || fail "token environment file must have mode 0600"
grep -Eq '^AFLOW_APP_TOKEN=[A-Za-z0-9._~-]+$' "$environment_file" || fail "token environment file must contain an opaque AFLOW_APP_TOKEN"
command -v "$uv_bin" >/dev/null || fail "uv executable is unavailable"
command -v "$npm_bin" >/dev/null || fail "npm executable is unavailable"
if (( ! skip_service )); then
  command -v systemctl >/dev/null || fail "systemctl is unavailable"
  command -v curl >/dev/null || fail "curl is unavailable"
  ip -4 -o addr show dev tailscale0 | grep -Eq "[[:space:]]$bind_address/" || fail "$bind_address is not assigned to tailscale0"
fi

mkdir -p -- "$release_root" "$state_root"
stage=$(mktemp -d "$release_root/.$commit.stage.XXXXXX")
temporary_current="$state_root/.current.$commit.new"
previous_service=$(mktemp /tmp/aflowd.service.previous.XXXXXX)
release_config="$release_dir/config/config.toml"
previous_config_backup=""
service_existed=0
service_rendered=0
installed=0
curl_config=""

rollback_install() {
  status=$?
  trap - EXIT
  rm -f -- "$temporary_current"
  [[ -z "$curl_config" ]] || rm -f -- "$curl_config"
  if [[ -n "$previous_config_backup" ]]; then
    cp --preserve=mode,timestamps -- "$previous_config_backup" "$release_config"
    rm -f -- "$previous_config_backup"
  fi
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
  [[ -z "$stage" ]] || rm -rf -- "$stage"
  rm -f -- "$previous_service"
  exit "$status"
}
trap rollback_install EXIT

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
    cat >"$stage/bin/$name" <<EOF
#!/bin/sh
exec "$release_dir/venv/bin/python" -P -c 'from $module import $function; raise SystemExit($function())' "\$@"
EOF
    chmod 0755 -- "$stage/bin/$name"
  }
  write_entrypoint aflow aflow.cli main
  write_entrypoint aflowd aflow.daemon main
  write_entrypoint aflow-app-server aflow_app_server.main run_server
  mv -- "$stage" "$release_dir"
  stage=""
fi

[[ -x "$release_dir/venv/bin/python" ]] || fail "release Python is unavailable"
[[ -f "$release_dir/src/apps/aflow_app/web/dist/index.html" ]] || fail "release web build is unavailable"
[[ -x "$release_dir/src/deploy/aflowd/validate-runtime.sh" ]] || fail "release runtime validator is unavailable"
for entrypoint in aflow aflowd aflow-app-server; do
  [[ -f "$release_dir/bin/$entrypoint" && ! -L "$release_dir/bin/$entrypoint" && -x "$release_dir/bin/$entrypoint" ]] || fail "release entrypoint is unavailable: $entrypoint"
done

render_template() {
  source=$1
  target=$2
  RELEASE_DIR="$release_dir" RELEASE_ID="$commit" PROJECT_ROOT="$project_root" PROJECT_CONFIG="$project_config" ENVIRONMENT_FILE="$environment_file" python3 - "$source" "$target" <<'PY'
import os
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
values = {
    "/@RELEASE_DIR@": os.environ["RELEASE_DIR"],
    "/@PROJECT_ROOT@": os.environ["PROJECT_ROOT"],
    "/@PROJECT_CONFIG@": os.environ["PROJECT_CONFIG"],
    "/@ENVIRONMENT_FILE@": os.environ["ENVIRONMENT_FILE"],
    "@RELEASE_DIR@": os.environ["RELEASE_DIR"],
    "@RELEASE_ID@": os.environ["RELEASE_ID"],
    "RELEASE_ID": os.environ["RELEASE_ID"],
    "@PROJECT_ROOT@": os.environ["PROJECT_ROOT"],
    "@PROJECT_CONFIG@": os.environ["PROJECT_CONFIG"],
    "@ENVIRONMENT_FILE@": os.environ["ENVIRONMENT_FILE"],
}
rendered = source.read_text(encoding="utf-8")
for marker, value in values.items():
    rendered = rendered.replace(marker, value)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(rendered, encoding="utf-8")
PY
}

release_realpath=$(realpath -e -- "$release_dir")
if [[ "$previous_target" == "$release_realpath" && ( -e "$release_config" || -L "$release_config" ) ]]; then
  [[ -f "$release_config" && ! -L "$release_config" ]] || fail "active release config must be a regular file"
  previous_config_backup=$(mktemp "$release_dir/config/.config.toml.previous.XXXXXX")
  cp --preserve=mode,timestamps -- "$release_config" "$previous_config_backup"
fi
render_template "$script_dir/aflow-app.toml" "$release_config"
"$script_dir/validate-runtime.sh" --release "$release_dir" --config "$release_config" --environment-file "$environment_file" --project-root "$project_root" --project-config "$project_config" --bind-address "$bind_address" --skip-interface-check
manifest="$release_dir/release-manifest.sha256"
if [[ ! -e "$manifest" ]]; then
  {
    printf 'source_commit=%s\n' "$commit"
    (
      cd "$release_dir"
      for entrypoint in aflow aflowd aflow-app-server; do
        sha256sum "bin/$entrypoint"
      done
    )
  } >"$manifest"
fi
[[ -f "$manifest" && ! -L "$manifest" ]] || fail "release manifest must be a regular file"
grep -Fxq "source_commit=$commit" "$manifest" || fail "release manifest commit does not match selected source"
tail -n +2 "$manifest" | (cd "$release_dir" && sha256sum --check --status) || fail "release entrypoint hashes do not match manifest"

if [[ -e "$service_path" ]]; then
  cp -- "$service_path" "$previous_service"
  service_existed=1
fi
if (( ! skip_service )); then
  render_template "$script_dir/aflowd.service" "$service_path"
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
  printf 'header = "Authorization: Bearer %s"\nurl = "http://%s:%s/ready"\n' "$token" "$bind_address" "$bind_port" >"$curl_config"
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
  (( ready )) || fail "authenticated readiness did not succeed after 20 attempts"
  rm -f -- "$curl_config"
  curl_config=""
fi

installed=0
trap - EXIT
[[ -z "$stage" ]] || rm -rf -- "$stage"
rm -f -- "$previous_service"
[[ -z "$previous_config_backup" ]] || rm -f -- "$previous_config_backup"
printf 'aflowd release %s is active at %s\n' "$commit" "$release_dir"

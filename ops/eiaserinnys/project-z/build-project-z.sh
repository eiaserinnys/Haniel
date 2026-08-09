#!/usr/bin/env bash
set -euo pipefail

PROJECT_Z_ROOT=${PROJECT_Z_ROOT:-/home/eias/services/haniel/services/project-z}
PROJECT_Z_PUBLISH_ROOT=${PROJECT_Z_PUBLISH_ROOT:-/home/eias/services/project-z}
PROJECT_Z_BUILD_LOCK=${PROJECT_Z_BUILD_LOCK:-/tmp/soulstream-heavy-verify.lock}
PROJECT_Z_MIN_AVAILABLE_MB=${PROJECT_Z_MIN_AVAILABLE_MB:-2000}
PROJECT_Z_BUILD_TIMEOUT_SECONDS=${PROJECT_Z_BUILD_TIMEOUT_SECONDS:-300}

available_mb=$(free -m | awk '/^Mem:/ {print $7}')
if [[ -z "$available_mb" || "$available_mb" -lt "$PROJECT_Z_MIN_AVAILABLE_MB" ]]; then
  echo "insufficient available memory for Project Z build: ${available_mb:-unknown}MB" >&2
  exit 1
fi

mkdir -p "$PROJECT_Z_PUBLISH_ROOT/releases"

exec 9>"$PROJECT_Z_BUILD_LOCK"
flock -w 300 9

timeout "$PROJECT_Z_BUILD_TIMEOUT_SECONDS" bash -c '
  set -euo pipefail

  project_root=$1
  publish_root=$2

  NODE_ENV=development npm --prefix "$project_root" ci --include=dev
  NODE_ENV=production npm --prefix "$project_root" run build

  test -f "$project_root/dist/index.html"

  release_sha=$(git -C "$project_root" rev-parse HEAD)
  release_stamp=$(date -u +%Y%m%dT%H%M%SZ)
  staging_dir=$(mktemp -d "$publish_root/releases/.staging-${release_sha:0:12}-XXXXXX")
  next_link="$publish_root/.current-${release_sha:0:12}-$$"

  cleanup() {
    rm -rf -- "$staging_dir"
    rm -f -- "$next_link"
  }
  trap cleanup EXIT

  cp -a "$project_root/dist/." "$staging_dir/"
  printf "%s\n" "$release_sha" > "$staging_dir/.release-sha"
  find "$staging_dir" -type d -exec chmod 755 {} +
  find "$staging_dir" -type f -exec chmod 644 {} +

  release_dir="$publish_root/releases/${release_stamp}-${release_sha:0:12}"
  mv "$staging_dir" "$release_dir"
  staging_dir=""

  ln -s "releases/$(basename "$release_dir")" "$next_link"
  mv -Tf "$next_link" "$publish_root/current"
  next_link=""

  printf "PROJECT_Z_RELEASE sha=%s path=%s\n" "$release_sha" "$release_dir"
' bash "$PROJECT_Z_ROOT" "$PROJECT_Z_PUBLISH_ROOT"

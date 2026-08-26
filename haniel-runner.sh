#!/usr/bin/env bash
# haniel-runner.sh - outer loop wrapper for Haniel self-update on Linux/systemd.
#
# Register this script as the systemd service ExecStart, not `python -m haniel`
# directly. Exit code 10 from Haniel means "self-update approved"; this wrapper
# then prepares a commit-specific release, atomically switches `current.txt`, writes
# the self-update result marker, and launches Haniel again.

set -u
unset NODE_CHANNEL_FD

ROOT_DIR="$(pwd)"
CONF_PATH="$ROOT_DIR/haniel-runner.conf"
RUNNER_SCRIPT_PATH="$(readlink -f "$0")"

if [[ ! -f "$CONF_PATH" ]]; then
  echo "Configuration file not found: $CONF_PATH" >&2
  exit 1
fi

declare -A CONFIG
while IFS='=' read -r raw_key raw_value; do
  key="$(printf '%s' "$raw_key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  value="$(printf '%s' "${raw_value:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [[ -z "$key" || "$key" == \#* ]]; then
    continue
  fi
  CONFIG["$key"]="$value"
done < "$CONF_PATH"

WEBHOOK_URL="${CONFIG[WEBHOOK_URL]:-}"
HANIEL_REPO="${CONFIG[HANIEL_REPO]:-}"
HANIEL_RELEASE_ROOT="${CONFIG[HANIEL_RELEASE_ROOT]:-.local/haniel-releases}"
HANIEL_RELEASE_RETAIN_EXTRA="${CONFIG[HANIEL_RELEASE_RETAIN_EXTRA]:-3}"
HANIEL_RELEASE_MIN_FREE_MB="${CONFIG[HANIEL_RELEASE_MIN_FREE_MB]:-5120}"
CONFIG_FILE="${CONFIG[CONFIG]:-}"
MAX_GIT_FAILURES="${CONFIG[MAX_GIT_FAILURES]:-3}"
PYTHON_BIN_CONFIG="${CONFIG[PYTHON_BIN]:-}"
SELF_UPDATE_EXIT_TIMEOUT="${CONFIG[SELF_UPDATE_EXIT_TIMEOUT]:-60}"
CRASH_RESTART_BASE_SECONDS="${CONFIG[CRASH_RESTART_BASE_SECONDS]:-5}"
CRASH_RESTART_MAX_SECONDS="${CONFIG[CRASH_RESTART_MAX_SECONDS]:-60}"
CRASH_RESET_SECONDS="${CONFIG[CRASH_RESET_SECONDS]:-300}"

if [[ -z "$HANIEL_REPO" || -z "$CONFIG_FILE" ]]; then
  echo "HANIEL_REPO and CONFIG must be set in haniel-runner.conf" >&2
  exit 1
fi

for numeric_value in \
  "$SELF_UPDATE_EXIT_TIMEOUT" \
  "$CRASH_RESTART_BASE_SECONDS" \
  "$CRASH_RESTART_MAX_SECONDS" \
  "$CRASH_RESET_SECONDS"; do
  if [[ ! "$numeric_value" =~ ^[0-9]+$ ]]; then
    echo "Runner timeout and backoff values must be non-negative integers" >&2
    exit 1
  fi
done
if (( CRASH_RESTART_BASE_SECONDS > CRASH_RESTART_MAX_SECONDS )); then
  echo "CRASH_RESTART_BASE_SECONDS must not exceed CRASH_RESTART_MAX_SECONDS" >&2
  exit 1
fi
if [[ ! "$HANIEL_RELEASE_RETAIN_EXTRA" =~ ^[0-9]+$ ]]; then
  echo "HANIEL_RELEASE_RETAIN_EXTRA must be a non-negative integer" >&2
  exit 1
fi
if [[ ! "$HANIEL_RELEASE_MIN_FREE_MB" =~ ^[1-9][0-9]*$ ]]; then
  echo "HANIEL_RELEASE_MIN_FREE_MB must be a positive integer" >&2
  exit 1
fi

resolve_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s/%s\n' "$ROOT_DIR" "$path"
  fi
}

REPO_PATH="$(resolve_path "$HANIEL_REPO")"
RELEASE_ROOT="$(resolve_path "$HANIEL_RELEASE_ROOT")"
CONFIG_PATH="$(resolve_path "$CONFIG_FILE")"
RELEASE_HELPER="$(dirname "$RUNNER_SCRIPT_PATH")/scripts/haniel_atomic_release.py"

detect_python() {
  if [[ -n "$PYTHON_BIN_CONFIG" ]]; then
    resolve_path "$PYTHON_BIN_CONFIG"
    return
  fi
  if [[ -x "$REPO_PATH/.venv/bin/python" ]]; then
    printf '%s\n' "$REPO_PATH/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  command -v python
}

PYTHON_BIN="$(detect_python)"
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable not found. Set PYTHON_BIN in haniel-runner.conf." >&2
  exit 1
fi
if [[ ! -f "$RELEASE_HELPER" ]]; then
  echo "Atomic release helper not found: $RELEASE_HELPER" >&2
  exit 1
fi

send_webhook() {
  local message="$1"
  local level="${2:-info}"

  if [[ -z "$WEBHOOK_URL" || "$WEBHOOK_URL" == "https://hooks.slack.com/services/YOUR/WEBHOOK/URL" ]]; then
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found; webhook skipped: $message" >&2
    return
  fi

  "$PYTHON_BIN" - "$WEBHOOK_URL" "$level" "$message" <<'PY'
import json
import subprocess
import sys

url, level, message = sys.argv[1:4]
prefix = {
    "error": ":rotating_light:",
    "warning": ":warning:",
}.get(level, ":information_source:")
body = json.dumps({"text": f"{prefix} *haniel-runner*: {message}"})
try:
    subprocess.run(
        ["curl", "-fsS", "-m", "10", "-H", "Content-Type: application/json", "-d", body, url],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
except Exception:
    pass
PY
}

LAST_UPDATE_ERROR=""
LAST_UPDATE_STARTED_AT=""
LAST_UPDATE_FINISHED_AT=""
PREPARATION_RESULT_PATH="$ROOT_DIR/.local/haniel_release_preparation.json"
PRUNE_REQUEST_PATH="$RELEASE_ROOT/.haniel-release-prune-request.json"
PRUNE_RESULT_PATH="$ROOT_DIR/.local/haniel_release_prune.json"
PREPARATION_OK="false"
PREPARATION_SWITCHED="false"
PREPARATION_ERROR_CODE=""
PREPARATION_WARNINGS=""
ACTIVE_REPO="$REPO_PATH"
ACTIVE_PYTHON="$PYTHON_BIN"
ACTIVE_COMMIT=""
SELF_UPDATE_EXIT_MARKER="$ROOT_DIR/.local/self_update_exit_requested"
FORCED_SELF_UPDATE_MARKER="$ROOT_DIR/.local/self_update_exit_forced"

load_preparation_result() {
  local fields=()
  if [[ ! -f "$PREPARATION_RESULT_PATH" ]]; then
    LAST_UPDATE_ERROR="atomic release helper did not write a result"
    PREPARATION_OK="false"
    PREPARATION_SWITCHED="false"
    PREPARATION_ERROR_CODE=""
    PREPARATION_WARNINGS=""
    ACTIVE_REPO=""
    ACTIVE_PYTHON=""
    ACTIVE_COMMIT=""
    return 1
  fi
  mapfile -t fields < <("$PYTHON_BIN" - "$PREPARATION_RESULT_PATH" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
print("true" if payload.get("ok") else "false")
print("true" if payload.get("switched") else "false")
print(payload.get("active_repo") or "")
print(payload.get("active_python") or "")
print(payload.get("active_commit") or "")
print((payload.get("error") or "").replace("\n", " "))
print(payload.get("error_code") or "")
print(" | ".join(payload.get("warnings") or []).replace("\n", " "))
PY
  )
  if (( ${#fields[@]} != 8 )); then
    LAST_UPDATE_ERROR="invalid atomic release helper result"
    PREPARATION_OK="false"
    PREPARATION_SWITCHED="false"
    ACTIVE_REPO=""
    ACTIVE_PYTHON=""
    ACTIVE_COMMIT=""
    PREPARATION_ERROR_CODE=""
    PREPARATION_WARNINGS=""
    return 1
  fi
  PREPARATION_OK="${fields[0]}"
  PREPARATION_SWITCHED="${fields[1]}"
  ACTIVE_REPO="${fields[2]}"
  ACTIVE_PYTHON="${fields[3]}"
  ACTIVE_COMMIT="${fields[4]}"
  LAST_UPDATE_ERROR="${fields[5]}"
  PREPARATION_ERROR_CODE="${fields[6]}"
  PREPARATION_WARNINGS="${fields[7]}"
}

load_prepared_target() {
  if [[ ! -f "$PREPARATION_RESULT_PATH" ]]; then
    return 1
  fi
  "$PYTHON_BIN" - "$PREPARATION_RESULT_PATH" "$RELEASE_ROOT" <<'PY'
import json
import pathlib
import re
import sys

result_path = pathlib.Path(sys.argv[1])
release_root = pathlib.Path(sys.argv[2]).resolve()
try:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    target = payload.get("target_commit")
    prepared = pathlib.Path(payload.get("prepared_release") or "").resolve(strict=True)
    prepared.relative_to((release_root / "releases").resolve())
    ready = json.loads(
        (prepared / ".haniel-release-ready.json").read_text(encoding="utf-8")
    )
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
if (
    payload.get("ok") is not True
    or payload.get("pre_staged") is not True
    or payload.get("switched") is not False
    or not isinstance(target, str)
    or re.fullmatch(r"[0-9a-f]{40}", target) is None
    or ready.get("commit") != target
):
    raise SystemExit(1)
print(target)
PY
}

write_self_update_marker() {
  local ok="$1"
  local marker_path="$ROOT_DIR/.local/self_update_result.json"
  mkdir -p "$ROOT_DIR/.local"

  "$PYTHON_BIN" - "$PREPARATION_RESULT_PATH" "$marker_path" "$LAST_UPDATE_STARTED_AT" "$LAST_UPDATE_FINISHED_AT" "$ok" <<'PY'
import json
import sys

result_path, marker_path, started_at, finished_at, ok_raw = sys.argv[1:6]
try:
    with open(result_path, encoding="utf-8") as handle:
        result = json.load(handle)
except (OSError, ValueError, TypeError) as exc:
    result = {"steps": [], "error": f"invalid release preparation result: {exc}"}

payload = {
    "version": 1,
    "started_at": started_at,
    "finished_at": finished_at,
    "ok": ok_raw == "true",
    "steps": result.get("steps") or [],
    "error": result.get("error"),
}

with open(marker_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
PY
}

update_haniel_release() {
  local prepared_target=""
  if [[ "$use_prepared_update" == "true" ]]; then
    if prepared_target="$(load_prepared_target)"; then
      echo "[haniel-runner] Reusing pre-staged release ${prepared_target:0:12}."
    else
      echo "[haniel-runner] Pre-stage handoff unavailable; using full preparation fallback."
      prepared_target=""
    fi
  fi
  use_prepared_update=false
  local arguments=(
    "$PYTHON_BIN"
    "$RELEASE_HELPER"
    --source "$REPO_PATH"
    --release-root "$RELEASE_ROOT"
    --bootstrap-python "$PYTHON_BIN"
    --result-json "$PREPARATION_RESULT_PATH"
    --max-git-failures "$MAX_GIT_FAILURES"
    --retain-extra "$HANIEL_RELEASE_RETAIN_EXTRA"
    --min-free-mb "$HANIEL_RELEASE_MIN_FREE_MB"
  )
  if [[ -n "$prepared_target" ]]; then
    arguments+=(--target-commit "$prepared_target")
  fi
  if [[ ! -f "$RELEASE_ROOT/current.txt" && ! -e "$RELEASE_ROOT/current" && ! -L "$RELEASE_ROOT/current" && -f "$SELF_UPDATE_EXIT_MARKER" ]]; then
    arguments+=(--prefer-orig-head)
  fi
  rm -f "$PREPARATION_RESULT_PATH"
  "${arguments[@]}" || true
  load_preparation_result || true
  if [[ -n "$PREPARATION_WARNINGS" ]]; then
    send_webhook "Atomic release cleanup warning: $PREPARATION_WARNINGS" "warning"
  fi
  if [[ "$PREPARATION_OK" != "true" ]]; then
    local level="error"
    if [[ "$PREPARATION_ERROR_CODE" == "insufficient_disk_space" ]]; then
      level="warning"
    fi
    if [[ -n "$ACTIVE_REPO" && -n "$ACTIVE_PYTHON" ]]; then
      send_webhook "Atomic release preparation failed: $LAST_UPDATE_ERROR. Launching previous release." "$level"
    else
      send_webhook "Atomic release preparation failed: $LAST_UPDATE_ERROR. No validated release is available." "$level"
    fi
    return 1
  fi
  return 0
}

start_release_prune() {
  PRUNE_PID=""
  if [[ ! -f "$PRUNE_REQUEST_PATH" ]]; then
    return
  fi
  rm -f "$PRUNE_RESULT_PATH"
  "$ACTIVE_PYTHON" "$RELEASE_HELPER" \
    --prune-only \
    --release-root "$RELEASE_ROOT" \
    --result-json "$PRUNE_RESULT_PATH" &
  PRUNE_PID=$!
}

report_release_prune_result() {
  local exit_code="${1:-0}"
  if [[ ! -f "$PRUNE_RESULT_PATH" ]]; then
    send_webhook "Atomic release cleanup exited with code $exit_code without a result." "warning"
    return
  fi
  if [[ "$exit_code" -ne 0 ]]; then
    send_webhook "Atomic release cleanup process exited with code $exit_code." "warning"
  fi
  if ! prune_warnings="$($ACTIVE_PYTHON - "$PRUNE_RESULT_PATH" 2>/dev/null <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    result = json.load(handle)
print(" | ".join(result.get("warnings") or []).replace("\n", " "))
PY
  )"; then
    send_webhook "Atomic release cleanup result is invalid." "warning"
    return
  fi
  if [[ -n "$prune_warnings" ]]; then
    send_webhook "Atomic release cleanup warning: $prune_warnings" "warning"
  fi
}

complete_release_prune() {
  if [[ -z "$PRUNE_PID" ]]; then
    return
  fi
  local exit_code=0
  wait "$PRUNE_PID" 2>/dev/null || exit_code=$?
  PRUNE_PID=""
  report_release_prune_result "$exit_code"
}

stop_release_prune() {
  local attempt=0
  while [[ -n "$PRUNE_PID" ]] && kill -0 "$PRUNE_PID" 2>/dev/null && (( attempt < 10 )); do
    sleep 0.1
    attempt=$((attempt + 1))
  done
  if [[ -n "$PRUNE_PID" ]] && kill -0 "$PRUNE_PID" 2>/dev/null; then
    kill "$PRUNE_PID" 2>/dev/null || true
  fi
  complete_release_prune
}

if [[ ! -d "$REPO_PATH" ]]; then
  send_webhook "Haniel repo not found at $REPO_PATH" "error"
  echo "Repo not found: $REPO_PATH" >&2
  exit 1
fi

EXIT_SELF_UPDATE=10
EXIT_RESTART=11
skip_update=false
write_self_update_marker=false
use_prepared_update=false
crash_restart_count=0
CHILD_PID=""
WATCHDOG_PID=""
PRUNE_PID=""

stop_wrapper() {
  stop_release_prune
  if [[ -n "$WATCHDOG_PID" ]] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
  fi
  if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
  fi
  exit 0
}

trap stop_wrapper SIGINT SIGTERM

start_self_update_watchdog() {
  local child_pid="$1"
  (
    while kill -0 "$child_pid" 2>/dev/null; do
      if [[ -f "$SELF_UPDATE_EXIT_MARKER" ]]; then
        sleep "$SELF_UPDATE_EXIT_TIMEOUT"
        if kill -0 "$child_pid" 2>/dev/null; then
          echo "[haniel-runner] Self-update process did not exit within ${SELF_UPDATE_EXIT_TIMEOUT}s; sending SIGKILL."
          mkdir -p "$ROOT_DIR/.local"
          : > "$FORCED_SELF_UPDATE_MARKER"
          kill -KILL "$child_pid" 2>/dev/null || true
        fi
        return
      fi
      sleep 1
    done
  ) &
  WATCHDOG_PID=$!
}

next_crash_delay() {
  local delay="$CRASH_RESTART_BASE_SECONDS"
  local attempt=1
  while (( attempt < crash_restart_count && delay < CRASH_RESTART_MAX_SECONDS )); do
    delay=$((delay * 2))
    if (( delay > CRASH_RESTART_MAX_SECONDS )); then
      delay="$CRASH_RESTART_MAX_SECONDS"
    fi
    attempt=$((attempt + 1))
  done
  printf '%s\n' "$delay"
}

while true; do
  if [[ "$skip_update" != "true" ]]; then
    echo "[haniel-runner] Preparing atomic Haniel release..."
    LAST_UPDATE_ERROR=""
    LAST_UPDATE_STARTED_AT="$("$PYTHON_BIN" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
    LAST_UPDATE_FINISHED_AT=""
    update_haniel_release || true
    LAST_UPDATE_FINISHED_AT="$("$PYTHON_BIN" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
    if [[ "$write_self_update_marker" == "true" ]]; then
      write_self_update_marker "$PREPARATION_OK"
    fi
    write_self_update_marker=false

    if [[ -z "$ACTIVE_REPO" || -z "$ACTIVE_PYTHON" || -z "$ACTIVE_COMMIT" || ! -x "$ACTIVE_PYTHON" ]]; then
      message="No previously validated Haniel release is available; refusing to launch."
      echo "[haniel-runner] $message" >&2
      send_webhook "$message" "error"
      exit 1
    fi

    active_runner="$ACTIVE_REPO/haniel-runner.sh"
    if [[ "$PREPARATION_SWITCHED" == "true" && -f "$active_runner" ]]; then
      active_runner_path="$(readlink -f "$active_runner")"
      if [[ "$RUNNER_SCRIPT_PATH" != "$active_runner_path" ]]; then
        echo "[haniel-runner] Re-executing current release wrapper."
        exec "$active_runner"
      fi
    fi
  fi
  skip_update=false

  echo "[haniel-runner] Launching haniel..."
  rm -f "$SELF_UPDATE_EXIT_MARKER" "$FORCED_SELF_UPDATE_MARKER"
  launch_started_at="$(date +%s)"
  HANIEL_ACTIVE_SELF_HEAD="$ACTIVE_COMMIT" \
    "$ACTIVE_PYTHON" -m haniel.cli run "$CONFIG_PATH" &
  CHILD_PID=$!
  start_release_prune
  start_self_update_watchdog "$CHILD_PID"
  while kill -0 "$CHILD_PID" 2>/dev/null; do
    if [[ -n "$PRUNE_PID" ]] && ! kill -0 "$PRUNE_PID" 2>/dev/null; then
      complete_release_prune
    fi
    sleep 0.2
  done
  wait "$CHILD_PID"
  exit_code=$?
  CHILD_PID=""
  if [[ -n "$WATCHDOG_PID" ]] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
  fi
  wait "$WATCHDOG_PID" 2>/dev/null || true
  WATCHDOG_PID=""
  stop_release_prune
  echo "[haniel-runner] haniel exited with code: $exit_code"

  launch_runtime=$(( $(date +%s) - launch_started_at ))
  if (( launch_runtime >= CRASH_RESET_SECONDS )); then
    crash_restart_count=0
  fi

  if [[ "$exit_code" -eq 0 ]]; then
    echo "[haniel-runner] Clean shutdown. Exiting wrapper."
    exit 0
  elif [[ "$exit_code" -eq "$EXIT_SELF_UPDATE" ]]; then
    crash_restart_count=0
    echo "[haniel-runner] Self-update requested. Looping..."
    send_webhook "Self-update initiated. Updating and restarting..." "info"
    write_self_update_marker=true
    use_prepared_update=true
    sleep 5
  elif [[ "$exit_code" -eq "$EXIT_RESTART" ]]; then
    crash_restart_count=0
    echo "[haniel-runner] Restart requested. Skipping update..."
    send_webhook "Restart initiated (no update)." "info"
    skip_update=true
    sleep 3
  else
    crash_restart_count=$((crash_restart_count + 1))
    restart_delay="$(next_crash_delay)"
    if [[ -f "$FORCED_SELF_UPDATE_MARKER" ]]; then
      echo "[haniel-runner] Forced self-update recovery after shutdown timeout."
      send_webhook "Self-update shutdown timed out; process was killed and will be updated." "error"
      write_self_update_marker=true
    else
      send_webhook "haniel exited with unexpected code $exit_code; recovering after ${restart_delay}s." "error"
    fi
    echo "[haniel-runner] Unexpected exit code $exit_code. Recovering after ${restart_delay}s..."
    sleep "$restart_delay"
  fi
done

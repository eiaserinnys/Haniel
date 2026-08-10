#!/usr/bin/env bash
# haniel-runner.sh - outer loop wrapper for Haniel self-update on Linux/systemd.
#
# Register this script as the systemd service ExecStart, not `python -m haniel`
# directly. Exit code 10 from Haniel means "self-update approved"; this wrapper
# then fetches, resets, reinstalls, writes the self-update result marker, and
# launches Haniel again.

set -u

ROOT_DIR="$(pwd)"
CONF_PATH="$ROOT_DIR/haniel-runner.conf"

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

resolve_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s/%s\n' "$ROOT_DIR" "$path"
  fi
}

REPO_PATH="$(resolve_path "$HANIEL_REPO")"
CONFIG_PATH="$(resolve_path "$CONFIG_FILE")"

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

STEP_NAMES=()
STEP_OKS=()
STEP_ERRORS=()
LAST_UPDATE_ERROR=""
LAST_UPDATE_STARTED_AT=""
LAST_UPDATE_FINISHED_AT=""

add_step() {
  local name="$1"
  local ok="$2"
  local error_message="${3:-}"

  if [[ -z "$error_message" && "$ok" != "true" ]]; then
    error_message="$name failed (no message)"
  fi

  STEP_NAMES+=("$name")
  STEP_OKS+=("$ok")
  STEP_ERRORS+=("$error_message")

  if [[ "$ok" != "true" && -z "$LAST_UPDATE_ERROR" ]]; then
    LAST_UPDATE_ERROR="$name failed: $error_message"
  fi
}

join_unit_sep() {
  local IFS=$'\x1f'
  printf '%s' "$*"
}

write_self_update_marker() {
  local ok="$1"
  local marker_path="$ROOT_DIR/.local/self_update_result.json"
  mkdir -p "$ROOT_DIR/.local"

  HANIEL_STEP_NAMES="$(join_unit_sep "${STEP_NAMES[@]}")" \
  HANIEL_STEP_OKS="$(join_unit_sep "${STEP_OKS[@]}")" \
  HANIEL_STEP_ERRORS="$(join_unit_sep "${STEP_ERRORS[@]}")" \
  HANIEL_LAST_UPDATE_ERROR="$LAST_UPDATE_ERROR" \
  "$PYTHON_BIN" - "$marker_path" "$LAST_UPDATE_STARTED_AT" "$LAST_UPDATE_FINISHED_AT" "$ok" <<'PY'
import json
import os
import sys

marker_path, started_at, finished_at, ok_raw = sys.argv[1:5]
sep = "\x1f"
names = os.environ.get("HANIEL_STEP_NAMES", "").split(sep) if os.environ.get("HANIEL_STEP_NAMES") else []
oks = os.environ.get("HANIEL_STEP_OKS", "").split(sep) if os.environ.get("HANIEL_STEP_OKS") else []
errors = os.environ.get("HANIEL_STEP_ERRORS", "").split(sep) if os.environ.get("HANIEL_STEP_ERRORS") else []

steps = []
for index, name in enumerate(names):
    steps.append({
        "name": name,
        "ok": index < len(oks) and oks[index] == "true",
        "error": errors[index] if index < len(errors) and errors[index] else None,
    })

payload = {
    "version": 1,
    "started_at": started_at,
    "finished_at": finished_at,
    "ok": ok_raw == "true",
    "steps": steps,
    "error": os.environ.get("HANIEL_LAST_UPDATE_ERROR") or None,
}

with open(marker_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
PY
}

update_haniel_repo() {
  local git_failures=0
  local output=""

  while (( git_failures < MAX_GIT_FAILURES )); do
    output="$(git -C "$REPO_PATH" fetch origin 2>&1)"
    if [[ $? -eq 0 ]]; then
      break
    fi
    git_failures=$((git_failures + 1))
    echo "git fetch failed (attempt $git_failures/$MAX_GIT_FAILURES): $output" >&2
    sleep 5
  done

  if (( git_failures >= MAX_GIT_FAILURES )); then
    add_step "git_fetch" "false" "git fetch failed $MAX_GIT_FAILURES times"
    send_webhook "git fetch failed $MAX_GIT_FAILURES times. Launching with current code." "error"
    return 1
  fi
  add_step "git_fetch" "true"

  local branch
  branch="$(git -C "$REPO_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [[ -z "$branch" ]]; then
    branch="main"
  fi

  output="$(git -C "$REPO_PATH" reset --hard "origin/$branch" 2>&1)"
  if [[ $? -ne 0 ]]; then
    add_step "git_reset" "false" "${output:-git reset failed}"
    send_webhook "git reset --hard failed. Launching with current code." "warning"
    return 1
  fi
  add_step "git_reset" "true"

  output="$("$PYTHON_BIN" -m pip install -e "$REPO_PATH" 2>&1)"
  if [[ $? -ne 0 ]]; then
    add_step "pip_install" "false" "${output:-pip install failed}"
    send_webhook "pip install failed. Attempting to launch with previous code." "warning"
  else
    add_step "pip_install" "true"
  fi

  local dashboard_path="$REPO_PATH/dashboard"
  if [[ -d "$dashboard_path" ]]; then
    echo "[haniel-runner] Building dashboard..."
    if ! command -v pnpm >/dev/null 2>&1; then
      add_step "pnpm_install" "false" "pnpm not found"
      send_webhook "Dashboard pnpm install failed. pnpm not found." "warning"
      return 0
    fi

    output="$(pnpm --dir "$dashboard_path" install 2>&1)"
    if [[ $? -ne 0 ]]; then
      add_step "pnpm_install" "false" "${output:-pnpm install failed}"
      send_webhook "Dashboard pnpm install failed. Launching with previous build." "warning"
      return 0
    fi
    add_step "pnpm_install" "true"

    output="$(pnpm --dir "$dashboard_path" build 2>&1)"
    if [[ $? -ne 0 ]]; then
      add_step "pnpm_build" "false" "${output:-pnpm build failed}"
      send_webhook "Dashboard build failed. Launching with previous build." "warning"
    else
      add_step "pnpm_build" "true"
    fi
  fi

  return 0
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
crash_restart_count=0
SELF_UPDATE_EXIT_MARKER="$ROOT_DIR/.local/self_update_exit_requested"
FORCED_SELF_UPDATE_MARKER="$ROOT_DIR/.local/self_update_exit_forced"
CHILD_PID=""
WATCHDOG_PID=""

stop_wrapper() {
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
    echo "[haniel-runner] Updating haniel repository..."
    STEP_NAMES=()
    STEP_OKS=()
    STEP_ERRORS=()
    LAST_UPDATE_ERROR=""
    LAST_UPDATE_STARTED_AT="$("$PYTHON_BIN" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
    LAST_UPDATE_FINISHED_AT=""
    update_haniel_repo || true
    LAST_UPDATE_FINISHED_AT="$("$PYTHON_BIN" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
    update_ok=true
    if [[ -n "$LAST_UPDATE_ERROR" ]]; then
      update_ok=false
    fi
    if [[ "$write_self_update_marker" == "true" ]]; then
      write_self_update_marker "$update_ok"
    fi
    write_self_update_marker=false
  fi
  skip_update=false

  echo "[haniel-runner] Launching haniel..."
  rm -f "$SELF_UPDATE_EXIT_MARKER" "$FORCED_SELF_UPDATE_MARKER"
  launch_started_at="$(date +%s)"
  "$PYTHON_BIN" -m haniel.cli run "$CONFIG_PATH" &
  CHILD_PID=$!
  start_self_update_watchdog "$CHILD_PID"
  wait "$CHILD_PID"
  exit_code=$?
  CHILD_PID=""
  if [[ -n "$WATCHDOG_PID" ]] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
  fi
  wait "$WATCHDOG_PID" 2>/dev/null || true
  WATCHDOG_PID=""
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

#!/bin/bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PID_FILE="/tmp/horse-race-app-$(id -u).pid"
STARTUP_LOG="/tmp/horse-race-saturday.log"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >> "$STARTUP_LOG"
}

is_this_app_process() {
  local pid="$1" command cwd
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *flask_app.py* ]] || return 1
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  [[ "$cwd" == "$REPO_DIR" ]]
}

if [[ ! -f "$PID_FILE" ]]; then
  log "No tracked app process; nothing to stop."
  exit 0
fi

pid="$(cat "$PID_FILE")"
if ! is_this_app_process "$pid"; then
  log "PID $pid is no longer this repository's Flask process; leaving it untouched."
  rm -f "$PID_FILE"
  exit 0
fi

log "Requesting graceful termination of app process $pid."
kill -TERM "$pid"
for _ in $(seq 1 30); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    log "App process $pid stopped cleanly."
    exit 0
  fi
  if ! is_this_app_process "$pid"; then
    rm -f "$PID_FILE"
    log "App process $pid exited or changed identity after termination request."
    exit 0
  fi
  sleep 1
done

log "App process $pid is still running after 30 seconds; no force kill was sent."
exit 1

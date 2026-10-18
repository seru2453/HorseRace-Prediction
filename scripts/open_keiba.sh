#!/bin/bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
APP_URL="http://localhost:8000"
APP_LOG="/tmp/horse-race-app.log"
STARTUP_LOG="/tmp/horse-race-saturday.log"
PID_FILE="/tmp/horse-race-app-$(id -u).pid"

export PATH="/opt/anaconda3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

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

record_app_pid() {
  local pid
  while IFS= read -r pid; do
    if is_this_app_process "$pid"; then
      printf '%s\n' "$pid" > "$PID_FILE"
      log "Tracking app process $pid in $PID_FILE."
      return 0
    fi
  done < <(lsof -nP -tiTCP:8000 -sTCP:LISTEN 2>/dev/null || true)
  rm -f "$PID_FILE"
  log "Could not safely identify this repository's Flask process; automatic stop will skip it."
  return 1
}

if curl --connect-timeout 1 --max-time 3 --fail --silent "$APP_URL/api/config" >/dev/null; then
  log "Application already responds at $APP_URL; skipping server start."
  record_app_pid || true
else
  if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    log "Port 8000 is occupied but the app health check failed; refusing to start another server."
    exit 1
  fi

  log "Starting make dev from $REPO_DIR."
  cd "$REPO_DIR"
  nohup make dev >> "$APP_LOG" 2>&1 </dev/null &
  app_pid=$!
  log "Started make dev with pid $app_pid; waiting for $APP_URL/api/config."

  ready=0
  for _ in $(seq 1 120); do
    if curl --connect-timeout 1 --max-time 3 --fail --silent "$APP_URL/api/config" >/dev/null; then
      ready=1
      break
    fi
    if ! kill -0 "$app_pid" 2>/dev/null && ! pgrep -f '[f]lask_app.py' >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  if [[ "$ready" -ne 1 ]]; then
    log "Application did not become healthy; inspect $APP_LOG."
    exit 1
  fi
  log "Application is responding at $APP_URL."
  if ! record_app_pid; then
    log "Server started, but its process could not be tracked for automatic stop."
  fi
fi

if open "$APP_URL"; then
  log "Opened $APP_URL in the default browser."
else
  log "Failed to open the default browser for $APP_URL."
  exit 1
fi

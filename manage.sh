#!/usr/bin/env bash
# Reliable start/stop/restart/status for main.py and server.py.
#
# Why this exists: on 2026-09-01, main.py was restarted repeatedly using
# `nohup ... &` plus a hand-typed `pkill -f "python3 main.py"` that never
# actually matched the real process command line -- every "restart"
# silently left the old process running and started a new one alongside
# it. By the time this was caught, 9 duplicate processes had been writing
# to the same SQLite file for hours. This script (plus the in-app lock in
# singleton_lock.py) makes that specific mistake impossible: it always
# operates on an exact PID it wrote itself, never a guessed pattern.
#
# Usage:
#   ./manage.sh start   [main|server|both]   (default: both)
#   ./manage.sh stop    [main|server|both]
#   ./manage.sh restart [main|server|both]
#   ./manage.sh status
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="./venv/bin/python3"
RUN_DIR="."  # PID/log files live alongside the repo, matching singleton_lock.py's config.BASE_DIR

pidfile_for() { echo "$RUN_DIR/$1.pid"; }
logfile_for() { echo "$RUN_DIR/$1.out.log"; }

is_running() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

status_one() {
  local name="$1"
  local pidfile
  pidfile=$(pidfile_for "$name")
  if [ -f "$pidfile" ] && is_running "$(cat "$pidfile")"; then
    echo "$name: running (pid $(cat "$pidfile"))"
  else
    echo "$name: not running"
  fi
}

stop_one() {
  local name="$1"
  local pidfile
  pidfile=$(pidfile_for "$name")
  if [ -f "$pidfile" ]; then
    local pid
    pid=$(cat "$pidfile")
    if is_running "$pid"; then
      echo "Stopping $name (pid $pid)..."
      kill "$pid"
      for _ in $(seq 1 20); do
        is_running "$pid" || break
        sleep 0.5
      done
      if is_running "$pid"; then
        echo "$name (pid $pid) didn't stop gracefully, forcing..."
        kill -9 "$pid" || true
      fi
    fi
    rm -f "$pidfile"
  else
    echo "$name: no pidfile, nothing to stop"
  fi
}

start_one() {
  local name="$1"
  local script="$2"
  local pidfile
  pidfile=$(pidfile_for "$name")
  local logfile
  logfile=$(logfile_for "$name")

  if [ -f "$pidfile" ] && is_running "$(cat "$pidfile")"; then
    echo "$name already running (pid $(cat "$pidfile")) -- refusing to start a second one."
    return 1
  fi
  rm -f "$pidfile"

  nohup "$PYTHON" "$script" > "$logfile" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$pidfile"
  echo "$name started (pid $new_pid), logging to $logfile"
}

cmd="${1:-}"
target="${2:-both}"

case "$cmd" in
  start)
    if [ "$target" = "main" ] || [ "$target" = "both" ]; then start_one main main.py; fi
    if [ "$target" = "server" ] || [ "$target" = "both" ]; then start_one server server.py; fi
    ;;
  stop)
    if [ "$target" = "main" ] || [ "$target" = "both" ]; then stop_one main; fi
    if [ "$target" = "server" ] || [ "$target" = "both" ]; then stop_one server; fi
    ;;
  restart)
    "$0" stop "$target"
    "$0" start "$target"
    ;;
  status)
    status_one main
    status_one server
    ;;
  *)
    echo "Usage: $0 {start|stop|restart} [main|server|both]   OR   $0 status"
    exit 1
    ;;
esac
exit 0

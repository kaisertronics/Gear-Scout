#!/bin/bash
# Keeps Gear Scout's local helper processes alive: the builder routine loop, the
# status pusher and the status server. Checks every 2 minutes, relaunches what died.
# Start once: nohup scripts/supervisor.sh >/dev/null 2>&1 &
cd "$(dirname "$0")/.." || exit 1
LOG=$HOME/Library/Logs/gear-scout/supervisor.log
up() { pgrep -f "$1" >/dev/null 2>&1; }
start() { # pattern, command...
  pat=$1; shift
  if ! up "$pat"; then
    echo "$(date '+%F %T') relaunching $pat" >> "$LOG"
    nohup "$@" >/dev/null 2>&1 &
  fi
}
while true; do
  start "scripts/routine_loop.sh" scripts/routine_loop.sh
  start "scripts/status_push.sh" scripts/status_push.sh
  start "scripts/status_server.py" python3 scripts/status_server.py
  sleep 120
done

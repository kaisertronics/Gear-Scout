#!/bin/bash
# Long-lived scheduler for the Gear Scout routines on the second Claude plan. Start in a Terminal tab of the Claude app:
#   /Users/blakekaiser/Documents/gear-scout/scripts/routine_loop.sh
# (launchd/cron can't read ~/Documents, so this runs from an app terminal.)
DIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$HOME/Library/Logs/gear-scout"
LOCK="$LOGDIR/plan2.lock"
STATE="$LOGDIR/plan2_state"      # lines: "<routine> <slot>"
mkdir -p "$LOGDIR"; touch "$STATE"
pending=""

last_slot() { awk -v r="$1" '$1==r{s=$2} END{print s}' "$STATE"; }

# slot id for a routine if due now (empty if not). Slots: hourly = date+hour, others = date.
slot_due() {
  hm=$(date +%H%M); d=$(date +%Y%m%d); h=$(date +%H); m=$((10#$(date +%M)))
  case "$1" in
    hourly) [ "$m" -ge 27 ] && echo "$d$h" ;;
    nightly) [ "$hm" -ge 1010 ] && [ "$hm" -lt 1310 ] && echo "$d" ;;
    code-review) [ "$hm" -ge 1130 ] && [ "$hm" -lt 1430 ] && echo "$d" ;;
  esac
}

lock_busy() {
  [ -d "$LOCK" ] || return 1
  age=$(( $(date +%s) - $(stat -f %m "$LOCK") ))
  pid=$(cat "$LOCK/pid" 2>/dev/null)
  if [ "$age" -gt 10800 ] || { [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; }; then rm -rf "$LOCK"; return 1; fi
  return 0
}

# earliest time either account's limit lifts (epoch), 0 if neither is limited
earliest_wait() {
  now=$(date +%s); best=0
  for a in plan2 plan1; do
    u=$(cat "$LOGDIR/${a}_limited_until" 2>/dev/null)
    if [ -n "$u" ] && [ "$u" -gt "$now" ] && { [ "$best" -eq 0 ] || [ "$u" -lt "$best" ]; }; then best=$u; fi
  done
  echo "$best"
}

case "$1" in -h|--help) sed -n '2,3p' "$0"; exit 0;; esac
echo "$(date) routine loop started"
wait_until=0
while true; do
  if [ "$(date +%s)" -ge "$wait_until" ] && ! lock_busy; then
    for r in hourly nightly code-review; do
      s=$(slot_due "$r")
      [ -n "$s" ] && [ "$(last_slot "$r")" != "$s" ] || continue
      mkdir "$LOCK" 2>/dev/null || break
      echo "$$" > "$LOCK/pid"
      echo "$r $s" >> "$STATE"
      echo "$(date) starting $r"
      "$DIR/run_routine.sh" "$r"; rc=$?
      echo "$(date) finished $r rc=$rc"
      rm -rf "$LOCK"
      if [ "$rc" -eq 75 ]; then
        # both accounts out of usage (or plan 1 reserved): un-run the slot, retry once a limit lifts
        grep -v "^$r $s\$" "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"
        w=$(earliest_wait); [ "$w" -eq 0 ] && w=$(( $(date +%s) + 300 ))
        wait_until=$w
      fi
      break
    done
  fi
  sleep 60
done

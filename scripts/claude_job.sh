#!/bin/bash
# Run a headless Claude job on whichever account has usage: plan 2 (info@, preferred) then plan 1 (owner's main, backup).
# Usage: scripts/claude_job.sh <job-name> <timeout-minutes> <prompt-file>
# Exit: claude's exit code; 75 = both accounts out of usage (or plan 1 reserved for the owner's chats) - nothing ran.
# Stdout: path of the log file used. Can be sourced to reuse detect_limit.
LOGDIR="$HOME/Library/Logs/gear-scout"
CLAUDE="/Users/blakekaiser/.local/bin/claude"
RUNS="$LOGDIR/accounts_runs.log"

# detect_limit <outputfile>: exit 0 and print the epoch the limit lifts if the output is a usage/rate-limit message.
# Wordings seen: "Claude AI usage limit reached|<epoch>", "You've hit your limit · resets 3pm (America/Los_Angeles)",
# "5-hour limit reached ∙ resets 3pm". Only the tail is checked so routine prose can't trigger it.
detect_limit() {
  f="$1"; [ -f "$f" ] || return 1
  tailtxt=$(tail -c 700 "$f" | tr '\n' ' ')
  echo "$tailtxt" | grep -Eiq "usage limit reached|hit your (usage )?limit|limit reached|rate limit|usage limit|out of (extra )?usage" || return 1
  now=$(date +%s); until=""
  ep=$(echo "$tailtxt" | sed -nE 's/.*limit reached\|([0-9]{10}).*/\1/p')
  if [ -n "$ep" ]; then until="$ep"; else
    t=$(echo "$tailtxt" | sed -nE 's/.*[Rr]esets( at)? ([0-9]{1,2})(:([0-9]{2}))?[ ]?([ap]m).*/\2 \4 \5/p')
    if [ -n "$t" ]; then
      set -- $t; h=$1; if [ $# -eq 3 ]; then m=$2; ap=$3; else m=00; ap=$2; fi
      cand=$(date -j -f "%Y-%m-%d %I:%M%p" "$(date +%Y-%m-%d) $h:$m$ap" +%s 2>/dev/null)
      if [ -n "$cand" ]; then [ "$cand" -le "$now" ] && cand=$((cand+86400)); until=$((cand+120)); fi
    fi
  fi
  if [ -z "$until" ] || [ "$until" -le "$now" ] || [ "$until" -gt $((now+8*3600)) ]; then until=$((now+5*3600)); fi
  echo "$until"; return 0
}

is_limited() { u=$(cat "$LOGDIR/$1_limited_until" 2>/dev/null); [ -n "$u" ] && [ "$u" -gt "$(date +%s)" ]; }

# pick_account <skip>: prints plan2 or plan1, or nothing. plan1 refused while reserved for the owner.
pick_account() {
  if [ "$1" != plan2 ] && ! is_limited plan2; then echo plan2; return 0; fi
  if [ "$1" != plan1 ] && ! is_limited plan1; then
    r="$LOGDIR/plan1_reserved"
    if [ -f "$r" ] && [ $(( $(date +%s) - $(stat -f %m "$r") )) -lt 600 ]; then return 1; fi
    echo plan1; return 0
  fi
  return 1
}

run_once() { # account log timeout_seconds promptfile -> sets code
  if [ "$1" = plan2 ]; then export CLAUDE_CONFIG_DIR="$HOME/.claude-plan2"; else unset CLAUDE_CONFIG_DIR; fi
  prompt=$(cat "$4")
  "$CLAUDE" -p --model "${CLAUDE_JOB_MODEL:-sonnet}" --permission-mode bypassPermissions "$prompt" >"$2" 2>&1 </dev/null &
  pid=$!
  ( sleep "$3"; kill -TERM "$pid" 2>/dev/null; sleep 20; kill -KILL "$pid" 2>/dev/null ) &
  wd=$!
  wait "$pid"; code=$?
  kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
}

claude_job_main() {
  name="$1"; tmo=$(( $2 * 60 )); pf="$3"
  [ -n "$name" ] && [ -n "$2" ] && [ -f "$pf" ] || { echo "usage: $0 <job-name> <timeout-minutes> <prompt-file>" >&2; return 2; }
  mkdir -p "$LOGDIR"
  [ -f "$LOGDIR/plan2_runs.log" ] && [ ! -f "$RUNS" ] && mv "$LOGDIR/plan2_runs.log" "$RUNS"
  find "$LOGDIR" \( -name 'job-*.log' -o -name 'plan2-*.log' \) -mtime +14 -delete 2>/dev/null
  skip=""; switched=""; stamp=$(date +%Y%m%d-%H%M)
  while :; do
    acct=$(pick_account "$skip") || { echo "$(date '+%Y-%m-%d %H:%M:%S') $name none exit=75 both accounts out of usage" >> "$RUNS"; echo "both accounts out of usage" >&2; return 75; }
    log="$LOGDIR/job-$name-$stamp-$acct.log"; start=$(date +%s)
    run_once "$acct" "$log" "$tmo" "$pf"; dur=$(( $(date +%s) - start ))
    why=""
    if until=$(detect_limit "$log"); then why=limited
    elif [ "$code" -ne 0 ] && head -c 300 "$log" | grep -q "Not logged in"; then why=notloggedin; until=$(( $(date +%s) + 3600 )); fi
    if [ -n "$why" ]; then
      echo "$until" > "$LOGDIR/${acct}_limited_until"
      echo "$(date '+%Y-%m-%d %H:%M:%S') $name $acct exit=$code ${dur}s $why" >> "$RUNS"
      [ -n "$skip" ] && { echo "both accounts out of usage" >&2; return 75; }
      skip="$acct"; switched=" switched"; continue
    fi
    [ "$code" -eq 0 ] && touch "$LOGDIR/plan2_heartbeat"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $name $acct exit=$code ${dur}s ok$switched" >> "$RUNS"
    echo "$log"; return "$code"
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then claude_job_main "$@"; exit $?; fi

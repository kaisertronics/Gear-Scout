#!/bin/bash
# Run one Gear Scout routine (hourly|nightly|code-review) headlessly via claude_job.sh (plan 2 first, plan 1 as backup).
# Usage: scripts/run_routine.sh <routine>      Exit 75 = both accounts out of usage (nothing ran).
DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="/Users/blakekaiser/Documents/gear-scout"
SKILLS="$HOME/.claude/scheduled-tasks"
LOGDIR="$HOME/Library/Logs/gear-scout"

NOTE='HEADLESS NOTE: you are running via `claude -p` on a background Claude account, not as a desktop-app scheduled task. The mcp__scheduled-tasks__*, mcp__ccd_session_mgmt__* and other app tools are NOT available: skip the BUDGET GUARD and the stuck-run check steps entirely. Everything else applies as written.'

# Body of SKILL.md: frontmatter and the PLAN-2 CHECK paragraph removed, HEADLESS NOTE prepended.
build_prompt() {
  awk -v note="$NOTE" '
    BEGIN{fm=0; skip=0; print note; print ""}
    fm<2 { if ($0=="---") fm++; next }
    skip { if ($0 ~ /^[[:space:]]*$/) skip=0; next }
    /^PLAN-2 CHECK:/ { skip=1; next }
    { print }' "$1"
}

run_main() {
  r="$1"
  case "$r" in hourly) tmo=75;; block) tmo=150;; nightly|code-review) tmo=120;; *) echo "usage: $0 hourly|nightly|code-review|block" >&2; exit 2;; esac
  mkdir -p "$LOGDIR"; cd "$PROJECT" || exit 2
  pf="$LOGDIR/prompt-$r.txt"
  if [ "$r" = block ]; then
    { echo "$NOTE"; echo; cat "$DIR/block_prompt.txt"; } > "$pf"
  else
    skill="$SKILLS/gear-scout-$r/SKILL.md"; [ -f "$skill" ] || { echo "missing $skill" >&2; exit 2; }
    build_prompt "$skill" > "$pf"
  fi
  "$DIR/claude_job.sh" "$r" "$tmo" "$pf" >/dev/null; code=$?
  [ "$code" -eq 0 ] && [ "$r" != hourly ] && touch "$LOGDIR/plan2_heartbeat_$r"
  exit "$code"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then run_main "$1"; fi

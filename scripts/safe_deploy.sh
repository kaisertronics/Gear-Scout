#!/bin/sh
# Rebuild and restart Gear Scout — but never while one of your searches or
# scrapes is running (a restart would cut it off), and never in the middle
# of a scheduled run. Usage: scripts/safe_deploy.sh [dashboard|scout|both]
set -e
cd "$(dirname "$0")/.."
target="${1:-both}"
for f in manual_scrape_status live_search_status lowest_status fbposts_status telex_status; do
  if docker exec gear-scout-dashboard sh -c "grep -q '\"running\"' /data/$f.json 2>/dev/null"; then
    echo "Not deploying: a search/scrape from the dashboard is running ($f). Try again when it finishes."
    exit 1
  fi
done
# Don't restart while you're using Gear Scout (a page opened in the last 3
# minutes): the site would drop out for ~30-60s under you.
if [ "$target" != "scout" ] && [ -z "$FORCE_DEPLOY" ]; then
  idle=$(docker exec gear-scout-dashboard sh -c 'f=/data/last_user_request; [ -f $f ] && echo $(( $(date +%s) - $(stat -c %Y $f) )) || echo 9999' 2>/dev/null || echo 9999)
  if [ "$idle" -lt 180 ]; then
    echo "Not deploying: you're using Gear Scout right now (last page ${idle}s ago). Try again in a few minutes."
    exit 1
  fi
fi
if [ "$target" != "dashboard" ]; then
  last_start=$(docker compose logs scout --since 30m 2>&1 | grep -c "Gear Scout run #" || true)
  last_done=$(docker compose logs scout --since 30m 2>&1 | grep -c "complete\." || true)
  if [ "$last_start" -gt "$last_done" ]; then
    echo "Not deploying: a scheduled scrape is in progress. Try again when it finishes."
    exit 1
  fi
fi
if [ "$target" != "dashboard" ]; then
  # The 15-minute Mic Focus / Wanted sweep isn't a "scheduled scrape" above:
  # don't cut it off either (Nov 1: a deploy did, mid-sweep).
  quiet=$(docker exec gear-scout python -c "import sqlite3,datetime as d;c=sqlite3.connect('/data/seen_listings.db');t=c.execute('select max(searched_at) from wanted_sweep').fetchone()[0];print(int((d.datetime.now(d.timezone.utc)-d.datetime.fromisoformat(t)).total_seconds()) if t else 9999)" 2>/dev/null || echo 9999)
  if [ "$quiet" -lt 120 ]; then
    echo "Not deploying: the 15-minute sweep is running (last search ${quiet}s ago). Try again in a few minutes."
    exit 1
  fi
fi
case "$target" in
  dashboard) docker compose build dashboard >/dev/null && docker compose up -d dashboard ;;
  scout) docker compose build scout >/dev/null && docker compose up -d scout ;;
  *) docker compose build dashboard scout >/dev/null && docker compose up -d dashboard scout ;;
esac
echo "Deployed ($target)."

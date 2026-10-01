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
if [ "$target" != "dashboard" ]; then
  last_start=$(docker compose logs scout --since 30m 2>&1 | grep -c "Gear Scout run #" || true)
  last_done=$(docker compose logs scout --since 30m 2>&1 | grep -c "complete\." || true)
  if [ "$last_start" -gt "$last_done" ]; then
    echo "Not deploying: a scheduled scrape is in progress. Try again when it finishes."
    exit 1
  fi
fi
case "$target" in
  dashboard) docker compose build dashboard >/dev/null && docker compose up -d dashboard ;;
  scout) docker compose build scout >/dev/null && docker compose up -d scout ;;
  *) docker compose build dashboard scout >/dev/null && docker compose up -d dashboard scout ;;
esac
echo "Deployed ($target)."

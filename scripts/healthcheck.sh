#!/bin/sh
# Gear Scout health check, run by launchd every 5 min (com.gearscout.healthcheck). No AI, no emails.
# Env: DRY_RUN=1 logs "would ..." instead of acting; HC_DASH_URL overrides the dashboard URL;
#      HC_SKIP_WAIT=1 skips the 60 s recheck (testing).
PATH=/Users/blakekaiser/.docker/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin
export PATH
DOCKER=/Users/blakekaiser/.docker/bin/docker
PROJ=/Users/blakekaiser/Documents/gear-scout
LOGDIR=/Users/blakekaiser/Library/Logs/gear-scout
LOG=$LOGDIR/healthcheck.log
WLOG=$PROJ/docs/watchdog_log.md
DASH_URL=${HC_DASH_URL:-http://localhost:8420/login}
DRY=${DRY_RUN:-0}
mkdir -p "$LOGDIR"
# launchd jobs are blocked (TCC) from ~/Documents, so the installed copy lives in ~/Library/gear-scout;
# anything needing the project dir is best-effort.
HAVE_PROJ=0; cd "$PROJ" 2>/dev/null && ls docker-compose.y*ml compose.y*ml >/dev/null 2>&1 && HAVE_PROJ=1

now() { date +%s; }
log() {
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  [ "$DRY" = 1 ] && pre="[dry-run] " || pre=""
  echo "$ts $pre$*" >> "$LOG"
  if [ "$DRY" != 1 ]; then
    [ "$HAVE_PROJ" = 1 ] && [ -d "$PROJ/docs" ] && echo "- $ts healthcheck: $*" >> "$WLOG" 2>/dev/null
  fi
  # keep log small
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1000000 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}
# stamp_ok FILE SECONDS -> true if stamp missing or older than SECONDS
stamp_ok() {
  [ -f "$1" ] || return 0
  last=$(cat "$1" 2>/dev/null); last=${last:-0}
  [ $(( $(now) - last )) -ge "$2" ]
}

# 1. Docker answering?
if ! $DOCKER info >/dev/null 2>&1; then
  if stamp_ok "$LOGDIR/docker_open.stamp" 3600; then
    log "docker not answering; opening Docker Desktop"
    [ "$DRY" = 1 ] || { now > "$LOGDIR/docker_open.stamp"; open -a Docker; }
  else
    log "docker not answering (already tried to open it within the hour)"
  fi
  exit 0
fi

busy_deploy() { pgrep -f 'safe_deploy.sh' >/dev/null 2>&1 || pgrep -f 'docker compose build' >/dev/null 2>&1 || pgrep -f 'docker-compose build' >/dev/null 2>&1; }

do_restart() { # service reason
  if busy_deploy; then log "skip restart $1 ($2): deploy/build in progress"; return; fi
  if ! stamp_ok "$LOGDIR/restart.stamp" 1800; then log "skip restart $1 ($2): restarted within last 30 min"; return; fi
  if [ "$DRY" = 1 ]; then log "would restart $1 ($2)"; return; fi
  log "restarting $1 ($2)"
  now > "$LOGDIR/restart.stamp"
  case "$1" in dashboard) cn=gear-scout-dashboard;; *) cn=gear-scout;; esac
  $DOCKER restart "$cn" >/dev/null 2>&1
}

# 2. Containers running?
running=$($DOCKER ps --format '{{.Names}}' 2>/dev/null)
for c in gear-scout gear-scout-dashboard; do
  if ! echo "$running" | grep -qx "$c"; then
    if busy_deploy; then log "skip compose up ($c down): deploy/build in progress"; continue; fi
    if [ "$DRY" = 1 ]; then log "would run docker compose up -d --no-build ($c not running)"
    else log "$c not running; starting it (docker start, else compose up -d --no-build)"; $DOCKER start "$c" >/dev/null 2>&1 || { [ "$HAVE_PROJ" = 1 ] && $DOCKER compose up -d --no-build >/dev/null 2>&1; }; fi
  fi
done

# 3. Sweep stalled?
if echo "$running" | grep -qx gear-scout; then
  fin=$($DOCKER exec gear-scout cat /data/last_sweep.json 2>/dev/null | sed -n 's/.*"finished"[^"]*"\([0-9T:-]*\).*/\1/p')
  if [ -n "$fin" ]; then
    fe=$(date -j -u -f '%Y-%m-%dT%H:%M:%S' "$fin" +%s 2>/dev/null)
    if [ -n "$fe" ] && [ $(( $(now) - fe )) -gt 3000 ]; then
      recent=$($DOCKER logs --since 20m gear-scout 2>&1 | head -c 200)
      [ -z "$recent" ] && do_restart scout "last sweep finished $fin UTC (>50 min) and no logs in 20 min"
    fi
  fi
fi

# 4. Dashboard answering?
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$DASH_URL" 2>/dev/null)
if [ "$code" != 200 ]; then
  [ "${HC_SKIP_WAIT:-0}" = 1 ] || sleep 60
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$DASH_URL" 2>/dev/null)
  [ "$code" != 200 ] && do_restart dashboard "dashboard returned $code twice"
fi
exit 0

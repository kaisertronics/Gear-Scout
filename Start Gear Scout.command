#!/bin/bash
# Gear Scout — one double-click to set up and start on this computer.
# Safe to run again any time: it only restores your data the first time.
cd "$(dirname "$0")" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

echo "=== Gear Scout ==="
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Desktop isn't installed yet."
  echo "Opening the download page — install it, open it once, then double-click this file again."
  open "https://www.docker.com/products/docker-desktop/"
  read -r -p "Press Enter to close…" _
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop…"
  open -a Docker
  until docker info >/dev/null 2>&1; do sleep 3; done
fi

echo "Building Gear Scout (the first time takes about 5–10 minutes)…"
docker compose build || { echo "Build failed — see the messages above."; read -r -p "Press Enter to close…" _; exit 1; }

if [ -f migration/gear_scout_data.tgz ] && [ ! -f migration/.restored ]; then
  echo "Restoring your listings, favorites, settings history and Facebook login…"
  docker compose run --rm --no-deps --entrypoint sh -v "$PWD/migration:/backup" scout \
    -c "tar xzf /backup/gear_scout_data.tgz -C /data" && touch migration/.restored \
    || { echo "Restoring the data failed — see the messages above."; read -r -p "Press Enter to close…" _; exit 1; }
fi

echo "Starting Gear Scout…"
docker compose up -d
until curl -s -o /dev/null http://localhost:8420; do sleep 3; done
echo "Gear Scout is running: http://localhost:8420"
open "http://localhost:8420"
echo
echo "Your phone link (ngrok) works once Gear Scout is no longer running on the old computer."
read -r -p "Press Enter to close this window (Gear Scout keeps running)…" _

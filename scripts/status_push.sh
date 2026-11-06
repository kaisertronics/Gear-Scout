#!/bin/bash
# Pushes the builder status into the dashboard container every 15 s so /builder works from any computer. Run in a Terminal tab.
DIR="$(cd "$(dirname "$0")" && pwd)"
while true; do
  python3 "$DIR/agent_status.py" | docker exec -i gear-scout-dashboard sh -c 'cat > /data/builder_status.json.tmp && mv /data/builder_status.json.tmp /data/builder_status.json' 2>/dev/null
  sleep 15
done

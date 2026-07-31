#!/bin/bash
# Browserless fallback: polls Sentinel and relays pending notifications as native
# macOS notifications. Requires: brew install terminal-notifier.
# Loaded by launchd (see the .plist next to this script).
set -euo pipefail

STATE_DIR="$HOME/.cache/sentinel-notifier"
STATE_FILE="$STATE_DIR/last_seen"
BASE_URL="${SENTINEL_URL:-http://localhost:8300}"

mkdir -p "$STATE_DIR"
SINCE=$(cat "$STATE_FILE" 2>/dev/null || echo "1970-01-01T00:00:00Z")

NOTIFS=$(curl -fsS --max-time 5 "$BASE_URL/api/notifications/pending?since=$SINCE") || exit 0

echo "$NOTIFS" | BASE_URL="$BASE_URL" /usr/bin/python3 -c '
import json, os, subprocess, sys
base_url = os.environ["BASE_URL"]
notifs = json.load(sys.stdin)
for n in notifs:
    subprocess.run(["terminal-notifier", "-title", "Sentinel", "-subtitle", n["title"][:80], "-message", n.get("body") or n["reason"], "-open", n.get("url") or base_url], check=False)
if notifs:
    print(notifs[-1]["sent_at"], end="")
' > "$STATE_FILE.new"

if [ -s "$STATE_FILE.new" ]; then mv "$STATE_FILE.new" "$STATE_FILE"; else rm -f "$STATE_FILE.new"; fi

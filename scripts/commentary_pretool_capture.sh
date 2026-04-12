#!/usr/bin/env bash
# PreToolUse hook: capture the current emulator frame from the viewer and
# stash it so the PostToolUse commentary hook can timestamp commentary to
# the moment the tool was *invoked*, not when it *finished*.

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090}"
FRAME_FILE="/tmp/.commentary_pending_frame"

# Query the viewer's /status endpoint for the current frame.
RESP=$(curl -s --max-time 1 "$VIEWER_URL/status" 2>/dev/null) || exit 0
FRAME=$(echo "$RESP" | jq -r '.frame // empty' 2>/dev/null) || exit 0

if [ -n "$FRAME" ] && [ "$FRAME" != "null" ]; then
    echo "$FRAME" > "$FRAME_FILE"
fi

#!/usr/bin/env bash
# PreToolUse hook: immediately capture the current emulator frame before
# any MCP tool execution can advance it.  Stashes the value for the
# PostToolUse commentary hook to use.
#
# This MUST be fast — the whole point is to grab the frame before the
# tool runs.  No transcript parsing, no jq pipelines, just one curl.

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090}"
FRAME_FILE="/tmp/.commentary_pending_frame"

# Drain stdin (hook protocol requires reading it, even if we don't use it).
cat > /dev/null

# Query the viewer's /status endpoint for the current frame.
RESP=$(curl -s --max-time 1 "$VIEWER_URL/status" 2>/dev/null) || exit 0
FRAME=$(echo "$RESP" | jq -r '.frame // empty' 2>/dev/null) || exit 0

if [ -n "$FRAME" ] && [ "$FRAME" != "null" ]; then
    echo "$FRAME" > "$FRAME_FILE"
    echo "$(date '+%H:%M:%S') | PreToolUse captured frame=$FRAME" >> /tmp/commentary_send.log
fi

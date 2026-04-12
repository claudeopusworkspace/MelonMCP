#!/usr/bin/env bash
# Stop hook: send the assistant's latest response to the viewer as commentary.
# Uses last_assistant_message from the hook input (current turn's text),
# NOT the transcript (which lags by one turn).
# Fails silently if the viewer isn't running — commentary is best-effort.

set -euo pipefail

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090/commentary}"

INPUT=$(cat)
LAST_RESPONSE=$(echo "$INPUT" | jq -r '.last_assistant_message // empty')

# Skip empty responses (tool-only turns produce no text)
if [ -z "$LAST_RESPONSE" ]; then
    exit 0
fi

# POST to the viewer — timeout quickly, don't block the CLI
curl -sf -X POST "$VIEWER_URL" \
    -H "Content-Type: application/json" \
    --max-time 2 \
    -d "$(jq -n --arg text "$LAST_RESPONSE" '{text: $text, style: "normal"}')" \
    >/dev/null 2>&1 || true

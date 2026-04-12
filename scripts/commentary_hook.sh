#!/usr/bin/env bash
# Stop hook: extract the last assistant text from the transcript and POST it
# to the viewer's commentary endpoint.  Fails silently if the viewer isn't
# running — that's fine, commentary is best-effort.

set -euo pipefail

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090/commentary}"

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    exit 0
fi

# Extract text blocks from the last assistant message in the JSONL transcript.
# Each line is a JSON object; assistant messages have type "assistant" and
# content is an array of blocks.  We want only "text" type blocks.
LAST_RESPONSE=$(jq -s '
    [.[] | select(.type == "assistant")]
    | last
    | .message.content // []
    | map(select(.type == "text") | .text)
    | join("\n")
' "$TRANSCRIPT" 2>/dev/null || true)

# Skip empty responses (e.g. tool-only turns)
if [ -z "$LAST_RESPONSE" ] || [ "$LAST_RESPONSE" = "null" ]; then
    exit 0
fi

# POST to the viewer — timeout quickly, don't block the CLI
curl -sf -X POST "$VIEWER_URL" \
    -H "Content-Type: application/json" \
    --max-time 2 \
    -d "$(jq -n --arg text "$LAST_RESPONSE" '{text: $text, style: "normal"}')" \
    >/dev/null 2>&1 || true

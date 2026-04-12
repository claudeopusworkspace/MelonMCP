#!/usr/bin/env bash
# Stop hook: extract the last assistant text from the transcript and POST it
# to the viewer's commentary endpoint.  Fails silently if the viewer isn't
# running — that's fine, commentary is best-effort.

set -euo pipefail

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090/commentary}"
SENT_HASH_FILE="/tmp/.commentary_last_hash"

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    exit 0
fi

# Extract text blocks from the last assistant message that actually contains
# text.  Many turns are tool_use-only (no visible text), so we filter those
# out and grab the most recent message that has at least one text block.
LAST_RESPONSE=$(jq -s '
    [.[] | select(.type == "assistant")
         | select(.message.content | map(select(.type == "text")) | length > 0)]
    | last
    | .message.content // []
    | map(select(.type == "text") | .text)
    | join("\n")
' "$TRANSCRIPT" 2>/dev/null || true)

# Skip empty responses (e.g. tool-only turns)
if [ -z "$LAST_RESPONSE" ] || [ "$LAST_RESPONSE" = "null" ]; then
    exit 0
fi

# Dedup: hash the response and skip if we already sent this exact text.
# Prevents re-sending the same message on tool-only turns that don't
# produce new text.
HASH=$(echo "$LAST_RESPONSE" | md5sum | cut -d' ' -f1)
if [ -f "$SENT_HASH_FILE" ] && [ "$(cat "$SENT_HASH_FILE")" = "$HASH" ]; then
    exit 0
fi
echo "$HASH" > "$SENT_HASH_FILE"

# POST to the viewer — timeout quickly, don't block the CLI
curl -sf -X POST "$VIEWER_URL" \
    -H "Content-Type: application/json" \
    --max-time 2 \
    -d "$(jq -n --arg text "$LAST_RESPONSE" '{text: $text, style: "normal"}')" \
    >/dev/null 2>&1 || true

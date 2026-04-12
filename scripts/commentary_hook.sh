#!/usr/bin/env bash
# PreToolUse hook: send the assistant's latest text to the viewer as commentary.
# Parses the transcript for the most recent text-bearing assistant message,
# deduplicates via md5 hash to avoid re-sending on consecutive tool calls.
# Fails silently if the viewer isn't running — commentary is best-effort.

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090/commentary}"
SENT_HASH_FILE="/tmp/.commentary_last_hash"

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    exit 0
fi

# Find the last assistant message that contains at least one text block.
LAST_RESPONSE=$(jq -s '
    [.[] | select(.type == "assistant")
         | select(.message.content | map(select(.type == "text")) | length > 0)]
    | last
    | .message.content // []
    | map(select(.type == "text") | .text)
    | join("\n")
' "$TRANSCRIPT" 2>/dev/null || true)

# Skip empty responses
if [ -z "$LAST_RESPONSE" ] || [ "$LAST_RESPONSE" = "null" ]; then
    exit 0
fi

# Dedup: skip if we already sent this exact text.
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

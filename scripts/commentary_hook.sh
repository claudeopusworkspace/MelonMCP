#!/usr/bin/env bash
# PreToolUse/Stop hook: send the assistant's latest text to the viewer as
# commentary.  Deduplicates via md5 hash.
# Fails silently if the viewer isn't running — commentary is best-effort.

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090/commentary}"
SENT_HASH_FILE="/tmp/.commentary_last_hash"
SEND_LOG="/tmp/commentary_send.log"

INPUT=$(cat)

HOOK_EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // empty')
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // empty')

TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    exit 0
fi

# Find the last assistant message that contains text.
LAST_RESPONSE=$(jq -sr '
    [.[] | select(.type == "assistant")
         | select((.message.content | type) == "array")
         | select(.message.content | map(select(.type == "text")) | length > 0)]
    | last
    | .message.content
    | map(select(.type == "text") | .text)
    | join("\n")
' "$TRANSCRIPT" 2>/dev/null || true)

# On Stop, prefer last_assistant_message (has current turn's text)
if [ "$HOOK_EVENT" = "Stop" ] && [ -n "$LAST_MSG" ]; then
    LAST_RESPONSE="$LAST_MSG"
fi

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

# Build payload — on PreToolUse the viewer's current frame is already correct
# (the tool hasn't run yet). On Stop we also use the current frame.
PAYLOAD=$(jq -n --arg text "$LAST_RESPONSE" '{text: $text, style: "normal"}')

# Log what we're sending
echo "$(date '+%H:%M:%S') | event=$HOOK_EVENT hash=$HASH" >> "$SEND_LOG"
echo "  text: ${LAST_RESPONSE:0:120}" >> "$SEND_LOG"

# POST to the viewer
HTTP_CODE=$(curl -s -o /tmp/.commentary_last_response -w '%{http_code}' -X POST "$VIEWER_URL" \
    -H "Content-Type: application/json" \
    --max-time 2 \
    -d "$PAYLOAD" 2>&1) || true

echo "  http: $HTTP_CODE response: $(cat /tmp/.commentary_last_response 2>/dev/null)" >> "$SEND_LOG"
echo "" >> "$SEND_LOG"

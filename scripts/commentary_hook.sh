#!/usr/bin/env bash
# PreToolUse/Stop hook: send the assistant's latest text to the viewer as
# commentary, timestamped to the correct frame (retroactively if needed).
#
# Parses the transcript for the most recent text-bearing assistant message,
# then finds the total_frame from the nearest preceding tool_result so the
# commentary appears at the right point in the stream timeline.
# Deduplicates via md5 hash.  Fails silently if the viewer isn't running.

VIEWER_URL="${COMMENTARY_URL:-http://localhost:8090/commentary}"
SENT_HASH_FILE="/tmp/.commentary_last_hash"
SEND_LOG="/tmp/commentary_send.log"

INPUT=$(cat)

# On Stop events, also check last_assistant_message for current-turn text
HOOK_EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // empty')
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // empty')

TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    exit 0
fi

# Walk the transcript: track total_frame from tool_results, and snapshot
# the text + frame whenever we see an assistant text message.
# Output as JSON so we can safely extract fields with jq.
RESULT=$(jq -s '
    reduce .[] as $entry (
        {last_frame: null, text: null, frame: null};
        if $entry.type == "user" and ($entry.message.content | type) == "array"
           and ($entry.message.content[0].type // "") == "tool_result" then
            ($entry.message.content[0].content // "") as $raw
            | if ($raw | type) == "string" then
                (try ($raw | fromjson | .total_frame // null) catch null) as $tf
                | if $tf != null then .last_frame = $tf else . end
              else . end
        elif $entry.type == "assistant" and ($entry.message.content | type) == "array"
             and ($entry.message.content | map(select(.type == "text")) | length) > 0 then
            .text = ($entry.message.content | map(select(.type == "text") | .text) | join("\n"))
            | .frame = .last_frame
        else . end
    )
' "$TRANSCRIPT" 2>/dev/null || true)

LAST_RESPONSE=$(echo "$RESULT" | jq -r '.text // empty')
FRAME=$(echo "$RESULT" | jq -r '.frame // 0')

# On Stop, prefer last_assistant_message if the transcript text is stale
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

# Build the JSON payload
PAYLOAD=$(jq -n --arg text "$LAST_RESPONSE" --argjson frame "${FRAME:-0}" '{text: $text, style: "normal", frame: $frame}')

# Log what we're sending
echo "$(date '+%H:%M:%S') | event=$HOOK_EVENT frame=$FRAME hash=$HASH" >> "$SEND_LOG"
echo "  text: ${LAST_RESPONSE:0:120}" >> "$SEND_LOG"

# POST to the viewer with the retroactive frame timestamp
HTTP_CODE=$(curl -s -o /tmp/.commentary_last_response -w '%{http_code}' -X POST "$VIEWER_URL" \
    -H "Content-Type: application/json" \
    --max-time 2 \
    -d "$PAYLOAD" 2>&1) || true

echo "  http: $HTTP_CODE response: $(cat /tmp/.commentary_last_response 2>/dev/null)" >> "$SEND_LOG"
echo "" >> "$SEND_LOG"

#!/usr/bin/env bash
# Debug: check what PreToolUse sees in the transcript

LOG="/tmp/commentary_pretool_debug.log"

INPUT=$(cat)
TIMESTAMP=$(date '+%H:%M:%S.%N')
TOOL=$(echo "$INPUT" | jq -r '.tool_name // "unknown"')

echo "=== PreToolUse [$TOOL] at $TIMESTAMP ===" >> "$LOG"
echo "--- Input keys ---" >> "$LOG"
echo "$INPUT" | jq 'keys' >> "$LOG" 2>&1

TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    echo "!!! No transcript" >> "$LOG"
    echo "" >> "$LOG"
    exit 0
fi

# Last 3 transcript entries
echo "--- Last 3 entries ---" >> "$LOG"
tail -3 "$TRANSCRIPT" | jq '{type, content_types: [.message.content[]?.type]}' >> "$LOG" 2>&1

# Last text-bearing assistant message
LAST_TEXT=$(jq -s '
    [.[] | select(.type == "assistant")
         | select(.message.content | map(select(.type == "text")) | length > 0)]
    | last
    | .message.content // []
    | map(select(.type == "text") | .text)
    | join("\n")
' "$TRANSCRIPT" 2>/dev/null || true)

echo "--- Last text (first 200 chars) ---" >> "$LOG"
echo "${LAST_TEXT:0:200}" >> "$LOG"
echo "" >> "$LOG"

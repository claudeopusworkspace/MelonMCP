#!/usr/bin/env bash
# Debug version of commentary hook — dumps all info to a log file
# so we can inspect what the hook sees at the time it fires.

LOG="/tmp/commentary_hook_debug.log"

INPUT=$(cat)
TIMESTAMP=$(date '+%H:%M:%S.%N')

echo "=== HOOK FIRED at $TIMESTAMP ===" >> "$LOG"
echo "--- Raw input keys ---" >> "$LOG"
echo "$INPUT" | jq 'keys' >> "$LOG" 2>&1
echo "--- transcript_path ---" >> "$LOG"
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
echo "$TRANSCRIPT" >> "$LOG"

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    echo "!!! Transcript not found or empty" >> "$LOG"
    echo "" >> "$LOG"
    exit 0
fi

# Count total lines and assistant messages
TOTAL_LINES=$(wc -l < "$TRANSCRIPT")
ASSISTANT_COUNT=$(jq -s '[.[] | select(.type == "assistant")] | length' "$TRANSCRIPT" 2>/dev/null)
TEXT_BEARING=$(jq -s '[.[] | select(.type == "assistant") | select(.message.content | map(select(.type == "text")) | length > 0)] | length' "$TRANSCRIPT" 2>/dev/null)

echo "--- Transcript stats ---" >> "$LOG"
echo "Total JSONL lines: $TOTAL_LINES" >> "$LOG"
echo "Assistant messages: $ASSISTANT_COUNT" >> "$LOG"
echo "Text-bearing assistant messages: $TEXT_BEARING" >> "$LOG"

# Show last 3 entries: type and content types
echo "--- Last 3 transcript entries ---" >> "$LOG"
tail -3 "$TRANSCRIPT" | jq '{type, content_types: [.message.content[]?.type]}' >> "$LOG" 2>&1

# The text we would send
LAST_TEXT=$(jq -s '
    [.[] | select(.type == "assistant")
         | select(.message.content | map(select(.type == "text")) | length > 0)]
    | last
    | .message.content // []
    | map(select(.type == "text") | .text)
    | join("\n")
' "$TRANSCRIPT" 2>/dev/null || true)

echo "--- Last text-bearing response (first 200 chars) ---" >> "$LOG"
echo "${LAST_TEXT:0:200}" >> "$LOG"

# The key we actually care about
echo "--- last_assistant_message (first 500 chars) ---" >> "$LOG"
echo "$INPUT" | jq -r '.last_assistant_message // "not present"' | head -c 500 >> "$LOG" 2>&1
echo "" >> "$LOG"

echo "--- stop_hook_active ---" >> "$LOG"
echo "$INPUT" | jq '.stop_hook_active // "not present"' >> "$LOG" 2>&1

echo "" >> "$LOG"

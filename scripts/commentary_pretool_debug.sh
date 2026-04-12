#!/usr/bin/env bash
# Debug: snapshot the JSONL transcript on each PreToolUse so Woj can inspect it.

LOG="/tmp/commentary_pretool_debug.log"
SNAPSHOT_DIR="/tmp/commentary_snapshots"
mkdir -p "$SNAPSHOT_DIR"

INPUT=$(cat)
TIMESTAMP=$(date '+%H:%M:%S.%N')
TIMESTAMP_FILE=$(date '+%H%M%S_%N')
TOOL=$(echo "$INPUT" | jq -r '.tool_name // "unknown"')

echo "=== PreToolUse [$TOOL] at $TIMESTAMP ===" >> "$LOG"

TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    echo "!!! No transcript" >> "$LOG"
    echo "" >> "$LOG"
    exit 0
fi

# Copy the full JSONL as-is
SNAP="$SNAPSHOT_DIR/${TIMESTAMP_FILE}_${TOOL}.jsonl"
cp "$TRANSCRIPT" "$SNAP"
echo "Snapshot: $SNAP ($(wc -l < "$SNAP") lines)" >> "$LOG"

# Also log a quick summary for the log file
LAST_TEXT=$(jq -s '
    [.[] | select(.type == "assistant")
         | select(.message.content | map(select(.type == "text")) | length > 0)]
    | last
    | .message.content // []
    | map(select(.type == "text") | .text)
    | join("\n")
' "$TRANSCRIPT" 2>/dev/null || true)

echo "Last text (first 150 chars): ${LAST_TEXT:0:150}" >> "$LOG"
echo "" >> "$LOG"

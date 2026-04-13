#!/usr/bin/env bash
# Manages the standalone recording server (start/stop/status).
#
# Usage:
#   recording_server.sh start   — start in background (default)
#   recording_server.sh stop    — stop the running server
#   recording_server.sh status  — check if running
#   recording_server.sh restart — stop + start
#
# The server runs on port 8091 and serves recorded sessions from
# /workspace/MelonMCP/recordings/.

set -euo pipefail

PROJECT_DIR="/workspace/MelonMCP"
VENV="$PROJECT_DIR/.venv"
PIDFILE="$PROJECT_DIR/.recording_server.pid"
LOGFILE="$PROJECT_DIR/.recording_server.log"
PORT=8091
RECORDINGS_DIR="$PROJECT_DIR/recordings"

_is_running() {
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        # Stale PID file
        rm -f "$PIDFILE"
    fi
    return 1
}

cmd_start() {
    if _is_running; then
        echo "Recording server already running (PID $(cat "$PIDFILE"))"
        return 0
    fi

    if [ ! -f "$VENV/bin/python" ]; then
        echo "Error: venv not found at $VENV" >&2
        return 1
    fi

    mkdir -p "$RECORDINGS_DIR"

    nohup "$VENV/bin/python" -m melonds_mcp.recording_server \
        --port "$PORT" \
        --recordings-dir "$RECORDINGS_DIR" \
        >> "$LOGFILE" 2>&1 &

    echo $! > "$PIDFILE"
    echo "Recording server started (PID $!, port $PORT)"
}

cmd_stop() {
    if ! _is_running; then
        echo "Recording server is not running"
        return 0
    fi

    local pid
    pid=$(cat "$PIDFILE")
    kill "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "Recording server stopped (was PID $pid)"
}

cmd_status() {
    if _is_running; then
        echo "Recording server is running (PID $(cat "$PIDFILE"), port $PORT)"
    else
        echo "Recording server is not running"
    fi
}

cmd_restart() {
    cmd_stop
    cmd_start
}

case "${1:-start}" in
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    status)  cmd_status  ;;
    restart) cmd_restart ;;
    *)
        echo "Usage: $0 {start|stop|status|restart}" >&2
        exit 1
        ;;
esac

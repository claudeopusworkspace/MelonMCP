"""Lightweight web viewer — streams DS screenshots via Server-Sent Events."""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .emulator import EmulatorState

logger = logging.getLogger(__name__)

_HTML_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>melonDS Viewer</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #1a1a2e;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    font-family: 'Courier New', monospace;
    color: #e0e0e0;
}
#container {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
}
#screen {
    image-rendering: pixelated;
    border: 2px solid #333;
    border-radius: 4px;
    width: 512px;
    height: 768px;
    background: #000;
}
#divider {
    position: relative;
    width: 512px;
    margin-top: -12px;
    margin-bottom: -12px;
    z-index: 1;
}
#divider hr {
    border: none;
    border-top: 1px dashed #444;
}
#status-bar {
    display: flex;
    gap: 24px;
    font-size: 14px;
    color: #888;
}
#mode-badge {
    padding: 1px 8px;
    border-radius: 3px;
    font-size: 12px;
    font-weight: bold;
    letter-spacing: 1px;
}
#mode-badge.live    { background: #2e7d32; color: #c8e6c9; }
#mode-badge.history { background: #e65100; color: #ffe0b2; }
.dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #666;
    margin-right: 6px;
    vertical-align: middle;
}
.dot.connected    { background: #4caf50; }
.dot.disconnected { background: #f44336; }
h1 {
    font-size: 16px;
    font-weight: normal;
    color: #666;
    letter-spacing: 2px;
    text-transform: uppercase;
}
#hint {
    font-size: 12px;
    color: #555;
}
</style>
</head>
<body>
<div id="container">
    <h1>melonDS Viewer</h1>
    <img id="screen" alt="DS Screen" src="/screenshot?t=0">
    <div id="divider"><hr></div>
    <div id="status-bar">
        <span><span id="dot" class="dot disconnected"></span><span id="status-text">Connecting\u2026</span></span>
        <span id="mode-badge" class="live">LIVE</span>
        <span>Frame: <span id="frame-count">\u2014</span></span>
        <span id="history-pos"></span>
    </div>
    <div id="hint">\u2190 \u2192 browse history &middot; Space: return to live</div>
</div>
<script>
(function() {
    var screen     = document.getElementById('screen');
    var dot        = document.getElementById('dot');
    var statusTxt  = document.getElementById('status-text');
    var frameTxt   = document.getElementById('frame-count');
    var modeBadge  = document.getElementById('mode-badge');
    var historyPos = document.getElementById('history-pos');

    var history   = [];    // ordered list of frame numbers
    var live      = true;  // true = showing latest, auto-updating
    var browseIdx = -1;    // index into history when browsing
    var sessionId = '';    // prevents cross-session cache collisions

    function showFrame(frame) {
        screen.src = '/screenshot?frame=' + frame + '&s=' + sessionId;
        frameTxt.textContent = frame;
    }

    function updateBadge() {
        if (live) {
            modeBadge.className = 'live';
            modeBadge.textContent = 'LIVE';
            historyPos.textContent = '';
        } else {
            modeBadge.className = 'history';
            modeBadge.textContent = 'HISTORY';
            historyPos.textContent = (browseIdx + 1) + ' / ' + history.length;
        }
    }

    function goLive() {
        live = true;
        browseIdx = -1;
        if (history.length > 0) {
            showFrame(history[history.length - 1]);
        }
        updateBadge();
    }

    function browseBack() {
        if (history.length === 0) return;
        if (live) {
            live = false;
            browseIdx = history.length - 2;
        } else {
            browseIdx--;
        }
        if (browseIdx < 0) browseIdx = 0;
        showFrame(history[browseIdx]);
        updateBadge();
    }

    function browseForward() {
        if (live || history.length === 0) return;
        if (browseIdx < history.length - 1) {
            browseIdx++;
            showFrame(history[browseIdx]);
            updateBadge();
        }
    }

    document.addEventListener('keydown', function(e) {
        if (e.key === 'ArrowLeft')  { e.preventDefault(); browseBack(); }
        if (e.key === 'ArrowRight') { e.preventDefault(); browseForward(); }
        if (e.key === ' ')          { e.preventDefault(); goLive(); }
    });

    function onFrame(frame) {
        history.push(frame);
        if (live) {
            showFrame(frame);
        }
        updateBadge();
    }

    function connect() {
        var es = new EventSource('/stream');

        es.onopen = function() {
            dot.className = 'dot connected';
            statusTxt.textContent = 'Connected';
        };

        es.addEventListener('frame', function(e) {
            onFrame(JSON.parse(e.data).frame);
        });

        es.addEventListener('init', function(e) {
            var d = JSON.parse(e.data);
            sessionId = d.session || '';
            onFrame(d.frame);
        });

        es.onerror = function() {
            dot.className = 'dot disconnected';
            statusTxt.textContent = 'Reconnecting\u2026';
            es.close();
            setTimeout(connect, 2000);
        };
    }

    connect();
})();
</script>
</body>
</html>
"""


class _ViewerHandler(BaseHTTPRequestHandler):
    """Serves the viewer page, current screenshot, and SSE stream."""

    # Silence per-request log lines
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._serve_html()
        elif path == "/screenshot":
            self._serve_screenshot()
        elif path == "/stream":
            self._serve_sse()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # -- endpoints ---------------------------------------------------------

    def _serve_html(self):
        body = _HTML_PAGE.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_screenshot(self):
        from urllib.parse import parse_qs, urlparse

        viewer: ViewerServer = self.server.viewer  # type: ignore[attr-defined]
        query = parse_qs(urlparse(self.path).query)
        frame_param = query.get("frame", [None])[0]

        if frame_param is not None:
            data = viewer.get_screenshot_for_frame(int(frame_param))
            cache = "public, max-age=86400, immutable"
        else:
            data = viewer.get_current_screenshot()
            cache = "no-cache, no-store, must-revalidate"

        if data is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _serve_sse(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        viewer: ViewerServer = self.server.viewer  # type: ignore[attr-defined]
        q: queue.Queue[str] = queue.Queue()
        viewer._register_client(q)

        try:
            # Send current frame immediately so the page is up-to-date
            frame = viewer.get_current_frame()
            self._sse_write("init", json.dumps({"frame": frame, "session": viewer.session_id}))

            while True:
                try:
                    event_data = q.get(timeout=30)
                    self._sse_write("frame", event_data)
                except queue.Empty:
                    # keepalive comment prevents proxy/browser timeouts
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            viewer._unregister_client(q)

    def _sse_write(self, event: str, data: str):
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        self.wfile.flush()


# -- Public API ------------------------------------------------------------


def archive_old_screenshots(screenshots_dir: Path) -> Path | None:
    """Move any existing screenshots into an archive subdirectory.

    Returns the archive path if files were moved, or None if nothing to archive.
    """
    if not screenshots_dir.is_dir():
        return None

    files = [f for f in screenshots_dir.iterdir() if f.is_file()]
    if not files:
        return None

    archive_dir = screenshots_dir / "archive"
    # Use the oldest file's mtime as the session label
    oldest = min(files, key=lambda f: f.stat().st_mtime)
    from datetime import datetime, timezone

    ts = datetime.fromtimestamp(oldest.stat().st_mtime, tz=timezone.utc)
    session_dir = archive_dir / ts.strftime("%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        shutil.move(str(f), str(session_dir / f.name))

    logger.info("Archived %d screenshot(s) to %s", len(files), session_dir)
    return session_dir


class ViewerServer:
    """Streams DS screenshots to a browser via SSE.

    Usage::

        viewer = ViewerServer(holder, port=8090)
        viewer.start()          # background thread
        viewer.notify()         # call after frame changes
        viewer.stop()
    """

    MAX_HISTORY = 500  # max screenshots kept in memory

    def __init__(self, holder: EmulatorState, port: int = 8090):
        self._holder = holder
        self._port = port
        self._session_id = uuid.uuid4().hex[:12]
        self._clients: list[queue.Queue[str]] = []
        self._clients_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._current_screenshot: bytes | None = None
        self._screenshot_lock = threading.Lock()
        # Frame history for browsing
        self._screenshot_history: dict[int, bytes] = {}
        self._history_order: list[int] = []

    @property
    def port(self) -> int:
        return self._port

    @property
    def session_id(self) -> str:
        return self._session_id

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Start serving in a daemon thread."""
        if self._thread is not None:
            return
        srv = ThreadingHTTPServer(("0.0.0.0", self._port), _ViewerHandler)
        srv.viewer = self  # type: ignore[attr-defined]
        srv.daemon_threads = True
        self._server = srv
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Viewer started on http://0.0.0.0:%d", self._port)

    def stop(self):
        """Shut down the server."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
            self._thread = None
            logger.info("Viewer stopped")

    # -- frame notification ------------------------------------------------

    def notify(self):
        """Capture a fresh screenshot and push an SSE event to all clients."""
        # Grab screenshot (while caller already holds the emulator lock)
        try:
            _, data = self._holder.capture_screenshot("both", "png")
        except Exception:
            return

        frame = self._holder.frame_count
        with self._screenshot_lock:
            self._current_screenshot = data
            self._screenshot_history[frame] = data
            self._history_order.append(frame)
            # Evict oldest entries when over the cap
            while len(self._history_order) > self.MAX_HISTORY:
                old = self._history_order.pop(0)
                self._screenshot_history.pop(old, None)

        event_data = json.dumps({"frame": frame})
        with self._clients_lock:
            for q in self._clients:
                q.put(event_data)

    # -- helpers used by handler -------------------------------------------

    def get_current_screenshot(self) -> bytes | None:
        with self._screenshot_lock:
            if self._current_screenshot is not None:
                return self._current_screenshot
        # No cached screenshot — try to capture one now
        try:
            with self._holder.lock:
                _, data = self._holder.capture_screenshot("both", "png")
            with self._screenshot_lock:
                self._current_screenshot = data
            return data
        except Exception:
            return None

    def get_screenshot_for_frame(self, frame: int) -> bytes | None:
        """Return the stored screenshot for a specific frame, or None."""
        with self._screenshot_lock:
            return self._screenshot_history.get(frame)

    def get_current_frame(self) -> int:
        return self._holder.frame_count

    def _register_client(self, q: queue.Queue[str]):
        with self._clients_lock:
            self._clients.append(q)
        logger.info("Viewer client connected (%d total)", len(self._clients))

    def _unregister_client(self, q: queue.Queue[str]):
        with self._clients_lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass
        logger.info("Viewer client disconnected (%d remaining)", len(self._clients))

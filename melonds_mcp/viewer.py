"""Unified streaming viewer — HLS video + commentary overlay + screenshot debug.

Serves a single page that combines:
- HLS video playback (segments loaded from the renderer's HTTP server)
- Commentary overlay synced to video playback position
- Screenshot history browsing (debug mode)
- Status bar with connection info, buffer, and frame counters

SSE endpoints:
- /stream  — frame update notifications (existing, kept for screenshot updates)
- /commentary — commentary events with frame-synced timing
"""

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


def _build_html(hls_port: int) -> str:
    """Build the unified viewer HTML with the HLS port baked in."""
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>melonDS Stream</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: #111;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    font-family: 'Courier New', monospace;
    color: #e0e0e0;
}}
#container {{
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
}}
#video-wrap {{
    position: relative;
    width: 512px;
    height: 768px;
}}
video {{
    image-rendering: pixelated;
    border: 2px solid #333;
    border-radius: 4px;
    width: 100%;
    height: 100%;
    background: #000;
}}
#screenshot {{
    image-rendering: pixelated;
    border: 2px solid #333;
    border-radius: 4px;
    width: 100%;
    height: 100%;
    background: #000;
    display: none;
}}
#commentary-overlay {{
    position: absolute;
    bottom: 24px;
    left: 0;
    right: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    pointer-events: none;
    z-index: 10;
}}
.commentary-msg {{
    background: rgba(0, 0, 0, 0.75);
    color: #fff;
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 14px;
    max-width: 460px;
    text-align: center;
    line-height: 1.4;
    animation: fadeIn 0.3s ease-out;
    transition: opacity 0.5s ease-out;
    /* Clamp to 3 lines max, ellipsis on overflow */
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
}}
.commentary-msg.excited {{
    background: rgba(255, 152, 0, 0.85);
    font-weight: bold;
}}
.commentary-msg.whisper {{
    background: rgba(0, 0, 0, 0.5);
    font-style: italic;
    font-size: 12px;
}}
.commentary-msg.fading {{
    opacity: 0;
}}
@keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to {{ opacity: 1; transform: translateY(0); }}
}}
h1 {{
    font-size: 16px;
    font-weight: normal;
    color: #666;
    letter-spacing: 2px;
    text-transform: uppercase;
}}
#status-bar {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    font-size: 13px;
    color: #888;
    justify-content: center;
}}
.dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
}}
.dot.buffering {{ background: #ff9800; }}
.dot.playing   {{ background: #4caf50; }}
.dot.error     {{ background: #f44336; }}
.dot.waiting   {{ background: #666; }}
.dot.connected {{ background: #4caf50; }}
#mode-badge {{
    padding: 1px 8px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 1px;
}}
#mode-badge.video   {{ background: #2e7d32; color: #c8e6c9; }}
#mode-badge.history {{ background: #e65100; color: #ffe0b2; }}
#unmute-btn {{
    padding: 4px 12px;
    border: 1px solid #555;
    border-radius: 4px;
    background: #2e7d32;
    color: #fff;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
    letter-spacing: 1px;
}}
#unmute-btn:hover {{ background: #388e3c; }}
#unmute-btn.muted {{ background: #c62828; }}
#volume-slider {{
    width: 60px;
    vertical-align: middle;
    cursor: pointer;
    accent-color: #4caf50;
}}
#vol-label {{ font-size: 11px; color: #888; }}
#hint {{
    font-size: 11px;
    color: #555;
}}
/* -- Commentary sidebar -- */
#sidebar-toggle {{
    position: fixed;
    top: 12px;
    right: 12px;
    padding: 6px 12px;
    border: 1px solid #444;
    border-radius: 4px;
    background: #222;
    color: #aaa;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
    z-index: 100;
    letter-spacing: 1px;
}}
#sidebar-toggle:hover {{ background: #333; color: #ddd; }}
#sidebar-toggle.active {{ background: #2e7d32; color: #fff; border-color: #4caf50; }}
#commentary-sidebar {{
    position: fixed;
    top: 0;
    right: -340px;
    width: 320px;
    height: 100vh;
    background: #1a1a1a;
    border-left: 1px solid #333;
    overflow-y: auto;
    padding: 48px 12px 12px;
    transition: right 0.25s ease;
    z-index: 90;
}}
#commentary-sidebar.open {{
    right: 0;
}}
#commentary-sidebar h2 {{
    font-size: 13px;
    color: #666;
    letter-spacing: 1px;
    text-transform: uppercase;
    margin-bottom: 12px;
}}
.sidebar-entry {{
    padding: 8px 10px;
    margin-bottom: 6px;
    border-radius: 4px;
    background: #222;
    font-size: 13px;
    line-height: 1.4;
    color: #ccc;
    border-left: 3px solid #555;
}}
.sidebar-entry.excited {{ border-left-color: #ff9800; }}
.sidebar-entry.whisper {{ border-left-color: #666; font-style: italic; color: #999; }}
.sidebar-entry .entry-time {{
    font-size: 10px;
    color: #555;
    margin-bottom: 4px;
}}
</style>
</head>
<body>
<div id="container">
    <h1>melonDS Stream</h1>
    <div id="video-wrap">
        <video id="player" muted autoplay></video>
        <img id="screenshot" alt="DS Screen">
        <div id="commentary-overlay"></div>
    </div>
    <div id="status-bar">
        <span><span id="dot" class="dot waiting"></span><span id="status">Waiting</span></span>
        <span id="mode-badge" class="video">VIDEO</span>
        <button id="unmute-btn" class="muted">UNMUTE</button>
        <input id="volume-slider" type="range" min="0" max="100" value="50">
        <span id="vol-label">50%</span>
        <span>Buffer: <span id="buffer-info">&mdash;</span></span>
        <span>Frame: <span id="frame-count">&mdash;</span></span>
        <span id="history-pos"></span>
    </div>
    <div id="hint">Arrow keys: browse screenshots &middot; Space: return to video</div>
</div>
<button id="sidebar-toggle">COMMENTARY</button>
<div id="commentary-sidebar">
    <h2>Commentary</h2>
    <div id="sidebar-entries"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
(function() {{
    var video      = document.getElementById('player');
    var screenshot = document.getElementById('screenshot');
    var dot        = document.getElementById('dot');
    var statusEl   = document.getElementById('status');
    var bufInfo    = document.getElementById('buffer-info');
    var muteBtn    = document.getElementById('unmute-btn');
    var volSlider  = document.getElementById('volume-slider');
    var volLabel   = document.getElementById('vol-label');
    var frameTxt   = document.getElementById('frame-count');
    var modeBadge  = document.getElementById('mode-badge');
    var historyPos = document.getElementById('history-pos');
    var overlay    = document.getElementById('commentary-overlay');

    var HLS_PORT = {hls_port};
    var hlsUrl = 'http://' + location.hostname + ':' + HLS_PORT + '/hls/stream.m3u8';

    // -- Mode: video (live HLS) or history (screenshot browsing) --
    var mode = 'video';
    var history = [];
    var browseIdx = -1;
    var sessionId = '';

    video.volume = 0.5;

    function setMode(m) {{
        mode = m;
        if (m === 'video') {{
            video.style.display = '';
            screenshot.style.display = 'none';
            modeBadge.className = 'video';
            modeBadge.textContent = 'VIDEO';
            historyPos.textContent = '';
        }} else {{
            video.style.display = 'none';
            screenshot.style.display = '';
            modeBadge.className = 'history';
            modeBadge.textContent = 'HISTORY';
            historyPos.textContent = (browseIdx + 1) + ' / ' + history.length;
        }}
    }}

    function showScreenshot(frame) {{
        screenshot.src = '/screenshot?frame=' + frame + '&s=' + sessionId;
        frameTxt.textContent = frame;
    }}

    function goVideo() {{
        setMode('video');
        browseIdx = -1;
    }}

    function browseBack() {{
        if (history.length === 0) return;
        if (mode === 'video') {{
            setMode('history');
            browseIdx = history.length - 1;
        }} else {{
            browseIdx = Math.max(0, browseIdx - 1);
        }}
        showScreenshot(history[browseIdx]);
        historyPos.textContent = (browseIdx + 1) + ' / ' + history.length;
    }}

    function browseForward() {{
        if (mode === 'video' || history.length === 0) return;
        if (browseIdx < history.length - 1) {{
            browseIdx++;
            showScreenshot(history[browseIdx]);
            historyPos.textContent = (browseIdx + 1) + ' / ' + history.length;
        }}
    }}

    document.addEventListener('keydown', function(e) {{
        if (e.key === 'ArrowLeft')  {{ e.preventDefault(); browseBack(); }}
        if (e.key === 'ArrowRight') {{ e.preventDefault(); browseForward(); }}
        if (e.key === ' ')          {{ e.preventDefault(); goVideo(); }}
    }});

    // -- Audio controls --
    muteBtn.addEventListener('click', function() {{
        video.muted = !video.muted;
        muteBtn.textContent = video.muted ? 'UNMUTE' : 'MUTE';
        muteBtn.className = video.muted ? 'muted' : '';
        muteBtn.id = 'unmute-btn';
    }});

    volSlider.addEventListener('input', function() {{
        video.volume = volSlider.value / 100;
        volLabel.textContent = volSlider.value + '%';
    }});

    // -- Buffer info --
    function updateBufferInfo() {{
        if (video.buffered.length > 0) {{
            var ahead = video.buffered.end(video.buffered.length - 1) - video.currentTime;
            bufInfo.textContent = ahead.toFixed(1) + 's';
        }}
        requestAnimationFrame(updateBufferInfo);
    }}
    updateBufferInfo();

    function setStatus(cls, text) {{
        dot.className = 'dot ' + cls;
        statusEl.textContent = text;
    }}

    // -- HLS video --
    var hlsReady = false;
    var retryTimer = null;

    function tryLoadHls() {{
        if (retryTimer) {{ clearTimeout(retryTimer); retryTimer = null; }}

        if (Hls.isSupported()) {{
            // EVENT playlist mode — the stream is a growing VOD, not
            // a true live stream.  hls.js plays linearly from the start,
            // pauses when it reaches the end of available data, and
            // resumes when new segments appear.  No live-sync settings
            // needed — no seeking, no skipping, no recovery fights.
            var hls = new Hls({{
                maxBufferLength: 30,
                maxMaxBufferLength: 60,
                maxBufferHole: 0.5,
                enableWorker: true,
                lowLatencyMode: false,
            }});

            hls.on(Hls.Events.MEDIA_ATTACHED, function() {{
                hls.loadSource(hlsUrl);
            }});

            hls.on(Hls.Events.MANIFEST_PARSED, function() {{
                hlsReady = true;
                setStatus('buffering', 'Buffering');
                video.play().catch(function() {{}});
            }});

            hls.on(Hls.Events.FRAG_BUFFERED, function() {{
                if (mode === 'video') setStatus('playing', 'Playing');
            }});

            // Show buffering status when waiting for data — the video
            // just pauses naturally and resumes when segments arrive.
            video.addEventListener('waiting', function() {{
                if (mode === 'video') setStatus('buffering', 'Buffering');
            }});
            video.addEventListener('playing', function() {{
                if (mode === 'video') setStatus('playing', 'Playing');
            }});

            hls.on(Hls.Events.ERROR, function(event, data) {{
                if (data.fatal) {{
                    hls.destroy();
                    hlsReady = false;
                    setStatus('error', 'Stream interrupted');
                    retryTimer = setTimeout(tryLoadHls, 3000);
                }}
            }});

            hls.attachMedia(video);
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
            video.src = hlsUrl;
            video.addEventListener('loadedmetadata', function() {{
                hlsReady = true;
                setStatus('playing', 'Playing');
                video.play().catch(function() {{}});
            }});
        }} else {{
            setStatus('error', 'HLS not supported');
        }}
    }}

    // Poll for HLS availability
    function waitForHls() {{
        setStatus('waiting', 'Waiting for stream');
        fetch(hlsUrl, {{method: 'HEAD'}}).then(function(r) {{
            if (r.ok) {{ tryLoadHls(); }}
            else {{ setTimeout(waitForHls, 1000); }}
        }}).catch(function() {{
            setTimeout(waitForHls, 1000);
        }});
    }}
    waitForHls();

    // -- Commentary sidebar --
    var sidebarToggle  = document.getElementById('sidebar-toggle');
    var sidebar        = document.getElementById('commentary-sidebar');
    var sidebarEntries = document.getElementById('sidebar-entries');
    var sidebarOpen    = false;

    sidebarToggle.addEventListener('click', function() {{
        sidebarOpen = !sidebarOpen;
        sidebar.className = sidebarOpen ? 'open' : '';
        sidebarToggle.className = sidebarOpen ? 'active' : '';
    }});

    function addToSidebar(text, style, streamTime) {{
        var mins = Math.floor(streamTime / 60);
        var secs = Math.floor(streamTime % 60);
        var ts = mins + ':' + (secs < 10 ? '0' : '') + secs;
        var entry = document.createElement('div');
        entry.className = 'sidebar-entry ' + (style || 'normal');
        entry.innerHTML = '<div class="entry-time">' + ts + '</div>' + text.replace(/</g, '&lt;').replace(/>/g, '&gt;');
        sidebarEntries.appendChild(entry);
        sidebarEntries.scrollTop = sidebarEntries.scrollHeight;
    }}

    // -- Commentary overlay --
    var commentaryQueue = [];
    var COMMENTARY_DISPLAY_SECS = 10;
    var COMMENTARY_FADE_SECS = 0.5;

    function addCommentary(streamTime, text, style) {{
        commentaryQueue.push({{streamTime: streamTime, text: text, style: style || 'normal', shown: false, inSidebar: false}});
    }}

    function updateCommentary() {{
        if (mode !== 'video' || !hlsReady) {{
            requestAnimationFrame(updateCommentary);
            return;
        }}
        var now = video.currentTime;
        for (var i = 0; i < commentaryQueue.length; i++) {{
            var c = commentaryQueue[i];
            if (!c.shown && now >= c.streamTime) {{
                c.shown = true;
                c.showTime = Date.now();
                var el = document.createElement('div');
                el.className = 'commentary-msg ' + c.style;
                el.textContent = c.text;
                c.el = el;
                overlay.appendChild(el);
                if (!c.inSidebar) {{
                    c.inSidebar = true;
                    addToSidebar(c.text, c.style, c.streamTime);
                }}
            }}
            if (c.shown && c.el) {{
                var elapsed = (Date.now() - c.showTime) / 1000;
                if (elapsed > COMMENTARY_DISPLAY_SECS + COMMENTARY_FADE_SECS) {{
                    if (c.el.parentNode) c.el.parentNode.removeChild(c.el);
                    commentaryQueue.splice(i, 1);
                    i--;
                }} else if (elapsed > COMMENTARY_DISPLAY_SECS) {{
                    c.el.classList.add('fading');
                }}
            }}
        }}
        requestAnimationFrame(updateCommentary);
    }}
    updateCommentary();

    // -- SSE for frame updates (screenshot history) --
    function connectFrameSSE() {{
        var es = new EventSource('/stream');
        es.addEventListener('init', function(e) {{
            var d = JSON.parse(e.data);
            sessionId = d.session || '';
            history.push(d.frame);
            frameTxt.textContent = d.frame;
        }});
        es.addEventListener('frame', function(e) {{
            var d = JSON.parse(e.data);
            history.push(d.frame);
            if (mode === 'video') frameTxt.textContent = d.frame;
        }});
        es.onerror = function() {{
            es.close();
            setTimeout(connectFrameSSE, 2000);
        }};
    }}
    connectFrameSSE();

    // -- SSE for commentary events --
    function connectCommentarySSE() {{
        var es = new EventSource('/commentary');
        es.addEventListener('commentary', function(e) {{
            var d = JSON.parse(e.data);
            addCommentary(d.stream_time, d.text, d.style);
        }});
        es.onerror = function() {{
            es.close();
            setTimeout(connectCommentarySSE, 2000);
        }};
    }}
    connectCommentarySSE();
}})();
</script>
</body>
</html>
"""


class _ViewerHandler(BaseHTTPRequestHandler):
    """Serves the unified viewer page, screenshots, and SSE streams."""

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
        elif path == "/commentary":
            self._serve_commentary_sse()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # -- endpoints ---------------------------------------------------------

    def _serve_html(self):
        viewer: ViewerServer = self.server.viewer  # type: ignore[attr-defined]
        body = _build_html(viewer._hls_port).encode()
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
            frame = viewer.get_current_frame()
            self._sse_write("init", json.dumps({"frame": frame, "session": viewer.session_id}))

            while True:
                try:
                    event_data = q.get(timeout=30)
                    self._sse_write("frame", event_data)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            viewer._unregister_client(q)

    def _serve_commentary_sse(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        viewer: ViewerServer = self.server.viewer  # type: ignore[attr-defined]
        q: queue.Queue[str] = queue.Queue()
        viewer._register_commentary_client(q)

        try:
            while True:
                try:
                    event_data = q.get(timeout=30)
                    self._sse_write("commentary", event_data)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            viewer._unregister_commentary_client(q)

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
    """Unified streaming viewer — HLS video + commentary overlay + screenshots.

    Usage::

        viewer = ViewerServer(holder, port=8090)
        viewer.start()          # background thread
        viewer.notify()         # call after frame changes
        viewer.add_commentary(frame, "text", "normal")
        viewer.stop()
    """

    MAX_HISTORY = 500  # max screenshots kept in memory

    def __init__(self, holder: EmulatorState, port: int = 8090):
        self._holder = holder
        self._port = port
        self._hls_port = 8091  # default, updated by set_hls_port()
        self._session_id = uuid.uuid4().hex[:12]
        self._stream_start_frame = 0

        # Frame/screenshot SSE clients
        self._clients: list[queue.Queue[str]] = []
        self._clients_lock = threading.Lock()

        # Commentary SSE clients
        self._commentary_clients: list[queue.Queue[str]] = []
        self._commentary_lock = threading.Lock()

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

    # -- configuration -----------------------------------------------------

    def set_hls_port(self, port: int) -> None:
        """Update the HLS port so the page knows where to load video from."""
        self._hls_port = port

    # -- frame notification ------------------------------------------------

    def notify(self):
        """Capture a fresh screenshot and push an SSE event to all clients."""
        try:
            _, data = self._holder.capture_screenshot("both", "png")
        except Exception:
            return

        frame = self._holder.frame_count
        with self._screenshot_lock:
            self._current_screenshot = data
            self._screenshot_history[frame] = data
            self._history_order.append(frame)
            while len(self._history_order) > self.MAX_HISTORY:
                old = self._history_order.pop(0)
                self._screenshot_history.pop(old, None)

        event_data = json.dumps({"frame": frame})
        with self._clients_lock:
            for q in self._clients:
                q.put(event_data)

    # -- commentary --------------------------------------------------------

    def add_commentary(self, frame: int, text: str, style: str = "normal") -> None:
        """Push a commentary event to all connected clients."""
        stream_time = (frame - self._stream_start_frame) / 60.0
        event_data = json.dumps({
            "frame": frame,
            "text": text,
            "style": style,
            "stream_time": max(0.0, stream_time),
        })
        with self._commentary_lock:
            for q in self._commentary_clients:
                try:
                    q.put_nowait(event_data)
                except queue.Full:
                    pass

    # -- helpers used by handler -------------------------------------------

    def get_current_screenshot(self) -> bytes | None:
        with self._screenshot_lock:
            if self._current_screenshot is not None:
                return self._current_screenshot
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

    def _register_commentary_client(self, q: queue.Queue[str]):
        with self._commentary_lock:
            self._commentary_clients.append(q)
        logger.info("Commentary client connected (%d total)", len(self._commentary_clients))

    def _unregister_commentary_client(self, q: queue.Queue[str]):
        with self._commentary_lock:
            try:
                self._commentary_clients.remove(q)
            except ValueError:
                pass
        logger.info("Commentary client disconnected (%d remaining)", len(self._commentary_clients))

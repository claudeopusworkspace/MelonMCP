"""Standalone recording browser — always-on HTTP server for listing and playing back recorded sessions.

Runs independently of the emulator on port 8092.  Serves:
- /              — redirect to /recordings
- /recordings    — session list
- /recordings/<stem>         — playback page
- /recordings/<file>.mp4     — video with range-request support
- /recordings/<file>.json    — metadata
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8092
DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"


# ---------------------------------------------------------------------------
# HTML builders
# ---------------------------------------------------------------------------

def _viewer_link(request_host: str) -> str:
    """Build a link back to the live viewer on port 8090 using the request hostname."""
    hostname = request_host.split(":")[0] if request_host else "localhost"
    return f"//{hostname}:8090/"


def _build_recordings_html(recordings: list[dict], viewer_url: str) -> str:
    """Build HTML page listing available recordings."""
    rows = ""
    for rec in recordings:
        stem = rec.get("filename", "")
        try:
            dt = datetime.strptime(stem, "%Y%m%d_%H%M%S")
            date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            date_str = stem
        dur = rec.get("duration", 0)
        dur_min = int(dur) // 60
        dur_sec = int(dur) % 60
        dur_str = f"{dur_min}:{dur_sec:02d}"
        rec_name = rec.get("name", "unnamed")
        if len(rec_name) > 80:
            rec_name = rec_name[:77] + "..."
        rec_name = rec_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        size_mb = rec.get("size_mb", 0)
        rows += f"""\
        <tr class="rec-row" onclick="location.href='/recordings/{stem}'">
            <td>{rec_name}</td>
            <td>{date_str}</td>
            <td>{dur_str}</td>
            <td>{size_mb:.1f} MB</td>
        </tr>
"""

    if not rows:
        rows = '<tr><td colspan="4" style="text-align:center;color:#555;padding:24px">No recordings yet</td></tr>'

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>melonDS Recordings</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: #111;
    font-family: 'Courier New', monospace;
    color: #e0e0e0;
    padding: 24px;
}}
h1 {{
    font-size: 16px;
    font-weight: normal;
    color: #666;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 16px;
}}
a {{ color: #4caf50; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.back {{ display: inline-block; margin-bottom: 16px; font-size: 13px; }}
table {{
    width: 100%;
    max-width: 900px;
    border-collapse: collapse;
}}
th {{
    text-align: left;
    padding: 8px 12px;
    font-size: 11px;
    color: #666;
    letter-spacing: 1px;
    text-transform: uppercase;
    border-bottom: 1px solid #333;
}}
td {{
    padding: 10px 12px;
    font-size: 13px;
    border-bottom: 1px solid #222;
}}
.rec-row {{ cursor: pointer; }}
.rec-row:hover {{ background: #1a1a1a; }}
</style>
</head>
<body>
<a class="back" href="{viewer_url}">&larr; Live Stream</a>
<h1>Recordings</h1>
<table>
    <thead>
        <tr>
            <th>Name</th>
            <th>Date</th>
            <th>Duration</th>
            <th>Size</th>
        </tr>
    </thead>
    <tbody>
{rows}
    </tbody>
</table>
</body>
</html>
"""


def _build_playback_html(stem: str, commentary: list[dict], meta: dict, viewer_url: str) -> str:
    """Build HTML page for recording playback with commentary."""
    commentary_json = json.dumps(commentary)

    try:
        dt = datetime.strptime(stem, "%Y%m%d_%H%M%S")
        date_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        date_str = stem

    rec_name = meta.get("name", "unnamed")
    duration = meta.get("duration", 0)
    dur_min = int(duration) // 60
    dur_sec = int(duration) % 60
    total_comments = len(commentary)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Recording — {rec_name}</title>
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
#commentary-overlay {{
    position: absolute;
    bottom: 60px;
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
    transition: opacity 0.3s;
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
h1 {{
    font-size: 16px;
    font-weight: normal;
    color: #666;
    letter-spacing: 2px;
    text-transform: uppercase;
}}
a {{ color: #4caf50; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.meta {{
    font-size: 12px;
    color: #555;
    display: flex;
    gap: 16px;
}}
.nav-links {{
    display: flex;
    gap: 16px;
    font-size: 13px;
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
    cursor: pointer;
    display: none;
}}
.sidebar-entry.visible {{ display: block; }}
.sidebar-entry.excited {{ border-left-color: #ff9800; }}
.sidebar-entry.whisper {{ border-left-color: #666; font-style: italic; color: #999; }}
.sidebar-entry .entry-time {{
    font-size: 10px;
    color: #555;
    margin-bottom: 4px;
}}
.sidebar-entry:hover {{ background: #2a2a2a; }}
</style>
</head>
<body>
<div id="container">
    <div class="nav-links">
        <a href="/recordings">&larr; All Recordings</a>
        <a href="{viewer_url}">Live Stream</a>
    </div>
    <h1>{rec_name}</h1>
    <div class="meta">
        <span>{date_str}</span>
        <span>{dur_min}:{dur_sec:02d}</span>
        <span>{total_comments} comment{"s" if total_comments != 1 else ""}</span>
    </div>
    <div id="video-wrap">
        <video id="player" controls>
            <source src="/recordings/{stem}.mp4" type="video/mp4">
        </video>
        <div id="commentary-overlay"></div>
    </div>
</div>
<button id="sidebar-toggle">COMMENTARY</button>
<div id="commentary-sidebar">
    <h2>Commentary</h2>
    <div id="sidebar-entries"></div>
</div>
<script>
(function() {{
    var video = document.getElementById('player');
    var overlay = document.getElementById('commentary-overlay');
    var sidebarToggle = document.getElementById('sidebar-toggle');
    var sidebar = document.getElementById('commentary-sidebar');
    var sidebarEntries = document.getElementById('sidebar-entries');
    var sidebarOpen = false;

    var COMMENTARY = {commentary_json};
    var DISPLAY_SECS = 10;

    var entries = [];
    for (var i = 0; i < COMMENTARY.length; i++) {{
        var c = COMMENTARY[i];
        var mins = Math.floor(c.time / 60);
        var secs = Math.floor(c.time % 60);
        var ts = mins + ':' + (secs < 10 ? '0' : '') + secs;
        var el = document.createElement('div');
        el.className = 'sidebar-entry ' + (c.style || 'normal');
        var safeText = c.text.replace(/</g, '&lt;').replace(/>/g, '&gt;');
        el.innerHTML = '<div class="entry-time">' + ts + '</div>' + safeText;
        el.dataset.time = c.time;
        el.addEventListener('click', (function(t) {{
            return function() {{ video.currentTime = t; video.play(); }};
        }})(c.time));
        sidebarEntries.appendChild(el);
        entries.push({{ el: el, time: c.time, text: c.text, style: c.style || 'normal' }});
    }}

    sidebarToggle.addEventListener('click', function() {{
        sidebarOpen = !sidebarOpen;
        sidebar.className = sidebarOpen ? 'open' : '';
        sidebarToggle.className = sidebarOpen ? 'active' : '';
    }});

    function updateCommentary() {{
        var now = video.currentTime;

        var lastVisible = null;
        for (var i = 0; i < entries.length; i++) {{
            if (entries[i].time <= now) {{
                entries[i].el.classList.add('visible');
                lastVisible = entries[i].el;
            }} else {{
                entries[i].el.classList.remove('visible');
            }}
        }}
        if (lastVisible && sidebarOpen) {{
            lastVisible.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
        }}

        overlay.innerHTML = '';
        for (var i = 0; i < COMMENTARY.length; i++) {{
            var c = COMMENTARY[i];
            if (c.time <= now && c.time > now - DISPLAY_SECS) {{
                var el = document.createElement('div');
                el.className = 'commentary-msg ' + (c.style || 'normal');
                el.textContent = c.text;
                var age = now - c.time;
                if (age > DISPLAY_SECS - 0.5) {{
                    el.style.opacity = Math.max(0, (DISPLAY_SECS - age) / 0.5);
                }}
                overlay.appendChild(el);
            }}
        }}

        requestAnimationFrame(updateCommentary);
    }}
    updateCommentary();

    video.addEventListener('seeking', function() {{
        overlay.innerHTML = '';
    }});

    video.addEventListener('error', function() {{
        var err = video.error;
        if (err) {{
            console.warn('Video error code=' + err.code + ': ' + (err.message || ''));
            var src = video.querySelector('source').src;
            video.removeAttribute('src');
            video.load();
            var newSource = document.createElement('source');
            newSource.src = src;
            newSource.type = 'video/mp4';
            video.appendChild(newSource);
            video.load();
        }}
    }});
}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _RecordingHandler(BaseHTTPRequestHandler):
    """Serves recording list, playback pages, and media files."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/recordings")
            self.end_headers()
        elif path == "/recordings":
            self._serve_recordings_list()
        elif path.startswith("/recordings/"):
            filename = path[len("/recordings/"):]
            if filename.endswith(".mp4") or filename.endswith(".json"):
                self._serve_file(filename)
            else:
                self._serve_playback_page(filename)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _serve_recordings_list(self):
        recordings_dir: Path = self.server.recordings_dir  # type: ignore[attr-defined]
        recordings = []
        if recordings_dir.is_dir():
            for mp4 in sorted(recordings_dir.glob("*.mp4"), reverse=True):
                info: dict = {
                    "filename": mp4.stem,
                    "size_mb": mp4.stat().st_size / 1_048_576,
                }
                json_path = mp4.with_suffix(".json")
                if json_path.is_file():
                    try:
                        meta = json.loads(json_path.read_text())
                        info["duration"] = meta.get("duration", 0)
                        info["started"] = meta.get("started", "")
                        info["name"] = meta.get("name", "unnamed")
                    except Exception:
                        pass
                recordings.append(info)
        host = self.headers.get("Host", "localhost")
        viewer_url = _viewer_link(host)
        body = _build_recordings_html(recordings, viewer_url).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_playback_page(self, stem: str):
        recordings_dir: Path = self.server.recordings_dir  # type: ignore[attr-defined]
        mp4_path = recordings_dir / f"{stem}.mp4"
        if not mp4_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        commentary: list = []
        meta: dict = {}
        json_path = mp4_path.with_suffix(".json")
        if json_path.is_file():
            try:
                meta = json.loads(json_path.read_text())
                commentary = meta.get("commentary", [])
            except Exception:
                pass

        host = self.headers.get("Host", "localhost")
        viewer_url = _viewer_link(host)
        body = _build_playback_html(stem, commentary, meta, viewer_url).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename: str):
        """Serve an MP4 or JSON file with range request support."""
        recordings_dir: Path = self.server.recordings_dir  # type: ignore[attr-defined]
        file_path = recordings_dir / filename
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        file_size = file_path.stat().st_size
        content_type = "application/json" if filename.endswith(".json") else "video/mp4"

        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            range_spec = range_header[6:]
            parts = range_spec.split("-", 1)
            try:
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
            except ValueError:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return

            if start >= file_size or end >= file_size:
                end = file_size - 1
            if start > end:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return

            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", length)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(remaining, 65536))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", file_size)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run(port: int = DEFAULT_PORT, recordings_dir: Path = DEFAULT_RECORDINGS_DIR) -> None:
    """Start the recording server (blocking)."""
    recordings_dir.mkdir(parents=True, exist_ok=True)

    srv = ThreadingHTTPServer(("0.0.0.0", port), _RecordingHandler)
    srv.recordings_dir = recordings_dir  # type: ignore[attr-defined]
    srv.daemon_threads = True

    # Graceful shutdown on SIGTERM
    def _handle_signal(signum, frame):
        logger.info("Recording server received signal %d, shutting down", signum)
        srv.shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "Recording server started on http://0.0.0.0:%d  (recordings: %s)",
        port,
        recordings_dir,
    )
    print(f"Recording server listening on http://0.0.0.0:{port}", flush=True)
    srv.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Standalone melonDS recording browser")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=DEFAULT_RECORDINGS_DIR,
        help=f"Directory containing .mp4/.json recordings (default {DEFAULT_RECORDINGS_DIR})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run(port=args.port, recordings_dir=args.recordings_dir)


if __name__ == "__main__":
    main()

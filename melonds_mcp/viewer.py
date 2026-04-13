"""Streaming viewer — HLS video + commentary, with separate snapshots page.

Pages:
- /           — HLS video playback with commentary overlay
- /snapshots  — Auto-updating screenshots with history browsing (debug)
- /recordings — Redirects to standalone recording server (port 8092)

SSE endpoints:
- /stream  — frame update notifications (used by /snapshots page)
- /commentary — commentary events with frame-synced timing (used by / page)
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


def _build_html(hls_port: int, stream_start_ms: int = 0) -> str:
    """Build the video stream page HTML with the HLS port baked in."""
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
a {{ color: #4caf50; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
#status-bar {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    font-size: 13px;
    color: #888;
    justify-content: center;
}}
#status-bar span {{
    white-space: nowrap;
}}
#stream-time, #buffer-info {{
    display: inline-block;
    min-width: 4.5em;
    text-align: right;
    font-variant-numeric: tabular-nums;
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
.nav-links {{
    font-size: 12px;
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
        <div id="commentary-overlay"></div>
    </div>
    <div id="status-bar">
        <span><span id="dot" class="dot waiting"></span><span id="status">Waiting</span></span>
        <button id="unmute-btn" class="muted">UNMUTE</button>
        <input id="volume-slider" type="range" min="0" max="100" value="50">
        <span id="vol-label">50%</span>
        <span>Time: <span id="stream-time">&mdash;</span></span>
        <span>Buffer: <span id="buffer-info">&mdash;</span></span>
    </div>
    <div class="nav-links">
        <a href="/snapshots">Snapshots</a> &middot;
        <a class="rec-link" href="/recordings">Recordings</a>
    </div>
</div>
<script>document.querySelectorAll('a.rec-link').forEach(function(a){{a.href=location.protocol+'//'+location.hostname+':8092/recordings';}});</script>
<button id="sidebar-toggle">COMMENTARY</button>
<div id="commentary-sidebar">
    <h2>Commentary</h2>
    <div id="sidebar-entries"></div>
</div>
<script>
(function() {{
    var video      = document.getElementById('player');
    var dot        = document.getElementById('dot');
    var statusEl   = document.getElementById('status');
    var bufInfo    = document.getElementById('buffer-info');
    var muteBtn    = document.getElementById('unmute-btn');
    var volSlider  = document.getElementById('volume-slider');
    var volLabel   = document.getElementById('vol-label');
    var streamTime = document.getElementById('stream-time');
    var overlay    = document.getElementById('commentary-overlay');

    var HLS_PORT = {hls_port};
    var STREAM_START_MS = {stream_start_ms};
    var hlsBaseUrl = 'http://' + location.hostname + ':' + HLS_PORT + '/hls/';

    video.volume = 0.5;

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
    function fmtTime(secs) {{
        var m = Math.floor(secs / 60);
        var s = Math.floor(secs % 60);
        return m + ':' + (s < 10 ? '0' : '') + s;
    }}
    function updateBufferInfo() {{
        if (video.buffered.length > 0) {{
            var cur = video.currentTime;
            streamTime.textContent = fmtTime(cur);
            var bufAhead = video.buffered.end(video.buffered.length - 1) - cur;
            var rate = video.playbackRate;
            var rateStr = (rate !== 1.0) ? ' (' + rate.toFixed(2) + 'x)' : '';
            var driftStr = '';
            if (liveOriginTime !== null) {{
                var elapsed = (Date.now() - liveOriginTime) / 1000;
                var wallTarget = liveOriginPosition + elapsed;
                var drift = (cur - wallTarget).toFixed(1);
                var sign = drift >= 0 ? '+' : '';
                driftStr = ' | drift ' + sign + drift + 's';
            }}
            bufInfo.textContent = bufAhead.toFixed(1) + 's' + driftStr + rateStr;
        }}
        requestAnimationFrame(updateBufferInfo);
    }}
    updateBufferInfo();

    function setStatus(cls, text) {{
        dot.className = 'dot ' + cls;
        statusEl.textContent = text;
    }}

    // -- MSE video loader --
    var mseReady = false;
    var lastSegmentName = null;
    var initFetched = false;
    var seekedToLive = false;
    var mediaSource = null;
    var sourceBuffer = null;
    var appendQueue = [];
    var appending = false;
    var pollTimer = null;

    var liveOriginTime = null;
    var liveOriginPosition = null;
    var LIVE_DRIFT_THRESHOLD = 2;

    function parseM3u8(text) {{
        var initUri = null, segments = [], ended = false;
        var lines = text.split('\\n');
        for (var i = 0; i < lines.length; i++) {{
            var line = lines[i].trim();
            if (line.indexOf('#EXT-X-MAP:') === 0) {{
                var m = line.match(/URI="([^"]+)"/);
                if (m) initUri = m[1];
            }} else if (line.length > 0 && line[0] !== '#') {{
                segments.push(line);
            }} else if (line === '#EXT-X-ENDLIST') {{
                ended = true;
            }}
        }}
        return {{ initUri: initUri, segments: segments, ended: ended }};
    }}

    function processAppendQueue() {{
        if (appending || appendQueue.length === 0 || !sourceBuffer) return;
        appending = true;
        try {{
            sourceBuffer.appendBuffer(appendQueue.shift());
        }} catch (e) {{
            console.error('appendBuffer error:', e);
            appending = false;
        }}
    }}

    function fetchAndAppend(url) {{
        return fetch(url).then(function(r) {{
            if (!r.ok) throw new Error('Fetch ' + r.status);
            return r.arrayBuffer();
        }}).then(function(buf) {{
            appendQueue.push(buf);
            processAppendQueue();
        }}).catch(function(e) {{
            console.error('Segment fetch error:', e);
        }});
    }}

    function pollPlaylist() {{
        fetch(hlsBaseUrl + 'stream.m3u8').then(function(r) {{
            if (!r.ok) return null;
            return r.text();
        }}).then(function(text) {{
            if (!text) return;
            var p = parseM3u8(text);
            if (!initFetched && p.initUri) {{
                initFetched = true;
                fetchAndAppend(hlsBaseUrl + p.initUri);
            }}
            var segsToFetch;
            if (lastSegmentName === null) {{
                segsToFetch = p.segments;
            }} else {{
                var idx = p.segments.indexOf(lastSegmentName);
                segsToFetch = (idx >= 0) ? p.segments.slice(idx + 1) : p.segments;
            }}
            for (var i = 0; i < segsToFetch.length; i++) {{
                fetchAndAppend(hlsBaseUrl + segsToFetch[i]);
            }}
            if (segsToFetch.length > 0) {{
                lastSegmentName = segsToFetch[segsToFetch.length - 1];
            }}
        }}).catch(function() {{}});
    }}

    function startMSE() {{
        if (!('MediaSource' in window)) {{
            setStatus('error', 'MSE not supported');
            return;
        }}
        mediaSource = new MediaSource();
        video.src = URL.createObjectURL(mediaSource);

        mediaSource.addEventListener('sourceopen', function() {{
            var codecs = [
                'video/mp4; codecs="avc1.42001e,mp4a.40.2"',
                'video/mp4; codecs="avc1.420029,mp4a.40.2"',
                'video/mp4; codecs="avc1.4d001e,mp4a.40.2"'
            ];
            for (var i = 0; i < codecs.length; i++) {{
                if (MediaSource.isTypeSupported(codecs[i])) {{
                    try {{
                        sourceBuffer = mediaSource.addSourceBuffer(codecs[i]);
                        break;
                    }} catch(e) {{ }}
                }}
            }}
            if (!sourceBuffer) {{
                setStatus('error', 'No supported codec');
                return;
            }}

            sourceBuffer.addEventListener('updateend', function() {{
                appending = false;
                if (!seekedToLive && appendQueue.length === 0 && video.buffered.length > 0) {{
                    var bufStart = video.buffered.start(0);
                    var bufEnd = video.buffered.end(video.buffered.length - 1);
                    var wallPos = (Date.now() - STREAM_START_MS) / 1000;
                    var seekTarget = Math.min(wallPos, bufEnd - 1.0);
                    seekTarget = Math.max(seekTarget, bufStart);
                    video.currentTime = seekTarget;
                    liveOriginPosition = video.currentTime;
                    liveOriginTime = Date.now();
                    seekedToLive = true;
                    video.play().catch(function() {{}});
                }}
                if (video.readyState >= 3) {{
                    video.play().catch(function() {{}});
                }}
                if (appendQueue.length === 0 && video.buffered.length > 0 && video.currentTime > 60) {{
                    var removeEnd = video.currentTime - 30;
                    if (removeEnd > video.buffered.start(0) + 10) {{
                        try {{
                            appending = true;
                            sourceBuffer.remove(video.buffered.start(0), removeEnd);
                            return;
                        }} catch(e) {{
                            appending = false;
                        }}
                    }}
                }}
                processAppendQueue();
            }});

            sourceBuffer.addEventListener('error', function(e) {{
                console.error('SourceBuffer error:', e);
                appending = false;
            }});

            mseReady = true;
            setStatus('buffering', 'Buffering');
            pollPlaylist();
            pollTimer = setInterval(pollPlaylist, 1000);
            video.play().catch(function() {{}});
        }});

        mediaSource.addEventListener('sourceclose', function() {{
            clearInterval(pollTimer);
        }});
    }}

    video.addEventListener('waiting', function() {{
        setStatus('buffering', 'Buffering');
        if (liveOriginTime !== null) {{
            var elapsed = (Date.now() - liveOriginTime) / 1000;
            liveOriginPosition += elapsed;
            liveOriginTime = null;
            video.playbackRate = 1.0;
        }}
    }});
    video.addEventListener('playing', function() {{
        setStatus('playing', 'Playing');
        if (seekedToLive && liveOriginTime === null) {{
            liveOriginPosition = video.currentTime;
            liveOriginTime = Date.now();
        }}
    }});

    function maintainLiveEdge() {{
        if (mseReady && !video.paused &&
            liveOriginTime !== null && video.buffered.length > 0) {{
            var elapsed = (Date.now() - liveOriginTime) / 1000;
            var wallTarget = liveOriginPosition + elapsed;
            var bufEnd = video.buffered.end(video.buffered.length - 1);
            var target = Math.min(wallTarget, bufEnd - 1.0);
            var drift = target - video.currentTime;
            if (drift > 5) {{
                video.playbackRate = 1.05;
            }} else if (drift > LIVE_DRIFT_THRESHOLD) {{
                video.playbackRate = 1.02;
            }} else if (drift < -LIVE_DRIFT_THRESHOLD) {{
                video.playbackRate = 0.98;
            }} else {{
                video.playbackRate = 1.0;
            }}
        }}
        setTimeout(maintainLiveEdge, 1000);
    }}
    maintainLiveEdge();

    function waitForStream() {{
        setStatus('waiting', 'Waiting for stream');
        fetch(hlsBaseUrl + 'stream.m3u8', {{method: 'HEAD'}}).then(function(r) {{
            if (r.ok) {{ startMSE(); }}
            else {{ setTimeout(waitForStream, 1000); }}
        }}).catch(function() {{
            setTimeout(waitForStream, 1000);
        }});
    }}
    waitForStream();

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
        var now = video.currentTime || 0;
        var effectiveTime = Math.min(streamTime, now);
        commentaryQueue.push({{streamTime: effectiveTime, text: text, style: style || 'normal', shown: false, inSidebar: false}});
    }}

    function updateCommentary() {{
        if (!mseReady) {{
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


def _build_snapshots_html() -> str:
    """Build the snapshots page — auto-updating screenshots with history browsing."""
    return """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>melonDS Snapshots</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #111;
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
#screenshot {
    image-rendering: pixelated;
    border: 2px solid #333;
    border-radius: 4px;
    width: 512px;
    height: 768px;
    background: #000;
}
h1 {
    font-size: 16px;
    font-weight: normal;
    color: #666;
    letter-spacing: 2px;
    text-transform: uppercase;
}
a { color: #4caf50; text-decoration: none; }
a:hover { text-decoration: underline; }
#status-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    font-size: 13px;
    color: #888;
    justify-content: center;
}
#status-bar span {
    white-space: nowrap;
}
.dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
}
.dot.connected { background: #4caf50; }
.dot.waiting   { background: #666; }
#mode-badge {
    padding: 1px 8px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 1px;
}
#mode-badge.live    { background: #2e7d32; color: #c8e6c9; }
#mode-badge.history { background: #e65100; color: #ffe0b2; }
#frame-count {
    display: inline-block;
    min-width: 4.5em;
    text-align: right;
    font-variant-numeric: tabular-nums;
}
#hint {
    font-size: 11px;
    color: #555;
}
.nav-links {
    font-size: 12px;
    color: #555;
}
</style>
</head>
<body>
<div id="container">
    <h1>melonDS Snapshots</h1>
    <img id="screenshot" alt="DS Screen">
    <div id="status-bar">
        <span><span id="dot" class="dot waiting"></span><span id="status">Connecting</span></span>
        <span id="mode-badge" class="live">LIVE</span>
        <span>Frame: <span id="frame-count">&mdash;</span></span>
        <span id="history-pos"></span>
    </div>
    <div id="hint">Arrow keys: browse history &middot; Space: return to live</div>
    <div class="nav-links">
        <a href="/">Video Stream</a> &middot;
        <a class="rec-link" href="/recordings">Recordings</a>
    </div>
</div>
<script>document.querySelectorAll('a.rec-link').forEach(function(a){{a.href=location.protocol+'//'+location.hostname+':8092/recordings';}});</script>
<script>
(function() {
    var img        = document.getElementById('screenshot');
    var dot        = document.getElementById('dot');
    var statusEl   = document.getElementById('status');
    var frameTxt   = document.getElementById('frame-count');
    var modeBadge  = document.getElementById('mode-badge');
    var historyPos = document.getElementById('history-pos');

    var mode = 'live';  // 'live' or 'history'
    var history = [];
    var browseIdx = -1;
    var sessionId = '';

    function setMode(m) {
        mode = m;
        if (m === 'live') {
            modeBadge.className = 'live';
            modeBadge.textContent = 'LIVE';
            historyPos.textContent = '';
            // Show latest screenshot
            if (history.length > 0) {
                showScreenshot(history[history.length - 1]);
            }
        } else {
            modeBadge.className = 'history';
            modeBadge.textContent = 'HISTORY';
            historyPos.textContent = (browseIdx + 1) + ' / ' + history.length;
        }
    }

    function showScreenshot(frame) {
        img.src = '/screenshot?frame=' + frame + '&s=' + sessionId;
        frameTxt.textContent = frame;
    }

    function goLive() {
        setMode('live');
        browseIdx = -1;
    }

    function browseBack() {
        if (history.length === 0) return;
        if (mode === 'live') {
            setMode('history');
            browseIdx = history.length - 1;
        } else {
            browseIdx = Math.max(0, browseIdx - 1);
        }
        showScreenshot(history[browseIdx]);
        historyPos.textContent = (browseIdx + 1) + ' / ' + history.length;
    }

    function browseForward() {
        if (mode === 'live' || history.length === 0) return;
        if (browseIdx < history.length - 1) {
            browseIdx++;
            showScreenshot(history[browseIdx]);
            historyPos.textContent = (browseIdx + 1) + ' / ' + history.length;
        }
    }

    document.addEventListener('keydown', function(e) {
        if (e.key === 'ArrowLeft')  { e.preventDefault(); browseBack(); }
        if (e.key === 'ArrowRight') { e.preventDefault(); browseForward(); }
        if (e.key === ' ')          { e.preventDefault(); goLive(); }
    });

    // -- SSE for frame updates --
    function connectSSE() {
        var es = new EventSource('/stream');
        es.addEventListener('init', function(e) {
            var d = JSON.parse(e.data);
            sessionId = d.session || '';
            history.push(d.frame);
            dot.className = 'dot connected';
            statusEl.textContent = 'Connected';
            if (mode === 'live') showScreenshot(d.frame);
        });
        es.addEventListener('frame', function(e) {
            var d = JSON.parse(e.data);
            history.push(d.frame);
            if (mode === 'live') {
                showScreenshot(d.frame);
            }
        });
        es.onerror = function() {
            dot.className = 'dot waiting';
            statusEl.textContent = 'Reconnecting';
            es.close();
            setTimeout(connectSSE, 2000);
        };
    }
    connectSSE();
})();
</script>
</body>
</html>
"""


class _ViewerHandler(BaseHTTPRequestHandler):
    """Serves video stream, snapshots pages and SSE streams.  Recordings redirect to port 8092."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._serve_html()
        elif path == "/snapshots":
            self._serve_snapshots()
        elif path == "/recordings" or path.startswith("/recordings/"):
            self._redirect_to_recording_server()
        elif path == "/screenshot":
            self._serve_screenshot()
        elif path == "/stream":
            self._serve_sse()
        elif path == "/commentary":
            self._serve_commentary_sse()
        elif path == "/status":
            self._serve_status()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/commentary":
            self._handle_post_commentary()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # -- POST handlers --------------------------------------------------------

    _VALID_STYLES = {"normal", "excited", "whisper"}

    def _handle_post_commentary(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Empty body")
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        text = data.get("text", "").strip()
        if not text:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing or empty 'text'")
            return
        style = data.get("style", "normal")
        if style not in self._VALID_STYLES:
            style = "normal"

        viewer: ViewerServer = self.server.viewer  # type: ignore[attr-defined]
        frame = data.get("frame") if data.get("frame") is not None else viewer.get_current_frame()
        viewer.add_commentary(frame, text, style)
        logger.info("Commentary via POST at frame %d: %s", frame, text[:80])

        resp = json.dumps({"ok": True, "frame": frame}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(resp))
        self.end_headers()
        self.wfile.write(resp)

    def _serve_status(self):
        viewer: ViewerServer = self.server.viewer  # type: ignore[attr-defined]
        frame = viewer.get_current_frame()
        resp = json.dumps({"frame": frame}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(resp))
        self.end_headers()
        self.wfile.write(resp)

    # -- endpoints ---------------------------------------------------------

    def _serve_html(self):
        viewer: ViewerServer = self.server.viewer  # type: ignore[attr-defined]
        body = _build_html(viewer._hls_port, viewer._stream_start_ms).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_snapshots(self):
        body = _build_snapshots_html().encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _redirect_to_recording_server(self):
        """Redirect recording requests to the standalone recording server on port 8092."""
        host = self.headers.get("Host", "localhost")
        hostname = host.split(":")[0]
        # Preserve the full path (list, playback, or file)
        target = f"//{hostname}:8092{self.path}"
        self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
        self.send_header("Location", target)
        self.end_headers()

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
    """Streaming viewer — HLS video + commentary, with separate snapshots page.

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
        self._stream_start_ms: int = 0  # wall-clock start, set in start()

        # Frame/screenshot SSE clients
        self._clients: list[queue.Queue[str]] = []
        self._clients_lock = threading.Lock()

        # Commentary SSE clients
        self._commentary_clients: list[queue.Queue[str]] = []
        self._commentary_lock = threading.Lock()

        # Journal reference for forwarding commentary to renderer/recorder
        self._journal = None

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
        import time as _time
        self._stream_start_ms = int(_time.time() * 1000)
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

    def set_journal(self, journal) -> None:
        """Set or clear the journal writer for forwarding commentary to the renderer."""
        self._journal = journal

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
        stream_time = max(0.0, (frame - self._stream_start_frame) / 60.0)
        event_data = json.dumps({
            "frame": frame,
            "text": text,
            "style": style,
            "stream_time": stream_time,
        })
        with self._commentary_lock:
            for q in self._commentary_clients:
                try:
                    q.put_nowait(event_data)
                except queue.Full:
                    pass

        # Forward to journal for recording
        if self._journal is not None:
            try:
                self._journal.write_commentary(stream_time, text, style)
            except Exception:
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

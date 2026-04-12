"""HLS video streamer — pipes DS frames + audio through ffmpeg to serve live HLS."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .emulator import EmulatorState

logger = logging.getLogger(__name__)

# DS constants
_FRAME_WIDTH = 256
_FRAME_HEIGHT = 384  # both screens stacked
_FRAME_RGB_SIZE = _FRAME_WIDTH * _FRAME_HEIGHT * 3  # 294912 bytes
_SAMPLE_RATE = 48000
_FPS = 60
_SAMPLES_PER_FRAME = _SAMPLE_RATE // _FPS  # 800
_MAX_BUFFER_SECS = 30.0  # max seconds content can lead wall-clock before throttling

class _StreamHandler(BaseHTTPRequestHandler):
    """Serves HLS segment files only — the viewer page lives in viewer.py."""

    def log_message(self, format, *args):
        pass  # silence per-request logs

    def _send_cors_error(self, code):
        """Send an error response with CORS headers so cross-origin
        fetches from the viewer page get a clean error instead of an
        opaque CORS block."""
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/hls/"):
            self._serve_hls_file(path[5:])  # strip /hls/ prefix
        else:
            self._send_cors_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        path = self.path.split("?")[0]
        if path.startswith("/hls/"):
            self._serve_hls_file(path[5:], head_only=True)
        else:
            self._send_cors_error(HTTPStatus.NOT_FOUND)

    def _serve_hls_file(self, filename: str, head_only: bool = False):
        streamer: HLSStreamer = self.server.streamer  # type: ignore[attr-defined]
        file_path = streamer.hls_dir / filename
        if not file_path.is_file():
            self._send_cors_error(HTTPStatus.NOT_FOUND)
            return

        if filename.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
            cache = "no-cache, no-store"
        elif filename.endswith(".ts"):
            content_type = "video/mp2t"
            cache = "public, max-age=300"
        elif filename.endswith(".m4s") or filename.endswith(".mp4"):
            content_type = "video/mp4"
            cache = "public, max-age=300"
        else:
            content_type = "application/octet-stream"
            cache = "no-cache"

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)


class HLSStreamer:
    """Streams DS video + audio via ffmpeg → HLS for browser playback.

    Usage::

        streamer = HLSStreamer(holder, port=8091)
        streamer.start()           # launches ffmpeg + HTTP server
        # ... emulation happens, on_cycle callback feeds frames to ffmpeg ...
        streamer.stop()
    """

    def __init__(self, holder: EmulatorState, port: int = 8091):
        self._holder = holder
        self._port = port
        self._hls_dir = Path(tempfile.mkdtemp(prefix="melonds_hls_"))
        self._video_fifo = self._hls_dir / "video.pipe"
        self._audio_fifo = self._hls_dir / "audio.pipe"
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._frame_writer: threading.Thread | None = None
        self._frame_queue: queue.Queue[tuple[bytes, bytes] | None] = queue.Queue(maxsize=300)
        self._running = False
        # Audio normalization buffer — accumulates raw PCM and emits
        # exactly _SAMPLES_PER_FRAME samples per cycle to keep ffmpeg's
        # audio stream perfectly aligned with the video frame rate.
        self._audio_buf = bytearray()
        # Real-time rate limiter state — used by the video writer thread
        # (not _on_cycle) to prevent content from leading wall-clock by
        # more than _MAX_BUFFER_SECS.  Keeping the throttle in the writer
        # thread avoids sleeping while the emulator lock is held.
        self._rt_origin: float | None = None  # wall-clock time of first frame
        self._rt_frames: int = 0  # frames written to ffmpeg since origin
        self._drop_count: int = 0  # frames dropped due to full queue

    @property
    def port(self) -> int:
        return self._port

    @property
    def hls_dir(self) -> Path:
        return self._hls_dir

    def start(self) -> None:
        """Start ffmpeg pipeline and HTTP server."""
        if self._running:
            return

        self._running = True

        # Enable audio capture in the C library
        emu = self._holder._require_rom()
        emu.audio_enable()

        # Create named pipes for ffmpeg input
        os.mkfifo(str(self._video_fifo))
        os.mkfifo(str(self._audio_fifo))

        # Start ffmpeg
        self._start_ffmpeg()

        # Start unified FIFO writer thread (must happen after ffmpeg starts
        # since open() on a FIFO blocks until the other end opens)
        self._frame_writer = threading.Thread(
            target=self._write_frames,
            daemon=True,
        )
        self._frame_writer.start()

        # Start HTTP server
        srv = ThreadingHTTPServer(("0.0.0.0", self._port), _StreamHandler)
        srv.streamer = self  # type: ignore[attr-defined]
        srv.daemon_threads = True
        self._http_server = srv
        self._http_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._http_thread.start()

        # Register per-cycle callback
        self._holder.on_each_cycle(self._on_cycle)

        logger.info(
            "HLS streamer started on http://0.0.0.0:%d (hls dir: %s)",
            self._port,
            self._hls_dir,
        )

    def _start_ffmpeg(self) -> None:
        """Launch the ffmpeg process reading from the two FIFOs."""
        cmd = [
            "ffmpeg",
            "-y",
            # Video input: raw RGB frames from FIFO
            "-f", "rawvideo",
            "-pixel_format", "rgb24",
            "-video_size", f"{_FRAME_WIDTH}x{_FRAME_HEIGHT}",
            "-framerate", str(_FPS),
            "-i", str(self._video_fifo),
            # Audio input: raw s16le stereo PCM from FIFO
            "-f", "s16le",
            "-ar", str(_SAMPLE_RATE),
            "-ac", "2",
            "-i", str(self._audio_fifo),
            # Video encoding
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", str(_FPS * 2),  # keyframe every 2 seconds
            # Audio encoding
            "-c:a", "aac",
            "-b:a", "128k",
            # HLS output — use fMP4 segments for sample-accurate audio
            # timing (MPEG-TS loses ~23ms per segment at AAC frame boundaries)
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_flags", "append_list",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(self._hls_dir / "segment_%05d.m4s"),
            str(self._hls_dir / "stream.m3u8"),
        ]

        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        logger.info("ffmpeg started (pid %d)", self._ffmpeg_proc.pid)

    def _write_frames(self) -> None:
        """Unified writer thread: drains (video, audio) tuples and writes
        both FIFOs in lockstep so ffmpeg always receives matching data.

        Real-time throttling is applied once per frame — this keeps audio
        and video perfectly synchronized and prevents either FIFO from
        racing ahead and deadlocking ffmpeg.

        The two FIFOs are opened concurrently because ffmpeg opens its
        inputs sequentially and won't open the audio FIFO until it has
        received some video data.  A sequential open here would deadlock:
        the writer can't send video until both FIFOs are open, but ffmpeg
        won't open the second until video flows on the first.
        """
        logger.info("Frame writer thread starting")
        frames_written = 0
        af_holder: list = []

        def _open_audio():
            af_holder.append(open(self._audio_fifo, "wb"))

        try:
            # Open audio FIFO in a background thread so it doesn't block
            # the video FIFO open.  ffmpeg opens inputs sequentially
            # (video first) so the video open unblocks first; the audio
            # open unblocks once ffmpeg gets to its second input.
            audio_opener = threading.Thread(target=_open_audio, daemon=True)
            audio_opener.start()
            with open(self._video_fifo, "wb") as vf:
                audio_opener.join()
                if not af_holder:
                    logger.error("Audio FIFO failed to open")
                    return
                af = af_holder[0]
                logger.info("Both FIFOs opened for writing")
                while self._running:
                    try:
                        pair = self._frame_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if pair is None:
                        break
                    video_data, audio_data = pair

                    # Real-time throttle — sleep when content is too far
                    # ahead of wall-clock.  Applied once before both writes
                    # so neither FIFO races ahead.
                    now = time.monotonic()
                    if self._rt_origin is None:
                        self._rt_origin = now
                    else:
                        self._rt_frames += 1
                        content_secs = self._rt_frames / _FPS
                        wall_secs = now - self._rt_origin
                        ahead = content_secs - wall_secs
                        if ahead > _MAX_BUFFER_SECS:
                            sleep_dur = ahead - _MAX_BUFFER_SECS
                            if sleep_dur > 1.0:
                                logger.debug(
                                    "Streamer throttle: sleeping %.3fs (%.1fs ahead, frame %d, q=%d)",
                                    sleep_dur, ahead, self._rt_frames, self._frame_queue.qsize(),
                                )
                            time.sleep(sleep_dur)

                    t_write = time.monotonic()
                    try:
                        vf.write(video_data)
                        af.write(audio_data)
                    except BrokenPipeError:
                        logger.warning("FIFO pipe broken")
                        break
                    write_dur = time.monotonic() - t_write
                    frames_written += 1
                    if write_dur > 1.0:
                        logger.warning(
                            "FIFO write blocked %.3fs (frame %d, qsize=%d)",
                            write_dur, frames_written, self._frame_queue.qsize(),
                        )
        except OSError as e:
            if self._running:
                logger.error("Error opening FIFOs: %s", e)
        except Exception:
            logger.error("Frame writer thread crashed", exc_info=True)
        finally:
            if af_holder:
                try:
                    af_holder[0].close()
                except Exception:
                    pass
        logger.info("Frame writer thread exiting (wrote %d frames)", frames_written)

    def _on_cycle(self) -> None:
        """Called after each emulator cycle — push frame + audio to ffmpeg."""
        if not self._running:
            return

        emu = self._holder.emu
        if emu is None:
            return

        t_cycle_start = time.monotonic()

        # Grab raw RGB frame
        raw_rgb = emu.screenshot()

        # Drain audio samples generated by this cycle into the normalization buffer
        audio_data = emu.audio_read()
        if audio_data:
            self._audio_buf.extend(audio_data)

        # Emit exactly _SAMPLES_PER_FRAME stereo samples (4 bytes each) to
        # keep the audio stream perfectly aligned with the video frame rate.
        # 48000 / 60 = 800.0 exactly, so no fractional accumulation needed.
        needed = _SAMPLES_PER_FRAME * 4  # 800 samples * 4 bytes (s16le stereo)
        if len(self._audio_buf) >= needed:
            normalized = bytes(self._audio_buf[:needed])
            del self._audio_buf[:needed]
        else:
            # Pad with silence if SPU produced fewer samples than expected
            normalized = bytes(self._audio_buf) + b"\x00" * (needed - len(self._audio_buf))
            self._audio_buf.clear()

        # Non-blocking enqueue — the real-time throttle lives in the writer
        # thread (_write_frames) so that _on_cycle never sleeps while the
        # emulator lock is held.  If the queue is full the frame is dropped;
        # the 300-frame (5s) queue provides ample runway.
        try:
            self._frame_queue.put_nowait((raw_rgb, normalized))
        except queue.Full:
            self._drop_count += 1
            # Log first drop and then periodically (every 300 = ~5s at 60fps)
            if self._drop_count == 1 or self._drop_count % 300 == 0:
                logger.warning(
                    "Streamer queue full — dropped %d frames so far (frame %d)",
                    self._drop_count, self._holder.frame_count,
                )
            # Yield CPU time so ffmpeg (separate process) can encode and
            # drain the FIFO pipes.  Without this, a tight emulation loop
            # starves ffmpeg and the queues stay permanently full.
            time.sleep(0)

        elapsed = time.monotonic() - t_cycle_start
        if elapsed > 0.5:
            logger.warning(
                "Streamer slow _on_cycle: %.3fs (frame %d, q=%d)",
                elapsed, self._holder.frame_count,
                self._frame_queue.qsize(),
            )

    def stop(self) -> None:
        """Shut down ffmpeg and HTTP server."""
        if not self._running:
            return

        logger.info(
            "HLS streamer shutting down (wrote %d frames, dropped %d)",
            self._rt_frames, self._drop_count,
        )
        self._running = False
        self._rt_origin = None
        self._rt_frames = 0
        self._drop_count = 0
        self._holder.remove_cycle_callback(self._on_cycle)

        # Disable audio capture
        try:
            if self._holder.emu is not None:
                self._holder.emu.audio_disable()
        except Exception:
            logger.warning("Error disabling audio capture during shutdown", exc_info=True)

        # Signal writer thread to exit
        try:
            self._frame_queue.put_nowait(None)
        except queue.Full:
            pass

        # Terminate ffmpeg
        if self._ffmpeg_proc is not None:
            self._ffmpeg_proc.terminate()
            try:
                self._ffmpeg_proc.wait(timeout=5)
                logger.debug("ffmpeg exited with code %d", self._ffmpeg_proc.returncode)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg did not exit in 5s, killing")
                self._ffmpeg_proc.kill()
            # Read any ffmpeg stderr for diagnostics
            try:
                stderr = self._ffmpeg_proc.stderr.read()
                if stderr:
                    last_lines = stderr.decode("utf-8", errors="replace").strip().splitlines()[-10:]
                    logger.debug("ffmpeg last stderr:\n  %s", "\n  ".join(last_lines))
            except Exception:
                pass
            self._ffmpeg_proc = None

        # Stop HTTP server
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server = None
            self._http_thread = None

        # Clean up temp files
        try:
            shutil.rmtree(self._hls_dir, ignore_errors=True)
        except Exception:
            logger.warning("Error cleaning up HLS temp dir", exc_info=True)

        logger.info("HLS streamer stopped")

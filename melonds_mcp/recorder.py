"""Session recorder — writes DS frames + audio to a permanent MP4 file.

Runs alongside the HLS streamer in the renderer subprocess.  The streamer
tees each (video, audio) frame pair to the recorder via write_frame().
Commentary events arrive from the journal and are saved to a companion
JSON file when the recording stops.

Output files (in recordings_dir):
    YYYYMMDD_HHMMSS.mp4  — fragmented MP4 (playable even on unclean shutdown)
    YYYYMMDD_HHMMSS.json — metadata + timestamped commentary
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# DS constants (must match streamer.py)
_FRAME_WIDTH = 256
_FRAME_HEIGHT = 384
_FRAME_RGB_SIZE = _FRAME_WIDTH * _FRAME_HEIGHT * 3
_SAMPLE_RATE = 48000
_FPS = 60
_SAMPLES_PER_FRAME = _SAMPLE_RATE // _FPS  # 800


class SessionRecorder:
    """Records DS video + audio to a permanent MP4 file.

    Usage::

        recorder = SessionRecorder(recordings_dir)
        recorder.start()
        # ... streamer calls recorder.write_frame(video, audio) each frame ...
        # ... journal delivers recorder.add_commentary(time, text, style) ...
        recorder.stop()  # finalizes MP4 + writes companion JSON
    """

    def __init__(self, recordings_dir: Path):
        self._recordings_dir = recordings_dir
        self._recordings_dir.mkdir(parents=True, exist_ok=True)

        self._timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._mp4_path = self._recordings_dir / f"{self._timestamp}.mp4"
        self._json_path = self._recordings_dir / f"{self._timestamp}.json"
        self._started_at = datetime.now(timezone.utc).isoformat()

        self._tmp_dir = Path(tempfile.mkdtemp(prefix="melonds_rec_"))
        self._video_fifo = self._tmp_dir / "video.pipe"
        self._audio_fifo = self._tmp_dir / "audio.pipe"

        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_log = None
        self._frame_writer: threading.Thread | None = None
        self._frame_queue: queue.Queue[tuple[bytes, bytes] | None] = queue.Queue(
            maxsize=300
        )
        self._running = False
        self._frames_written = 0

        self._commentary: list[dict] = []
        self._commentary_lock = threading.Lock()

    @property
    def mp4_path(self) -> Path:
        return self._mp4_path

    def start(self) -> None:
        """Start ffmpeg and the writer thread."""
        if self._running:
            return

        self._running = True

        os.mkfifo(str(self._video_fifo))
        os.mkfifo(str(self._audio_fifo))

        self._start_ffmpeg()

        self._frame_writer = threading.Thread(
            target=self._write_frames, name="recorder-writer", daemon=True
        )
        self._frame_writer.start()

        logger.info(
            "Session recorder started: %s (tmp: %s)", self._mp4_path, self._tmp_dir
        )

    def _start_ffmpeg(self) -> None:
        """Launch ffmpeg writing fragmented MP4."""
        cmd = [
            "ffmpeg",
            "-y",
            # Video input
            "-probesize", "32",
            "-analyzeduration", "0",
            "-f", "rawvideo",
            "-pixel_format", "rgb24",
            "-video_size", f"{_FRAME_WIDTH}x{_FRAME_HEIGHT}",
            "-framerate", str(_FPS),
            "-i", str(self._video_fifo),
            # Audio input
            "-probesize", "32",
            "-analyzeduration", "0",
            "-f", "s16le",
            "-ar", str(_SAMPLE_RATE),
            "-ac", "2",
            "-i", str(self._audio_fifo),
            # Video encoding
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-g", str(_FPS * 2),
            # Audio encoding
            "-c:a", "aac",
            "-b:a", "128k",
            # Fragmented MP4 output — playable even without clean shutdown
            "-movflags", "+frag_keyframe+empty_moov",
            "-f", "mp4",
            str(self._mp4_path),
        ]

        self._ffmpeg_log = open(self._tmp_dir / "ffmpeg.log", "w")
        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._ffmpeg_log,
        )
        logger.info("Recorder ffmpeg started (pid %d)", self._ffmpeg_proc.pid)

    def _write_frames(self) -> None:
        """Writer thread: drains queue and writes to FIFOs.

        Uses the same concurrent FIFO open pattern as HLSStreamer to avoid
        deadlock with ffmpeg's sequential input opening.
        """
        logger.info("Recorder writer thread starting")
        af_holder: list = []

        def _open_audio():
            af_holder.append(open(self._audio_fifo, "wb"))

        try:
            audio_opener = threading.Thread(target=_open_audio, daemon=True)
            audio_opener.start()
            with open(self._video_fifo, "wb") as vf:
                # Primer frame so ffmpeg finishes probing video input
                vf.write(b"\x00" * _FRAME_RGB_SIZE)
                vf.flush()
                audio_opener.join(timeout=10.0)
                if not af_holder:
                    logger.error("Recorder audio FIFO failed to open")
                    return
                af = af_holder[0]

                # Enlarge pipe buffers
                _F_SETPIPE_SZ = 1031
                _PIPE_BUF_TARGET = 1 << 20
                for pipe_fd, label in [(vf, "rec-video"), (af, "rec-audio")]:
                    try:
                        fcntl.fcntl(pipe_fd.fileno(), _F_SETPIPE_SZ, _PIPE_BUF_TARGET)
                    except OSError:
                        pass

                # Matching silence for primer frame
                af.write(b"\x00" * (_SAMPLES_PER_FRAME * 4))
                af.flush()
                logger.info("Recorder FIFOs opened")

                while self._running:
                    try:
                        pair = self._frame_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if pair is None:
                        break
                    video_data, audio_data = pair
                    try:
                        vf.write(video_data)
                        af.write(audio_data)
                    except BrokenPipeError:
                        logger.warning("Recorder FIFO broken")
                        break
                    self._frames_written += 1
        except OSError as e:
            if self._running:
                logger.error("Recorder FIFO error: %s", e)
        except Exception:
            logger.error("Recorder writer thread crashed", exc_info=True)
        finally:
            if af_holder:
                try:
                    af_holder[0].close()
                except Exception:
                    pass
        logger.info(
            "Recorder writer thread exiting (wrote %d frames)", self._frames_written
        )

    def write_frame(self, video_data: bytes, audio_data: bytes) -> None:
        """Enqueue a frame for recording. Non-blocking — drops if full."""
        if not self._running:
            return
        try:
            self._frame_queue.put_nowait((video_data, audio_data))
        except queue.Full:
            pass  # silent drop — acceptable for recording

    def add_commentary(
        self, stream_time: float, text: str, style: str = "normal"
    ) -> None:
        """Record a commentary event with its timestamp."""
        with self._commentary_lock:
            self._commentary.append({
                "time": stream_time,
                "text": text,
                "style": style,
            })

    def stop(self) -> None:
        """Finalize the recording: close ffmpeg, write companion JSON."""
        if not self._running:
            return

        logger.info("Recorder stopping (wrote %d frames)", self._frames_written)
        self._running = False

        # Signal writer thread to exit
        try:
            self._frame_queue.put_nowait(None)
        except queue.Full:
            pass

        # Wait for writer thread
        if self._frame_writer and self._frame_writer.is_alive():
            self._frame_writer.join(timeout=10.0)

        # Terminate ffmpeg
        if self._ffmpeg_proc is not None:
            self._ffmpeg_proc.terminate()
            try:
                self._ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait(timeout=2)
            # Log last few lines for diagnostics
            try:
                self._ffmpeg_log.close()
                log_path = self._tmp_dir / "ffmpeg.log"
                if log_path.is_file():
                    last_lines = (
                        log_path.read_text(errors="replace").strip().splitlines()[-5:]
                    )
                    if last_lines:
                        logger.debug(
                            "Recorder ffmpeg last stderr:\n  %s",
                            "\n  ".join(last_lines),
                        )
            except Exception:
                pass
            self._ffmpeg_proc = None

        # Write companion JSON
        duration = self._frames_written / _FPS
        with self._commentary_lock:
            commentary = list(self._commentary)
        meta = {
            "started": self._started_at,
            "duration": round(duration, 2),
            "frames": self._frames_written,
            "commentary": commentary,
        }
        try:
            self._json_path.write_text(json.dumps(meta, indent=2))
            logger.info("Recorder wrote metadata: %s", self._json_path)
        except Exception:
            logger.error("Failed to write recorder JSON", exc_info=True)

        # Clean up temp dir
        try:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        except Exception:
            pass

        logger.info("Session recorder stopped: %s", self._mp4_path)

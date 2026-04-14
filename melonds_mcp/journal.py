"""Input journal for the rendering emulator — durable stream of replay events.

The main emulator writes journal entries as it processes MCP/bridge commands.
A separate rendering process reads them and replays at its own pace for the HLS
video stream and session recording.

Transport: append-only JSONL file, tail-followed by the reader.

This design decouples the renderer from the MCP server's process lifecycle.
The renderer survives server exit, drains remaining journal entries, and
finalises the recording before exiting.

Entry types:
  {"type":"frames","count":N,"buttons":[...],"touch_x":X,"touch_y":Y}
  {"type":"load_state","path":"/abs/path/to/state.dst"}
  {"type":"reset"}
  {"type":"load_rom","rom_path":"/abs/path/to/rom.nds"}
  {"type":"commentary","stream_time":12.5,"text":"...","style":"normal"}
  {"type":"sync","state_path":"/abs/path/to/state.dst"}
  {"type":"shutdown"}
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class JournalWriter:
    """Writes journal entries to an append-only JSONL file.

    All writes are durable — no queue, no drops.  A threading lock ensures
    safety when the MCP tool thread and bridge thread write concurrently.
    """

    def __init__(self, journal_path: str) -> None:
        self._journal_path = journal_path
        self._file = None
        self._lock = threading.Lock()
        self._running = False
        self._shutdown_written = False

    @property
    def journal_path(self) -> str:
        return self._journal_path

    def start(self) -> str:
        """Create the journal file and mark as running.  Returns the path."""
        Path(self._journal_path).unlink(missing_ok=True)
        self._file = open(self._journal_path, "a")
        self._running = True
        logger.info("Journal writer started: %s", self._journal_path)
        return self._journal_path

    def stop(self) -> None:
        """Write a shutdown entry (if not already written), close the file."""
        if not self._running:
            return
        self._running = False
        if not self._shutdown_written:
            self._shutdown_written = True
            self._write_entry({"type": "shutdown"})
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
        logger.info("Journal writer stopped")

    # ── Public write methods ──

    def write_frames(
        self,
        count: int,
        buttons: list[str] | None = None,
        touch_x: int | None = None,
        touch_y: int | None = None,
    ) -> None:
        self._write_entry({
            "type": "frames",
            "count": count,
            "buttons": buttons,
            "touch_x": touch_x,
            "touch_y": touch_y,
        })

    def write_load_state(self, path: str) -> None:
        self._write_entry({"type": "load_state", "path": path})

    def write_reset(self) -> None:
        self._write_entry({"type": "reset"})

    def write_load_rom(self, rom_path: str) -> None:
        self._write_entry({"type": "load_rom", "rom_path": rom_path})

    def write_sync(self, state_path: str) -> None:
        self._write_entry({"type": "sync", "state_path": state_path})

    def write_commentary(
        self, stream_time: float, text: str, style: str = "normal"
    ) -> None:
        self._write_entry({
            "type": "commentary",
            "stream_time": stream_time,
            "text": text,
            "style": style,
        })

    def write_shutdown(self) -> None:
        if not self._shutdown_written:
            self._shutdown_written = True
            self._write_entry({"type": "shutdown"})

    # ── Internal ──

    def _write_entry(self, entry: dict) -> None:
        """Append a JSON line to the journal file.  Thread-safe and flushed."""
        with self._lock:
            if self._file is None:
                return
            self._file.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._file.flush()


class JournalReader:
    """Reads journal entries by tail-following a JSONL file.

    Used by the rendering process to consume entries written by the main
    emulator.  Iterable — yields parsed JSON dicts.  Blocks between entries.

    Stops when it sees a ``shutdown`` entry, or when the server PID is no
    longer alive and no new data arrives for 1 second.
    """

    def __init__(self, journal_path: str, server_pid: int | None = None) -> None:
        self._journal_path = journal_path
        self._server_pid = server_pid
        self._file = None
        self._shutdown_seen = False

    def connect(self) -> None:
        """Open the journal file for reading.

        Retries briefly if the file doesn't exist yet (the writer may not
        have created it by the time we try to open).
        """
        for attempt in range(20):
            try:
                self._file = open(self._journal_path, "r")
                logger.info("Journal reader opened %s", self._journal_path)
                return
            except FileNotFoundError:
                if attempt >= 19:
                    raise
                time.sleep(0.25)

    def close(self) -> None:
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def cleanup(self) -> None:
        """Remove the journal file from disk."""
        self.close()
        try:
            Path(self._journal_path).unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("Journal file cleaned up: %s", self._journal_path)

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        """Read and return the next journal entry.  Blocks until available.

        Uses a tail-follow pattern: reads lines from the file, and when at
        EOF polls every 50 ms for new data.  Stops on shutdown entry or
        server death + sustained EOF.
        """
        eof_since: float | None = None

        while True:
            pos = self._file.tell()
            raw = self._file.readline()

            if raw.endswith("\n"):
                # Complete line — parse and return
                eof_since = None
                line = raw.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("type") == "shutdown":
                    self._shutdown_seen = True
                return entry

            # No complete line (empty string = EOF, or partial write)
            if raw:
                # Partial line — seek back so we re-read it next time
                self._file.seek(pos)

            if self._shutdown_seen:
                raise StopIteration

            # Track how long we've been at EOF
            now = time.monotonic()
            if eof_since is None:
                eof_since = now

            # If at EOF for >1s, check server liveness
            if now - eof_since > 1.0 and self._server_pid is not None:
                if not self._is_pid_alive():
                    raise StopIteration

            time.sleep(0.05)

    def _is_pid_alive(self) -> bool:
        """Check if the server PID is still running."""
        try:
            os.kill(self._server_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we can't signal it — still alive
            return True

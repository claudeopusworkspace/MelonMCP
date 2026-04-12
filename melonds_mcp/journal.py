"""Input journal for the rendering emulator — one-way stream of replay events.

The main emulator writes journal entries as it processes MCP commands.
A separate rendering process reads them and replays at real-time 60fps
for the HLS video stream.

Protocol: line-delimited JSON over a Unix domain socket.

Entry types:
  {"type":"frames","count":N,"buttons":[...],"touch_x":X,"touch_y":Y}
  {"type":"load_state","path":"/abs/path/to/state.dst"}
  {"type":"reset"}
  {"type":"load_rom","rom_path":"/abs/path/to/rom.nds"}
  {"type":"shutdown"}
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Queue capacity — ~33 seconds of single-frame entries at 60fps.
_QUEUE_MAXSIZE = 2000


class JournalWriter:
    """Writes journal entries to a Unix domain socket for the renderer.

    Runs a background thread that accepts one connection from the renderer
    and drains entries from an internal queue to the socket.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._thread: threading.Thread | None = None
        self._server_sock: socket.socket | None = None
        self._running = False
        self._connected = False
        self._drop_count = 0

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> str:
        """Bind the socket and start the writer thread. Returns socket path."""
        # Clean up stale socket
        Path(self._socket_path).unlink(missing_ok=True)

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(self._socket_path)
        self._server_sock.listen(1)
        self._server_sock.settimeout(1.0)
        self._running = True

        self._thread = threading.Thread(
            target=self._writer_loop, name="journal-writer", daemon=True
        )
        self._thread.start()
        logger.info("Journal writer started on %s", self._socket_path)
        return self._socket_path

    def stop(self) -> None:
        """Send shutdown and clean up."""
        if not self._running:
            return
        self._running = False
        # Enqueue shutdown sentinel — don't block if queue is full, just force it
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Clear some space and retry
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        Path(self._socket_path).unlink(missing_ok=True)
        logger.info("Journal writer stopped")

    # ── Public write methods ──

    def write_frames(
        self,
        count: int,
        buttons: list[str] | None = None,
        touch_x: int | None = None,
        touch_y: int | None = None,
    ) -> None:
        """Write a frames entry. Non-blocking — drops if queue is full."""
        entry = json.dumps({
            "type": "frames",
            "count": count,
            "buttons": buttons,
            "touch_x": touch_x,
            "touch_y": touch_y,
        })
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            self._drop_count += 1
            if self._drop_count == 1 or self._drop_count % 300 == 0:
                logger.warning(
                    "Journal queue full — dropped frame entry (total drops: %d)",
                    self._drop_count,
                )

    def write_load_state(self, path: str) -> None:
        """Write a load_state entry. Blocks until queued (never dropped)."""
        entry = json.dumps({"type": "load_state", "path": path})
        self._queue.put(entry)  # blocking

    def write_reset(self) -> None:
        """Write a reset entry. Blocks until queued (never dropped)."""
        entry = json.dumps({"type": "reset"})
        self._queue.put(entry)

    def write_load_rom(self, rom_path: str) -> None:
        """Write a load_rom entry. Blocks until queued (never dropped)."""
        entry = json.dumps({"type": "load_rom", "rom_path": rom_path})
        self._queue.put(entry)

    def write_sync(self, state_path: str) -> None:
        """Write a sync entry. Blocks until queued (never dropped).

        The renderer loads this savestate to jump ahead when it falls
        too far behind the main emulator.
        """
        entry = json.dumps({"type": "sync", "state_path": state_path})
        self._queue.put(entry)  # blocking

    def write_shutdown(self) -> None:
        """Write a shutdown entry. Blocks until queued (never dropped)."""
        entry = json.dumps({"type": "shutdown"})
        self._queue.put(entry)

    # ── Background thread ──

    def _writer_loop(self) -> None:
        """Accept one connection, then drain queue to socket until shutdown."""
        conn: socket.socket | None = None
        try:
            conn = self._accept_connection()
            if conn is None:
                return
            self._connected = True
            logger.info("Journal renderer connected")
            self._drain_to_socket(conn)
        except Exception:
            logger.warning("Journal writer thread error", exc_info=True)
        finally:
            self._connected = False
            if conn:
                try:
                    conn.close()
                except OSError:
                    pass

    def _accept_connection(self) -> socket.socket | None:
        """Wait for a renderer to connect. Returns None if shutting down."""
        while self._running:
            try:
                conn, _ = self._server_sock.accept()
                conn.settimeout(5.0)
                return conn
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    logger.warning("Journal accept error", exc_info=True)
                return None
        return None

    def _drain_to_socket(self, conn: socket.socket) -> None:
        """Read entries from queue and write to socket until shutdown."""
        while self._running or not self._queue.empty():
            try:
                entry = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if entry is None:
                # Shutdown sentinel — send shutdown entry then exit
                try:
                    conn.sendall(b'{"type":"shutdown"}\n')
                except OSError:
                    pass
                return

            try:
                conn.sendall(entry.encode("utf-8") + b"\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                logger.warning("Journal socket broken — renderer disconnected")
                self._connected = False
                return


class JournalReader:
    """Reads journal entries from a Unix domain socket.

    Used by the rendering process to consume entries from the main emulator.
    Iterable — yields parsed JSON dicts. Blocks on recv.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        self._buf = b""

    def connect(self) -> None:
        """Connect to the journal socket."""
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self._socket_path)
        logger.info("Journal reader connected to %s", self._socket_path)

    def close(self) -> None:
        """Close the socket connection."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        """Read and return the next journal entry. Blocks until available."""
        while True:
            # Check if we have a complete line in the buffer
            newline_pos = self._buf.find(b"\n")
            if newline_pos >= 0:
                line = self._buf[:newline_pos]
                self._buf = self._buf[newline_pos + 1:]
                if line:
                    return json.loads(line.decode("utf-8"))
                continue  # empty line, skip

            # Need more data
            if self._sock is None:
                raise StopIteration
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                raise StopIteration
            if not chunk:
                raise StopIteration
            self._buf += chunk

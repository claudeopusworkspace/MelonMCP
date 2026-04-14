"""Lightweight IPC bridge — exposes the running EmulatorState to external scripts.

Runs a Unix domain socket server in a background thread. Scripts connect and
send line-delimited JSON requests, receiving JSON responses. This lets custom
scripts call advance_frames(), read_memory(), etc. on the *same* emulator the
MCP server is driving, with no savestate handoff overhead.

Protocol (line-delimited JSON over Unix domain socket):
  Request:  {"method": "advance_frames", "params": {"count": 16, "buttons": ["right"]}}\n
  Response: {"result": {"frames_advanced": 16, "total_frame": 1234}}\n
  Error:    {"error": "No ROM loaded."}\n
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .constants import buttons_to_bitmask

logger = logging.getLogger(__name__)

# Maximum request size (64 KB should be plenty for any single call)
MAX_REQUEST_SIZE = 65536

# Bridge methods that advance emulation frames and should wait for the
# renderer to catch up in live pacing mode (mirrors _CATCHUP_TOOLS in server.py).
_PACING_METHODS = {
    "advance_frames", "advance_frames_until", "advance_frame",
    "press_buttons", "tap_touch_screen", "cycle",
}


class BridgeServer:
    """Unix socket server that dispatches JSON-RPC-like calls to EmulatorState."""

    def __init__(self, holder, socket_path: str) -> None:
        from .emulator import EmulatorState

        self._holder: EmulatorState = holder
        self._socket_path = socket_path
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._methods: dict[str, Callable[..., Any]] = self._build_dispatch()

    def _build_dispatch(self) -> dict[str, Callable[..., Any]]:
        """Map method names to handler functions."""
        return {
            "advance_frames": self._advance_frames,
            "advance_frames_until": self._advance_frames_until,
            "advance_frame": self._advance_frame,
            "press_buttons": self._press_buttons,
            "tap_touch_screen": self._tap_touch_screen,
            "get_screenshot": self._get_screenshot,
            "read_memory": self._read_memory,
            "read_memory_range": self._read_memory_range,
            "read_memory_block": self._read_memory_block,
            "write_memory": self._write_memory,
            "input_keypad_update": self._input_keypad_update,
            "cycle": self._cycle,
            "save_state": self._save_state,
            "load_state": self._load_state,
            "get_status": self._get_status,
            "get_frame_count": self._get_frame_count,
            "create_checkpoint": self._create_checkpoint,
            "list_checkpoints": self._list_checkpoints,
            "revert_to_checkpoint": self._revert_to_checkpoint,
            "save_checkpoint": self._save_checkpoint,
            "set_stream_config": self._set_stream_config,
            "start_video_stream": self._start_video_stream,
            "stop_video_stream": self._stop_video_stream,
        }

    # -- Journal helper --

    def _journal_write(self, method: str, **kwargs) -> None:
        """Write a journal entry if the journal is active.

        The journal is a durable file — writes are safe regardless of
        whether the renderer is currently running.
        """
        j = getattr(self._holder, "_journal", None)
        if j is None:
            return
        getattr(j, method)(**kwargs)

    # -- Method handlers --

    def _advance_frames(self, count: int = 1, buttons: list[str] | None = None,
                        touch_x: int | None = None, touch_y: int | None = None) -> dict:
        advanced = self._holder.advance_frames(count, buttons, touch_x, touch_y)
        self._journal_write("write_frames", count=count, buttons=buttons,
                            touch_x=touch_x, touch_y=touch_y)
        return {"frames_advanced": advanced, "total_frame": self._holder.frame_count}

    def _advance_frames_until(
        self,
        max_frames: int,
        conditions: list[dict],
        poll_interval: int = 1,
        buttons: list[str] | None = None,
        touch_x: int | None = None,
        touch_y: int | None = None,
        read_addresses: list[dict] | None = None,
    ) -> dict:
        result = self._holder.advance_frames_until(
            max_frames, conditions, poll_interval, buttons, touch_x, touch_y, read_addresses,
        )
        self._journal_write("write_frames", count=result["frames_elapsed"],
                            buttons=buttons, touch_x=touch_x, touch_y=touch_y)
        return result

    def _advance_frame(self, buttons: list[str] | None = None,
                       touch_x: int | None = None, touch_y: int | None = None) -> dict:
        self._holder.advance_frame(buttons, touch_x, touch_y)
        self._journal_write("write_frames", count=1, buttons=buttons,
                            touch_x=touch_x, touch_y=touch_y)
        return {"total_frame": self._holder.frame_count}

    def _press_buttons(self, buttons: list[str], frames: int = 1) -> dict:
        self._holder.press_buttons(buttons, frames)
        self._journal_write("write_frames", count=frames, buttons=buttons,
                            touch_x=None, touch_y=None)
        self._journal_write("write_frames", count=1, buttons=None,
                            touch_x=None, touch_y=None)  # release
        return {"total_frame": self._holder.frame_count}

    def _tap_touch_screen(self, x: int, y: int, frames: int = 1) -> dict:
        self._holder.tap_touch_screen(x, y, frames)
        self._journal_write("write_frames", count=frames, buttons=None,
                            touch_x=x, touch_y=y)
        self._journal_write("write_frames", count=1, buttons=None,
                            touch_x=None, touch_y=None)  # release
        return {"total_frame": self._holder.frame_count}

    def _get_screenshot(self, screen: str = "both", fmt: str = "png") -> dict:
        import base64
        mime, data = self._holder.capture_screenshot(screen, fmt)
        return {"mime": mime, "data_b64": base64.b64encode(data).decode("ascii"), "size": len(data)}

    def _read_memory(self, address: int, size: str = "byte", signed: bool = False) -> dict:
        emu = self._holder._require_rom()
        fns = {
            ("byte", False): emu.memory_read_byte,
            ("byte", True): emu.memory_read_byte_signed,
            ("short", False): emu.memory_read_short,
            ("short", True): emu.memory_read_short_signed,
            ("long", False): emu.memory_read_long,
            ("long", True): emu.memory_read_long_signed,
        }
        fn = fns.get((size, signed))
        if fn is None:
            raise ValueError(f"Invalid size: {size}")
        return {"value": fn(address)}

    def _read_memory_range(self, address: int, size: str = "byte",
                           count: int = 1, signed: bool = False) -> dict:
        emu = self._holder._require_rom()
        fns = {
            ("byte", False): emu.memory_read_byte,
            ("byte", True): emu.memory_read_byte_signed,
            ("short", False): emu.memory_read_short,
            ("short", True): emu.memory_read_short_signed,
            ("long", False): emu.memory_read_long,
            ("long", True): emu.memory_read_long_signed,
        }
        fn = fns.get((size, signed))
        if fn is None:
            raise ValueError(f"Invalid size: {size}")
        step = {"byte": 1, "short": 2, "long": 4}[size]
        values = [fn(address + i * step) for i in range(count)]
        return {"values": values}

    def _read_memory_block(self, address: int, size: int) -> dict:
        """Read a contiguous block of memory as base64 (single FFI call)."""
        import base64
        emu = self._holder._require_rom()
        data = emu.memory_read_block(address, size)
        return {"data_b64": base64.b64encode(data).decode("ascii"), "size": len(data)}

    def _write_memory(self, address: int, value: int, size: str = "byte") -> dict:
        emu = self._holder._require_rom()
        if size == "byte":
            emu.memory_write_byte(address, value)
        elif size == "short":
            emu.memory_write_short(address, value)
        elif size == "long":
            emu.memory_write_long(address, value)
        else:
            raise ValueError(f"Invalid size: {size}")
        logger.debug("write_memory addr=0x%08X value=%d size=%s", address, value, size)
        return {"success": True}

    def _input_keypad_update(self, keys: int = 0, buttons: list[str] | None = None) -> dict:
        emu = self._holder._require_rom()
        if buttons:
            keys = buttons_to_bitmask(buttons)
        emu.input_keypad_update(keys)
        return {"keys": keys}

    def _cycle(self) -> dict:
        emu = self._holder._require_rom()
        emu.cycle()
        self._holder.frame_count += 1
        self._journal_write("write_frames", count=1, buttons=None,
                            touch_x=None, touch_y=None)
        return {"total_frame": self._holder.frame_count}

    def _save_state(self, path: str) -> dict:
        emu = self._holder._require_rom()
        success = emu.savestate_save(path)
        logger.info("save_state path=%s success=%s", path, success)
        return {"success": success, "path": path}

    def _load_state(self, path: str) -> dict:
        emu = self._holder._require_rom()
        success = emu.savestate_load(path)
        if success:
            self._journal_write("write_load_state", path=path)
        logger.info("load_state path=%s success=%s", path, success)
        return {"success": success, "path": path}

    def _get_status(self) -> dict:
        return {
            "initialized": self._holder.is_initialized,
            "rom_loaded": self._holder.is_rom_loaded,
            "frame_count": self._holder.frame_count,
            "rom_path": self._holder.rom_path,
        }

    def _get_frame_count(self) -> dict:
        return {"frame_count": self._holder.frame_count}

    def _create_checkpoint(self, action: str = "manual") -> dict:
        emu = self._holder._require_rom()
        cp = self._holder.checkpoints.create(emu, self._holder.frame_count, action)
        return {"checkpoint_id": cp.id, "frame": cp.frame, "action": cp.action}

    def _list_checkpoints(self, limit: int = 20) -> dict:
        from datetime import datetime

        checkpoints = self._holder.checkpoints.list_recent(limit)
        return {
            "total_checkpoints": self._holder.checkpoints.total_count,
            "showing": len(checkpoints),
            "checkpoints": [
                {
                    "id": cp.id,
                    "frame": cp.frame,
                    "action": cp.action,
                    "time": datetime.fromtimestamp(cp.timestamp).strftime("%H:%M:%S"),
                }
                for cp in checkpoints
            ],
        }

    def _revert_to_checkpoint(self, checkpoint_id: str) -> dict:
        before_count = self._holder.checkpoints.total_count
        cp = self._holder.checkpoints.revert(self._holder, checkpoint_id)
        self._journal_write("write_load_state", path=cp.path)
        discarded = before_count - self._holder.checkpoints.total_count
        return {
            "reverted_to": {"id": cp.id, "frame": cp.frame, "action": cp.action},
            "total_frame": self._holder.frame_count,
            "remaining_checkpoints": self._holder.checkpoints.total_count,
            "discarded_checkpoints": discarded,
        }

    def _save_checkpoint(self, checkpoint_id: str, name: str) -> dict:
        dest_path = str(self._holder.savestates_dir / f"{name}.dst")
        cp = self._holder.checkpoints.promote(checkpoint_id, dest_path)
        return {
            "name": name,
            "path": dest_path,
            "source_checkpoint": {
                "id": cp.id,
                "frame": cp.frame,
                "action": cp.action,
            },
        }

    def _set_stream_config(self, enabled: bool | None = None) -> dict:
        from .settings import get_stream, set_stream_override
        set_stream_override(enabled)
        return {"override": enabled, "effective": get_stream()}

    def _start_video_stream(self, name: str = "unnamed", port: int = 18091) -> dict:
        from .server import _tool_start_video_stream
        return _tool_start_video_stream(self._holder, port=port, name=name)

    def _stop_video_stream(self) -> dict:
        from .server import _tool_stop_video_stream
        return _tool_stop_video_stream(self._holder)

    # -- Server lifecycle --

    def start(self) -> str:
        """Start the bridge server. Returns the socket path."""
        # Clean up stale socket
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(self._socket_path)
        self._server_sock.listen(5)
        self._server_sock.settimeout(1.0)  # Allow periodic shutdown checks
        self._running = True

        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()
        logger.info("Bridge server started on %s", self._socket_path)
        return self._socket_path

    def stop(self) -> None:
        """Stop the bridge server."""
        logger.info("Bridge server stopping")
        self._running = False
        if self._server_sock:
            self._server_sock.close()
        if self._thread:
            self._thread.join(timeout=3)
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        logger.info("Bridge server stopped")

    def _serve_loop(self) -> None:
        """Accept connections and handle requests."""
        while self._running:
            try:
                conn, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            peer = f"client-{id(conn):x}"
            logger.info("Bridge client connected (%s)", peer)
            try:
                self._handle_connection(conn, peer)
            except Exception:
                logger.warning("Unhandled error in bridge connection (%s)", peer, exc_info=True)
            finally:
                conn.close()
                logger.info("Bridge client disconnected (%s)", peer)

    def _handle_connection(self, conn: socket.socket, peer: str) -> None:
        """Handle a single client connection (may send multiple requests)."""
        buf = b""
        conn.settimeout(30.0)
        while True:
            try:
                chunk = conn.recv(MAX_REQUEST_SIZE)
            except socket.timeout:
                logger.debug("Bridge recv timeout (%s), closing connection", peer)
                break
            if not chunk:
                break
            buf += chunk
            # Process complete lines
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                response = self._dispatch(line, peer)
                conn.sendall(response.encode("utf-8") + b"\n")

    def _dispatch(self, raw: bytes, peer: str) -> str:
        """Parse a JSON request and dispatch to the appropriate handler."""
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Bridge invalid JSON from %s: %s", peer, e)
            return json.dumps({"error": f"Invalid JSON: {e}"})

        method = req.get("method")
        if not method or method not in self._methods:
            logger.warning("Bridge unknown method %r from %s", method, peer)
            return json.dumps({"error": f"Unknown method: {method!r}. Available: {sorted(self._methods.keys())}"})

        params = req.get("params", {})
        logger.debug("Bridge dispatch %s(%s) from %s", method, _summarize_params(params), peer)

        t_lock_start = time.monotonic()
        with self._holder.lock:
            t_lock_acquired = time.monotonic()
            lock_wait = t_lock_acquired - t_lock_start
            if lock_wait > 0.1:
                logger.warning(
                    "Bridge lock contention: waited %.3fs for lock (method=%s, peer=%s)",
                    lock_wait, method, peer,
                )
            try:
                result = self._methods[method](**params)
                elapsed = time.monotonic() - t_lock_acquired
                if elapsed > 1.0:
                    logger.info(
                        "Bridge slow dispatch: %s took %.3fs (peer=%s)",
                        method, elapsed, peer,
                    )
                else:
                    logger.debug("Bridge dispatch %s completed in %.3fs", method, elapsed)
            except Exception as e:
                logger.warning(
                    "Bridge dispatch error: method=%s error=%s (peer=%s)",
                    method, e, peer, exc_info=True,
                )
                return json.dumps({"error": f"{type(e).__name__}: {e}"})

        # Stream catchup runs AFTER the lock is released so other tools
        # aren't blocked while we wait for the renderer.
        if method in _PACING_METHODS:
            from .server import _wait_for_stream_catchup
            _wait_for_stream_catchup(self._holder)

        return json.dumps({"result": result})


def _summarize_params(params: dict) -> str:
    """Create a short summary of params for logging (avoid dumping huge data)."""
    parts = []
    for k, v in params.items():
        if isinstance(v, str) and len(v) > 80:
            parts.append(f"{k}=<str len={len(v)}>")
        elif isinstance(v, (list, dict)) and len(str(v)) > 80:
            parts.append(f"{k}=<{type(v).__name__} len={len(v)}>")
        else:
            parts.append(f"{k}={v!r}")
    return ", ".join(parts)

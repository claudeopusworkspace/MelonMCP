"""Client for connecting to the running melonDS MCP bridge from external scripts.

Usage:
    from melonds_mcp.client import connect

    emu = connect()  # or connect("/path/to/.melonds_bridge.sock")
    emu.advance_frames(16, buttons=["right"])
    pos = emu.read_memory(0x0227F450, size="long")
    print(pos)  # {"value": 42}
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any


class EmulatorClient:
    """Client that talks to the BridgeServer over a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._sock: socket.socket | None = None

    def _ensure_connected(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self._socket_path)
        return self._sock

    def _call(self, method: str, **params: Any) -> Any:
        """Send a request and return the result (or raise on error)."""
        sock = self._ensure_connected()
        req = json.dumps({"method": method, "params": params})
        sock.sendall(req.encode("utf-8") + b"\n")

        # Read response (line-delimited)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                self._sock = None
                raise ConnectionError("Bridge connection closed.")
            buf += chunk

        line = buf.split(b"\n", 1)[0]
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(f"Bridge error: {resp['error']}")
        return resp["result"]

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # -- Convenience methods --

    def advance_frames(self, count: int = 1, buttons: list[str] | None = None,
                       touch_x: int | None = None, touch_y: int | None = None) -> dict:
        """Advance N frames holding the given inputs."""
        params: dict[str, Any] = {"count": count}
        if buttons:
            params["buttons"] = buttons
        if touch_x is not None:
            params["touch_x"] = touch_x
        if touch_y is not None:
            params["touch_y"] = touch_y
        return self._call("advance_frames", **params)

    def advance_frames_until(
        self,
        max_frames: int,
        conditions: list[dict],
        poll_interval: int = 1,
        buttons: list[str] | None = None,
        touch_x: int | None = None,
        touch_y: int | None = None,
        read_addresses: list[dict] | None = None,
    ) -> dict:
        """Advance up to max_frames, returning early when a memory condition is met.

        Runs the poll loop internally at full emulator speed, eliminating
        round-trip overhead. Returns dict with triggered, condition_index,
        frames_elapsed, total_frame, and optional reads.
        """
        params: dict[str, Any] = {
            "max_frames": max_frames,
            "conditions": conditions,
            "poll_interval": poll_interval,
        }
        if buttons:
            params["buttons"] = buttons
        if touch_x is not None:
            params["touch_x"] = touch_x
        if touch_y is not None:
            params["touch_y"] = touch_y
        if read_addresses:
            params["read_addresses"] = read_addresses
        return self._call("advance_frames_until", **params)

    def advance_frame(self, buttons: list[str] | None = None,
                      touch_x: int | None = None, touch_y: int | None = None) -> dict:
        """Advance one frame."""
        params: dict[str, Any] = {}
        if buttons:
            params["buttons"] = buttons
        if touch_x is not None:
            params["touch_x"] = touch_x
        if touch_y is not None:
            params["touch_y"] = touch_y
        return self._call("advance_frame", **params)

    def press_buttons(self, buttons: list[str], frames: int = 1) -> dict:
        """Press and release buttons (hold for N frames, release for 1)."""
        return self._call("press_buttons", buttons=buttons, frames=frames)

    def tap_touch_screen(self, x: int, y: int, frames: int = 1) -> dict:
        """Tap the touchscreen."""
        return self._call("tap_touch_screen", x=x, y=y, frames=frames)

    def read_memory(self, address: int, size: str = "byte", signed: bool = False) -> int:
        """Read a single value from memory. Returns the value directly."""
        result = self._call("read_memory", address=address, size=size, signed=signed)
        return result["value"]

    def read_memory_range(self, address: int, size: str = "byte",
                          count: int = 1, signed: bool = False) -> list[int]:
        """Read multiple consecutive values. Returns list of values."""
        result = self._call("read_memory_range", address=address, size=size,
                            count=count, signed=signed)
        return result["values"]

    def read_memory_block(self, address: int, size: int) -> bytes:
        """Read a contiguous block of memory as raw bytes (bulk, single FFI call).

        Uses base64 encoding on the wire for efficiency. Preferred over
        read_memory_range for large reads (>256 bytes).
        """
        import base64
        result = self._call("read_memory_block", address=address, size=size)
        return base64.b64decode(result["data_b64"])

    def write_memory(self, address: int, value: int, size: str = "byte") -> None:
        """Write a value to memory."""
        self._call("write_memory", address=address, value=value, size=size)

    def input_keypad_update(self, keys: int = 0, buttons: list[str] | None = None) -> None:
        """Set raw keypad state (for fine-grained frame control with cycle())."""
        params: dict[str, Any] = {"keys": keys}
        if buttons:
            params["buttons"] = buttons
        self._call("input_keypad_update", **params)

    def cycle(self) -> int:
        """Advance exactly one frame (no input changes). Returns frame count."""
        result = self._call("cycle")
        return result["total_frame"]

    def save_state(self, path: str) -> bool:
        """Save emulator state to a file."""
        result = self._call("save_state", path=path)
        return result["success"]

    def load_state(self, path: str) -> bool:
        """Load emulator state from a file."""
        result = self._call("load_state", path=path)
        return result["success"]

    def get_status(self) -> dict:
        """Get emulator status."""
        return self._call("get_status")

    def get_frame_count(self) -> int:
        """Get current frame count."""
        return self._call("get_frame_count")["frame_count"]

    def create_checkpoint(self, action: str = "manual") -> dict:
        """Create a checkpoint at the current emulator state. Returns checkpoint info."""
        return self._call("create_checkpoint", action=action)

    def list_checkpoints(self, limit: int = 20) -> dict:
        """List recent checkpoints. Returns dict with total count and checkpoint list."""
        return self._call("list_checkpoints", limit=limit)

    def revert_to_checkpoint(self, checkpoint_id: str) -> dict:
        """Revert to a checkpoint by hash ID, discarding all later checkpoints."""
        return self._call("revert_to_checkpoint", checkpoint_id=checkpoint_id)

    def save_checkpoint(self, checkpoint_id: str, name: str) -> dict:
        """Save a checkpoint as a permanent named savestate without loading it."""
        return self._call("save_checkpoint", checkpoint_id=checkpoint_id, name=name)

    def set_stream_config(self, enabled: bool | None = None) -> dict:
        """Override the stream setting for the life of the server process.

        enabled=True/False forces streaming on/off; enabled=None clears the
        override so the env-var + settings.json chain takes over again.
        Returns {"override": <bool|None>, "effective": <bool>}.
        """
        return self._call("set_stream_config", enabled=enabled)

    def get_screenshot(self, screen: str = "both", fmt: str = "png") -> tuple[str, bytes]:
        """Capture screenshot. Returns (mime_type, image_bytes)."""
        import base64
        result = self._call("get_screenshot", screen=screen, fmt=fmt)
        return result["mime"], base64.b64decode(result["data_b64"])


def connect(socket_path: str | None = None) -> EmulatorClient:
    """Connect to the running melonDS MCP bridge.

    Args:
        socket_path: Path to the Unix socket. If not provided, searches for
                     .melonds_bridge.sock in common locations (CWD, parent dirs).

    Returns:
        Connected EmulatorClient instance.
    """
    if socket_path:
        return EmulatorClient(socket_path)

    # Search for the socket file
    search = [
        Path.cwd() / ".melonds_bridge.sock",
        Path.cwd() / "MelonMCP" / ".melonds_bridge.sock",
    ]
    # Also check MELONDS_BRIDGE_SOCK env var
    env_path = os.environ.get("MELONDS_BRIDGE_SOCK")
    if env_path:
        search.insert(0, Path(env_path))

    for candidate in search:
        if candidate.exists():
            return EmulatorClient(str(candidate))

    paths_tried = [str(p) for p in search]
    raise FileNotFoundError(
        f"Bridge socket not found. Searched: {paths_tried}. "
        "Is the MCP server running? Set MELONDS_BRIDGE_SOCK or pass the path explicitly."
    )

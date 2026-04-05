"""Thin ctypes wrapper around libmelonds.so — 1:1 mapping of the C shim interface."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path


def _find_library() -> str:
    """Search for libmelonds.so in known locations."""
    env_path = os.environ.get("MELONDS_LIB_PATH")
    if env_path and os.path.exists(env_path):
        return os.path.realpath(env_path)

    project_root = Path(__file__).parent.parent
    candidates = [
        project_root / "build" / "libmelonds.so",
    ]
    for path in candidates:
        resolved = path.resolve()
        if resolved.exists():
            return str(resolved)

    raise FileNotFoundError(
        "libmelonds.so not found. Build it first with: ./scripts/build_libmelonds.sh"
    )


class MelonDS:
    """Python wrapper around the melonDS C shim library.

    This is a thin, low-level wrapper. Each method maps directly to a
    C function in melonds_shim.cpp. Higher-level logic belongs in emulator.py.
    """

    # Pre-allocated screenshot buffer (256x384 RGB = 294912 bytes)
    _SCREENSHOT_SIZE = 98304 * 3

    # Pre-allocated audio read buffer (~800 samples/frame * 60 frames = ~1 sec)
    _AUDIO_READ_FRAMES = 48000
    _AUDIO_BUF_SIZE = _AUDIO_READ_FRAMES * 2  # stereo s16

    def __init__(self) -> None:
        lib_path = _find_library()
        self._lib = ctypes.CDLL(lib_path)
        self._setup_signatures()
        self._screenshot_buf = (ctypes.c_char * self._SCREENSHOT_SIZE)()
        self._audio_buf = (ctypes.c_short * self._AUDIO_BUF_SIZE)()

    def _setup_signatures(self) -> None:
        lib = self._lib

        # ── Lifecycle ──
        lib.melonds_init.argtypes = []
        lib.melonds_init.restype = ctypes.c_int

        lib.melonds_free.argtypes = []
        lib.melonds_free.restype = None

        lib.melonds_open.argtypes = [ctypes.c_char_p]
        lib.melonds_open.restype = ctypes.c_int

        lib.melonds_pause.argtypes = []
        lib.melonds_pause.restype = None

        lib.melonds_resume.argtypes = []
        lib.melonds_resume.restype = None

        lib.melonds_reset.argtypes = []
        lib.melonds_reset.restype = None

        lib.melonds_running.argtypes = []
        lib.melonds_running.restype = ctypes.c_int

        lib.melonds_cycle.argtypes = []
        lib.melonds_cycle.restype = None

        # ── Audio ──
        lib.melonds_audio_enable.argtypes = []
        lib.melonds_audio_enable.restype = None

        lib.melonds_audio_disable.argtypes = []
        lib.melonds_audio_disable.restype = None

        lib.melonds_audio_samples_available.argtypes = []
        lib.melonds_audio_samples_available.restype = ctypes.c_uint

        lib.melonds_audio_read.argtypes = [
            ctypes.POINTER(ctypes.c_short),
            ctypes.c_uint,
        ]
        lib.melonds_audio_read.restype = ctypes.c_uint

        # ── Display ──
        lib.melonds_screenshot.argtypes = [ctypes.c_char_p]
        lib.melonds_screenshot.restype = None

        # ── Input ──
        lib.melonds_input_keypad_update.argtypes = [ctypes.c_ushort]
        lib.melonds_input_keypad_update.restype = None

        lib.melonds_input_keypad_get.argtypes = []
        lib.melonds_input_keypad_get.restype = ctypes.c_ushort

        lib.melonds_input_set_touch_pos.argtypes = [
            ctypes.c_ushort,
            ctypes.c_ushort,
        ]
        lib.melonds_input_set_touch_pos.restype = None

        lib.melonds_input_release_touch.argtypes = []
        lib.melonds_input_release_touch.restype = None

        # ── Savestates ──
        lib.melonds_savestate_save.argtypes = [ctypes.c_char_p]
        lib.melonds_savestate_save.restype = ctypes.c_int

        lib.melonds_savestate_load.argtypes = [ctypes.c_char_p]
        lib.melonds_savestate_load.restype = ctypes.c_int

        lib.melonds_savestate_slot_save.argtypes = [ctypes.c_int]
        lib.melonds_savestate_slot_save.restype = None

        lib.melonds_savestate_slot_load.argtypes = [ctypes.c_int]
        lib.melonds_savestate_slot_load.restype = None

        lib.melonds_savestate_slot_exists.argtypes = [ctypes.c_int]
        lib.melonds_savestate_slot_exists.restype = ctypes.c_int

        # ── Memory ──
        lib.melonds_memory_read_byte.argtypes = [ctypes.c_int]
        lib.melonds_memory_read_byte.restype = ctypes.c_ubyte

        lib.melonds_memory_read_byte_signed.argtypes = [ctypes.c_int]
        lib.melonds_memory_read_byte_signed.restype = ctypes.c_byte

        lib.melonds_memory_read_short.argtypes = [ctypes.c_int]
        lib.melonds_memory_read_short.restype = ctypes.c_ushort

        lib.melonds_memory_read_short_signed.argtypes = [ctypes.c_int]
        lib.melonds_memory_read_short_signed.restype = ctypes.c_short

        # Use c_uint/c_int (always 32-bit) instead of c_ulong/c_long (64-bit on Linux)
        lib.melonds_memory_read_long.argtypes = [ctypes.c_int]
        lib.melonds_memory_read_long.restype = ctypes.c_uint

        lib.melonds_memory_read_long_signed.argtypes = [ctypes.c_int]
        lib.melonds_memory_read_long_signed.restype = ctypes.c_int

        lib.melonds_memory_write_byte.argtypes = [ctypes.c_int, ctypes.c_ubyte]
        lib.melonds_memory_write_byte.restype = None

        lib.melonds_memory_write_short.argtypes = [
            ctypes.c_int,
            ctypes.c_ushort,
        ]
        lib.melonds_memory_write_short.restype = None

        lib.melonds_memory_write_long.argtypes = [
            ctypes.c_int,
            ctypes.c_uint,
        ]
        lib.melonds_memory_write_long.restype = None

        # ── Backup (battery save) ──
        lib.melonds_backup_import.argtypes = [ctypes.c_char_p]
        lib.melonds_backup_import.restype = ctypes.c_int

        lib.melonds_backup_export.argtypes = [ctypes.c_char_p]
        lib.melonds_backup_export.restype = ctypes.c_int

        # ── JIT ──
        lib.melonds_jit_enabled.argtypes = []
        lib.melonds_jit_enabled.restype = ctypes.c_int

    # ── Lifecycle ──

    def init(self) -> int:
        return self._lib.melonds_init()

    def free(self) -> None:
        self._lib.melonds_free()

    def open(self, filename: str) -> int:
        """Load a ROM. Returns 1 on success, 0 on failure."""
        return self._lib.melonds_open(filename.encode("utf-8"))

    def pause(self) -> None:
        self._lib.melonds_pause()

    def resume(self) -> None:
        self._lib.melonds_resume()

    def reset(self) -> None:
        self._lib.melonds_reset()

    def running(self) -> bool:
        return bool(self._lib.melonds_running())

    def cycle(self) -> None:
        """Advance one frame of emulation."""
        self._lib.melonds_cycle()

    # ── Display ──

    def screenshot(self) -> bytes:
        """Capture both screens as raw RGB bytes (294912 bytes, 256x384)."""
        self._lib.melonds_screenshot(self._screenshot_buf)
        return bytes(self._screenshot_buf)

    # ── Input ──

    def input_keypad_update(self, keys: int) -> None:
        """Set the keypad state bitmask (1 = pressed) for the current/next frame."""
        self._lib.melonds_input_keypad_update(ctypes.c_ushort(keys))

    def input_keypad_get(self) -> int:
        """Get current keypad state bitmask (1 = pressed)."""
        return self._lib.melonds_input_keypad_get()

    def input_set_touch_pos(self, x: int, y: int) -> None:
        """Set touchscreen press position (bottom screen coords: 0-255, 0-191)."""
        self._lib.melonds_input_set_touch_pos(
            ctypes.c_ushort(x), ctypes.c_ushort(y)
        )

    def input_release_touch(self) -> None:
        self._lib.melonds_input_release_touch()

    # ── Savestates ──

    def savestate_save(self, filename: str) -> bool:
        return bool(self._lib.melonds_savestate_save(filename.encode("utf-8")))

    def savestate_load(self, filename: str) -> bool:
        return bool(self._lib.melonds_savestate_load(filename.encode("utf-8")))

    def savestate_slot_save(self, index: int) -> None:
        self._lib.melonds_savestate_slot_save(index)

    def savestate_slot_load(self, index: int) -> None:
        self._lib.melonds_savestate_slot_load(index)

    def savestate_slot_exists(self, index: int) -> bool:
        return bool(self._lib.melonds_savestate_slot_exists(index))

    # ── Memory ──

    def memory_read_byte(self, address: int) -> int:
        return self._lib.melonds_memory_read_byte(address)

    def memory_read_byte_signed(self, address: int) -> int:
        return self._lib.melonds_memory_read_byte_signed(address)

    def memory_read_short(self, address: int) -> int:
        return self._lib.melonds_memory_read_short(address)

    def memory_read_short_signed(self, address: int) -> int:
        return self._lib.melonds_memory_read_short_signed(address)

    def memory_read_long(self, address: int) -> int:
        return self._lib.melonds_memory_read_long(address)

    def memory_read_long_signed(self, address: int) -> int:
        return self._lib.melonds_memory_read_long_signed(address)

    def memory_write_byte(self, address: int, value: int) -> None:
        self._lib.melonds_memory_write_byte(address, ctypes.c_ubyte(value))

    def memory_write_short(self, address: int, value: int) -> None:
        self._lib.melonds_memory_write_short(address, ctypes.c_ushort(value))

    def memory_write_long(self, address: int, value: int) -> None:
        self._lib.melonds_memory_write_long(address, ctypes.c_uint(value))

    # ── Backup (battery save) ──

    def backup_import(self, filename: str) -> bool:
        return bool(
            self._lib.melonds_backup_import(filename.encode("utf-8"))
        )

    def backup_export(self, filename: str) -> bool:
        return bool(
            self._lib.melonds_backup_export(filename.encode("utf-8"))
        )

    # ── Audio ──

    def audio_enable(self) -> None:
        """Initialize the audio output buffer."""
        self._lib.melonds_audio_enable()

    def audio_disable(self) -> None:
        """Drain the audio output buffer."""
        self._lib.melonds_audio_disable()

    def audio_samples_available(self) -> int:
        """Return number of stereo sample frames available to read."""
        return self._lib.melonds_audio_samples_available()

    def audio_read(self, max_frames: int = 0) -> bytes:
        """Read available audio samples as raw s16le stereo PCM bytes.

        Args:
            max_frames: Max stereo frames to read (0 = all available, up to buffer size).

        Returns:
            Raw bytes of s16le stereo PCM data (4 bytes per frame: L16 R16).
        """
        if max_frames <= 0:
            max_frames = self._AUDIO_READ_FRAMES
        if max_frames > self._AUDIO_READ_FRAMES:
            max_frames = self._AUDIO_READ_FRAMES
        count = self._lib.melonds_audio_read(self._audio_buf, max_frames)
        if count == 0:
            return b""
        return bytes(ctypes.cast(
            self._audio_buf,
            ctypes.POINTER(ctypes.c_char * (count * 4)),
        ).contents)

    # ── JIT ──

    def jit_enabled(self) -> bool:
        """Return whether JIT recompilation is enabled."""
        return bool(self._lib.melonds_jit_enabled())

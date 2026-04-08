"""Stateful emulator holder — lifecycle management, input helpers, screenshot capture."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from .constants import (
    SCREENSHOT_RGB_SIZE,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    TOTAL_HEIGHT,
    TOTAL_WIDTH,
    buttons_to_bitmask,
)
from .libmelonds import MelonDS

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """A savestate checkpoint taken automatically before an input action."""

    id: str
    frame: int
    action: str
    timestamp: float
    path: str


class CheckpointManager:
    """Ring buffer of automatic savestate checkpoints (max 300)."""

    MAX_CHECKPOINTS = 300

    def __init__(self, checkpoints_dir: Path):
        self._dir = checkpoints_dir
        self._dir.mkdir(exist_ok=True)
        self._ring: deque[Checkpoint] = deque(maxlen=self.MAX_CHECKPOINTS)
        self._counter = 0

    def create(self, emu: MelonDS, frame_count: int, action: str) -> Checkpoint:
        """Save the current emulator state as a checkpoint before an action."""
        self._counter += 1
        raw = f"{time.time():.6f}:{frame_count}:{self._counter}"
        hash_id = hashlib.sha256(raw.encode()).hexdigest()[:8]

        # If at capacity, the deque will auto-drop the oldest — delete its file
        if len(self._ring) == self._ring.maxlen:
            oldest = self._ring[0]
            Path(oldest.path).unlink(missing_ok=True)

        path = str(self._dir / f"{hash_id}.mst")
        emu.savestate_save(path)

        cp = Checkpoint(
            id=hash_id,
            frame=frame_count,
            action=action,
            timestamp=time.time(),
            path=path,
        )
        self._ring.append(cp)
        return cp

    def list_recent(self, limit: int = 20) -> list[Checkpoint]:
        """Return recent checkpoints in chronological order (oldest first)."""
        items = list(self._ring)
        if 0 < limit < len(items):
            items = items[-limit:]
        return items

    @property
    def total_count(self) -> int:
        return len(self._ring)

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        """Find a checkpoint by its hash ID."""
        for cp in self._ring:
            if cp.id == checkpoint_id:
                return cp
        return None

    def revert(self, holder: EmulatorState, checkpoint_id: str) -> Checkpoint:
        """Load a checkpoint and discard all checkpoints after it."""
        cp = self.get(checkpoint_id)
        if cp is None:
            raise ValueError(f"Checkpoint not found: {checkpoint_id!r}")

        if not Path(cp.path).exists():
            raise FileNotFoundError(f"Checkpoint file missing: {cp.path}")

        emu = holder._require_rom()
        emu.savestate_load(cp.path)
        holder.frame_count = cp.frame

        # Remove all checkpoints after the reverted one
        items = list(self._ring)
        idx = next(i for i, item in enumerate(items) if item.id == checkpoint_id)
        for item in items[idx + 1 :]:
            Path(item.path).unlink(missing_ok=True)
        self._ring.clear()
        self._ring.extend(items[: idx + 1])

        holder._notify_frame_change()
        return cp

    def promote(self, checkpoint_id: str, dest_path: str) -> Checkpoint:
        """Copy a checkpoint's savestate to a permanent save state path.

        Does not modify the checkpoint ring or the current emulator state.
        """
        cp = self.get(checkpoint_id)
        if cp is None:
            raise ValueError(f"Checkpoint not found: {checkpoint_id!r}")

        if not Path(cp.path).exists():
            raise FileNotFoundError(f"Checkpoint file missing: {cp.path}")

        shutil.copy2(cp.path, dest_path)
        return cp

    def clear(self) -> int:
        """Delete all checkpoint files and reset the buffer. Returns count deleted."""
        count = len(self._ring)
        for cp in self._ring:
            Path(cp.path).unlink(missing_ok=True)
        self._ring.clear()
        self._counter = 0
        return count


@dataclass
class EmulatorState:
    """Singleton holder for melonDS instance and associated state."""

    emu: MelonDS | None = None
    rom_path: str | None = None
    is_initialized: bool = False
    is_rom_loaded: bool = False
    frame_count: int = 0
    data_dir: Path = field(default_factory=lambda: Path.cwd())
    lock: threading.Lock = field(default_factory=threading.Lock)
    _frame_callbacks: list[Callable[[], None]] = field(default_factory=list)
    _cycle_callbacks: list[Callable[[], None]] = field(default_factory=list)
    _checkpoints: CheckpointManager | None = field(default=None, init=False, repr=False)

    def on_frame_change(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked after any operation that changes frames."""
        self._frame_callbacks.append(callback)

    def on_each_cycle(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked after every single emulated frame.

        Unlike on_frame_change (fires once per MCP action batch), this fires
        after *every* cycle. Used by the HLS streamer to capture each frame.
        """
        self._cycle_callbacks.append(callback)

    def remove_cycle_callback(self, callback: Callable[[], None]) -> None:
        """Remove a previously registered per-cycle callback."""
        try:
            self._cycle_callbacks.remove(callback)
        except ValueError:
            pass

    def _notify_cycle(self) -> None:
        """Fire all registered per-cycle callbacks."""
        for cb in self._cycle_callbacks:
            try:
                cb()
            except Exception:
                logger.warning("cycle callback error in %s", cb, exc_info=True)

    def _notify_frame_change(self) -> None:
        """Fire all registered frame-change callbacks."""
        for cb in self._frame_callbacks:
            try:
                cb()
            except Exception:
                logger.warning("frame callback error in %s", cb, exc_info=True)

    @property
    def checkpoints_dir(self) -> Path:
        d = self.data_dir / "checkpoints"
        d.mkdir(exist_ok=True)
        return d

    @property
    def checkpoints(self) -> CheckpointManager:
        if self._checkpoints is None:
            self._checkpoints = CheckpointManager(self.checkpoints_dir)
        return self._checkpoints

    @property
    def savestates_dir(self) -> Path:
        d = self.data_dir / "savestates"
        d.mkdir(exist_ok=True)
        return d

    @property
    def macros_dir(self) -> Path:
        d = self.data_dir / "macros"
        d.mkdir(exist_ok=True)
        return d

    @property
    def watches_dir(self) -> Path:
        d = self.data_dir / "watches"
        d.mkdir(exist_ok=True)
        return d

    @property
    def snapshots_dir(self) -> Path:
        d = self.data_dir / "snapshots"
        d.mkdir(exist_ok=True)
        return d

    @property
    def screenshots_dir(self) -> Path:
        d = self.data_dir / "screenshots"
        d.mkdir(exist_ok=True)
        return d

    def initialize(self) -> str:
        """Initialize the melonDS engine. Must be called first."""
        if self.is_initialized:
            logger.debug("initialize() called but already initialized")
            return "Already initialized."

        logger.info("Initializing melonDS engine")
        self.emu = MelonDS()
        result = self.emu.init()
        if result == -1:
            logger.error("melonds_init() failed (returned -1)")
            raise RuntimeError("melonds_init() failed")

        self.is_initialized = True
        jit = self.emu.jit_enabled()
        logger.info("melonDS initialized successfully (JIT: %s)", jit)
        return f"melonDS initialized successfully (JIT: {jit})."

    def load_rom(self, rom_path: str) -> str:
        """Load a ROM file. Requires initialization first."""
        if not self.is_initialized or self.emu is None:
            raise RuntimeError("Call init_emulator first.")

        path = Path(rom_path).resolve()
        if not path.exists():
            logger.error("ROM not found: %s", path)
            raise FileNotFoundError(f"ROM not found: {path}")

        logger.info("Loading ROM: %s", path)
        result = self.emu.open(str(path))
        if result < 1:
            logger.error("Failed to load ROM: %s (error code: %d)", path, result)
            raise RuntimeError(f"Failed to load ROM: {path} (error code: {result})")

        self.rom_path = str(path)
        self.is_rom_loaded = True
        self.frame_count = 0
        self._notify_frame_change()
        logger.info("ROM loaded successfully: %s", path.name)
        return f"ROM loaded: {path.name}"

    def _require_rom(self) -> MelonDS:
        """Guard: require a ROM to be loaded. Returns the emu instance."""
        if not self.is_rom_loaded or self.emu is None:
            raise RuntimeError("No ROM loaded. Call load_rom first.")
        return self.emu

    def advance_frame(
        self,
        buttons: list[str] | None = None,
        touch_x: int | None = None,
        touch_y: int | None = None,
    ) -> None:
        """Set input and advance one frame."""
        emu = self._require_rom()

        # Set keypad
        bitmask = buttons_to_bitmask(buttons) if buttons else 0
        emu.input_keypad_update(bitmask)

        # Set touch
        if touch_x is not None and touch_y is not None:
            emu.input_set_touch_pos(touch_x, touch_y)
        else:
            emu.input_release_touch()

        emu.cycle()
        self.frame_count += 1
        self._notify_cycle()

    def advance_frames(
        self,
        count: int,
        buttons: list[str] | None = None,
        touch_x: int | None = None,
        touch_y: int | None = None,
    ) -> int:
        """Advance multiple frames holding the same input. Returns frames advanced.

        When count > 1, GPU rendering is skipped on intermediate frames for
        performance. Only the final frame is fully rendered.
        """
        emu = self._require_rom()
        t0 = time.monotonic()
        if count > 1:
            emu.set_skip_render(True)
            try:
                for _ in range(count - 1):
                    self.advance_frame(buttons, touch_x, touch_y)
            finally:
                emu.set_skip_render(False)
        self.advance_frame(buttons, touch_x, touch_y)
        elapsed = time.monotonic() - t0
        self._notify_frame_change()
        if count > 1:
            logger.debug(
                "advance_frames: %d frames in %.3fs (%.1f fps), now at frame %d",
                count, elapsed, count / elapsed if elapsed > 0 else 0, self.frame_count,
            )
        if elapsed > 5.0:
            logger.info(
                "Slow advance_frames: %d frames took %.3fs (buttons=%s, frame=%d)",
                count, elapsed, buttons, self.frame_count,
            )
        return count

    def press_buttons(self, buttons: list[str], frames: int = 1) -> None:
        """Press buttons for N frames, then release for 1 frame."""
        for _ in range(frames):
            self.advance_frame(buttons)
        # Release — always render the final frame
        self.advance_frame()
        self._notify_frame_change()

    def tap_touch_screen(self, x: int, y: int, frames: int = 1) -> None:
        """Tap the touchscreen for N frames, then release for 1 frame."""
        for _ in range(frames):
            self.advance_frame(touch_x=x, touch_y=y)
        # Release — always render the final frame
        self.advance_frame()
        self._notify_frame_change()

    def run_macro_steps(self, steps: list[dict]) -> int:
        """Execute a list of macro steps. Returns total frames advanced."""
        frames_before = self.frame_count
        for step in steps:
            action = step["action"]
            if action == "press":
                self.press_buttons(step["buttons"], step.get("frames", 1))
            elif action == "hold":
                self.advance_frames(
                    step.get("frames", 1),
                    step.get("buttons"),
                    step.get("touch_x"),
                    step.get("touch_y"),
                )
            elif action == "wait":
                self.advance_frames(step.get("frames", 1))
            elif action == "tap":
                self.tap_touch_screen(
                    step["x"], step["y"], step.get("frames", 1)
                )
            else:
                raise ValueError(f"Unknown macro action: {action!r}")
        self._notify_frame_change()
        return self.frame_count - frames_before

    def capture_screenshot(
        self, screen: str = "both", fmt: str = "png"
    ) -> tuple[str, bytes]:
        """Capture the current screen as an image.

        Args:
            screen: "top", "bottom", or "both".
            fmt: "png" or "jpeg".

        Returns:
            Tuple of (mime_type, image_bytes).
        """
        emu = self._require_rom()
        raw_rgb = emu.screenshot()

        assert len(raw_rgb) == SCREENSHOT_RGB_SIZE

        img = Image.frombytes("RGB", (TOTAL_WIDTH, TOTAL_HEIGHT), raw_rgb)

        if screen == "top":
            img = img.crop((0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))
        elif screen == "bottom":
            img = img.crop((0, SCREEN_HEIGHT, SCREEN_WIDTH, SCREEN_HEIGHT * 2))

        buf = io.BytesIO()
        if fmt == "jpeg":
            img.save(buf, format="JPEG", quality=85)
            mime = "image/jpeg"
        else:
            img.save(buf, format="PNG")
            mime = "image/png"

        return mime, buf.getvalue()

    def capture_screenshot_base64(
        self, screen: str = "both", fmt: str = "png"
    ) -> str:
        """Capture the current screen as a base64-encoded string."""
        _, image_bytes = self.capture_screenshot(screen, fmt)
        return base64.b64encode(image_bytes).decode("ascii")

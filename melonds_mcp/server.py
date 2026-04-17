"""MCP server exposing melonDS emulator control tools for LLM gameplay."""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .constants import FRAMES_PER_SECOND, VALID_BUTTONS
from .emulator import EmulatorState

logger = logging.getLogger(__name__)


def _with_lock(holder: EmulatorState):
    """Decorator factory — wraps a tool function so it acquires the emulator lock."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            with holder.lock:
                lock_wait = time.monotonic() - t0
                if lock_wait > 0.1:
                    logger.warning(
                        "MCP lock contention: waited %.3fs for lock (tool=%s)",
                        lock_wait, fn.__name__,
                    )
                return fn(*args, **kwargs)
        return wrapper
    return decorator


# ── Stream catchup ──────────────────────────────────────────────

_CATCHUP_MAX_GAP = 1800    # 30s * 60fps — max frames renderer may lag
_CATCHUP_TIMEOUT = 60      # seconds to wait before giving up
_RESYNC_THRESHOLD = 3600   # 60s behind triggers a savestate resync

# Tools that advance emulation frames and should wait for the renderer.
_CATCHUP_TOOLS = {
    "advance_frames", "advance_frames_until",
    "press_buttons", "tap_touch_screen", "run_macro",
}


def _wait_for_stream_catchup(holder: EmulatorState, timeout: int = _CATCHUP_TIMEOUT) -> None:
    """Block until the renderer is within 30 s of the main emulator frame.

    Called *after* releasing the emulator lock so other tools can proceed.
    No-op when no renderer is running or when stream pacing is ``"async"``.
    """
    from .settings import get_stream_pacing

    if get_stream_pacing() != "live":
        return

    proc = getattr(holder, "_renderer_proc", None)
    if proc is None or proc.poll() is not None:
        return

    frame_file = getattr(holder, "_renderer_frame_file", None)
    if frame_file is None:
        return
    deadline = time.monotonic() + timeout
    resync_sent = False

    while time.monotonic() < deadline:
        try:
            data = json.loads(frame_file.read_text())
            renderer_frame = data["emulator_frame"]
            gap = holder.frame_count - renderer_frame
            if gap <= _CATCHUP_MAX_GAP:
                return
            # If far behind and we haven't resynced yet, trigger one
            if gap > _RESYNC_THRESHOLD and not resync_sent:
                _trigger_resync(holder)
                resync_sent = True
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        # Re-check renderer health each iteration
        if proc.poll() is not None:
            return
        time.sleep(0.5)

    logger.warning(
        "Stream catchup timed out (main_frame=%d, timeout=%ds)",
        holder.frame_count, timeout,
    )


def _trigger_resync(holder: EmulatorState) -> None:
    """Save state and send a sync entry so the renderer can jump ahead."""
    journal = getattr(holder, "_journal", None)
    if journal is None:
        return
    sync_path = str(holder.data_dir / f".renderer_sync_{os.getpid()}.mst")
    with holder.lock:
        if holder.emu is not None:
            holder.emu.savestate_save(sync_path)
    journal.write_sync(sync_path)
    logger.info("Triggered renderer resync at frame %d", holder.frame_count)


def _with_lock_and_catchup(holder: EmulatorState):
    """Lock wrapper that also waits for stream catchup after execution.

    The emulator lock is held during execution, then released before the
    catchup poll so other tools aren't blocked while we wait.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            with holder.lock:
                lock_wait = time.monotonic() - t0
                if lock_wait > 0.1:
                    logger.warning(
                        "MCP lock contention: waited %.3fs for lock (tool=%s)",
                        lock_wait, fn.__name__,
                    )
                result = fn(*args, **kwargs)
            _wait_for_stream_catchup(holder)
            return result
        return wrapper
    return decorator


# Limits
MAX_ADVANCE_FRAMES = 3600  # 60 seconds at 60fps
MAX_MEMORY_READ_COUNT = 4096
MAX_MEMORY_DUMP_SIZE = 1024 * 1024  # 1 MB
MAX_DIFF_RESULTS = 500
MAX_MACRO_STEPS = 100
MAX_MACRO_REPEAT = 100
MAX_WATCH_FIELDS = 64

# Valid macro step actions and their required/optional fields
_MACRO_STEP_SCHEMA: dict[str, dict[str, Any]] = {
    "press": {"required": ["buttons"], "optional": ["frames"]},
    "hold": {"required": [], "optional": ["buttons", "frames", "touch_x", "touch_y"]},
    "wait": {"required": [], "optional": ["frames"]},
    "tap": {"required": ["x", "y"], "optional": ["frames"]},
}

# Valid watch field sizes and their byte widths
_WATCH_FIELD_SIZES = {"byte": 1, "short": 2, "long": 4}

# Valid transform types
_WATCH_TRANSFORM_TYPES = {"map"}


# ── Tool logic functions (testable without MCP protocol) ──────────


def _start_bridge(holder: EmulatorState) -> str | None:
    """Start the IPC bridge server if not already running."""
    if hasattr(holder, "_bridge") and holder._bridge is not None:
        return holder._bridge._socket_path
    from .bridge import BridgeServer

    sock_path = os.environ.get(
        "MELONDS_BRIDGE_SOCK",
        str(holder.data_dir / ".melonds_bridge.sock"),
    )
    bridge = BridgeServer(holder, sock_path)
    path = bridge.start()
    holder._bridge = bridge
    return path


_JOURNAL_CHUNK_SIZE = 60  # 1 second of frames per journal entry


def _journal_write(holder: EmulatorState, method: str, **kwargs) -> None:
    """Write a journal entry if the journal is active. Check renderer health.

    Large frame entries are automatically chunked into 60-frame (1 second)
    entries so the renderer can report fine-grained progress.
    """
    j = getattr(holder, "_journal", None)
    if j is None:
        return
    # Check if renderer is still alive
    proc = getattr(holder, "_renderer_proc", None)
    if proc and proc.poll() is not None:
        logger.warning("Renderer process exited (code=%d)", proc.returncode)
        holder._renderer_proc = None
        j.stop()
        holder._journal = None
        return

    # Chunk large frame writes for finer-grained renderer progress
    if method == "write_frames" and kwargs.get("count", 1) > _JOURNAL_CHUNK_SIZE:
        count = kwargs["count"]
        buttons = kwargs.get("buttons")
        touch_x = kwargs.get("touch_x")
        touch_y = kwargs.get("touch_y")
        while count > 0:
            chunk = min(count, _JOURNAL_CHUNK_SIZE)
            j.write_frames(chunk, buttons, touch_x, touch_y)
            count -= chunk
        return

    getattr(j, method)(**kwargs)


def _journal_macro_steps(holder: EmulatorState, steps: list[dict]) -> None:
    """Write journal entries for decomposed macro steps."""
    for step in steps:
        action = step["action"]
        if action == "press":
            _journal_write(holder, "write_frames", count=step.get("frames", 1),
                           buttons=step["buttons"], touch_x=None, touch_y=None)
            _journal_write(holder, "write_frames", count=1,
                           buttons=None, touch_x=None, touch_y=None)
        elif action == "hold":
            _journal_write(holder, "write_frames", count=step.get("frames", 1),
                           buttons=step.get("buttons"), touch_x=step.get("touch_x"),
                           touch_y=step.get("touch_y"))
        elif action == "wait":
            _journal_write(holder, "write_frames", count=step.get("frames", 1),
                           buttons=None, touch_x=None, touch_y=None)
        elif action == "tap":
            _journal_write(holder, "write_frames", count=step.get("frames", 1),
                           buttons=None, touch_x=step["x"], touch_y=step["y"])
            _journal_write(holder, "write_frames", count=1,
                           buttons=None, touch_x=None, touch_y=None)


def _tool_init_emulator(holder: EmulatorState) -> dict[str, Any]:
    logger.info("Tool: init_emulator")
    msg = holder.initialize()
    bridge_path = _start_bridge(holder)
    result: dict[str, Any] = {"success": True, "message": msg}
    if bridge_path:
        result["bridge_socket"] = bridge_path
    logger.info("init_emulator complete (bridge=%s)", bridge_path)
    return result


def _tool_load_rom(holder: EmulatorState, rom_path: str, name: str = "unnamed") -> dict[str, Any]:
    logger.info("Tool: load_rom path=%s name=%s", rom_path, name)

    # If the renderer is running, journal the ROM load so it reloads too
    _journal_write(holder, "write_load_rom", rom_path=str(Path(rom_path).resolve()))

    msg = holder.load_rom(rom_path)
    result: dict[str, Any] = {"success": True, "rom_path": holder.rom_path, "message": msg}

    # Auto-start viewer + stream + recording if enabled
    from .settings import get_stream

    if get_stream():
        viewer_result = _tool_start_viewer(holder)
        stream_result = _tool_start_video_stream(holder, name=name)
        result["auto_started"] = "stream"
        result["viewer_url"] = viewer_result.get("url")
        result["stream_url"] = stream_result.get("url")

    return result


def _tool_start_viewer(holder: EmulatorState, port: int = 8090) -> dict[str, Any]:
    from .viewer import ViewerServer, archive_old_screenshots

    logger.info("Tool: start_viewer port=%d", port)
    if hasattr(holder, "_viewer") and holder._viewer is not None:
        logger.debug("Viewer already running on port %d", holder._viewer.port)
        return {
            "success": True,
            "message": f"Viewer already running on port {holder._viewer.port}.",
            "url": f"http://localhost:{holder._viewer.port}",
        }
    archived = archive_old_screenshots(holder.screenshots_dir)
    viewer = ViewerServer(holder, port=port)
    viewer.start()
    holder._viewer = viewer
    holder.on_frame_change(viewer.notify)
    logger.info("Viewer started on port %d", port)
    result: dict[str, Any] = {
        "success": True,
        "message": f"Viewer started on port {port}.",
        "url": f"http://localhost:{port}",
    }
    if archived:
        result["archived_screenshots"] = str(archived)
    return result


def _tool_start_video_stream(holder: EmulatorState, port: int = 18091, name: str = "unnamed") -> dict[str, Any]:
    import socket
    import subprocess
    import sys

    from .journal import JournalWriter

    logger.info("Tool: start_video_stream port=%d name=%s", port, name)

    # Check if renderer is already running
    proc = getattr(holder, "_renderer_proc", None)
    if proc is not None and proc.poll() is None:
        existing_port = getattr(holder, "_renderer_port", port)
        logger.debug("Renderer already running (pid=%d)", proc.pid)
        return {
            "success": True,
            "message": f"Video stream already running (renderer pid={proc.pid}).",
            "url": f"http://localhost:{existing_port}",
        }

    holder._require_rom()

    # Probe for a free port starting at the requested one. A detached renderer
    # from a prior session may still be holding the default, so we step up
    # rather than fail to bind in the subprocess.
    chosen_port = port
    for candidate in range(port, port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", candidate))
                chosen_port = candidate
                break
            except OSError:
                continue
    else:
        raise RuntimeError(f"No free port found in range {port}-{port + 99}")
    if chosen_port != port:
        logger.info("Port %d busy, using %d instead", port, chosen_port)

    # Namespace per-session renderer files by server PID so a detached
    # renderer from a previous session cannot collide with ours.
    pid = os.getpid()
    journal_path = str(holder.data_dir / f".melonds_journal_{pid}.jsonl")
    frame_file = holder.data_dir / f".renderer_frame_{pid}"
    renderer_log = str(holder.data_dir / f".renderer_{pid}.log")

    # Save current state so the renderer can start from the same point
    initial_state = None
    if holder.frame_count > 0:
        initial_state = str(holder.data_dir / f".renderer_initial_{pid}.mst")
        holder.emu.savestate_save(initial_state)
        logger.info("Saved initial state for renderer at frame %d", holder.frame_count)

    # Start journal writer (append-only JSONL file)
    journal = JournalWriter(journal_path)
    journal.start()
    holder._journal = journal
    holder._renderer_frame_file = frame_file
    holder._renderer_port = chosen_port

    # Record the starting frame so commentary stream_time can be computed
    holder._stream_start_frame = holder.frame_count

    # Build renderer command
    cmd = [
        sys.executable, "-m", "melonds_mcp.renderer",
        "--journal-file", journal_path,
        "--rom", holder.rom_path,
        "--port", str(chosen_port),
        "--frame-file", str(frame_file),
        "--server-pid", str(pid),
        "--log-file", renderer_log,
    ]
    if initial_state:
        cmd += ["--initial-state", initial_state]

    from .settings import get_stream
    if get_stream():
        recordings_dir = holder.data_dir / "recordings"
        recordings_dir.mkdir(exist_ok=True)
        cmd += ["--record-dir", str(recordings_dir), "--record-name", name]

    # Launch renderer in its own session so it survives MCP server exit
    renderer_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    holder._renderer_proc = renderer_proc
    logger.info(
        "Renderer subprocess launched (pid=%d, port=%d, log=%s)",
        renderer_proc.pid, chosen_port, renderer_log,
    )

    # Tell the viewer about the HLS port so the unified page can load video
    viewer = getattr(holder, "_viewer", None)
    if viewer is not None:
        viewer.set_hls_port(chosen_port)
        viewer._stream_start_frame = holder.frame_count
        viewer.set_journal(journal)

    return {
        "success": True,
        "message": f"HLS video stream started on port {chosen_port} (renderer pid={renderer_proc.pid}).",
        "url": f"http://localhost:{chosen_port}",
    }


def _tool_stop_video_stream(holder: EmulatorState) -> dict[str, Any]:
    import subprocess

    from .settings import get_stream_pacing

    logger.info("Tool: stop_video_stream")
    journal = getattr(holder, "_journal", None)
    proc = getattr(holder, "_renderer_proc", None)

    if journal is None and proc is None:
        return {"success": True, "message": "No video stream running."}

    # Disconnect journal from viewer before shutdown
    viewer = getattr(holder, "_viewer", None)
    if viewer is not None:
        viewer.set_journal(None)

    # Write shutdown entry and close the journal file.
    if journal:
        try:
            journal.write_shutdown()
        except Exception:
            logger.warning("Failed to write journal shutdown", exc_info=True)
        journal.stop()

    pacing = get_stream_pacing()
    if proc and proc.poll() is None:
        if pacing == "live":
            # In live mode the renderer should be close to caught up — wait
            # a reasonable amount of time then terminate if stuck.
            try:
                proc.wait(timeout=15.0)
                logger.info("Renderer exited (code=%d)", proc.returncode)
            except subprocess.TimeoutExpired:
                logger.warning("Renderer did not exit in 15s, terminating")
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
        else:
            # In async mode the renderer runs independently and will
            # finish on its own after processing all journal entries.
            logger.info(
                "Async mode — renderer (pid=%d) will finish independently",
                proc.pid,
            )

    holder._journal = None
    holder._renderer_proc = None
    holder._renderer_frame_file = None
    holder._renderer_port = None
    msg = "Video stream stopped."
    if pacing != "live" and proc and proc.poll() is None:
        msg += f" Renderer (pid={proc.pid}) is still finishing the recording."
    return {"success": True, "message": msg}



def _tool_advance_frames(
    holder: EmulatorState,
    count: int,
    buttons: list[str],
    touch_x: int | None,
    touch_y: int | None,
) -> dict[str, Any]:
    if count < 1:
        raise ValueError("count must be >= 1")
    if count > MAX_ADVANCE_FRAMES:
        raise ValueError(f"count must be <= {MAX_ADVANCE_FRAMES}")
    advanced = holder.advance_frames(count, buttons or None, touch_x, touch_y)
    _journal_write(holder, "write_frames", count=count,
                   buttons=buttons or None, touch_x=touch_x, touch_y=touch_y)
    return {
        "frames_advanced": advanced,
        "total_frame": holder.frame_count,
        "buttons": buttons,
    }


def _tool_advance_frames_until(
    holder: EmulatorState,
    max_frames: int,
    conditions: list[dict],
    poll_interval: int,
    buttons: list[str],
    touch_x: int | None,
    touch_y: int | None,
    read_addresses: list[dict],
) -> dict[str, Any]:
    if max_frames < 1:
        raise ValueError("max_frames must be >= 1")
    if max_frames > MAX_ADVANCE_FRAMES:
        raise ValueError(f"max_frames must be <= {MAX_ADVANCE_FRAMES}")
    if not conditions:
        raise ValueError("Must specify at least one condition.")
    if len(conditions) > 16:
        raise ValueError("Too many conditions (max 16).")
    if poll_interval < 1:
        raise ValueError("poll_interval must be >= 1")
    if poll_interval > max_frames:
        raise ValueError("poll_interval must be <= max_frames")

    # Validate condition schemas
    valid_types = {"value", "changed", "pattern"}
    valid_operators = {"==", "!=", ">", "<", ">=", "<=", "&"}
    valid_sizes = {"byte", "short", "long"}
    for i, cond in enumerate(conditions):
        ctype = cond.get("type")
        if ctype not in valid_types:
            raise ValueError(
                f"conditions[{i}]: type must be one of {valid_types}, got {ctype!r}"
            )
        if "address" not in cond:
            raise ValueError(f"conditions[{i}]: missing required field 'address'")
        if ctype == "value":
            if "operator" not in cond:
                raise ValueError(f"conditions[{i}]: value condition requires 'operator'")
            if cond["operator"] not in valid_operators:
                raise ValueError(
                    f"conditions[{i}]: operator must be one of {valid_operators}"
                )
            if "value" not in cond:
                raise ValueError(f"conditions[{i}]: value condition requires 'value'")
        if ctype in ("value", "changed"):
            if cond.get("size", "byte") not in valid_sizes:
                raise ValueError(f"conditions[{i}]: size must be one of {valid_sizes}")
        if ctype == "pattern":
            if "length" not in cond:
                raise ValueError(f"conditions[{i}]: pattern condition requires 'length'")
            if "pattern" not in cond:
                raise ValueError(f"conditions[{i}]: pattern condition requires 'pattern'")
            # Validate hex string
            try:
                bytes.fromhex(cond["pattern"])
            except ValueError:
                raise ValueError(
                    f"conditions[{i}]: 'pattern' must be a valid hex string"
                )

    # Validate read_addresses
    for i, spec in enumerate(read_addresses):
        if "address" not in spec:
            raise ValueError(f"read_addresses[{i}]: missing required field 'address'")
        if spec.get("size", "byte") not in valid_sizes:
            raise ValueError(f"read_addresses[{i}]: size must be one of {valid_sizes}")

    result = holder.advance_frames_until(
        max_frames=max_frames,
        conditions=conditions,
        poll_interval=poll_interval,
        buttons=buttons or None,
        touch_x=touch_x,
        touch_y=touch_y,
        read_addresses=read_addresses or None,
    )
    frames_elapsed = result["frames_elapsed"]
    _journal_write(holder, "write_frames", count=frames_elapsed,
                   buttons=buttons or None, touch_x=touch_x, touch_y=touch_y)
    return result


def _tool_press_buttons(
    holder: EmulatorState, buttons: list[str], frames: int
) -> dict[str, Any]:
    if not buttons:
        raise ValueError("Must specify at least one button.")
    if frames < 1 or frames > MAX_ADVANCE_FRAMES:
        raise ValueError(f"frames must be 1-{MAX_ADVANCE_FRAMES}")
    emu = holder._require_rom()
    desc = f"press: {', '.join(buttons)}"
    if frames > 1:
        desc += f" ({frames}f)"
    cp = holder.checkpoints.create(emu, holder.frame_count, desc)
    holder.press_buttons(buttons, frames)
    _journal_write(holder, "write_frames", count=frames, buttons=buttons,
                   touch_x=None, touch_y=None)
    _journal_write(holder, "write_frames", count=1, buttons=None,
                   touch_x=None, touch_y=None)  # release frame
    return {
        "buttons": buttons,
        "held_frames": frames,
        "total_frame": holder.frame_count,
        "checkpoint_id": cp.id,
    }


def _tool_tap_touch_screen(
    holder: EmulatorState, x: int, y: int, frames: int
) -> dict[str, Any]:
    if not (0 <= x <= 255):
        raise ValueError("x must be 0-255")
    if not (0 <= y <= 191):
        raise ValueError("y must be 0-191")
    if frames < 1 or frames > MAX_ADVANCE_FRAMES:
        raise ValueError(f"frames must be 1-{MAX_ADVANCE_FRAMES}")
    emu = holder._require_rom()
    desc = f"tap: ({x}, {y})"
    if frames > 1:
        desc += f" ({frames}f)"
    cp = holder.checkpoints.create(emu, holder.frame_count, desc)
    holder.tap_touch_screen(x, y, frames)
    _journal_write(holder, "write_frames", count=frames, buttons=None,
                   touch_x=x, touch_y=y)
    _journal_write(holder, "write_frames", count=1, buttons=None,
                   touch_x=None, touch_y=None)  # release frame
    return {
        "x": x,
        "y": y,
        "held_frames": frames,
        "total_frame": holder.frame_count,
        "checkpoint_id": cp.id,
    }


def _tool_get_screenshot(
    holder: EmulatorState, screen: str
) -> tuple[str, bytes]:
    if screen not in ("top", "bottom", "both"):
        raise ValueError("screen must be 'top', 'bottom', or 'both'")
    return holder.capture_screenshot(screen, fmt="png")


def _tool_save_screenshot(
    holder: EmulatorState, file_path: str, screen: str
) -> dict[str, Any]:
    if screen not in ("top", "bottom", "both"):
        raise ValueError("screen must be 'top', 'bottom', or 'both'")
    mime, image_bytes = holder.capture_screenshot(screen, fmt="png")
    p = Path(file_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(image_bytes)
    return {
        "success": True,
        "path": str(p),
        "size_bytes": len(image_bytes),
        "screen": screen,
        "frame": holder.frame_count,
    }


def _tool_get_status(holder: EmulatorState) -> dict[str, Any]:
    status: dict[str, Any] = {
        "initialized": holder.is_initialized,
        "rom_loaded": holder.is_rom_loaded,
        "frame_count": holder.frame_count,
        "fps": FRAMES_PER_SECOND,
    }
    if holder.rom_path:
        status["rom_path"] = holder.rom_path
    if holder.emu and holder.is_rom_loaded:
        status["running"] = holder.emu.running()
        status["jit_enabled"] = holder.emu.jit_enabled()
    return status


def _tool_save_state(holder: EmulatorState, name: str) -> dict[str, Any]:
    logger.info("Tool: save_state name=%s", name)
    holder._require_rom()
    path = str(holder.savestates_dir / f"{name}.mst")
    success = holder.emu.savestate_save(path)
    logger.info("save_state completed (name=%s, success=%s)", name, success)
    return {"success": success, "name": name, "path": path}


_LOAD_STATE_TIMEOUT = 120  # seconds

# Tools that manage their own locking (excluded from bulk _with_lock wrapping).
_SELF_LOCKING_TOOLS = {"load_state"}


def _tool_load_state(holder: EmulatorState, name: str) -> dict[str, Any]:
    """Load a savestate with robust timeout handling.

    This function is excluded from the bulk _with_lock wrapping because it
    needs fine-grained control: the lock is acquired inside the worker thread
    so the main thread's timeout polling can always fire — even if lock
    acquisition itself blocks (e.g. another long-running tool holds it).
    """
    logger.info("Tool: load_state name=%s", name)
    holder._require_rom()
    path = str(holder.savestates_dir / f"{name}.mst")
    if not Path(path).exists():
        logger.warning("load_state: savestate not found: %s", name)
        raise FileNotFoundError(f"Savestate not found: {name}")

    t0 = time.monotonic()
    deadline = t0 + _LOAD_STATE_TIMEOUT
    done = threading.Event()
    result_box: list[Any] = [None, None]  # [success_bool, exception]

    def _worker() -> None:
        try:
            # Acquire lock inside the worker so the main thread isn't blocked.
            acquired = holder.lock.acquire(timeout=_LOAD_STATE_TIMEOUT)
            if not acquired:
                result_box[1] = TimeoutError("lock acquisition timed out")
                return
            try:
                result_box[0] = holder.emu.savestate_load(path)
            finally:
                holder.lock.release()
        except Exception as exc:
            result_box[1] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    # Poll with 1-second granularity so we're never dependent on a single
    # blocking wait respecting its timeout (belt-and-suspenders).
    while not done.is_set():
        if time.monotonic() >= deadline:
            elapsed = time.monotonic() - t0
            logger.error(
                "load_state TIMED OUT after %ds (name=%s, path=%s)",
                int(elapsed), name, path,
            )
            return {
                "success": False,
                "name": name,
                "error": (
                    f"load_state timed out after {int(elapsed)} seconds "
                    "(known intermittent issue). "
                    "Please try calling load_state again — it usually succeeds on retry."
                ),
            }
        done.wait(timeout=1.0)

    elapsed = time.monotonic() - t0

    if result_box[1] is not None:
        logger.error("load_state failed after %.3fs: %s", elapsed, result_box[1])
        return {
            "success": False,
            "name": name,
            "error": str(result_box[1]),
        }

    success = result_box[0]
    logger.info("load_state completed in %.3fs (name=%s, success=%s)", elapsed, name, success)
    if success:
        _journal_write(holder, "write_load_state", path=path)
    holder._notify_frame_change()
    return {"success": success, "name": name, "total_frame": holder.frame_count}


def _tool_list_states(holder: EmulatorState) -> dict[str, Any]:
    states = []
    if holder.savestates_dir.exists():
        for f in sorted(holder.savestates_dir.glob("*.mst")):
            states.append({
                "name": f.stem,
                "path": str(f),
                "size_bytes": f.stat().st_size,
            })
    return {"states": states}


def _tool_reset(holder: EmulatorState) -> dict[str, Any]:
    logger.info("Tool: reset_emulator (was at frame %d)", holder.frame_count)
    emu = holder._require_rom()
    emu.reset()
    holder.frame_count = 0
    _journal_write(holder, "write_reset")
    holder._notify_frame_change()
    return {"success": True, "message": "NDS reset.", "total_frame": 0}


def _tool_list_checkpoints(
    holder: EmulatorState, limit: int
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    checkpoints = holder.checkpoints.list_recent(limit)
    return {
        "total_checkpoints": holder.checkpoints.total_count,
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


def _tool_revert_to_checkpoint(
    holder: EmulatorState, checkpoint_id: str
) -> dict[str, Any]:
    before_count = holder.checkpoints.total_count
    cp = holder.checkpoints.revert(holder, checkpoint_id)
    _journal_write(holder, "write_load_state", path=cp.path)
    discarded = before_count - holder.checkpoints.total_count
    return {
        "success": True,
        "reverted_to": {
            "id": cp.id,
            "frame": cp.frame,
            "action": cp.action,
        },
        "total_frame": holder.frame_count,
        "remaining_checkpoints": holder.checkpoints.total_count,
        "discarded_checkpoints": discarded,
    }


def _tool_promote_checkpoint(
    holder: EmulatorState, checkpoint_id: str, name: str
) -> dict[str, Any]:
    dest_path = str(holder.savestates_dir / f"{name}.mst")
    cp = holder.checkpoints.promote(checkpoint_id, dest_path)
    return {
        "success": True,
        "name": name,
        "path": dest_path,
        "source_checkpoint": {
            "id": cp.id,
            "frame": cp.frame,
            "action": cp.action,
        },
    }


def _tool_read_memory(
    holder: EmulatorState,
    address: int,
    size: str,
    count: int,
    signed: bool,
) -> dict[str, Any]:
    emu = holder._require_rom()
    if count < 1 or count > MAX_MEMORY_READ_COUNT:
        raise ValueError(f"count must be 1-{MAX_MEMORY_READ_COUNT}")

    read_fns = {
        ("byte", False): emu.memory_read_byte,
        ("byte", True): emu.memory_read_byte_signed,
        ("short", False): emu.memory_read_short,
        ("short", True): emu.memory_read_short_signed,
        ("long", False): emu.memory_read_long,
        ("long", True): emu.memory_read_long_signed,
    }
    fn = read_fns.get((size, signed))
    if fn is None:
        raise ValueError(f"size must be 'byte', 'short', or 'long'")

    size_bytes = {"byte": 1, "short": 2, "long": 4}[size]
    values = []
    for i in range(count):
        values.append(fn(address + i * size_bytes))

    return {
        "address": f"0x{address:08X}",
        "size": size,
        "signed": signed,
        "values": values,
        "hex_values": [f"0x{v & ((1 << (size_bytes * 8)) - 1):0{size_bytes * 2}X}" for v in values],
    }


def _tool_write_memory(
    holder: EmulatorState,
    address: int,
    value: int,
    size: str,
) -> dict[str, Any]:
    emu = holder._require_rom()
    if size == "byte":
        emu.memory_write_byte(address, value)
    elif size == "short":
        emu.memory_write_short(address, value)
    elif size == "long":
        emu.memory_write_long(address, value)
    else:
        raise ValueError("size must be 'byte', 'short', or 'long'")

    return {
        "success": True,
        "address": f"0x{address:08X}",
        "value": value,
        "size": size,
    }


def _tool_backup_save_import(
    holder: EmulatorState, path: str
) -> dict[str, Any]:
    emu = holder._require_rom()
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Save file not found: {p}")
    success = emu.backup_import(str(p))
    return {"success": success, "path": str(p)}


def _tool_backup_save_export(
    holder: EmulatorState, path: str
) -> dict[str, Any]:
    emu = holder._require_rom()
    p = Path(path).resolve()
    success = emu.backup_export(str(p))
    return {"success": success, "path": str(p)}


# ── Memory scanning helpers ───────────────────────────────────────


def _read_memory_region(holder: EmulatorState, address: int, size: int) -> bytes:
    """Read a contiguous region of memory as raw bytes (bulk, single FFI call)."""
    emu = holder._require_rom()
    return emu.memory_read_block(address, size)


def _tool_dump_memory(
    holder: EmulatorState, address: int, size: int, file_path: str
) -> dict[str, Any]:
    if size < 1 or size > MAX_MEMORY_DUMP_SIZE:
        raise ValueError(f"size must be 1-{MAX_MEMORY_DUMP_SIZE} (1 MB max)")
    data = _read_memory_region(holder, address, size)
    p = Path(file_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {
        "success": True,
        "address": f"0x{address:08X}",
        "size": size,
        "path": str(p),
        "frame": holder.frame_count,
    }


def _tool_snapshot_memory(
    holder: EmulatorState, name: str, address: int, size: int
) -> dict[str, Any]:
    if size < 1 or size > MAX_MEMORY_DUMP_SIZE:
        raise ValueError(f"size must be 1-{MAX_MEMORY_DUMP_SIZE} (1 MB max)")
    data = _read_memory_region(holder, address, size)
    # Save binary data
    bin_path = holder.snapshots_dir / f"{name}.bin"
    bin_path.write_bytes(data)
    # Save metadata
    meta = {
        "name": name,
        "address": address,
        "size": size,
        "frame": holder.frame_count,
    }
    meta_path = holder.snapshots_dir / f"{name}.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    return {
        "success": True,
        "name": name,
        "address": f"0x{address:08X}",
        "size": size,
        "frame": holder.frame_count,
    }


def _tool_diff_snapshots(
    holder: EmulatorState,
    name_a: str,
    name_b: str,
    value_size: str,
    filter_type: str,
) -> dict[str, Any]:
    # Load snapshots
    for name in (name_a, name_b):
        if not (holder.snapshots_dir / f"{name}.bin").exists():
            raise FileNotFoundError(f"Snapshot not found: {name!r}")

    meta_a = json.loads((holder.snapshots_dir / f"{name_a}.json").read_text())
    meta_b = json.loads((holder.snapshots_dir / f"{name_b}.json").read_text())

    if meta_a["address"] != meta_b["address"] or meta_a["size"] != meta_b["size"]:
        raise ValueError(
            "Snapshots must cover the same address range. "
            f"{name_a}: 0x{meta_a['address']:08X}+{meta_a['size']}, "
            f"{name_b}: 0x{meta_b['address']:08X}+{meta_b['size']}"
        )

    data_a = (holder.snapshots_dir / f"{name_a}.bin").read_bytes()
    data_b = (holder.snapshots_dir / f"{name_b}.bin").read_bytes()
    base_addr = meta_a["address"]

    if value_size not in _WATCH_FIELD_SIZES:
        raise ValueError(f"value_size must be one of {list(_WATCH_FIELD_SIZES.keys())}")
    step = _WATCH_FIELD_SIZES[value_size]

    # Parse filter
    filter_fn = None
    if filter_type == "changed":
        filter_fn = lambda old, new: old != new
    elif filter_type == "increased":
        filter_fn = lambda old, new: new > old
    elif filter_type == "decreased":
        filter_fn = lambda old, new: new < old
    elif filter_type == "unchanged":
        filter_fn = lambda old, new: old == new
    elif filter_type.startswith("delta:"):
        try:
            delta = int(filter_type.split(":", 1)[1])
        except ValueError:
            raise ValueError(f"Invalid delta filter: {filter_type!r}. Use 'delta:N' (e.g. 'delta:1').")
        filter_fn = lambda old, new, d=delta: (new - old) == d
    else:
        raise ValueError(
            f"Unknown filter: {filter_type!r}. Valid: changed, increased, decreased, "
            "unchanged, delta:N (e.g. delta:1, delta:-1)"
        )

    # Compare
    signed = value_size != "byte"  # use unsigned for bytes
    results = []
    total_compared = 0
    total_matched = 0

    for offset in range(0, len(data_a) - step + 1, step):
        if step == 1:
            val_a = data_a[offset]
            val_b = data_b[offset]
        elif step == 2:
            val_a = int.from_bytes(data_a[offset : offset + 2], "little", signed=False)
            val_b = int.from_bytes(data_b[offset : offset + 2], "little", signed=False)
        else:  # 4
            val_a = int.from_bytes(data_a[offset : offset + 4], "little", signed=False)
            val_b = int.from_bytes(data_b[offset : offset + 4], "little", signed=False)

        total_compared += 1
        if filter_fn(val_a, val_b):
            total_matched += 1
            if len(results) < MAX_DIFF_RESULTS:
                results.append({
                    "address": f"0x{base_addr + offset:08X}",
                    "offset": offset,
                    "old": val_a,
                    "new": val_b,
                    "delta": val_b - val_a,
                })

    return {
        "snapshot_a": name_a,
        "snapshot_b": name_b,
        "value_size": value_size,
        "filter": filter_type,
        "total_compared": total_compared,
        "total_matched": total_matched,
        "results_shown": len(results),
        "truncated": total_matched > MAX_DIFF_RESULTS,
        "results": results,
    }


def _tool_list_snapshots(holder: EmulatorState) -> dict[str, Any]:
    snapshots = []
    if holder.snapshots_dir.exists():
        for f in sorted(holder.snapshots_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                snapshots.append({
                    "name": data["name"],
                    "address": f"0x{data['address']:08X}",
                    "size": data["size"],
                    "frame": data["frame"],
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return {"snapshots": snapshots}


# ── Macro helpers ────────────────────────────────────────────────


def _validate_macro_steps(steps: list[dict]) -> None:
    """Validate macro steps against the schema."""
    if not steps:
        raise ValueError("Macro must have at least one step.")
    if len(steps) > MAX_MACRO_STEPS:
        raise ValueError(f"Macro can have at most {MAX_MACRO_STEPS} steps.")

    for i, step in enumerate(steps):
        if "action" not in step:
            raise ValueError(f"Step {i}: missing 'action' field.")
        action = step["action"]
        if action not in _MACRO_STEP_SCHEMA:
            raise ValueError(
                f"Step {i}: unknown action {action!r}. "
                f"Valid: {list(_MACRO_STEP_SCHEMA.keys())}"
            )
        schema = _MACRO_STEP_SCHEMA[action]
        for field in schema["required"]:
            if field not in step:
                raise ValueError(
                    f"Step {i} ({action}): missing required field {field!r}."
                )
        valid_fields = {"action"} | set(schema["required"]) | set(schema["optional"])
        for field in step:
            if field not in valid_fields:
                raise ValueError(
                    f"Step {i} ({action}): unknown field {field!r}. "
                    f"Valid: {sorted(valid_fields)}"
                )
        if "frames" in step:
            f = step["frames"]
            if not isinstance(f, int) or f < 1 or f > MAX_ADVANCE_FRAMES:
                raise ValueError(f"Step {i}: frames must be 1-{MAX_ADVANCE_FRAMES}.")


def _tool_create_macro(
    holder: EmulatorState,
    name: str,
    description: str,
    steps: list[dict],
) -> dict[str, Any]:
    _validate_macro_steps(steps)
    macro = {"name": name, "description": description, "steps": steps}
    path = holder.macros_dir / f"{name}.json"
    path.write_text(json.dumps(macro, indent=2))
    return {
        "success": True,
        "name": name,
        "description": description,
        "step_count": len(steps),
        "path": str(path),
    }


def _tool_list_macros(holder: EmulatorState) -> dict[str, Any]:
    macros = []
    if holder.macros_dir.exists():
        for f in sorted(holder.macros_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                macros.append({
                    "name": data["name"],
                    "description": data["description"],
                    "step_count": len(data["steps"]),
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return {"macros": macros}


def _tool_run_macro(
    holder: EmulatorState, name: str, repeat: int
) -> dict[str, Any]:
    emu = holder._require_rom()
    if repeat < 1 or repeat > MAX_MACRO_REPEAT:
        raise ValueError(f"repeat must be 1-{MAX_MACRO_REPEAT}")
    path = holder.macros_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Macro not found: {name!r}")
    data = json.loads(path.read_text())
    steps = data["steps"]
    _validate_macro_steps(steps)

    desc = f"macro: {name}"
    if repeat > 1:
        desc += f" (x{repeat})"
    cp = holder.checkpoints.create(emu, holder.frame_count, desc)

    total_frames = 0
    for _ in range(repeat):
        total_frames += holder.run_macro_steps(steps)
        _journal_macro_steps(holder, steps)

    return {
        "name": name,
        "repeat": repeat,
        "frames_advanced": total_frames,
        "total_frame": holder.frame_count,
        "checkpoint_id": cp.id,
    }


def _tool_delete_macro(holder: EmulatorState, name: str) -> dict[str, Any]:
    path = holder.macros_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Macro not found: {name!r}")
    path.unlink()
    return {"success": True, "name": name}


# ── ROM filesystem helpers ────────────────────────────────────────


def _get_rom_object(holder: EmulatorState):
    """Load and cache the parsed NDS ROM object."""
    holder._require_rom()
    if not hasattr(holder, "_rom_obj") or holder._rom_obj is None:
        import ndspy.rom

        holder._rom_obj = ndspy.rom.NintendoDSRom.fromFile(holder.rom_path)
    return holder._rom_obj


def _walk_rom_folder(folder, prefix: str = "") -> list[dict[str, Any]]:
    """Recursively enumerate files in an ndspy Folder."""
    entries: list[dict[str, Any]] = []
    for name, subfolder in folder.folders:
        entries.append({"path": prefix + name + "/", "type": "directory"})
        entries.extend(_walk_rom_folder(subfolder, prefix + name + "/"))
    for fname in folder.files:
        entries.append({"path": prefix + fname, "type": "file"})
    return entries


def _tool_list_rom_files(
    holder: EmulatorState, path: str
) -> dict[str, Any]:
    rom = _get_rom_object(holder)
    root = rom.filenames

    # Navigate to the requested path
    if path and path != "/":
        parts = [p for p in path.strip("/").split("/") if p]
        current = root
        for part in parts:
            found = False
            for name, subfolder in current.folders:
                if name == part:
                    current = subfolder
                    found = True
                    break
            if not found:
                raise FileNotFoundError(f"ROM path not found: {path!r}")
        entries: list[dict[str, Any]] = []
        for name, subfolder in current.folders:
            entries.append({"path": path.rstrip("/") + "/" + name + "/", "type": "directory"})
        for fname in current.files:
            fid = rom.filenames.idOf(path.strip("/") + "/" + fname)
            entries.append({
                "path": path.rstrip("/") + "/" + fname,
                "type": "file",
                "size": len(rom.files[fid]),
            })
    else:
        entries = []
        for name, subfolder in root.folders:
            entries.append({"path": name + "/", "type": "directory"})
        for fname in root.files:
            entries.append({"path": fname, "type": "file"})

    return {"path": path, "entries": entries}


def _tool_extract_rom_file(
    holder: EmulatorState, rom_path: str, output_path: str
) -> dict[str, Any]:
    rom = _get_rom_object(holder)
    clean_path = rom_path.strip("/")
    try:
        fid = rom.filenames.idOf(clean_path)
    except ValueError:
        raise FileNotFoundError(f"File not found in ROM: {rom_path!r}")
    data = rom.files[fid]
    p = Path(output_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {
        "success": True,
        "rom_path": rom_path,
        "output_path": str(p),
        "size": len(data),
    }


def _tool_unpack_narc(
    holder: EmulatorState, file_path: str, output_dir: str
) -> dict[str, Any]:
    import ndspy.narc

    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    narc = ndspy.narc.NARC(p.read_bytes())

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    extracted = []
    for i, data in enumerate(narc.files):
        fname = f"{i:04d}.bin"
        (out / fname).write_bytes(data)
        extracted.append({"index": i, "name": fname, "size": len(data)})

    return {
        "success": True,
        "source": str(p),
        "output_dir": str(out),
        "file_count": len(extracted),
        "files": extracted[:50],  # Cap listing at 50
        "truncated": len(extracted) > 50,
    }


# ── Watch helpers ────────────────────────────────────────────────


def _validate_watch_fields(fields: list[dict]) -> None:
    """Validate memory watch field definitions."""
    if not fields:
        raise ValueError("Watch must have at least one field.")
    if len(fields) > MAX_WATCH_FIELDS:
        raise ValueError(f"Watch can have at most {MAX_WATCH_FIELDS} fields.")

    names_seen: set[str] = set()
    for i, field in enumerate(fields):
        # Required keys
        for key in ("name", "offset", "size"):
            if key not in field:
                raise ValueError(f"Field {i}: missing required key {key!r}.")

        name = field["name"]
        if not isinstance(name, str) or not name:
            raise ValueError(f"Field {i}: 'name' must be a non-empty string.")
        if name in names_seen:
            raise ValueError(f"Field {i}: duplicate field name {name!r}.")
        names_seen.add(name)

        if not isinstance(field["offset"], int) or field["offset"] < 0:
            raise ValueError(f"Field {i} ({name}): 'offset' must be a non-negative integer.")

        if field["size"] not in _WATCH_FIELD_SIZES:
            raise ValueError(
                f"Field {i} ({name}): 'size' must be one of {list(_WATCH_FIELD_SIZES.keys())}."
            )

        # Optional keys
        if "signed" in field and not isinstance(field["signed"], bool):
            raise ValueError(f"Field {i} ({name}): 'signed' must be a boolean.")

        if "transform" in field:
            _validate_transform(i, name, field["transform"])

        valid_keys = {"name", "offset", "size", "signed", "transform"}
        for key in field:
            if key not in valid_keys:
                raise ValueError(
                    f"Field {i} ({name}): unknown key {key!r}. Valid: {sorted(valid_keys)}"
                )


def _validate_transform(field_idx: int, field_name: str, transform: dict) -> None:
    """Validate a single field transform."""
    if "type" not in transform:
        raise ValueError(f"Field {field_idx} ({field_name}): transform missing 'type'.")
    if transform["type"] not in _WATCH_TRANSFORM_TYPES:
        raise ValueError(
            f"Field {field_idx} ({field_name}): unknown transform type {transform['type']!r}. "
            f"Valid: {sorted(_WATCH_TRANSFORM_TYPES)}"
        )

    if transform["type"] == "map":
        if "values" not in transform:
            raise ValueError(
                f"Field {field_idx} ({field_name}): map transform requires 'values' dict."
            )
        if not isinstance(transform["values"], dict):
            raise ValueError(
                f"Field {field_idx} ({field_name}): 'values' must be a dict mapping "
                "string keys (raw values) to display strings."
            )
        valid_keys = {"type", "values", "default"}
        for key in transform:
            if key not in valid_keys:
                raise ValueError(
                    f"Field {field_idx} ({field_name}): unknown transform key {key!r}."
                )


def _apply_transform(transform: dict, raw_value: int) -> str | None:
    """Apply a transform to a raw value. Returns display string or None."""
    if transform["type"] == "map":
        values = transform["values"]
        # Look up by string key (JSON keys are always strings)
        result = values.get(str(raw_value))
        if result is not None:
            return result
        return transform.get("default")
    return None


def _execute_watch_fields(
    holder: EmulatorState, base_address: int, fields: list[dict]
) -> list[dict[str, Any]]:
    """Read memory for each field and apply transforms."""
    emu = holder._require_rom()
    results = []
    for field in fields:
        address = base_address + field["offset"]
        size = field["size"]
        signed = field.get("signed", False)

        # Read the raw value
        if size == "byte":
            raw = emu.memory_read_byte_signed(address) if signed else emu.memory_read_byte(address)
        elif size == "short":
            raw = emu.memory_read_short_signed(address) if signed else emu.memory_read_short(address)
        else:  # long
            raw = emu.memory_read_long_signed(address) if signed else emu.memory_read_long(address)

        entry: dict[str, Any] = {
            "name": field["name"],
            "value": raw,
        }

        # Apply transform if present
        if "transform" in field:
            display = _apply_transform(field["transform"], raw)
            if display is not None:
                entry["display"] = display

        results.append(entry)
    return results


def _tool_create_watch(
    holder: EmulatorState,
    name: str,
    description: str,
    base_address: int,
    fields: list[dict],
) -> dict[str, Any]:
    _validate_watch_fields(fields)
    watch = {
        "name": name,
        "description": description,
        "base_address": base_address,
        "fields": fields,
    }
    path = holder.watches_dir / f"{name}.json"
    path.write_text(json.dumps(watch, indent=2))
    return {
        "success": True,
        "name": name,
        "description": description,
        "base_address": f"0x{base_address:08X}",
        "field_count": len(fields),
        "path": str(path),
    }


def _tool_list_watches(holder: EmulatorState) -> dict[str, Any]:
    watches = []
    if holder.watches_dir.exists():
        for f in sorted(holder.watches_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                watches.append({
                    "name": data["name"],
                    "description": data["description"],
                    "base_address": f"0x{data['base_address']:08X}",
                    "field_count": len(data["fields"]),
                    "fields": [fd["name"] for fd in data["fields"]],
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return {"watches": watches}


def _tool_read_watch(holder: EmulatorState, name: str) -> dict[str, Any]:
    holder._require_rom()
    path = holder.watches_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Watch not found: {name!r}")
    data = json.loads(path.read_text())
    _validate_watch_fields(data["fields"])

    results = _execute_watch_fields(holder, data["base_address"], data["fields"])
    return {
        "name": name,
        "description": data["description"],
        "base_address": f"0x{data['base_address']:08X}",
        "frame": holder.frame_count,
        "fields": results,
    }


def _tool_delete_watch(holder: EmulatorState, name: str) -> dict[str, Any]:
    path = holder.watches_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Watch not found: {name!r}")
    path.unlink()
    return {"success": True, "name": name}


# ── Server factory ───────────────────────────────────────────────


def create_server(data_dir: Path | None = None) -> FastMCP:
    """Create the melonDS MCP server."""
    holder = EmulatorState(data_dir=data_dir or Path.cwd())
    holder._journal = None
    holder._renderer_proc = None
    holder._stream_start_frame = 0

    mcp = FastMCP(name="melonDS MCP")

    # ── Core emulation ──

    @mcp.tool()
    def init_emulator() -> dict[str, Any]:
        """Initialize the melonDS emulation engine. Must be called before any other tool."""
        return _tool_init_emulator(holder)

    @mcp.tool()
    def load_rom(rom_path: str, name: str) -> dict[str, Any]:
        """Load a Nintendo DS ROM (.nds) file. Requires init_emulator first.

        Args:
            rom_path: Path to the .nds ROM file.
            name: Name for the recording session (shown on the recordings page).
                  Only used when auto-start is set to "stream".
        """
        return _tool_load_rom(holder, rom_path, name)

    @mcp.tool()
    def start_viewer(port: int = 8090) -> dict[str, Any]:
        """Start a web viewer that streams the DS screens to a browser.

        Opens a lightweight HTTP server on the given port. Navigate to
        http://localhost:<port> to see both DS screens updating live after
        every MCP command that advances frames.

        Args:
            port: HTTP port to listen on (default 8090).
        """
        return _tool_start_viewer(holder, port)

    @mcp.tool()
    def start_video_stream(name: str, port: int = 18091) -> dict[str, Any]:
        """Start an HLS video stream of the DS gameplay with audio.

        Launches a separate rendering emulator process that replays inputs
        at real-time 60fps and encodes them into an HLS stream served on
        the given port. Navigate to http://localhost:<port> to watch
        gameplay with audio.

        The renderer runs independently — the main emulator processes
        commands at full speed while the renderer plays them back for
        the stream.

        This is separate from the screenshot viewer (start_viewer) which
        is designed for debugging with frame-by-frame history browsing.

        Args:
            name: Name for this recording session (shown on the recordings page).
            port: HTTP port to listen on (default 18091).
        """
        return _tool_start_video_stream(holder, port, name)

    @mcp.tool()
    def stop_video_stream() -> dict[str, Any]:
        """Stop the HLS video stream and shut down the rendering process."""
        return _tool_stop_video_stream(holder)

    @mcp.tool()
    def set_stream_config(enabled: bool | None = None) -> dict[str, Any]:
        """Override the stream setting for the life of this server process.

        The override sits above env vars and settings.json in the resolution
        chain, so a single MCP session can flip streaming on/off without
        touching disk.

        Args:
            enabled: True to force streaming on, False to force it off,
                None to clear the override (fall back to env + settings.json).

        Notes:
            - Affects future load_rom calls (auto-viewer/stream/recording).
            - Does NOT stop a currently running stream — call stop_video_stream()
              for that.

        Returns:
            {"override": <bool|None>, "effective": <bool>} — the override value
            now in force and the effective stream state after resolution.
        """
        from .settings import get_stream, set_stream_override
        set_stream_override(enabled)
        return {"override": enabled, "effective": get_stream()}

    @mcp.tool()
    def advance_frames(
        count: int = 1,
        buttons: list[str] = [],
        touch_x: int | None = None,
        touch_y: int | None = None,
    ) -> dict[str, Any]:
        """Advance emulation by N frames, holding the given inputs throughout.

        Args:
            count: Number of frames to advance (1-3600). DS runs at 60fps.
            buttons: Buttons to hold. Valid: a, b, x, y, l, r, start, select, up, down, left, right.
            touch_x: Touchscreen X position (0-255). Both touch_x and touch_y required for touch input.
            touch_y: Touchscreen Y position (0-191).
        """
        return _tool_advance_frames(holder, count, buttons, touch_x, touch_y)

    @mcp.tool()
    def advance_frames_until(
        max_frames: int,
        conditions: list[dict],
        poll_interval: int = 1,
        buttons: list[str] = [],
        touch_x: int | None = None,
        touch_y: int | None = None,
        read_addresses: list[dict] = [],
    ) -> dict[str, Any]:
        """Advance up to N frames, returning early when a memory condition is met.

        Runs the poll loop internally at full emulator speed, eliminating MCP
        round-trip overhead. Checks conditions every poll_interval frames.
        Multiple conditions use OR logic (first match wins).

        Condition types:
        - value: {"type": "value", "address": int, "size": "byte"|"short"|"long",
                  "operator": "=="|"!="|">"|"<"|">="|"<="|"&", "value": int}
        - changed: {"type": "changed", "address": int, "size": "byte"|"short"|"long"}
          Fires when the value differs from its state at the start of the call.
        - pattern: {"type": "pattern", "address": int, "length": int, "pattern": "hex"}
          Scans a memory range for a byte sequence. Fires when found (skips if already present).

        Args:
            max_frames: Upper bound on frames to advance (1-3600).
            conditions: List of condition objects to check (OR logic, max 16).
            poll_interval: Check conditions every N frames (default 1).
            buttons: Buttons to hold throughout. Valid: a, b, x, y, l, r, start, select, up, down, left, right.
            touch_x: Touchscreen X position (0-255).
            touch_y: Touchscreen Y position (0-191).
            read_addresses: Additional memory reads on return. Each: {"address": int, "size": "byte"|"short"|"long", "count": int}.
        """
        return _tool_advance_frames_until(
            holder, max_frames, conditions, poll_interval,
            buttons, touch_x, touch_y, read_addresses,
        )

    @mcp.tool()
    def press_buttons(buttons: list[str], frames: int = 1) -> dict[str, Any]:
        """Press and release buttons. Holds for N frames then releases for 1 frame.

        This is the natural "press a button" action. For example, press_buttons(["a"])
        taps A once. press_buttons(["a"], frames=30) holds A for half a second.

        Args:
            buttons: Buttons to press. Valid: a, b, x, y, l, r, start, select, up, down, left, right.
            frames: How many frames to hold before releasing (1-3600).
        """
        return _tool_press_buttons(holder, buttons, frames)

    @mcp.tool()
    def tap_touch_screen(x: int, y: int, frames: int = 8) -> dict[str, Any]:
        """Tap the touchscreen (bottom screen) at a position. Holds for N frames then releases.

        The bottom screen is 256x192 pixels. Coordinates are relative to the bottom screen.

        Args:
            x: X position (0-255).
            y: Y position (0-191).
            frames: How many frames to hold the tap (1-3600). Default 8 for reliable input.
        """
        return _tool_tap_touch_screen(holder, x, y, frames)

    @mcp.tool()
    def get_screenshot(screen: str = "both") -> Any:
        """Capture the current display as a PNG image.

        Args:
            screen: Which screen to capture: "top", "bottom", or "both" (stacked vertically).
        """
        from mcp.types import ImageContent

        mime, image_bytes = _tool_get_screenshot(holder, screen)
        import base64

        return ImageContent(
            type="image",
            data=base64.b64encode(image_bytes).decode("ascii"),
            mimeType=mime,
        )

    @mcp.tool()
    def save_screenshot(file_path: str, screen: str = "both") -> dict[str, Any]:
        """Save the current display as a PNG file on disk. Useful for visual documentation.

        Args:
            file_path: Where to save the PNG (e.g. "/workspace/screenshots/frame_100.png").
            screen: Which screen to capture: "top", "bottom", or "both" (stacked vertically).
        """
        return _tool_save_screenshot(holder, file_path, screen)

    @mcp.tool()
    def get_status() -> dict[str, Any]:
        """Get the current emulator status: initialization state, ROM info, frame count, JIT status, etc."""
        return _tool_get_status(holder)

    # ── State management ──

    @mcp.tool()
    def save_state(name: str) -> dict[str, Any]:
        """Save the current emulator state to a named file. Use before risky actions.

        Args:
            name: Name for the savestate (e.g. "before_boss", "checkpoint_1").
        """
        return _tool_save_state(holder, name)

    @mcp.tool()
    def load_state(name: str) -> dict[str, Any]:
        """Load a previously saved emulator state.

        Args:
            name: Name of the savestate to load.
        """
        return _tool_load_state(holder, name)

    @mcp.tool()
    def list_states() -> dict[str, Any]:
        """List all available savestates."""
        return _tool_list_states(holder)

    @mcp.tool()
    def reset_emulator() -> dict[str, Any]:
        """Reset the NDS. Equivalent to power cycling the console."""
        return _tool_reset(holder)

    # ── Checkpoints (automatic rewind history) ──

    @mcp.tool()
    def list_checkpoints(limit: int = 20) -> dict[str, Any]:
        """List recent automatic checkpoints in chronological order.

        Checkpoints are saved automatically before every input action (press_buttons,
        tap_touch_screen, run_macro). Use revert_to_checkpoint to go back in time.

        Args:
            limit: How many recent checkpoints to show (default 20). Max 300.
        """
        return _tool_list_checkpoints(holder, limit)

    @mcp.tool()
    def revert_to_checkpoint(checkpoint_id: str) -> dict[str, Any]:
        """Revert the emulator to a previous checkpoint, undoing all actions after it.

        Loads the savestate from before that action was executed and discards all
        later checkpoints. Use list_checkpoints to find the ID to revert to.

        Args:
            checkpoint_id: The 8-character hash ID of the checkpoint (from list_checkpoints).
        """
        return _tool_revert_to_checkpoint(holder, checkpoint_id)

    @mcp.tool()
    def save_checkpoint(checkpoint_id: str, name: str) -> dict[str, Any]:
        """Save a checkpoint as a permanent named savestate without loading it.

        Copies the checkpoint's savestate file to the savestates directory under the
        given name. The current emulator state is not affected — use this to preserve
        a checkpoint for later debugging without losing your current position.

        Args:
            checkpoint_id: The 8-character hash ID of the checkpoint (from list_checkpoints).
            name: Name for the permanent savestate (e.g. "before_bug", "boss_fight").
        """
        return _tool_promote_checkpoint(holder, checkpoint_id, name)

    # ── Memory ──

    @mcp.tool()
    def read_memory(
        address: int,
        size: str = "byte",
        count: int = 1,
        signed: bool = False,
    ) -> dict[str, Any]:
        """Read values from emulator memory. Useful for checking game state (HP, score, position, etc.)
        when you know the memory addresses.

        Args:
            address: Memory address to start reading from (e.g. 0x02000000).
            size: Size of each read: "byte" (1), "short" (2), or "long" (4 bytes).
            count: Number of consecutive values to read (1-256).
            signed: If True, interpret values as signed integers.
        """
        return _tool_read_memory(holder, address, size, count, signed)

    @mcp.tool()
    def write_memory(
        address: int,
        value: int,
        size: str = "byte",
    ) -> dict[str, Any]:
        """Write a value to emulator memory.

        Args:
            address: Memory address to write to.
            value: Value to write.
            size: Size of the write: "byte" (1), "short" (2), or "long" (4 bytes).
        """
        return _tool_write_memory(holder, address, value, size)

    # ── Memory scanning ──

    @mcp.tool()
    def dump_memory(address: int, size: int, file_path: str) -> dict[str, Any]:
        """Dump a region of memory to a binary file on disk.

        Useful for offline analysis of large memory regions. The file contains
        raw bytes that can be loaded with Python, hex editors, etc.

        Args:
            address: Starting memory address (e.g. 0x02000000).
            size: Number of bytes to dump (max 1048576 = 1 MB).
            file_path: Where to save the binary file.
        """
        return _tool_dump_memory(holder, address, size, file_path)

    @mcp.tool()
    def snapshot_memory(name: str, address: int, size: int) -> dict[str, Any]:
        """Take a named snapshot of a memory region for later comparison.

        Use with diff_snapshots to find addresses that changed between two game states.
        Workflow: snapshot state A, perform an action, snapshot state B, diff them.

        Args:
            name: Unique name for this snapshot (e.g. "before_move", "after_move_right").
            address: Starting memory address (e.g. 0x02000000 for main RAM).
            size: Number of bytes to snapshot (max 1048576 = 1 MB).
        """
        return _tool_snapshot_memory(holder, name, address, size)

    @mcp.tool()
    def diff_snapshots(
        name_a: str,
        name_b: str,
        value_size: str = "short",
        filter: str = "changed",
    ) -> dict[str, Any]:
        """Compare two memory snapshots and find addresses that changed.

        Both snapshots must cover the same address range (same address and size).
        Returns up to 500 matching results with old/new values and deltas.

        Args:
            name_a: Name of the first (earlier) snapshot.
            name_b: Name of the second (later) snapshot.
            value_size: How to interpret memory: "byte" (1), "short" (2), or "long" (4 bytes).
            filter: What changes to include:
                - "changed": any value that differs (default)
                - "increased": value went up
                - "decreased": value went down
                - "unchanged": value stayed the same (for narrowing searches)
                - "delta:N": exact difference (e.g. "delta:1", "delta:-1")
        """
        return _tool_diff_snapshots(holder, name_a, name_b, value_size, filter)

    @mcp.tool()
    def list_snapshots() -> dict[str, Any]:
        """List all saved memory snapshots."""
        return _tool_list_snapshots(holder)

    # ── ROM filesystem ──

    @mcp.tool()
    def list_rom_files(path: str = "/") -> dict[str, Any]:
        """List files and directories inside the loaded ROM's NitroFS filesystem.

        Every NDS ROM contains an internal filesystem. This tool lets you browse it
        to find map data, sprites, scripts, and other game assets.

        Args:
            path: Directory path within the ROM (e.g. "/", "/fielddata/", "/fielddata/land_data/").
        """
        return _tool_list_rom_files(holder, path)

    @mcp.tool()
    def extract_rom_file(rom_path: str, output_path: str) -> dict[str, Any]:
        """Extract a file from the loaded ROM's internal filesystem to disk.

        Args:
            rom_path: Path within the ROM (e.g. "fielddata/land_data/land_data.narc").
            output_path: Where to save the extracted file on disk.
        """
        return _tool_extract_rom_file(holder, rom_path, output_path)

    @mcp.tool()
    def unpack_narc(file_path: str, output_dir: str) -> dict[str, Any]:
        """Unpack a NARC archive file (standard Nintendo DS archive format).

        NARC files contain numbered sub-files. Common in DS games for map data,
        model data, textures, etc. Files are extracted as 0000.bin, 0001.bin, etc.

        Args:
            file_path: Path to the .narc file on disk (extract it first with extract_rom_file).
            output_dir: Directory to extract files into.
        """
        return _tool_unpack_narc(holder, file_path, output_dir)

    # ── Macros ──

    @mcp.tool()
    def create_macro(
        name: str,
        description: str,
        steps: list[dict],
    ) -> dict[str, Any]:
        """Create a reusable input macro. Macros are saved to disk and persist across sessions.

        Each step is a dict with an "action" and its parameters. Available actions:

        - {"action": "press", "buttons": ["a"], "frames": 1}
          Press and release buttons (hold for N frames, then release for 1 frame).

        - {"action": "hold", "buttons": ["right"], "frames": 60}
          Hold buttons for N frames WITHOUT releasing. Can also include touch_x/touch_y.

        - {"action": "wait", "frames": 30}
          Advance N frames with no input (all buttons released).

        - {"action": "tap", "x": 128, "y": 96, "frames": 1}
          Tap the touchscreen for N frames, then release for 1 frame.

        Example — mash A through dialogue (press A, wait, repeat 5 times):
          steps=[
            {"action": "press", "buttons": ["a"]},
            {"action": "wait", "frames": 15},
            {"action": "press", "buttons": ["a"]},
            {"action": "wait", "frames": 15},
            {"action": "press", "buttons": ["a"]},
            {"action": "wait", "frames": 15},
            {"action": "press", "buttons": ["a"]},
            {"action": "wait", "frames": 15},
            {"action": "press", "buttons": ["a"]},
            {"action": "wait", "frames": 15},
          ]

        Args:
            name: Unique name for the macro (used as filename, e.g. "mash_a", "walk_right").
            description: Short description of what the macro does.
            steps: List of step dicts. Max 100 steps per macro.
        """
        return _tool_create_macro(holder, name, description, steps)

    @mcp.tool()
    def list_macros() -> dict[str, Any]:
        """List all saved macros with their names, descriptions, and step counts."""
        return _tool_list_macros(holder)

    @mcp.tool()
    def run_macro(name: str, repeat: int = 1) -> dict[str, Any]:
        """Execute a saved macro. Optionally repeat it multiple times.

        Args:
            name: Name of the macro to run.
            repeat: Number of times to run the macro (1-100). Useful for repeated
                    actions like mashing A through long dialogue.
        """
        return _tool_run_macro(holder, name, repeat)

    @mcp.tool()
    def delete_macro(name: str) -> dict[str, Any]:
        """Delete a saved macro.

        Args:
            name: Name of the macro to delete.
        """
        return _tool_delete_macro(holder, name)

    # ── Memory watches ──

    @mcp.tool()
    def create_watch(
        name: str,
        description: str,
        base_address: int,
        fields: list[dict],
    ) -> dict[str, Any]:
        """Create a reusable memory watch. Watches are saved to disk and persist across sessions.

        A watch reads structured data from a known memory address. Useful for monitoring
        game state (party Pokemon, inventory, player position, etc.) without screenshots.

        Each field reads a value at base_address + offset. Fields can optionally include
        a transform to convert raw values to meaningful display strings.

        Field format:
            {
                "name": "species_id",     # Label for this value
                "offset": 0,              # Byte offset from base_address
                "size": "short",          # "byte" (1), "short" (2), or "long" (4 bytes)
                "signed": false,          # Optional, default false
                "transform": {            # Optional
                    "type": "map",
                    "values": {"393": "Piplup", "390": "Chimchar"},
                    "default": "Unknown"
                }
            }

        Transform types:
            - "map": Dictionary lookup. Keys are stringified raw values, values are display strings.
                     "default" is returned when no key matches (omit to return no display value).

        Args:
            name: Unique name for the watch (e.g. "party_slot_1", "player_position").
            description: Short description of what this watch monitors.
            base_address: Starting memory address (e.g. 0x02000000).
            fields: List of field definitions (max 64).
        """
        return _tool_create_watch(holder, name, description, base_address, fields)

    @mcp.tool()
    def list_watches() -> dict[str, Any]:
        """List all saved memory watches with their names, descriptions, and field names."""
        return _tool_list_watches(holder)

    @mcp.tool()
    def read_watch(name: str) -> dict[str, Any]:
        """Execute a saved memory watch and return the current values.

        Returns each field's raw value and, if a transform is defined, a display value.
        For example, a species_id field with a map transform might return:
            {"name": "species_id", "value": 393, "display": "Piplup"}

        Args:
            name: Name of the watch to read.
        """
        return _tool_read_watch(holder, name)

    @mcp.tool()
    def delete_watch(name: str) -> dict[str, Any]:
        """Delete a saved memory watch.

        Args:
            name: Name of the watch to delete.
        """
        return _tool_delete_watch(holder, name)

    # ── Battery save (backup) ──

    @mcp.tool()
    def backup_save_import(path: str) -> dict[str, Any]:
        """Import a battery save file (.sav). The emulator will reset after import.

        Args:
            path: Path to the save file.
        """
        return _tool_backup_save_import(holder, path)

    @mcp.tool()
    def backup_save_export(path: str) -> dict[str, Any]:
        """Export the current battery save to a file.

        Args:
            path: Destination path for the save file.
        """
        return _tool_backup_save_export(holder, path)

    # Wrap all registered tools with the emulator lock so MCP tool calls
    # and bridge calls (from the background thread) never hit the emulator
    # concurrently. melonDS is not thread-safe.
    # Tools in _SELF_LOCKING_TOOLS manage their own lock acquisition (e.g.
    # load_state acquires inside a worker thread so the timeout can fire).
    lock_wrap = _with_lock(holder)
    catchup_wrap = _with_lock_and_catchup(holder)
    for name, tool in mcp._tool_manager._tools.items():
        if name in _SELF_LOCKING_TOOLS:
            pass  # manages own lock acquisition
        elif name in _CATCHUP_TOOLS:
            tool.fn = catchup_wrap(tool.fn)
        else:
            tool.fn = lock_wrap(tool.fn)

    return mcp

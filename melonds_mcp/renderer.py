"""Rendering emulator process — replays journal entries for HLS streaming and recording.

Launched as a subprocess by the main MCP server (with its own session so it
survives server exit):

    python -m melonds_mcp.renderer --journal-file <path> --rom <path> \
        --frame-file <path> [--initial-state <path>] --port <port> \
        [--server-pid <pid>] [--record-dir <dir>] [--record-name <name>] \
        [--log-file <path>]

Initializes its own melonDS instance, attaches the HLS streamer, and
replays input journal entries from the main emulator.  The streamer's
existing real-time throttle handles pacing.

In async mode the renderer continues processing journal entries after the
MCP server has exited, then cleans up and finalises the recording.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger("melonds_mcp.renderer")

def _setup_logging(log_file: str | None = None) -> None:
    """Configure logging for the renderer process.

    If *log_file* is given, logs to that file instead of stderr.
    """
    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(log_file, mode="w")
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [renderer] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="melonDS rendering emulator")
    parser.add_argument("--journal-file", required=True, help="Path to journal JSONL file")
    parser.add_argument("--rom", required=True, help="Path to NDS ROM file")
    parser.add_argument("--initial-state", default=None, help="Path to initial savestate")
    parser.add_argument("--port", type=int, default=18091, help="HLS stream HTTP port")
    parser.add_argument("--frame-file", required=True, help="Path to atomic frame-position file")
    parser.add_argument("--server-pid", type=int, default=None, help="PID of the MCP server (for liveness detection)")
    parser.add_argument("--record-dir", default=None, help="Directory for recording output (enables recording)")
    parser.add_argument("--record-name", default="unnamed", help="Name for the recording session")
    parser.add_argument("--log-file", default=None, help="Path to renderer log file (default: stderr)")
    return parser.parse_args()


def _write_frame_position(frame_file: Path, emulator_frame: int, stream_frame: int) -> None:
    """Atomically write the renderer's current frame position for the main process."""
    tmp_path = frame_file.with_suffix(frame_file.suffix + ".tmp")
    payload = json.dumps({"emulator_frame": emulator_frame, "stream_frame": stream_frame})
    tmp_path.write_text(payload)
    os.replace(str(tmp_path), str(frame_file))


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_file)
    frame_file = Path(args.frame_file)

    logger.info(
        "Renderer starting: rom=%s port=%d frame_file=%s initial_state=%s server_pid=%s",
        args.rom, args.port, args.frame_file, args.initial_state, args.server_pid,
    )

    from .emulator import EmulatorState
    from .journal import JournalReader
    from .streamer import HLSStreamer

    # Initialize emulator
    holder = EmulatorState()
    holder.initialize()
    holder.load_rom(args.rom)

    # Load initial state if provided (syncs to main emulator's position)
    if args.initial_state:
        success = holder.emu.savestate_load(args.initial_state)
        if success:
            logger.info("Loaded initial state: %s", args.initial_state)
        else:
            logger.warning("Failed to load initial state: %s", args.initial_state)

    # Start HLS streamer — it registers on_each_cycle to capture frames
    streamer = HLSStreamer(holder, port=args.port, blocking=True)
    streamer.start()
    logger.info("HLS streamer started on port %d (blocking mode)", args.port)

    # Start session recorder if enabled
    recorder = None
    if args.record_dir:
        from .recorder import SessionRecorder
        recorder = SessionRecorder(Path(args.record_dir), name=args.record_name)
        recorder.start()
        streamer.set_recorder(recorder)
        logger.info("Session recorder started, output dir: %s, name: %s", args.record_dir, args.record_name)

    # Connect to journal file (tail-follow)
    reader = JournalReader(args.journal_file, server_pid=args.server_pid)
    reader.connect()
    logger.info("Journal reader connected, entering replay loop")

    # Handle SIGTERM so the recorder can finalize on kill
    def _sigterm_handler(signum, frame):
        logger.info("Renderer received SIGTERM")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Write initial frame position
    _write_frame_position(frame_file, holder.frame_count, streamer._rt_frames)

    # Main replay loop
    try:
        for entry in reader:
            entry_type = entry.get("type")

            if entry_type == "frames":
                count = entry.get("count", 1)
                buttons = entry.get("buttons")
                touch_x = entry.get("touch_x")
                touch_y = entry.get("touch_y")
                # Advance one frame at a time so every frame is fully
                # rendered for the HLS stream.  advance_frames() uses
                # skip_render on intermediate frames which produces stale
                # framebuffer data in the streamer's _on_cycle callback.
                for _ in range(count):
                    holder.advance_frame(buttons, touch_x, touch_y)
                holder._notify_frame_change()

            elif entry_type == "load_state":
                path = entry["path"]
                success = holder.emu.savestate_load(path)
                if success:
                    logger.info("Renderer loaded state: %s", path)
                else:
                    logger.warning("Renderer failed to load state: %s", path)
                holder._notify_frame_change()

            elif entry_type == "reset":
                holder.emu.reset()
                holder.frame_count = 0
                holder._notify_frame_change()
                logger.info("Renderer reset")

            elif entry_type == "commentary":
                if recorder is not None:
                    recorder.add_commentary(
                        entry["stream_time"],
                        entry["text"],
                        entry.get("style", "normal"),
                    )

            elif entry_type == "load_rom":
                rom_path = entry["rom_path"]
                logger.info("Renderer loading new ROM: %s", rom_path)
                streamer.stop()
                holder.load_rom(rom_path)
                streamer = HLSStreamer(holder, port=args.port, blocking=True)
                if recorder is not None:
                    streamer.set_recorder(recorder)
                streamer.start()
                logger.info("Renderer restarted streamer for new ROM (recorder continues)")

            elif entry_type == "sync":
                state_path = entry["state_path"]
                success = holder.emu.savestate_load(state_path)
                if success:
                    logger.info("Renderer synced to state: %s", state_path)
                else:
                    logger.warning("Renderer failed to sync state: %s", state_path)
                holder._notify_frame_change()

            elif entry_type == "shutdown":
                logger.info("Renderer received shutdown entry")
                break

            else:
                logger.warning("Unknown journal entry type: %s", entry_type)

            # Update frame position file after every journal entry
            _write_frame_position(frame_file, holder.frame_count, streamer._rt_frames)

    except StopIteration:
        logger.info("Journal ended (server exited or no more entries)")
    except Exception:
        logger.error("Renderer replay loop error", exc_info=True)
    finally:
        # Stop recorder before streamer so it can flush remaining frames
        if recorder is not None:
            recorder.stop()
        # Clean up frame position file
        frame_file.unlink(missing_ok=True)
        frame_file.with_suffix(frame_file.suffix + ".tmp").unlink(missing_ok=True)
        # Clean up journal file
        reader.cleanup()
        streamer.stop()
        logger.info("Renderer exiting")


if __name__ == "__main__":
    main()

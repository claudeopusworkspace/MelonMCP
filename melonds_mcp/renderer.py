"""Rendering emulator process — replays journal entries at real-time for HLS streaming.

Launched as a subprocess by the main MCP server:
    python -m melonds_mcp.renderer --journal-sock <path> --rom <path> \
        [--initial-state <path>] --port <port> --data-dir <path>

Initializes its own melonDS instance, attaches the HLS streamer, and
replays input journal entries from the main emulator. The streamer's
existing real-time throttle handles pacing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("melonds_mcp.renderer")

# Filename for the atomic frame-position file read by the main process.
_FRAME_FILE = ".renderer_frame"
_FRAME_FILE_TMP = ".renderer_frame.tmp"


def _setup_logging() -> None:
    """Configure logging for the renderer process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [renderer] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="melonDS rendering emulator")
    parser.add_argument("--journal-sock", required=True, help="Path to journal Unix socket")
    parser.add_argument("--rom", required=True, help="Path to NDS ROM file")
    parser.add_argument("--initial-state", default=None, help="Path to initial savestate")
    parser.add_argument("--port", type=int, default=8091, help="HLS stream HTTP port")
    parser.add_argument("--data-dir", required=True, help="Data directory for frame position file")
    parser.add_argument("--record-dir", default=None, help="Directory for recording output (enables recording)")
    return parser.parse_args()


def _write_frame_position(data_dir: Path, emulator_frame: int, stream_frame: int) -> None:
    """Atomically write the renderer's current frame position for the main process."""
    tmp_path = data_dir / _FRAME_FILE_TMP
    final_path = data_dir / _FRAME_FILE
    payload = json.dumps({"emulator_frame": emulator_frame, "stream_frame": stream_frame})
    tmp_path.write_text(payload)
    os.replace(str(tmp_path), str(final_path))


def main() -> None:
    _setup_logging()
    args = _parse_args()
    data_dir = Path(args.data_dir)

    logger.info(
        "Renderer starting: rom=%s port=%d data_dir=%s initial_state=%s",
        args.rom, args.port, args.data_dir, args.initial_state,
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
        recorder = SessionRecorder(Path(args.record_dir))
        recorder.start()
        streamer.set_recorder(recorder)
        logger.info("Session recorder started, output dir: %s", args.record_dir)

    # Connect to journal
    reader = JournalReader(args.journal_sock)
    retry_count = 0
    max_retries = 10
    while retry_count < max_retries:
        try:
            reader.connect()
            break
        except (ConnectionRefusedError, FileNotFoundError):
            retry_count += 1
            if retry_count >= max_retries:
                logger.error("Failed to connect to journal socket after %d retries", max_retries)
                streamer.stop()
                sys.exit(1)
            time.sleep(0.5)

    logger.info("Connected to journal, entering replay loop")

    # Write initial frame position
    _write_frame_position(data_dir, holder.frame_count, streamer._rt_frames)

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
                if recorder is not None:
                    recorder.stop()
                streamer.stop()
                holder.load_rom(rom_path)
                streamer = HLSStreamer(holder, port=args.port, blocking=True)
                if args.record_dir:
                    from .recorder import SessionRecorder
                    recorder = SessionRecorder(Path(args.record_dir))
                    recorder.start()
                    streamer.set_recorder(recorder)
                streamer.start()
                logger.info("Renderer restarted streamer for new ROM")

            elif entry_type == "sync":
                state_path = entry["state_path"]
                success = holder.emu.savestate_load(state_path)
                if success:
                    logger.info("Renderer synced to state: %s", state_path)
                else:
                    logger.warning("Renderer failed to sync state: %s", state_path)
                holder._notify_frame_change()

            elif entry_type == "shutdown":
                logger.info("Renderer received shutdown")
                break

            else:
                logger.warning("Unknown journal entry type: %s", entry_type)

            # Update frame position file after every journal entry
            _write_frame_position(data_dir, holder.frame_count, streamer._rt_frames)

    except StopIteration:
        logger.info("Journal socket closed — main emulator disconnected")
    except Exception:
        logger.error("Renderer replay loop error", exc_info=True)
    finally:
        # Stop recorder before streamer so it can flush remaining frames
        if recorder is not None:
            recorder.stop()
        # Clean up frame position file
        frame_file = data_dir / _FRAME_FILE
        frame_file.unlink(missing_ok=True)
        (data_dir / _FRAME_FILE_TMP).unlink(missing_ok=True)
        reader.close()
        streamer.stop()
        logger.info("Renderer exiting")


if __name__ == "__main__":
    main()

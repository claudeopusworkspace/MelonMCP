"""Rendering emulator process — replays journal entries at real-time for HLS streaming.

Launched as a subprocess by the main MCP server:
    python -m melonds_mcp.renderer --journal-sock <path> --rom <path> [--initial-state <path>] --port <port>

Initializes its own melonDS instance, attaches the HLS streamer, and
replays input journal entries from the main emulator. The streamer's
existing real-time throttle handles pacing.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

logger = logging.getLogger("melonds_mcp.renderer")


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
    return parser.parse_args()


def main() -> None:
    _setup_logging()
    args = _parse_args()

    logger.info(
        "Renderer starting: rom=%s port=%d initial_state=%s",
        args.rom, args.port, args.initial_state,
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
    streamer = HLSStreamer(holder, port=args.port)
    streamer.start()
    logger.info("HLS streamer started on port %d", args.port)

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

    # Main replay loop
    try:
        for entry in reader:
            entry_type = entry.get("type")

            if entry_type == "frames":
                count = entry.get("count", 1)
                buttons = entry.get("buttons")
                touch_x = entry.get("touch_x")
                touch_y = entry.get("touch_y")
                holder.advance_frames(count, buttons, touch_x, touch_y)

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

            elif entry_type == "load_rom":
                rom_path = entry["rom_path"]
                logger.info("Renderer loading new ROM: %s", rom_path)
                streamer.stop()
                holder.load_rom(rom_path)
                streamer = HLSStreamer(holder, port=args.port)
                streamer.start()
                logger.info("Renderer restarted streamer for new ROM")

            elif entry_type == "shutdown":
                logger.info("Renderer received shutdown")
                break

            else:
                logger.warning("Unknown journal entry type: %s", entry_type)

    except StopIteration:
        logger.info("Journal socket closed — main emulator disconnected")
    except Exception:
        logger.error("Renderer replay loop error", exc_info=True)
    finally:
        reader.close()
        streamer.stop()
        logger.info("Renderer exiting")


if __name__ == "__main__":
    main()

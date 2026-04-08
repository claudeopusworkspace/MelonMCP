"""Benchmark: advance_frames with and without GPU render skipping.

Loads a ROM, advances past boot, then times 1800-frame bulk advances
with skip_render enabled vs disabled.
"""

import sys
import time

sys.path.insert(0, ".")

from melonds_mcp.emulator import EmulatorState


ROM_PATH = "RenegadePlatinum.nds"
WARMUP_FRAMES = 60
BENCH_FRAMES = 1800
RUNS = 3


def bench_advance(holder: EmulatorState, label: str, use_skip: bool) -> None:
    """Run BENCH_FRAMES advance and report timing."""
    times = []
    for run in range(RUNS):
        # Save state before each run to keep consistent starting point
        holder.emu.savestate_save("/tmp/bench_state.mst")

        if use_skip:
            # Normal path — advance_frames uses skip_render internally
            t0 = time.monotonic()
            holder.advance_frames(BENCH_FRAMES)
            elapsed = time.monotonic() - t0
        else:
            # Force no-skip: set skip_render(False) and advance one at a time
            holder.emu.set_skip_render(False)
            t0 = time.monotonic()
            for _ in range(BENCH_FRAMES):
                holder.advance_frame()
            elapsed = time.monotonic() - t0

        fps = BENCH_FRAMES / elapsed
        times.append(elapsed)
        print(f"  Run {run + 1}: {elapsed:.3f}s  ({fps:.0f} FPS)")

        # Restore state for next run
        holder.emu.savestate_load("/tmp/bench_state.mst")

    avg = sum(times) / len(times)
    avg_fps = BENCH_FRAMES / avg
    print(f"  Average: {avg:.3f}s  ({avg_fps:.0f} FPS)\n")
    return avg_fps


def main() -> None:
    print(f"=== Render Skip Benchmark ===")
    print(f"ROM: {ROM_PATH}")
    print(f"Frames per run: {BENCH_FRAMES}")
    print(f"Runs: {RUNS}\n")

    holder = EmulatorState()
    holder.initialize()
    holder.load_rom(ROM_PATH)

    # Warm up past boot screen
    print(f"Warming up ({WARMUP_FRAMES} frames)...")
    holder.advance_frames(WARMUP_FRAMES)
    print()

    # Benchmark WITHOUT skip render (all frames rendered)
    print(f"[WITHOUT render skipping] ({BENCH_FRAMES} frames, all rendered):")
    fps_no_skip = bench_advance(holder, "no-skip", use_skip=False)

    # Benchmark WITH skip render (only last frame rendered)
    print(f"[WITH render skipping] ({BENCH_FRAMES} frames, only last rendered):")
    fps_skip = bench_advance(holder, "skip", use_skip=True)

    # Summary
    speedup = fps_skip / fps_no_skip if fps_no_skip > 0 else 0
    print("=== Summary ===")
    print(f"Without skip: {fps_no_skip:.0f} FPS")
    print(f"With skip:    {fps_skip:.0f} FPS")
    print(f"Speedup:      {speedup:.2f}x")


if __name__ == "__main__":
    main()

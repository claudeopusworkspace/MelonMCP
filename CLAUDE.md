# MelonMCP — melonDS MCP Server

Port of the DeSmuME MCP server to melonDS for JIT-enabled DS emulation.

## Status

**Core port complete.** The C shim, ctypes wrapper, and all Python layers have been ported. JIT is confirmed working. 133 unit tests passing.

### What's done
- `shim/melonds_shim.cpp` — extern "C" wrapper (lifecycle, display, input, savestates, memory, audio, save data, JIT)
- `shim/platform_stubs.cpp` — Platform.h implementations (file I/O, threading, logging, save callbacks, no-op multimedia stubs)
- `shim/CMakeLists.txt` — builds `libmelonds.so` linking melonDS core as static lib
- `melonds_mcp/` — full Python package (14 modules: libmelonds, emulator, server, bridge, client, journal, renderer, streamer, viewer, constants, settings, __main__)
- `tests/` — 133 tests passing (constants, checkpoints, macros, watches, advance_frames_until)
- Build automation (`scripts/build_libmelonds.sh`)
- Streaming rework: unified viewer, commentary overlay, stream-paced tool calls

### What's not done yet
- End-to-end testing with a real ROM
- Investigate Woj's previously observed image artifacting

## Motivation

DeSmuME's x86 JIT is disabled by default and documented as buggy (unmaintained since ~2014, with known Pokemon-specific crashes). melonDS has a mature, stable JIT recompiler that's on by default for x64, offering substantial performance gains.

## Architecture

```
MCP tools (server.py)  →  EmulatorState (emulator.py)  →  ctypes wrapper (libmelonds.py)  →  libmelonds.so
Bridge (bridge.py)  ↗       ↑ Journal (journal.py) → Renderer (renderer.py) → HLSStreamer (streamer.py)
                                                        ↑ position file ↓
Viewer (viewer.py, port 8090): / = HLS video + commentary, /snapshots = auto-updating screenshots + history browse
RecordingServer (recording_server.py, port 8091): /recordings = list + playback (always-on, started by ~/.profile)
```

**Streaming architecture:** The main emulator processes MCP commands at full speed and journals actions to a renderer subprocess via Unix socket. The renderer replays frames one-at-a-time (no render skipping) and pipes them through ffmpeg to produce HLS segments (port 18091). The unified viewer page (port 8090) loads HLS video cross-origin and receives commentary events via SSE. Frame-advancing tools block until the renderer catches up to within 30 seconds. If the renderer falls 60+ seconds behind, a savestate resync is triggered automatically.

**Recording browser:** A standalone HTTP server on port 8091 serves the recording list and playback pages. It reads recordings off disk and runs independently of the emulator — auto-started by `~/.profile` so recordings are always browsable. The viewer (8090) redirects `/recordings*` to this server.

Sources are configured via `recording_sources.json` at project root (gitignored — it's local/machine-specific). Each entry has `slug` (URL segment), `label` (section header), and `path` (absolute directory). URLs are `/recordings/<slug>/<stem>[.mp4|.json]`, and the list page groups entries by source. If the config file is absent, the server falls back to single-source mode at `$PROJECT_DIR/recordings` with unprefixed URLs (`/recordings/<stem>`).

The C shim (`shim/melonds_shim.cpp`) wraps the melonDS `NDS` C++ class as a flat C API. Platform callbacks (`shim/platform_stubs.cpp`) provide file I/O, threading, and save data persistence.

## Build

```bash
# Initialize melonDS submodule (if not present):
git submodule update --init

# Build libmelonds.so:
./scripts/build_libmelonds.sh

# Setup Python env:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run tests:
.venv/bin/python -m pytest tests/ -v
```

## Key Differences from DeSmuME

- **JIT enabled by default** — the whole reason for this port
- **No SDL dependency** — melonDS core doesn't need SDL
- **Audio at 48kHz** (was 44.1kHz) — cleaner 800 samples/frame
- **BGRA framebuffer** — SoftRenderer outputs BGRA, shim converts to RGB24
- **Input bitmask inversion** — melonDS uses 1=released (DS hardware convention), shim bridges to 1=pressed (Python convention)
- **Memory savestates** — melonDS savestates are in-memory buffers, shim handles file I/O
- **GPU render skipping** — our fork adds `GPU.SkipRender` flag; `advance_frames()` skips pixel rendering on intermediate frames, only rendering the final frame
- **No movie support** — dropped from tools (melonDS has no built-in movie recording)
- **No volume/language control** — dropped from tools
- **Save data via Platform callback** — `WriteNDSSave` auto-writes `.sav` files

## Run as MCP Server

```bash
.venv/bin/python -m melonds_mcp
```

Or configure in your MCP client's settings.

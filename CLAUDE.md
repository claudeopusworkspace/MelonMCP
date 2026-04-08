# MelonMCP — melonDS MCP Server

Port of the DeSmuME MCP server to melonDS for JIT-enabled DS emulation.

## Status

**Core port complete.** The C shim, ctypes wrapper, and all Python layers have been ported. JIT is confirmed working. 76 unit tests passing.

### What's done
- `shim/melonds_shim.cpp` — extern "C" wrapper (lifecycle, display, input, savestates, memory, audio, save data, JIT)
- `shim/platform_stubs.cpp` — Platform.h implementations (file I/O, threading, logging, save callbacks, no-op multimedia stubs)
- `shim/CMakeLists.txt` — builds `libmelonds.so` linking melonDS core as static lib
- `melonds_mcp/` — full Python package (14 modules: libmelonds, emulator, server, bridge, client, journal, renderer, streamer, viewer, constants, settings, __main__)
- `tests/` — 76 tests passing (constants, checkpoints, macros, watches)
- Build automation (`scripts/build_libmelonds.sh`)

### What's not done yet
- End-to-end testing with a real ROM
- HLS streaming verification
- Investigate Woj's previously observed image artifacting

## Motivation

DeSmuME's x86 JIT is disabled by default and documented as buggy (unmaintained since ~2014, with known Pokemon-specific crashes). melonDS has a mature, stable JIT recompiler that's on by default for x64, offering substantial performance gains.

## Architecture

```
MCP tools (server.py)  →  EmulatorState (emulator.py)  →  ctypes wrapper (libmelonds.py)  →  libmelonds.so
Bridge (bridge.py)  ↗       ↑ Journal (journal.py) → Renderer (renderer.py)
```

The C shim (`shim/melonds_shim.cpp`) wraps the melonDS `NDS` C++ class as a flat C API. Platform callbacks (`shim/platform_stubs.cpp`) provide file I/O, threading, and save data persistence.

## Build

```bash
# Clone our melonDS fork (if not present):
git clone -b feat/skip-render https://github.com/claudeopusworkspace/melonDS.git melonds-src

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

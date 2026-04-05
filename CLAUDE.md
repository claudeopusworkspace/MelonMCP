# MelonMCP — melonDS MCP Server

Port of the DeSmuME MCP server to melonDS for JIT-enabled DS emulation.

## Status

**Not yet started.** This project exists to hold research and planning for a future port of `/workspace/DesmumeMCP` to use melonDS as its emulation backend. Development on DesmumeMCP should be paused during active work here to keep the port in sync.

## Motivation

DeSmuME's x86 JIT is disabled by default and documented as buggy (unmaintained since ~2014, with known Pokemon-specific crashes). melonDS has a mature, stable JIT recompiler that's on by default for x64, offering substantial performance gains.

## Architecture Plan

The DeSmuME MCP stack is layered:

```
MCP tools (server.py)  →  EmulatorState (emulator.py)  →  ctypes wrapper (libdesmume.py)  →  libdesmume.so
Bridge (bridge.py)  ↗       ↑ Journal (journal.py) → Renderer (renderer.py)
```

Only the bottom layer needs replacing:
1. **C shim** — `extern "C"` wrapper around melonDS's `NDS` C++ class → `libmelonds.so`
2. **Python ctypes wrapper** — `libmelonds.py` replacing `libdesmume.py`
3. **Everything above** — `emulator.py`, `server.py`, `bridge.py`, `journal.py`, `renderer.py`, `streamer.py`, `viewer.py` — ports with minimal changes

## melonDS Core API (NDS class, src/NDS.h)

Key methods that map to our interface:

| melonDS (NDS class) | DeSmuME equivalent | Notes |
|---|---|---|
| `NDS(NDSArgs&&)` | `desmume_init()` | Constructor, takes config args |
| `Reset()` | `desmume_reset()` | |
| `Start()` / `Stop()` | `desmume_resume()` / `desmume_pause()` | |
| `RunFrame()` | `desmume_cycle()` | Single frame advance |
| `SetKeyMask(u32)` | `desmume_input_keypad_update()` | |
| `TouchScreen(x, y)` | `desmume_input_set_touch_pos()` | |
| `ReleaseScreen()` | `desmume_input_release_touch()` | |
| `ARM9Read8/16/32(addr)` | `desmume_memory_read_byte/short/long()` | |
| `ARM9Write8/16/32(addr, val)` | `desmume_memory_write_byte/short/long()` | |
| `DoSavestate(Savestate*)` | `desmume_savestate_save/load()` | Single method, bidirectional |
| `IsJITEnabled()` / `SetJITArgs()` | N/A (no DeSmuME equivalent) | JIT control |

## Build Requirements

```bash
# melonDS core only (no Qt GUI):
cmake -B build -DBUILD_QT_SDL=OFF -DENABLE_JIT=ON
cmake --build build
```

## Platform.h Stubs

melonDS requires ~12 Platform.h callbacks for headless operation:
- File I/O (open, read, write local files for BIOS/firmware/saves)
- Logging
- Threading primitives
- Most multimedia callbacks (camera, mic, networking) can be no-ops

## Known Concerns

- Woj previously observed image artifacting in melonDS GUI — needs investigation (may be renderer-specific, not core issue)
- melonDS uses OpenGL/Vulkan for its software renderer output in GUI mode; headless raw framebuffer access needs verification
- GPLv3 license (same as DeSmuME)

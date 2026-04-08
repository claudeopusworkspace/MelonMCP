/*
 * melonds_shim.cpp — extern "C" wrapper around melonDS NDS class
 *
 * Provides a flat C API for Python ctypes to call. Holds a single global
 * NDS instance with SoftRenderer for headless operation.
 *
 * Copyright (C) 2026 MelonMCP contributors
 * Licensed under GPLv3 (same as melonDS)
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <memory>
#include <fstream>
#include <filesystem>

#include "NDS.h"
#include "NDSCart.h"
#include "GPU.h"
#include "GPU_Soft.h"
#include "SPU.h"
#include "Savestate.h"
#include "Args.h"
#include "Platform.h"

using namespace melonDS;

// ── Global state ──

static NDS* g_nds = nullptr;
static bool g_running = false;
std::string g_save_path;          // set by melonds_open, used by Platform::WriteNDSSave
static std::string g_slot_prefix; // for slot-based savestates

// ── Helpers ──

static std::vector<u8> read_file(const char* path)
{
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) return {};
    auto size = f.tellg();
    f.seekg(0);
    std::vector<u8> buf(size);
    f.read(reinterpret_cast<char*>(buf.data()), size);
    return buf;
}

static bool write_file(const char* path, const void* data, size_t len)
{
    std::ofstream f(path, std::ios::binary);
    if (!f) return false;
    f.write(reinterpret_cast<const char*>(data), len);
    return f.good();
}

static std::string slot_path(int index)
{
    return g_slot_prefix + ".slot" + std::to_string(index) + ".mst";
}

// ── Lifecycle ──

extern "C" {

int melonds_init(void)
{
    if (g_nds) return 0;

    try {
        NDSArgs args {};
        // defaults: FreeBIOS, JIT enabled, generated firmware
        g_nds = new NDS(std::move(args));
        g_nds->Reset();

        // set up software renderer for headless framebuffer access
        auto renderer = std::make_unique<SoftRenderer>(*g_nds);
        g_nds->SetRenderer(std::move(renderer));

        g_nds->SPU.InitOutput();
        g_running = false;
        return 0;
    } catch (...) {
        return -1;
    }
}

void melonds_free(void)
{
    if (g_nds) {
        g_nds->Stop();
        delete g_nds;
        g_nds = nullptr;
    }
    g_running = false;
    g_save_path.clear();
    g_slot_prefix.clear();
}

int melonds_open(const char* filename)
{
    if (!g_nds || !filename) return 0;

    // read ROM file
    auto romdata = read_file(filename);
    if (romdata.empty()) {
        fprintf(stderr, "melonds_shim: failed to read ROM: %s\n", filename);
        return 0;
    }

    // parse ROM
    auto cart = NDSCart::ParseROM(romdata.data(), (u32)romdata.size());
    if (!cart) {
        fprintf(stderr, "melonds_shim: failed to parse ROM: %s\n", filename);
        return 0;
    }

    // derive save path from ROM path
    std::filesystem::path rom_path(filename);
    g_save_path = (rom_path.parent_path() / rom_path.stem()).string() + ".sav";
    g_slot_prefix = (rom_path.parent_path() / rom_path.stem()).string();

    // load existing save data if present
    std::optional<NDSCart::NDSCartArgs> cart_args;
    if (std::filesystem::exists(g_save_path)) {
        auto sav = read_file(g_save_path.c_str());
        if (!sav.empty()) {
            NDSCart::NDSCartArgs ca;
            ca.SRAM = std::make_unique<u8[]>(sav.size());
            memcpy(ca.SRAM.get(), sav.data(), sav.size());
            ca.SRAMLength = (u32)sav.size();
            cart_args = std::move(ca);

            // re-parse with save data
            cart = NDSCart::ParseROM(romdata.data(), (u32)romdata.size(),
                                     nullptr, std::move(cart_args));
            if (!cart) {
                fprintf(stderr, "melonds_shim: failed to parse ROM with save: %s\n", filename);
                return 0;
            }
        }
    }

    // insert cart and boot
    g_nds->SetNDSCart(std::move(cart));
    g_nds->Reset();

    std::string romname = rom_path.filename().string();
    if (g_nds->NeedsDirectBoot()) {
        g_nds->SetupDirectBoot(romname);
    }

    g_nds->Start();
    g_running = true;
    return 1;
}

void melonds_pause(void)
{
    g_running = false;
}

void melonds_resume(void)
{
    if (!g_nds) return;
    g_running = true;
    g_nds->Start();
}

void melonds_reset(void)
{
    if (!g_nds) return;
    g_nds->Reset();
    if (g_nds->NeedsDirectBoot()) {
        g_nds->SetupDirectBoot("");
    }
    melonds_resume();
}

int melonds_running(void)
{
    return g_running ? 1 : 0;
}

void melonds_cycle(void)
{
    if (!g_nds || !g_running) return;
    g_nds->RunFrame();
    // BufferAudio is called internally by RunFrame
}

// ── Display ──
// Output: RGB24, 256x384 (top + bottom screens), 294912 bytes total

void melonds_screenshot(char* screenshot_buffer)
{
    if (!g_nds || !screenshot_buffer) return;

    void* top_ptr = nullptr;
    void* bot_ptr = nullptr;
    bool ok = g_nds->GPU.GetFramebuffers(&top_ptr, &bot_ptr);
    if (!ok || !top_ptr || !bot_ptr) {
        memset(screenshot_buffer, 0, 256 * 384 * 3);
        return;
    }

    u32* top = (u32*)top_ptr;
    u32* bot = (u32*)bot_ptr;

    // SoftRenderer outputs BGRA: byte0=B, byte1=G, byte2=R, byte3=A
    // We convert to RGB24
    unsigned char* out = (unsigned char*)screenshot_buffer;
    for (int i = 0; i < 256 * 192; i++) {
        u32 px = top[i];
        out[i * 3 + 0] = (px >> 16) & 0xFF; // R
        out[i * 3 + 1] = (px >> 8) & 0xFF;  // G
        out[i * 3 + 2] = px & 0xFF;         // B
    }
    int off = 256 * 192 * 3;
    for (int i = 0; i < 256 * 192; i++) {
        u32 px = bot[i];
        out[off + i * 3 + 0] = (px >> 16) & 0xFF; // R
        out[off + i * 3 + 1] = (px >> 8) & 0xFF;  // G
        out[off + i * 3 + 2] = px & 0xFF;         // B
    }
}

// ── Input ──
// DeSmuME/Python convention: 1 = pressed
// melonDS convention: 1 = released (DS hardware KEYINPUT register)
// The shim bridges: invert the mask for SetKeyMask

void melonds_input_keypad_update(unsigned short keys)
{
    if (!g_nds) return;
    // invert: Python sends 1=pressed, melonDS wants 1=released
    u32 mask = (~(u32)keys) & 0xFFF;
    g_nds->SetKeyMask(mask);
}

unsigned short melonds_input_keypad_get(void)
{
    if (!g_nds) return 0;
    // KeyInput: bits 0-9 = standard keys (1=released), bits 16-17 = X/Y (1=released)
    u32 ki = g_nds->KeyInput;
    u32 lo = ki & 0x3FF;
    u32 hi = (ki >> 16) & 0x3;
    u32 mask = lo | (hi << 10);
    // invert back to 1=pressed for Python
    return (unsigned short)((~mask) & 0xFFF);
}

void melonds_input_set_touch_pos(unsigned short x, unsigned short y)
{
    if (!g_nds) return;
    g_nds->TouchScreen(x, y);
}

void melonds_input_release_touch(void)
{
    if (!g_nds) return;
    g_nds->ReleaseScreen();
}

// ── Savestates ──

int melonds_savestate_save(const char* filename)
{
    if (!g_nds || !filename) return 0;

    Savestate state;
    g_nds->DoSavestate(&state);
    state.Finish();

    if (state.Error) return 0;

    return write_file(filename, state.Buffer(), state.Length()) ? 1 : 0;
}

int melonds_savestate_load(const char* filename)
{
    if (!g_nds || !filename) return 0;

    auto buf = read_file(filename);
    if (buf.empty()) return 0;

    Savestate state(buf.data(), (u32)buf.size(), false);
    g_nds->DoSavestate(&state);

    if (state.Error) return 0;

    return 1;
}

void melonds_savestate_slot_save(int index)
{
    if (g_slot_prefix.empty()) return;
    melonds_savestate_save(slot_path(index).c_str());
}

void melonds_savestate_slot_load(int index)
{
    if (g_slot_prefix.empty()) return;
    melonds_savestate_load(slot_path(index).c_str());
}

int melonds_savestate_slot_exists(int index)
{
    if (g_slot_prefix.empty()) return 0;
    return std::filesystem::exists(slot_path(index)) ? 1 : 0;
}

// ── Memory ──

unsigned char melonds_memory_read_byte(int address)
{
    if (!g_nds) return 0;
    return g_nds->ARM9Read8((u32)address);
}

signed char melonds_memory_read_byte_signed(int address)
{
    return (signed char)melonds_memory_read_byte(address);
}

unsigned short melonds_memory_read_short(int address)
{
    if (!g_nds) return 0;
    return g_nds->ARM9Read16((u32)address);
}

signed short melonds_memory_read_short_signed(int address)
{
    return (signed short)melonds_memory_read_short(address);
}

unsigned int melonds_memory_read_long(int address)
{
    if (!g_nds) return 0;
    return g_nds->ARM9Read32((u32)address);
}

signed int melonds_memory_read_long_signed(int address)
{
    return (signed int)melonds_memory_read_long(address);
}

void melonds_memory_write_byte(int address, unsigned char value)
{
    if (!g_nds) return;
    g_nds->ARM9Write8((u32)address, value);
}

void melonds_memory_write_short(int address, unsigned short value)
{
    if (!g_nds) return;
    g_nds->ARM9Write16((u32)address, value);
}

void melonds_memory_write_long(int address, unsigned int value)
{
    if (!g_nds) return;
    g_nds->ARM9Write32((u32)address, value);
}

// ── Audio ──

void melonds_audio_enable(void)
{
    if (!g_nds) return;
    g_nds->SPU.InitOutput();
}

void melonds_audio_disable(void)
{
    if (!g_nds) return;
    g_nds->SPU.DrainOutput();
}

unsigned int melonds_audio_samples_available(void)
{
    if (!g_nds) return 0;
    return (unsigned int)g_nds->SPU.GetOutputSize();
}

unsigned int melonds_audio_read(signed short* output, unsigned int max_frames)
{
    if (!g_nds || !output) return 0;
    int read = g_nds->SPU.ReadOutput(output, (int)max_frames);
    return (unsigned int)(read > 0 ? read : 0);
}

// ── Save data (battery backup) ──

int melonds_backup_import(const char* filename)
{
    if (!g_nds || !filename) return 0;

    auto sav = read_file(filename);
    if (sav.empty()) return 0;

    g_nds->SetNDSSave(sav.data(), (u32)sav.size());
    return 1;
}

int melonds_backup_export(const char* filename)
{
    if (!g_nds || !filename) return 0;

    const u8* data = g_nds->GetNDSSave();
    u32 len = g_nds->GetNDSSaveLength();
    if (!data || len == 0) return 0;

    return write_file(filename, data, len) ? 1 : 0;
}

// ── Render skipping ──

void melonds_set_skip_render(int skip)
{
    if (!g_nds) return;
    g_nds->GPU.SkipRender = (skip != 0);
}

int melonds_get_skip_render(void)
{
    if (!g_nds) return 0;
    return g_nds->GPU.SkipRender ? 1 : 0;
}

// ── JIT ──

int melonds_jit_enabled(void)
{
    if (!g_nds) return 0;
    return g_nds->IsJITEnabled() ? 1 : 0;
}

} // extern "C"

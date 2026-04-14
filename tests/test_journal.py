"""Tests for the file-based journal (JournalWriter + JournalReader)."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from melonds_mcp.journal import JournalReader, JournalWriter


@pytest.fixture
def journal_path(tmp_path):
    return str(tmp_path / "test.journal.jsonl")


# ── JournalWriter ──


class TestJournalWriter:
    def test_start_creates_file(self, journal_path):
        w = JournalWriter(journal_path)
        w.start()
        assert os.path.exists(journal_path)
        w.stop()

    def test_write_frames_appends_line(self, journal_path):
        w = JournalWriter(journal_path)
        w.start()
        w.write_frames(count=10, buttons=["a"], touch_x=None, touch_y=None)
        w.stop()

        lines = open(journal_path).readlines()
        # shutdown entry + frames entry
        assert len(lines) == 2
        entry = json.loads(lines[0])
        assert entry["type"] == "frames"
        assert entry["count"] == 10
        assert entry["buttons"] == ["a"]

    def test_stop_writes_shutdown(self, journal_path):
        w = JournalWriter(journal_path)
        w.start()
        w.stop()

        lines = open(journal_path).readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "shutdown"

    def test_write_shutdown_then_stop_produces_single_shutdown(self, journal_path):
        w = JournalWriter(journal_path)
        w.start()
        w.write_shutdown()
        w.stop()

        lines = open(journal_path).readlines()
        shutdown_entries = [json.loads(l) for l in lines if json.loads(l)["type"] == "shutdown"]
        assert len(shutdown_entries) == 1

    def test_all_entry_types(self, journal_path):
        w = JournalWriter(journal_path)
        w.start()
        w.write_frames(count=5)
        w.write_load_state(path="/tmp/state.dst")
        w.write_reset()
        w.write_load_rom(rom_path="/tmp/rom.nds")
        w.write_sync(state_path="/tmp/sync.dst")
        w.write_commentary(stream_time=1.5, text="hello", style="normal")
        w.write_shutdown()
        w.stop()

        lines = open(journal_path).readlines()
        types = [json.loads(l)["type"] for l in lines]
        assert types == [
            "frames", "load_state", "reset", "load_rom",
            "sync", "commentary", "shutdown",
        ]

    def test_thread_safety(self, journal_path):
        """Multiple threads writing concurrently should not corrupt the file."""
        w = JournalWriter(journal_path)
        w.start()

        errors = []

        def writer(thread_id):
            try:
                for i in range(50):
                    w.write_frames(count=i, buttons=[f"t{thread_id}"])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.stop()

        assert not errors
        lines = open(journal_path).readlines()
        # 4 threads * 50 writes + 1 shutdown
        assert len(lines) == 201
        # Every line should be valid JSON
        for line in lines:
            json.loads(line)


# ── JournalReader ──


class TestJournalReader:
    def test_reads_all_entries(self, journal_path):
        # Write entries first
        w = JournalWriter(journal_path)
        w.start()
        for i in range(5):
            w.write_frames(count=i)
        w.write_shutdown()
        w.stop()

        # Read them back
        r = JournalReader(journal_path)
        r.connect()
        entries = list(r)
        r.close()

        assert len(entries) == 6  # 5 frames + 1 shutdown
        assert entries[-1]["type"] == "shutdown"

    def test_tail_follow_sees_new_entries(self, journal_path):
        """Reader should see entries written after it starts reading."""
        w = JournalWriter(journal_path)
        w.start()

        r = JournalReader(journal_path)
        r.connect()

        # Write in a background thread with a small delay
        results = []

        def read_entries():
            for entry in r:
                results.append(entry)

        reader_thread = threading.Thread(target=read_entries, daemon=True)
        reader_thread.start()

        # Give the reader a moment to start polling
        time.sleep(0.1)

        # Write entries with delays
        w.write_frames(count=1)
        time.sleep(0.1)
        w.write_frames(count=2)
        time.sleep(0.1)
        w.write_shutdown()
        w.stop()

        reader_thread.join(timeout=5.0)
        assert not reader_thread.is_alive()

        assert len(results) == 3  # 2 frames + 1 shutdown
        assert results[0]["count"] == 1
        assert results[1]["count"] == 2
        assert results[2]["type"] == "shutdown"

    def test_detects_dead_server_pid(self, journal_path):
        """Reader should stop when server PID is dead and no new data arrives."""
        w = JournalWriter(journal_path)
        w.start()
        w.write_frames(count=1)
        # Don't write shutdown — simulate server crash
        w._running = False
        w._file.close()
        w._file = None

        # Use a definitely-dead PID
        dead_pid = 99999999
        r = JournalReader(journal_path, server_pid=dead_pid)
        r.connect()

        entries = list(r)
        r.close()

        assert len(entries) == 1
        assert entries[0]["type"] == "frames"

    def test_cleanup_removes_file(self, journal_path):
        w = JournalWriter(journal_path)
        w.start()
        w.stop()

        r = JournalReader(journal_path)
        r.connect()
        r.cleanup()

        assert not os.path.exists(journal_path)

    def test_connect_retries_on_missing_file(self, journal_path):
        """Reader should retry if the file doesn't exist yet."""
        r = JournalReader(journal_path, server_pid=None)

        # Create the file after a delay
        def delayed_create():
            time.sleep(0.3)
            open(journal_path, "w").close()

        t = threading.Thread(target=delayed_create, daemon=True)
        t.start()

        r.connect()  # Should succeed after retry
        r.close()
        t.join()

"""Tests for the automatic checkpoint (rewind) system."""

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from melonds_mcp.emulator import Checkpoint, CheckpointManager, EmulatorState


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path / "checkpoints"


@pytest.fixture
def mgr(tmp_dir):
    return CheckpointManager(tmp_dir)


def _make_mock_emu():
    """Create a mock emulator whose savestate_save writes a dummy file."""
    emu = MagicMock()

    def fake_save(path):
        Path(path).write_bytes(b"fake-savestate")
        return True

    emu.savestate_save.side_effect = fake_save
    emu.savestate_load.return_value = True
    return emu


@pytest.fixture
def mock_emu():
    return _make_mock_emu()


@pytest.fixture
def holder(tmp_path):
    h = EmulatorState(data_dir=tmp_path)
    h.emu = _make_mock_emu()
    h.is_initialized = True
    h.is_rom_loaded = True
    h.frame_count = 100
    return h


class TestCheckpointManager:
    def test_create_checkpoint(self, mgr, mock_emu):
        cp = mgr.create(mock_emu, 100, "press: a")
        assert isinstance(cp, Checkpoint)
        assert len(cp.id) == 8
        assert cp.frame == 100
        assert cp.action == "press: a"
        assert cp.path.endswith(".mst")
        mock_emu.savestate_save.assert_called_once_with(cp.path)

    def test_list_recent_default(self, mgr, mock_emu):
        for i in range(5):
            mgr.create(mock_emu, i * 10, f"press: {i}")
        result = mgr.list_recent()
        assert len(result) == 5

    def test_list_recent_with_limit(self, mgr, mock_emu):
        for i in range(10):
            mgr.create(mock_emu, i * 10, f"press: {i}")
        result = mgr.list_recent(3)
        assert len(result) == 3
        # Should be the 3 most recent (oldest first)
        assert result[0].action == "press: 7"
        assert result[2].action == "press: 9"

    def test_total_count(self, mgr, mock_emu):
        assert mgr.total_count == 0
        mgr.create(mock_emu, 0, "press: a")
        assert mgr.total_count == 1
        mgr.create(mock_emu, 10, "press: b")
        assert mgr.total_count == 2

    def test_get_existing(self, mgr, mock_emu):
        cp = mgr.create(mock_emu, 100, "press: a")
        found = mgr.get(cp.id)
        assert found is cp

    def test_get_missing(self, mgr):
        assert mgr.get("nonexist") is None

    def test_unique_ids(self, mgr, mock_emu):
        ids = set()
        for i in range(50):
            cp = mgr.create(mock_emu, i, f"press: {i}")
            ids.add(cp.id)
        assert len(ids) == 50

    def test_ring_buffer_evicts_oldest(self, tmp_dir, mock_emu):
        from collections import deque

        mgr = CheckpointManager(tmp_dir)
        # Override max for testing
        mgr._ring = deque(maxlen=3)

        cp1 = mgr.create(mock_emu, 10, "press: a")
        cp2 = mgr.create(mock_emu, 20, "press: b")
        cp3 = mgr.create(mock_emu, 30, "press: x")

        assert Path(cp1.path).exists()  # file created by mock

        cp4 = mgr.create(mock_emu, 40, "press: y")

        # cp1 should have been evicted and its file deleted
        assert not Path(cp1.path).exists()
        assert mgr.total_count == 3
        assert mgr.get(cp1.id) is None
        assert mgr.get(cp4.id) is cp4

    def test_revert_restores_state(self, holder):
        mgr = holder.checkpoints
        emu = holder.emu

        holder.frame_count = 100
        cp1 = mgr.create(emu, holder.frame_count, "press: a")
        holder.frame_count = 110

        cp2 = mgr.create(emu, holder.frame_count, "press: b")
        holder.frame_count = 120

        cp3 = mgr.create(emu, holder.frame_count, "press: x")
        holder.frame_count = 130

        # Revert to cp1
        result = mgr.revert(holder, cp1.id)
        assert result is cp1
        assert holder.frame_count == 100
        emu.savestate_load.assert_called_with(cp1.path)

    def test_revert_discards_later_checkpoints(self, holder):
        mgr = holder.checkpoints
        emu = holder.emu

        holder.frame_count = 100
        cp1 = mgr.create(emu, holder.frame_count, "press: a")
        holder.frame_count = 110

        cp2 = mgr.create(emu, holder.frame_count, "press: b")
        # Write dummy files so unlink can work
        Path(cp2.path).write_bytes(b"dummy")
        holder.frame_count = 120

        cp3 = mgr.create(emu, holder.frame_count, "press: x")
        Path(cp3.path).write_bytes(b"dummy")
        holder.frame_count = 130

        mgr.revert(holder, cp1.id)

        assert mgr.total_count == 1
        assert mgr.get(cp1.id) is not None
        assert mgr.get(cp2.id) is None
        assert mgr.get(cp3.id) is None
        # Files for cp2 and cp3 should be deleted
        assert not Path(cp2.path).exists()
        assert not Path(cp3.path).exists()

    def test_revert_missing_id_raises(self, holder):
        with pytest.raises(ValueError, match="Checkpoint not found"):
            holder.checkpoints.revert(holder, "bad_id_0")

    def test_revert_missing_file_raises(self, holder):
        mgr = holder.checkpoints
        cp = mgr.create(holder.emu, 100, "press: a")
        # Delete the savestate file
        Path(cp.path).unlink(missing_ok=True)
        with pytest.raises(FileNotFoundError, match="Checkpoint file missing"):
            mgr.revert(holder, cp.id)

    def test_promote_copies_file(self, mgr, mock_emu, tmp_path):
        cp = mgr.create(mock_emu, 100, "press: a")
        dest = str(tmp_path / "savestates" / "my_save.mst")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)

        result = mgr.promote(cp.id, dest)
        assert result is cp
        assert Path(dest).exists()
        assert Path(dest).read_bytes() == Path(cp.path).read_bytes()
        # Original checkpoint file still exists
        assert Path(cp.path).exists()
        # Checkpoint ring unchanged
        assert mgr.total_count == 1
        assert mgr.get(cp.id) is cp

    def test_promote_missing_id_raises(self, mgr):
        with pytest.raises(ValueError, match="Checkpoint not found"):
            mgr.promote("bad_id_0", "/tmp/doesnt_matter.mst")

    def test_promote_missing_file_raises(self, mgr, mock_emu, tmp_path):
        cp = mgr.create(mock_emu, 100, "press: a")
        Path(cp.path).unlink()
        with pytest.raises(FileNotFoundError, match="Checkpoint file missing"):
            mgr.promote(cp.id, str(tmp_path / "out.mst"))

    def test_clear(self, mgr, mock_emu):
        cp1 = mgr.create(mock_emu, 10, "press: a")
        cp2 = mgr.create(mock_emu, 20, "press: b")
        # Write dummy files
        Path(cp1.path).write_bytes(b"dummy")
        Path(cp2.path).write_bytes(b"dummy")

        count = mgr.clear()
        assert count == 2
        assert mgr.total_count == 0
        assert not Path(cp1.path).exists()
        assert not Path(cp2.path).exists()


class TestEmulatorStateCheckpoints:
    def test_checkpoints_property_creates_manager(self, tmp_path):
        holder = EmulatorState(data_dir=tmp_path)
        mgr = holder.checkpoints
        assert isinstance(mgr, CheckpointManager)
        assert (tmp_path / "checkpoints").is_dir()

    def test_checkpoints_property_returns_same_instance(self, tmp_path):
        holder = EmulatorState(data_dir=tmp_path)
        assert holder.checkpoints is holder.checkpoints


class TestToolIntegration:
    """Test that tool functions create checkpoints correctly."""

    def test_press_buttons_creates_checkpoint(self, holder):
        from melonds_mcp.server import _tool_press_buttons

        result = _tool_press_buttons(holder, ["a"], 1)
        assert "checkpoint_id" in result
        assert len(result["checkpoint_id"]) == 8
        assert holder.checkpoints.total_count == 1
        cp = holder.checkpoints.get(result["checkpoint_id"])
        assert cp.action == "press: a"

    def test_press_buttons_multi_frame_description(self, holder):
        from melonds_mcp.server import _tool_press_buttons

        result = _tool_press_buttons(holder, ["a", "b"], 30)
        cp = holder.checkpoints.get(result["checkpoint_id"])
        assert cp.action == "press: a, b (30f)"

    def test_tap_touch_screen_creates_checkpoint(self, holder):
        from melonds_mcp.server import _tool_tap_touch_screen

        result = _tool_tap_touch_screen(holder, 128, 96, 8)
        assert "checkpoint_id" in result
        cp = holder.checkpoints.get(result["checkpoint_id"])
        assert cp.action == "tap: (128, 96) (8f)"

    def test_run_macro_creates_checkpoint(self, holder):
        import json

        from melonds_mcp.server import _tool_run_macro

        # Write a test macro
        macro = {
            "name": "test",
            "description": "test macro",
            "steps": [{"action": "wait", "frames": 5}],
        }
        holder.macros_dir.mkdir(parents=True, exist_ok=True)
        (holder.macros_dir / "test.json").write_text(json.dumps(macro))

        result = _tool_run_macro(holder, "test", 1)
        assert "checkpoint_id" in result
        cp = holder.checkpoints.get(result["checkpoint_id"])
        assert cp.action == "macro: test"

    def test_run_macro_repeat_description(self, holder):
        import json

        from melonds_mcp.server import _tool_run_macro

        macro = {
            "name": "mash_b",
            "description": "mash B",
            "steps": [{"action": "wait", "frames": 1}],
        }
        holder.macros_dir.mkdir(parents=True, exist_ok=True)
        (holder.macros_dir / "mash_b.json").write_text(json.dumps(macro))

        result = _tool_run_macro(holder, "mash_b", 5)
        cp = holder.checkpoints.get(result["checkpoint_id"])
        assert cp.action == "macro: mash_b (x5)"

    def test_list_checkpoints(self, holder):
        from melonds_mcp.server import _tool_list_checkpoints, _tool_press_buttons

        for i in range(5):
            _tool_press_buttons(holder, ["a"], 1)

        result = _tool_list_checkpoints(holder, 3)
        assert result["total_checkpoints"] == 5
        assert result["showing"] == 3
        assert len(result["checkpoints"]) == 3
        # Each checkpoint has expected fields
        cp = result["checkpoints"][0]
        assert "id" in cp
        assert "frame" in cp
        assert "action" in cp
        assert "time" in cp

    def test_revert_to_checkpoint(self, holder):
        from melonds_mcp.server import (
            _tool_list_checkpoints,
            _tool_press_buttons,
            _tool_revert_to_checkpoint,
        )

        r1 = _tool_press_buttons(holder, ["a"], 1)
        r2 = _tool_press_buttons(holder, ["b"], 1)
        r3 = _tool_press_buttons(holder, ["x"], 1)

        result = _tool_revert_to_checkpoint(holder, r1["checkpoint_id"])
        assert result["success"] is True
        assert result["reverted_to"]["id"] == r1["checkpoint_id"]
        assert result["remaining_checkpoints"] == 1
        assert result["discarded_checkpoints"] == 2


    def test_promote_checkpoint(self, holder):
        from melonds_mcp.server import _tool_press_buttons, _tool_promote_checkpoint

        r1 = _tool_press_buttons(holder, ["a"], 1)
        cp_id = r1["checkpoint_id"]

        result = _tool_promote_checkpoint(holder, cp_id, "debug_save")
        assert result["success"] is True
        assert result["name"] == "debug_save"
        assert result["source_checkpoint"]["id"] == cp_id
        assert Path(result["path"]).exists()
        # Checkpoint still exists in the ring
        assert holder.checkpoints.get(cp_id) is not None


class TestBridgeCheckpoints:
    """Test checkpoint methods exposed through the bridge server."""

    def test_create_checkpoint(self, holder):
        from melonds_mcp.bridge import BridgeServer

        bridge = BridgeServer(holder, "/tmp/test.sock")
        result = bridge._create_checkpoint(action="bridge: heal party")
        assert len(result["checkpoint_id"]) == 8
        assert result["frame"] == holder.frame_count
        assert result["action"] == "bridge: heal party"
        assert holder.checkpoints.total_count == 1

    def test_create_checkpoint_default_action(self, holder):
        from melonds_mcp.bridge import BridgeServer

        bridge = BridgeServer(holder, "/tmp/test.sock")
        result = bridge._create_checkpoint()
        assert result["action"] == "manual"

    def test_list_checkpoints(self, holder):
        from melonds_mcp.bridge import BridgeServer

        bridge = BridgeServer(holder, "/tmp/test.sock")
        bridge._create_checkpoint(action="step 1")
        bridge._create_checkpoint(action="step 2")
        bridge._create_checkpoint(action="step 3")

        result = bridge._list_checkpoints(limit=2)
        assert result["total_checkpoints"] == 3
        assert result["showing"] == 2
        assert result["checkpoints"][0]["action"] == "step 2"
        assert result["checkpoints"][1]["action"] == "step 3"
        assert "time" in result["checkpoints"][0]

    def test_revert_to_checkpoint(self, holder):
        from melonds_mcp.bridge import BridgeServer

        bridge = BridgeServer(holder, "/tmp/test.sock")
        r1 = bridge._create_checkpoint(action="step 1")
        bridge._create_checkpoint(action="step 2")
        bridge._create_checkpoint(action="step 3")

        result = bridge._revert_to_checkpoint(r1["checkpoint_id"])
        assert result["reverted_to"]["id"] == r1["checkpoint_id"]
        assert result["remaining_checkpoints"] == 1
        assert result["discarded_checkpoints"] == 2

    def test_save_checkpoint(self, holder):
        from melonds_mcp.bridge import BridgeServer

        bridge = BridgeServer(holder, "/tmp/test.sock")
        r1 = bridge._create_checkpoint(action="step 1")

        result = bridge._save_checkpoint(r1["checkpoint_id"], "saved_step_1")
        assert result["name"] == "saved_step_1"
        assert result["source_checkpoint"]["id"] == r1["checkpoint_id"]
        assert Path(result["path"]).exists()

    def test_shared_history_with_mcp_tools(self, holder):
        """Bridge and MCP tool checkpoints share the same history."""
        from melonds_mcp.bridge import BridgeServer
        from melonds_mcp.server import _tool_press_buttons

        bridge = BridgeServer(holder, "/tmp/test.sock")

        # MCP tool creates a checkpoint
        r1 = _tool_press_buttons(holder, ["a"], 1)

        # Bridge creates a checkpoint
        r2 = bridge._create_checkpoint(action="bridge: navigate")

        # Both visible in the same list
        result = bridge._list_checkpoints(limit=20)
        assert result["total_checkpoints"] == 2
        actions = [cp["action"] for cp in result["checkpoints"]]
        assert actions[0] == "press: a"
        assert actions[1] == "bridge: navigate"

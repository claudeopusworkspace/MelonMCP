"""Tests for advance_frames_until validation and condition logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from melonds_mcp.emulator import EmulatorState
from melonds_mcp.server import _tool_advance_frames_until


# ---------------------------------------------------------------------------
# Validation tests (server-layer)
# ---------------------------------------------------------------------------

def _make_holder():
    """Create a minimal mock EmulatorState for validation tests."""
    holder = MagicMock(spec=EmulatorState)
    holder.frame_count = 100
    holder.advance_frames_until.return_value = {
        "triggered": False,
        "condition_index": -1,
        "frames_elapsed": 60,
        "total_frame": 160,
    }
    holder._journal = None
    return holder


class TestValidation:
    """Tests for input validation in _tool_advance_frames_until."""

    def test_max_frames_zero(self):
        with pytest.raises(ValueError, match="max_frames must be >= 1"):
            _tool_advance_frames_until(
                _make_holder(), 0,
                [{"type": "value", "address": 0x02000000, "operator": "==", "value": 1}],
                1, [], None, None, [],
            )

    def test_max_frames_too_large(self):
        with pytest.raises(ValueError, match="max_frames must be <= 3600"):
            _tool_advance_frames_until(
                _make_holder(), 9999,
                [{"type": "value", "address": 0x02000000, "operator": "==", "value": 1}],
                1, [], None, None, [],
            )

    def test_no_conditions(self):
        with pytest.raises(ValueError, match="at least one condition"):
            _tool_advance_frames_until(
                _make_holder(), 60, [], 1, [], None, None, [],
            )

    def test_too_many_conditions(self):
        conds = [
            {"type": "value", "address": 0x02000000, "operator": "==", "value": i}
            for i in range(17)
        ]
        with pytest.raises(ValueError, match="Too many conditions"):
            _tool_advance_frames_until(
                _make_holder(), 60, conds, 1, [], None, None, [],
            )

    def test_invalid_condition_type(self):
        with pytest.raises(ValueError, match="type must be one of"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "bogus", "address": 0x02000000}],
                1, [], None, None, [],
            )

    def test_value_missing_operator(self):
        with pytest.raises(ValueError, match="requires 'operator'"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "value", "address": 0x02000000, "value": 1}],
                1, [], None, None, [],
            )

    def test_value_invalid_operator(self):
        with pytest.raises(ValueError, match="operator must be one of"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "value", "address": 0x02000000, "operator": "~", "value": 1}],
                1, [], None, None, [],
            )

    def test_value_missing_value(self):
        with pytest.raises(ValueError, match="requires 'value'"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "value", "address": 0x02000000, "operator": "=="}],
                1, [], None, None, [],
            )

    def test_value_invalid_size(self):
        with pytest.raises(ValueError, match="size must be one of"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "value", "address": 0x02000000, "size": "quad",
                  "operator": "==", "value": 1}],
                1, [], None, None, [],
            )

    def test_changed_invalid_size(self):
        with pytest.raises(ValueError, match="size must be one of"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "changed", "address": 0x02000000, "size": "word"}],
                1, [], None, None, [],
            )

    def test_pattern_missing_length(self):
        with pytest.raises(ValueError, match="requires 'length'"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "pattern", "address": 0x02000000, "pattern": "FF"}],
                1, [], None, None, [],
            )

    def test_pattern_missing_pattern(self):
        with pytest.raises(ValueError, match="requires 'pattern'"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "pattern", "address": 0x02000000, "length": 256}],
                1, [], None, None, [],
            )

    def test_pattern_invalid_hex(self):
        with pytest.raises(ValueError, match="valid hex string"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "pattern", "address": 0x02000000, "length": 256,
                  "pattern": "ZZZZ"}],
                1, [], None, None, [],
            )

    def test_missing_condition_address(self):
        with pytest.raises(ValueError, match="missing required field 'address'"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "changed"}],
                1, [], None, None, [],
            )

    def test_poll_interval_zero(self):
        with pytest.raises(ValueError, match="poll_interval must be >= 1"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "changed", "address": 0x02000000}],
                0, [], None, None, [],
            )

    def test_poll_interval_exceeds_max_frames(self):
        with pytest.raises(ValueError, match="poll_interval must be <= max_frames"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "changed", "address": 0x02000000}],
                120, [], None, None, [],
            )

    def test_read_addresses_missing_address(self):
        with pytest.raises(ValueError, match="read_addresses.*missing required field"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "changed", "address": 0x02000000}],
                1, [], None, None,
                [{"size": "byte"}],
            )

    def test_read_addresses_invalid_size(self):
        with pytest.raises(ValueError, match="read_addresses.*size must be one of"):
            _tool_advance_frames_until(
                _make_holder(), 60,
                [{"type": "changed", "address": 0x02000000}],
                1, [], None, None,
                [{"address": 0x02000000, "size": "double"}],
            )

    def test_valid_call_passes_through(self):
        """Valid inputs should reach the emulator method and return its result."""
        holder = _make_holder()
        result = _tool_advance_frames_until(
            holder, 600,
            [{"type": "value", "address": 0x02000000, "operator": "==", "value": 2}],
            1, [], None, None, [],
        )
        holder.advance_frames_until.assert_called_once()
        assert result["frames_elapsed"] == 60

    def test_valid_pattern_condition(self):
        holder = _make_holder()
        result = _tool_advance_frames_until(
            holder, 100,
            [{"type": "pattern", "address": 0x0257D2EC, "length": 65536,
              "pattern": "D2ECB6F8"}],
            15, [], None, None, [],
        )
        holder.advance_frames_until.assert_called_once()

    def test_valid_changed_condition(self):
        holder = _make_holder()
        result = _tool_advance_frames_until(
            holder, 100,
            [{"type": "changed", "address": 0x02000000, "size": "long"}],
            15, [], None, None, [],
        )
        holder.advance_frames_until.assert_called_once()

    def test_multiple_conditions(self):
        holder = _make_holder()
        result = _tool_advance_frames_until(
            holder, 600,
            [
                {"type": "value", "address": 0x02000000, "operator": "==", "value": 2},
                {"type": "changed", "address": 0x02000010, "size": "short"},
                {"type": "pattern", "address": 0x02580000, "length": 1024, "pattern": "AABB"},
            ],
            1, ["a"], None, None,
            [{"address": 0x02000000, "size": "long"}],
        )
        holder.advance_frames_until.assert_called_once()


# ---------------------------------------------------------------------------
# Condition-checking unit tests (emulator-layer)
# ---------------------------------------------------------------------------

class TestCheckCondition:
    """Test the _check_condition helper directly."""

    def _make_emulator_state(self):
        """Build an EmulatorState with a mocked emu backend."""
        state = EmulatorState.__new__(EmulatorState)
        state.emu = MagicMock()
        state.is_rom_loaded = True
        state.frame_count = 0
        return state

    # -- value conditions --

    def test_value_eq_match(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 5
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "byte", "operator": "==", "value": 5}
        )
        assert result == {"matched_value": 5}

    def test_value_eq_no_match(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 3
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "byte", "operator": "==", "value": 5}
        )
        assert result is None

    def test_value_neq(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 3
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "byte", "operator": "!=", "value": 5}
        )
        assert result == {"matched_value": 3}

    def test_value_gt(self):
        es = self._make_emulator_state()
        es.emu.memory_read_short.return_value = 10
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "short", "operator": ">", "value": 5}
        )
        assert result == {"matched_value": 10}

    def test_value_lt(self):
        es = self._make_emulator_state()
        es.emu.memory_read_long.return_value = 2
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "long", "operator": "<", "value": 5}
        )
        assert result == {"matched_value": 2}

    def test_value_gte(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 5
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "byte", "operator": ">=", "value": 5}
        )
        assert result == {"matched_value": 5}

    def test_value_lte(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 4
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "byte", "operator": "<=", "value": 5}
        )
        assert result == {"matched_value": 4}

    def test_value_bitmask(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 0b10101010
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "byte", "operator": "&", "value": 0b00001010}
        )
        assert result == {"matched_value": 0b10101010}

    def test_value_bitmask_no_match(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 0b10100000
        result = es._check_condition(
            {"type": "value", "address": 0x100, "size": "byte", "operator": "&", "value": 0b00000101}
        )
        assert result is None

    def test_value_unknown_operator(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 5
        with pytest.raises(ValueError, match="Unknown operator"):
            es._check_condition(
                {"type": "value", "address": 0x100, "size": "byte", "operator": "~", "value": 5}
            )

    def test_value_default_size_is_byte(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 42
        result = es._check_condition(
            {"type": "value", "address": 0x100, "operator": "==", "value": 42}
        )
        assert result == {"matched_value": 42}
        es.emu.memory_read_byte.assert_called_with(0x100)

    # -- changed conditions --

    def test_changed_fires(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 10
        result = es._check_condition(
            {"type": "changed", "address": 0x200, "size": "byte"},
            initial_value=5,
        )
        assert result == {"initial_value": 5, "matched_value": 10}

    def test_changed_no_change(self):
        es = self._make_emulator_state()
        es.emu.memory_read_byte.return_value = 5
        result = es._check_condition(
            {"type": "changed", "address": 0x200, "size": "byte"},
            initial_value=5,
        )
        assert result is None

    # -- pattern conditions --

    def test_pattern_found(self):
        es = self._make_emulator_state()
        # Pattern "AABB" at offset 10 in a 32-byte block starting at 0x1000
        data = b"\x00" * 10 + b"\xAA\xBB" + b"\x00" * 20
        es.emu.memory_read_block.return_value = data
        result = es._check_condition(
            {"type": "pattern", "address": 0x1000, "length": 32, "pattern": "AABB"}
        )
        assert result == {"matched_offset": 0x1000 + 10}

    def test_pattern_not_found(self):
        es = self._make_emulator_state()
        es.emu.memory_read_block.return_value = b"\x00" * 32
        result = es._check_condition(
            {"type": "pattern", "address": 0x1000, "length": 32, "pattern": "AABB"}
        )
        assert result is None

    # -- unknown type --

    def test_unknown_type(self):
        es = self._make_emulator_state()
        with pytest.raises(ValueError, match="Unknown condition type"):
            es._check_condition({"type": "alien", "address": 0x100})


# ---------------------------------------------------------------------------
# Integration-style tests (mocked emu, real loop logic)
# ---------------------------------------------------------------------------

class TestAdvanceFramesUntilLoop:
    """Test the advance_frames_until method with a mocked emulator backend."""

    def _make_state(self):
        """Build an EmulatorState wired to a mock MelonDS."""
        state = EmulatorState.__new__(EmulatorState)
        state.emu = MagicMock()
        state.is_rom_loaded = True
        state.frame_count = 1000
        state.lock = MagicMock()
        state._cycle_callbacks = []
        state._frame_callbacks = []
        # cycle() is called by advance_frame
        state.emu.cycle = MagicMock()
        state.emu.set_skip_render = MagicMock()
        state.emu.input_keypad_update = MagicMock()
        state.emu.input_release_touch = MagicMock()
        return state

    def test_value_triggers_immediately(self):
        """Condition met on first check -> returns after 2 frames (1 loop + 1 render)."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 2
        result = state.advance_frames_until(
            max_frames=600,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 2}],
        )
        assert result["triggered"] is True
        assert result["condition_index"] == 0
        assert result["frames_elapsed"] == 2  # 1 check frame + 1 render frame
        assert result["matched_value"] == 2

    def test_value_triggers_after_several_frames(self):
        """Value changes after 5 frames."""
        state = self._make_state()
        call_count = 0

        def mock_read(addr):
            nonlocal call_count
            call_count += 1
            return 2 if call_count >= 5 else 0

        state.emu.memory_read_byte.side_effect = mock_read
        result = state.advance_frames_until(
            max_frames=600,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 2}],
        )
        assert result["triggered"] is True
        assert result["frames_elapsed"] == 6  # 5 check + 1 render

    def test_max_frames_reached(self):
        """Condition never met -> runs to max_frames."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 0
        result = state.advance_frames_until(
            max_frames=10,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 99}],
        )
        assert result["triggered"] is False
        assert result["condition_index"] == -1
        assert result["frames_elapsed"] == 11  # 10 loop + 1 render

    def test_poll_interval(self):
        """With poll_interval=5, condition at frame 3 isn't seen until frame 5."""
        state = self._make_state()
        frame_in_loop = 0

        def mock_read(addr):
            nonlocal frame_in_loop
            # The value is ready by "frame 3" but we only check at multiples of 5
            # memory_read_byte is called at each check; frame count is tracked by advance_frame
            return 2  # Always match — but only checked at poll intervals

        state.emu.memory_read_byte.side_effect = mock_read
        result = state.advance_frames_until(
            max_frames=600,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 2}],
            poll_interval=5,
        )
        assert result["triggered"] is True
        # First check at frame 5
        assert result["frames_elapsed"] == 6  # 5 + 1 render

    def test_changed_condition(self):
        """Changed condition fires when value differs from initial."""
        state = self._make_state()
        call_count = 0

        def mock_read(addr):
            nonlocal call_count
            call_count += 1
            # First call is for initial capture, next 2 return same, then changes
            if call_count <= 3:
                return 42
            return 99

        state.emu.memory_read_byte.side_effect = mock_read
        result = state.advance_frames_until(
            max_frames=100,
            conditions=[{"type": "changed", "address": 0x200, "size": "byte"}],
        )
        assert result["triggered"] is True
        assert result["initial_value"] == 42
        assert result["matched_value"] == 99

    def test_pattern_condition(self):
        """Pattern condition fires when pattern appears in memory."""
        state = self._make_state()
        call_count = 0

        def mock_block(addr, length):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:  # initial check + first poll
                return b"\x00" * length
            # Pattern appears
            return b"\x00" * 10 + b"\xAA\xBB" + b"\x00" * (length - 12)

        state.emu.memory_read_block.side_effect = mock_block
        result = state.advance_frames_until(
            max_frames=100,
            conditions=[{"type": "pattern", "address": 0x1000, "length": 64,
                         "pattern": "AABB"}],
        )
        assert result["triggered"] is True
        assert result["matched_offset"] == 0x1000 + 10

    def test_pattern_already_present_skipped(self):
        """If pattern is already present at frame 0, don't trigger until it disappears and reappears."""
        state = self._make_state()
        call_count = 0

        def mock_block(addr, length):
            nonlocal call_count
            call_count += 1
            # Always present — should never trigger
            return b"\x00" * 5 + b"\xAA\xBB" + b"\x00" * (length - 7)

        state.emu.memory_read_block.side_effect = mock_block
        result = state.advance_frames_until(
            max_frames=10,
            conditions=[{"type": "pattern", "address": 0x1000, "length": 32,
                         "pattern": "AABB"}],
        )
        assert result["triggered"] is False

    def test_pattern_reappears_after_disappearing(self):
        """Pattern present at start, disappears, then reappears -> triggers."""
        state = self._make_state()
        call_count = 0

        def mock_block(addr, length):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Initial check: pattern present
                return b"\xAA\xBB" + b"\x00" * (length - 2)
            elif call_count <= 4:
                # Polls 2-4: pattern gone
                return b"\x00" * length
            else:
                # Poll 5+: pattern reappears
                return b"\x00" * 3 + b"\xAA\xBB" + b"\x00" * (length - 5)

        state.emu.memory_read_block.side_effect = mock_block
        result = state.advance_frames_until(
            max_frames=100,
            conditions=[{"type": "pattern", "address": 0x1000, "length": 32,
                         "pattern": "AABB"}],
        )
        assert result["triggered"] is True
        assert result["matched_offset"] == 0x1000 + 3

    def test_multiple_conditions_first_wins(self):
        """With multiple conditions (OR), the first to match determines condition_index."""
        state = self._make_state()
        # cond 0 never matches, cond 1 matches immediately
        state.emu.memory_read_byte.return_value = 0
        state.emu.memory_read_short.return_value = 42

        result = state.advance_frames_until(
            max_frames=100,
            conditions=[
                {"type": "value", "address": 0x100, "size": "byte",
                 "operator": "==", "value": 99},
                {"type": "value", "address": 0x200, "size": "short",
                 "operator": "==", "value": 42},
            ],
        )
        assert result["triggered"] is True
        assert result["condition_index"] == 1

    def test_render_skip_called(self):
        """Verify set_skip_render is used correctly."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 1
        state.advance_frames_until(
            max_frames=5,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 1}],
        )
        # Should have called set_skip_render(True) then set_skip_render(False)
        calls = state.emu.set_skip_render.call_args_list
        assert calls[0].args == (True,)
        assert calls[1].args == (False,)

    def test_read_addresses(self):
        """read_addresses should be populated in the return value."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 2
        state.emu.memory_read_long.return_value = 0xDEADBEEF

        result = state.advance_frames_until(
            max_frames=100,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 2}],
            read_addresses=[{"address": 0x300, "size": "long"}],
        )
        assert result["triggered"] is True
        assert "reads" in result
        assert result["reads"]["0x00000300"] == 0xDEADBEEF

    def test_read_addresses_multi_count(self):
        """read_addresses with count > 1 returns a list."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 2

        read_vals = [10, 20, 30]
        call_idx = 0

        original_read = state.emu.memory_read_short.side_effect

        def mock_short(addr):
            nonlocal call_idx
            val = read_vals[call_idx % len(read_vals)]
            call_idx += 1
            return val

        state.emu.memory_read_short.side_effect = mock_short

        result = state.advance_frames_until(
            max_frames=100,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 2}],
            read_addresses=[{"address": 0x400, "size": "short", "count": 3}],
        )
        assert result["reads"]["0x00000400"] == [10, 20, 30]

    def test_buttons_passed_through(self):
        """Buttons should be applied on every frame."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 5
        state.advance_frames_until(
            max_frames=3,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 5}],
            buttons=["a"],
        )
        # input_keypad_update should have been called on every advance_frame
        assert state.emu.input_keypad_update.call_count >= 2  # at least loop + render

    def test_frame_count_incremented(self):
        """frame_count should be updated after the call."""
        state = self._make_state()
        initial = state.frame_count
        state.emu.memory_read_byte.return_value = 0  # never triggers
        result = state.advance_frames_until(
            max_frames=10,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 99}],
        )
        assert state.frame_count == initial + result["frames_elapsed"]

    def test_total_frame_in_result(self):
        """total_frame should equal frame_count after the call."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 0
        result = state.advance_frames_until(
            max_frames=5,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 99}],
        )
        assert result["total_frame"] == state.frame_count

    def test_trailing_frame_releases_buttons(self):
        """Regression for issue #10: the post-trigger render frame must NOT
        carry the polling buttons forward, otherwise chained calls (e.g. a
        per-tile navigation primitive) commit one extra step of the previous
        direction before the next call's input takes effect."""
        from melonds_mcp.constants import buttons_to_bitmask

        state = self._make_state()
        state.emu.memory_read_byte.return_value = 1  # triggers immediately

        state.advance_frames_until(
            max_frames=10,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 1}],
            buttons=["right"],
            touch_x=50,
            touch_y=60,
        )

        # During the polling loop, "right" must have been held.
        right_mask = buttons_to_bitmask(["right"])
        keypad_calls = [c.args[0] for c in state.emu.input_keypad_update.call_args_list]
        assert keypad_calls[0] == right_mask, "loop frame should hold polling buttons"

        # The trailing render frame must release: final keypad update is 0,
        # and touch is released (not re-set) on the way out.
        assert keypad_calls[-1] == 0, (
            f"trailing frame must release buttons, got bitmask {keypad_calls[-1]:#x}"
        )
        assert state.emu.input_release_touch.call_count >= 1

    def test_trailing_frame_releases_when_no_polling_buttons(self):
        """When buttons=None, behavior is unchanged: every frame is empty."""
        state = self._make_state()
        state.emu.memory_read_byte.return_value = 1
        state.advance_frames_until(
            max_frames=10,
            conditions=[{"type": "value", "address": 0x100, "size": "byte",
                         "operator": "==", "value": 1}],
        )
        keypad_calls = [c.args[0] for c in state.emu.input_keypad_update.call_args_list]
        assert all(mask == 0 for mask in keypad_calls)

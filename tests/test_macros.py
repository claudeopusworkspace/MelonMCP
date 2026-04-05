"""Tests for macro step validation."""

import pytest

from melonds_mcp.server import _validate_macro_steps


def test_valid_press_step():
    _validate_macro_steps([{"action": "press", "buttons": ["a"]}])


def test_valid_hold_step():
    _validate_macro_steps([{"action": "hold", "buttons": ["right"], "frames": 60}])


def test_valid_wait_step():
    _validate_macro_steps([{"action": "wait", "frames": 30}])


def test_valid_tap_step():
    _validate_macro_steps([{"action": "tap", "x": 128, "y": 96, "frames": 5}])


def test_valid_multi_step():
    _validate_macro_steps([
        {"action": "press", "buttons": ["a"]},
        {"action": "wait", "frames": 15},
        {"action": "hold", "buttons": ["right", "b"], "frames": 60},
        {"action": "tap", "x": 0, "y": 0},
    ])


def test_empty_steps_rejected():
    with pytest.raises(ValueError, match="at least one step"):
        _validate_macro_steps([])


def test_missing_action_rejected():
    with pytest.raises(ValueError, match="missing 'action'"):
        _validate_macro_steps([{"buttons": ["a"]}])


def test_unknown_action_rejected():
    with pytest.raises(ValueError, match="unknown action"):
        _validate_macro_steps([{"action": "jump"}])


def test_missing_required_field_rejected():
    with pytest.raises(ValueError, match="missing required field"):
        _validate_macro_steps([{"action": "press"}])  # missing "buttons"


def test_unknown_field_rejected():
    with pytest.raises(ValueError, match="unknown field"):
        _validate_macro_steps([{"action": "wait", "frames": 10, "color": "red"}])


def test_invalid_frames_rejected():
    with pytest.raises(ValueError, match="frames must be"):
        _validate_macro_steps([{"action": "wait", "frames": 0}])


def test_too_many_steps_rejected():
    steps = [{"action": "wait", "frames": 1}] * 101
    with pytest.raises(ValueError, match="at most 100"):
        _validate_macro_steps(steps)

"""Tests for button bitmask constants and conversion."""

import pytest

from melonds_mcp.constants import (
    BUTTON_MAP,
    Key,
    KeyMask,
    buttons_to_bitmask,
    keymask,
)


def test_keymask_values():
    """Verify bitmask values match ctrlssdl.cpp update_keypad() bit positions."""
    assert keymask(Key.A) == 0x0001
    assert keymask(Key.B) == 0x0002
    assert keymask(Key.SELECT) == 0x0004
    assert keymask(Key.START) == 0x0008
    assert keymask(Key.RIGHT) == 0x0010
    assert keymask(Key.LEFT) == 0x0020
    assert keymask(Key.UP) == 0x0040
    assert keymask(Key.DOWN) == 0x0080
    assert keymask(Key.R) == 0x0100
    assert keymask(Key.L) == 0x0200
    assert keymask(Key.X) == 0x0400
    assert keymask(Key.Y) == 0x0800


def test_keymask_enum_matches():
    """KeyMask enum values should match keymask() function output."""
    assert KeyMask.A == keymask(Key.A)
    assert KeyMask.B == keymask(Key.B)
    assert KeyMask.START == keymask(Key.START)
    assert KeyMask.SELECT == keymask(Key.SELECT)
    assert KeyMask.L == keymask(Key.L)
    assert KeyMask.R == keymask(Key.R)
    assert KeyMask.X == keymask(Key.X)
    assert KeyMask.Y == keymask(Key.Y)
    assert KeyMask.UP == keymask(Key.UP)
    assert KeyMask.DOWN == keymask(Key.DOWN)
    assert KeyMask.LEFT == keymask(Key.LEFT)
    assert KeyMask.RIGHT == keymask(Key.RIGHT)


def test_button_map_coverage():
    """All 12 standard DS buttons should be in BUTTON_MAP."""
    expected = {"a", "b", "x", "y", "l", "r", "start", "select", "up", "down", "left", "right"}
    assert set(BUTTON_MAP.keys()) == expected


def test_buttons_to_bitmask_single():
    assert buttons_to_bitmask(["a"]) == KeyMask.A
    assert buttons_to_bitmask(["start"]) == KeyMask.START


def test_buttons_to_bitmask_multiple():
    mask = buttons_to_bitmask(["a", "up", "r"])
    assert mask == KeyMask.A | KeyMask.UP | KeyMask.R


def test_buttons_to_bitmask_empty():
    assert buttons_to_bitmask([]) == 0


def test_buttons_to_bitmask_case_insensitive():
    assert buttons_to_bitmask(["A"]) == KeyMask.A
    assert buttons_to_bitmask(["Start"]) == KeyMask.START
    assert buttons_to_bitmask(["LEFT"]) == KeyMask.LEFT


def test_buttons_to_bitmask_strips_whitespace():
    assert buttons_to_bitmask([" a "]) == KeyMask.A


def test_buttons_to_bitmask_invalid():
    with pytest.raises(ValueError, match="Unknown button"):
        buttons_to_bitmask(["invalid"])


def test_buttons_to_bitmask_duplicate():
    """Duplicate buttons should not cause issues (OR is idempotent)."""
    assert buttons_to_bitmask(["a", "a"]) == KeyMask.A


def test_combined_bitmask_no_overlap():
    """Each button should occupy a unique bit."""
    all_masks = list(BUTTON_MAP.values())
    combined = 0
    for m in all_masks:
        assert combined & m == 0, f"Bit overlap detected for mask {m:#06x}"
        combined |= m

"""DS button bitmasks, screen dimensions, and related constants."""

from enum import IntEnum, IntFlag

# Screen dimensions (per screen)
SCREEN_WIDTH = 256
SCREEN_HEIGHT = 192

# Both screens stacked vertically
TOTAL_WIDTH = SCREEN_WIDTH
TOTAL_HEIGHT = SCREEN_HEIGHT * 2  # 384

# Pixel counts
SCREEN_PIXEL_COUNT = SCREEN_WIDTH * SCREEN_HEIGHT  # 49152
TOTAL_PIXEL_COUNT = SCREEN_PIXEL_COUNT * 2  # 98304

# Screenshot buffer sizes
SCREENSHOT_RGB_SIZE = TOTAL_PIXEL_COUNT * 3  # 294912 bytes
SCREENSHOT_RGBX_SIZE = TOTAL_PIXEL_COUNT * 4  # 393216 bytes

# DS runs at ~60fps
FRAMES_PER_SECOND = 60


class Key(IntEnum):
    """Key indices matching DS hardware KEYINPUT register bit positions."""

    A = 0
    B = 1
    SELECT = 2
    START = 3
    RIGHT = 4
    LEFT = 5
    UP = 6
    DOWN = 7
    R = 8
    L = 9
    X = 10
    Y = 11
    DEBUG = 12
    BOOST = 13
    LID = 14


def keymask(key: Key) -> int:
    """Convert key index to bitmask: KEYMASK_(k) = (1 << k)."""
    return 1 << key


class KeyMask(IntFlag):
    """Pre-computed key bitmasks for input_keypad_update."""

    NONE = 0
    A = 1 << Key.A  # 0x0001
    B = 1 << Key.B  # 0x0002
    SELECT = 1 << Key.SELECT  # 0x0004
    START = 1 << Key.START  # 0x0008
    RIGHT = 1 << Key.RIGHT  # 0x0010
    LEFT = 1 << Key.LEFT  # 0x0020
    UP = 1 << Key.UP  # 0x0040
    DOWN = 1 << Key.DOWN  # 0x0080
    R = 1 << Key.R  # 0x0100
    L = 1 << Key.L  # 0x0200
    X = 1 << Key.X  # 0x0400
    Y = 1 << Key.Y  # 0x0800


# String-to-bitmask lookup for MCP tool convenience
BUTTON_MAP: dict[str, int] = {
    "a": KeyMask.A,
    "b": KeyMask.B,
    "x": KeyMask.X,
    "y": KeyMask.Y,
    "l": KeyMask.L,
    "r": KeyMask.R,
    "start": KeyMask.START,
    "select": KeyMask.SELECT,
    "up": KeyMask.UP,
    "down": KeyMask.DOWN,
    "left": KeyMask.LEFT,
    "right": KeyMask.RIGHT,
}

# All valid button names (for error messages)
VALID_BUTTONS = sorted(BUTTON_MAP.keys())


def buttons_to_bitmask(buttons: list[str]) -> int:
    """Convert a list of button names to a u16 bitmask.

    Args:
        buttons: List of button names like ["a", "up", "r"].

    Returns:
        Combined bitmask for input_keypad_update.

    Raises:
        ValueError: If an unknown button name is provided.
    """
    mask = 0
    for btn in buttons:
        btn_lower = btn.lower().strip()
        if btn_lower not in BUTTON_MAP:
            raise ValueError(
                f"Unknown button: {btn!r}. Valid buttons: {VALID_BUTTONS}"
            )
        mask |= BUTTON_MAP[btn_lower]
    return mask

"""Project settings — loads from settings.json with settings.default.json as fallback."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _PROJECT_ROOT / "settings.default.json"
_USER_PATH = _PROJECT_ROOT / "settings.json"


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning {} on any failure."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.debug("Could not load %s: %s", path, e)
        return {}


def load_settings() -> dict[str, Any]:
    """Load settings with user overrides merged on top of defaults.

    Resolution order:
        1. settings.default.json (checked into repo)
        2. settings.json (user-local, gitignored)
    """
    settings = _load_json(_DEFAULT_PATH)
    user = _load_json(_USER_PATH)
    settings.update(user)
    return settings


def get_stream() -> bool:
    """Return the stream setting: whether to auto-start viewer, HLS stream, and recording on ROM load."""
    settings = load_settings()
    return bool(settings.get("stream", False))

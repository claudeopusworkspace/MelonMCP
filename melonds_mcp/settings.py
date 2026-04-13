"""Project settings — loads from settings.json with settings.default.json as fallback."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _PROJECT_ROOT / "settings.default.json"
_USER_PATH = _PROJECT_ROOT / "settings.json"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


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


def _parse_bool_env(name: str) -> bool | None:
    """Parse an env var as a boolean. Returns None if unset/empty.

    Accepts (case-insensitive): 1/0, true/false, yes/no, on/off.
    Raises ValueError on any other non-empty value so typos don't silently
    become "falsy".
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ValueError(
        f"Invalid boolean value for {name}={raw!r}; "
        f"expected one of {sorted(_TRUTHY | _FALSY)}"
    )


def get_stream() -> bool:
    """Return the stream setting: whether to auto-start viewer, HLS stream, and recording on ROM load.

    Resolution order (first match wins):
        1. MELONDS_NO_STREAM env var (if truthy, forces stream off)
        2. MELONDS_STREAM env var (explicit on/off)
        3. settings.json / settings.default.json "stream" key
    """
    no_stream = _parse_bool_env("MELONDS_NO_STREAM")
    if no_stream is True:
        return False

    stream_env = _parse_bool_env("MELONDS_STREAM")
    if stream_env is not None:
        return stream_env

    settings = load_settings()
    return bool(settings.get("stream", False))

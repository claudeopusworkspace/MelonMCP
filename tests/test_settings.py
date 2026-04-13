"""Tests for settings loading and env-var overrides."""

from __future__ import annotations

import pytest

from melonds_mcp import settings as settings_mod


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure stream-related env vars are unset for each test."""
    monkeypatch.delenv("MELONDS_STREAM", raising=False)
    monkeypatch.delenv("MELONDS_NO_STREAM", raising=False)


@pytest.fixture
def stub_settings(monkeypatch):
    """Stub load_settings() so tests don't depend on on-disk JSON."""

    def _stub(value: dict):
        monkeypatch.setattr(settings_mod, "load_settings", lambda: value)

    return _stub


@pytest.fixture(autouse=True)
def clear_stream_override():
    """Reset the process-local override before and after each test.

    The override is module-global, so leaks between tests would be confusing.
    """
    settings_mod.set_stream_override(None)
    yield
    settings_mod.set_stream_override(None)


def test_get_stream_reads_json_when_env_unset(clean_env, stub_settings):
    stub_settings({"stream": True})
    assert settings_mod.get_stream() is True

    stub_settings({"stream": False})
    assert settings_mod.get_stream() is False


def test_get_stream_defaults_false_when_missing(clean_env, stub_settings):
    stub_settings({})
    assert settings_mod.get_stream() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On", " true "])
def test_melonds_stream_truthy_overrides_json_false(
    monkeypatch, clean_env, stub_settings, value
):
    stub_settings({"stream": False})
    monkeypatch.setenv("MELONDS_STREAM", value)
    assert settings_mod.get_stream() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "Off", " 0 "])
def test_melonds_stream_falsy_overrides_json_true(
    monkeypatch, clean_env, stub_settings, value
):
    stub_settings({"stream": True})
    monkeypatch.setenv("MELONDS_STREAM", value)
    assert settings_mod.get_stream() is False


def test_melonds_stream_empty_falls_through_to_json(
    monkeypatch, clean_env, stub_settings
):
    stub_settings({"stream": True})
    monkeypatch.setenv("MELONDS_STREAM", "")
    assert settings_mod.get_stream() is True


def test_melonds_stream_invalid_raises(monkeypatch, clean_env, stub_settings):
    stub_settings({"stream": True})
    monkeypatch.setenv("MELONDS_STREAM", "maybe")
    with pytest.raises(ValueError, match="MELONDS_STREAM"):
        settings_mod.get_stream()


def test_melonds_no_stream_forces_off(monkeypatch, clean_env, stub_settings):
    stub_settings({"stream": True})
    monkeypatch.setenv("MELONDS_NO_STREAM", "1")
    assert settings_mod.get_stream() is False


def test_melonds_no_stream_falsy_does_not_force_off(
    monkeypatch, clean_env, stub_settings
):
    """MELONDS_NO_STREAM=0 should not force streaming off — it should fall through."""
    stub_settings({"stream": True})
    monkeypatch.setenv("MELONDS_NO_STREAM", "0")
    assert settings_mod.get_stream() is True


def test_melonds_no_stream_beats_melonds_stream(
    monkeypatch, clean_env, stub_settings
):
    """MELONDS_NO_STREAM=1 wins even if MELONDS_STREAM=1."""
    stub_settings({"stream": False})
    monkeypatch.setenv("MELONDS_NO_STREAM", "1")
    monkeypatch.setenv("MELONDS_STREAM", "1")
    assert settings_mod.get_stream() is False


def test_melonds_no_stream_invalid_raises(monkeypatch, clean_env, stub_settings):
    stub_settings({"stream": True})
    monkeypatch.setenv("MELONDS_NO_STREAM", "sometimes")
    with pytest.raises(ValueError, match="MELONDS_NO_STREAM"):
        settings_mod.get_stream()


# ── Process-local override (set via the set_stream_config MCP tool) ──────────


def test_stream_override_false_beats_env_and_settings(
    monkeypatch, clean_env, stub_settings
):
    """Override sits at tier 0 — wins over MELONDS_STREAM and settings.json."""
    stub_settings({"stream": True})
    monkeypatch.setenv("MELONDS_STREAM", "1")
    settings_mod.set_stream_override(False)
    assert settings_mod.get_stream() is False


def test_stream_override_true_beats_no_stream_env(
    monkeypatch, clean_env, stub_settings
):
    """Override beats even MELONDS_NO_STREAM, which otherwise wins everything."""
    stub_settings({"stream": False})
    monkeypatch.setenv("MELONDS_NO_STREAM", "1")
    settings_mod.set_stream_override(True)
    assert settings_mod.get_stream() is True


def test_stream_override_none_falls_through(
    monkeypatch, clean_env, stub_settings
):
    """Clearing the override restores the env + settings.json chain."""
    stub_settings({"stream": True})
    settings_mod.set_stream_override(False)
    assert settings_mod.get_stream() is False
    settings_mod.set_stream_override(None)
    assert settings_mod.get_stream() is True


def test_get_stream_override_reports_current_value(clean_env, stub_settings):
    stub_settings({})
    assert settings_mod.get_stream_override() is None
    settings_mod.set_stream_override(True)
    assert settings_mod.get_stream_override() is True
    settings_mod.set_stream_override(False)
    assert settings_mod.get_stream_override() is False
    settings_mod.set_stream_override(None)
    assert settings_mod.get_stream_override() is None

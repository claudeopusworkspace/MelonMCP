"""Tests for memory watch validation and transforms."""

import pytest

from melonds_mcp.server import _apply_transform, _validate_watch_fields


class TestValidateWatchFields:
    def test_valid_simple_field(self):
        _validate_watch_fields([{"name": "hp", "offset": 0, "size": "short"}])

    def test_valid_signed_field(self):
        _validate_watch_fields([
            {"name": "x_pos", "offset": 4, "size": "long", "signed": True}
        ])

    def test_valid_field_with_map_transform(self):
        _validate_watch_fields([{
            "name": "species",
            "offset": 0,
            "size": "short",
            "transform": {
                "type": "map",
                "values": {"393": "Piplup", "390": "Chimchar"},
                "default": "Unknown",
            },
        }])

    def test_valid_multi_field(self):
        _validate_watch_fields([
            {"name": "species_id", "offset": 0, "size": "short"},
            {"name": "level", "offset": 2, "size": "byte"},
            {"name": "current_hp", "offset": 4, "size": "short"},
            {"name": "max_hp", "offset": 6, "size": "short"},
        ])

    def test_empty_fields_rejected(self):
        with pytest.raises(ValueError, match="at least one field"):
            _validate_watch_fields([])

    def test_missing_name_rejected(self):
        with pytest.raises(ValueError, match="missing required key 'name'"):
            _validate_watch_fields([{"offset": 0, "size": "byte"}])

    def test_missing_offset_rejected(self):
        with pytest.raises(ValueError, match="missing required key 'offset'"):
            _validate_watch_fields([{"name": "hp", "size": "byte"}])

    def test_missing_size_rejected(self):
        with pytest.raises(ValueError, match="missing required key 'size'"):
            _validate_watch_fields([{"name": "hp", "offset": 0}])

    def test_invalid_size_rejected(self):
        with pytest.raises(ValueError, match="must be one of"):
            _validate_watch_fields([{"name": "hp", "offset": 0, "size": "qword"}])

    def test_negative_offset_rejected(self):
        with pytest.raises(ValueError, match="non-negative integer"):
            _validate_watch_fields([{"name": "hp", "offset": -1, "size": "byte"}])

    def test_duplicate_name_rejected(self):
        with pytest.raises(ValueError, match="duplicate field name"):
            _validate_watch_fields([
                {"name": "hp", "offset": 0, "size": "byte"},
                {"name": "hp", "offset": 1, "size": "byte"},
            ])

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            _validate_watch_fields([
                {"name": "hp", "offset": 0, "size": "byte", "color": "red"}
            ])

    def test_transform_missing_type_rejected(self):
        with pytest.raises(ValueError, match="transform missing 'type'"):
            _validate_watch_fields([{
                "name": "species",
                "offset": 0,
                "size": "short",
                "transform": {"values": {}},
            }])

    def test_transform_unknown_type_rejected(self):
        with pytest.raises(ValueError, match="unknown transform type"):
            _validate_watch_fields([{
                "name": "species",
                "offset": 0,
                "size": "short",
                "transform": {"type": "regex", "pattern": ".*"},
            }])

    def test_map_transform_missing_values_rejected(self):
        with pytest.raises(ValueError, match="requires 'values' dict"):
            _validate_watch_fields([{
                "name": "species",
                "offset": 0,
                "size": "short",
                "transform": {"type": "map"},
            }])

    def test_too_many_fields_rejected(self):
        fields = [{"name": f"f{i}", "offset": i, "size": "byte"} for i in range(65)]
        with pytest.raises(ValueError, match="at most 64"):
            _validate_watch_fields(fields)


class TestApplyTransform:
    def test_map_hit(self):
        t = {"type": "map", "values": {"393": "Piplup", "390": "Chimchar"}}
        assert _apply_transform(t, 393) == "Piplup"
        assert _apply_transform(t, 390) == "Chimchar"

    def test_map_miss_with_default(self):
        t = {"type": "map", "values": {"393": "Piplup"}, "default": "Unknown"}
        assert _apply_transform(t, 999) == "Unknown"

    def test_map_miss_without_default(self):
        t = {"type": "map", "values": {"393": "Piplup"}}
        assert _apply_transform(t, 999) is None

    def test_map_zero_key(self):
        t = {"type": "map", "values": {"0": "None/Empty"}, "default": "???"}
        assert _apply_transform(t, 0) == "None/Empty"

    def test_map_negative_key(self):
        t = {"type": "map", "values": {"-1": "Invalid"}, "default": "OK"}
        assert _apply_transform(t, -1) == "Invalid"

"""Loading YAML with positions, and the strictness rules that go with it."""

from __future__ import annotations

from pathlib import Path

import pytest

from aipcb.source import SourceError, load_yaml


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "f.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestPositions:
    def test_records_key_positions(self, tmp_path: Path) -> None:
        loaded = load_yaml(write(tmp_path, "nets:\n  VCC:\n    class: power\n"))
        loc = loaded.smap.get(("nets", "VCC"))
        assert loc is not None
        assert (loc.line, loc.col) == (2, 3)

    def test_points_at_the_key_not_the_value_block(self, tmp_path: Path) -> None:
        loaded = load_yaml(write(tmp_path, "components:\n  U1:\n    part: X\n"))
        loc = loaded.smap.get(("components", "U1"))
        assert loc is not None and loc.col == 3

    def test_falls_back_to_nearest_ancestor(self, tmp_path: Path) -> None:
        loaded = load_yaml(write(tmp_path, "nets:\n  VCC:\n    class: power\n"))
        exact = loaded.smap.get(("nets", "VCC"))
        missing = loaded.smap.get(("nets", "VCC", "voltage", "deeper"))
        assert missing == exact

    def test_sequence_indices_are_recorded(self, tmp_path: Path) -> None:
        loaded = load_yaml(write(tmp_path, "libraries:\n  - a.yaml\n  - b.yaml\n"))
        loc = loaded.smap.get(("libraries", 1))
        assert loc is not None and loc.line == 3


class TestStrictness:
    @pytest.mark.parametrize("word", ["NO", "ON", "OFF", "YES", "no", "yes", "y", "n"])
    def test_norway_problem(self, tmp_path: Path, word: str) -> None:
        """A net named NO must stay the string 'NO', not become False."""
        loaded = load_yaml(write(tmp_path, f"nets:\n  {word}: {{}}\n  x: {word}\n"))
        assert word in loaded.data["nets"]
        assert loaded.data["nets"]["x"] == word

    def test_real_booleans_still_work(self, tmp_path: Path) -> None:
        loaded = load_yaml(write(tmp_path, "a: true\nb: false\nc: True\n"))
        assert loaded.data == {"a": True, "b": False, "c": True}

    def test_duplicate_keys_are_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError) as excinfo:
            load_yaml(write(tmp_path, "a: 1\na: 2\n"))
        assert "duplicate key" in str(excinfo.value)
        assert excinfo.value.loc is not None and excinfo.value.loc.line == 2

    def test_tabs_get_a_useful_hint(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError) as excinfo:
            load_yaml(write(tmp_path, "a:\n\t- 1\n"))
        assert excinfo.value.hint is not None
        assert "tabs" in excinfo.value.hint

    def test_numbers_keep_their_types(self, tmp_path: Path) -> None:
        loaded = load_yaml(write(tmp_path, "i: 12\nf: 1.6\nn: null\nh: 0x10\n"))
        assert loaded.data == {"i": 12, "f": 1.6, "n": None, "h": 16}


class TestFailureModes:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="no such file"):
            load_yaml(tmp_path / "absent.yaml")

    def test_empty_file(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="empty"):
            load_yaml(write(tmp_path, ""))

    def test_top_level_must_be_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="expected a mapping"):
            load_yaml(write(tmp_path, "- one\n- two\n"))

    def test_syntax_error_reports_a_position(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError) as excinfo:
            load_yaml(write(tmp_path, "a: [1, 2\nb: 3\n"))
        assert excinfo.value.loc is not None

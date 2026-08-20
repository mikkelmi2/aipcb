"""The S-expression layer: parsing, writing, and the guarantees both must hold to."""

from __future__ import annotations

import pytest

from aipcb.kicad.sexpr import (
    Atom,
    SExprError,
    SNode,
    dump,
    dump_all,
    num,
    parse,
    parse_all,
    quoted,
    sym,
)


class TestParsing:
    def test_simple_node(self) -> None:
        node = parse('(version 20241229)')
        assert node.name == "version"
        assert node.value(0) == "20241229"

    def test_nested(self) -> None:
        node = parse('(a (b 1) (c "two"))')
        assert [c.name for c in node.children()] == ["b", "c"]
        assert node.get("c") == "two"

    def test_quoted_and_bare_are_distinguished(self) -> None:
        node = parse('(x yes "yes")')
        atoms = node.atoms()
        assert atoms[0] == Atom("yes", quoted=False)
        assert atoms[1] == Atom("yes", quoted=True)

    def test_escapes(self) -> None:
        node = parse(r'(x "a\"b\\c\nd")')
        assert node.value(0) == 'a"b\\c\nd'

    def test_empty_node(self) -> None:
        assert parse("(a)").items == []

    def test_repeated_children(self) -> None:
        node = parse("(a (b 1) (b 2) (b 3))")
        assert [c.value(0) for c in node.children("b")] == ["1", "2", "3"]
        assert node.child("b") is not None and node.child("b").value(0) == "1"

    @pytest.mark.parametrize(
        "text",
        [
            "(unterminated",
            '(x "unterminated',
            "(a) (b)",  # two roots
            "",
            ")",
            '("quoted-head" 1)',
        ],
    )
    def test_malformed_input_raises(self, text: str) -> None:
        with pytest.raises(SExprError):
            parse(text)

    def test_error_reports_position(self) -> None:
        with pytest.raises(SExprError) as excinfo:
            parse("(a\n  (b 1)\n  (c")
        assert excinfo.value.line == 3

    def test_multi_root_needs_parse_all(self) -> None:
        text = '(version 1)\n(rule "a" (constraint clearance))'
        with pytest.raises(SExprError):
            parse(text)
        nodes = parse_all(text)
        assert [n.name for n in nodes] == ["version", "rule"]


class TestWriting:
    def test_round_trip_is_lossless(self) -> None:
        text = '(kicad_pcb\n\t(version 20241229)\n\t(net 0 "")\n)'
        tree = parse(text)
        assert parse(dump(tree)) == tree

    def test_dump_is_deterministic(self) -> None:
        tree = parse('(a (b 1) (c "x"))')
        assert dump(tree) == dump(tree)
        assert dump(parse(dump(tree))) == dump(tree)

    def test_dump_all_round_trips(self) -> None:
        nodes = parse_all('(version 1)\n(rule "a" (constraint clearance))')
        assert parse_all(dump_all(nodes)) == nodes

    def test_quoting_survives(self) -> None:
        tree = SNode("x").add(quoted("a b"), sym("bare"))
        assert dump(tree, trailing_newline=False) == '(x "a b" bare)'

    def test_special_characters_are_escaped(self) -> None:
        tree = SNode("x").add(quoted('a"b\\c\nd'))
        assert parse(dump(tree)).value(0) == 'a"b\\c\nd'

    def test_nested_indentation_uses_tabs(self) -> None:
        tree = parse("(a (b (c 1)))")
        assert "\n\t(b" in dump(tree)


class TestNumberFormatting:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0"),
            (0.0, "0"),
            (-0.0, "0"),
            (1.6, "1.6"),
            (-1.27, "-1.27"),
            (1.0, "1"),
            (0.1 + 0.2, "0.3"),
            (1e-7, "0"),
            (127, "127"),
            (1234567.0, "1234567"),
        ],
    )
    def test_formats_like_kicad(self, value: float, expected: str) -> None:
        assert num(value).value == expected

    def test_never_uses_exponent_notation(self) -> None:
        assert "e" not in num(0.0000001).value
        assert "e" not in num(1e20).value


class TestNodeHelpers:
    def test_replace_appends_when_absent(self) -> None:
        node = parse("(a (b 1))")
        node.replace("c", SNode("c").add(sym("2")))
        assert node.get("c") == "2"

    def test_replace_swaps_first_match(self) -> None:
        node = parse("(a (b 1) (b 2))")
        node.replace("b", SNode("b").add(sym("9")))
        assert [c.value(0) for c in node.children("b")] == ["9", "2"]

    def test_remove_returns_count(self) -> None:
        node = parse("(a (b 1) (b 2) (c 3))")
        assert node.remove("b") == 2
        assert node.child("b") is None

    def test_get_returns_none_for_missing(self) -> None:
        assert parse("(a)").get("missing") is None

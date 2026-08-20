"""Lossless S-expression reader/writer for KiCad 8/9 files.

Design notes
------------
This module deliberately does *not* model KiCad's schema. It parses a file into a
generic tree of :class:`SNode` / :class:`Atom` and can write that tree back out.
Two properties matter and are enforced by the test-suite:

* **Lossless** -- ``parse(dump(parse(text))) == parse(text)`` for arbitrary KiCad
  files. No token is dropped, renamed, or invented. This is what lets ``aipcb
  build`` re-emit a board that a human has edited in KiCad without destroying the
  parts of the file we do not understand (milestone M6).
* **Deterministic** -- ``dump`` is a pure function of the tree. Same tree in, same
  bytes out, on every platform and Python version.

Atoms keep their original lexical form. A bare atom such as ``1.6`` is stored as
the string ``"1.6"`` and written back as ``1.6`` -- never normalised to ``1.6000``
or reparsed as a float. That is the only way to guarantee byte-stability without
reimplementing KiCad's number formatting.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Union

__all__ = [
    "Atom",
    "SExprError",
    "SNode",
    "dump",
    "dump_all",
    "num",
    "parse",
    "parse_all",
    "quoted",
    "sym",
]


class SExprError(ValueError):
    """Raised when an S-expression cannot be parsed."""

    def __init__(self, message: str, *, line: int, col: int) -> None:
        super().__init__(f"{message} (line {line}, column {col})")
        self.line = line
        self.col = col


@dataclass(frozen=True, slots=True)
class Atom:
    """A leaf token.

    ``value`` holds the *decoded* text (escape sequences resolved). ``quoted``
    records whether the token appeared in double quotes, because KiCad
    distinguishes ``yes`` (a symbol) from ``"yes"`` (a string) in some contexts.
    """

    value: str
    quoted: bool = False

    def __str__(self) -> str:
        return self.value


Item = Union[Atom, "SNode"]


@dataclass(slots=True)
class SNode:
    """A parenthesised list whose head is a bare symbol, e.g. ``(version 20241229)``."""

    name: str
    items: list[Item] = field(default_factory=list)

    # -- construction helpers -------------------------------------------------

    def add(self, *items: Item) -> SNode:
        """Append items and return ``self`` so calls can be chained."""
        self.items.extend(items)
        return self

    # -- read helpers ---------------------------------------------------------

    def children(self, name: str | None = None) -> Iterator[SNode]:
        """Yield child nodes, optionally filtered by ``name``."""
        for item in self.items:
            if isinstance(item, SNode) and (name is None or item.name == name):
                yield item

    def child(self, name: str) -> SNode | None:
        """Return the first child node named ``name``, or ``None``."""
        return next(self.children(name), None)

    def atoms(self) -> list[Atom]:
        """Return the leaf tokens directly under this node."""
        return [i for i in self.items if isinstance(i, Atom)]

    def value(self, index: int = 0) -> str | None:
        """Return the decoded text of the ``index``-th leaf token."""
        leaves = self.atoms()
        return leaves[index].value if index < len(leaves) else None

    def get(self, name: str, index: int = 0) -> str | None:
        """Return the ``index``-th leaf of child ``name`` -- ``(uuid "x")`` -> ``"x"``."""
        node = self.child(name)
        return None if node is None else node.value(index)

    def replace(self, name: str, node: SNode) -> None:
        """Replace the first child named ``name``, appending it if absent."""
        for i, item in enumerate(self.items):
            if isinstance(item, SNode) and item.name == name:
                self.items[i] = node
                return
        self.items.append(node)

    def remove(self, name: str) -> int:
        """Drop every child named ``name``; return how many were removed."""
        before = len(self.items)
        self.items = [
            i for i in self.items if not (isinstance(i, SNode) and i.name == name)
        ]
        return before - len(self.items)


# ---------------------------------------------------------------------------
# constructors
# ---------------------------------------------------------------------------


def sym(text: str) -> Atom:
    """A bare symbol: ``yes``, ``F.Cu``, ``20241229``."""
    return Atom(text, quoted=False)


def quoted(text: str) -> Atom:
    """A quoted string: ``"R1"``."""
    return Atom(text, quoted=True)


def num(value: float | int, places: int = 6) -> Atom:
    """Format a number the way KiCad does: no exponent, no trailing zeros.

    KiCad writes ``0``, ``1.6`` and ``-1.27`` rather than ``0.0`` or ``1.6e+00``.
    Rounding to ``places`` (default 6, i.e. nanometre resolution in mm) keeps
    floating-point noise out of the output so builds stay byte-stable.
    """
    if isinstance(value, int):
        return Atom(str(value), quoted=False)
    rounded = round(float(value), places)
    if rounded == 0:  # collapse -0.0
        rounded = 0.0
    text = f"{rounded:.{places}f}".rstrip("0").rstrip(".")
    return Atom(text or "0", quoted=False)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

_WHITESPACE = " \t\r\n"
_DELIMITERS = _WHITESPACE + "()"


class _Reader:
    __slots__ = ("line", "line_start", "pos", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.line = 1
        self.line_start = 0

    def error(self, message: str) -> SExprError:
        return SExprError(message, line=self.line, col=self.pos - self.line_start + 1)

    def skip_whitespace(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch == "\n":
                self.line += 1
                self.pos += 1
                self.line_start = self.pos
            elif ch in " \t\r":
                self.pos += 1
            else:
                return

    def read_node(self) -> SNode:
        if self.pos >= len(self.text) or self.text[self.pos] != "(":
            raise self.error("expected '('")
        self.pos += 1
        self.skip_whitespace()

        name_atom = self.read_atom()
        if name_atom.quoted:
            raise self.error("list head must be a bare symbol, not a quoted string")
        node = SNode(name_atom.value)

        while True:
            self.skip_whitespace()
            if self.pos >= len(self.text):
                raise self.error(f"unterminated list '({node.name}'")
            ch = self.text[self.pos]
            if ch == ")":
                self.pos += 1
                return node
            if ch == "(":
                node.items.append(self.read_node())
            else:
                node.items.append(self.read_atom())

    def read_atom(self) -> Atom:
        if self.pos >= len(self.text):
            raise self.error("expected a token")
        if self.text[self.pos] == '"':
            return self.read_string()

        start = self.pos
        text, n = self.text, len(self.text)
        while self.pos < n and text[self.pos] not in _DELIMITERS:
            self.pos += 1
        if self.pos == start:
            raise self.error(f"expected a token, found {text[start]!r}")
        return Atom(text[start:self.pos], quoted=False)

    def read_string(self) -> Atom:
        self.pos += 1  # opening quote
        out: list[str] = []
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch == '"':
                self.pos += 1
                return Atom("".join(out), quoted=True)
            if ch == "\\":
                self.pos += 1
                if self.pos >= n:
                    break
                esc = text[self.pos]
                out.append(_UNESCAPE.get(esc, esc))
                self.pos += 1
                continue
            if ch == "\n":
                self.line += 1
                self.line_start = self.pos + 1
            out.append(ch)
            self.pos += 1
        raise self.error("unterminated string")


_UNESCAPE = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
_ESCAPE = {"\n": "\\n", "\r": "\\r", "\t": "\\t", '"': '\\"', "\\": "\\\\"}


def parse(text: str) -> SNode:
    """Parse a complete KiCad file into a tree. Raises :class:`SExprError`."""
    reader = _Reader(text)
    reader.skip_whitespace()
    node = reader.read_node()
    reader.skip_whitespace()
    if reader.pos != len(text):
        raise reader.error(
            "trailing content after the top-level expression "
            "(use parse_all() for multi-root formats such as .kicad_dru)"
        )
    return node


def parse_all(text: str) -> list[SNode]:
    """Parse a file holding several top-level expressions, as ``.kicad_dru`` does."""
    reader = _Reader(text)
    nodes: list[SNode] = []
    reader.skip_whitespace()
    while reader.pos < len(text):
        nodes.append(reader.read_node())
        reader.skip_whitespace()
    return nodes


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _escape(value: str) -> str:
    return "".join(_ESCAPE.get(ch, ch) for ch in value)


def _atom_text(atom: Atom) -> str:
    return f'"{_escape(atom.value)}"' if atom.quoted else atom.value


#: Nodes whose contents KiCad keeps on a single line regardless of length.
INLINE_NODES = frozenset(
    {
        "at", "xy", "xyz", "start", "end", "mid", "center", "size", "offset",
        "scale", "rotate", "width", "thickness", "layer", "uuid", "type",
        "version", "generator", "generator_version", "tedit", "tstamp",
        "descr", "tags", "attr", "hide", "unlocked", "locked", "fields_autoplaced",
        "in_bom", "on_board", "dnp", "exclude_from_sim", "reference", "value",
        "datasheet", "footprint", "net", "net_name", "pinfunction", "pintype",
        "number", "name", "diameter", "drill", "roundrect_rratio", "solder_mask_margin",
        "clearance", "trace_width", "via_dia", "via_drill", "uvia_dia", "uvia_drill",
        "justify", "bold", "italic", "face", "color", "fill", "pts", "paper",
        "title", "date", "rev", "company", "comment", "path", "sheetname",
        "sheetfile", "property", "lib_id", "lib_name", "unit", "convert",
        "mirror", "pin_names", "pin_numbers", "length", "shape", "angle",
        "radius", "layers", "chamfer", "chamfer_ratio", "zone_connect",
        "thermal_width", "thermal_gap", "die_length", "free", "island",
        "min_thickness", "filled_areas_thickness", "keep_end_layers",
        "remove_unused_layers", "teardrops", "tstamps", "embedded_fonts",
    }
)

#: Nodes that are inline only when every item is a leaf (no nested lists).
_MAX_INLINE_ITEMS = 8


def _is_inline(node: SNode) -> bool:
    if any(isinstance(item, SNode) for item in node.items):
        return False
    if node.name in INLINE_NODES:
        return True
    return len(node.items) <= _MAX_INLINE_ITEMS


def _write(node: SNode, depth: int, out: list[str], indent: str) -> None:
    pad = indent * depth
    head = f"{pad}({node.name}"
    if not node.items:
        out.append(head + ")")
        return
    if _is_inline(node):
        body = " ".join(_atom_text(i) for i in node.items if isinstance(i, Atom))
        out.append(f"{head} {body})")
        return

    out.append(head)
    inner = indent * (depth + 1)
    for item in node.items:
        if isinstance(item, Atom):
            out.append(inner + _atom_text(item))
        else:
            _write(item, depth + 1, out, indent)
    out.append(pad + ")")


def dump(node: SNode, *, indent: str = "\t", trailing_newline: bool = True) -> str:
    """Serialise a tree. Pure function of ``node`` -- identical input, identical bytes."""
    out: list[str] = []
    _write(node, 0, out, indent)
    text = "\n".join(out)
    return text + "\n" if trailing_newline else text


def dump_all(nodes: list[SNode], *, indent: str = "\t") -> str:
    """Serialise several top-level expressions, blank-line separated."""
    return "\n".join(dump(n, indent=indent, trailing_newline=False) for n in nodes) + "\n"

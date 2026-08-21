"""Reconciling a hand-moved part with the source that fixed it.

Somebody opens the board in KiCad and nudges the USB connector half a millimetre,
because that is what lines it up with the enclosure they have in front of them. Two
things are now true and they disagree: the YAML says one position and the board says
another.

`aipcb` refuses to pick a side silently. A ``fixed:`` placement is mechanical law,
so ``aipcb build`` puts the part back where the source says and *reports* the
disagreement. This module is the other direction: it reads the drift back out of the
board and writes it into the YAML, in place, so the source becomes the thing the
person actually meant.

The edit is surgical. The file is rewritten line by line rather than round-tripped
through a YAML dumper, because a dumper would return a file with every comment gone
and every block re-flowed -- and the comments in a mechanical block are the part
that says *why*, which is the last thing that should be lost to a tool being
helpful.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from aipcb.compile.frame import BoardFrame
from aipcb.kicad.sexpr import SNode
from aipcb.netlist import Netlist

__all__ = ["Drift", "apply_drift", "find_drift", "format_entry"]

#: How far a part has to have moved before it counts as moved. KiCad writes four
#: decimal places, so anything smaller is the file format, not a person.
DRIFT_EPSILON = 1e-4


@dataclass(frozen=True, slots=True)
class Drift:
    """One part that sits somewhere other than where the source puts it."""

    refdes: str
    level: str
    """``fixed``, ``edge`` or ``region`` -- what the source says about it today."""
    source: tuple[float, float, float]
    """Where the source puts it: x, y, rotation, in the board frame."""
    board: tuple[float, float, float]
    """Where the board has it, in the same frame."""

    @property
    def distance(self) -> float:
        return math.dist(self.source[:2], self.board[:2])

    @property
    def rotation_delta(self) -> float:
        turned = abs(self.board[2] - self.source[2]) % 360
        return min(turned, 360 - turned)

    def describe(self) -> str:
        parts = []
        if self.distance >= DRIFT_EPSILON:
            parts.append(f"moved {self.distance:.3f} mm")
        if self.rotation_delta >= DRIFT_EPSILON:
            parts.append(f"turned {self.rotation_delta:g}°")
        moved = " and ".join(parts) or "changed"
        return (
            f"{self.refdes} {moved} from its {self.level} position in source: "
            f"({_number(self.source[0])}, {_number(self.source[1])}) "
            f"-> ({_number(self.board[0])}, {_number(self.board[1])})"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "refdes": self.refdes,
            "level": self.level,
            "source": {"x": self.source[0], "y": self.source[1], "rot": self.source[2]},
            "board": {"x": self.board[0], "y": self.board[1], "rot": self.board[2]},
            "distance_mm": round(self.distance, 4),
            "rotation_deg": round(self.rotation_delta, 4),
        }


def find_drift(
    netlist: Netlist,
    board: SNode,
    frame: BoardFrame,
    generated: dict[str, tuple[float, float, float]],
) -> list[Drift]:
    """Which mechanically placed parts sit somewhere other than where they belong.

    ``generated`` is where a fresh placement pass puts each part, in KiCad
    coordinates -- for a ``fixed`` part that is the source's own coordinate, and for
    an ``edge`` or ``region`` part it is the position the placer chose from the set
    the source allowed. Both are "what the source means", which is what drift is
    measured against.
    """
    placed = _board_positions(board)
    drifts: list[Drift] = []
    for refdes in sorted(netlist.placement):
        entry = netlist.placement[refdes]
        here, there = generated.get(refdes), placed.get(refdes)
        if here is None or there is None:
            continue
        source = (*frame.to_source(here[:2]), here[2])
        actual = (*frame.to_source(there[:2]), there[2])
        drift = Drift(refdes, entry.level, source, actual)
        if drift.distance < DRIFT_EPSILON and drift.rotation_delta < DRIFT_EPSILON:
            continue
        drifts.append(drift)
    return drifts


def _board_positions(board: SNode) -> dict[str, tuple[float, float, float]]:
    out: dict[str, tuple[float, float, float]] = {}
    for footprint in board.children("footprint"):
        at = footprint.child("at")
        reference = next(
            (p.value(1) for p in footprint.children("property") if p.value(0) == "Reference"),
            None,
        )
        if at is None or reference is None:
            continue
        out[reference] = (
            float(at.value(0) or 0), float(at.value(1) or 0), float(at.value(2) or 0)
        )
    return out


# ---------------------------------------------------------------------------
# rewriting the source
# ---------------------------------------------------------------------------


def format_entry(drift: Drift) -> str:
    """The ``fixed:`` mapping this drift should become, without its indentation."""
    x, y, rot = drift.board
    body = f"x: {_number(x)}, y: {_number(y)}"
    if abs(rot) >= DRIFT_EPSILON:
        body += f", rot: {_number(rot)}"
    return f"fixed: {{ {body} }}"


def apply_drift(text: str, netlist: Netlist, drifts: list[Drift]) -> tuple[str, list[str]]:
    """Rewrite a design's ``placement:`` block with the positions from the board.

    Returns the new text and the reference designators actually changed. Every other
    line of the file -- comments, ``reason:``, the rest of the design -- is left
    exactly as it was, because a mechanical block's comments are the half that says
    why the position is what it is.

    An ``edge`` or ``region`` entry becomes a ``fixed`` one. That is what moving the
    part by hand *means*: the position is no longer something the placer may choose,
    and pretending otherwise would put the part back where it was on the next build.
    """
    lines = text.splitlines(keepends=True)
    changed: list[str] = []
    # Rewritten from the bottom up so that earlier line numbers stay valid.
    for drift in sorted(drifts, key=lambda d: d.refdes, reverse=True):
        name = netlist.mech_names.get(drift.refdes, drift.refdes)
        span = _entry_span(lines, name)
        if span is None:
            continue
        start, end = span
        replaced = _replace_level(lines, start, end, format_entry(drift))
        if replaced is None:
            continue
        lines[start:end] = replaced
        changed.append(drift.refdes)
    return "".join(lines), sorted(changed)


def _entry_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """The line range of one entry inside the top-level ``placement:`` block."""
    block = _block_span(lines, "placement:")
    if block is None:
        return None
    start, end = block
    key = re.compile(rf"^(\s+){re.escape(name)}\s*:\s*$")
    for index in range(start, end):
        match = key.match(lines[index].rstrip("\n"))
        if match is None:
            continue
        indent = len(match.group(1))
        stop = index + 1
        while stop < end and _is_deeper(lines[stop], indent):
            stop += 1
        return (index, stop)
    return None


def _block_span(lines: list[str], header: str) -> tuple[int, int] | None:
    for index, line in enumerate(lines):
        if line.rstrip("\n") != header.rstrip():
            continue
        stop = index + 1
        while stop < len(lines) and _is_deeper(lines[stop], 0):
            stop += 1
        return (index + 1, stop)
    return None


def _is_deeper(line: str, indent: int) -> bool:
    """Whether a line belongs inside a block indented by ``indent`` spaces."""
    if not line.strip():
        return True
    return len(line) - len(line.lstrip(" ")) > indent


def _replace_level(
    lines: list[str], start: int, end: int, replacement: str
) -> list[str] | None:
    """Swap the ``fixed:``/``edge:``/``region:`` key of one entry for a new one."""
    level = re.compile(r"^(\s+)(fixed|edge|region)\s*:")
    for index in range(start, end):
        match = level.match(lines[index])
        if match is None:
            continue
        indent = match.group(1)
        stop = index + 1
        while stop < end and _is_deeper(lines[stop], len(indent)):
            stop += 1
        return [*lines[start:index], f"{indent}{replacement}\n", *lines[stop:end]]
    return None


def read_board(path: Path) -> SNode:
    """Parse a ``.kicad_pcb`` from disk."""
    from aipcb.kicad.sexpr import parse

    return parse(path.read_text(encoding="utf-8"))


def _number(value: float) -> str:
    """A coordinate as the source would write it: no trailing zeros, no exponent."""
    rounded = round(value, 4)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"

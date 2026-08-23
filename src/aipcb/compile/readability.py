# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Measuring how readable a generated schematic is.

"Readable" is a judgement, and this module does not pretend otherwise. What it does
is measure the four things that make the judgement go the wrong way, so that a
change to the placement can be argued from numbers rather than from taste:

* **Crossings** -- wires that cross other wires, and wires that run through a
  symbol's body. The second is the one that actually happens in a netlist-first
  schematic, because the wires are stubs and stubs are short.
* **Collisions** -- drawn things whose boxes overlap: two labels on top of each
  other, a value written across a ground symbol. This is where the visual noise on
  a machine-placed sheet comes from, and it is the number that moved furthest in
  M14.
* **Copper-free wire length** -- the total length of wire on the sheet. Lower is
  usually better; it goes *up* when stubs are staggered to stop labels colliding,
  which is a trade this module measures rather than hides.
* **Cohesion** -- how far each decoupling capacitor sits from the nearest pin of
  the IC it is declared ``for:``. This is the single number that says whether the
  source's intent reached the drawing.

Everything is read back out of the ``.kicad_sch`` itself rather than taken from the
planner, so the same measurement runs against a sheet built by any version of this
tool -- which is what makes a before-and-after table possible at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from aipcb.compile.geometry import Point, place_point
from aipcb.kicad.sexpr import SNode

__all__ = [
    "Metrics",
    "PlacedSymbol",
    "measure_schematic",
    "read_symbols",
]

#: Width of one character at the 1.27 mm text size everything on the sheet uses.
CHAR_W = 0.85

#: Height of one line of that text, with the leading a plot actually gives it.
TEXT_H = 1.9

#: Extra width a global label's box adds around its text.
LABEL_BOX = 2.54


@dataclass(frozen=True, slots=True)
class Box:
    left: float
    top: float
    right: float
    bottom: float

    def overlaps(self, other: Box, slack: float = 0.0) -> bool:
        return not (
            self.right - slack <= other.left
            or other.right - slack <= self.left
            or self.bottom - slack <= other.top
            or other.bottom - slack <= self.top
        )

    @property
    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)


@dataclass(frozen=True, slots=True)
class PlacedSymbol:
    """One symbol instance as the file records it."""

    refdes: str
    lib_id: str
    origin: Point
    rotation: float
    pins: tuple[Point, ...]
    body: Box

    @property
    def is_scaffolding(self) -> bool:
        """Power symbols and flags: real on the sheet, absent from the design."""
        return self.refdes.startswith("#")


@dataclass(slots=True)
class Metrics:
    """What one sheet measures."""

    components: int = 0
    symbols: int = 0
    sheets: int = 1
    paper: str = ""
    wire_crossings: int = 0
    wire_through_symbol: int = 0
    collisions: int = 0
    wire_length_mm: float = 0.0
    labels: int = 0
    power_symbols: int = 0
    decoupling_pairs: int = 0
    decoupling_mean_mm: float = 0.0
    decoupling_max_mm: float = 0.0
    drawing_area_mm2: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": self.components,
            "symbols": self.symbols,
            "sheets": self.sheets,
            "paper": self.paper,
            "wire_crossings": self.wire_crossings,
            "wire_through_symbol": self.wire_through_symbol,
            "collisions": self.collisions,
            "wire_length_mm": round(self.wire_length_mm, 2),
            "labels": self.labels,
            "power_symbols": self.power_symbols,
            "decoupling_pairs": self.decoupling_pairs,
            "decoupling_mean_mm": round(self.decoupling_mean_mm, 2),
            "decoupling_max_mm": round(self.decoupling_max_mm, 2),
            "drawing_area_mm2": round(self.drawing_area_mm2, 1),
        }


# ---------------------------------------------------------------------------
# reading a sheet back
# ---------------------------------------------------------------------------


def _library(root: SNode) -> dict[str, tuple[tuple[float, float], ...]]:
    """Pin positions per embedded symbol, in library space."""
    pins: dict[str, list[tuple[float, float]]] = {}
    lib = root.child("lib_symbols")
    if lib is None:
        return {}
    for symbol in lib.children("symbol"):
        name = symbol.value(0) or ""
        found: list[tuple[float, float]] = []
        for unit in symbol.children("symbol"):
            for pin in unit.children("pin"):
                at = pin.child("at")
                if at is None:
                    continue
                atoms = at.atoms()
                if len(atoms) >= 2:
                    found.append((float(atoms[0].value), float(atoms[1].value)))
        pins[name] = found
    return {name: tuple(found) for name, found in pins.items()}


def _library_bodies(root: SNode) -> dict[str, Box]:
    """The drawn extent of each embedded symbol, in library space."""
    bodies: dict[str, Box] = {}
    lib = root.child("lib_symbols")
    if lib is None:
        return {}
    for symbol in lib.children("symbol"):
        name = symbol.value(0) or ""
        xs: list[float] = []
        ys: list[float] = []

        def collect(
            node: SNode, xs: list[float] = xs, ys: list[float] = ys
        ) -> None:
            if node.name in ("xy", "start", "end", "mid", "center"):
                atoms = node.atoms()
                if len(atoms) >= 2:
                    try:
                        xs.append(float(atoms[0].value))
                        ys.append(float(atoms[1].value))
                    except ValueError:  # pragma: no cover - malformed library
                        pass
            for child in node.children():
                collect(child, xs, ys)

        for unit in symbol.children("symbol"):
            for shape in unit.children():
                if shape.name != "pin":
                    collect(shape, xs, ys)
        bodies[name] = (
            Box(min(xs), -max(ys), max(xs), -min(ys)) if xs else Box(0, 0, 0, 0)
        )
    return bodies


def read_symbols(root: SNode) -> list[PlacedSymbol]:
    """Every placed symbol on a sheet, with its pins already in sheet space."""
    pin_library = _library(root)
    bodies = _library_bodies(root)
    placed: list[PlacedSymbol] = []
    for node in root.children("symbol"):
        lib_id = node.get("lib_id") or ""
        at = node.child("at")
        if at is None:
            continue
        atoms = at.atoms()
        origin = Point(float(atoms[0].value), float(atoms[1].value))
        rotation = float(atoms[2].value) if len(atoms) > 2 else 0.0
        refdes = ""
        for prop in node.children("property"):
            keys = prop.atoms()
            if len(keys) >= 2 and keys[0].value == "Reference":
                refdes = keys[1].value
        pins = tuple(
            place_point(origin, rotation, x, y).rounded()
            for x, y in pin_library.get(lib_id, ())
        )
        raw = bodies.get(lib_id, Box(0, 0, 0, 0))
        corners = [
            place_point(origin, rotation, x, -y)
            for x, y in (
                (raw.left, raw.top), (raw.left, raw.bottom),
                (raw.right, raw.top), (raw.right, raw.bottom),
            )
        ]
        body = Box(
            min(p.x for p in corners), min(p.y for p in corners),
            max(p.x for p in corners), max(p.y for p in corners),
        )
        placed.append(PlacedSymbol(refdes, lib_id, origin, rotation, pins, body))
    return placed


def _wires(root: SNode) -> list[tuple[Point, Point]]:
    segments: list[tuple[Point, Point]] = []
    for wire in root.children("wire"):
        pts = wire.child("pts")
        if pts is None:
            continue
        points = [
            Point(float(xy.atoms()[0].value), float(xy.atoms()[1].value))
            for xy in pts.children("xy")
            if len(xy.atoms()) >= 2
        ]
        segments.extend(pairwise(points))
    return segments


def _label_boxes(root: SNode) -> list[tuple[str, Box]]:
    boxes: list[tuple[str, Box]] = []
    for kind in ("global_label", "label", "hierarchical_label"):
        for node in root.children(kind):
            text = node.value(0) or ""
            at = node.child("at")
            if at is None:
                continue
            atoms = at.atoms()
            x, y = float(atoms[0].value), float(atoms[1].value)
            angle = float(atoms[2].value) if len(atoms) > 2 else 0.0
            length = CHAR_W * len(text) + LABEL_BOX
            boxes.append((text, _directed_box(Point(x, y), angle, length)))
    return boxes


def _text_boxes(root: SNode, symbols: list[PlacedSymbol]) -> list[tuple[str, Box]]:
    """Visible reference and value text, which is what lands on other things."""
    boxes: list[tuple[str, Box]] = []
    for node, placed in zip(root.children("symbol"), symbols, strict=False):
        for prop in node.children("property"):
            atoms = prop.atoms()
            if len(atoms) < 2 or atoms[0].value not in ("Reference", "Value"):
                continue
            effects = prop.child("effects")
            if effects is not None and effects.child("hide") is not None:
                continue
            at = prop.child("at")
            if at is None:
                continue
            coords = at.atoms()
            x, y = float(coords[0].value), float(coords[1].value)
            text = atoms[1].value
            width = CHAR_W * len(text)
            justify = ""
            if effects is not None and (j := effects.child("justify")) is not None:
                justify = " ".join(a.value for a in j.atoms())
            left = x if "left" in justify else x - width / 2
            boxes.append(
                (
                    placed.refdes,
                    Box(left, y - TEXT_H / 2, left + width, y + TEXT_H / 2),
                )
            )
    return boxes


def _directed_box(at: Point, angle: float, length: float) -> Box:
    """A label's box, running from its anchor in the direction it is written."""
    dx, dy = math.cos(math.radians(angle)), -math.sin(math.radians(angle))
    end = Point(at.x + dx * length, at.y + dy * length)
    half = TEXT_H / 2
    return Box(
        min(at.x, end.x) - (half if abs(dx) < 0.5 else 0.0),
        min(at.y, end.y) - (half if abs(dy) < 0.5 else 0.0),
        max(at.x, end.x) + (half if abs(dx) < 0.5 else 0.0),
        max(at.y, end.y) + (half if abs(dy) < 0.5 else 0.0),
    )


# ---------------------------------------------------------------------------
# geometry predicates
# ---------------------------------------------------------------------------


def _side(a: Point, b: Point, p: Point) -> float:
    return (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x)


def _crosses(a: tuple[Point, Point], b: tuple[Point, Point]) -> bool:
    """A proper crossing: the two segments meet somewhere that is not an endpoint.

    Two stubs meeting at a shared pin are connected, not crossed, so shared
    endpoints do not count -- otherwise every junction on the sheet would be
    reported as visual noise.
    """
    shared = {a[0].rounded(3), a[1].rounded(3)} & {b[0].rounded(3), b[1].rounded(3)}
    if shared:
        return False
    d1, d2 = _side(*a, b[0]), _side(*a, b[1])
    d3, d4 = _side(*b, a[0]), _side(*b, a[1])
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _segment_hits_box(a: Point, b: Point, box: Box) -> bool:
    """Whether a segment passes through a box, ignoring a mere touch at its edge."""
    inset = Box(box.left + 0.01, box.top + 0.01, box.right - 0.01, box.bottom - 0.01)
    if inset.right <= inset.left or inset.bottom <= inset.top:
        return False
    for point in (a, b):
        if inset.left < point.x < inset.right and inset.top < point.y < inset.bottom:
            return True
    edges = (
        (Point(inset.left, inset.top), Point(inset.right, inset.top)),
        (Point(inset.right, inset.top), Point(inset.right, inset.bottom)),
        (Point(inset.right, inset.bottom), Point(inset.left, inset.bottom)),
        (Point(inset.left, inset.bottom), Point(inset.left, inset.top)),
    )
    return any(_crosses((a, b), edge) for edge in edges)


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------


def measure_schematic(
    root: SNode, decoupling: dict[str, str] | None = None
) -> Metrics:
    """Measure one sheet.

    ``decoupling`` maps a capacitor's reference designator to the reference
    designator of the component it is declared ``for:``. It comes from the netlist,
    because the sheet does not record intent -- only where things ended up.
    """
    symbols = read_symbols(root)
    wires = _wires(root)
    metrics = Metrics(
        symbols=len(symbols),
        components=sum(1 for s in symbols if not s.is_scaffolding),
        paper=root.get("paper") or "",
        labels=sum(1 for _ in root.children("global_label")),
        power_symbols=sum(1 for s in symbols if s.refdes.startswith("#PWR")),
    )

    for index, first in enumerate(wires):
        for second in wires[index + 1 :]:
            if _crosses(first, second):
                metrics.wire_crossings += 1
        metrics.wire_length_mm += math.dist(first[0], first[1])

    bodies = [s for s in symbols if s.body.area > 0.01]
    for start, end in wires:
        for placed in bodies:
            # A stub leaving its own symbol starts on that symbol's own pin, so a
            # pin that belongs to this body does not count as running through it.
            if any(
                math.isclose(p.x, start.x, abs_tol=0.01)
                and math.isclose(p.y, start.y, abs_tol=0.01)
                for p in placed.pins
            ) or any(
                math.isclose(p.x, end.x, abs_tol=0.01)
                and math.isclose(p.y, end.y, abs_tol=0.01)
                for p in placed.pins
            ):
                continue
            if _segment_hits_box(start, end, placed.body):
                metrics.wire_through_symbol += 1

    # A label is owned by nothing: two labels reading the same net name that land on
    # top of each other are still two things a reader has to untangle.
    drawn: list[tuple[str, Box]] = [
        *[("", box) for _, box in _label_boxes(root)],
        *_text_boxes(root, symbols),
        *[(s.refdes, s.body) for s in symbols if s.body.area > 0.01],
    ]
    for index, (owner, box) in enumerate(drawn):
        for other_owner, other in drawn[index + 1 :]:
            if owner and owner == other_owner:
                continue
            if box.overlaps(other, slack=0.2):
                metrics.collisions += 1

    if symbols:
        left = min(min(s.body.left, s.origin.x) for s in symbols)
        top = min(min(s.body.top, s.origin.y) for s in symbols)
        right = max(max(s.body.right, s.origin.x) for s in symbols)
        bottom = max(max(s.body.bottom, s.origin.y) for s in symbols)
        metrics.drawing_area_mm2 = (right - left) * (bottom - top)

    by_ref = {s.refdes: s for s in symbols}
    distances: list[float] = []
    for cap, host in sorted((decoupling or {}).items()):
        cap_symbol = by_ref.get(cap)
        host_symbol = by_ref.get(host)
        if cap_symbol is None or host_symbol is None or not host_symbol.pins:
            continue
        distances.append(
            min(math.dist(cap_symbol.origin, pin) for pin in host_symbol.pins)
        )
    if distances:
        metrics.decoupling_pairs = len(distances)
        metrics.decoupling_mean_mm = sum(distances) / len(distances)
        metrics.decoupling_max_mm = max(distances)
    return metrics

"""Whether a slice's return path is one conductor, asked before the solver is.

M13.6 spent four solver runs proving one sentence: **a slice's return path has to be
a single connected conductor.** ``examples/pcie-sata``'s ``REFCLK`` slice held a
tenth of its energy in a resonance for forty thousand timesteps because it did not
have one -- ``In2.Cu`` carried ``P3V3`` and touched no via, no pad and no port, and
``SATA1_TX``'s window fell between two rows of an 8 mm stitching grid and reached
the solver with its two pours and both planes mutually isolated. Neither showed up
as an error. Both produced a number, and the number was wrong.

This module is that sentence written as a check, and M13.7 wires it into slice
generation so the same class of defect fails loudly instead of simulating quietly.
Two clauses:

* **Every sheet of area copper is bonded to the others.** A zone declared on three
  layers is *three* sheets until a via joins them, so it is exploded per layer and
  the vias are what put it back together. Anything left in its own island is
  floating, and :func:`floating_copper` names it with its net, its layer and how
  many square millimetres are at no defined potential.
* **The reference the pair is measured against is one of them.** A controlled-
  impedance class names a ``reference:`` layer; :func:`unbonded_reference` asks
  whether that layer carries copper in the bonded conductor at all.

**What it is measured on, and the approximation that buys.** The slice is checked
before it is filled -- there is no ``kicad-cli`` run and no ``pcbnew`` in the loop
at generation time -- so a zone's copper is its *outline*, not the filled polygon
with the tracks' clearance cut out of it. That is enough to answer "is this sheet
tied to anything", which is the question M13.6's defect turned on, and it is not
enough to answer "is this sheet in one piece once it is filled". The second question
is :mod:`aipcb.checks.planes`'s, it runs on the real board where the fill exists,
and the two are deliberately not merged: this one has to be cheap enough to run on
every slice, every time.

Connectivity is net-gated, which is KiCad's own rule: two pieces of copper are
joined when they carry the same net, share a layer and overlap. A GND via inside a
GND pour ties it; a signal via crossing the same pour does not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aipcb.kicad.sexpr import SNode

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from shapely.geometry.base import BaseGeometry

__all__ = [
    "Floating",
    "ReturnPath",
    "inspect_return_path",
]

Point = tuple[float, float]

#: Copper smaller than this is rounding, not a sheet. Matches the epsilon
#: :mod:`aipcb.checks.planes` measures islands against, for the same reason.
_AREA_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class Floating:
    """One sheet of area copper the slice connects to nothing."""

    net: str
    """The net it carries. ``P3V3`` on the eleven slices M13.6 measured."""
    layer: str
    area_mm2: float

    def describe(self) -> str:
        carried = self.net or "no net"
        return f"{self.layer} ({carried}), {self.area_mm2:.0f} mm2"


@dataclass(frozen=True, slots=True)
class _Piece:
    """One conductor on one layer: a zone's outline, a track, a via barrel, a pad."""

    net: str
    layers: frozenset[str]
    shape: BaseGeometry
    zone_area_mm2: float
    """Non-zero only for zones, which are the copper this module reports about."""
    layer_name: str
    """The single layer a zone piece was exploded onto; empty for everything else."""


def _polygon(pts: SNode | None) -> BaseGeometry | None:
    from shapely.geometry import Polygon as ShapelyPolygon

    if pts is None:
        return None
    points = [
        (float(xy.value(0) or 0), float(xy.value(1) or 0)) for xy in pts.children("xy")
    ]
    if len(points) < 3:
        return None
    shape = ShapelyPolygon(points)
    if not shape.is_valid:
        shape = shape.buffer(0)
    return None if shape.is_empty else shape


def _zone_layers(zone: SNode) -> list[str]:
    node = zone.child("layers") or zone.child("layer")
    return [str(atom.value) for atom in node.atoms()] if node is not None else []


def _via_span(via: SNode, metals: Sequence[str]) -> frozenset[str]:
    """Every copper layer a via passes through, not just the two it names.

    A through via says ``(layers "F.Cu" "B.Cu")`` and shorts every plane between
    them. Reading only the two named layers is how a stitching grid looks like it
    ties nothing to the inner planes.
    """
    node = via.child("layers")
    named = [str(atom.value) for atom in node.atoms()] if node is not None else []
    indices = [metals.index(name) for name in named if name in metals]
    if len(indices) < 2:
        return frozenset(named)
    return frozenset(metals[min(indices) : max(indices) + 1])


def _outline(board: SNode) -> BaseGeometry | None:
    """The slice's own board outline, as a box around its ``Edge.Cuts`` lines.

    Zones are carried into a slice with the *board's* polygon, which is far bigger
    than the window and is clipped to the outline at fill time. Reporting "3 082 mm2
    of floating copper" for a 166 mm2 slice would be true of the polygon and useless
    to a reader, so the areas this module reports are what the outline keeps.
    """
    from shapely.geometry import box

    xs: list[float] = []
    ys: list[float] = []
    for line in board.children("gr_line"):
        if str(line.get("layer") or "") != "Edge.Cuts":
            continue
        for end in ("start", "end"):
            node = line.child(end)
            if node is not None:
                xs.append(float(node.value(0) or 0))
                ys.append(float(node.value(1) or 0))
    if len(xs) < 2:
        return None
    return box(min(xs), min(ys), max(xs), max(ys))


def _pieces(board: SNode, metals: Sequence[str]) -> list[_Piece]:
    from shapely.geometry import LineString
    from shapely.geometry import Point as ShapelyPoint

    names = {
        int(n.value(0) or 0): str(n.value(1) or "") for n in board.children("net")
    }
    outline = _outline(board)
    out: list[_Piece] = []

    for zone in board.children("zone"):
        if zone.child("keepout") is not None:
            continue
        shape = _polygon((zone.child("polygon") or SNode("x")).child("pts"))
        if shape is not None and outline is not None:
            shape = shape.intersection(outline)
        if shape is None or shape.is_empty or shape.area <= _AREA_EPSILON:
            continue
        net = str(zone.get("net_name") or "")
        for layer in _zone_layers(zone):
            out.append(
                _Piece(
                    net=net,
                    layers=frozenset({layer}),
                    shape=shape,
                    zone_area_mm2=float(shape.area),
                    layer_name=layer,
                )
            )

    for segment in board.children("segment"):
        start, end = segment.child("start"), segment.child("end")
        if start is None or end is None:
            continue
        width = float(segment.get("width") or 0.0)
        line = LineString(
            [
                (float(start.value(0) or 0), float(start.value(1) or 0)),
                (float(end.value(0) or 0), float(end.value(1) or 0)),
            ]
        )
        out.append(
            _Piece(
                net=names.get(int(segment.get("net") or 0), ""),
                layers=frozenset({str(segment.get("layer") or "")}),
                shape=line.buffer(max(width, 1e-6) / 2),
                zone_area_mm2=0.0,
                layer_name="",
            )
        )

    for via in board.children("via"):
        at = via.child("at")
        if at is None:
            continue
        centre = (float(at.value(0) or 0), float(at.value(1) or 0))
        size = float(via.get("size") or 0.0)
        out.append(
            _Piece(
                net=names.get(int(via.get("net") or 0), ""),
                layers=_via_span(via, metals),
                shape=ShapelyPoint(centre).buffer(max(size, 1e-6) / 2, quad_segs=8),
                zone_area_mm2=0.0,
                layer_name="",
            )
        )

    for footprint in board.children("footprint"):
        placed = footprint.child("at")
        if placed is None:
            continue
        origin = (float(placed.value(0) or 0), float(placed.value(1) or 0))
        angle = math.radians(float(placed.value(2) or 0))
        for pad in footprint.children("pad"):
            offset = pad.child("at")
            extent = pad.child("size")
            layers = pad.child("layers")
            if offset is None or extent is None or layers is None:
                continue
            # KiCad rotates a pad's offset with its footprint. Every pad a slice
            # carries today sits at the footprint's own origin, where the rotation
            # cancels; doing it properly here is what keeps that from being a
            # coincidence the next footprint quietly breaks.
            local = (float(offset.value(0) or 0), float(offset.value(1) or 0))
            centre = (
                origin[0] + local[0] * math.cos(angle) - local[1] * math.sin(angle),
                origin[1] + local[0] * math.sin(angle) + local[1] * math.cos(angle),
            )
            carried = pad.child("net")
            out.append(
                _Piece(
                    net=names.get(
                        int(carried.value(0) or 0) if carried is not None else 0, ""
                    ),
                    layers=frozenset(str(a.value) for a in layers.atoms()),
                    shape=ShapelyPoint(centre).buffer(
                        max(float(extent.value(0) or 0), 1e-6) / 2, quad_segs=8
                    ),
                    zone_area_mm2=0.0,
                    layer_name="",
                )
            )
    return out


def _islands(pieces: Sequence[_Piece]) -> list[list[int]]:
    """Indices of the pieces, grouped into connected conductors.

    Union-find over same-net, layer-sharing, overlapping pairs. Quadratic in the
    number of pieces of one net, which on a slice is a few dozen; the whole check
    costs single-digit milliseconds beside a slice's own second of work.
    """
    parent = list(range(len(pieces)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    by_net: dict[str, list[int]] = {}
    for index, piece in enumerate(pieces):
        by_net.setdefault(piece.net, []).append(index)

    for indices in by_net.values():
        for position, i in enumerate(indices):
            for j in indices[position + 1 :]:
                if not (pieces[i].layers & pieces[j].layers):
                    continue
                if pieces[i].shape.intersects(pieces[j].shape):
                    union(i, j)

    groups: dict[int, list[int]] = {}
    for index in range(len(pieces)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


@dataclass(frozen=True, slots=True)
class ReturnPath:
    """What one slice's copper adds up to: one conductor, or several."""

    floating: tuple[Floating, ...]
    """Sheets of zone copper outside the main conductor, largest first."""
    bonded: frozenset[str]
    """The copper layers the main conductor reaches."""

    def unbonded(self, reference: str | None) -> str | None:
        """``reference`` when the main conductor does not reach it, otherwise ``None``.

        The clause behind it: a controlled-impedance number is a statement about a
        line *and its reference*, so a slice that does not connect the declared
        plane is not measuring the class it claims to be measuring. ``None`` in,
        ``None`` out -- a class with no declared reference has nothing to check.
        """
        if reference is None:
            return None
        return None if reference in self.bonded else reference


def inspect_return_path(board: SNode, metals: Sequence[str]) -> ReturnPath:
    """Walk a slice's copper once and say whether its return path is one conductor.

    One pass rather than a function per question: the union-find is the expensive
    part -- about twelve milliseconds on a slice with a hundred and thirty segments
    -- and both questions are answered off the same islands.
    """
    pieces = _pieces(board, metals)
    islands = _islands(pieces)
    main = max(
        islands,
        key=lambda island: (
            sum(pieces[i].zone_area_mm2 for i in island),
            -min(island),
        ),
        default=[],
    )
    inside = set(main)
    floating = [
        Floating(net=piece.net, layer=piece.layer_name, area_mm2=piece.zone_area_mm2)
        for index, piece in enumerate(pieces)
        if piece.zone_area_mm2 > _AREA_EPSILON and index not in inside
    ]
    bonded: set[str] = set()
    for index in main:
        bonded |= pieces[index].layers
    return ReturnPath(
        floating=tuple(sorted(floating, key=lambda f: (-f.area_mm2, f.layer, f.net))),
        bonded=frozenset(bonded),
    )

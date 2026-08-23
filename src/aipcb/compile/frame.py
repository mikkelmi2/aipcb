# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The board reference frame: one place where Y-up meets Y-down.

The source describes the board in its own frame -- millimetres, Y up, origin at the
bottom-left corner of the outline. KiCad's board space is millimetres, Y *down*,
with the board sitting wherever ``layout.origin_mm`` puts it. Every mechanical
coordinate in the source has to cross that boundary exactly once, and the whole
point of this module is that there is exactly one crossing to get wrong.

The ADR 0006 postmortem's lesson was that a sign convention nobody tested is a bug
waiting for a board revision. So the conversion is a pure function, it is used by
the emitter, the placer, the validator and ``sync-placement`` alike, and it has a
regression test built on a deliberately asymmetric outline -- one whose north edge
and south edge differ, so that getting the flip backwards cannot look right.

A :class:`BoardFrame` also carries the resolved geometry: the outline and every
cutout as closed rings in *KiCad* coordinates, canonicalised so that the same board
always emits the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aipcb.model.board import (
    Arc,
    Board,
    Line,
    Ring,
    Segment,
    rect_ring,
    ring_area,
    tessellate,
)
from aipcb.model.layout import BoardOutline

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from aipcb.netlist import Netlist

__all__ = [
    "DEFAULT_EDGE_CLEARANCE",
    "BoardFrame",
    "canonical_ring",
    "frame_for",
    "frame_from_board",
    "frame_from_legacy",
]

Point = tuple[float, float]

#: What copper must keep clear of the board edge when the source does not say.
#: KiCad's own default, so a board that states nothing behaves as KiCad expects.
DEFAULT_EDGE_CLEARANCE = 0.5


@dataclass(frozen=True, slots=True)
class BoardFrame:
    """A board's boundary, resolved into KiCad coordinates.

    ``origin`` is the KiCad position of the frame's top-left corner, which is what
    ``layout.origin_mm`` has always meant. ``flip`` says whether the source frame
    this was built from is Y-up: the new ``board:`` block is, and the legacy
    ``layout.outline`` is not.
    """

    origin: Point
    outline: Ring
    cutouts: tuple[Ring, ...] = ()
    edge_clearance: float | None = None
    source_min: Point = (0.0, 0.0)
    source_max: Point = (0.0, 0.0)
    flip: bool = True
    declared: bool = True
    """False when the outline was invented to fit whatever was placed."""

    # -- the conversion --------------------------------------------------------

    def to_kicad(self, point: Point) -> Point:
        """A point in the source's board frame, in KiCad board coordinates."""
        x, y = point
        min_x, _ = self.source_min
        _, max_y = self.source_max
        if not self.flip:
            return (self.origin[0] + x, self.origin[1] + y)
        return (self.origin[0] + x - min_x, self.origin[1] + max_y - y)

    def to_source(self, point: Point) -> Point:
        """The inverse, for reading a position back out of a board."""
        x, y = point
        min_x, _ = self.source_min
        _, max_y = self.source_max
        if not self.flip:
            return (x - self.origin[0], y - self.origin[1])
        return (x - self.origin[0] + min_x, max_y - (y - self.origin[1]))

    def rotation_to_kicad(self, degrees: float) -> float:
        """A rotation in the source frame, as KiCad's rotation.

        KiCad measures a footprint's angle counter-clockwise *as drawn*, which is
        the same sense as a Y-up source frame reads it. The flip that takes a
        position from one to the other does not apply to the angle, because the
        board is not mirrored -- only its coordinate axis is written the other way
        up. So this is the identity, and it exists to say so in one place rather
        than to be rediscovered at every call site.
        """
        return round(degrees % 360, 4)

    # -- shape -----------------------------------------------------------------

    @property
    def width(self) -> float:
        return self.source_max[0] - self.source_min[0]

    @property
    def height(self) -> float:
        return self.source_max[1] - self.source_min[1]

    @property
    def clearance(self) -> float:
        """The edge clearance to route to, defaulted."""
        return (
            self.edge_clearance
            if self.edge_clearance is not None
            else DEFAULT_EDGE_CLEARANCE
        )

    def polygon(self) -> tuple[Point, ...]:
        """The outline as a closed polygon in KiCad coordinates, arcs tessellated."""
        return tessellate(self.outline)

    def cutout_polygons(self) -> tuple[tuple[Point, ...], ...]:
        return tuple(tessellate(ring) for ring in self.cutouts)

    def kicad_bounds(self) -> tuple[Point, Point]:
        """The outline's bounding box in KiCad coordinates, as (min, max)."""
        points = self.polygon()
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys)), (max(xs), max(ys))

    @property
    def aux_origin(self) -> Point:
        """The board's bottom-left corner, in KiCad coordinates.

        This is what KiCad calls the drill/place file origin, and it is the frame
        fabrication output is written in. The bottom-left corner is the deliberate
        choice: KiCad negates Y when it plots, so an origin at the *lowest* edge of
        the board -- the largest KiCad Y -- is the only corner that makes every
        exported coordinate non-negative. Anything above it plots as a negative
        number, which consumers that parse unsigned coordinates drop silently.

        It is also the corner the rest of the world already agrees on: the source
        frame's own origin (:mod:`aipcb.compile.frame` maps source (0, 0) here) and
        the corner Gerber consumers measure from. And it is a pure function of the
        outline, so it moves only when the board does.
        """
        (min_x, _), (_, max_y) = self.kicad_bounds()
        return (min_x, max_y)

    def shape(self) -> Any:
        """The board as a Shapely polygon with a hole per cutout."""
        from shapely.geometry import Polygon as ShapelyPolygon

        return ShapelyPolygon(self.polygon(), self.cutout_polygons())

    def usable(self, margin: float) -> Any:
        """Where a footprint may sit: the board, less its cutouts, less a margin."""
        shape = self.shape()
        if margin > 0:
            shape = shape.buffer(-margin, join_style="mitre")
        return shape


# ---------------------------------------------------------------------------
# canonical form
# ---------------------------------------------------------------------------


def canonical_ring(ring: Ring) -> Ring:
    """Rotate and orient a ring so the same board always emits the same bytes.

    The ring is wound so its signed area is positive in KiCad coordinates -- which,
    with Y pointing down, is clockwise as it appears on screen -- and rotated to
    start at its lowest ``(x, y)`` vertex. Two consequences, both wanted: a
    rectangle comes out in exactly the corner order this tool has emitted since M3,
    so migrating a design to a ``board:`` block changes no bytes; and rewriting a
    polygon's vertex list from a different starting corner does not churn the diff.
    """
    if not ring:
        return ring
    if ring_area(ring) < 0:
        ring = tuple(edge.reversed() for edge in reversed(ring))
    starts = [edge.a for edge in ring]
    pivot = min(range(len(starts)), key=lambda i: (starts[i], i))
    return tuple(ring[pivot:] + ring[:pivot])


def _map_ring(ring: Ring, convert: Any) -> Ring:
    out: list[Segment] = []
    for edge in ring:
        if isinstance(edge, Arc):
            out.append(Arc(convert(edge.a), convert(edge.mid), convert(edge.b)))
        else:
            out.append(Line(convert(edge.a), convert(edge.b)))
    return tuple(out)


# ---------------------------------------------------------------------------
# building a frame
# ---------------------------------------------------------------------------


def frame_from_board(board: Board, origin: Point) -> BoardFrame:
    """Resolve a ``board:`` block into KiCad coordinates. The source frame is Y-up."""
    outline_ring, cutout_rings = board.rings()
    points = tessellate(outline_ring)
    min_x = min(p[0] for p in points)
    min_y = min(p[1] for p in points)
    max_x = max(p[0] for p in points)
    max_y = max(p[1] for p in points)

    frame = BoardFrame(
        origin=origin,
        outline=(),
        edge_clearance=board.edge_clearance,
        source_min=(min_x, min_y),
        source_max=(max_x, max_y),
        flip=True,
    )
    convert = frame.to_kicad
    return BoardFrame(
        origin=origin,
        outline=canonical_ring(_map_ring(outline_ring, convert)),
        cutouts=tuple(canonical_ring(_map_ring(r, convert)) for r in cutout_rings),
        edge_clearance=board.edge_clearance,
        source_min=(min_x, min_y),
        source_max=(max_x, max_y),
        flip=True,
    )


def frame_from_legacy(outline: BoardOutline, origin: Point, *, declared: bool = True) -> BoardFrame:
    """Resolve the pre-M9 ``layout.outline``, whose points are already Y-down.

    Kept working rather than migrated for the designer: a design that says
    ``shape: rect`` still means what it meant, and the only difference M9 makes to
    it is that the ring now goes through the same canonical form as everything
    else -- which for a rectangle is a no-op.
    """
    if outline.shape == "rect":
        width = outline.width_mm or 100.0
        height = outline.height_mm or 80.0
        ring = rect_ring((0.0, 0.0), (width, height), outline.corner_radius_mm)
        span = ((0.0, 0.0), (width, height))
    else:
        points = tuple(outline.points_mm)
        ring = tuple(
            Line(points[i], points[(i + 1) % len(points)]) for i in range(len(points))
        )
        span = (
            (min(p[0] for p in points), min(p[1] for p in points)),
            (max(p[0] for p in points), max(p[1] for p in points)),
        )

    frame = BoardFrame(
        origin=origin, outline=(), source_min=span[0], source_max=span[1], flip=False
    )
    return BoardFrame(
        origin=origin,
        outline=canonical_ring(_map_ring(ring, frame.to_kicad)),
        source_min=span[0],
        source_max=span[1],
        flip=False,
        declared=declared,
    )


def frame_for(netlist: Netlist) -> BoardFrame | None:
    """The frame a design declares, from either the new block or the old one."""
    origin = netlist.layout.origin_mm if netlist.layout else (100.0, 100.0)
    if netlist.board is not None:
        return frame_from_board(netlist.board, origin)
    outline = netlist.layout.outline if netlist.layout else None
    if outline is not None:
        return frame_from_legacy(outline, origin)
    return None


def auto_frame(width: float, height: float, origin: Point) -> BoardFrame:
    """A rectangle big enough for what was placed, for a design that declares none."""
    return frame_from_legacy(
        BoardOutline(shape="rect", width_mm=width, height_mm=height),
        origin,
        declared=False,
    )

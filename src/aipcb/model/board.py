# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The board's mechanical boundary -- layer 2's hardest constraint.

Everything else in ``layout:`` is intent the compiler is free to interpret. The
outline is not: it is the shape somebody machined, or the shape the enclosure
leaves. Cutouts are the same, which is why each carries a ``reason`` -- an agent
reading the source has to be able to tell a hole that exists for a flex tail from
routing area it could reclaim.

**The source frame is Y-up.** ``board.origin`` says so explicitly rather than
leaving it to be inferred, because KiCad's board space is Y-down and a silent
mismatch between the two is a whole class of bug that looks right until the board
comes back mirrored. The conversion lives in one place,
:mod:`aipcb.compile.frame`, and has a regression test with a deliberately
asymmetric outline.

Nothing here knows about KiCad. A ring built by this module is a closed sequence
of line and arc edges in the source's own millimetres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from aipcb.model.common import Strict

__all__ = [
    "ARC_SEGMENTS",
    "Arc",
    "ArcTo",
    "Board",
    "Cutout",
    "Line",
    "Outline",
    "Ring",
    "Segment",
    "Slot",
    "polygon_ring",
    "rect_ring",
    "ring_area",
    "tessellate",
    "vertex_point",
]

Point = tuple[float, float]

#: How many straight segments an arc is approximated by when a ring has to be a
#: polygon -- for containment tests, triangulation and packing. Arcs are emitted to
#: KiCad as arcs; this is only for the geometry the tools reason with, where 24
#: segments puts the chord error of a 2 mm fillet at under half a micron.
ARC_SEGMENTS = 24

#: Two lengths that should be equal are, if they agree to this many millimetres.
#: A tenth of a fabricator's tolerance, and far coarser than the noise a hand-typed
#: arc centre carries.
_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# ring geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line:
    """A straight edge of a ring."""

    a: Point
    b: Point

    @property
    def end(self) -> Point:
        return self.b

    def reversed(self) -> Line:
        return Line(self.b, self.a)

    def points(self) -> tuple[Point, ...]:
        return (self.a, self.b)


@dataclass(frozen=True, slots=True)
class Arc:
    """A circular edge, carried as KiCad carries one: start, a point on it, end.

    Three points rather than centre-and-angles because that is the representation
    that survives every transform this toolchain applies -- translation, the Y flip
    into KiCad space, reversal -- without a sign convention of its own.
    """

    a: Point
    mid: Point
    b: Point

    @property
    def end(self) -> Point:
        return self.b

    def reversed(self) -> Arc:
        return Arc(self.b, self.mid, self.a)

    def points(self) -> tuple[Point, ...]:
        """The arc as a polyline, start and end included."""
        centre = arc_centre(self.a, self.mid, self.b)
        if centre is None:  # pragma: no cover - three collinear points
            return (self.a, self.mid, self.b)
        cx, cy = centre
        radius = math.dist(centre, self.a)
        start = math.atan2(self.a[1] - cy, self.a[0] - cx)
        through = math.atan2(self.mid[1] - cy, self.mid[0] - cx)
        finish = math.atan2(self.b[1] - cy, self.b[0] - cx)
        sweep = _sweep(start, through, finish)
        steps = max(2, math.ceil(ARC_SEGMENTS * abs(sweep) / math.tau))
        return tuple(
            (cx + radius * math.cos(start + sweep * i / steps),
             cy + radius * math.sin(start + sweep * i / steps))
            for i in range(steps + 1)
        )


Segment = Line | Arc
#: A closed sequence of edges. ``ring[i].end == ring[i + 1].a``, and the last
#: edge's end is the first edge's start.
Ring = tuple[Segment, ...]


def _sweep(start: float, through: float, finish: float) -> float:
    """The signed angle from ``start`` to ``finish`` passing through ``through``."""
    def normalise(angle: float) -> float:
        return angle % math.tau

    ccw = normalise(finish - start)
    if normalise(through - start) <= ccw:
        return ccw
    return ccw - math.tau


def arc_centre(a: Point, mid: Point, b: Point) -> Point | None:
    """The centre of the circle through three points, or ``None`` if collinear."""
    ax, ay = a
    bx, by = mid
    cx, cy = b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay)
          + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx)
          + (cx**2 + cy**2) * (bx - ax)) / d
    return (ux, uy)


def arc_through(start: Point, end: Point, centre: Point, *, ccw: bool = True) -> Arc:
    """An arc from ``start`` to ``end`` about ``centre``, in the given direction."""
    cx, cy = centre
    radius = math.dist(centre, start)
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)
    sweep = (a1 - a0) % math.tau if ccw else -((a0 - a1) % math.tau)
    if abs(sweep) < 1e-12:
        sweep = math.tau if ccw else -math.tau
    half = a0 + sweep / 2
    return Arc(start, (cx + radius * math.cos(half), cy + radius * math.sin(half)), end)


def tessellate(ring: Ring) -> tuple[Point, ...]:
    """A ring as a closed polygon, arcs approximated. The last point is dropped."""
    points: list[Point] = []
    for edge in ring:
        for point in edge.points()[:-1]:
            points.append(point)
    return tuple(points)


def ring_area(ring: Ring) -> float:
    """The signed area of a ring, positive when it winds counter-clockwise."""
    points = tessellate(ring)
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------


class ArcTo(Strict):
    """A polygon vertex reached by an arc rather than a straight line."""

    arc_to: tuple[float, float] = Field(
        description="Where the arc ends. It starts at the previous vertex.",
    )
    center: tuple[float, float] = Field(
        description="The arc's centre. It must be equidistant from both ends.",
    )
    direction: Literal["ccw", "cw"] = Field(
        default="ccw",
        description="Which way round the centre the arc travels, in the source's "
        "Y-up frame. Counter-clockwise is the direction that rounds off the corner "
        "of a counter-clockwise polygon.",
    )


Vertex = Annotated[tuple[float, float] | ArcTo, Field(union_mode="left_to_right")]


class Outline(Strict):
    """The board edge. ``rect`` covers the common case; ``polygon`` covers the rest."""

    rect: tuple[float, float] | None = Field(
        default=None, description="Shorthand for a rectangle: `[width, height]` in mm.",
    )
    corner_radius: float = Field(
        default=0.0, ge=0, description="Fillet radius for a `rect` outline's corners.",
    )
    polygon: tuple[Vertex, ...] = Field(
        default=(),
        description="The edge as vertices, counter-clockwise, closing implicitly. "
        "A vertex is `[x, y]` or `{arc_to: [x, y], center: [cx, cy]}`.",
    )

    @field_validator("rect")
    @classmethod
    def _positive(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is not None and (v[0] <= 0 or v[1] <= 0):
            raise ValueError(f"a rect outline needs a positive width and height, got {list(v)}")
        return v

    @model_validator(mode="after")
    def _one_shape(self) -> Outline:
        if (self.rect is None) == (not self.polygon):
            raise ValueError(
                "an outline is either a `rect: [width, height]` or a `polygon:` of "
                "at least three vertices, not both and not neither"
            )
        if self.polygon:
            if len(self.polygon) < 3:
                raise ValueError(
                    f"a polygon outline needs at least 3 vertices, got {len(self.polygon)}"
                )
            if self.corner_radius:
                raise ValueError(
                    "`corner_radius` rounds a `rect`'s corners; a polygon states its "
                    "own arcs with `arc_to`"
                )
        elif self.rect is not None and self.corner_radius > min(self.rect) / 2:
            raise ValueError(
                f"a corner_radius of {self.corner_radius} mm does not fit a "
                f"{self.rect[0]} x {self.rect[1]} mm board"
            )
        return self

    def ring(self) -> Ring:
        """The edge as a closed ring of line and arc segments, in source coordinates."""
        if self.rect is not None:
            return rect_ring((0.0, 0.0), self.rect, self.corner_radius)
        return polygon_ring(self.polygon)


class Slot(Strict):
    """A rounded slot, as milled by a cutter of the stated width."""

    from_: tuple[float, float] = Field(alias="from")
    to: tuple[float, float]
    width: float = Field(gt=0, description="The cutter's diameter, in millimetres.")

    @model_validator(mode="after")
    def _has_length(self) -> Slot:
        if math.dist(self.from_, self.to) < _TOLERANCE:
            raise ValueError(
                "a slot's `from` and `to` are the same point; give it a length, or "
                "use a `rect` cutout"
            )
        return self


class Cutout(Strict):
    """A hole through the board. Mechanical law, not reclaimable routing area."""

    rect: tuple[tuple[float, float], tuple[float, float]] | None = Field(
        default=None, description="Two opposite corners: `[[x1, y1], [x2, y2]]`.",
    )
    slot: Slot | None = None
    polygon: tuple[Vertex, ...] = ()
    reason: str | None = Field(
        default=None,
        description="Why the hole is there. Strongly encouraged: it is what tells a "
        "reader the area cannot be reclaimed.",
    )

    @model_validator(mode="after")
    def _one_shape(self) -> Cutout:
        given = [n for n, v in (("rect", self.rect), ("slot", self.slot),
                                ("polygon", self.polygon or None)) if v is not None]
        if len(given) != 1:
            raise ValueError(
                "a cutout is exactly one of `rect`, `slot` or `polygon`, "
                + (f"but names {', '.join(given)}" if given else "and names none")
            )
        if self.polygon and len(self.polygon) < 3:
            raise ValueError(
                f"a polygon cutout needs at least 3 vertices, got {len(self.polygon)}"
            )
        if self.rect is not None:
            (x1, y1), (x2, y2) = self.rect
            if abs(x2 - x1) < _TOLERANCE or abs(y2 - y1) < _TOLERANCE:
                raise ValueError(
                    f"a rect cutout needs two opposite corners, got {self.rect} which "
                    "has no area"
                )
        return self

    @property
    def label(self) -> str:
        if self.rect is not None:
            return f"rect {list(self.rect[0])}-{list(self.rect[1])}"
        if self.slot is not None:
            return f"slot {list(self.slot.from_)}-{list(self.slot.to)}"
        return f"polygon of {len(self.polygon)} vertices"

    def ring(self) -> Ring:
        """The hole as a closed ring, in source coordinates."""
        if self.rect is not None:
            (x1, y1), (x2, y2) = self.rect
            origin = (min(x1, x2), min(y1, y2))
            size = (abs(x2 - x1), abs(y2 - y1))
            return rect_ring(origin, size, 0.0)
        if self.slot is not None:
            return _slot_ring(self.slot)
        return polygon_ring(self.polygon)


class Board(Strict):
    """The board's mechanical boundary, and the frame every other coordinate is in."""

    origin: Literal["bottom_left"] = Field(
        default="bottom_left",
        description="Where the source's (0, 0) is. Stated explicitly because the "
        "source is Y-up and KiCad is Y-down; the emitter converts.",
    )
    outline: Outline
    cutouts: tuple[Cutout, ...] = ()
    edge_clearance: float | None = Field(
        default=None,
        gt=0,
        description="How far copper must stay from the edge and from every cutout. "
        "Feeds both KiCad's edge-clearance design rule and the router.",
    )

    def rings(self) -> tuple[Ring, tuple[Ring, ...]]:
        """The outline's ring and every cutout's, in source coordinates."""
        return self.outline.ring(), tuple(c.ring() for c in self.cutouts)


# ---------------------------------------------------------------------------
# ring builders
# ---------------------------------------------------------------------------


def rect_ring(origin: Point, size: Point, radius: float) -> Ring:
    """A rectangle, counter-clockwise from its bottom-left corner, optionally filleted."""
    ox, oy = origin
    width, height = size
    left, right = ox, ox + width
    bottom, top = oy, oy + height
    if radius <= 0:
        corners = ((left, bottom), (right, bottom), (right, top), (left, top))
        return tuple(
            Line(corners[i], corners[(i + 1) % 4]) for i in range(4)
        )
    r = radius
    return (
        Line((left + r, bottom), (right - r, bottom)),
        arc_through((right - r, bottom), (right, bottom + r), (right - r, bottom + r)),
        Line((right, bottom + r), (right, top - r)),
        arc_through((right, top - r), (right - r, top), (right - r, top - r)),
        Line((right - r, top), (left + r, top)),
        arc_through((left + r, top), (left, top - r), (left + r, top - r)),
        Line((left, top - r), (left, bottom + r)),
        arc_through((left, bottom + r), (left + r, bottom), (left + r, bottom + r)),
    )


def _slot_ring(slot: Slot) -> Ring:
    """A stadium: two parallel edges closed by a half-circle at each end."""
    (ax, ay), (bx, by) = slot.from_, slot.to
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    r = slot.width / 2
    # The left-hand normal, so the ring comes out counter-clockwise in a Y-up frame.
    nx, ny = -uy * r, ux * r
    a_left, a_right = (ax + nx, ay + ny), (ax - nx, ay - ny)
    b_left, b_right = (bx + nx, by + ny), (bx - nx, by - ny)
    return (
        Line(a_right, b_right),
        arc_through(b_right, b_left, (bx, by)),
        Line(b_left, a_left),
        arc_through(a_left, a_right, (ax, ay)),
    )


def vertex_point(vertex: Vertex) -> Point:
    """Where a source vertex is, whether it was reached by a line or by an arc."""
    if isinstance(vertex, ArcTo):
        return (vertex.arc_to[0], vertex.arc_to[1])
    return (vertex[0], vertex[1])


def polygon_ring(vertices: tuple[Vertex, ...]) -> Ring:
    """Turn a source vertex list into a closed ring.

    A plain vertex is reached by a straight line from the one before it; an
    ``arc_to`` vertex by an arc about the centre it names. The ring closes back to
    the first vertex, which is a line unless the first entry is itself an arc.
    """
    points: list[Point] = [vertex_point(vertex) for vertex in vertices]

    ring: list[Segment] = []
    for index, vertex in enumerate(vertices):
        start = points[index - 1]
        end = points[index]
        if isinstance(vertex, ArcTo):
            centre: Point = (vertex.center[0], vertex.center[1])
            ring.append(
                arc_through(start, end, centre, ccw=vertex.direction == "ccw")
            )
        else:
            ring.append(Line(start, end))
    # `polygon_ring` builds edge *i* as "into vertex i", so the list starts with the
    # edge that closes the ring. Rotating puts it back in reading order, which is
    # what a diff of the emitted file should look like.
    return tuple(ring[1:] + ring[:1])


def arc_radii(vertex: ArcTo, start: Point) -> tuple[float, float]:
    """The two radii an ``arc_to`` implies, for the validator to compare."""
    centre: Point = (vertex.center[0], vertex.center[1])
    end: Point = (vertex.arc_to[0], vertex.arc_to[1])
    return (math.dist(start, centre), math.dist(end, centre))
